"""Tests for the M1 golden-vector pipeline (host/vectors.py)."""

from __future__ import annotations

import copy
import struct
from typing import Any

import pytest

from host import vectors as V


# --- synthetic case builder -------------------------------------------------

def make_case(**overrides: Any) -> dict[str, Any]:
    """A minimal well-formed upstream-shaped case: NOP at pc=0x1000."""
    initial = {
        "d0": 0x11111111, "d1": 0x22222222, "d2": 3, "d3": 4,
        "d4": 5, "d5": 6, "d6": 7, "d7": 8,
        "a0": 0x100, "a1": 0x200, "a2": 0x300, "a3": 0x400,
        "a4": 0x500, "a5": 0x600, "a6": 0x700,
        "usp": 0x8000, "ssp": 0x800,
        "sr": 0x2705,          # supervisor, int mask 7, CCR = 0b00101
        "pc": 0x1000,
        "prefetch": [0x4E71, 0x4E71],   # NOP; NOP
        "ram": [[0x1004, 0xAA], [0x1005, 0xBB]],
    }
    final = copy.deepcopy(initial)
    final["pc"] = 0x1002
    final["prefetch"] = [0x4E71, 0xBBAA]
    case = {
        "name": "4e71 [NOP] 1",
        "initial": initial,
        "final": final,
        "length": 4,
        "transactions": [["r", 4, 6, 0x1004, ".w", 0xAABB], ["n", 2]],
    }
    for key, val in overrides.items():
        state, _, field = key.partition("_")
        if state in ("initial", "final") and field:
            case[state][field] = val
        else:
            case[key] = val
    return case


def exception_case() -> dict[str, Any]:
    """Supervisor-mode case taking a group-1/2 exception (3-word frame +
    vector-table read), modeled on real dropped cases from the suite."""
    c = make_case()
    c["final"]["pc"] = 0x1400
    c["final"]["ssp"] = c["initial"]["ssp"] - 6
    ssp = c["initial"]["ssp"]
    c["transactions"] = [
        ["n", 4],
        ["w", 4, 5, ssp - 4, ".w", 0x1000],   # PC low
        ["w", 4, 5, ssp - 6, ".w", 0x2705],   # SR
        ["w", 4, 5, ssp - 2, ".w", 0x0000],   # PC high
        ["r", 4, 5, 20, ".w", 0x0000],        # vector 5 hi
        ["r", 4, 5, 22, ".w", 0x1400],        # vector 5 lo
        ["r", 4, 6, 0x1400, ".w", 0x4E71],
    ]
    return c


# --- filter logic -----------------------------------------------------------

def test_normal_case_retained() -> None:
    assert V.classify(make_case()) is None


def test_trace_case_dropped() -> None:
    c = make_case()
    c["initial"]["sr"] |= 0x8000
    c["final"]["sr"] |= 0x8000
    assert V.classify(c) == "trace"


def test_exception_case_dropped() -> None:
    assert V.classify(exception_case()) == "exception"


def test_frame_writes_without_vector_read_kept() -> None:
    # e.g. MOVEM legitimately writing near SSP must not be misclassified
    c = exception_case()
    c["transactions"] = [t for t in c["transactions"] if t[0] != "r"]
    c["final"]["pc"] = 0x1002
    c["final"]["ssp"] = c["initial"]["ssp"] - 6
    assert V.classify(c) is None


def test_supervisor_bit_change_dropped() -> None:
    c = make_case()
    c["final"]["sr"] = c["initial"]["sr"] & ~0x2000
    assert V.classify(c) == "sr-system-change"


def test_user_mode_supervisor_cycle_dropped() -> None:
    c = make_case()
    c["initial"]["sr"] = 0x0005
    c["final"]["sr"] = 0x0005
    c["transactions"] = [["w", 4, 5, 0x7F0, ".w", 0]]  # FC2 set
    assert V.classify(c) == "exception"


def test_odd_pc_dropped() -> None:
    c = make_case()
    c["initial"]["pc"] = 0x1001
    c["final"]["pc"] = 0x1003
    assert V.classify(c) == "odd-pc"


def test_inactive_sp_change_dropped() -> None:
    c = make_case()
    c["final"]["usp"] += 4  # supervisor case: USP must stay put
    assert V.classify(c) == "inactive-sp-change"


def test_prefetch_conflict_dropped() -> None:
    c = make_case()
    c["initial"]["ram"].append([0x1000, 0x60])  # disagrees with 0x4E
    assert V.classify(c) == "prefetch-conflict"


def test_ram_matching_prefetch_is_no_conflict() -> None:
    c = make_case()
    c["initial"]["ram"].append([0x1000, 0x4E])
    c["final"]["ram"].append([0x1000, 0x4E])
    assert V.classify(c) is None


# --- normalization ----------------------------------------------------------

def test_sr_normalized_to_user_ccr() -> None:
    n = V.normalize(make_case())
    assert n["sr"] == 0x05
    assert n["exp_sr"] == 0x05


