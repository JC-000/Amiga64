"""Library-table region and process context (b1-review fix round 1).

C4 amendment (docs/WAVE4_BRIEF.md): library bases and their FULL vector
tables live in guest region `hle $0F0000-$0FFFFF`; the program load region
is `$010000-$0EFFFF`.  Every slot down to the deepest known LVO (exec 828,
dos 1158) is staged — implemented ($A1xx) or sentinel ($A0FF) — with no
overlap and all padding staged.  Also: full-size ExecBase fields read via
SysBase (F1), library versions (F3), exec AllocVec/FreeVec/StackSwap (F4),
stack fields consistent with the real stack (F4/F7), env gaps staged (F7).

Expectations are literal where possible so the file goes red against the
pre-fix environment for the right reason.
"""

import re
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host import hle_env, image  # noqa: E402
from host.hle_dos_env import DOS_FILE_FUNCS  # noqa: E402
from host.image import STACK_TOP  # noqa: E402

LIB_LO, LIB_HI = 0x0F0000, 0x0FFFFF
EXEC_DEPTH, DOS_DEPTH = 828, 1158
EXECBASE_SIZE = 632
SENTINEL = 0xA0FF


def _mem(chunks):
    mem = {}
    for c in chunks:
        for i, b in enumerate(c.data):
            a = c.guest_offset + i
            assert a not in mem, f"chunks overlap at ${a:06X}"
            mem[a] = b
    return mem


@pytest.fixture(scope="module")
def mem():
    return _mem(hle_env.env_chunks(STACK_TOP))


def be32(mem, a):
    return int.from_bytes(bytes(mem[a + i] for i in range(4)), "big")


def be16(mem, a):
    return int.from_bytes(bytes(mem[a + i] for i in range(2)), "big")


# --- F1: region + full tables ----------------------------------------------------

def test_guest_inc_carves_library_region():
    src = (ROOT / "runtime/rc/guest.inc").read_text()
    regions = re.findall(r"^; @region (\w+) \$([0-9A-F]{6})-\$([0-9A-F]{6})", src, re.M)
    spans = {(int(lo, 16), int(hi, 16)): owner for owner, lo, hi in regions}
    assert spans.get((0x010000, 0x0EFFFF)) == "run"
    assert spans.get((LIB_LO, LIB_HI)) == "hle"


def test_bases_in_library_region_and_absexecbase(mem):
    for base in (hle_env.EXEC_BASE, hle_env.DOS_BASE):
        assert LIB_LO <= base <= LIB_HI
    assert be32(mem, 4) == hle_env.EXEC_BASE


@pytest.mark.parametrize("lib,depth", [("exec.library", EXEC_DEPTH),
                                       ("dos.library", DOS_DEPTH)])
def test_every_slot_staged_down_to_deepest_lvo(mem, lib, depth):
    base, funcs = hle_env.LIBRARIES[lib]
    implemented = {lvo: 0xA100 | idx for lvo, idx in funcs.values()}
    for lvo in range(-6, -depth - 1, -6):
        a = base + lvo
        assert LIB_LO <= a and a + 5 < base, f"{lib} {lvo}"
        want = implemented.get(lvo, SENTINEL)
        assert be16(mem, a) == want, f"{lib} LVO {lvo} at ${a:06X}"
        for k in range(2, 6):
            assert a + k in mem


def test_tables_and_bases_disjoint_and_padding_staged(mem):
    eb, db = hle_env.EXEC_BASE, hle_env.DOS_BASE
    spans = sorted([(eb - EXEC_DEPTH, eb + EXECBASE_SIZE),
                    (db - DOS_DEPTH, db + 70)])
    assert spans[0][1] <= spans[1][0], spans
    lo, hi = min(s[0] for s in spans), max(s[1] for s in spans)
    missing = [a for a in range(LIB_LO, hi) if a not in mem]
    assert not missing, f"unstaged bytes in the library area, first ${missing[0]:06X}"


