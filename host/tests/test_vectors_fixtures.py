"""Integration tests against the vendored fixtures in fixtures/vectors/."""

from __future__ import annotations

import pytest

from host import vectors as V

pytestmark = pytest.mark.skipif(
    not (V.FIXTURES_DIR / V.MANIFEST_NAME).exists(),
    reason="vendored fixtures not present (run python3 -m host.vectors)",
)

SAMPLE_STEMS = ("NOP", "MOVE.b", "ADD.w", "MOVEM.l")


def _as_decoded(norm: dict) -> dict:
    n = {k: v for k, v in norm.items() if k != "name"}
    n["init_mem"] = [tuple(p) for p in n["init_mem"]]
    n["exp_mem"] = [tuple(p) for p in n["exp_mem"]]
    return n


@pytest.mark.parametrize("stem", SAMPLE_STEMS)
def test_vendored_roundtrip_identity(stem: str) -> None:
    cases = load(stem)
    blob = V.compile_batch(cases)
    assert V.decode_batch(blob) == [_as_decoded(V.normalize(c))
                                    for c in cases]


def load(stem: str):
    cases = V.load_fixture(stem)
    assert 0 < len(cases) <= V.DEFAULT_SAMPLE_N
    return cases


@pytest.mark.parametrize("stem", SAMPLE_STEMS)
def test_vendored_cases_pass_filter(stem: str) -> None:
    for case in load(stem):
        assert V.classify(case) is None


@pytest.mark.parametrize("stem", SAMPLE_STEMS)
def test_vendored_cases_fit_buf_case(stem: str) -> None:
    for case in load(stem):
        assert V.case_bin_size(V.normalize(case)) <= V.MAX_CASE_BYTES


@pytest.mark.parametrize("stem", SAMPLE_STEMS)
def test_opcode_word_matches_name(stem: str) -> None:
    # the hex opcode in the upstream case name must be fetchable from
    # materialized memory at the initial PC (big-endian)
    for case in load(stem):
        n = V.normalize(case)
        mem = dict(n["init_mem"])
        opcode = (mem[n["pc"]] << 8) | mem[(n["pc"] + 1) & V.ADDR_MASK]
        assert opcode == int(case["name"].split()[0], 16)


def test_vendored_sr_is_user_ccr_only() -> None:
    for stem in SAMPLE_STEMS:
        for case in load(stem):
            n = V.normalize(case)
            assert n["sr"] & ~V.CCR_MASK == 0
            assert n["exp_sr"] & ~V.CCR_MASK == 0


def test_manifest_matches_fixtures() -> None:
    manifest = V.load_manifest()
    assert manifest["commit"] == V.UPSTREAM_COMMIT
    for stem in V.M1_FILES:
        assert stem in manifest["files"]
        assert V.fixture_path(stem).exists()


def test_offline_cli_end_to_end(tmp_path) -> None:
    out = tmp_path / "vectors"
    rc = V.main(["--out", str(out), "--offline",
                 "--only", "MOVE.b,ADD.w,NOP"])
    assert rc == 0
    for stem in ("MOVE.b", "ADD.w", "NOP"):
        blob = (out / f"{stem}.bin").read_bytes()
        assert V.decode_batch(blob)  # parses cleanly, non-empty


def test_offline_compile_deterministic(tmp_path) -> None:
    out_a = tmp_path / "a"
    out_b = tmp_path / "b"
    assert V.main(["--out", str(out_a), "--offline", "--only", "ADD.w"]) == 0
    assert V.main(["--out", str(out_b), "--offline", "--only", "ADD.w"]) == 0
    assert (out_a / "ADD.w.bin").read_bytes() == \
        (out_b / "ADD.w.bin").read_bytes()


def test_revendor_encoding_deterministic() -> None:
    # re-encoding the already-vendored cases must be byte-identical to the
    # fixture on disk (gzip mtime pinned, sorted JSON keys)
    stem = "NOP"
    cases = V.load_fixture(stem)
    on_disk = V.fixture_path(stem).read_bytes()
    assert V.encode_fixture(stem, cases, V.DEFAULT_SAMPLE_N) == on_disk
