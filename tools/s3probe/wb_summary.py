#!/usr/bin/env python3
"""Summarise aros_boot_model.py --adf runs: phases, consumers, wall-clock.

WB-measure (docs/WB_BOOT.md, tasks 2-3).  Reads boot_report.json files and
prints Markdown tables:

* per phase (reset -> first disk read -> boot block -> first
  Startup-Sequence command -> LoadWB -> Workbench window -> boot idle):
  68k instructions, virtual time, STOP time, 256-byte code-page changes,
  blitter words and trapped I/O calls, all [MODEL];
* the top consumers per phase, by task / CLI command and by ROM module
  (PCs mapped to the ROM's RomTag ranges; "RAM" = code loaded from disk);
* the wall-clock estimate at 48 MHz and 1 MHz from the S3_PLAN rates [EST].

Usage: python3 tools/s3probe/wb_summary.py RUN_DIR [RUN_DIR ...] [--json OUT]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import struct
import sys

PHASES = [("reset", "disk_first_read", "(i) reset -> first disk read"),
          ("disk_first_read", "bootblock", "(ii) -> boot block entered"),
          ("bootblock", "first_command", "(iii) -> first Startup-Sequence command"),
          ("first_command", "loadwb", "Startup-Sequence up to LoadWB"),
          ("loadwb", "wb_window", "LoadWB -> Workbench window"),
          ("wb_window", "idle", "(iv) -> boot idle (disk quiet)")]

# Conversion inputs.  docs/S3_PLAN.md 3.5 and 2.3, docs/HARDWARE.md.
RATES = {
    # instructions per second, exception mode, before refills [EST]
    "ips": {"48": (52_000, 68_000), "1": (1_400, 1_500)},
    # one 256-byte code-window refill [EST]
    "refill_s": {"48": 90e-6, "1": 380 / 0.96e6},
    # synchronous blitter, per word [EST]
    "blit_s": {"48": 3.3e-6, "1": 20.0 / 275_000},
    # one trapped $BF/$DF access, 200-400 C64 cycles [EST]
    "io_s": {"48": (200 / 38.9e6, 400 / 38.9e6), "1": (200 / 0.96e6, 400 / 0.96e6)},
    # one full hires4 VIC refresh [EST]; the policy assumed is one per
    # guest second of boot (refresh on change, duty-capped)
    "refresh_s": {"48": 0.035, "1": 0.69},
}


def rom_modules(rom_dir: str | None = None):
    """[(start, end, name)] from the RomTags of the pinned ROM images."""
    if rom_dir is None:
        c = glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "third_party", "aros", "aros-rom.bin"))
        if not c:
            return []
        rom_dir = os.path.dirname(c[0])
    mem = {0xF80000: open(os.path.join(rom_dir, "aros-rom.bin"), "rb").read(),
           0xE00000: open(os.path.join(rom_dir, "aros-ext.bin"), "rb").read()}

    def cstr(a):
        for b, m in mem.items():
            if b <= a < b + len(m):
                s = m[a - b:a - b + 64]
                return s.split(b"\0")[0].decode("latin-1")
        return "?"
    tags = []
    for base, m in mem.items():
        for off in range(0, len(m) - 26, 2):
            if m[off:off + 2] == b"\x4a\xfc" and struct.unpack_from(">I", m, off + 2)[0] == base + off:
                end = struct.unpack_from(">I", m, off + 6)[0]
                tags.append((base + off, end, cstr(struct.unpack_from(">I", m, off + 14)[0])))
    tags.sort()
    out = []
    for i, (a, end, name) in enumerate(tags):
        bank_end = (a | 0x7FFFF) + 1
        nxt = min(tags[i + 1][0], bank_end) if i + 1 < len(tags) else bank_end
        stop = min(end, nxt) if end > a else nxt
        out.append((a, stop, name))
        if stop < nxt:                      # code after EndSkip, before the next tag
            out.append((stop, nxt, name + " (after EndSkip)"))
    return out


def module_of(addr: int, mods) -> str:
    if addr < 0xE00000:
        return "RAM (code loaded from disk, LVO jumps)"
    lo, hi = 0, len(mods)
    while lo < hi:
        mid = (lo + hi) // 2
        if mods[mid][0] <= addr:
            lo = mid + 1
        else:
            hi = mid
    if lo and mods[lo - 1][0] <= addr < mods[lo - 1][1]:
        return mods[lo - 1][2]
    return "(ROM entry code before the bank's first RomTag)"


def diff(b: dict, a: dict) -> dict:
    return {k: v - a.get(k, 0) for k, v in b.items() if v - a.get(k, 0)}


def by_module(pcblk: dict, mods) -> dict:
    out: dict[str, int] = {}
    for k, v in pcblk.items():
        name = module_of(int(k) << 6, mods)
        out[name] = out.get(name, 0) + v
    return out


def top(d: dict, n: int = 6) -> list:
    return sorted(d.items(), key=lambda kv: -kv[1])[:n]


def wall(n, refills, blit, io, vsec):
    """(low, high) seconds at 48 MHz and at 1 MHz [EST]."""
    res = {}
    for mhz in ("48", "1"):
        ips_lo, ips_hi = RATES["ips"][mhz]
        io_lo, io_hi = RATES["io_s"][mhz]
        fixed = refills * RATES["refill_s"][mhz] + blit * RATES["blit_s"][mhz] \
            + vsec * RATES["refresh_s"][mhz]
        res[mhz] = (n / ips_hi + fixed + io * io_lo, n / ips_lo + fixed + io * io_hi)
    return res


def summarise(run_dir: str, mods) -> dict:
    rep = json.load(open(os.path.join(run_dir, "boot_report.json")))
    wb = rep["wb"]
    marks = dict(wb["markers"])
    zero = {"icount": 0, "t_s": 0.0, "idle_s": 0.0, "code_page_changes": 0,
            "blit_words": 0, "io_calls": 0, "disk_reads": 0, "tasks": {}, "pcblk": {}}
    marks["reset"] = zero
    phases = []
    for a, b, label in PHASES:
        if a not in marks or b not in marks:
            phases.append({"phase": label, "missing": b if b not in marks else a})
            continue
        A, B = marks[a], marks[b]
        n = B["icount"] - A["icount"]
        p = {"phase": label, "from": a, "to": b, "instructions": n,
             "virtual_s": round(B["t_s"] - A["t_s"], 3),
             "idle_s": round(B["idle_s"] - A["idle_s"], 3),
             "code_page_changes": B["code_page_changes"] - A["code_page_changes"],
             "blit_words": B["blit_words"] - A["blit_words"],
             "io_calls": B["io_calls"] - A["io_calls"],
             "disk_reads": B["disk_reads"] - A["disk_reads"],
             "tasks": top(diff(B["tasks"], A["tasks"]), 8),
             "modules": top(by_module(diff(B["pcblk"], A["pcblk"]), mods), 8)}
        p["wall"] = wall(n, p["code_page_changes"], p["blit_words"], p["io_calls"], p["virtual_s"])
        phases.append(p)
    end = wb["end"]
    idle = marks.get("idle")
    steady = None
    if idle and end["t_s"] > idle["t_s"]:
        steady = round((end["icount"] - idle["icount"]) / (end["t_s"] - idle["t_s"]))
    cmds: dict[str, int] = {}
    for k, v in end["tasks"].items():
        cmds[k] = cmds.get(k, 0) + v
    total = idle or end
    return {"run": os.path.basename(run_dir.rstrip("/")), "adf": wb["adf"],
            "td_hle": wb["td_hle"], "phases": phases,
            "total": {"instructions": total["icount"], "virtual_s": total["t_s"],
                      "idle_s": total["idle_s"],
                      "code_page_changes": total["code_page_changes"],
                      "blit_words": total["blit_words"], "io_calls": total["io_calls"],
                      "wall": wall(total["icount"], total["code_page_changes"],
                                   total["blit_words"], total["io_calls"], total["t_s"])},
            "steady_instr_per_virtual_s": steady,
            "consumers_to_idle": top(total["tasks"], 25),
            "modules_to_idle": top(by_module(total["pcblk"], mods), 20),
            "commands": wb["commands"], "disk": wb["disk"]}


def fmt_s(lo, hi):
    f = (lambda x: f"{x:.0f}") if hi >= 10 else (lambda x: f"{x:.1f}")
    return f"{f(lo)}–{f(hi)}"


def markdown(s: dict) -> str:
    out = [f"### {s['run']} ({s['adf']}{', HLE trackdisk' if s['td_hle'] else ''})", "",
           "| Phase | Instr [MODEL] | Virtual s (idle) | Code-page chg | Blit words | I/O calls | Disk reads | 48 MHz s [EST] | 1 MHz min [EST] |",
           "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for p in s["phases"] + [dict(s["total"], phase="**total to idle**", disk_reads="")]:
        if "missing" in p:
            out.append(f"| {p['phase']} | not reached ({p['missing']}) |||||||| ")
            continue
        w = p["wall"]
        out.append(f"| {p['phase']} | {p['instructions']:,} | {p['virtual_s']:.1f} ({p['idle_s']:.1f}) | "
                   f"{p['code_page_changes']:,} | {p['blit_words']:,} | {p['io_calls']:,} | {p.get('disk_reads', '')} | "
                   f"{fmt_s(*w['48'])} | {fmt_s(w['1'][0] / 60, w['1'][1] / 60)} |")
    out += ["", f"Steady state after idle: {s['steady_instr_per_virtual_s']} instr per virtual second [MODEL].", ""]
    for p in s["phases"]:
        if "tasks" in p:
            out.append(f"- {p['phase']}: tasks " + ", ".join(f"{k} {v:,}" for k, v in p["tasks"][:5])
                       + "; modules " + ", ".join(f"{k} {v:,}" for k, v in p["modules"][:5]))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    mods = rom_modules()
    res = [summarise(r, mods) for r in a.runs]
    for s in res:
        print(markdown(s))
        print()
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
