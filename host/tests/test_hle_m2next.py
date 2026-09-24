"""Host-only tests for the M2-next HLE environment (WS-B1, docs/HLE.md).

Pins what the vbcc startup + vc.lib need from the staged guest env:
fake Process/CLI, console FileHandles (contract C2), HLE state block,
CLI launch stub, LVO slot placement for every implemented call, HLE index
ranges (contract C3), unimplemented-LVO sentinels, and the guest.inc
mirror of every new fixed address (contract C4).
"""

import re
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host import hle_env  # noqa: E402
from host.hle_dos_env import DOS_FILE_FUNCS  # noqa: E402
from host.image import STACK_TOP  # noqa: E402


def _mem(chunks):
    mem = {}
    for c in chunks:
        for i, b in enumerate(c.data):
            assert c.guest_offset + i not in mem, \
                f"chunks overlap at ${c.guest_offset + i:06X}"
            mem[c.guest_offset + i] = b
    return mem


def _env():
    return _mem(hle_env.env_chunks(STACK_TOP))


def _be32(mem, a):
    return int.from_bytes(bytes(mem[a + i] for i in range(4)), "big")


def _be16(mem, a):
    return int.from_bytes(bytes(mem[a + i] for i in range(2)), "big")


# --- LVO table / index allocation (C3) ---------------------------------------

EXPECTED_EXEC = {
    "OpenLibrary": -552, "CloseLibrary": -414, "FindTask": -294,
    "AllocMem": -198, "FreeMem": -210, "SetSignal": -306,
    "CreatePool": -696, "DeletePool": -702, "AllocPooled": -708,
    "FreePooled": -714, "AllocVec": -684, "FreeVec": -690, "StackSwap": -732,
}
EXPECTED_DOS_B1 = {
    "Output": -60, "Write": -48, "Input": -54, "Read": -42,
    "IsInteractive": -216,
}


@pytest.mark.parametrize("name,lvo", sorted(EXPECTED_EXEC.items()))
def test_exec_lvo(name, lvo):
    assert hle_env.EXEC_FUNCS[name][0] == lvo


@pytest.mark.parametrize("name,lvo", sorted(EXPECTED_DOS_B1.items()))
def test_dos_lvo(name, lvo):
    assert hle_env.DOS_FUNCS[name][0] == lvo


def test_ws_b1_indices_in_range_and_unique():
    b1 = {n: v for n, v in list(hle_env.EXEC_FUNCS.items())
          + list(hle_env.DOS_FUNCS.items()) if n not in DOS_FILE_FUNCS}
    idx = [i for _, i in b1.values()]
    assert all(0x00 <= i <= 0x2F for i in idx), idx
    assert len(set(idx)) == len(idx)
    # existing v0 indices keep their numbers
    assert hle_env.EXEC_FUNCS["OpenLibrary"][1] == 0x00
    assert hle_env.EXEC_FUNCS["CloseLibrary"][1] == 0x01
    assert hle_env.DOS_FUNCS["Output"][1] == 0x02
    assert hle_env.DOS_FUNCS["Write"][1] == 0x03
    # dense from $00: hle.s dispatches through a table of HLE_FN_COUNT
    assert sorted(idx) == list(range(len(idx)))
    assert hle_env.HLE_FN_COUNT == len(idx)


def test_all_indices_unique_across_packages():
    idx = [i for _, i in list(hle_env.EXEC_FUNCS.values())
           + list(hle_env.DOS_FUNCS.values())]
    assert len(set(idx)) == len(idx)
    for _, i in DOS_FILE_FUNCS.values():
        assert 0x30 <= i <= 0x4F


def test_each_lvo_claimed_once_per_library():
    for base, funcs in hle_env.LIBRARIES.values():
        lvos = [lvo for lvo, _ in funcs.values()]
        assert len(set(lvos)) == len(lvos)


