"""WS-A: the U64 mailbox dispatcher invalidates the REC shadow (C1).

tests/harness_util.u64_install_dispatcher writes a small 6502 loop into
the $CE00 page; contract C1 (docs/WAVE4_BRIEF.md) requires every
host-dispatched call to run mem_rec_dirty before the target.  This test
installs the dispatcher into a fake transport whose "CPU" is a minimal
6502 interpreter (just the opcodes the dispatcher uses) and checks the
JSR trace of real dispatched calls -- with and without the C1 hook.
"""

from __future__ import annotations

import pytest

pytest.importorskip("c64_test_harness")

from tests import harness_util as hu  # noqa: E402

MAIN = 0x2DB5                 # any address; main = lda #$A5 / sta abs / jmp *
REC_DIRTY = 0x0BF8
TARGET = 0x2CB4


class FakeCpuTransport:
    """64 KB RAM; writing MBOX=1 runs the dispatcher until it re-enters
    its spin loop, recording every JSR target."""

    rest_put_chunk_size = 4096

    def __init__(self):
        self.mem = bytearray(0x10000)
        idle = MAIN + hu.IDLE_JMP_OFFSET
        self.mem[MAIN:MAIN + 8] = bytes([0xA9, 0xA5, 0x8D, 0xFF, 0xCF,
                                         0x4C, idle & 0xFF, idle >> 8])
        self.mem[REC_DIRTY] = 0x60            # rts
        self.mem[TARGET] = 0x60               # rts
        self.jsrs: list[int] = []

    def read_memory(self, addr, length):
        return bytes(self.mem[addr:addr + length])

    def write_memory(self, addr, data):
        self.mem[addr:addr + len(data)] = data
        if addr == hu.U64_MBOX and data[:1] == b"\x01":
            self._run()

    def _run(self):
        m = self.mem
        idle = MAIN + hu.IDLE_JMP_OFFSET
        pc = m[idle + 1] | m[idle + 2] << 8      # follow the retargeted jmp
        a = 0
        z = False
        stack: list[int] = []
        spins = 0
        for _ in range(10_000):
            op = m[pc]
            w = m[pc + 1] | m[pc + 2] << 8
            if op == 0x4C:                                   # jmp abs
                pc = w
            elif op == 0xAD:                                 # lda abs
                a = m[w]; z = a == 0; pc += 3
            elif op == 0xA9:                                 # lda #
                a = m[pc + 1]; z = a == 0; pc += 2
            elif op == 0x8D:                                 # sta abs
                m[w] = a; pc += 3
            elif op == 0xF0:                                 # beq rel
                off = m[pc + 1]
                off = off - 256 if off & 0x80 else off
                if z:
                    spins += 1
                    if spins > 1:        # back in the idle spin: done
                        return
                pc = pc + 2 + (off if z else 0)
            elif op == 0x20:                                 # jsr abs
                self.jsrs.append(w); stack.append(pc + 3); pc = w
            elif op == 0x60:                                 # rts
                pc = stack.pop()
            else:
                raise AssertionError(f"unexpected opcode {op:02X} at {pc:04X}")
        raise AssertionError("dispatcher did not return to its spin loop")


class FakeTarget:
    backend = "u64"

    def __init__(self):
        self.transport = FakeCpuTransport()


@pytest.mark.parametrize("hook", [True, False])
def test_dispatcher_calls_rec_dirty_before_every_target(hook):
    tgt = FakeTarget()
    hu.u64_install_dispatcher(tgt, MAIN,
                              rec_dirty_addr=REC_DIRTY if hook else None)
    tr = tgt.transport
    selftest = list(tr.jsrs)
    tr.jsrs.clear()
    hu.call_runner(tgt, TARGET, timeout=1.0)
    hu.call_runner(tgt, TARGET, timeout=1.0)
    if hook:
        assert selftest == [REC_DIRTY, hu.U64_SELFTEST_RTS]
        assert tr.jsrs == [REC_DIRTY, TARGET, REC_DIRTY, TARGET]
    else:
        assert selftest == [hu.U64_SELFTEST_RTS]
        assert tr.jsrs == [TARGET, TARGET]
    # the dispatcher stays inside the harness mailbox page, below MBOX
    body = tgt._u64_dispatch["jsr_operand"]
    assert hu.U64_DISPATCH_PAGE <= body < hu.U64_MBOX
