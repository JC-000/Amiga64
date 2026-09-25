"""AROS reference oracle: run the vbcc programs on a real AmigaOS-compatible
system (the AROS m68k ROM in FS-UAE) and diff against Amiga64.

Usage:
    python3 -m tests.aros_oracle --prg build/<pkg>/runner.prg
        [--only NAME[,NAME]] [--timeout SECS] [--json FILE] [--strict]
    python3 -m tests.aros_oracle --table ...   # vs run_vbcc's expectations

For each C program registered in ``tests.run_vbcc.programs()`` (imported,
never copied), the same Hunk binary (compiled by ``run_vbcc.compile_c``)
runs twice:

* **Amiga64** — with ``--prg``, in VICE through ``run_vbcc.stage_and_run``
  (screen, D0, halt reason, RAMDISK files read back).  With ``--table``, the
  run_vbcc expectations stand in for it (they are what the gate proves the
  runner produces).
* **AROS** — in FS-UAE (A500, 68000, the pinned AROS ROM from
  ``tools/aros/fetch.sh``, headless, warp) booting a host directory as DH0:
  whose ``S:Startup-Sequence`` runs the program with ``<NIL: >`` a file,
  records ``$RC`` and copies RAM: back to the host.  Every run has a
  wall-clock timeout.

Comparison (docs/ORACLE.md "What is compared"): AROS's stdout is rendered
through a host model of ``hle_putc`` (40x25, lazy scroll, control bytes
dropped, >= $80 -> '?', no case), so both sides are compared at the C64
screen's resolution; then the exit code, whether each side finished, and
the program's ``expect_files`` (RAMDISK = AROS's current dir + RAM:).

Verdicts: MATCH, RETRY-MATCH (matched only after a boot-script stall was
re-run), DIFF (every difference listed; each line of a known DIFF carries
its pinned docs/ORACLE.md category, any other line is NEW and the program
UNTRIAGED), SKIP (with a written reason).
The oracle is never part of the gate: without FS-UAE, the ROM cache (or
one that fails verification) or vbcc it prints a SKIP line and exits 0.
Exit code: 1 on an untriaged DIFF or stale triage (with --strict also on
any DIFF or RETRY-MATCH), else 0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field

from tests import run_vbcc
from tests.run_vbcc import SCREEN_COLS, SCREEN_ROWS, SPACE, Program

PROJECT_ROOT = run_vbcc.PROJECT_ROOT
MANIFEST = os.path.join(PROJECT_ROOT, "tools", "aros", "manifest.txt")
CACHE_ROOT = os.environ.get("AMIGA64_AROS_CACHE",
                            os.path.expanduser("~/.cache/amiga64/aros"))
FS_UAE = os.environ.get("FS_UAE",
                        "/Applications/FS-UAE.app/Contents/MacOS/fs-uae")
SCREEN_SIZE = SCREEN_COLS * SCREEN_ROWS

# SKIP needs a written reason, and only these kinds (WAVE6_BRIEF C2).
SKIP: dict[str, str] = {
    "tspin": "timing-dependent: counts busy-loop spins until the DateStamp "
             "tick moves; the count is a function of CPU speed vs the tick "
             "source, not of the API",
}

# Known DIFFs, triaged in docs/ORACLE.md.  Each triaged program pins its
# EXACT diff lines, each with its category; only two volatile values are
# patterns ({HEX}: AROS's ThisTask address, {UNIXTIME}: the host clock).
# A diff line not pinned here, or a pinned line that no longer appears,
# makes the program UNTRIAGED (exit 1).  Triage never turns a DIFF into a
# MATCH: known DIFFs are still reported as DIFF.
ARTIFACT = "artifact"                 # environment / layout / clock / white-box
HLE_LIMIT = "HLE limitation"          # Amiga64 departs from AmigaOS knowingly
AROS_DIV = "AROS divergence"          # AROS departs from AmigaOS 3.x
OUR_BUG = "our bug"
CATEGORIES = (ARTIFACT, HLE_LIMIT, AROS_DIV, OUR_BUG)
VOLATILE = {"{HEX}": "[0-9A-F]{1,8}", "{UNIXTIME}": "[0-9]{9,10}"}


@dataclass(frozen=True)
class Triage:
    note: str
    lines: tuple[tuple[str, str], ...]    # (category, expected diff line)


def _stress(rej: str, seed: int, maxs: str) -> Triage:
    return Triage(
        "white-box check of the HLE heap at fixed addresses ($400, "
        "$100000): on AROS the first structural check fails",
        ((ARTIFACT, "exit code: Amiga64 0, AROS 100"),
         (ARTIFACT, "row 0: Amiga64 |STRESS OK| AROS |FAIL 100 TOP|"),
         (ARTIFACT, "row 1: Amiga64 |TOP0=1000D0 HD0=0| AROS |A=C01118 B=0|"),
         (ARTIFACT, f"row 2: Amiga64 |{rej}| AROS |OP=0 SEED={seed}|"),
         (ARTIFACT, f"row 3: Amiga64 |{maxs}| AROS ||")))


TRIAGE: dict[str, Triage] = {
    "sysbase": Triage(
        "the fixture pins the HLE's ExecBase/DOSBase; AROS reports V51/V50 "
        "where AmigaOS 3.1 (and Amiga64) say V40",
        ((ARTIFACT, "row 0: Amiga64 |THISTASK=A40| AROS |THISTASK={HEX}|"),
         (HLE_LIMIT, "row 2: Amiga64 |MAXLOCMEM=200000| AROS |MAXLOCMEM=100000|"),
         (AROS_DIV, "row 3: Amiga64 |EXEC VER=40| AROS |EXEC VER=51|"),
         (AROS_DIV, "row 4: Amiga64 |DOS VER=40| AROS |DOS VER=50|"),
         (AROS_DIV, "row 6: Amiga64 |DOS V41 NULL| AROS |DOS V41 OK|"),
         (AROS_DIV, "row 7: Amiga64 |DOS V50 NULL| AROS |DOS V50 OK|"),
         (HLE_LIMIT, "row 9: Amiga64 |UTILITY NULL| AROS |UTILITY OK|"),
         (HLE_LIMIT, "row 10: Amiga64 |LIBLIST EMPTY| AROS |LIBLIST BAD|"))),
    "time": Triage(
        "clock source: the HLE clock starts at 1978-01-01, AROS reads the "
        "host time; the same=1 row matches",
        ((ARTIFACT, "row 0: Amiga64 |TIME=252460800| AROS |TIME={UNIXTIME}|"),)),
    "system": Triage(
        "HLE SystemTagList runs nothing (-1, docs/HLE.md); AROS runs "
        "'echo hi', whose output lands before the parent's buffered stdio",
        ((HLE_LIMIT, "row 0: Amiga64 |SYSTEM(NULL)=1| AROS |HI|"),
         (HLE_LIMIT, "row 1: Amiga64 |SYSTEM(CMD)=-1| AROS |SYSTEM(NULL)=1|"),
         (HLE_LIMIT, "row 2: Amiga64 || AROS |SYSTEM(CMD)=0|"))),
    "readitem": Triage(
        "ReadItem is not implemented in the HLE (the fixture pins the "
        "sentinel-slot halt); AROS: ITEM_NOTHING on NIL:",
        ((HLE_LIMIT, "Amiga64 did not exit: HALT_ALINE in the "
                     "dos.library/ReadItem (LVO -810) slot"),
         (HLE_LIMIT, "exit code: Amiga64 None, AROS 0"),
         (HLE_LIMIT, "row 0: Amiga64 || AROS |READITEM=0|"))),
    "putstr": Triage(
        "PutStr is not implemented in the HLE (the halt is pinned); AROS's "
        "PutStr is buffered, so its line follows the stdio lines",
        ((HLE_LIMIT, "Amiga64 did not exit: HALT_ALINE in the "
                     "dos.library/PutStr (LVO -948) slot"),
         (HLE_LIMIT, "exit code: Amiga64 None, AROS 3"),
         (HLE_LIMIT, "row 1: Amiga64 || AROS |AFTER|"),
         (HLE_LIMIT, "row 2: Amiga64 || AROS |PUTSTR WORKS|"))),
    "leak_open": Triage(
        "the HLE has 8 file-handle slots (docs/HLE_DOS.md); AROS opens all 10",
        ((HLE_LIMIT, "row 0: Amiga64 |OPENED 8| AROS |OPENED 10|"),)),
    "fio_big": Triage(
        "needs Amiga64's heap layout (a buffer across guest $180000); on "
        "AROS the placement malloc fails, exit 10 before any I/O",
        ((ARTIFACT, "exit code: Amiga64 0, AROS 10"),
         (ARTIFACT, "row 0: Amiga64 |STRADDLE=1 ODD=1| AROS |STRADDLE=0 ODD=1|"),
         (ARTIFACT, "row 1: Amiga64 |READ 3000 BAD=0 EOF=1| AROS ||"),
         (ARTIFACT, "file big.bin: Amiga64 3000 bytes "
                    + repr(bytes((i * 7 + (i >> 8) + 1) & 0xFF
                                 for i in range(32)))
                    + ", AROS 0 bytes b''"))),
    "fio_reuse": Triage(
        "the RAMDISK has 32 directory entries (docs/HLE_DOS.md); AROS RAM: "
        "does not fill up, so the loop stops at its cap of 41",
        ((HLE_LIMIT, "exit code: Amiga64 0, AROS 11"),
         (HLE_LIMIT, "row 0: Amiga64 |CREATED=32| AROS |CREATED=41|"),
         (HLE_LIMIT, "row 3: Amiga64 |MORE=NULL| AROS |MORE=OPEN|"),
         (HLE_LIMIT, "file more: Amiga64 absent, AROS 0 bytes b''"))),
    "heap_stress1": _stress("REJ=3 OOM=0 DEL=4", 6, "MAXFREE=24A0 MAXBLK=1548"),
    "heap_stress2": _stress("REJ=1 OOM=4 DEL=3", 7,
                            "MAXFREE=311518 MAXBLK=2D4AF0"),
}


def _pattern(line: str) -> re.Pattern:
    rx = re.escape(line)
    for tok, sub in VOLATILE.items():
        rx = rx.replace(re.escape(tok), sub)
    return re.compile(rx)


def triage_check(name: str, diffs: list[str]
                 ) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Match *diffs* against the program's pinned triage: (known lines with
    their category, new lines, pinned lines that did not appear)."""
    t = TRIAGE.get(name)
    pinned = list(t.lines) if t else []
    known, new = [], []
    for d in diffs:
        for i, (cat, line) in enumerate(pinned):
            if _pattern(line).fullmatch(d):
                known.append((d, cat))
                del pinned[i]
                break
        else:
            new.append(d)
    return known, new, [line for _cat, line in pinned]


