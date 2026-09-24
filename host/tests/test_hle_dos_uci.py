"""Byte-level protocol test for the ULTDOS backend (runtime/ops/dosio_uci.s).

HARDWARE STATUS: UNVALIDATED — nothing here touches a device.  This pins the
exact UCI traffic the driver produces so the later hardware leg has a
fixed expectation.

How: ``dosio_uci.s`` is assembled standalone (cl65) together with a
generated shim that provides the ``dosio_*`` parameter block it imports,
then EXECUTED instruction by instruction in a small 6502 emulator (below).
Reads/writes of ``$DF1C-$DF1F`` go to :class:`UciDevice`, a model of

- the FPGA register protocol, 1541ultimate
  ``fpga/io/command_interface/vhdl_source/command_protocol.vhd`` (status bits
  lines 54-61, register mux 97-106, control writes 148-170, queue pointers
  176-185), including the documented hazard that the "new command" bit 0
  clears BEFORE the reply queues are valid
  (``software/io/command_interface/command_intf.cc`` 155-160 then
  ``copy_result`` 173-191), and
- the DOS command target, ``software/filemanager/dos.cc`` (parse_command
  lines 86-153 / 467-492, get_more_data 784-802) plus the open-mode rules of
  ``software/filemanager/filemanager.cc`` ``fopen_impl`` 668-689 and the flag
  values of ``software/filesystem/fs_errors_flags.h`` 44-53.

Firmware tree: ~/Documents/1541ultimate @ 880de4dd.

Every pushed command is logged; the tests assert the exact bytes, the
handshake discipline (PUSH written as exactly $01, never while busy, a
single DATA_ACC per reply, no second OPEN on a busy target) and the
resulting file-system state.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host import hle_dos_env as de  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("cl65") is None,
                                reason="cl65 (cc65) not installed")

UCI_CTL, UCI_CMD, UCI_RSP, UCI_STA = 0xDF1C, 0xDF1D, 0xDF1E, 0xDF1F

# fs_errors_flags.h:44-53
FA_READ, FA_WRITE, FA_CREATE_NEW, FA_CREATE_ALWAYS, FA_OPEN_ALWAYS = (
    0x01, 0x02, 0x04, 0x08, 0x10)
# dos.h:11-17
DOS_OPEN, DOS_CLOSE, DOS_READ, DOS_WRITE, DOS_SEEK, DOS_INFO = (
    0x02, 0x03, 0x04, 0x05, 0x06, 0x07)


# =============================================================================
# Minimal NMOS 6502 (documented opcodes, binary mode only)
# =============================================================================

class CPU:
    def __init__(self, mem: "Bus"):
        self.m = mem
        self.a = self.x = self.y = 0
        self.sp = 0xFF
        self.pc = 0
        self.c = self.z = self.i = self.d = self.v = self.n = 0
        self.steps = 0

    # --- helpers
    def rd(self, a):
        return self.m.read(a & 0xFFFF)

    def wr(self, a, v):
        self.m.write(a & 0xFFFF, v & 0xFF)

    def fetch(self):
        v = self.rd(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return v

    def fetch16(self):
        lo = self.fetch()
        return lo | (self.fetch() << 8)

    def push(self, v):
        self.wr(0x100 | self.sp, v)
        self.sp = (self.sp - 1) & 0xFF

    def pull(self):
        self.sp = (self.sp + 1) & 0xFF
        return self.rd(0x100 | self.sp)

    def nz(self, v):
        v &= 0xFF
        self.z = int(v == 0)
        self.n = v >> 7
        return v

    def p(self):
        return (self.n << 7 | self.v << 6 | 0x30 | self.d << 3 | self.i << 2
                | self.z << 1 | self.c)

    def setp(self, v):
        self.n, self.v, self.d = v >> 7 & 1, v >> 6 & 1, v >> 3 & 1
        self.i, self.z, self.c = v >> 2 & 1, v >> 1 & 1, v & 1

    # --- addressing: return effective address
    def ea(self, mode):
        if mode == "imm":
            a = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
            return a
        if mode == "zp":
            return self.fetch()
        if mode == "zpx":
            return (self.fetch() + self.x) & 0xFF
        if mode == "zpy":
            return (self.fetch() + self.y) & 0xFF
        if mode == "abs":
            return self.fetch16()
        if mode == "abx":
            return (self.fetch16() + self.x) & 0xFFFF
        if mode == "aby":
            return (self.fetch16() + self.y) & 0xFFFF
        if mode == "izx":
            z = (self.fetch() + self.x) & 0xFF
            return self.rd(z) | self.rd((z + 1) & 0xFF) << 8
        if mode == "izy":
            z = self.fetch()
            return ((self.rd(z) | self.rd((z + 1) & 0xFF) << 8) + self.y) & 0xFFFF
        raise AssertionError(mode)

    def adc(self, v):
        assert not self.d, "decimal mode not modelled"
        r = self.a + v + self.c
        self.v = int((~(self.a ^ v) & (self.a ^ r) & 0x80) != 0)
        self.c = int(r > 0xFF)
        self.a = self.nz(r)

    def cmp(self, reg, v):
        r = reg - v
        self.c = int(r >= 0)
        self.nz(r)

    def branch(self, cond):
        off = self.fetch()
        if cond:
            self.pc = (self.pc + (off - 256 if off & 0x80 else off)) & 0xFFFF

    def step(self):
        self.steps += 1
        op = self.fetch()
        e = OPS.get(op)
        if e is None:
            raise AssertionError(f"illegal opcode ${op:02X} at ${self.pc - 1:04X}")
        e(self)


def _build_ops():
    ops = {}
    groups = {
        "ora": {0x09: "imm", 0x05: "zp", 0x15: "zpx", 0x0D: "abs", 0x1D: "abx", 0x19: "aby", 0x01: "izx", 0x11: "izy"},
        "and": {0x29: "imm", 0x25: "zp", 0x35: "zpx", 0x2D: "abs", 0x3D: "abx", 0x39: "aby", 0x21: "izx", 0x31: "izy"},
        "eor": {0x49: "imm", 0x45: "zp", 0x55: "zpx", 0x4D: "abs", 0x5D: "abx", 0x59: "aby", 0x41: "izx", 0x51: "izy"},
        "adc": {0x69: "imm", 0x65: "zp", 0x75: "zpx", 0x6D: "abs", 0x7D: "abx", 0x79: "aby", 0x61: "izx", 0x71: "izy"},
        "sbc": {0xE9: "imm", 0xE5: "zp", 0xF5: "zpx", 0xED: "abs", 0xFD: "abx", 0xF9: "aby", 0xE1: "izx", 0xF1: "izy"},
        "cmp": {0xC9: "imm", 0xC5: "zp", 0xD5: "zpx", 0xCD: "abs", 0xDD: "abx", 0xD9: "aby", 0xC1: "izx", 0xD1: "izy"},
        "lda": {0xA9: "imm", 0xA5: "zp", 0xB5: "zpx", 0xAD: "abs", 0xBD: "abx", 0xB9: "aby", 0xA1: "izx", 0xB1: "izy"},
        "sta": {0x85: "zp", 0x95: "zpx", 0x8D: "abs", 0x9D: "abx", 0x99: "aby", 0x81: "izx", 0x91: "izy"},
        "ldx": {0xA2: "imm", 0xA6: "zp", 0xB6: "zpy", 0xAE: "abs", 0xBE: "aby"},
        "ldy": {0xA0: "imm", 0xA4: "zp", 0xB4: "zpx", 0xAC: "abs", 0xBC: "abx"},
        "stx": {0x86: "zp", 0x96: "zpy", 0x8E: "abs"},
        "sty": {0x84: "zp", 0x94: "zpx", 0x8C: "abs"},
        "cpx": {0xE0: "imm", 0xE4: "zp", 0xEC: "abs"},
        "cpy": {0xC0: "imm", 0xC4: "zp", 0xCC: "abs"},
        "bit": {0x24: "zp", 0x2C: "abs"},
        "inc": {0xE6: "zp", 0xF6: "zpx", 0xEE: "abs", 0xFE: "abx"},
        "dec": {0xC6: "zp", 0xD6: "zpx", 0xCE: "abs", 0xDE: "abx"},
        "asl": {0x06: "zp", 0x16: "zpx", 0x0E: "abs", 0x1E: "abx"},
        "lsr": {0x46: "zp", 0x56: "zpx", 0x4E: "abs", 0x5E: "abx"},
        "rol": {0x26: "zp", 0x36: "zpx", 0x2E: "abs", 0x3E: "abx"},
        "ror": {0x66: "zp", 0x76: "zpx", 0x6E: "abs", 0x7E: "abx"},
    }

    def mk(name, mode):
        def f(c):
            a = c.ea(mode)
            if name == "ora":
                c.a = c.nz(c.a | c.rd(a))
            elif name == "and":
                c.a = c.nz(c.a & c.rd(a))
            elif name == "eor":
                c.a = c.nz(c.a ^ c.rd(a))
            elif name == "adc":
                c.adc(c.rd(a))
            elif name == "sbc":
                c.adc(c.rd(a) ^ 0xFF)
            elif name == "cmp":
                c.cmp(c.a, c.rd(a))
            elif name == "cpx":
                c.cmp(c.x, c.rd(a))
            elif name == "cpy":
                c.cmp(c.y, c.rd(a))
            elif name == "lda":
                c.a = c.nz(c.rd(a))
            elif name == "ldx":
                c.x = c.nz(c.rd(a))
            elif name == "ldy":
                c.y = c.nz(c.rd(a))
            elif name == "sta":
                c.wr(a, c.a)
            elif name == "stx":
                c.wr(a, c.x)
            elif name == "sty":
                c.wr(a, c.y)
            elif name == "bit":
                v = c.rd(a)
                c.z = int((c.a & v) == 0)
                c.n, c.v = v >> 7, v >> 6 & 1
            else:
                v = c.rd(a)
                if name == "inc":
                    v = v + 1
                elif name == "dec":
                    v = v - 1
                elif name == "asl":
                    c.c = v >> 7
                    v = v << 1
                elif name == "lsr":
                    c.c = v & 1
                    v = v >> 1
                elif name == "rol":
                    v, c.c = (v << 1) | c.c, v >> 7
                elif name == "ror":
                    v, c.c = (v >> 1) | (c.c << 7), v & 1
                c.wr(a, c.nz(v))
        return f

    for name, modes in groups.items():
        for op, mode in modes.items():
            ops[op] = mk(name, mode)

    def acc(fn):
        def f(c):
            c.a = c.nz(fn(c))
        return f

    def asl_a(c):
        c.c = c.a >> 7
        return c.a << 1

    def lsr_a(c):
        c.c = c.a & 1
        return c.a >> 1

    def rol_a(c):
        r = (c.a << 1) | c.c
        c.c = c.a >> 7
        return r

    def ror_a(c):
        r = (c.a >> 1) | (c.c << 7)
        c.c = c.a & 1
        return r

    ops[0x0A], ops[0x4A] = acc(asl_a), acc(lsr_a)
    ops[0x2A], ops[0x6A] = acc(rol_a), acc(ror_a)
    for op, flag, val in ((0x10, "n", 0), (0x30, "n", 1), (0x50, "v", 0),
                          (0x70, "v", 1), (0x90, "c", 0), (0xB0, "c", 1),
                          (0xD0, "z", 0), (0xF0, "z", 1)):
        ops[op] = (lambda fl, vv: lambda c: c.branch(getattr(c, fl) == vv))(flag, val)

    def jsr(c):
        t = c.fetch16()
        ret = (c.pc - 1) & 0xFFFF
        c.push(ret >> 8)
        c.push(ret & 0xFF)
        c.pc = t

    def rts(c):
        lo = c.pull()
        c.pc = ((c.pull() << 8 | lo) + 1) & 0xFFFF

    def jmp_ind(c):
        p = c.fetch16()
        c.pc = c.rd(p) | c.rd((p & 0xFF00) | ((p + 1) & 0xFF)) << 8

    def brk(c):
        raise AssertionError(f"BRK at ${c.pc - 1:04X}")

    def rti(c):
        c.setp(c.pull())
        lo = c.pull()
        c.pc = c.pull() << 8 | lo

    ops[0x20], ops[0x60], ops[0x6C], ops[0x00], ops[0x40] = jsr, rts, jmp_ind, brk, rti
    ops[0x4C] = lambda c: setattr(c, "pc", c.fetch16())
    simple = {
        0xEA: lambda c: None,
        0xAA: lambda c: setattr(c, "x", c.nz(c.a)),
        0xA8: lambda c: setattr(c, "y", c.nz(c.a)),
        0x8A: lambda c: setattr(c, "a", c.nz(c.x)),
        0x98: lambda c: setattr(c, "a", c.nz(c.y)),
        0xBA: lambda c: setattr(c, "x", c.nz(c.sp)),
        0x9A: lambda c: setattr(c, "sp", c.x),
        0xE8: lambda c: setattr(c, "x", c.nz(c.x + 1)),
        0xC8: lambda c: setattr(c, "y", c.nz(c.y + 1)),
        0xCA: lambda c: setattr(c, "x", c.nz(c.x - 1)),
        0x88: lambda c: setattr(c, "y", c.nz(c.y - 1)),
        0x18: lambda c: setattr(c, "c", 0),
        0x38: lambda c: setattr(c, "c", 1),
        0x58: lambda c: setattr(c, "i", 0),
        0x78: lambda c: setattr(c, "i", 1),
        0xB8: lambda c: setattr(c, "v", 0),
        0xD8: lambda c: setattr(c, "d", 0),
        0xF8: lambda c: setattr(c, "d", 1),
        0x48: lambda c: c.push(c.a),
        0x08: lambda c: c.push(c.p()),
        0x68: lambda c: setattr(c, "a", c.nz(c.pull())),
        0x28: lambda c: c.setp(c.pull()),
    }
    ops.update(simple)
    return ops


OPS = _build_ops()


# =============================================================================
# UCI device model: FPGA handshake + DOS targets 1/2 (firmware 880de4dd)
# =============================================================================

@dataclass
class DosTarget:
    fs: dict
    file: str | None = None
    pos: int = 0
    remaining: int = 0

    def parse(self, msg: bytes, dev: "UciDevice") -> tuple[list[bytes], bytes]:
        """-> (reply blocks, status) exactly as dos.cc would produce."""
        cmd = msg[1]
        if cmd == DOS_OPEN:                           # dos.cc:111-124
            name = msg[3:].split(b"\0")[0].decode("latin-1")
            # FatFS long names are case-insensitive: resolve to the stored key
            name = next((k for k in self.fs if k.lower() == name.lower()), name)
            flags = msg[2]
            if self.file is not None:
                dev.violations.append(f"OPEN on busy target (leaks {self.file})")
            self.file = None
            if dev.readonly and flags & FA_WRITE:
                return [b""], b"WRITE PROTECTED"
            exists = name in self.fs                  # filemanager.cc:668-689
            if exists and flags & FA_CREATE_NEW:
                return [b""], b"FILE EXISTS"
            create = ((flags & FA_CREATE_NEW and not exists)
                      or flags & FA_CREATE_ALWAYS
                      or (flags & FA_OPEN_ALWAYS and not exists))
            if not exists and not create:
                return [b""], b"FILE DOESN'T EXIST"
            if create and (flags & FA_WRITE) == 0:
                dev.violations.append("create without FA_WRITE")
            if create:
                self.fs[name] = bytearray()
            dev.open_flags[name] = flags
            self.file, self.pos = name, 0
            return [b""], b"00,OK"
        if cmd == DOS_CLOSE:                          # dos.cc:125-134
            if self.file is None:
                return [b""], b"84,NO FILE TO CLOSE"
            self.file = None
            return [b""], b"00,OK"
        if cmd == DOS_SEEK:                           # dos.cc:135-153
            if self.file is None:
                return [b""], b"85,NO FILE OPEN"
            pos = struct.unpack_from("<I", msg, 2)[0]
            data = self.fs[self.file]
            if pos > len(data):
                dev.violations.append(f"seek past EOF ({pos} > {len(data)}) "
                                      "would extend the file")
                data.extend(bytes(pos - len(data)))
            self.pos = pos
            return [b""], b"00,OK"
        if cmd == DOS_READ:                           # dos.cc:467-477, 784-802
            if self.file is None:
                return [b""], b"85,NO FILE OPEN"
            self.remaining = msg[2] | msg[3] << 8
            blocks = []
            data = self.fs[self.file]
            while True:
                n = min(self.remaining, 512)
                chunk = bytes(data[self.pos:self.pos + n])
                self.pos += len(chunk)
                self.remaining -= len(chunk)
                blocks.append(chunk)
                if len(chunk) != n or self.remaining == 0:
                    break
            return blocks, b""                         # success status EMPTY
        if cmd == DOS_WRITE:                          # dos.cc:478-492
            if self.file is None:
                return [b""], b"85,NO FILE OPEN"
            payload = msg[4:]
            data = self.fs[self.file]
            if self.pos > len(data):
                data.extend(bytes(self.pos - len(data)))
            data[self.pos:self.pos + len(payload)] = payload
            self.pos += len(payload)
            return [b""], b"00,OK"
        if cmd == DOS_INFO:                           # dos.cc:154-182
            if self.file is None:
                return [b""], b"85,NO FILE OPEN"
            name = self.file.encode()
            info = struct.pack("<IHH3sB", len(self.fs[self.file]), 0, 0,
                               b"   ", 0) + name
            return [info], b"00,OK"
        return [b""], b"21,UNKNOWN COMMAND"


@dataclass
class UciDevice:
    fs: dict = field(default_factory=dict)
    latency: int = 3                  # status polls before a reply is valid
    readonly: bool = False            # FA_WRITE opens fail "WRITE PROTECTED"
    hang_cmds: set = field(default_factory=set)  # processed, reply never posted
    state: int = 0                    # 2-bit protocol state (vhd:48-52)
    newcmd: int = 0
    error: int = 0
    abort: int = 0
    cmd: bytearray = field(default_factory=bytearray)
    blocks: list = field(default_factory=list)
    resp: bytes = b""
    rpos: int = 0
    stat: bytes = b""
    spos: int = 0
    pending: int = 0                  # polls until the current phase advances
    phase: str = ""
    log: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    open_flags: dict = field(default_factory=dict)
    ctl_writes: list = field(default_factory=list)
    targets: dict = field(default_factory=dict)

    def __post_init__(self):
        self.targets = {1: DosTarget(self.fs), 2: DosTarget(self.fs)}

    # firmware task progress, driven by C64 status polls
    def _tick(self):
        if not self.phase or self.phase == "hang":
            return
        self.pending -= 1
        if self.pending > 0:
            return
        if self.phase == "accept":            # command_intf.cc:155-158
            self.newcmd = 0                   # bit 0 clears, queues not ready
            self.phase, self.pending = "reply", self.latency
        elif self.phase == "reply":           # copy_result, cc:173-191
            self._post_block()
            self.phase = ""
        elif self.phase == "abort":           # cc:121-128 HANDSHAKE_RESET
            self.abort, self.state, self.newcmd = 0, 0, 0
            self.cmd.clear()
            self.phase = ""

    def _post_block(self):
        blk = self.blocks.pop(0)
        self.resp, self.rpos = blk, 0
        self.stat, self.spos = (self.pending_status if not self.blocks
                                else b""), 0
        self.state = 0b11 if self.blocks else 0b10

    def _valid(self):
        return (self.state & 2) and not self.abort

    def read(self, addr):
        if addr == UCI_CTL:
            self._tick()
            v = self.state << 4 | self.error << 3 | self.abort << 2 | self.newcmd
            if self._valid() and self.rpos < len(self.resp):
                v |= 0x80
            if self._valid() and self.spos < len(self.stat):
                v |= 0x40
            return v
        if addr == UCI_CMD:
            return 0xC9
        if addr == UCI_RSP:
            if self._valid() and self.rpos < len(self.resp):
                self.rpos += 1
                return self.resp[self.rpos - 1]
            return 0x00
        if addr == UCI_STA:
            if self._valid() and self.spos < len(self.stat):
                self.spos += 1
                return self.stat[self.spos - 1]
            return 0x00
        return 0xFF

    def write(self, addr, v):
        if addr == UCI_CMD:
            if len(self.cmd) < 895:           # vhd:145-146
                self.cmd.append(v)
            return
        if addr != UCI_CTL:
            return
        self.ctl_writes.append(v)
        if v & 0x08:
            self.error = 0
        if v & 0x01:
            if v & 0xE0:
                self.violations.append(f"PUSH with freeze/trigger/irq bits: ${v:02X}")
            if self.state == 0:
                self.state, self.newcmd = 0b01, 1
                msg = bytes(self.cmd)
                self.cmd.clear()
                self.log.append(msg)
                tgt = self.targets.get(msg[0] & 0x0F) if msg else None
                if tgt is None:
                    blocks, st = [b""], b"NO TARGET"
                else:
                    blocks, st = tgt.parse(msg, self)
                self.blocks, self.pending_status = blocks, st
                self.phase, self.pending = "accept", self.latency
                if msg and len(msg) > 1 and msg[1] in self.hang_cmds:
                    self.phase = "hang"       # firmware never replies
            else:
                self.error = 1
                self.violations.append("PUSH while not idle")
        if v & 0x02 and not self.state & 2:
            self.violations.append(f"DATA_ACC outside a reply (state {self.state:02b})")
        if v & 0x02 and self.state & 2:       # vhd:163-167 DATA_ACC
            if self.state == 0b11:
                self.state = 0b01
                self.phase, self.pending = "reply", self.latency
            else:
                self.state = 0
        if v & 0x04:
            self.abort = 1
            self.phase, self.pending = "abort", self.latency


class Bus:
    def __init__(self, dev: UciDevice):
        self.ram = bytearray(0x10000)
        self.dev = dev

    def read(self, a):
        if 0xDF1C <= a <= 0xDF1F:
            return self.dev.read(a)
        return self.ram[a]

    def write(self, a, v):
        if 0xDF1C <= a <= 0xDF1F:
            self.dev.write(a, v)
        else:
            self.ram[a] = v


# =============================================================================
# Standalone build of dosio_uci.s + parameter-block shim
# =============================================================================

SHIM = """
.export dosio_buf, dosio_name, dosio_nlen, dosio_mode, dosio_slot
.export dosio_len, dosio_arg, dosio_err
.segment "BSS"
dosio_buf:  .res 256
dosio_name: .res 64
dosio_nlen: .res 1
dosio_mode: .res 1
dosio_slot: .res 1
dosio_len:  .res 2
dosio_arg:  .res 4
dosio_err:  .res 1
dosio_curfid: .res 1
lc_fail:    .res 1                ; test knob: nonzero -> lockcheck refuses
lc_arg:     .res 1                ; last file id passed to dosio_lockcheck
lc_calls:   .res 1
.export dosio_curfid, lc_fail, lc_arg, lc_calls, dosio_lockcheck
.segment "CODE"
; stand-in for hle_dos.s dosio_lockcheck (the real one is VICE-tested):
; A = file id -> C=1 + dosio_err=202 when lc_fail, else curfid := A
dosio_lockcheck:
        sta lc_arg
        inc lc_calls
        ldx lc_fail
        bne @no
        sta dosio_curfid
        clc
        rts
