"""Tests for the W6-S supervisor/exception vector set (host/vectors.py)."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from host import vectors as V
from host.tests.test_vectors import exception_case, make_case

ROOT = Path(__file__).resolve().parents[2]


def _addr_error_case():
    c = exception_case()
    tr = c["transactions"]
    tr[4] = ["r", 4, 5, 12, ".w", 0x0000]     # vector 3 = address error
    tr[5] = ["r", 4, 5, 14, ".w", 0x1400]
    return c


# --- exception detection / filter ------------------------------------------

def test_exception_vector_detected() -> None:
    assert V.exception_vector(exception_case()) == 5
    assert V.exception_vector(_addr_error_case()) == 3
    assert V.exception_vector(make_case()) is None


def test_exception_vector_ignores_low_reads_not_leading_to_final_pc() -> None:
    c = exception_case()
    c["final"]["pc"] = 0x1002        # vector value != final PC
    assert V.exception_vector(c) is None


def test_exception_vector_needs_transactions() -> None:
    c = exception_case()
    del c["transactions"]
    assert V.exception_vector(c) is None


def test_classify_super_keeps_exceptions_drops_address_errors() -> None:
    assert V.classify_super(exception_case()) is None
    assert V.classify_super(_addr_error_case()) == "address-error"
    assert V.classify_super(make_case()) is None


def test_classify_super_exc_only() -> None:
    assert V.classify_super(make_case(), exc_only=True) == "no-exception"
    assert V.classify_super(exception_case(), exc_only=True) is None


def test_classify_super_trace_oddpc_prefetch() -> None:
    assert V.classify_super(make_case(initial_sr=0xA700)) == "trace"
    assert V.classify_super(make_case(initial_pc=0x1001)) == "odd-pc"
    c = make_case()
    c["initial"]["ram"].append([0x1000, 0x12])
    assert V.classify_super(c) == "prefetch-conflict"


def test_classify_super_keeps_supervisor_changes() -> None:
    # an SR system-byte change (RTE/MOVE to SR) is exactly what M1 drops
    c = make_case(final_sr=0x0005)
    c["final"]["ssp"] = 0x806
    assert V.classify(c) == "sr-system-change"
    assert V.classify_super(c) is None


# --- normalization ----------------------------------------------------------

def test_normalize_super_full_sr_and_both_sps() -> None:
    c = make_case(final_sr=0x0005)       # S -> U (e.g. RTE)
    c["final"]["usp"] = 0x9000
    c["final"]["ssp"] = 0x806
    n = V.normalize_super(c)
    assert n["sr"] == 0x2705 and n["exp_sr"] == 0x0005
    assert n["regs"][15] == 0x800 and n["osp"] == 0x8000     # A7=SSP, USP
    assert n["exp_regs"][15] == 0x9000 and n["exp_osp"] == 0x806
    assert n["flags"] == V.FLAG_EXC and n["exp_halt"] == 0


def test_normalize_super_user_start() -> None:
    c = make_case(initial_sr=0x0005, final_sr=0x2005)
    n = V.normalize_super(c)
    assert n["regs"][15] == 0x8000 and n["osp"] == 0x800
    assert n["exp_regs"][15] == 0x800 and n["exp_osp"] == 0x8000


# --- binary format ------------------------------------------------------------

def test_super_pack_decode_roundtrip() -> None:
    norms = [V.normalize_super(exception_case()), V.normalize_super(make_case())]
    norms[1]["exp_halt"], norms[1]["exp_reason"] = 1, V.HALT_STOP
    blob = V.pack_super_norms(norms)
    out = V.decode_super_batch(blob)
    for a, b in zip(norms, out):
        for k in ("regs", "pc", "sr", "exp_regs", "exp_pc", "exp_sr", "osp",
                  "exp_osp", "flags", "exp_halt", "exp_reason"):
            assert a[k] == b[k], k
        assert [tuple(p) for p in a["exp_mem"]] == b["exp_mem"]
        assert len(b["raw"]) == V.case_bin_size(a)
        assert len(b["ext"]) == V.SUPER_EXT_LEN


def test_super_record_is_a_standard_record() -> None:
    # the driver feeds the record part to the UNCHANGED test_run_case
    from tests.case_format import decode_batch as tdecode
    n = V.normalize_super(exception_case())
    rec = V.pack_super_case(n)[:-V.SUPER_EXT_LEN]
    assert rec == V.pack_case(n)
    (c,) = tdecode(b"\x01\x00" + rec)
    assert c.sr == n["sr"] and c.regs == n["regs"]


def test_decode_super_rejects_trailing() -> None:
    blob = V.pack_super_norms([V.normalize_super(make_case())])
    with pytest.raises(ValueError):
        V.decode_super_batch(blob + b"\x00")


def test_super_size_leaves_ext_area() -> None:
    assert V.MAX_SUPER_CASE_BYTES + V.SUPER_EXT_AREA == V.MAX_CASE_BYTES
    assert V.SUPER_EXT_LEN <= V.SUPER_EXT_AREA


# --- vendored super fixtures -------------------------------------------------

def test_super_fixtures_vendored_and_filtered() -> None:
    manifest = V.load_manifest()
    for stem in V.SUPER_FILES:
        assert stem in manifest["files"], stem
        cases = V.load_fixture(stem)
        assert 0 < len(cases) <= V.DEFAULT_SAMPLE_N
        exc_only = stem.endswith(V.EXC_SUFFIX)
        for case in cases:
            assert V.classify_super(case, exc_only) is None
            n = V.normalize_super(case)
            assert V.case_bin_size(n) <= V.MAX_SUPER_CASE_BYTES


def test_super_exclusions_are_address_errors_only() -> None:
    manifest = V.load_manifest()["files"]
    for stem in V.SUPER_FILES:
        dropped = set(manifest[stem]["dropped"])
        allowed = {"address-error"} | (
            {"no-exception"} if stem.endswith(V.EXC_SUFFIX) else set())
        assert dropped <= allowed, (stem, dropped)


def test_super_fixture_sources_pinned() -> None:
    import gzip
    import json
    for stem in V.SUPER_FILES:
        with gzip.open(V.fixture_path(stem)) as f:
            doc = json.load(f)
        assert doc["commit"] == V.UPSTREAM_COMMIT
        assert doc["source"] == f"{V.SUITE_PATH}/{V.upstream_stem(stem)}.json.gz"


def test_m1_and_super_sets_disjoint() -> None:
    assert not set(V.M1_FILES) & set(V.SUPER_FILES)


def test_offline_cli_super(tmp_path) -> None:
    out = tmp_path / "vs"
    assert V.main(["--set", "super", "--out", str(out), "--offline"]) == 0
    names = sorted(p.name for p in out.glob("*.bin"))
    assert names == sorted([f"{s}.bin" for s in V.SUPER_FILES]
                           + [f"{V.SUPER_HAND_STEM}.bin"])
    for p in out.glob("*.bin"):
        assert V.decode_super_batch(p.read_bytes())


def test_cli_super_rejects_m1_stem(tmp_path) -> None:
    with pytest.raises(SystemExit):
        V.main(["--set", "super", "--out", str(tmp_path), "--offline",
                "--only", "ADD.w"])


# --- hand cases ---------------------------------------------------------------

def test_hand_cases_fit_and_names_unique() -> None:
    cases = V.super_hand_cases()
    assert len({c["name"] for c in cases}) == len(cases)
    for c in cases:
        assert V.case_bin_size(c) <= V.MAX_SUPER_CASE_BYTES


def test_hand_flag_clear_cases_pin_todays_halts() -> None:
    clear = [c for c in V.super_hand_cases() if not c["flags"] & V.FLAG_EXC]
    assert len(clear) >= 20
    for c in clear:
        assert c["exp_halt"] == 1
        assert c["exp_reason"] in (V.HALT_ILLEGAL, V.HALT_ALINE, V.HALT_FLINE)
        assert c["exp_regs"] == c["regs"] and c["exp_osp"] == c["osp"]
        assert c["exp_sr"] == c["sr"] and c["exp_mem"] == c["init_mem"]
        assert c["exp_pc"] == c["pc"] + 2


def test_hand_cases_cover_every_exception_class() -> None:
    vectors = set()
    for c in V.super_hand_cases():
        if c["flags"] & V.FLAG_EXC and c["exp_pc"] in {
                V._handler(v) for v in V.HAND_VECTORS}:
            vectors.add((c["exp_pc"] - 0x4000) // 0x10)
    # illegal, div0, CHK, TRAPV, privilege, A-line, F-line, TRAP
    assert {4, 5, 6, 7, 8, 10, 11, 32, 33, 47} <= vectors


# --- resource contract mirrors ---------------------------------------------

def test_driver_fallback_matches_zp_inc() -> None:
    text = (ROOT / "runtime/rc/zp.inc").read_text()
    eq = dict(re.findall(r"^(\w+)\s*=\s*\$([0-9A-Fa-f]+)", text, re.M))
    assert int(eq["st_exc"], 16) == 0x88
    assert int(eq["r_osp"], 16) == 0x89
    drv = (ROOT / "tests/run_super_vectors.py").read_text()
    assert int(eq["st_ipc"], 16) == 0x8D
    assert '"st_exc": 0x88, "r_osp": 0x89, "st_ipc": 0x8D' in drv


def test_halt_codes_match_core() -> None:
    core = (ROOT / "runtime/core.s").read_text()
    for name in ("HALT_STOP", "HALT_ADDRESS"):
        m = re.search(rf"^{name}\s*=\s*\$([0-9A-Fa-f]+)", core, re.M)
        assert m and int(m.group(1), 16) == getattr(V, name)
    macros = (ROOT / "runtime/macros.inc").read_text()
    for name in ("HALT_OK", "HALT_ALINE", "HALT_FLINE", "HALT_ILLEGAL"):
        m = re.search(rf"^{name}\s*=\s*\$([0-9A-Fa-f]+)", macros, re.M)
        assert m and int(m.group(1), 16) == getattr(V, name)


def test_make_case_helper_unchanged() -> None:
    # guard: the shared synthetic builder still yields a supervisor NOP
    c = copy.deepcopy(make_case())
    assert c["initial"]["sr"] & V.SR_SUPER