# --- the comparison model ------------------------------------------------------

def _render(data: bytes, cell) -> list:
    """Host model of hle_putc (runtime/ops/hle.s): print *data* on a blank
    40x25 screen; each printable byte c is stored as cell(c)."""
    blank = cell(SPACE)
    scr = [blank] * SCREEN_SIZE
    cur = col = 0

    def ensure_row():
        nonlocal cur, col
        if cur >= SCREEN_SIZE:            # lazy scroll
            scr[:SCREEN_SIZE - SCREEN_COLS] = scr[SCREEN_COLS:]
            scr[SCREEN_SIZE - SCREEN_COLS:] = [blank] * SCREEN_COLS
            cur, col = SCREEN_SIZE - SCREEN_COLS, 0

    for c in data:
        if c == 0x0A:
            ensure_row()
            cur += SCREEN_COLS - col
            col = 0
            continue
        if c < 0x20:
            continue
        ensure_row()
        scr[cur] = cell(c)
        cur += 1
        col = (col + 1) % SCREEN_COLS
    return scr


def render_screen(data: bytes) -> bytes:
    """The C64 screen codes the runner would show after printing *data*."""
    return bytes(_render(data, run_vbcc.ascii_to_screen))


def render_text(data: bytes) -> list[str]:
    """The same 25 rows with the printed characters, case kept (>= $80 is
    '?', as hle_putc prints it), trailing blanks stripped."""
    cells = _render(data, lambda c: chr(c) if c < 0x80 else "?")
    return ["".join(cells[r * SCREEN_COLS:(r + 1) * SCREEN_COLS]).rstrip()
            for r in range(SCREEN_ROWS)]


