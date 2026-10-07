"""W11-B (S3.6 blitter v0): host-side checks of tests/run_blt.py and the
blt.s registration.  The guest legs themselves run in VICE (tests/run_blt.py)."""
from __future__ import annotations

import os
import random
import struct

import pytest

from host import rescheck
from tests import chip_oracle as co
from tests import run_blt as rb
from tests.run_c2v import makefile_sources

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BLT_S = os.path.join(ROOT, "runtime", "ops", "blt.s")


def test_emit_encodes_the_ops():
    w = rb.emit([("W", 0x040, 0x09F0), ("BLIT", 0x0103), ("R", 0x002, 0x2000)])
    assert w[:4] == [0x33FC, 0x8240, 0x00DF, 0xF096]          # DMACON = DMAEN | BLTEN
    assert w[4:7] == [0x49F9, rb.RECBUF >> 16, rb.RECBUF & 0xFFFF]
    assert w[7:11] == [0x33FC, 0x09F0, 0x00DF, 0xF040]
    assert w[11:15] == [0x33FC, 0x0103, 0x00DF, 0xF058]
    # WaitBlit: btst #6,DMACONR (8 bytes) / bne.s back to the btst
    assert w[15:19] == [0x0839, 0x0006, 0x00DF, 0xF002]
    assert w[19] & 0xFF00 == 0x6600
    disp = w[19] & 0xFF
    assert (2 * 19 + 2 + (disp - 256)) == 2 * 15
    assert w[20:25] == [0x3039, 0x00DF, 0xF002, 0x0240, 0x2000] and w[25] == 0x38C0
    assert w[-1] == rb.HALT


def test_long_and_byte_writes():
    w = rb.emit([("WL", 0x056, 0x81000103), ("WB", 0x059, 0x41)])
    assert w[7:12] == [0x23FC, 0x8100, 0x0103, 0x00DF, 0xF056]
    assert w[12:16] == [0x0839, 0x0006, 0x00DF, 0xF002]          # WaitBlit after a long
    assert w[17:21] == [0x13FC, 0x0041, 0x00DF, 0xF059]


def test_model_byte_write_lands_on_both_halves():
    m = rb.model_run([("W", 0x040, 0x0100), ("W", 0x054, 0x0002), ("W", 0x056, 0x8000),
                      ("WB", 0x059, 0x41)], [], [])
    assert m["chip"].regs[0x058 >> 1] == 0x4141


def test_line_mode_is_a_noop_in_the_reference_used_here():
    init = [(0x28000, bytes(range(256)) * 4)]
    ops = [*co.blit_setup(0x0BCA, 0x0001, a=0x8000, c=0x28000, d=0x28000, amod=-40,
                          cmod=40, dmod=40, bdat=0xFFFF), ("BLIT", (30 << 6) | 2),
           ("R", 0x01E, 0x0040)]
    dumps = [(0x28000, 0x400)]
    m = rb.model_run(ops, init, dumps)
    assert m["D"]["028000"] == init[0][1].hex()                # untouched
    assert m["R"] == [0x0040]                                  # completion still raised
    raw = rb.model_run(ops, init, dumps, line_noop=False)
    assert raw["D"]["028000"] != init[0][1].hex()              # the model's approximation draws


def test_oracle_leg_covers_the_brief_fixture():
    names = {c.name for c in rb.oracle_cases()}
    # copy, cookie-cut $CA with shifts and masks, clear, descending shifted copy, fills
    for n in ("copy_a", "cookie_ca", "desc_cookie_bsh", "clear_d", "desc_shift_copy",
              "fill_ife", "fill_ife_fci", "fill_efe", "fill_both", "blit_irq_bzero",
              "dmacon_bltpri", "aros_cd_ca_wide", "aros_bcd_ca"):
        assert n in names
    for c in rb.oracle_cases():
        assert all(op[0] in ("W", "BLIT", "R") for op in c.ops)
        assert any(op[0] == "BLIT" or (op[0] == "R" and op[1] == 0x002) for op in c.ops)


def test_oracle_data_below_the_guest_program():
    for c in rb.oracle_cases():
        for a, d in c.init:
            assert a + len(d) <= rb.CODE_AT
        for a, n in c.dumps:
            assert a + n <= rb.CODE_AT


