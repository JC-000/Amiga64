"""W7-T (S3.1): vector cases that touch the I/O banks $BF/$DF are excluded.

Banks $BF (CIAs) and $DF (custom chips) are chip registers since the I/O
trap (docs/CONTRACTS.md "I/O trap ($BF/$DF)"), while SingleStepTests
assumes flat RAM there.  Supervisor ruling (Wave 7): a POST-sample
exclusion (host/vectors.py exclude_io_bank), never a re-sample, so every
other case stays bit-identical to the hardware-validated set.
"""

from __future__ import annotations

import pytest

from host import vectors as v


def _case(ram_addr: int, pc: int = 0x001000, final_addr: int | None = None):
    fa = ram_addr if final_addr is None else final_addr
    return {"name": f"t {ram_addr:06X}",
            "initial": {"pc": pc, "sr": 0x2700, "ram": [[ram_addr, 1]]},
            "final": {"pc": pc + 2, "sr": 0x2700, "ram": [[fa, 2]]}}


@pytest.mark.parametrize("addr", [0xBF0000, 0xBFE001, 0xBFFFFF, 0xDF0000,
                                  0xDFF180, 0xDFFFFF, 0x7FBFE001])
def test_io_bank_case_is_excluded(addr: int) -> None:
    assert v.touches_io_bank(_case(addr))


@pytest.mark.parametrize("addr", [0xBEFFFF, 0xC00000, 0xDEFFFF, 0xE00000,
                                  0x00BF00, 0x0000DF])
def test_neighbour_banks_are_kept(addr: int) -> None:
    assert not v.touches_io_bank(_case(addr))


def test_final_ram_and_prefetch_count() -> None:
    assert v.touches_io_bank(_case(0x001000, final_addr=0xDF1234))
    assert v.touches_io_bank(_case(0x001000, pc=0xBFFFFE))


def test_exclusion_is_post_sample_without_resample(monkeypatch) -> None:
    # keep the pre-sample filter out of the way: every synthetic case is
    # a retained user-mode case
    monkeypatch.setattr(v, "classify", lambda c: None)
    monkeypatch.setattr(v, "case_bin_size", lambda n: 0)
    monkeypatch.setattr(v, "normalize", lambda c: c)
    pool = [_case(0x010000 + 16 * i) for i in range(100)]
    pool[4] = _case(0xDFF180)                 # a sampled one (stride 2)
    pool[5] = _case(0xBF0000)                 # an unsampled one
    base = v.stride_sample(pool, 50)
    got, stats = v.filter_and_sample(iter(pool), 50)
    assert got == [c for c in base if c is not pool[4]]    # pure deletion
    assert stats["sampled"] == 49
    assert stats["dropped_after_sampling"] == {v.IO_BANK_REASON: 1}
    assert stats["dropped"] == {}                         # distinct reason


def test_vendored_fixtures_have_no_io_bank_case() -> None:
    for stem in v.M1_FILES + v.SUPER_FILES:
        cases = v.load_fixture(stem)
        assert not any(v.touches_io_bank(c) for c in cases), stem


def test_counts_after_exclusion() -> None:
    m1 = sum(len(v.load_fixture(s)) for s in v.M1_FILES)
    sup = sum(len(v.load_fixture(s)) for s in v.SUPER_FILES)
    assert m1 == 5150 - 21
    assert sup + len(v.super_hand_cases()) == 679 - 1
    man = v.load_manifest()["files"]
    assert sum(f.get("dropped_after_sampling", {}).get(v.IO_BANK_REASON, 0)
               for f in man.values()) == 22


def _mem_addrs(norm):
    return [a for a, _ in norm["init_mem"]] + [a for a, _ in norm["exp_mem"]]


def test_hand_vectors_touch_no_io_bank() -> None:
    from host import handvec
    cases = (v.decode_batch(handvec.build_batch())
             + v.decode_batch(handvec.build_wrap_batch()))
    assert len(cases) == 63                 # HAND 48 + WRAP 15
    for c in cases:
        assert not any((a >> 16) in v.IO_BANKS for a in _mem_addrs(c))
    for c in v.super_hand_cases():
        assert not any((a >> 16) in v.IO_BANKS for a in _mem_addrs(c)), c["name"]