@dataclass
class Outcome:
    screen: bytes                     # SCREEN_SIZE screen codes
    exit_code: int | None             # u32; None when it did not exit
    halt: str                         # "HALT_OK" = exited normally
    files: dict[str, bytes | None]    # lowercased name -> contents / absent
    wall: float = 0.0
    stdout: bytes | None = None       # AROS only: the raw output
    text: list[str] | None = None     # rows with case (not from a C64 screen)
    stalled: bool = False             # AROS: the program exited (RC recorded)
                                      # but the boot script timed out after it


def table_outcome(p: Program) -> Outcome:
    """What run_vbcc asserts the runner produces for *p*."""
    halt = run_vbcc.HALT_REASONS[p.reason]
    if p.where:
        halt += f" in the {p.where} slot"
    text = [ln[:SCREEN_COLS].rstrip() for ln in p.lines]
    return Outcome(run_vbcc.expected_screen(p.lines), p.exit_code, halt,
                   {k.lower(): v for k, v in (p.expect_files or {}).items()},
                   text=text + [""] * (SCREEN_ROWS - len(text)))


def aros_outcome(stdout: bytes, rc: int | None, files: dict[str, bytes],
                 timed_out: bool, wall: float, why: str = "") -> Outcome:
    halt = "HALT_OK"
    if timed_out:
        why = why or (f" (the program exited, RC={rc}; the boot script "
                      f"stalled after it)" if rc is not None
                      else " (the program did not exit)")
        halt = f"AROS run timed out after {wall:.0f}s{why}"
    elif rc is None:
        halt = f"AROS run left no exit code{why}"
    return Outcome(render_screen(stdout),
                   None if rc is None else rc & 0xFFFF_FFFF, halt,
                   {k.lower(): v for k, v in files.items()}, wall, stdout,
                   render_text(stdout), stalled=timed_out and rc is not None)


