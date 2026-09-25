"""W5-H allocator: host-side layout pins (runtime/ops/hle_mem.s,
docs/HLE.md "Heap").  The allocator itself is exercised in VICE by
tests/run_vbcc.py (u_heap* units, heap_* programs)."""

import re
from pathlib import Path

from host import hle_env
from tests import run_vbcc

ROOT = Path(__file__).resolve().parents[2]


def _guest_equates():
    src = (ROOT / "runtime/rc/guest.inc").read_text()
    return {m.group(1): int(m.group(2), 16)
            for m in re.finditer(r"^(\w+)\s*=\s*\$([0-9A-Fa-f]+)", src, re.M)}


def test_heap_ctl_mirrors_guest_inc():
    eq = _guest_equates()
    for name in ("HEAP_CTL", "HEAP_FIRST"):
        assert eq[name] == getattr(hle_env, name), name
    assert hle_env.HEAP_CTL == hle_env.HEAP_BASE
    assert hle_env.HEAP_FIRST == hle_env.HEAP_CTL + 8
    assert hle_env.HEAP_FIRST % 8 == 0


def test_env_stages_a_pristine_heap():
    # hle_mem.s formats the heap when ENV_STATE+0 still holds HEAP_BASE
    blk = hle_env.state_block()
    assert int.from_bytes(blk[0:4], "big") == hle_env.HEAP_BASE
    # and the env never stages anything inside the heap region
    for c in hle_env.env_chunks(stack_top=0x00F000, entry=0x010000):
        end = c.guest_offset + len(c.data) - 1
        assert end < hle_env.HEAP_BASE or c.guest_offset > hle_env.HEAP_END


def test_unit_layout_constants():
    assert run_vbcc.P == hle_env.HEAP_FIRST + 8


def _prg(tmp_path, code: bytes) -> str:
    p = tmp_path / "r.prg"
    p.write_bytes(b"\x01\x08" + code)
    return str(p)


def test_pool_wiring_detection(tmp_path, monkeypatch):
    import tests.harness_util as hu
    labels = {"hle_pool_alloc": 0x3E37, "hle_pool_delete": 0x4AF8}
    monkeypatch.setattr(hu, "lookup_label", lambda lab, n: lab[n])
    wired = b"\x00\x20\x37\x3E\x00\x20\xF8\x4A"
    assert run_vbcc.pool_wiring(_prg(tmp_path, wired), labels) is True
    only_alloc = b"\x00\x20\x37\x3E\x00"
    assert run_vbcc.pool_wiring(_prg(tmp_path, only_alloc), labels) is False
    jmp_delete = b"\x20\x37\x3E\x4C\xF8\x4A"
    assert run_vbcc.pool_wiring(_prg(tmp_path, jmp_delete), labels) is True
    assert run_vbcc.pool_wiring(_prg(tmp_path, wired), {}) is None
