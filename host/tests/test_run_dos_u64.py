"""tests/run_dos.py u64 path fails closed (fake target, no device).

W5-T fix round 1: before the first scenario on hardware the suite must
refuse to run when the Command Interface is not Disabled, when a zeroing
readback is non-zero, or when hle_dos_init does not select RAMDISK.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests import run_dos as rd  # noqa: E402
from tests.run_program import Session  # noqa: E402

LABELS = {"dosio_want_uci": 0x4C87, "hle_dos_magic": 0x4C7E,
          "dosio_backend": 0x4C86, "hle_dos_init": 0x2000}


class Labels:
    def address(self, name):
        return LABELS.get(name.lstrip("."))


class Mem:
    """C64 RAM with optional write-protected addresses (a lost write)."""

    def __init__(self, stuck=()):
        self.ram = bytearray(0x10000)
        self.stuck = set(stuck)

    def read_memory(self, addr, n):
        return bytes(self.ram[addr:addr + n])

    def write_memory(self, addr, data):
        for i, b in enumerate(data):
            if addr + i not in self.stuck:
                self.ram[addr + i] = b


class Target:
    backend = "u64"

    def __init__(self, mem):
        self.transport = mem


class FakeSession(Session):
    """hle_dos_init as hle_dos.s does it: ULTDOS iff want == "UCI!" (the
    fake detection always reports a UCI) unless *init_backend* forces a
    value (a broken init)."""
    init_backend = None

    def _call(self, label):
        assert label == "hle_dos_init", label
        ram = self.target.transport.ram
        w = LABELS["dosio_want_uci"]
        be = 1 if bytes(ram[w:w + 4]) == b"UCI!" else 0
        if self.init_backend is not None:
            be = self.init_backend
        ram[LABELS["dosio_backend"]] = be
        m = LABELS["hle_dos_magic"]
        ram[m:m + 4] = b"DOS!"
        self.calls = getattr(self, "calls", 0) + 1


def make(stuck=(), residue=b"UCI!", magic=b"DOS!", init_backend=None):
    mem = Mem(stuck)
    w, m = LABELS["dosio_want_uci"], LABELS["hle_dos_magic"]
    mem.ram[w:w + 4] = residue
    mem.ram[m:m + 4] = magic
    s = FakeSession(target=Target(mem), labels=Labels(), timeout=1.0)
    s.init_backend = init_backend
    return s, mem


def test_preflight_clean_zeroes_and_proves_ramdisk():
    s, mem = make()
    rd.zero_dos_bss(s, s.labels)
    w, m = LABELS["dosio_want_uci"], LABELS["hle_dos_magic"]
    assert mem.ram[w:w + 4] == bytes(4)
    assert mem.ram[LABELS["dosio_backend"]] == 0
    assert s.calls == 1                        # hle_dos_init really ran
    assert mem.ram[m:m + 4] == bytes(4)        # re-zeroed: lazy init stays
    assert s.failures == []


@pytest.mark.parametrize("name", ["dosio_want_uci", "hle_dos_magic"])
def test_preflight_aborts_on_nonzero_readback(name):
    s, _ = make(stuck={LABELS[name] + 3})      # one byte never changes
    with pytest.raises(RuntimeError):
        rd.zero_dos_bss(s, s.labels)
    assert s.failures


def test_preflight_aborts_when_init_selects_ultdos():
    s, _ = make(init_backend=1)
    with pytest.raises(RuntimeError):
        rd.zero_dos_bss(s, s.labels)
    assert s.failures


@pytest.mark.parametrize("before,ok", [
    ({"C64 and Cartridge Settings": {"Command Interface": "Disabled"}}, True),
    ({"C64 and Cartridge Settings": {"Command Interface": "Enabled"}}, False),
    ({"C64 and Cartridge Settings": {}}, False),
    ({}, False),
])
def test_command_interface_must_be_disabled(before, ok):
    if ok:
        rd.u64_preflight(before)
    else:
        with pytest.raises(RuntimeError):
            rd.u64_preflight(before)