def _row(screen: bytes, r: int) -> str:
    return run_vbcc.screen_to_text(screen[r * SCREEN_COLS:(r + 1) * SCREEN_COLS])


def _desc(data: bytes | None) -> str:
    return "absent" if data is None else f"{len(data)} bytes {data[:32]!r}"


def compare(a64: Outcome, aros: Outcome) -> list[str]:
    """Every difference between the two outcomes ([] = MATCH)."""
    diffs = []
    if a64.halt != "HALT_OK":
        diffs.append(f"Amiga64 did not exit: {a64.halt}")
    if aros.halt != "HALT_OK":
        diffs.append(aros.halt)
    if a64.exit_code != aros.exit_code:       # None = did not exit
        diffs.append(f"exit code: Amiga64 {a64.exit_code}, "
                     f"AROS {aros.exit_code}")
    for r in range(SCREEN_ROWS):
        a, b = _row(a64.screen, r), _row(aros.screen, r)
        if a64.screen[r * SCREEN_COLS:(r + 1) * SCREEN_COLS] != \
                aros.screen[r * SCREEN_COLS:(r + 1) * SCREEN_COLS]:
            diffs.append(f"row {r}: Amiga64 |{a}| AROS |{b}|")
        elif a64.text and aros.text and a64.text[r] != aros.text[r]:
            diffs.append(f"row {r} (case): Amiga64 |{a64.text[r]}| "
                         f"AROS |{aros.text[r]}|")
    for name, want in a64.files.items():
        got = aros.files.get(name.lower())
        if got != want:
            diffs.append(f"file {name}: Amiga64 {_desc(want)}, "
                         f"AROS {_desc(got)}")
    return diffs


def verdict(a64: Outcome, aros: Outcome) -> tuple[str, list[str]]:
    diffs = compare(a64, aros)
    return ("DIFF" if diffs else "MATCH"), diffs


# --- the pinned ROM cache ----------------------------------------------------

@dataclass
class PinnedFile:
    path: str
    member: str
    sha256: str


@dataclass
class Manifest:
    url: str
    sha256: str
    files: list[PinnedFile] = field(default_factory=list)


def read_manifest(path: str) -> Manifest:
    m = None
    files = []
    with open(path) as f:
        for line in f:
            parts = line.strip().split("|")
            if parts[0] == "archive" and len(parts) == 3:
                m = Manifest(parts[1], parts[2])
            elif parts[0] == "file" and len(parts) == 4:
                files.append(PinnedFile(*parts[1:]))
    if m is None or not files:
        raise ValueError(f"bad manifest {path}")
    m.files = files
    return m


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def find_cache(manifest: str = MANIFEST, root: str = CACHE_ROOT
               ) -> tuple[str | None, str]:
    """(cache dir, "") when every pinned file is present and verifies,
    else (None, why).  Fails closed: a hash mismatch is never used."""
    try:
        m = read_manifest(manifest)
    except (OSError, ValueError) as e:
        return None, f"manifest unreadable: {e}"
    d = os.path.join(root, m.sha256)
    if not os.path.isdir(d):
        return None, f"no AROS cache at {d} (run tools/aros/fetch.sh)"
    for pf in m.files:
        p = os.path.join(d, pf.path)
        if not os.path.isfile(p):
            return None, f"{pf.path} missing in {d} (run tools/aros/fetch.sh)"
        if _sha256(p) != pf.sha256:
            return None, (f"{pf.path} SHA256 mismatch in {d}: refusing to use "
                          f"it (delete the directory, re-run tools/aros/fetch.sh)")
    return d, ""


