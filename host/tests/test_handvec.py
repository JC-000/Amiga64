"""Tests for the hand-written vector batch (host/handvec.py)."""

from __future__ import annotations

from host import handvec as H
from host.vectors import MAX_CASE_BYTES, case_bin_size, decode_batch


def test_deterministic():
    assert H.build_batch() == H.build_batch()
    assert [c["name"] for c in H.all_cases()] == \
        [c["name"] for c in H.all_cases()]


def test_case_counts():
    cases = H.all_cases()
    assert len(cases) == 48
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names)
    # per-op minimum coverage (6+ cases each)
    for prefix in ("ORI-ccr", "ANDI-ccr", "EORI-ccr", "MOVEfSR",
                   "MOVEtCCR", "LINK", "UNLK"):
        assert sum(n.startswith(prefix) for n in names) >= 6, prefix


def test_cases_well_formed():
    for c in H.all_cases():
        mem = dict(c["init_mem"])
        # opcode word materialized big-endian at PC
        assert c["pc"] in mem and c["pc"] + 1 in mem, c["name"]
        assert c["pc"] % 2 == 0 and c["exp_pc"] % 2 == 0, c["name"]
        assert 0 <= c["sr"] <= 0x1F and 0 <= c["exp_sr"] <= 0x1F, c["name"]
        assert len(c["regs"]) == 16 and len(c["exp_regs"]) == 16, c["name"]
        # expected mem covers every init byte (unchanged-byte assertion)
        exp = dict(c["exp_mem"])
        assert set(mem) <= set(exp), c["name"]
        assert case_bin_size(c) <= MAX_CASE_BYTES, c["name"]
        # sorted, duplicate-free mem lists
        for key in ("init_mem", "exp_mem"):
            addrs = [a for a, _ in c[key]]
            assert addrs == sorted(addrs) and len(addrs) == len(set(addrs)), \
                c["name"]


def test_roundtrip_through_decode_batch():
    cases = H.all_cases()
    decoded = decode_batch(H.build_batch())
    assert len(decoded) == len(cases)
    for orig, back in zip(cases, decoded):
        for key in ("regs", "pc", "sr", "init_mem", "exp_regs",
                    "exp_pc", "exp_sr", "exp_mem"):
            assert back[key] == orig[key], (orig["name"], key)
