"""W16-X: the AmigaOS launcher's host side (no device, no emulator).

* The default W15 builds (fixtures/mod: the stub UI and W15-U's ui.s, with
  and without MOD_TRACE) are byte-identical to the builds of the same
  sources with the MOD_HUNK/UI_HUNK switch blocks taken out (supervisor
  ruling: the switches change nothing by default).
* The launcher's hunk executable: the entry is the first code hunk, the
  UI's buffers are one CHIP BSS hunk, the code and data hunks store every
  byte (mwend.s: no tail that only LoadSeg would clear).
* fixtures/modwb/os.i against the table below (public documentation:
  build/evidence/w16-x/PROGRESS.md "Public sources"), and the host mirrors
  in tests/run_modwb.py.
* tests/run_modwb.loadseg relocates as LoadSeg does.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess

import pytest

from host import hunk
from tests import run_modwb as W

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MOD = os.path.join(ROOT, "fixtures", "mod")
MODWB = os.path.join(ROOT, "fixtures", "modwb")
VBCC = os.path.expanduser("~/.local/opt/vbcc/bin")
need_vbcc = pytest.mark.skipif(not os.path.exists(os.path.join(VBCC, "vlink")),
                               reason="no vbcc/vasm/vlink")

# AmigaOS LVOs and offsets (RKRM Libraries / Includes & Autodocs, HRM)
OS_TABLE = {
    "_LVOSupervisor": -30, "_LVODisable": -120, "_LVOEnable": -126, "_LVOForbid": -132,
    "_LVOPermit": -138, "_LVOSuperState": -150, "_LVOUserState": -156,
    "_LVOAllocMem": -198, "_LVOAllocAbs": -204, "_LVOFreeMem": -210, "_LVOFindTask": -294, "_LVOGetMsg": -372,
    "_LVOReplyMsg": -378, "_LVOWaitPort": -384, "_LVOCloseLibrary": -414,
    "_LVOOpenDevice": -444, "_LVOCloseDevice": -450, "_LVOOpenResource": -498,
    "_LVOOpenLibrary": -552, "_LVOCreateMsgPort": -666, "_LVODeleteMsgPort": -672,
    "_LVOOpen": -30, "_LVOClose": -36, "_LVORead": -42, "_LVOSeek": -66,
    "_LVOCurrentDir": -126, "_LVOLoadView": -222, "_LVOWaitBlit": -228,
    "_LVOWaitTOF": -270, "_LVOOwnBlitter": -456, "_LVODisownBlitter": -462,
    "_LVODisplayBeep": -96, "_LVORethinkDisplay": -390, "_LVOEasyRequestArgs": -588,
    "_LVOAddICRVector": -6, "_LVORemICRVector": -12, "_LVOAbleICR": -18, "_LVOSetICR": -24,
    "MEMF_CHIP": 2, "MODE_OLDFILE": 1005, "OFFSET_BEGINNING": -1, "OFFSET_END": 1,
    "NT_INTERRUPT": 2, "LN_TYPE": 8, "LN_PRI": 9, "LN_NAME": 10, "MN_REPLYPORT": 14,
    "MN_LENGTH": 18, "pr_MsgPort": 92, "pr_CLI": 172, "sm_NumArgs": 28, "sm_ArgList": 36,
    "wa_Lock": 0, "wa_Name": 4, "IS_DATA": 14, "IS_CODE": 18, "IS_SIZE": 22,
    "IO_FLAGS": 30, "IO_ERROR": 31, "ioa_Data": 34, "ioa_Length": 38, "IOA_SIZE": 68,
    "ADIOF_NOWAIT": 0x40, "gb_ActiView": 34, "gb_copinit": 38, "es_StructSize": 0,
    "es_Flags": 4, "es_Title": 8, "es_TextFormat": 12, "es_GadgetFormat": 16,
    "DMACONR": 0x002, "ADKCONR": 0x010, "INTENAR": 0x01C, "COP1LC": 0x080,
    "COPJMP1": 0x088, "ADKCON": 0x09E, "CIAICRB_TA": 0, "CIAICRB_TB": 1,
    "CIAA_PRA": 0xBFE001, "CIAA_CRA": 0xBFEE01, "CIAB_PRA": 0xBFD000,
}


def equates(path: str) -> dict[str, int]:
    out = {}
    for line in open(path):
        m = re.match(r"(\w+)\s+equ\s+(-?\$[0-9a-fA-F]+|-?\d+)", line)
        if m:
            v = m.group(2)
            neg = v.startswith("-")
            v = v.lstrip("-")
            n = int(v[1:], 16) if v.startswith("$") else int(v)
            out[m.group(1)] = -n if neg else n
    return out


def test_os_i_table():
    e = equates(os.path.join(MODWB, "os.i"))
    assert {k: e.get(k) for k in OS_TABLE} == OS_TABLE
    assert set(e) == set(OS_TABLE), "os.i and the pinned table differ"


def test_state_mirror():
    """tests/run_modwb.ST against modwb.s's rs offsets."""
    rs, off = {}, 0
    sizes = {"l": 4, "w": 2, "b": 1}
    src = open(os.path.join(MODWB, "modwb.s")).read()
    block = src[src.index("rsreset"):src.index("A_SIZE")]
    for m in re.finditer(r"^(A_\w+)\s+rs\.([lwb])\s+(\d+)", block, re.M):
        rs[m.group(1)] = off
        off += sizes[m.group(2)] * int(m.group(3))
    assert off == W.ST_SIZE
    want = {"A_" + k.upper(): v for k, v in W.ST.items()}
    assert {k: rs[k] for k in want} == want
    e = equates(os.path.join(MODWB, "modwb.s"))
    assert (e["ANCHOR_AT"], e["PROF_AT"]) == (W.ANCHOR, W.PROF) == (0x2DC040, 0x2DC000)
    assert (e["ST_DONE"], e["ST_FAILED"]) == (W.ST_DONE, W.ST_FAILED)