# --- AROS in FS-UAE ------------------------------------------------------------

ORACLE_DIR = os.path.join(PROJECT_ROOT, "tests", "oracle")


def _template(name: str) -> str:
    """tests/oracle/<name>.in without its '#' comment lines."""
    with open(os.path.join(ORACLE_DIR, name + ".in")) as f:
        return "".join(ln for ln in f if not ln.startswith("#"))


def _stage_files(p: Program, wdir: str) -> bool:
    """Write the program's RAMDISK files to *wdir*; True if there were any."""
    files = p.files or {}
    items = ([(f.name, f.data) for f in files] if isinstance(files, list)
             else list(files.items()))
    for name, data in items:
        with open(os.path.join(wdir, name), "wb") as f:
            f.write(data)
    return bool(items)


def _read_view(d: str) -> dict[str, bytes]:
    """RAM: as copied back to the host: {lowercased name: contents}."""
    view: dict[str, bytes] = {}
    for n in sorted(os.listdir(d)):
        p = os.path.join(d, n)
        if not n.endswith(".uaem") and os.path.isfile(p):
            with open(p, "rb") as f:
                view[n.lower()] = f.read()
    return view


def run_aros(p: Program, blob: bytes, cache: str, fs_uae: str,
             timeout: float, keep: str | None = None,
             manifest: str = MANIFEST) -> Outcome:
    """Boot AROS in FS-UAE, run *blob* once, collect its outcome.  Only
    the manifest's pinned (and find_cache-verified) C/ files are used."""
    commands = [f.path for f in read_manifest(manifest).files
                if f.path.startswith("C/")]
    work = tempfile.mkdtemp(prefix=f"aros-{p.name}-")
    try:
        hd = os.path.join(work, "hd")
        for sub in ("S", "C", "bin", "w", "ram", "o", "env", "t"):
            os.makedirs(os.path.join(hd, sub))
        for cmd in commands:
            dst = os.path.join(hd, cmd)
            shutil.copy(os.path.join(cache, cmd), dst)
            os.chmod(dst, 0o755)
        with open(os.path.join(hd, "bin", p.name), "wb") as f:
            f.write(blob)
        os.chmod(os.path.join(hd, "bin", p.name), 0o755)
        staged = _stage_files(p, os.path.join(hd, "w"))
        with open(os.path.join(hd, "S", "Startup-Sequence"), "w") as f:
            f.write(_template("Startup-Sequence").format(
                name=p.name, stage="Copy DH0:w/#? RAM: QUIET\n" if staged
                else ""))
        cfg = os.path.join(work, "run.fs-uae")
        with open(cfg, "w") as f:
            f.write(_template("a500.fs-uae").format(
                rom=os.path.join(cache, "aros-rom.bin"),
                ext=os.path.join(cache, "aros-ext.bin"),
                hd=hd, base=os.path.join(work, "base")))
        done = os.path.join(hd, "o", "done.txt")
        t0 = time.monotonic()
        with open(os.path.join(work, "fs-uae.log"), "w") as log:
            proc = subprocess.Popen([fs_uae, cfg], cwd=work, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL)
            why = ""
            try:
                while not os.path.exists(done):
                    if time.monotonic() - t0 > timeout:
                        break
                    if proc.poll() is not None:
                        why = f" (FS-UAE exited {proc.returncode})"
                        break
                    time.sleep(0.1)
                time.sleep(0.2)           # let the host fs flush done.txt's peers
            finally:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        wall = time.monotonic() - t0
        finished = os.path.exists(done)

        def slurp(name: str) -> bytes | None:
            q = os.path.join(hd, "o", name)
            return open(q, "rb").read() if os.path.exists(q) else None

        stdout = slurp("stdout.txt") or b""
        rc_txt = (slurp("rc.txt") or b"").strip()
        try:
            rc = int(rc_txt)
        except ValueError:
            rc = None
        return aros_outcome(stdout, rc, _read_view(os.path.join(hd, "ram")),
                            not finished, wall, why)
    finally:
        if keep:
            dst = os.path.join(keep, p.name)
            shutil.rmtree(dst, ignore_errors=True)
            # hd/C (AROS commands) stays out: only the cache holds AROS
            shutil.copytree(work, dst,
                            ignore=shutil.ignore_patterns("base", "C"))
        shutil.rmtree(work, ignore_errors=True)


