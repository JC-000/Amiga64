"""W15-P: MAIN is at its limit (supervisor, Wave 15).  The product link must
keep RODATA on its page ($8D00): CODE growth past it would move RODATA and
the BSS page (perf_cycles' tables, run_pf's variant builds).  Also the
audio hook's own invariants: aud_mon in DATA, AUD_ORG at $CA00."""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAP = os.path.join(ROOT, "build", "runner.map")
LBL = os.path.join(ROOT, "build", "runner.lbl")


def segs() -> dict:
    out = {}
    for m in re.finditer(r"^(\w+)\s+([0-9A-F]{6})\s+([0-9A-F]{6})\s+([0-9A-F]{6})",
                         open(MAP).read(), re.M):
        out[m.group(1)] = (int(m.group(2), 16), int(m.group(3), 16))
    return out


@pytest.mark.skipif(not os.path.exists(MAP), reason="build/runner.map: run make first")
def test_rodata_stays_on_its_page():
    s = segs()
    assert s["RODATA"][0] == 0x8D00, f"RODATA moved to ${s['RODATA'][0]:04X}"
    assert s["CODE"][1] < 0x8D00, f"CODE ends at ${s['CODE'][1]:04X}"
    assert s["BSS"][0] == 0x8F00, f"BSS moved to ${s['BSS'][0]:04X}"
    print(f"CODE margin to RODATA: {0x8D00 - 1 - s['CODE'][1]} B")


@pytest.mark.skipif(not os.path.exists(LBL), reason="build/runner.lbl: run make first")
def test_hook_labels():
    lab = {}
    for line in open(LBL):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    s = segs()
    assert lab["AUD_ORG"] == 0xCA00
    lo, hi = s["DATA"]
    assert lo <= lab["aud_mon"] <= hi, "aud_mon must sit in DATA (0 at load)"