def test_hle_s_mirrors_index_count():
    src = (ROOT / "runtime/ops/hle.s").read_text()
    m = re.search(r"^HLE_FN_COUNT\s*=\s*(\d+)", src, re.M)
    assert m and int(m.group(1)) == hle_env.HLE_FN_COUNT
    # dispatch table lists one handler per index, in index order
    lo = re.search(r"hle_tab_lo:\s*\n((?:\s*\.byte[^\n]*\n)+)", src).group(1)
    names = re.findall(r"<(\w+)", lo)
    assert len(names) == hle_env.HLE_FN_COUNT
    by_idx = {i: n for n, (_, i) in list(hle_env.EXEC_FUNCS.items())
              + list(hle_env.DOS_FUNCS.items()) if i < 0x30}
    for i, handler in enumerate(names):
        assert handler == "hle_" + by_idx[i].lower(), (i, handler)


# --- slots --------------------------------------------------------------------

def test_every_implemented_slot_holds_its_a1xx_word():
    mem = _env()
    for base, funcs in hle_env.LIBRARIES.values():
        for lvo, index in funcs.values():
            assert _be16(mem, base + lvo) == 0xA100 | index


def test_unimplemented_slots_hold_sentinel():
    mem = _env()
    # a few LVOs vbcc's Workbench path would call but we do not implement
    for base, lvo in [(hle_env.EXEC_BASE, -384), (hle_env.EXEC_BASE, -132),
                      (hle_env.EXEC_BASE, -738), (hle_env.EXEC_BASE, -6),
                      (hle_env.DOS_BASE, -126), (hle_env.DOS_BASE, -1158)]:
        assert _be16(mem, base + lvo) == hle_env.OP_LVO_UNIMPL
    assert hle_env.OP_LVO_UNIMPL & 0xFF00 not in (0xA100, 0xAF00)


def test_sentinel_does_not_cover_file_dos_slots():
    mem = _env()
    for lvo, index in DOS_FILE_FUNCS.values():
        assert _be16(mem, hle_env.DOS_BASE + lvo) == 0xA100 | index


# --- structures -----------------------------------------------------------------

def test_execbase_version_selects_exec_pools():
    """vc.lib uses exec memory pools when lib_Version >= 39 (else an
    Allocate/Deallocate MemHeader emulation); we advertise V40."""
    mem = _env()
    assert _be16(mem, hle_env.EXEC_BASE + 20) == hle_env.EXEC_VERSION == 40


def test_process_fields():
    mem = _env()
    p = hle_env.ENV_PROCESS
    assert p % 4 == 0
    assert mem[p + 8] == 13                                  # NT_PROCESS
    assert _be32(mem, p + 0x3A) == hle_env.ENV_STACK_LOWER       # tc_SPLower
    assert _be32(mem, p + 0x3E) == STACK_TOP + 4             # tc_SPUpper
    assert _be32(mem, p + 0x9C) == hle_env.FH_CONIN >> 2     # pr_CIS
    assert _be32(mem, p + 0xA0) == hle_env.FH_CONOUT >> 2    # pr_COS
    assert _be32(mem, p + 0xAC) == hle_env.ENV_CLI >> 2     # pr_CLI
    assert _be32(mem, p + 0xE0) == 0                         # pr_CES
    assert STACK_TOP + 4 - hle_env.ENV_STACK_LOWER >= 4096       # vbcc __stack


def test_cli_command_name_bstr():
    mem = _mem(hle_env.env_chunks(STACK_TOP, command_name=b"hello"))
    cli = hle_env.ENV_CLI
    assert cli % 4 == 0
    name = _be32(mem, cli + 0x10) << 2
    assert name == hle_env.ENV_CLI_NAME
    assert mem[name] == 5 and bytes(mem[name + 1 + i] for i in range(5)) == b"hello"
    assert mem[name + 6] == 0          # room for the NUL vbcc's startup writes


@pytest.mark.parametrize("attr,kind", [("FH_CONOUT", 0), ("FH_CONIN", 1),
                                        ("FH_NIL", 2)])
def test_filehandles(attr, kind):
    mem = _env()
    fh = getattr(hle_env, attr)
    assert fh % 4 == 0
    assert _be32(mem, fh + hle_env.FH_ARGS_OFF) == kind
    for off in range(hle_env.FH_SIZE):
        if not hle_env.FH_ARGS_OFF <= off < hle_env.FH_ARGS_OFF + 4:
            assert mem[fh + off] == 0, f"{attr}+{off}"
    assert hle_env.FH_SIZE == 44 and hle_env.FH_ARGS_OFF == 36
    assert hle_env.FH_KIND_FILE0 == 0x10