# --- Amiga64 in VICE -----------------------------------------------------------

def a64_outcomes(prg: str, progs: list[Program], blobs: dict[str, bytes],
                 timeout: float) -> tuple[dict[str, Outcome], dict[str, str]]:
    """Run *progs* in one VICE session exactly as run_vbcc does; returns
    (outcomes, skips) keyed by program name."""
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes)
    from host import hle_dos_env
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_dos import Guest
    from tests.run_program import Session
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    run_vbcc.POOL_WIRED = run_vbcc.pool_wiring(prg, labels)
    outs: dict[str, Outcome] = {}
    skips: dict[str, str] = {}
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=timeout)
            for p in progs:
                if p.pools and run_vbcc.POOL_WIRED is False:
                    skips[p.name] = "Amiga64 build: W5-H pool wiring absent"
                    continue
                if p.needs_dos and run_vbcc.hle_dos_is_stub(sess):
                    skips[p.name] = "Amiga64 build: hle_dos.s is the stub"
                    continue
                t0 = time.monotonic()
                res, _manifest, why = run_vbcc.stage_and_run(
                    sess, p.name, blobs[p.name], p.budget or 0, poison=True,
                    files=p.files)
                wall = time.monotonic() - t0
                ok = res.halt != 0 and res.timeout == 0 and res.reason == 0
                screen = read_bytes(target.transport, run_vbcc.SCREEN,
                                    SCREEN_SIZE)
                files: dict[str, bytes | None] = {}
                if p.expect_files:
                    ents = hle_dos_env.parse_directory(Guest(sess, labels).read)
                    for k in p.expect_files:
                        e = ents.get(k.lower())
                        files[k.lower()] = None if e is None else e.data
                outs[p.name] = Outcome(bytes(screen),
                                       res.regs["D0"] if ok else None,
                                       "HALT_OK" if ok else why, files, wall)
        finally:
            mgr.release(target)
    return outs, skips


