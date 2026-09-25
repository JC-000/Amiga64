"""tests/bench/u64bench.py: check_loaded and ViceBoot (no VICE, no device).

W5-T fix round 1: check_loaded must compare the whole loaded image (a
same-size build that differs anywhere is refused), masking only bytes the
boot itself rewrites; ViceBoot must stay usable after a failed start.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.bench import u64bench as ub  # noqa: E402

LOAD = 0x0801
MAIN = 0x1000
LABELS = {"test_run_program": 0x1800, "test_load_chunk": 0x1700,
          "mem_rec_dirty": 0x0C40, "main": MAIN}


class Labels:
    def address(self, name):
        return LABELS.get(name.lstrip("."))


class Mem:
    def __init__(self):
        self.ram = bytearray(0x10000)

    def read_memory(self, addr, n):
        return bytes(self.ram[addr:addr + n])


class Target:
    def __init__(self, backend="vice"):
        self.backend = backend
        self.transport = Mem()


def image(stub: bytes = b"") -> bytearray:
    """A 6 KB PRG body: BASIC stub page, then pseudo-random code."""
    body = bytearray((i * 37 + 11) & 0xFF for i in range(0x1800))
    body[0:0x0C] = bytes([0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + bytes(3)
    body[0x0C:0x0C + len(stub)] = stub
    body[0x0C + len(stub):0xFF] = bytes(0xFF - 0x0C - len(stub))
    return body


def write_prg(tmp_path, name, body) -> str:
    p = tmp_path / name
    p.write_bytes(bytes([LOAD & 0xFF, LOAD >> 8]) + bytes(body))
    return str(p)


def boot(t, body):
    t.transport.ram[LOAD:LOAD + len(body)] = body


def test_identical_image_passes(tmp_path):
    body = image()
    t = Target()
    boot(t, body)
    ub.check_loaded(t, write_prg(tmp_path, "a.prg", body), Labels())


@pytest.mark.parametrize("off", [0x0400, 0x1234, 0x17FF])
def test_same_size_build_differing_anywhere_is_refused(tmp_path, off):
    # far from every label: a 3x8-byte label sample cannot see it
    a = image()
    b = bytearray(a)
    b[off] ^= 0x5A
    t = Target()
    boot(t, a)
    with pytest.raises(RuntimeError, match="not the loaded PRG"):
        ub.check_loaded(t, write_prg(tmp_path, "b.prg", b), Labels())


def test_boot_stub_self_modified_operand_is_masked(tmp_path):
    # wave-5 boot stub: lda #>BSS / sta @pg+2 ... @pg: sta $xx00,y /
    # inc @pg+2 -- the page operand ($081E here) changes while booting
    stub = bytes([0xA9, 0x00, 0x85, 0x83, 0xA8, 0xA9, 0x4A, 0x8D, 0x1E, 0x08,
                  0x98, 0xA2, 0x05, 0xF0, 0x0B, 0x99, 0x00, 0x4A, 0xC8, 0xD0,
                  0xFA, 0xEE, 0x1E, 0x08, 0xCA, 0xD0, 0xF4, 0x4C, 0x00, 0x20])
    body = image(stub)
    assert body[0x081E - LOAD] == 0x4A
    t = Target()
    boot(t, body)
    t.transport.ram[0x081E] = 0x4F                 # after the clear loop
    ub.check_loaded(t, write_prg(tmp_path, "s.prg", body), Labels())
    t.transport.ram[0x081D] ^= 1                   # the byte next to it
    with pytest.raises(RuntimeError):
        ub.check_loaded(t, write_prg(tmp_path, "s.prg", body), Labels())


def test_u64_dispatcher_retarget_is_masked_only_on_u64(tmp_path):
    body = image()
    prg = write_prg(tmp_path, "d.prg", body)
    for backend, ok in (("u64", True), ("vice", False)):
        t = Target(backend)
        boot(t, body)
        t.transport.ram[MAIN + 7] = 0xCE           # harness_util retarget
        if ok:
            ub.check_loaded(t, prg, Labels())
        else:
            with pytest.raises(RuntimeError):
                ub.check_loaded(t, prg, Labels())


# ----------------------------------------------------------------- ViceBoot

def test_viceboot_usable_after_failed_start(monkeypatch):
    calls = []

    class Mgr:
        def __init__(self, **kw):
            pass

        def acquire(self):
            calls.append("acquire")
            raise RuntimeError("VICE failed to start")

        def release(self, t):
            calls.append("release")

        def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(ub, "UnifiedManager", Mgr)
    v = ub.ViceBoot()
    for _ in range(3):
        with pytest.raises(RuntimeError, match="VICE failed to start"):
            v.load("x.prg")
    v.close()                                      # no AttributeError
    assert calls.count("acquire") == 3
    assert calls.count("shutdown") == 3            # every manager shut down
    assert "release" not in calls                  # nothing was acquired
    assert v.mgr is None and v.target is None


def test_viceboot_close_after_failed_prepare(monkeypatch):
    calls = []

    class Mgr:
        def __init__(self, **kw):
            pass

        def acquire(self):
            return object()

        def release(self, t):
            calls.append("release")

        def shutdown(self):
            calls.append("shutdown")

    def bad_prepare(t, prg):
        raise TimeoutError("runner never ready")
    monkeypatch.setattr(ub, "UnifiedManager", Mgr)
    monkeypatch.setattr(ub, "prepare_target", bad_prepare)
    v = ub.ViceBoot()
    with pytest.raises(TimeoutError):
        v.load("x.prg")
    assert calls == ["release", "shutdown"]        # instance not leaked
    v.close()
    assert calls == ["release", "shutdown"]
