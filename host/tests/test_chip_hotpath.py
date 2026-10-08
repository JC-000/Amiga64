"""W7-T (S3.1): the I/O trap adds instructions ONLY off the same-bank path.

Every line W7-T added to runtime/core.s carries a "; W7-T" tag.  Stripping
the tagged lines (and restoring the one retargeted jump, tagged
"W7-T (was <old>)") from the memory-API region must give back the base
fingerprint that host/tests/test_super_hotpath.py pins for main 66649e8;
the other hot-path regions must be untouched.  Structural checks pin WHERE
the tagged lines sit (docs/S3_PLAN.md §1.2-1.3): after MIO_PROG's
same-bank `beq done` and before `sta m_rbk`, in mio_full before its first
REC store, on STRADDLE's slow branch, in mio_split before the bank store,
and as the per-entry `trap: jmp chip_*` after an unconditional exit.
The self-tests show each check fires on a misplaced line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from host.tests import test_super_hotpath as hp

CORE = Path(__file__).resolve().parents[2] / "runtime" / "core.s"
BASE_MEMORY_API = (246, "55bef079f4cb52cf")   # main 66649e8 (test_super_hotpath)
TAG = "; W7-T"


def strip_w7t(src: str) -> str:
    out = []
    for line in src.splitlines(keepends=True):
        if TAG not in line:
            out.append(line)
            continue
        m = re.search(r"; W7-T \(was ([^)]*)\)", line)
        if m:                           # retargeted line: restore the base form
            label = re.match(r"^(\s*[@A-Za-z_]\w*:)?", line).group(0)
            out.append(f"{label}        {m.group(1)}\n")
            continue
        m = re.match(r"^(\s*[@A-Za-z_]\w*:)", line)
        if m:                           # keep a label that heads a tagged line
            out.append(m.group(1) + "\n")
    return "".join(out)


def _src() -> str:
    return CORE.read_text(encoding="utf-8")


def _region(src: str, name: str) -> str:
    a, b = hp.REGIONS[name]
    i = src.index(a)
    return src[i:src.index(b, i + len(a))]


def _macro(src: str, name: str) -> list[str]:
    i = src.index(f".macro {name}")
    j = src.index(".endmacro", i)
    return src[i:j].splitlines()


def _proc(src: str, name: str) -> list[str]:
    i = src.index(f".proc {name}\n")
    j = src.index(".endproc", i)
    return src[i:j].splitlines()


def _code(line: str) -> str:
    return " ".join(re.sub(r"^\s*[@A-Za-z_]\w*:", "",
                           line.split(";", 1)[0]).split())


def test_stripped_source_has_the_base_fingerprints() -> None:
    from host.tests.test_cwin_hotpath import restore_base   # W12-C WINCHK
    fp = hp.fingerprints(strip_w7t(restore_base(_src())))
    assert fp["memory_api"] == BASE_MEMORY_API
    for name, want in hp.EXPECTED.items():
        if name != "memory_api":
            assert fp[name] == want, name


def test_tagged_lines_only_in_the_memory_api() -> None:
    src = _src()
    for name in hp.REGIONS:
        if name != "memory_api":
            assert TAG not in _region(src, name), name
    assert TAG in _region(src, "memory_api")


def check_mio_prog(src: str) -> None:
    """Same-bank path of MIO_PROG untagged; the warm I/O test sits between
    the bank compare's `beq done` and `sta m_rbk`, and is exactly
    cmp #$DF / beq trap / cmp #$BF / beq trap (never taken: 8 cycles)."""
    m = _macro(src, "MIO_PROG")
    i = next(k for k, l in enumerate(m) if _code(l) == "cmp m_rbk")
    assert _code(m[i + 1]) == "BRNC beq, done"
    for l in m[:i + 2]:
        if TAG in l:
            # only the cold path's retargeted jump may be tagged up here
            assert _code(l) == "jmp hiok" and "was jmp done" in l, l
    test = [_code(l) for l in m[i + 2:i + 6]]
    assert test == ["cmp #$DF", "beq trap", "cmp #$BF", "beq trap"]
    assert all(TAG in l for l in m[i + 2:i + 6])
    assert _code(m[i + 6]) == "sta m_rbk" and TAG not in m[i + 6]
    assert _code(m[i + 7]) == "sta REC_REUBANK"


