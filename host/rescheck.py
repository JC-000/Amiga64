"""Machine checker for the resource contract (runtime/rc/*.inc).

The rc files are the single source of truth for every fixed address the
project claims (docs/RESOURCES.md).  ca65/ld65 catch duplicate *names*
(prefix-registry collisions) but fail silently on address *overlaps* and on
raw literals that bypass the contract — this checker closes that gap
(the c64-lib-contract lesson: conformance must not be human review only).

Checks
  (a) regions inside one space never overlap (any owner, ``free`` included);
  (b) every region lies inside its space bounds, and the regions cover the
      space completely (unclaimed space must be *explicitly* declared
      ``free`` — no silent gaps);
  (c) every equate in an rc file lies inside the @region block it is
      declared under;
  (d) source scan: raw address literals in runtime/ + tests/runner/ that
      land in a region owned by a DIFFERENT module, in ``free`` space, in
      the ``mb`` mailbox, or in ``sys`` hardware space are violations
      unless allowlisted.  Heuristics are deliberately conservative;
      anything ambiguous is reported as UNCLASSIFIED (a warning), never
      guessed at.
  (e) host mirror cross-check: the guest-space constants duplicated in
      host/hle_env.py and host/image.py must match runtime/rc/guest.inc.
  (f) ``@under`` regions (the RAM under an I/O window, which the VIC reads
      while the CPU sees I/O): allowed only in the windows of
      ``UNDER_WINDOWS`` (space ``ram``, $D000-$DFFF), only for a registered
      module owner (FILE_OWNERS / OWNER_GLOBS: never a hardware prefix such
      as VIC); each must lie inside ONE ``sys`` region, never
      overlap another ``@under`` region, have a module owner, and every
      equate under it must carry that owner's prefix (``<owner>_``), so an
      I/O register equate cannot move into it.  They take no part in (a)/(b)
      and the scan (d) still classifies raw literals by the sys region.

Region grammar (in rc files; parsed from comments)::

    ; @space <name> $<start>-$<end>          one per file
    ; @region <owner> $<start>-$<end> <role text>
    ; @under <owner> $<start>-$<end> <role text>   RAM under a sys I/O region

Equate lines (``name = $hex``) below a ``@region``/``@under`` line belong
to it.

Usage: ``python3 -m host.rescheck [--root DIR] [-v]`` — exit 1 on violation.
Wired into ``make rescheck`` and host/tests/test_rescheck.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- What to parse / scan ---------------------------------------------------

RC_FILES = {
    "zp": "runtime/rc/zp.inc",
    "ram": "runtime/rc/ram.inc",
    "guest": "runtime/rc/guest.inc",
}

# Module owner of each scanned source file (docs/RESOURCES.md registry).
# ``sys``/``free``/``mb`` are region owners only — no file maps to them.
FILE_OWNERS = {
    "runtime/core.s": "core",
    "runtime/macros.inc": "core",
    "runtime/ops/g1_move.s": "g1",
    "runtime/ops/g2_arith.s": "g2",
    "runtime/ops/g3_logic.s": "g3",
    "runtime/ops/g4_shift.s": "g4",
    "runtime/ops/g5_flow.s": "g5",
    "runtime/ops/g6_muldiv.s": "g6",
    "runtime/ops/hle.s": "hle",
    "runtime/ops/c2v.s": "c2v",         # W7-V c2v: VIC converter v0
    "tests/runner/runner.s": "run",
    "tests/runner/run_program.s": "run",
}

# Owners by glob, for module families that grow by file (checked after the
# exact table above).  Every runtime/tests-runner source MUST resolve to an
# owner — an unregistered .s file is a violation, so new sources can never
# silently escape the scan.
OWNER_GLOBS = [
    ("runtime/ops/hle*.s", "hle"),      # hle.s, hle_dos.s (M2)
    ("runtime/ops/dosio*.s", "hle"),    # dos I/O backends (WS-B2)
]
SCANNED_GLOBS = ["runtime/*.s", "runtime/ops/*.s", "tests/runner/*.s"]

# Where @under (RAM under an I/O window) is legal: space -> inclusive ranges.
UNDER_WINDOWS = {"ram": ((0xD000, 0xDFFF),)}
# Who may own an @under region: the registered module owners.
MODULE_OWNERS = frozenset(FILE_OWNERS.values()) | {o for _, o in OWNER_GLOBS}


def source_owners(root: Path) -> tuple[dict[str, str], list[str]]:
    """Map every scanned source to its owner; also list unregistered ones."""
    owners = {rel: o for rel, o in FILE_OWNERS.items() if (root / rel).exists()}
    unregistered = []
    for pat in SCANNED_GLOBS:
        for path in sorted(root.glob(pat)):
            rel = path.relative_to(root).as_posix()
            if rel in owners:
                continue
            owner = next((o for g, o in OWNER_GLOBS
                          if Path(rel).match(g)), None)
            if owner is None:
                unregistered.append(rel)
            else:
                owners[rel] = owner
    return owners, unregistered


# Allowlist for scan findings: (path, regex matched against the stripped
# source line, reason).  Document every entry — each one is a hole in the
# checker.  Currently EMPTY: the migration left no raw foreign literals.
ALLOWLIST: list[tuple[str, str, str]] = []

# Guest constants mirrored on the host side: rc name -> (py file, py name).
HOST_MIRRORS = {
    "ABSEXECBASE_ADDR": ("host/hle_env.py", "ABSEXECBASE_ADDR"),
    "ENV_BASE": ("host/hle_env.py", "ENV_BASE"),
    "EXIT_ADDR": ("host/hle_env.py", "EXIT_ADDR"),
    "EXEC_BASE": ("host/hle_env.py", "EXEC_BASE"),
    "DOS_BASE": ("host/hle_env.py", "DOS_BASE"),
    "STACK_TOP": ("host/image.py", "STACK_TOP"),
    "PROG_BASE": ("host/image.py", "BASE_ADDR"),
    "PROG_END": ("host/image.py", "PROG_END"),
}

# --- rc-file model ----------------------------------------------------------


@dataclass
class Region:
    owner: str
    lo: int
    hi: int
    role: str
    line: int
    under: bool = False     # an @under region (RAM under a sys I/O window)


@dataclass
class Equate:
    name: str
    value: int
    line: int
    region: Region | None


@dataclass
class Space:
    name: str
    lo: int
    hi: int
    path: str
    regions: list[Region] = field(default_factory=list)
    equates: list[Equate] = field(default_factory=list)
    under: list[Region] = field(default_factory=list)

    def region_at(self, addr: int) -> Region | None:
        for r in self.regions:
            if r.lo <= addr <= r.hi:
                return r
        return None


_SPACE_RE = re.compile(r"^;\s*@space\s+(\w+)\s+\$([0-9A-Fa-f]+)-\$([0-9A-Fa-f]+)\s*$")
_UNDER_RE = re.compile(r"^;\s*@under\s+(\w+)\s+\$([0-9A-Fa-f]+)-\$([0-9A-Fa-f]+)\s+(.*)$")
_REGION_RE = re.compile(r"^;\s*@region\s+(\w+)\s+\$([0-9A-Fa-f]+)-\$([0-9A-Fa-f]+)\s+(.*)$")
_EQUATE_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*\$([0-9A-Fa-f]+)\s*(?:;.*)?$")


def parse_rc_text(text: str, path: str) -> tuple[Space | None, list[str]]:
    """Parse one rc file.  Returns (space, errors)."""
    errors: list[str] = []
    space: Space | None = None
    current: Region | None = None
    for n, raw in enumerate(text.splitlines(), 1):
        m = _SPACE_RE.match(raw.strip())
        if m:
            if space is not None:
                errors.append(f"{path}:{n}: second @space line (one per file)")
                continue
            space = Space(m.group(1), int(m.group(2), 16), int(m.group(3), 16), path)
            continue
        m = _UNDER_RE.match(raw.strip())
        if m:
            if space is None:
                errors.append(f"{path}:{n}: @under before @space")
                continue
            current = Region(m.group(1), int(m.group(2), 16), int(m.group(3), 16),
                             m.group(4).strip(), n, under=True)
            space.under.append(current)
            continue
        m = _REGION_RE.match(raw.strip())
        if m:
            if space is None:
                errors.append(f"{path}:{n}: @region before @space")
                continue
            current = Region(m.group(1), int(m.group(2), 16), int(m.group(3), 16),
                             m.group(4).strip(), n)
            space.regions.append(current)
            continue
        m = _EQUATE_RE.match(raw)
        if m:
            if space is None:
                errors.append(f"{path}:{n}: equate before @space")
                continue
            space.equates.append(Equate(m.group(1), int(m.group(2), 16), n, current))
    if space is None:
        errors.append(f"{path}: no @space declaration")
    return space, errors


def check_space(space: Space) -> list[str]:
    """Checks (a)-(c) for one parsed space."""
    errors: list[str] = []
    p = space.path

    for r in space.regions:
        if r.lo > r.hi:
            errors.append(f"{p}:{r.line}: region ${r.lo:X}-${r.hi:X} is inverted")
        if r.lo < space.lo or r.hi > space.hi:
            errors.append(
                f"{p}:{r.line}: region {r.owner} ${r.lo:X}-${r.hi:X} outside "
                f"space {space.name} ${space.lo:X}-${space.hi:X}")

    by_lo = sorted(space.regions, key=lambda r: (r.lo, r.hi))
    for a, b in zip(by_lo, by_lo[1:]):
        if b.lo <= a.hi:
            errors.append(
                f"{p}:{b.line}: region {b.owner} ${b.lo:X}-${b.hi:X} overlaps "
                f"{a.owner} ${a.lo:X}-${a.hi:X} (line {a.line})")

    # Coverage: unclaimed space must be declared 'free', never left implicit.
    cursor = space.lo
    for r in by_lo:
        if r.lo > cursor:
            errors.append(
                f"{p}: undeclared gap ${cursor:X}-${r.lo - 1:X} in space "
                f"{space.name} (declare it, e.g. as a 'free' region)")
        cursor = max(cursor, r.hi + 1)
    if cursor <= space.hi:
        errors.append(
            f"{p}: undeclared gap ${cursor:X}-${space.hi:X} at end of space "
            f"{space.name}")

    # (f) @under regions: RAM under exactly one sys I/O region.
    for u in space.under:
        where = f"{p}:{u.line}: @under {u.owner} ${u.lo:X}-${u.hi:X}"
        if u.lo > u.hi:
            errors.append(f"{where} is inverted")
        if u.owner not in MODULE_OWNERS:
            errors.append(f"{where}: owner '{u.owner}' is not a registered "
                          f"module (FILE_OWNERS/OWNER_GLOBS)")
        if not any(lo <= u.lo and u.hi <= hi
                   for lo, hi in UNDER_WINDOWS.get(space.name, ())):
            errors.append(f"{where} (space {space.name}) is outside the "
                          f"UNDER_WINDOWS: @under is only allowed in space ram "
                          f"$D000-$DFFF")
        if not any(r.owner == "sys" and r.lo <= u.lo and u.hi <= r.hi
                   for r in space.regions):
            errors.append(f"{where} does not lie inside one sys region")
    by_lo_u = sorted(space.under, key=lambda r: (r.lo, r.hi))
    for a, b in zip(by_lo_u, by_lo_u[1:]):
        if b.lo <= a.hi:
            errors.append(
                f"{p}:{b.line}: @under {b.owner} ${b.lo:X}-${b.hi:X} overlaps "
                f"@under {a.owner} ${a.lo:X}-${a.hi:X} (line {a.line})")
    for e in space.equates:
        if e.region is not None and e.region.under and \
                not e.name.startswith(e.region.owner + "_"):
            errors.append(
                f"{p}:{e.line}: equate {e.name} under @under {e.region.owner} "
                f"lacks the owner prefix '{e.region.owner}_' (I/O register "
                f"equates stay under sys)")

    for e in space.equates:
        if e.region is None:
            errors.append(f"{p}:{e.line}: equate {e.name} not under any @region")
        elif not (e.region.lo <= e.value <= e.region.hi):
            errors.append(
                f"{p}:{e.line}: equate {e.name} = ${e.value:X} outside its "
                f"@region {e.region.owner} ${e.region.lo:X}-${e.region.hi:X}")
    return errors


# --- Source scan (check d) --------------------------------------------------

_MNEMONICS = ("adc and asl bit cmp cpx cpy dec eor inc jmp jsr lda ldx ldy "
              "lsr ora rol ror sbc sta stx sty").split()
_ZP_INSN_RE = re.compile(
    r"^(?:[@\w]+:)?\s*(?P<op>" + "|".join(_MNEMONICS) + r")\s+"
    r"\$(?P<val>[0-9A-Fa-f]{1,2})\s*(?:,\s*[xyXY])?$", re.IGNORECASE)
_HEX_LIT_RE = re.compile(r"\$([0-9A-Fa-f]{3,8})\b")
_SMALL_EQU_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*\$([0-9A-Fa-f]{1,2})\s*$")


def _strip_comment(line: str) -> str:
    return line.split(";", 1)[0].rstrip()


@dataclass
class Finding:
    path: str
    line: int
    text: str
    message: str
    fatal: bool

    def __str__(self) -> str:
        kind = "VIOLATION" if self.fatal else "UNCLASSIFIED"
        return f"{kind}: {self.path}:{self.line}: {self.message}\n    {self.text.strip()}"


def _allowlisted(path: str, stripped: str) -> bool:
    for allow_path, pattern, _reason in ALLOWLIST:
        if path.endswith(allow_path) and re.search(pattern, stripped):
            return True
    return False


def _judge(spaces: dict[str, Space], space_name: str, value: int, owner: str,
           path: str, n: int, stripped: str, what: str) -> Finding | None:
    space = spaces.get(space_name)
    if space is None:
        return None
    region = space.region_at(value)
    if _allowlisted(path, stripped):
        return None
    if region is None:
        return Finding(path, n, stripped,
                       f"{what} ${value:X} in UNDECLARED {space_name} space", True)
    if region.owner == owner:
        return None
    return Finding(
        path, n, stripped,
        f"{what} ${value:X} hits {space_name} region '{region.owner}' "
        f"${region.lo:X}-${region.hi:X} ({region.role}) but this file's "
        f"owner is '{owner}'", True)


_VALUE_TAG_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\$?[0-9A-Fa-f]+\s*;.*@value\b")


def scan_source(path: str, text: str, owner: str,
                spaces: dict[str, Space],
                corpus: str | None = None) -> list[Finding]:
    """Scan one assembly source for raw address literals.

    Heuristics (low-false-positive by design):
      - comments stripped; immediates (#...), lo/hi selectors (<$ / >$) and
        offset arithmetic (+ $x / - $x) skipped;
      - 1-2 digit literals count only as direct ZP addressing when they are
        the sole operand of a load/store/rmw mnemonic;
      - a 1-2 digit *equate* counts as a ZP claim only when the symbol is
        used in a memory-addressing context somewhere in the scanned
        corpus (shared includes: usage may be in another file); a symbol
        used only as an immediate is a plain value; no use at all is
        reported UNCLASSIFIED;
      - 3-4 digit literals in $0400-$DFFF are checked against the ram space;
      - 5-6 digit literals are checked against the guest space;
      - 7-8 digit literals are 32-bit data values, ignored.
    """
    findings: list[Finding] = []
    lines = text.splitlines()
    body = "\n".join(_strip_comment(ln) for ln in lines)
    if corpus is None:
        corpus = body

    for n, raw in enumerate(lines, 1):
        stripped = _strip_comment(raw)
        if not stripped:
            continue

        # `NAME = literal ; @value` declares a plain value (size, offset,
        # flag bits), never an address.  Checked: using the name as a
        # memory operand anywhere in the corpus is a violation.
        tv = _VALUE_TAG_RE.match(raw)
        if tv:
            name = tv.group(1)
            if re.search(r"(?:" + "|".join(_MNEMONICS) + r")\s+\(?"
                         + re.escape(name) + r"\b", corpus, re.IGNORECASE):
                findings.append(Finding(
                    path, n, stripped,
                    f"{name} is tagged @value but used as a memory operand",
                    True))
            continue

        m = _ZP_INSN_RE.match(stripped.strip())
        if m:
            v = int(m.group("val"), 16)
            f = _judge(spaces, "zp", v, owner, path, n, stripped,
                       "direct ZP operand")
            if f:
                findings.append(f)
            continue

        m = _SMALL_EQU_RE.match(stripped)
        if m:
            name, v = m.group(1), int(m.group(2), 16)
            # ZP claim only if the name is used as a memory operand later on.
            mem_use = re.search(
                r"(?:" + "|".join(_MNEMONICS) + r")\s+\(?" + re.escape(name)
                + r"\b(?!\s*[+-])", corpus, re.IGNORECASE)
            imm_use = re.search(r"#\s*[<>]?" + re.escape(name) + r"\b", corpus)
            if mem_use:
                f = _judge(spaces, "zp", v, owner, path, n, stripped,
                           f"ZP equate {name}")
                if f:
                    findings.append(f)
            elif not imm_use:
                findings.append(Finding(
                    path, n, stripped,
                    f"small equate {name} = ${v:02X}: cannot classify "
                    "(address or value?) — move it to runtime/rc/ if it is "
                    "an address", False))
            continue

        for m in _HEX_LIT_RE.finditer(stripped):
            start = m.start()
            prev = stripped[:start].rstrip()[-1:] or ""
            if prev in "#+-<>":
                continue
            digits = m.group(1)
            v = int(digits, 16)
            if len(digits) <= 4:
                if 0x0400 <= v <= 0xDFFF:
                    f = _judge(spaces, "ram", v, owner, path, n, stripped,
                               "absolute literal")
                    if f:
                        findings.append(f)
            elif len(digits) <= 6:
                f = _judge(spaces, "guest", v, owner, path, n, stripped,
                           "guest-address literal")
                if f:
                    findings.append(f)
            # 7-8 digits: 32-bit data value, not an address — ignore.
    return findings


# --- Host mirror cross-check (check e) --------------------------------------

_PY_CONST_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*(?:#.*)?$")


def parse_py_constants(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = _PY_CONST_RE.match(line.strip())
        if m:
            out[m.group(1)] = int(m.group(2), 0)
    return out


def check_host_mirrors(root: Path, guest: Space) -> list[str]:
    errors: list[str] = []
    rc_vals = {e.name: e.value for e in guest.equates}
    py_cache: dict[str, dict[str, int]] = {}
    for rc_name, (py_file, py_name) in HOST_MIRRORS.items():
        if py_file not in py_cache:
            fp = root / py_file
            py_cache[py_file] = (
                parse_py_constants(fp.read_text()) if fp.exists() else {})
        py_vals = py_cache[py_file]
        if rc_name not in rc_vals:
            errors.append(f"guest.inc: mirrored constant {rc_name} missing")
            continue
        if py_name not in py_vals:
            errors.append(f"{py_file}: mirrored constant {py_name} not found")
            continue
        if py_vals[py_name] != rc_vals[rc_name]:
            errors.append(
                f"mirror mismatch: {py_file} {py_name}=0x{py_vals[py_name]:X} "
                f"!= rc/guest.inc {rc_name}=${rc_vals[rc_name]:X}")
    # ENV_END is exclusive: must equal the hle env region end + 1.
    hle_env = next((r for r in guest.regions if r.owner == "hle"), None)
    env_end = py_cache.get("host/hle_env.py", {}).get("ENV_END")
    if hle_env and env_end is not None and env_end != hle_env.hi + 1:
        errors.append(
            f"host/hle_env.py ENV_END=0x{env_end:X} != guest hle region end "
            f"${hle_env.hi:X} + 1")
    return errors


# --- Driver -----------------------------------------------------------------


def load_spaces(root: Path) -> tuple[dict[str, Space], list[str]]:
    spaces: dict[str, Space] = {}
    errors: list[str] = []
    for name, rel in RC_FILES.items():
        fp = root / rel
        if not fp.exists():
            errors.append(f"{rel}: missing rc file")
            continue
        space, errs = parse_rc_text(fp.read_text(), rel)
        errors.extend(errs)
        if space is not None:
            if space.name != name:
                errors.append(f"{rel}: @space '{space.name}', expected '{name}'")
            spaces[name] = space
    return spaces, errors


def run_checks(root: Path, verbose: bool = False) -> int:
    spaces, errors = load_spaces(root)
    for space in spaces.values():
        errors.extend(check_space(space))

    owners, unregistered = source_owners(root)
    errors.extend(f"{rel}: source file has no registered owner "
                  f"(add it to FILE_OWNERS/OWNER_GLOBS in host/rescheck.py)"
                  for rel in unregistered)
    sources = {rel: (root / rel).read_text() for rel in owners}
    corpus = "\n".join(
        _strip_comment(ln) for text in sources.values()
        for ln in text.splitlines())
    findings: list[Finding] = []
    for rel, text in sources.items():
        findings.extend(
            scan_source(rel, text, owners[rel], spaces, corpus))

    if "guest" in spaces:
        errors.extend(check_host_mirrors(root, spaces["guest"]))

    fatal = errors + [str(f) for f in findings if f.fatal]
    warn = [str(f) for f in findings if not f.fatal]

    for msg in fatal:
        print(f"rescheck: ERROR: {msg}" if not msg.startswith("VIOLATION")
              else f"rescheck: {msg}")
    for msg in warn:
        print(f"rescheck: {msg}")

    n_regions = sum(len(s.regions) for s in spaces.values())
    n_equates = sum(len(s.equates) for s in spaces.values())
    if fatal:
        print(f"rescheck: FAIL — {len(fatal)} violation(s), "
              f"{len(warn)} unclassified")
        return 1
    print(f"rescheck: OK — {len(spaces)} spaces, {n_regions} regions, "
          f"{n_equates} equates, {len(sources)} sources scanned, "
          f"{len(warn)} unclassified")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path,
                    default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: auto)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    return run_checks(args.root, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
