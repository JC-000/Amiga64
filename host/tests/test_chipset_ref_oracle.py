"""host/chipset_ref.py against FS-UAE (A500, OCS) through tests/chip_oracle.py.

The recorded legs need no emulator: tests/chip_oracle/fsuae_dumps.json is
FS-UAE's serial output for our own bare-metal case ROM (no Kickstart), and
the model must reproduce every case; every mutant in chip_oracle.MUTANTS
must make at least one case differ.  The live leg is opt-in
(AMIGA64_CHIP_ORACLE=1) and skips without FS-UAE or vasm.
"""
import json
import os

import pytest

from tests import chip_oracle as co

CASES = co.cases()
REC = json.load(open(co.RECORDED))


def test_recording_is_for_these_cases():
    assert REC["case_hash"] == co.case_hash(CASES), \
        "cases or head.s changed: python3 -m tests.chip_oracle --record"
    assert sorted(REC["cases"]) == sorted(c.name for c in CASES)


def test_oracle_covers_the_brief():
    names = {c.name for c in CASES}
    for need in ("copy_a", "cookie_ca", "clear_d", "desc_shift_copy", "fill_ife", "fill_efe",
                 "cia_oneshot", "cia_continuous", "cia_tb_counts_ta", "cia_icr_readclear",
                 # fix round 1: the review's cases and the gaps it lists
                 "cop_blt_nodang", "cop_low_dang", "cop_skip", "b_off_bsh", "a_stale_dat",
                 "c_stale_dat", "fill_both", "cia_latch0_tb", "desc_masks", "bzero_d_off",
                 "adkcon_setclr", "ipl_levels", "ipl_priority_mask", "tod_hi_halts",
                 "tod_mid_no_halt", "tod_latch", "tod_alarm"):
        assert need in names


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_model_matches_fsuae(case):
    ref = co.load_ref()
    d = co.diff(case, REC["cases"][case.name], co.run_model(case, ref))
    if case.limit:                      # documented limit: the model says so, loudly
        assert len(d) == 1 and d[0].startswith("LIMIT") and "CopperSkipError" in d[0]
    elif case.diverge:                  # W11-B: documented divergence, still differing
        assert d and not co.is_diff(d) and d[0] == f"LIMIT: {case.diverge}"
    else:
        assert d == []


def test_no_recorded_timeout():
    for name, c in REC["cases"].items():
        assert co.TIMEOUT_REC not in c["R"], name


def test_timeout_on_both_sides_is_not_a_match():
    c = next(x for x in CASES if x.name == "cia_oneshot")
    both = {"R": [co.TIMEOUT_REC], "D": {}}
    d = co.diff(c, both, {"R": [co.TIMEOUT_REC], "D": {}})
    assert d and d[0].startswith("INCONCLUSIVE") and co.is_diff(d)


def test_unexpected_model_error_is_a_diff():
    c = next(x for x in CASES if x.name == "copy_a")
    assert co.is_diff(co.diff(c, REC["cases"]["copy_a"], {"E": "NotImplementedError: x"}))


@pytest.mark.parametrize("name", list(co.MUTANTS))
def test_mutant_is_killed(name):
    ref = co.load_ref(co.mutant_source(name))
    res = co.compare(CASES, REC["cases"], ref)
    assert any(co.is_diff(d) for d in res.values()), f"mutant {name} survives every case"


def test_red_wrong_minterm_and_oneshot_rule_fail_the_diff():
    for name, case in (("minterm_term_swap", "cookie_ca"), ("oneshot_no_stop", "cia_oneshot")):
        res = co.compare(CASES, REC["cases"], co.load_ref(co.mutant_source(name)))
        assert co.is_diff(res[case]), (name, case)


def test_prefix_model_fails_the_review_cases():
    """fix round 1 red leg: the model as reviewed (the 5 bugs + TODMID halt)."""
    import pathlib
    old = pathlib.Path(co.CHIPSET_REF).read_text()
    for a, b in (("if reg < (0x040 if regs[0x02E >> 1] & 2 else 0x080):",
                  "if reg < 0x040 and not regs[0x02E >> 1] & 2:"),
                 ("if con1 & 0x18 == 0x08:  # IFE alone: edges drawn (both = EFE)",
                  "if con1 & 0x08:"),
                 ("span = self.ta_latch + 1                # latch 0: underflow every tick",
                  "span = self.ta_latch + 1 if self.ta_latch else 0x10000")):
        assert old.count(a) == 1
        old = old.replace(a, b)
    res = co.compare(CASES, REC["cases"], co.load_ref(old))
    bad = {k for k, d in res.items() if co.is_diff(d)}
    assert {"cop_blt_nodang", "cop_low_dang", "fill_both", "cia_latch0_tb"} <= bad


def test_diff_teeth():
    c = next(x for x in CASES if x.name == "copy_a")
    hw = json.loads(json.dumps(REC["cases"]["copy_a"]))
    mdl = co.run_model(c, co.load_ref())
    hx = hw["D"]["013000"]
    hw["D"]["013000"] = hx[:100] + ("0" if hx[100] != "0" else "1") + hx[101:]
    assert any("1 bytes differ" in d for d in co.diff(c, hw, mdl))
    hw2 = {"R": [1], "D": REC["cases"]["copy_a"]["D"]}
    assert co.diff(c, hw2, mdl)                          # an extra read
    assert co.diff(c, None, mdl)                         # a missing case


def test_parse_serial():
    got = co.parse_serial("CHIPORACLE 1\n@x\nR 0001 00FF\nD 013000 0aB1\nEND\n")
    assert got == {"x": {"R": [1, 255], "D": {"013000": "0ab1"}}}


def test_emitted_program_is_deterministic():
    assert co.emit_asm(CASES) == co.emit_asm(co.cases())


@pytest.mark.skipif(os.environ.get("AMIGA64_CHIP_ORACLE") != "1",
                    reason="live FS-UAE leg is opt-in (AMIGA64_CHIP_ORACLE=1)")
def test_live_fsuae(tmp_path):
    got = co.live(CASES, 180, str(tmp_path / "work"))
    if got is None:
        pytest.skip("no FS-UAE or vasm")
    hw, _ = got
    assert hw == REC["cases"]                            # FS-UAE is deterministic here
    res = co.compare(CASES, hw, co.load_ref())
    assert not any(co.is_diff(d) for d in res.values()), {k: v for k, v in res.items() if v}


def test_rom_tail_holds_the_iack_autovector_numbers(tmp_path):
    """68000 IACK reads the vector number from the ROM's last 16 bytes: they
    must be 24..31 (autovectors), or interrupts go through vector 0."""
    assert co.IACK_BYTES == bytes([0, 24, 0, 25, 0, 26, 0, 27, 0, 28, 0, 29, 0, 30, 0, 31])
    if not os.path.exists(os.path.join(co.VBCC, "bin", "vasmm68k_mot")):
        pytest.skip("no vasm")
    rom = open(co.build_rom(CASES, str(tmp_path)), "rb").read()
    assert len(rom) == co.ROM_SIZE and rom[-16:] == co.IACK_BYTES