def check_mio_full(src: str) -> None:
    """The cold path tests the bank before its first REC store, and its
    I/O exit stores a non-I/O bank into m_rbk with m_rok left 0."""
    p = _proc(src, "mio_full")
    first_rec = next(k for k, l in enumerate(p) if "REC_" in _code(l))
    test = [_code(l) for l in p[:first_rec] if TAG in l]
    assert test[:5] == ["lda m_addr+2", "cmp #$DF", "beq io", "cmp #$BF",
                        "beq io"]
    io = next(k for k, l in enumerate(p) if re.match(r"^io:", l))
    assert [_code(l) for l in p[io:io + 3]] == ["lda #0", "sta m_rbk", "rts"]
    assert not any("m_rok" in _code(l) for l in p[io:])
    stores = [k for k, l in enumerate(p) if _code(l) == "sta m_rbk"]
    assert len(stores) == 2          # the programmed bank, and the I/O exit


def check_straddle(src: str) -> None:
    m = _macro(src, "STRADDLE")
    i = next(k for k, l in enumerate(m) if _code(l) == "BRNC beq, slow")
    assert not any(TAG in l for l in m[:i + 1])
    assert [_code(l) for l in m[i + 1:i + 6]] == [
        "lda m_addr+2", "cmp #$BE", "beq slow", "cmp #$DE", "beq slow"]
    assert _code(m[i + 6]) == "" and m[i + 6].startswith("no:")


def check_split(src: str) -> None:
    p = _proc(src, "mio_split")
    k = next(k for k, l in enumerate(p) if _code(l) == "sta REC_REUBANK")
    assert [_code(l) for l in p[k - 4:k]] == ["cmp #$DF", "beq io",
                                              "cmp #$BF", "beq io"]
    cmd = next(k for k, l in enumerate(p) if _code(l) == "sta REC_COMMAND")
    assert k < cmd
    io = next(k for k, l in enumerate(p) if re.match(r"^io:", l))
    assert [_code(l) for l in p[io:io + 2]] == ["jsr chip_split", "jmp next"]


ENTRIES = {"mem_read8": "chip_r8", "mem_read16": "chip_r16",
           "mem_read32": "chip_r32", "mem_write8": "chip_w8",
           "mem_write16": "chip_w16", "mem_write32": "chip_w32"}


def check_entries(src: str) -> None:
    for entry, target in ENTRIES.items():
        p = [l for l in _proc(src, entry) if _code(l)]
        traps = [k for k, l in enumerate(p) if re.match(r"^trap:", l)]
        assert len(traps) == 1, entry
        k = traps[0]
        assert _code(p[k]) == f"jmp {target}" and TAG in p[k], entry
        assert _code(p[k - 1]).split()[0] in ("rts", "jmp"), entry


CHECKS = [check_mio_prog, check_mio_full, check_straddle, check_split,
          check_entries]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_structure(check) -> None:
    check(_src())


# --- teeth: each structural check / the fingerprint fires on a misplaced line

def _mutate(src: str, anchor: str, new: str) -> str:
    assert src.count(anchor) == 1, anchor
    return src.replace(anchor, new)


MUTANTS = {
    # an I/O test on the same-bank path (tagged: only the structure sees it)
    "same_bank_test": (check_mio_prog,
                       "        cmp m_rbk               ; 3\n",
                       "        cmp #$DF                ; W7-T\n"
                       "        cmp m_rbk               ; 3\n"),
    # m_rbk stored before the I/O test
    "rbk_first": (check_mio_prog,
                  "        BRNC beq, done                ; 3\n"
                  "        cmp #$DF                ; W7-T 2  bank changed: I/O?\n",
                  "        BRNC beq, done                ; 3\n"
                  "        sta m_rbk               ; W7-T\n"
                  "        cmp #$DF                ; W7-T 2  bank changed: I/O?\n"),
    # the cold test moved after the first REC store
    "cold_late": (check_mio_full,
                  "        sta m_rlen\n        lda m_addr+2            ; W7-T",
                  "        sta m_rlen\n        sta REC_LENLO\n"
                  "        lda m_addr+2            ; W7-T"),
    # the split test after the command store
    "split_late": (check_split,
                   "        beq io                  ; W7-T\n"
                   "        sta REC_REUBANK\n",
                   "        sta REC_REUBANK\n"
                   "        beq io                  ; W7-T\n"),
}


@pytest.mark.parametrize("name", list(MUTANTS))
def test_structure_checks_have_teeth(name: str) -> None:
    check, anchor, new = MUTANTS[name]
    with pytest.raises((AssertionError, StopIteration)):
        check(_mutate(_src(), anchor, new))


def test_fingerprint_catches_an_untagged_line() -> None:
    src = _mutate(_src(), "        cmp m_rbk               ; 3\n",
                  "        cmp m_rbk               ; 3\n        nop\n")
    assert hp.fingerprints(strip_w7t(src))["memory_api"] != BASE_MEMORY_API