# --- driver ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.aros_oracle",
                                 description=__doc__.split("\n")[0])
    side = ap.add_mutually_exclusive_group(required=True)
    side.add_argument("--prg", help="runner PRG: run Amiga64 in VICE")
    side.add_argument("--table", action="store_true",
                      help="compare against run_vbcc's expectations instead")
    ap.add_argument("--only", help="comma-separated program names")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="wall-clock limit per FS-UAE run (default 120 s)")
    ap.add_argument("--vice-timeout", type=float, default=1800.0)
    ap.add_argument("--fs-uae", default=FS_UAE)
    ap.add_argument("--cache", default=CACHE_ROOT)
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "build",
                                                  "vbcc-oracle"),
                    help="build dir for the compiled programs")
    ap.add_argument("--keep", help="copy each AROS run dir here")
    ap.add_argument("--json", help="write the per-program report here")
    ap.add_argument("--strict", action="store_true",
                    help="exit 1 on any DIFF, triaged or not")
    ap.add_argument("--retries", type=int, default=1,
                    help="re-run up to N times when the boot script stalled "
                    "after the program exited (default 1; reported, and "
                    "--strict fails a RETRY-MATCH)")
    args = ap.parse_args(argv)

    if not (os.path.isfile(args.fs_uae) and os.access(args.fs_uae, os.X_OK)):
        print(f"SKIP aros_oracle: FS-UAE not found at {args.fs_uae}")
        return 0
    cache, why = find_cache(args.manifest, args.cache)
    if cache is None:
        print(f"SKIP aros_oracle: {why}")
        return 0
    try:
        run_vbcc.vbcc_home()
    except SystemExit as e:
        print(f"SKIP aros_oracle: {e}")
        return 0

    table = run_vbcc.programs()
    names = args.only.split(",") if args.only else list(table)
    for n in names:
        if n not in table:
            print(f"error: unknown program {n!r}", file=sys.stderr)
            return 2
    progs = [table[n] for n in names]
    todo = [p for p in progs if p.name not in SKIP]
    os.makedirs(args.out, exist_ok=True)
    blobs = {p.name: run_vbcc.compile_c(p.name, args.out, p.src, p.defines)
             for p in todo}
    print(f"AROS cache {cache}\nFS-UAE {args.fs_uae}\n"
          f"Amiga64 side: {args.prg or 'run_vbcc expectations (--table)'}")

    skips = {n: SKIP[n] for n in names if n in SKIP}
    if args.prg:
        a64, more = a64_outcomes(os.path.abspath(args.prg), todo, blobs,
                                 args.vice_timeout)
        skips.update(more)
    else:
        a64 = {p.name: table_outcome(p) for p in todo}

    report = []
    untriaged = ndiff = retried = retry_match = 0
    for p in progs:
        if p.name in skips:
            print(f"SKIP  {p.name}: {skips[p.name]}")
            report.append({"name": p.name, "verdict": "SKIP",
                           "reason": skips[p.name]})
            continue
        ar = run_aros(p, blobs[p.name], cache, args.fs_uae, args.timeout,
                      args.keep, args.manifest)
        stalls = []
        # only a boot-script stall AFTER the program provably exited (its
        # $RC was recorded) is re-run; a guest that never exits is a DIFF
        while ar.stalled and len(stalls) < args.retries:
            stalls.append(ar.halt)
            print(f"      {p.name}: {ar.halt}; retrying")
            ar = run_aros(p, blobs[p.name], cache, args.fs_uae, args.timeout,
                          args.keep, args.manifest)
        v, diffs = verdict(a64[p.name], ar)
        known, new, missing = triage_check(p.name, diffs)
        tag = ""
        if stalls:
            retried += 1
            tag = f"  (retried after {len(stalls)} stall)"
            if v == "MATCH":
                v = "RETRY-MATCH"
                retry_match += 1
        if v == "DIFF":
            ndiff += 1
        if new or missing:
            untriaged += 1
            tag += (f"  [UNTRIAGED: {len(new)} new, {len(missing)} missing "
                    f"pinned line(s)]" if v == "DIFF" else
                    f"  [TRIAGE STALE: {len(missing)} pinned line(s) absent]")
        elif known:
            cats = sorted({c for _d, c in known}, key=CATEGORIES.index)
            tag += f"  [{', '.join(cats)}: {TRIAGE[p.name].note}]"
        a64_wall = (f"Amiga64 {a64[p.name].wall:.1f}s" if args.prg
                    else "vs table")
        print(f"{v:5s} {p.name}  (AROS {ar.wall:.1f}s, {a64_wall}){tag}")
        cat_of = dict(known)
        for d in diffs:
            print(f"        [{cat_of.get(d, 'NEW')}] {d}")
        for m in missing:
            print(f"        [MISSING pinned] {m}")
        report.append({"name": p.name, "verdict": v, "diffs": diffs,
                       "categories": [cat_of.get(d, "NEW") for d in diffs],
                       "missing_pinned": missing,
                       "triaged": bool(known) and not new and not missing,
                       "aros_wall": round(ar.wall, 2),
                       "a64_wall": (round(a64[p.name].wall, 2) if args.prg
                                    else None),
                       "aros_rc": ar.exit_code,
                       "retries": len(stalls),
                       "retried_stalls": stalls,
                       "aros_stdout": (ar.stdout or b"").decode("latin-1")})
    counts = {k: sum(r["verdict"] == k for r in report)
              for k in ("MATCH", "RETRY-MATCH", "DIFF", "SKIP")}
    print(f"\n{counts['MATCH']} MATCH, {counts['RETRY-MATCH']} RETRY-MATCH, "
          f"{counts['DIFF']} DIFF ({untriaged} untriaged), "
          f"{counts['SKIP']} SKIP; {retried} program(s) retried")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"cache": cache, "a64": args.prg or "table",
                       "counts": counts, "untriaged": untriaged,
                       "retried": retried, "programs": report}, f, indent=1)
    strict_bad = ndiff or retry_match
    return 1 if untriaged or (args.strict and strict_bad) else 0

if __name__ == "__main__":
    sys.exit(main())