@pytest.mark.parametrize("seed", [0x5306, 1, 2])
def test_rand_cases_stay_in_their_regions(seed):
    """Every word a random case's two blits touch lies in the staged regions
    (so the model never indexes outside its RAM and the C64 never touches
    the program or the record buffer)."""
    rng = random.Random(seed)
    for k in range(64):
        ops, init, dumps = rb.rand_case(rng, k)
        ref = co.load_ref()
        seen = set()

        class Spy(ref.Ram):
            def r16(self, a):
                seen.add(a)
                return super().r16(a)

            def w16(self, a, v):
                seen.add(a)
                super().w16(a, v)

        ram = Spy(rb.CHIP)
        chip = ref.Chipset(ram)
        rb.model_run(ops, init, dumps, ref=ref, chip=chip)
        lo, hi = rb.SRC_R, rb.DST_R + 0x4000
        assert all(lo <= a < hi - 1 for a in seen), (k, min(seen), max(seen))


def test_iobank_model_runs_in_two_megabytes():
    """The model masks every pointer to $1FFFFE: pointers in $BF/$DF/$9F/$FF
    land in chip RAM, and the descending blit below 0 wraps to $1FFFxx."""
    ref = co.load_ref()
    seen = []

    class Spy(ref.Ram):
        def w16(self, a, v):
            seen.append(a % rb.CHIP)
            super().w16(a, v)

    ram = Spy(rb.CHIP)
    ops = [*co.blit_setup(0x09F0, a=0xBF8000, d=0xDF0040), ("BLIT", (1 << 6) | 2),
           *co.blit_setup(0x01FF, 0x0002, d=0x000002), ("BLIT", (1 << 6) | 3)]
    rb.model_run(ops, [], [], ref=ref, chip=ref.Chipset(ram))
    assert seen[:2] == [0x1F0040, 0x1F0042]
    assert seen[2:] == [0x000002, 0x000000, 0x1FFFFE]


def test_mutant_targets_occur_once():
    src = open(BLT_S).read()
    assert len(rb.MUTANTS) <= 10
    for name, (path, edits) in rb.MUTANTS.items():
        assert path == "runtime/ops/blt.s"
        for old, _new in edits:
            assert src.count(old) == 1, (name, old)
        assert rb.MUTANT_LEGS[name] and set(rb.MUTANT_LEGS[name]) <= set(rb.LEGS)


def test_mutants_cover_the_brief():
    want = {"lforder", "descaddr", "fillfci", "maskswap", "modulo", "bpre", "bzero",
            "chipmask"}
    assert want <= set(rb.MUTANTS)


def test_blt_registered_and_linked_between_cop_and_disp():
    assert rescheck.FILE_OWNERS["runtime/ops/blt.s"] == "blt"
    srcs = [os.path.relpath(p, ROOT) for p in makefile_sources()]
    i = srcs.index("runtime/ops/blt.s")
    assert srcs[i - 1] == "runtime/ops/cop.s" and srcs[i + 1] == "runtime/ops/disp.s"


def test_dma_bank_always_masked():
    """Every REC bank store in blt.s goes through the 2 MB mask."""
    lines = open(BLT_S).read().splitlines()
    for i, ln in enumerate(lines):
        if "sta REC_REUBANK" in ln:
            assert "and #BLT_BANKS" in lines[i - 1], i
    assert "BLT_BANKS = $1F" in open(BLT_S).read()


def test_rowwise_model_differs_only_for_in_row_overlap():
    """rev-blt M1: the row-wise model (blt.s's fetch-then-store) equals the
    word-serial model on every oracle case except the in-row overlap ones."""
    differ = {c.name for c in rb.oracle_cases()
              if rb.model_run(c.ops, c.init, c.dumps)["D"]
              != rb.model_run(c.ops, c.init, c.dumps, rowwise=True)["D"]}
    assert differ == rb.ROWWISE_CASES


def test_documented_limit_cases_are_in_the_oracle():
    cs = {c.name: c for c in co.cases()}
    assert cs["p_odd_mod"].diverge and cs["p_overlap_asc"].diverge
    assert not cs["p_overlap_asc2"].diverge          # model = FS-UAE there
    assert rb.ROWWISE_CASES | rb.MODEL_ONLY_CASES <= {c.name for c in rb.oracle_cases()}