def test_no_library_bytes_outside_its_region(mem):
    for a in mem:
        assert not (0x010000 <= a <= 0x0EFFFF), f"${a:06X} in the load region"


def test_file_dos_slots_still_theirs(mem):
    for lvo, idx in DOS_FILE_FUNCS.values():
        assert be16(mem, hle_env.DOS_BASE + lvo) == 0xA100 | idx


def test_lvo_depth_exported():
    assert hle_env.LVO_DEPTH == {"exec.library": EXEC_DEPTH,
                                 "dos.library": DOS_DEPTH}


# --- F1: ExecBase fields ---------------------------------------------------------

def test_execbase_fields(mem):
    s = hle_env.EXEC_BASE
    assert mem[s + 8] == 9                                  # NT_LIBRARY
    assert be16(mem, s + 16) == EXEC_DEPTH                  # lib_NegSize
    assert be16(mem, s + 18) == EXECBASE_SIZE               # lib_PosSize
    assert be16(mem, s + 20) == 40                          # lib_Version
    assert be32(mem, s + 276) == hle_env.ENV_PROCESS        # ThisTask
    assert be16(mem, s + 296) == 0                          # AttnFlags: 68000
    assert be32(mem, s + 62) == 0x200000                    # MaxLocMem
    name = be32(mem, s + 10)                                # ln_Name
    assert bytes(mem[name + i] for i in range(13)) == b"exec.library\0"
    for a in range(s, s + EXECBASE_SIZE):
        assert a in mem


def test_dosbase_fields(mem):
    d = hle_env.DOS_BASE
    assert mem[d + 8] == 9
    assert be16(mem, d + 16) == DOS_DEPTH
    assert be16(mem, d + 20) == 40                          # F3
    name = be32(mem, d + 10)
    assert bytes(mem[name + i] for i in range(12)) == b"dos.library\0"


# --- F4/F7: exec calls + stack fields ----------------------------------------------

@pytest.mark.parametrize("name,lvo", [("AllocVec", -684), ("FreeVec", -690),
                                      ("StackSwap", -732)])
def test_f4_exec_calls(name, lvo):
    assert hle_env.EXEC_FUNCS[name][0] == lvo
    assert 0 <= hle_env.EXEC_FUNCS[name][1] <= 0x2F


def test_stack_fields_consistent(mem):
    p = hle_env.ENV_PROCESS
    lower, upper = be32(mem, p + 0x3A), be32(mem, p + 0x3E)
    assert lower == 0x001000
    # the exit longword at [STACK_TOP] is the top of the stack, inside it
    assert upper == STACK_TOP + 4
    assert be32(mem, p + 0x84) == upper - lower             # pr_StackSize
    cli = be32(mem, p + 0xAC) << 2
    assert be32(mem, cli + 0x34) == (upper - lower) // 4    # cli_DefaultStack


def test_env_gaps_staged(mem):
    missing = [a for a in range(0x000400, 0x000C00) if a not in mem]
    assert not missing, f"unstaged env bytes, first ${missing[0]:06X}"


def test_program_reaching_library_region_rejected():
    """host/image.py does not bound the load region (no grant to add it);
    drivers call hle_env.check_program_fits(manifest) before staging."""
    from host import fixturegen, hunk
    b = fixturegen.HunkBuilder()
    b.add_code([0x4E75])
    b.add_bss(0x0F0000 - 0x010000)           # ends exactly at $0F0004+
    _, manifest = image.build_image(hunk.parse(b.build()))
    with pytest.raises(ValueError, match="load region"):
        hle_env.check_program_fits(manifest)
    b = fixturegen.HunkBuilder()
    b.add_code([0x4E75])
    b.add_bss(0x0F0000 - 0x010000 - 8)       # last byte $0EFFFF: fits
    _, manifest = image.build_image(hunk.parse(b.build()))
    hle_env.check_program_fits(manifest)