def test_supervisor_case_a7_is_ssp() -> None:
    c = make_case()
    c["final"]["ssp"] = 0x7FC  # e.g. a push happened
    n = V.normalize(c)
    assert n["regs"][15] == 0x800
    assert n["exp_regs"][15] == 0x7FC
    assert 0x8000 not in n["regs"]  # usp discarded


def test_user_case_a7_is_usp() -> None:
    c = make_case()
    c["initial"]["sr"] = 0x0005
    c["final"]["sr"] = 0x0005
    n = V.normalize(c)
    assert n["regs"][15] == 0x8000
    assert n["exp_regs"][15] == 0x8000


def test_prefetch_materialized_at_pc() -> None:
    n = V.normalize(make_case())
    mem = dict(n["init_mem"])
    # a naive interpreter fetching big-endian words at PC must see the opcode
    assert (mem[0x1000] << 8) | mem[0x1001] == 0x4E71
    assert (mem[0x1002] << 8) | mem[0x1003] == 0x4E71
    assert mem[0x1004] == 0xAA and mem[0x1005] == 0xBB


def test_exp_mem_asserts_unwritten_bytes_and_final_ram() -> None:
    c = make_case()
    c["final"]["ram"] = [[0x1004, 0x12], [0x1005, 0xBB]]
    n = V.normalize(c)
    exp = dict(n["exp_mem"])
    assert exp[0x1004] == 0x12          # written byte: final value wins
    assert exp[0x1000] == 0x4E          # opcode stream asserted unchanged
    assert exp[0x1005] == 0xBB


def test_addresses_masked_to_24_bit() -> None:
    c = make_case()
    c["initial"]["pc"] = 0xFF001000
    c["final"]["pc"] = 0xFF001002
    n = V.normalize(c)
    assert n["pc"] == 0x001000
    assert n["exp_pc"] == 0x001002


# --- sampling ---------------------------------------------------------------

def test_stride_sample_deterministic_and_sized() -> None:
    items = list(range(1000))
    a = V.stride_sample(items, 50)
    b = V.stride_sample(items, 50)
    assert a == b
    assert len(a) == 50
    assert a == sorted(a)  # original order preserved
    stride = 1000 // 50
    assert {y - x for x, y in zip(a, a[1:])} == {stride}


def test_stride_sample_small_input_passthrough() -> None:
    items = list(range(10))
    assert V.stride_sample(items, 50) == items


def test_filter_and_sample_stats() -> None:
    cases = [make_case() for _ in range(10)] + [exception_case()]
    sampled, stats = V.filter_and_sample(cases, 5)
    assert stats["total"] == 11
    assert stats["retained_pool"] == 10
    assert stats["sampled"] == 5
    assert stats["dropped"] == {"exception": 1}


# --- binary round-trip on synthetic cases -----------------------------------

def _as_decoded(norm: dict[str, Any]) -> dict[str, Any]:
    n = {k: v for k, v in norm.items() if k != "name"}
    n["init_mem"] = [tuple(p) for p in n["init_mem"]]
    n["exp_mem"] = [tuple(p) for p in n["exp_mem"]]
    return n


def test_pack_case_layout() -> None:
    n = V.normalize(make_case())
    blob = V.pack_case(n)
    assert len(blob) == 144 + 4 * (len(n["init_mem"]) + len(n["exp_mem"]))
    (case_len,) = struct.unpack_from("<H", blob, 0)
    assert case_len == len(blob)
    regs = struct.unpack_from("<16I", blob, 2)
    assert regs[0] == 0x11111111 and regs[15] == 0x800
    pc, sr = struct.unpack_from("<IH", blob, 66)
    assert pc == 0x1000 and sr == 0x05


def test_batch_roundtrip_synthetic() -> None:
    cases = [make_case() for _ in range(3)]
    cases[1]["initial"]["d3"] = 0xDEADBEEF
    cases[1]["final"]["d3"] = 0xDEADBEEF
    blob = V.compile_batch(cases)
    (count,) = struct.unpack_from("<H", blob, 0)
    assert count == 3
    decoded = V.decode_batch(blob)
    assert decoded == [_as_decoded(V.normalize(c)) for c in cases]


def test_compile_batch_rejects_unfiltered() -> None:
    with pytest.raises(ValueError, match="exception"):
        V.compile_batch([exception_case()])


def test_decode_batch_rejects_trailing_garbage() -> None:
    blob = V.compile_batch([make_case()]) + b"\x00"
    with pytest.raises(ValueError, match="trailing"):
        V.decode_batch(blob)


# --- determinism ------------------------------------------------------------

def test_fixture_encoding_deterministic() -> None:
    cases = [make_case(), make_case()]
    assert V.encode_fixture("NOP", cases, 50) == \
        V.encode_fixture("NOP", cases, 50)


def test_compile_deterministic() -> None:
    cases = [make_case() for _ in range(4)]
    assert V.compile_batch(cases) == V.compile_batch(cases)