@no:    lda #202
        sta dosio_err
        sec
        rts
"""

CFG = """
MEMORY {
    ZP:   start = $0080, size = $0070, type = rw;
    MAIN: start = $1000, size = $4000, file = %O;
    BSSM: start = $6000, size = $2000, type = rw;
}
SEGMENTS {
    ZEROPAGE: load = ZP, type = zp, optional = yes;
    CODE:     load = MAIN, type = ro;
    RODATA:   load = MAIN, type = ro, optional = yes;
    DATA:     load = MAIN, type = rw, optional = yes;
    BSS:      load = BSSM, type = bss, optional = yes;
}
"""

ENTRIES = ("dosio_uci_detect", "dosio_uci_init", "dosio_uci_open",
           "dosio_uci_close", "dosio_uci_size", "dosio_uci_seek",
           "dosio_uci_rdchunk", "dosio_uci_wrchunk", "dosio_uci_setsize")


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    d = tmp_path_factory.mktemp("uci")
    (d / "shim.s").write_text(SHIM)
    (d / "t.cfg").write_text(CFG)
    src = ROOT / "runtime/ops/dosio_uci.s"
    if not src.exists():
        pytest.fail("runtime/ops/dosio_uci.s does not exist")
    out, lbl = d / "uci.bin", d / "uci.lbl"
    # Tiny fence: the fence is a turbo-timing guard, not protocol; the
    # emulator is untimed.  Production values are pinned separately.
    r = subprocess.run(
        ["cl65", "-t", "none", "-C", str(d / "t.cfg"), "-o", str(out),
         "-Ln", str(lbl), "--asm-define", "DOSIO_UCI_FENCE_OUTER=1",
         "--asm-define", "DOSIO_UCI_FENCE_INNER=1",
         "--asm-define", "DOSIO_UCI_POLLS_HI=4",   # 1024-poll timeouts
         str(src), str(d / "shim.s")],
        capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
    labels = {}
    for line in lbl.read_text().splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "al":
            labels[parts[2].lstrip(".")] = int(parts[1], 16)
    return out.read_bytes(), labels


class Harness:
    RET = 0xFFF0

    def __init__(self, build, fs=None, latency=3):
        self.image, self.lab = build
        self.dev = UciDevice(fs=fs if fs is not None else {}, latency=latency)
        self.bus = Bus(self.dev)
        self.bus.ram[0x1000:0x1000 + len(self.image)] = self.image
        self.cpu = CPU(self.bus)

    def sym(self, name):
        return self.lab[name]

    def poke(self, name, data: bytes, off=0):
        a = self.sym(name) + off
        self.bus.ram[a:a + len(data)] = data

    def peek(self, name, n, off=0):
        a = self.sym(name) + off
        return bytes(self.bus.ram[a:a + n])

    def call(self, name, limit=3_000_000):
        c = self.cpu
        c.sp = 0xFF
        ret = self.RET - 1
        c.push(ret >> 8)
        c.push(ret & 0xFF)
        c.pc = self.sym(name)
        n = 0
        while c.pc != self.RET:
            c.step()
            n += 1
            assert n < limit, f"{name} did not return (PC=${c.pc:04X})"
        return c.c                         # carry = error flag

    # --- operations as the generic layer drives them
    def open(self, slot, name, mode):
        raw = name.encode()
        self.poke("dosio_name", raw + b"\0")
        self.poke("dosio_nlen", bytes([len(raw)]))
        self.poke("dosio_mode", bytes([mode]))
        self.poke("dosio_slot", bytes([slot]))
        return self.call("dosio_uci_open")

    def err(self):
        return self.peek("dosio_err", 1)[0]

    def arg(self):
        return struct.unpack("<I", self.peek("dosio_arg", 4))[0]

    def read(self, slot, n):
        self.poke("dosio_slot", bytes([slot]))
        self.poke("dosio_len", struct.pack("<H", n))
        cf = self.call("dosio_uci_rdchunk")
        got = struct.unpack("<H", self.peek("dosio_len", 2))[0]
        return cf, self.peek("dosio_buf", got)

    def write(self, slot, data):
        self.poke("dosio_slot", bytes([slot]))
        self.poke("dosio_buf", data)
        self.poke("dosio_len", struct.pack("<H", len(data)))
        return self.call("dosio_uci_wrchunk")

    def seek(self, slot, pos):
        self.poke("dosio_slot", bytes([slot]))
        self.poke("dosio_arg", struct.pack("<I", pos))
        return self.call("dosio_uci_seek")

    def size(self, slot):
        self.poke("dosio_slot", bytes([slot]))
        self.call("dosio_uci_size")
        return self.arg()

    def close(self, slot):
        self.poke("dosio_slot", bytes([slot]))
        return self.call("dosio_uci_close")


MODE_OLD, MODE_NEW, MODE_RW = 0, 1, 2      # generic-layer mode codes


def cmds(h):
    return [bytes(m) for m in h.dev.log]


def assert_clean(h):
    assert h.dev.violations == []
    # PUSH is always written as exactly $01 (vhd:152-161 latches bits 7-5)
    assert all(v in (0x01, 0x02, 0x04, 0x08) for v in h.dev.ctl_writes), \
        h.dev.ctl_writes


# =============================================================================
# Tests
# =============================================================================

def test_entry_points_exported(build):
    _, lab = build
    for e in ENTRIES:
        assert e in lab, e


def test_production_fence_matches_c64_https():
    # c64-https src/net/uci/uci_regs.inc:93-109 (hardware-tuned at 64 MHz)
    eq = de.asm_equates(ROOT / "runtime/ops/dosio_uci.s")
    assert eq["DOSIO_UCI_FENCE_OUTER"] == 5
    assert eq["DOSIO_UCI_FENCE_INNER"] == 217


def test_detect_reads_identification_register(build):
    h = Harness(build)
    assert h.call("dosio_uci_detect") == 1        # C=1: present ($DF1D=$C9)


def test_detect_absent_on_open_bus(build):
    h = Harness(build)
    h.dev.read = lambda a: 0xFF                   # VICE REU mirror / no UCI
    assert h.call("dosio_uci_detect") == 0


def test_init_aborts_then_closes_both_targets(build):
    h = Harness(build)
    h.dev.targets[1].file = "stale"               # leaked by a previous run
    h.dev.fs["stale"] = bytearray()
    assert h.call("dosio_uci_init") == 0
    assert h.dev.ctl_writes[0] == 0x04            # abort first
    assert cmds(h) == [bytes([1, DOS_CLOSE]), bytes([2, DOS_CLOSE])]
    assert h.dev.targets[1].file is None
    assert_clean(h)


def test_open_oldfile_bytes_and_size_query(build):
    h = Harness(build, fs={"in.bin": bytearray(b"x" * 700)})
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "in.bin", MODE_OLD) == 0
    # OLDFILE = read+write, must exist: FA_READ|FA_WRITE, no create flags
    assert cmds(h) == [b"\x01\x02\x03in.bin", b"\x01\x07"]
    assert h.size(0) == 700
    assert_clean(h)


def test_open_oldfile_missing_reports_not_found(build):
    h = Harness(build)
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "nosuch", MODE_OLD) == 1
    assert h.err() == de.ERROR_OBJECT_NOT_FOUND
    assert "nosuch" not in h.dev.fs              # never created
    for m in cmds(h):
        assert m[1] != DOS_OPEN or m[2] & (FA_CREATE_NEW | FA_CREATE_ALWAYS
                                           | FA_OPEN_ALWAYS) == 0
    assert_clean(h)


def test_open_newfile_creates_or_truncates(build):
    h = Harness(build, fs={"out.txt": bytearray(b"old contents")})
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "out.txt", MODE_NEW) == 0
    assert cmds(h)[0] == b"\x01\x02\x0Bout.txt"  # READ|WRITE|CREATE_ALWAYS
    assert h.dev.fs["out.txt"] == b""
    assert h.size(0) == 0
    assert_clean(h)


def test_open_readwrite_creates_without_truncating(build):
    h = Harness(build, fs={"keep": bytearray(b"12345")})
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "rw.dat", MODE_RW) == 0
    assert h.open(1, "keep", MODE_RW) == 0
    opens = [m for m in cmds(h) if m[1] == DOS_OPEN]
    assert opens == [b"\x01\x02\x13rw.dat", b"\x02\x02\x13keep"]  # OPEN_ALWAYS
    assert h.dev.fs["rw.dat"] == b"" and h.dev.fs["keep"] == b"12345"
    assert h.size(1) == 5
    assert_clean(h)


def test_two_targets_then_exhaustion(build):
    h = Harness(build, fs={"a": bytearray(b"A"), "b": bytearray(b"B"),
                           "c": bytearray(b"C")})
    h.call("dosio_uci_init")
    assert h.open(0, "a", MODE_OLD) == 0
    assert h.open(5, "b", MODE_OLD) == 0
    assert h.open(6, "c", MODE_OLD) == 1          # one file per DOS target
    assert h.err() == de.ERROR_NO_FREE_STORE
    assert h.close(0) == 0
    assert h.open(6, "c", MODE_OLD) == 0          # target 1 reused
    assert h.dev.targets[1].file == "c" and h.dev.targets[2].file == "b"
    assert_clean(h)


def test_read_chunk_bytes_and_eof(build):
    data = bytes((i * 7 + 3) & 0xFF for i in range(300))
    h = Harness(build, fs={"in.bin": bytearray(data)})
    h.call("dosio_uci_init")
    h.open(0, "in.bin", MODE_OLD)
    h.dev.log.clear()
    cf, got = h.read(0, 256)
    assert cf == 0 and got == data[:256]
    assert cmds(h) == [b"\x01\x04\x00\x01"]       # READ_DATA, LE16 length 256
    cf, got = h.read(0, 100)                      # short read at EOF
    assert cf == 0 and got == data[256:]
    cf, got = h.read(0, 10)
    assert cf == 0 and got == b""
    assert_clean(h)


def test_write_chunk_bytes(build):
    h = Harness(build)
    h.call("dosio_uci_init")
    h.open(1, "out", MODE_NEW)
    h.dev.log.clear()
    payload = bytes(range(256))
    assert h.write(1, payload) == 0
    # WRITE_DATA: bytes 2-3 are ignored by dos.cc:483 (payload from byte 4)
    assert cmds(h) == [b"\x01\x05\x00\x00" + payload]
    assert h.write(1, b"ABC") == 0
    assert h.dev.fs["out"] == payload + b"ABC"
    assert_clean(h)


def test_seek_sends_absolute_le32(build):
    h = Harness(build, fs={"f": bytearray(bytes(0x20000))})
    h.call("dosio_uci_init")
    h.open(0, "f", MODE_OLD)
    h.dev.log.clear()
    assert h.seek(0, 0x12345) == 0
    assert cmds(h) == [b"\x01\x06\x45\x23\x01\x00"]
    cf, got = h.read(0, 1)
    assert h.dev.targets[1].pos == 0x12346
    assert_clean(h)


def test_close_bytes_and_target_release(build):
    h = Harness(build, fs={"f": bytearray(b"1")})
    h.call("dosio_uci_init")
    h.open(0, "f", MODE_OLD)
    h.dev.log.clear()
    assert h.close(0) == 0
    assert cmds(h) == [b"\x01\x03"]
    assert h.dev.targets[1].file is None
    assert_clean(h)


def test_write_failure_maps_to_disk_full(build):
    h = Harness(build)
    h.call("dosio_uci_init")
    h.open(0, "f", MODE_NEW)
    real = h.dev.targets[1].parse

    def failing(msg, dev):
        if msg[1] == DOS_WRITE:
            return [b""], b"DISK IS FULL"
        return real(msg, dev)
    h.dev.targets[1].parse = failing
    assert h.write(0, b"xyz") == 1
    assert h.err() == de.ERROR_DISK_FULL
    assert_clean(h)


def test_slow_firmware_still_waits_for_state_not_bit0(build):
    # Large latency: bit 0 clears long before the queues are valid (#486).
    h = Harness(build, fs={"in": bytearray(b"hello world")}, latency=40)
    h.call("dosio_uci_init")
    assert h.open(0, "in", MODE_OLD) == 0
    cf, got = h.read(0, 11)
    assert got == b"hello world"
    assert_clean(h)


def test_setsize_updates_cached_size(build):
    h = Harness(build)
    h.call("dosio_uci_init")
    h.open(0, "f", MODE_NEW)
    h.poke("dosio_arg", struct.pack("<I", 1234))
    h.call("dosio_uci_setsize")
    assert h.size(0) == 1234


# =============================================================================
# Review fix round 1
# =============================================================================

def test_stale_error_bit_is_cleared_before_push(build):
    # ruling 3: an ERROR bit left by someone else must not turn an accepted
    # push into a fake refusal (and leave the reply pending).
    h = Harness(build, fs={"f": bytearray(b"12345")})
    h.call("dosio_uci_init")
    h.dev.error = 1
    assert h.open(0, "f", MODE_OLD) == 0
    assert h.size(0) == 5
    assert h.dev.error == 0
    assert_clean(h)


def test_lost_open_reply_never_double_opens(build):
    # ruling 3: when the OPEN reply never comes, the file may be open in the
    # firmware -> no second OPEN on that target (read-only retry path), the
    # target is closed instead and the Open fails.
    h = Harness(build, fs={"f": bytearray(b"12345")})
    h.call("dosio_uci_init")
    h.dev.log.clear()
    h.dev.hang_cmds = {DOS_OPEN}
    assert h.open(0, "f", MODE_OLD) == 1
    assert h.err() == de.ERROR_DEVICE_NOT_MOUNTED
    opens = [m for m in cmds(h) if m[1] == DOS_OPEN]
    assert len(opens) == 1, cmds(h)
    h.dev.hang_cmds = set()
    assert bytes([1, DOS_CLOSE]) in cmds(h)
    assert h.dev.targets[1].file is None
    assert_clean(h)
    assert h.open(0, "f", MODE_OLD) == 0      # target 1 usable again
    assert_clean(h)


def test_readonly_retry_after_definitive_refusal(build):
    h = Harness(build, fs={"ro": bytearray(b"xyz")})
    h.dev.readonly = True
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "ro", MODE_OLD) == 0
    opens = [m for m in cmds(h) if m[1] == DOS_OPEN]
    assert opens == [b"\x01\x02\x03ro", b"\x01\x02\x01ro"]
    assert h.size(0) == 3
    assert_clean(h)                            # no OPEN on a busy target


def test_newfile_is_never_retried_readonly(build):
    h = Harness(build)
    h.dev.readonly = True
    h.call("dosio_uci_init")
    h.dev.log.clear()
    assert h.open(0, "n", MODE_NEW) == 1
    assert len([m for m in cmds(h) if m[1] == DOS_OPEN]) == 1
    assert_clean(h)


def test_file_info_failure_fails_the_open(build):
    # ruling 4: no unknown-size handles.
    h = Harness(build, fs={"f": bytearray(b"12345")})
    h.call("dosio_uci_init")
    real = h.dev.targets[1].parse

    def no_info(msg, dev):
        if msg[1] == DOS_INFO:
            return [b""], b"88,NO INFORMATION AVAILABLE"
        return real(msg, dev)
    h.dev.targets[1].parse = no_info
    h.dev.log.clear()
    assert h.open(0, "f", MODE_OLD) == 1
    assert h.err() == de.ERROR_OBJECT_NOT_FOUND
    assert cmds(h)[-1] == bytes([1, DOS_CLOSE])
    assert h.dev.targets[1].file is None
    h.dev.targets[1].parse = real
    assert h.open(0, "f", MODE_OLD) == 0      # target released
    assert h.dev.targets[1].file == "f"
    assert_clean(h)


def test_lock_check_precedes_open_and_ids_follow_names(build):
    # ruling 7: the backend asks dosio_lockcheck (hle_dos.s) BEFORE sending
    # OPEN, with a file id shared by handles on the same name.
    h = Harness(build, fs={"a": bytearray(b"A"), "b": bytearray(b"B")})
    h.call("dosio_uci_init")
    assert h.open(0, "a", MODE_OLD) == 0
    fid_a = h.peek("lc_arg", 1)[0]
    h.poke("dosio_curfid", b"\0")
    assert h.open(1, "A", MODE_OLD) == 0      # same file, other target
    assert h.peek("lc_arg", 1)[0] == fid_a
    h.close(0)
    h.close(1)
    assert h.open(2, "b", MODE_OLD) == 0
    fid_b = h.peek("lc_arg", 1)[0]
    assert h.open(3, "a", MODE_OLD) == 0
    assert h.peek("lc_arg", 1)[0] != fid_b     # different file, different id
    h.close(2)
    h.close(3)
    h.dev.log.clear()
    h.poke("lc_fail", b"\1")
    assert h.open(4, "a", MODE_NEW) == 1
    assert h.err() == de.ERROR_OBJECT_IN_USE
    assert not [m for m in cmds(h) if m[1] == DOS_OPEN]   # nothing sent
    assert h.dev.fs["a"] == b"A"                             # not truncated
    assert_clean(h)


def test_open_fid_distinct_while_other_target_holds_stale_id(build):
    h = Harness(build, fs={"a": bytearray(b"A"), "b": bytearray(b"B"),
                           "c": bytearray(b"C")})
    h.call("dosio_uci_init")
    h.open(0, "a", MODE_OLD)                   # target 1
    h.open(1, "a", MODE_OLD)                   # target 2, same id
    fid_a = h.peek("lc_arg", 1)[0]
    h.close(0)                                 # target 1 free, 2 keeps fid_a
    h.open(2, "c", MODE_OLD)                   # target 1 again
    assert h.peek("lc_arg", 1)[0] != fid_a
    assert_clean(h)