def test_stub_mirror():
    e = equates(os.path.join(MODWB, "stub", "xstub.s"))
    assert (e["XP"], e["XS"], e["XLOG"], e["HEAP"]) == (W.XP, W.XS, W.XLOG, W.HEAP)
    assert e["OSB"] + 0x7800 == W.COPLIST
    assert e["XSTACK_S"] == W.SSP0


# --------------------------------------------------------------------------
# the switches change nothing by default
# --------------------------------------------------------------------------

SWITCHES = ("MOD_HUNK", "UI_HUNK")


def strip_switches(text: str) -> str:
    """Remove every `ifd/ifnd SWITCH ... [else ...] endc/endif` block,
    keeping what a build WITHOUT the switch assembles (nested conditionals
    of other names are kept as they are)."""
    out, stack = [], []          # stack: (is_switch, keep_now) per open conditional
    for line in text.splitlines(keepends=True):
        w = line.split()
        op = w[0].lower() if w else ""
        if op in ("ifd", "ifnd") and len(w) > 1:
            sw = w[1] in SWITCHES
            keep = (op == "ifnd") if sw else True
            stack.append((sw, keep))
            if not sw:
                out.append(line)
            continue
        if op.startswith("if") and op not in ("ifd", "ifnd"):
            stack.append((False, True))
            out.append(line)
            continue
        if op == "else" and stack:
            sw, keep = stack[-1]
            if sw:
                stack[-1] = (sw, not keep)
                continue
            out.append(line)
            continue
        if op in ("endc", "endif") and stack:
            sw, _ = stack.pop()
            if not sw:
                out.append(line)
            continue
        if all(k for s, k in stack if s):
            out.append(line)
    assert not stack
    return "".join(out)


def test_strip_switches():
    src = ("a\n ifd MOD_HUNK\nb\n else\nc\n endc\n ifnd UI_HUNK\nd\n ifnd X\ne\n endif\n"
           " endif\n ifd UI_HUNK\nf\n endif\n")
    assert strip_switches(src) == "a\nc\nd\n ifnd X\ne\n endif\n"


