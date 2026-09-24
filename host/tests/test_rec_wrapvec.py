"""WS-A: the 512 KB-straddle / bank-carry hand vectors (host/handvec.py
WRAP batch) are well formed and really straddle what they claim to."""

from __future__ import annotations

from host import handvec as H
from host.vectors import MAX_CASE_BYTES, case_bin_size, decode_batch


def test_wrap_batch_roundtrip_and_shape():
    cases = H.wrap_cases()
    names = [c["name"] for c in cases]
    assert len(set(names)) == len(names) == len(cases) >= 12
    decoded = decode_batch(H.build_wrap_batch())
    assert len(decoded) == len(cases)
    for orig, back in zip(cases, decoded):
        assert back["exp_mem"] == orig["exp_mem"], orig["name"]
        assert case_bin_size(orig) <= MAX_CASE_BYTES, orig["name"]
        assert set(dict(orig["init_mem"])) <= set(dict(orig["exp_mem"]))


def test_hand_batch_unchanged():
    # the historical batch keeps its 48 cases (test_handvec pins it too)
    assert len(H.all_cases()) == 48


def test_straddle_cases_touch_both_sides_and_guard_the_wrap_target():
    by = {c["name"]: c for c in H.wrap_cases()}
    for a, target in ((0x07FFFE, 0x000000), (0x17FFFE, 0x100000),
                      (0xFFFFFE, 0xF80000)):
        st = by[f"WRAP-movel-st-{a:06x}"]
        init, exp = dict(st["init_mem"]), dict(st["exp_mem"])
        written = {k for k in exp if exp[k] != init.get(k)}
        hi_half = {(a + 2) & 0xFFFFFF, (a + 3) & 0xFFFFFF}
        assert hi_half <= written, st["name"]          # crosses the line
        if target != (a + 2) & 0xFFFFFF:
            # the REC's 19-bit wrap target is asserted unchanged
            assert target in init and exp[target] == init[target]
    # bank-carry cases keep a wrong-bank twin that must stay untouched
    for name, twin in (("BANK-movel-copy-01fffe", 0x020100),
                       ("BANK-cmpml-01fffe", 0x020200)):
        c = by[name]
        assert dict(c["exp_mem"])[twin] == dict(c["init_mem"])[twin]
