#!/usr/bin/env python3
"""Static scan of AROS m68k-amiga sources for chipset / CIA / privileged use.

W6-D (docs/S3_PLAN.md question 3).  Reads an AROS source checkout that lives
OUTSIDE the repo (default ~/.cache/amiga64/aros-src, a sparse shallow clone)
and reports, per source directory:

  * custom-chip registers referenced ($DFF000 + offset), by name, from
    `->field` accesses (field names taken from the tree's own
    compiler/include/hardware/custom.h), from literal 0xdffXXX addresses and
    from the amiga_hwreg.h-style macro names (INTENA, SERDAT, ...);
  * CIA registers (ciapra.. ciacrb fields, literal 0xbfeXXX / 0xbfdXXX);
  * privileged / exception-related 68k instructions in assembly and inline
    asm (move to/from SR, RTE, STOP, RESET, MOVEC, USP, TRAP, ILLEGAL).

This is an OVER-approximation of the boot surface: it cannot tell whether a
reference executes before "insert disk"; the plan document does that triage
by reading the boot path.  No AROS text is copied into the output beyond
register / instruction names and file paths.

Usage: python3 tools/s3probe/aros_hwscan.py [--src DIR] [--json OUT] [--md OUT]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT_SRC = Path.home() / ".cache" / "amiga64" / "aros-src"

# Source dirs that make up the amiga-m68k ROM image (boot/mmakefile.src
# KLIBS/KDEVS/KHIDDS/KRSRCS/KHOOKS for the main and ext banks) and that can
# touch hardware.  Generic rom/ code that never touches hardware is included
# too so the scan can prove it (0 hits).
SCAN_DIRS = [
    "arch/m68k-amiga/boot", "arch/m68k-amiga/exec", "arch/m68k-amiga/kernel",
    "arch/m68k-amiga/cia", "arch/m68k-amiga/timer",
    "arch/m68k-amiga/expansion", "arch/m68k-amiga/graphics",
    "arch/m68k-amiga/hidd/amigavideo", "arch/m68k-amiga/hidd/keyboard",
    "arch/m68k-amiga/hidd/mouse", "arch/m68k-amiga/hidd/gayle_ata",
    "arch/m68k-amiga/devs/trackdisk", "arch/m68k-amiga/devs/audio",
    "arch/m68k-amiga/disk", "arch/m68k-amiga/diag", "arch/m68k-amiga/romboot",
    "arch/m68k-amiga/potgo", "arch/m68k-amiga/battclock",
    "arch/m68k-amiga/card", "arch/m68k-amiga/devs/cd",
    "arch/m68k-all", "rom/exec", "rom/kernel", "rom/dosboot", "rom/graphics",
    "rom/intuition", "rom/timer", "rom/expansion",
]

CIA_FIELDS = ["ciapra", "ciaprb", "ciaddra", "ciaddrb", "ciatalo", "ciatahi",
              "ciatblo", "ciatbhi", "ciatodlow", "ciatodmid", "ciatodhi",
              "ciasdr", "ciaicr", "ciacra", "ciacrb"]
# CIA register n sits at base + n*$100 (CIA-A $BFE001 odd, CIA-B $BFD000 even)
CIA_BY_INDEX = ["ciapra", "ciaprb", "ciaddra", "ciaddrb", "ciatalo",
                "ciatahi", "ciatblo", "ciatbhi", "ciatodlow", "ciatodmid",
                "ciatodhi", "(unused)", "ciasdr", "ciaicr", "ciacra", "ciacrb"]

PRIV_PATTERNS = {
    "move_to_sr": re.compile(r"\bmove\.?w?\s+[^;\n]*,\s*%?sr\b", re.I),
    "move_from_sr": re.compile(r"\bmove\.?w?\s+%?sr\s*,", re.I),
    "or/and/eori_sr": re.compile(r"\b(or|and|eor)i?\.w\s+#[^,\n]*,\s*%?sr\b", re.I),
    "rte": re.compile(r"\brte\b", re.I),
    "stop": re.compile(r"\bstop\s+#", re.I),
    "reset": re.compile(r"^\s*\"?\s*reset\b", re.I | re.M),
    "movec": re.compile(r"\bmovec\b", re.I),
    "usp": re.compile(r"%?\busp\b", re.I),
    "trap": re.compile(r"\btrap\s+#", re.I),
    "illegal": re.compile(r"^\s*\"?\s*illegal\b", re.I | re.M),
}


def parse_custom_h(path: Path) -> dict[str, int]:
    """field name -> byte offset from $DFF000, from struct Custom."""
    text = path.read_text(errors="replace")
    body = text[text.index("struct Custom"):]
    body = body[body.index("{") + 1:]
    # strip comments
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    sizes = {"UWORD": 2, "UBYTE": 1, "APTR": 4, "ULONG": 4, "UWORD *": 4}
    fields: dict[str, int] = {}
    off = 0
    depth = 0
    lines = iter(body.splitlines())
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("struct AudChannel") or s.startswith("struct SpriteDef"):
            # nested: collect members until "} name[n];"
            sub = []
            for l2 in lines:
                t = l2.strip()
                if t.startswith("}"):
                    m = re.match(r"}\s*(\w+)\[(\d+)\];", t)
                    name, n = m.group(1), int(m.group(2))
                    break
                if t in ("{", ""):
                    continue
                m = re.match(r"(UWORD\s*\*|UWORD|APTR|ULONG|UBYTE)\s+(\w+)(?:\[(\d+)\])?;", t)
                if m:
                    typ = m.group(1).replace(" ", "")
                    sz = 4 if "*" in typ else sizes[typ]
                    sub.append((m.group(2), sz, int(m.group(3) or 1)))
            stride = sum(sz * k for _, sz, k in sub)
            for i in range(n):
                o = off + i * stride
                for fname, sz, k in sub:
                    fields.setdefault(f"{name}[{i}].{fname}", o)
                    fields.setdefault(fname, off + 0)  # generic member name
                    o += sz * k
            off += n * stride
            continue
        if s.startswith("}"):
            break
        m = re.match(r"(UWORD|APTR|ULONG|UBYTE)\s+(\w+)(?:\[(\d+)\])?;", s)
        if not m:
            continue
        typ, name, n = m.group(1), m.group(2), int(m.group(3) or 1)
        fields[name] = off
        off += sizes[typ] * n
    return fields


def reg_name_for_offset(fields: dict[str, int], off: int) -> str:
    best = None
    for name, o in fields.items():
        if "[" in name or name.startswith("pad"):
            continue
        if o <= off and (best is None or o > best[1]):
            best = (name, o)
    if best is None:
        return f"${off:03X}"
    return best[0] if best[1] == off else f"{best[0]}+{off - best[1]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, default=DEFAULT_SRC)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--md", type=Path)
    a = ap.parse_args()
    src = a.src
    custom_h = src / "compiler/include/hardware/custom.h"
    if not custom_h.exists():
        print(f"SKIP: {custom_h} missing (fetch AROS sources first)")
        return 0
    fields = parse_custom_h(custom_h)
    # macro names used by arch code (amiga_hwreg.h style): same spelling upper
    macro_names = {n.upper(): n for n in fields if "[" not in n}
    field_re = re.compile(r"->\s*(\w+)\b|\.\s*(\w+)\b")
    lit_custom = re.compile(r"0x0*dff([0-9a-f]{3})\b", re.I)
    lit_cia = re.compile(r"0x0*bf([de])([0-9a-f])([0-9a-f]{2})\b", re.I)
    macro_re = re.compile(r"\b(" + "|".join(sorted(macro_names, key=len, reverse=True)) + r")\b")

    out: dict[str, dict] = {}
    for d in SCAN_DIRS:
        root = src / d
        if not root.exists():
            continue
        custom_regs: dict[str, set] = defaultdict(set)
        cia_regs: dict[str, set] = defaultdict(set)
        priv: dict[str, set] = defaultdict(set)
        for p in sorted(root.rglob("*")):
            if p.suffix not in (".c", ".S", ".s", ".h", ".asm") or not p.is_file():
                continue
            rel = str(p.relative_to(src))
            t = p.read_text(errors="replace")
            t_nc = re.sub(r"/\*.*?\*/", "", t, flags=re.S)
            t_nc = re.sub(r"//[^\n]*", "", t_nc)
            hw_file = ("hardware/custom.h" in t or "0xdff" in t.lower()
                       or "amiga_hwreg.h" in t or "chipset.h" in t)
            if hw_file:
                for m in field_re.finditer(t_nc):
                    name = m.group(1) or m.group(2)
                    if name in fields and "[" not in name and not name.startswith("pad") \
                            and name not in ("pos", "ctl", "dataa", "datab", "ac_ptr", "ac_len",
                                             "ac_per", "ac_vol", "ac_dat", "ac_pad"):
                        custom_regs[name].add(rel)
                for m in macro_re.finditer(t_nc):
                    custom_regs[macro_names[m.group(1)]].add(rel)
            for m in lit_custom.finditer(t_nc):
                o = int(m.group(1), 16)
                # a bare 0xdff000 is the chip base (register set via offset)
                name = "(base $DFF000)" if o == 0 else (
                    reg_name_for_offset(fields, o) if o < 0x200 else f"$DFF{o:03X}(>$1FF)")
                custom_regs[name].add(rel)
            for m in re.finditer(r"\b(" + "|".join(CIA_FIELDS) + r")\b", t_nc):
                cia_regs[m.group(1)].add(rel)
            for m in lit_cia.finditer(t_nc):
                chip = "A" if m.group(1).lower() == "e" else "B"
                cia_regs[f"CIA{chip}.{CIA_BY_INDEX[int(m.group(2), 16)]}"].add(rel)
            if p.suffix in (".S", ".s", ".asm") or "asm" in t_nc:
                for k, rx in PRIV_PATTERNS.items():
                    if rx.search(t_nc):
                        priv[k].add(rel)
        out[d] = {
            "custom": {k: sorted(v) for k, v in sorted(custom_regs.items(),
                                                        key=lambda kv: fields.get(kv[0], 999))},
            "cia": {k: sorted(v) for k, v in sorted(cia_regs.items())},
            "priv": {k: sorted(v) for k, v in sorted(priv.items())},
        }

    union_custom = sorted({r for v in out.values() for r in v["custom"]},
                          key=lambda r: fields.get(r, 999))
    summary = {"custom_offsets": {r: fields.get(r) for r in union_custom},
               "dirs": out}
    if a.json:
        a.json.write_text(json.dumps(summary, indent=1))
    lines = ["# AROS m68k-amiga static hardware scan (tools/s3probe/aros_hwscan.py)", "",
             f"source: {src} (outside the repo)", "",
             "| dir | custom regs | CIA regs | privileged |", "|---|---|---|---|"]
    for d, v in out.items():
        cr = ", ".join(v["custom"]) or "-"
        ci = ", ".join(v["cia"]) or "-"
        pr = ", ".join(v["priv"]) or "-"
        lines.append(f"| {d} | {cr} | {ci} | {pr} |")
    lines += ["", "## union of custom registers referenced (offset from $DFF000)", ""]
    lines += [f"- {r} ${fields[r]:03X}" if fields.get(r) is not None else f"- {r}"
              for r in union_custom]
    md = "\n".join(lines) + "\n"
    if a.md:
        a.md.write_text(md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
