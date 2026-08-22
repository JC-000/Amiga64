#!/usr/bin/env python3
"""Generate the 256-entry opcode dispatch table (build/gen/dispatch.s).

Owner: core agent.  The family map below mirrors docs/CONTRACTS.md and is
the single source of truth for op_hi -> handler routing; nobody hand-edits
the emitted table.

Conflict-avoidance: the generator scans runtime/core.s and runtime/ops/*.s
for ``.export <label>`` of every family handler named in the map.  Handlers
that are exported somewhere get an ``.import`` line; handlers not exported
anywhere get a local stub ``<label>: jmp op_illegal`` inside dispatch.s.
This keeps the PRG linking before/while the group agents land their files.
The stubs are NOT exported, so a later real ``.export`` never collides —
but note the Makefile only regenerates dispatch.s when gen_dispatch.py
changes, so after adding a new ``.export fam_*`` run ``rm -f
build/gen/dispatch.s`` (or make clean) to drop the stale stub.

Emitted interface (imported by runtime/core.s):
    dispatch_lo / dispatch_hi — two 256-byte tables (segment "DISPATCH",
    page-aligned via runtime/c64.cfg) holding the lo/hi bytes of each
    handler address, indexed by op_hi.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# op_hi ranges (inclusive) -> handler label.  Mirrors docs/CONTRACTS.md.
FAMILY_MAP: list[tuple[int, int, str]] = [
    (0x00, 0x0F, "fam_0"),
    (0x10, 0x3F, "fam_move"),
    (0x40, 0x4F, "fam_4"),
    (0x50, 0x5F, "fam_5"),
    (0x60, 0x6F, "fam_bcc"),
    (0x70, 0x7F, "fam_moveq"),
    (0x80, 0x8F, "fam_8"),
    (0x90, 0x9F, "fam_sub"),
    (0xA0, 0xAF, "op_aline"),
    (0xB0, 0xBF, "fam_b"),
    (0xC0, 0xCF, "fam_c"),
    (0xD0, 0xDF, "fam_add"),
    (0xE0, 0xEF, "fam_shift"),
    (0xF0, 0xFF, "op_fline"),
]

FALLBACK = "op_illegal"

# Cross-group op labels (docs/CONTRACTS.md family map): implemented in one
# group file, `.import`ed by another family's secondary decode.  Any of these
# not yet exported by a real source gets an EXPORTED stub (jmp op_illegal) so
# importers link while the owning agent's file is still unwritten.  Unlike
# family stubs these must be exported to satisfy the importer's .import; the
# Makefile regenerates dispatch.s whenever runtime sources change, so the
# stub disappears in the same build that adds the real .export.
CROSS_OPS: list[str] = [
    "op_scc",     # G3, imported by fam_5 (G2)
    "op_dbcc",    # G5, imported by fam_5 (G2)
    "op_eor",     # G3, imported by fam_b (G2)
    "op_exg",     # G5, imported by fam_c (G3)
    "op_movem",   # G6, imported by fam_4 (G5)
    "op_mulu",    # G6, imported by fam_c (G3)
    "op_muls",    # G6, imported by fam_c (G3)
    "op_divu",    # G6, imported by fam_8 (G3)
    "op_divs",    # G6, imported by fam_8 (G3)
    "op_abcd",    # G6, imported by fam_c (G3)
    "op_sbcd",    # G6, imported by fam_8 (G3)
    "op_nbcd",    # G6, imported by fam_4 (G5)
    "hle_aline",  # M2 HLE, imported by op_aline (core); stub preserves halt-ALINE
]

# Matches ".export a, b" / ".exportzp x" (ca65 directives, case-insensitive).
# Anchored so commented-out exports ("; .export foo") are ignored.
_EXPORT_RE = re.compile(r"^\s*\.export(?:zp)?\s+(.+)$", re.IGNORECASE)


def find_exports(paths) -> set[str]:
    """Return the set of symbol names exported by the given ca65 sources."""
    names: set[str] = set()
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _EXPORT_RE.match(line)
            if not m:
                continue
            body = m.group(1).split(";", 1)[0]  # strip trailing comment
            for tok in body.split(","):
                # ".export name = value" / "name: absolute" forms
                name = tok.split("=", 1)[0].split(":", 1)[0].strip()
                if name:
                    names.add(name)
    return names


def build_table() -> list[str]:
    """Expand FAMILY_MAP into a dense 256-entry handler list."""
    table: list[str | None] = [None] * 256
    for lo, hi, label in FAMILY_MAP:
        for i in range(lo, hi + 1):
            if table[i] is not None:
                raise ValueError(f"family map overlap at ${i:02X}")
            table[i] = label
    missing = [i for i, e in enumerate(table) if e is None]
    if missing:
        raise ValueError(f"family map holes at {['$%02X' % i for i in missing]}")
    return table  # type: ignore[return-value]


def _byte_rows(table: list[str], op: str) -> list[str]:
    rows = []
    for i in range(0, 256, 8):
        entries = ", ".join(f"{op}({label})" for label in table[i : i + 8])
        rows.append(f"        .byte {entries}   ; ${i:02X}-${i + 7:02X}")
    return rows


def generate(exported: set[str]) -> str:
    """Render dispatch.s given the set of exported symbols."""
    table = build_table()
    handlers = sorted(set(table))
    imports = [h for h in handlers if h in exported]
    stubs = [h for h in handlers if h not in exported]

    lines = [
        "; AUTO-GENERATED by host/gen_dispatch.py — DO NOT EDIT.",
        "; 256-entry op_hi dispatch table per docs/CONTRACTS.md.",
        "",
        ".export dispatch_lo, dispatch_hi",
        "",
    ]
    if FALLBACK in exported:
        lines.append(f".import {FALLBACK}")
    for h in imports:
        lines.append(f".import {h}")
    lines.append("")
    lines.append('.segment "CODE"')
    if FALLBACK not in exported:
        # Broken tree (core.s missing/incomplete): self-looping fallback so
        # the file still assembles standalone.
        lines.append(f"{FALLBACK}:      jmp {FALLBACK}    ; fallback stub")
    for h in stubs:
        lines.append(f"{h}:")
        lines.append(f"        jmp {FALLBACK}          ; stub: not yet implemented")
    for c in CROSS_OPS:
        if c in exported:
            continue  # real implementation links directly; nothing to emit
        lines.append(f".export {c}")
        lines.append(f"{c}:")
        lines.append(f"        jmp {FALLBACK}          ; cross-op stub: owner not landed")
    lines += [
        "",
        '.segment "DISPATCH"          ; page-aligned via runtime/c64.cfg',
        "dispatch_lo:",
        *_byte_rows(table, "<"),
        "dispatch_hi:",
        *_byte_rows(table, ">"),
        "",
    ]
    return "\n".join(lines)


def scan_sources(runtime_dir: Path) -> list[Path]:
    srcs = []
    core = runtime_dir / "core.s"
    if core.exists():
        srcs.append(core)
    srcs.extend(sorted((runtime_dir / "ops").glob("*.s")))
    return srcs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", required=True, help="output .s path")
    ap.add_argument(
        "--runtime-dir",
        default="runtime",
        help="directory holding core.s and ops/*.s (default: runtime)",
    )
    args = ap.parse_args(argv)

    exported = find_exports(scan_sources(Path(args.runtime_dir)))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate(exported), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