def test_fh_magic_retired():
    assert not hasattr(hle_env, "FH_MAGIC")


def test_hle_state_block():
    mem = _env()
    s = hle_env.ENV_STATE
    assert _be32(mem, s + 0) == hle_env.HEAP_BASE      # heap_next
    assert _be16(mem, s + 4) == 0                      # cursor
    assert mem[s + 6] == 0                             # column


def test_hle_clock_block():
    # m2-post: DateStamp state, staged as 0/0/0 (1978-01-01 00:00:00, tick 0)
    mem = _env()
    c = hle_env.ENV_CLOCK
    assert hle_env.ENV_STATE + hle_env.STATE_SIZE <= c
    assert c + hle_env.CLOCK_SIZE <= hle_env.ENV_LAUNCH
    assert bytes(mem[c + i] for i in range(8)) == bytes(8)
    assert hle_env.clock_block(7, 1439, 2999) == bytes.fromhex("00000007059F0BB7")
    with pytest.raises(ValueError):
        hle_env.clock_block(0, 1440, 0)
    with pytest.raises(ValueError):
        hle_env.clock_block(0, 0, 3000)


def test_heap_region():
    assert hle_env.HEAP_BASE == 0x100000 and hle_env.HEAP_END == 0x7FFFFF
    assert hle_env.HEAP_BASE % 8 == 0


@pytest.mark.parametrize("via_env", [False, True])
def test_launch_stub_encoding(via_env):
    if via_env:
        mem = _mem(hle_env.env_chunks(STACK_TOP, entry=0x010000, args=b"a b\n"))
        pc = hle_env.ENV_LAUNCH
    else:
        chunks, pc = hle_env.launch_chunks(0x010000, args=b"a b\n")
        mem = _mem(chunks)
    assert pc == hle_env.ENV_LAUNCH
    # lea ENV_ARGS.w,a0 / moveq #len,d0 / jmp entry.l
    assert _be16(mem, pc) == 0x41F8 and _be16(mem, pc + 2) == hle_env.ENV_ARGS
    assert _be16(mem, pc + 4) == 0x7004
    assert _be16(mem, pc + 6) == 0x4EF9 and _be32(mem, pc + 8) == 0x010000
    a = hle_env.ENV_ARGS
    assert bytes(mem[a + i] for i in range(5)) == b"a b\n\0"


def test_launch_args_limits():
    with pytest.raises(ValueError):
        hle_env.launch_chunks(0x010000, args=b"x" * 40)


# --- placement (C4): WS-B1 structures in $000400-$000BFF only -------------------

def test_env_structs_inside_ws_b1_region_and_disjoint():
    chunks = hle_env.env_chunks(STACK_TOP, entry=0x10000)
    _mem(chunks)                                       # no overlap
    for c in chunks:
        end = c.guest_offset + len(c.data) - 1
        if c.guest_offset in (hle_env.ABSEXECBASE_ADDR, STACK_TOP,
                              hle_env.ENV_LIBS):      # library region: C4 amd.
            continue
        assert 0x000400 <= c.guest_offset and end <= 0x000BFF, \
            f"${c.guest_offset:06X}-${end:06X}"


def test_guest_inc_mirrors_env_constants():
    src = (ROOT / "runtime/rc/guest.inc").read_text()
    eq = {m.group(1): int(m.group(2), 16)
          for m in re.finditer(r"^(\w+)\s*=\s*\$([0-9A-Fa-f]+)", src, re.M)}
    for name in ("ENV_STATE", "ENV_CLOCK", "ENV_LAUNCH", "ENV_ARGS", "ENV_CLI_NAME",
                 "ENV_CLI", "ENV_PROCESS", "FH_CONOUT", "FH_CONIN",
                 "FH_NIL", "HEAP_BASE", "HEAP_END"):
        assert eq[name] == getattr(hle_env, name), name