def _make(src_dir: str, out: str, extra: list[str]) -> bytes:
    r = subprocess.run(["make", "-s", "-C", src_dir, f"OUT={out}", *extra],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    with open(os.path.join(out, "modplay.bin"), "rb") as f:
        return f.read()


@need_vbcc
@pytest.mark.parametrize("extra", [[], ["UI_SRC=ui/ui.s"], ["DEFS=MOD_TRACE"],
                                   ["UI_SRC=ui/ui.s", "DEFS=MOD_TRACE"]],
                         ids=["stub", "ui", "trace", "ui-trace"])
def test_default_builds_identical(tmp_path, extra):
    plain = tmp_path / "plain"
    shutil.copytree(MOD, plain)
    for p in list(plain.rglob("*.s")) + list(plain.rglob("*.i")):
        t = p.read_text()
        s = strip_switches(t)
        if s != t:
            p.write_text(s)
    a = _make(MOD, str(tmp_path / "a"), extra)
    b = _make(str(plain), str(tmp_path / "b"), extra)
    assert a == b


@need_vbcc
def test_strip_has_teeth(tmp_path):
    """The comparison would see a switch block that changes the default."""
    plain = tmp_path / "plain"
    shutil.copytree(MOD, plain)
    p = plain / "replay.s"
    t = p.read_text().replace("        ifd     MOD_HUNK                ; W16-X",
                              "        ifnd    MOD_HUNK                ; W16-X", 1)
    p.write_text(t)
    a = _make(MOD, str(tmp_path / "a"), [])
    b = _make(str(plain), str(tmp_path / "b"), [])
    assert a != b


# --------------------------------------------------------------------------
# the launcher's executable
# --------------------------------------------------------------------------

@need_vbcc
def test_launcher_hunks(tmp_path):
    exe = W.build([], str(tmp_path / "mw"))
    ex = hunk.parse(exe)
    kinds = [(s.kind, s.memory_flags) for s in ex.segments]
    assert kinds[0] == ("code", 0)
    chip = [s for s in ex.segments if s.memory_flags == 1]
    # the state struct (the anchor's target, rule 3) and the UI's buffers
    assert [s.kind for s in chip] == ["data", "bss"]
    assert chip[0].alloc_size >= W.ST_SIZE and chip[0].data[:W.ST_SIZE] == bytes(W.ST_SIZE)
    assert chip[1].alloc_size == 0x100 + 0xF00 + 48000
    for s in ex.segments:
        if s.kind != "bss":
            assert len(s.data) == s.alloc_size, f"{s.kind}: a tail only LoadSeg would clear"
    assert exe[:4] == b"\x00\x00\x03\xf3"


def test_loadseg_relocates():
    """Two hunks, a RELOC32 from code to data: the long becomes data's
    address + its stored offset; the code tail is $A5, BSS zero."""
    def l(v):
        return struct.pack(">I", v)
    code = l(0x3E9) + l(2) + l(8) + l(0x12345678)     # 2 longs: offset 8 into data, junk
    rel = l(0x3EC) + l(1) + l(1) + l(0) + l(0)          # 1 reloc to hunk 1 at offset 0
    data = l(0x3EA) + l(4) + bytes(range(16))
    bss = l(0x3EB) + l(4)
    exe = (l(0x3F3) + l(0) + l(3) + l(0) + l(2) + l(3) + l(4) + l(4 | 0x40000000)
           + code + rel + l(0x3F2) + data + l(0x3F2) + bss + l(0x3F2))
    ch, addrs = W.loadseg(exe, (0x30000, 0x38000, 0x3A000, 0x90000))
    assert addrs == [0x30000, 0x38000, 0x90000]
    c = dict(ch)
    assert c[0x30000][:8] == l(0x38000 + 8) + l(0x12345678)
    assert c[0x30000][8:] == b"\xA5" * 4
    assert c[0x90000] == bytes(16)


def test_profile_block_check():
    b = W.profile_block(2557, s=3)
    assert b[:8] == b"A64P\x00\x01\x00\x40"
    s = sum(struct.unpack(">15I", b[:60])) & 0xFFFFFFFF
    assert struct.unpack(">I", b[60:])[0] == (~s) & 0xFFFFFFFF
    assert W.profile_block(2557, bad_check=True) != b


def test_cases_cover_the_rulings():
    names = {c.name for c in W.cases()}
    assert {"wb_end", "cli_loop", "proj_rt", "lmb", "lmb_held", "claim_refused",
            "init_err"} <= names
    assert len(W.MUTANTS) <= 10
