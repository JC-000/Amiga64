"""WS-B2 dos.library file-I/O driver: RAMDISK backend end to end in VICE.

Usage:
    python3 -m tests.run_dos --prg build/ws-b2/runner.prg
        [--only NAME[,NAME]] [--budget N] [--timeout SECS]

Why a separate driver (docs/HLE_DOS.md "Testing"): until WS-B1's hle.s
routes Read/Write on file kinds (fh_Args >= $10) to ``hle_dos_read`` /
``hle_dos_write`` (contract C2), a guest Read/Write through the production
DOSBase never reaches WS-B2.  This driver therefore stages a TEST-ONLY env:
the normal ``hle_env.env_chunks()`` (which already merges
``DOS_FILE_FUNCS``: Open/Close/Seek/IoErr) plus two overriding chunks that
plant ``$A14E`` / ``$A14F`` (``hle_dos_dispatch`` direct entries into
``hle_dos_read`` / ``hle_dos_write``) in the Read (-42) / Write (-48) slots.

Guest program = a hand-encoded "call script" interpreter (no m68k
toolchain): it walks a table of calls (LVO, D1 [literal or "D0 of step k"],
D2, D3), jsr's each through ``LVO(A6)``, and records per step
{D0, IoErr(), byte at [D2] read back through the memory API, D1 used}.
The host then checks every step against hand-written expectations and
reads RAMDISK files and guest buffers back.

Scenario ``codepage`` points A6 at an in-page library (slot words inside
the program's own code page) and Reads new code over the program's exit
stub: it only passes if the bulk DMA into guest memory invalidated the
instruction window (C1 / CONTRACTS.md; teeth: the ``DOSIO_NOINVAL`` build
must fail it).

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass, field

from host import hle_dos_env as de
from host import hle_env, hunk, image
from host.fixturegen import HunkBuilder
from host.image import Chunk

# --- Guest layout of the script program -----------------------------------------
SCRIPT = 0x020000          # call table
RESULTS = 0x028000         # 16 bytes per executed step
BUF = 0x030000             # read buffers (zeroed before each run)
BUF_LEN = 0x1000
SRC = 0x031000             # write source pattern
SRC_LEN = 0x400
NAMES = 0x02F000           # NUL-terminated file names
STEP_SIZE = 16
REC_SIZE = 16              # script record: w lvo, w d1sel, l d1, l d2, l d3

INPAGE_BASE = 0x0100F0     # codepage scenario: library base inside code page
PATCH_OFF = 0x44           # code offset of the exit stub ("done:")

LVO = {"Open": -30, "Close": -36, "Read": de.LVO_READ, "Write": de.LVO_WRITE,
       "Seek": -66, "IoErr": -132}


def script_code(a6_base: int) -> list[int]:
    """The call-script interpreter (68000 words).  Offsets in comments."""
    loop = 0x16
    done = PATCH_OFF
    return [
        0x4DF9, a6_base >> 16, a6_base & 0xFFFF,   # 00 lea a6_base,a6
        0x45F9, SCRIPT >> 16, SCRIPT & 0xFFFF,     # 06 lea SCRIPT,a2
        0x47F9, RESULTS >> 16, RESULTS & 0xFFFF,   # 0C lea RESULTS,a3
        # 12 lea RESULTS,a4 would push loop to $18; use movea.l a3,a4 (2 words)
        0x284B,                                    # 12 movea.l a3,a4
        0x4E71,                                    # 14 nop
        0x381A,                                    # 16 loop: move.w (a2)+,d4
        0x6700, done - 0x1A,                       # 18 beq.w done
        0x3A1A,                                    # 1C move.w (a2)+,d5
        0x221A,                                    # 1E move.l (a2)+,d1
        0x4A45,                                    # 20 tst.w d5
        0x6B06,                                    # 22 bmi.s lit (+6 -> $2A)
        0xE94D,                                    # 24 lsl.w #4,d5
        0x2234, 0x5000,                            # 26 move.l 0(a4,d5.w),d1
        0x241A,                                    # 2A lit: move.l (a2)+,d2
        0x261A,                                    # 2C move.l (a2)+,d3
        0x2E01,                                    # 2E move.l d1,d7
        0x4EB6, 0x4000,                            # 30 jsr 0(a6,d4.w)
        0x26C0,                                    # 34 move.l d0,(a3)+
        0x4EAE, LVO["IoErr"] & 0xFFFF,             # 36 jsr -132(a6)
        0x26C0,                                    # 3A move.l d0,(a3)+
        0x2042,                                    # 3C movea.l d2,a0
        0x7C00,                                    # 3E moveq #0,d6
        0x1C10,                                    # 40 move.b (a0),d6
        0x26C6,                                    # 42 move.l d6,(a3)+ ... see below
    ]


def build_program(a6_base: int, inpage: bool) -> list[int]:
    words = script_code(a6_base)
    # 44 must be `done`, so the 4th result long and the loop branch live
    # after the exit stub: $42 stores the byte, then we branch over `done`.
    # Re-layout: replace the tail so that offset $44 = done.
    words = words[:-1]                       # drop the $42 placeholder
    words += [
        0x6006,                                    # 42 bra.s tail ($4A)
        0x7001, 0x4E75, 0x4E71,                    # 44 done: moveq #1,d0; rts; nop
        0x26C6,                                    # 4A tail: move.l d6,(a3)+
        0x26C7,                                    # 4C move.l d7,(a3)+
        0x60C6,                                    # 4E bra.s loop ($16): $16-$50 = -$3A
    ]
    assert len(words) * 2 == 0x50
    if inpage:
        # pad to INPAGE_BASE - 132 and plant the in-page library slots
        lib_lo = INPAGE_BASE - 0x010000 - 132
        words += [0x4E71] * ((lib_lo - len(words) * 2) // 2)
        slots = {LVO["Open"]: 0x30, LVO["Close"]: 0x31, LVO["Seek"]: 0x32,
                 LVO["IoErr"]: 0x33, LVO["Read"]: de.HLE_DOS_IDX_READ,
                 LVO["Write"]: de.HLE_DOS_IDX_WRITE}
        area = bytearray(132 + 2)
        for lvo, idx in slots.items():
            off = 132 + lvo
            area[off:off + 2] = struct.pack(">H", 0xA100 | idx)
        words += [int.from_bytes(area[i:i + 2], "big")
                  for i in range(0, len(area), 2)]
    return words


# --- Scenario model -----------------------------------------------------------------

@dataclass
class Step:
    fn: str
    d1: int = 0
    d2: int = 0
    d3: int = 0
    ref: int = -1              # >= 0: D1 := D0 of step `ref`
    d0: int | None = None      # expected D0
    ioerr: int | None = None   # expected IoErr() after the call
    byte: int | None = None    # expected guest byte at [D2] after the call


@dataclass
class Scenario:
    name: str
    files: dict[str, bytes] | None     # None: do not stage a RAMDISK
    names: list[str]
    steps: list[Step]
    zap_header: bool = False           # stage an invalid RAMDISK header
    inpage: bool = False
    init: bool = True                  # call hle_dos_init before the run
    expect_files: dict[str, bytes] = field(default_factory=dict)
    expect_buf: dict[int, bytes] = field(default_factory=dict)
    expect_d0: int | None = None       # final D0 (program exit)
    expect_fh_args: dict[int, int] = field(default_factory=dict)


def name_addr(names: list[str], name: str) -> int:
    addr = NAMES
    for n in names:
        if n == name:
            return addr
        addr += len(n.encode()) + 1
    raise KeyError(name)


def s32(v: int) -> int:
    return v & 0xFFFFFFFF


TRUE = de.DOSTRUE
FALSE = de.DOSFALSE
NEG1 = 0xFFFFFFFF


def pattern(n: int, mul: int = 7, add: int = 3) -> bytes:
    return bytes((i * mul + add) & 0xFF for i in range(n))


IN_DATA = pattern(700)
SRC_DATA = pattern(SRC_LEN, 13, 0x41)
A_DATA = b"ABCD"
PATCH = bytes.fromhex("70634E75")      # moveq #99,d0 ; rts
LONGNAME = "x" * 32                     # one over RD_NAME_MAX


def scenarios() -> list[Scenario]:
    bp = de.fh_bptr
    O, R, W, S, C = "Open", "Read", "Write", "Seek", "Close"
    OLD, NEW, RW = de.MODE_OLDFILE, de.MODE_NEWFILE, de.MODE_READWRITE
    BEG, CUR, END = s32(de.OFFSET_BEGINNING), de.OFFSET_CURRENT, de.OFFSET_END
    E_NF, E_SEEK, E_INV = (de.ERROR_OBJECT_NOT_FOUND, de.ERROR_SEEK_ERROR,
                           de.ERROR_INVALID_LOCK)

    # ---- S1: reads, EOF, Seek, invalid handles, open errors -----------------
    n1 = ["nosuch", "in.bin", "IN.BIN", LONGNAME]
    na = lambda n: name_addr(n1, n)             # noqa: E731
    s1 = [
        Step(O, na("nosuch"), OLD, d0=0, ioerr=E_NF),                        # 0
        Step(O, na("in.bin"), OLD, d0=bp(0), ioerr=E_NF),                    # 1
        Step(R, d2=BUF + 1, d3=5, ref=1, d0=5, byte=IN_DATA[0]),             # 2
        Step(R, d2=BUF + 0x101, d3=0, ref=1, d0=0, byte=0),                  # 3 zero-length
        Step(R, d2=BUF + 7, d3=1000, ref=1, d0=695, byte=IN_DATA[5]),        # 4 >256, odd buf
        Step(R, d2=BUF + 0x800, d3=10, ref=1, d0=0, byte=0),                 # 5 EOF
        Step(S, d2=s32(-10), d3=END, ref=1, d0=700),                         # 6 old pos
        Step(R, d2=BUF + 0x900, d3=100, ref=1, d0=10, byte=IN_DATA[690]),    # 7 past EOF
        Step(S, d2=3, d3=CUR, ref=1, d0=NEG1, ioerr=E_SEEK),                 # 8 beyond end
        Step(S, d2=100, d3=BEG, ref=1, d0=700, ioerr=E_SEEK),                # 9 pos unchanged
        Step(S, d2=0, d3=CUR, ref=1, d0=100),                                # 10
        Step(S, d2=0, d3=7, ref=1, d0=NEG1, ioerr=E_SEEK),                   # 11 bad mode
        Step(S, d2=s32(-1), d3=BEG, ref=1, d0=NEG1, ioerr=E_SEEK),           # 12 negative
        Step(R, d2=BUF + 0xA00, d3=2, ref=1, d0=2, byte=IN_DATA[100]),       # 13 at 100
        Step(C, ref=1, d0=TRUE, ioerr=E_SEEK),                               # 14
        Step(C, ref=1, d0=FALSE, ioerr=E_INV),                               # 15 double close
        Step(R, d2=BUF + 0xB00, d3=4, ref=1, d0=NEG1, ioerr=E_INV, byte=0),  # 16 closed fh
        Step(W, d2=SRC, d3=4, ref=1, d0=NEG1, ioerr=E_INV),                  # 17 closed fh
        Step(S, d2=0, d3=BEG, ref=1, d0=NEG1, ioerr=E_INV),                  # 18 closed fh
        Step(C, d1=0, d0=TRUE, ioerr=E_INV),                                 # 19 Close(0)
        Step(C, d1=0x1234, d0=FALSE, ioerr=E_INV),                           # 20 bogus BPTR
        Step(C, d1=bp(0) + 1, d0=FALSE, ioerr=E_INV),                        # 21 mid-slot BPTR
        Step(O, na("IN.BIN"), OLD, d0=bp(0), ioerr=E_INV),                   # 22 case-insens.
        Step(O, na(LONGNAME), NEW, d0=0,
             ioerr=de.ERROR_INVALID_COMPONENT_NAME),                          # 23
        Step(O, na("in.bin"), 1234, d0=0, ioerr=de.ERROR_ACTION_NOT_KNOWN),  # 24 bad mode
        Step(C, ref=22, d0=TRUE),                                            # 25
    ]
    sc1 = Scenario(
        "reads", {"in.bin": IN_DATA}, n1, s1, init=False,
        expect_files={"in.bin": IN_DATA},
        expect_buf={BUF: b"\0" + IN_DATA[:5] + b"\0" + IN_DATA[5:700],
                    BUF + 0x900: IN_DATA[690:700] + b"\0",
                    BUF + 0xA00: IN_DATA[100:102] + b"\0",
                    BUF + 0x800: b"\0" * 16},
        expect_fh_args={0: de.FH_KIND_DEAD, 1: 0})

    # ---- S2: writes, read-back, modes, auto-format ---------------------------
    n2 = ["out.txt", "rw.dat", "t.txt"]
    nb = lambda n: name_addr(n2, n)             # noqa: E731
    out = bytearray(SRC_DATA[1:301])
    out[100:104] = SRC_DATA[0x200:0x204]
    s2 = [
        Step(O, nb("out.txt"), OLD, d0=0, ioerr=E_NF),                       # 0 fresh disk
        Step(O, nb("out.txt"), NEW, d0=bp(0), ioerr=E_NF),                   # 1
        Step(W, d2=SRC + 1, d3=300, ref=1, d0=300),                          # 2 odd, >256
        Step(W, d2=SRC, d3=0, ref=1, d0=0),                                  # 3 zero-length
        Step(S, d2=0, d3=BEG, ref=1, d0=300),                                # 4
        Step(R, d2=BUF + 3, d3=1000, ref=1, d0=300, byte=SRC_DATA[1]),       # 5 read-back
        Step(R, d2=BUF + 0x400, d3=1, ref=1, d0=0, byte=0),                  # 6 EOF
        Step(S, d2=100, d3=BEG, ref=1, d0=300),                              # 7
        Step(W, d2=SRC + 0x200, d3=4, ref=1, d0=4),                          # 8 overwrite
        Step(S, d2=0, d3=END, ref=1, d0=104),                                # 9 size kept
        Step(S, d2=0, d3=CUR, ref=1, d0=300),                                # 10
        Step(C, ref=1, d0=TRUE),                                             # 11
        Step(O, nb("out.txt"), OLD, d0=bp(0)),                               # 12 reopen
        Step(S, d2=0, d3=END, ref=12, d0=0),                                 # 13
        Step(S, d2=0, d3=CUR, ref=12, d0=300),                               # 14 persisted
        Step(W, d2=SRC + 0x300, d3=2, ref=12, d0=2),                         # 15 OLDFILE append
        Step(C, ref=12, d0=TRUE),                                            # 16
        Step(O, nb("rw.dat"), RW, d0=bp(0)),                                 # 17 RW creates
        Step(S, d2=0, d3=END, ref=17, d0=0),                                 # 18
        Step(S, d2=0, d3=CUR, ref=17, d0=0),                                 # 19 empty
        Step(W, d2=SRC + 5, d3=2, ref=17, d0=2),                             # 20
        Step(O, nb("out.txt"), RW, d0=bp(1)),                                # 21 RW existing
        Step(S, d2=0, d3=END, ref=21, d0=0),                                 # 22 pos 0
        Step(S, d2=0, d3=CUR, ref=21, d0=302),                               # 23 not truncated
        Step(C, ref=21, d0=TRUE),                                            # 24
        Step(C, ref=17, d0=TRUE),                                            # 25
        Step(O, nb("t.txt"), NEW, d0=bp(0)),                                 # 26
        Step(W, d2=SRC + 0x40, d3=10, ref=26, d0=10),                        # 27
        Step(C, ref=26, d0=TRUE),                                            # 28
        Step(O, nb("t.txt"), NEW, d0=bp(0)),                                 # 29 truncates
        Step(S, d2=0, d3=END, ref=29, d0=0),                                 # 30
        Step(S, d2=0, d3=CUR, ref=29, d0=0),                                 # 31 size 0
        Step(W, d2=SRC + 0x10, d3=3, ref=29, d0=3),                          # 32
        Step(C, ref=29, d0=TRUE, ioerr=E_NF),                                # 33 sticky IoErr
    ]
    sc2 = Scenario(
        "writes", None, n2, s2, zap_header=True,
        expect_files={"out.txt": bytes(out) + SRC_DATA[0x300:0x302],
                      "rw.dat": SRC_DATA[5:7],
                      "t.txt": SRC_DATA[0x10:0x13]},
        expect_buf={BUF: b"\0\0\0" + SRC_DATA[1:301] + b"\0"})

    # ---- S3: handle exhaustion + shared file size ----------------------------
    n3 = ["a"]
    nc = name_addr(n3, "a")
    s3 = [Step(O, nc, OLD, d0=bp(i), ioerr=0) for i in range(de.FH_SLOTS)]   # 0-7
    s3 += [
        Step(O, nc, OLD, d0=0, ioerr=de.ERROR_NO_FREE_STORE),                 # 8
        Step(C, ref=3, d0=TRUE, ioerr=de.ERROR_NO_FREE_STORE),                # 9
        Step(O, nc, OLD, d0=bp(3)),                                           # 10 reuse
        Step(R, d2=BUF, d3=4, ref=7, d0=4, byte=A_DATA[0]),                   # 11
        Step(R, d2=BUF + 4, d3=2, ref=0, d0=2, byte=A_DATA[0]),               # 12 own pos
        Step(R, d2=BUF + 8, d3=4, ref=0, d0=2, byte=A_DATA[2]),               # 13
        Step(S, d2=0, d3=END, ref=1, d0=0),                                   # 14
        Step(W, d2=SRC, d3=4, ref=1, d0=4),                                   # 15 grow to 8
        Step(S, d2=0, d3=END, ref=2, d0=0),                                   # 16
        Step(S, d2=0, d3=CUR, ref=2, d0=8),                                   # 17 shared size
        Step(R, d2=BUF + 16, d3=8, ref=10, d0=8, byte=A_DATA[0]),             # 18
    ]
    s3 += [Step(C, ref=r, d0=TRUE) for r in (0, 1, 2, 10, 4, 5, 6, 7)]       # 19-26
    s3 += [Step(C, ref=0, d0=FALSE, ioerr=E_INV)]                             # 27
    sc3 = Scenario(
        "handles", {"a": A_DATA}, n3, s3,
        expect_files={"a": A_DATA + SRC_DATA[:4]},
        expect_buf={BUF: A_DATA + b"AB\0\0CD" + b"\0" * 6
                    + A_DATA + SRC_DATA[:4]},
        expect_fh_args={i: de.FH_KIND_DEAD for i in range(de.FH_SLOTS)})

    # ---- S4: Read over the executing code page (window invalidation) ---------
    n4 = ["patch"]
    s4 = [
        Step(O, name_addr(n4, "patch"), OLD, d0=bp(0), ioerr=0),
        Step(R, d2=image.BASE_ADDR + PATCH_OFF, d3=4, ref=0, d0=4,
             byte=PATCH[0]),
    ]
    sc4 = Scenario("codepage", {"patch": PATCH}, n4, s4, inpage=True,
                   expect_d0=0x63, expect_fh_args={0: de.FH_KIND_FILE0})
    return [sc1, sc2, sc3, sc4]


# --- Staging --------------------------------------------------------------------

def script_bytes(sc: Scenario) -> bytes:
    out = bytearray()
    for st in sc.steps:
        out += struct.pack(">hhIII", LVO[st.fn], st.ref, s32(st.d1),
                           s32(st.d2), s32(st.d3))
    out += b"\0" * REC_SIZE
    return bytes(out)


def test_env_chunks(stack_top: int) -> list[Chunk]:
    """Production env + TEST-ONLY Read/Write slot overrides (staged last)."""
    chunks = hle_env.env_chunks(stack_top=stack_top)
    for lvo, idx in ((de.LVO_READ, de.HLE_DOS_IDX_READ),
                     (de.LVO_WRITE, de.HLE_DOS_IDX_WRITE)):
        chunks.append(Chunk(hle_env.DOS_BASE + lvo,
                            struct.pack(">H", 0xA100 | idx) + b"\0" * 4))
    return chunks


def scenario_chunks(sc: Scenario) -> tuple[list[Chunk], dict]:
    b = HunkBuilder()
    a6 = INPAGE_BASE if sc.inpage else hle_env.DOS_BASE
    b.add_code(build_program(a6, sc.inpage))
    chunks, manifest = image.build_image(hunk.parse(b.build()))
    names = b"".join(n.encode() + b"\0" for n in sc.names)
    chunks += test_env_chunks(manifest["stack_top"])
    chunks += [
        Chunk(SCRIPT, script_bytes(sc)),
        Chunk(RESULTS, bytes(STEP_SIZE * (len(sc.steps) + 1))),
        Chunk(BUF, bytes(BUF_LEN)),
        Chunk(SRC, SRC_DATA),
        Chunk(NAMES, names),
        Chunk(de.FH_POOL, bytes(de.FH_SIZE * de.FH_SLOTS)),
    ]
    if sc.files is not None:
        chunks += de.ramdisk_chunks(sc.files)
    if sc.zap_header:
        chunks.append(Chunk(de.RD_BASE, bytes(de.RD_HDR_SIZE)))
    return chunks, manifest


# --- Bulk guest reads (test-only helper injected into buf_case) -----------------

PEEK_CODE_AT = 0xC000        # buf_case: free between staging records
PEEK_DST = 0xC800            # buf_result (runner result area, $C800-$CDFF)
PEEK_MAX = 0x400


def peek_routine(src: int, n: int, rec_dirty: int) -> bytes:
    """6502: one REU->C64 DMA of n bytes to PEEK_DST, then mem_rec_dirty (C1)."""
    return bytes([
        0xA9, PEEK_DST & 0xFF, 0x8D, 0x02, 0xDF,
        0xA9, PEEK_DST >> 8, 0x8D, 0x03, 0xDF,
        0xA9, src & 0xFF, 0x8D, 0x04, 0xDF,
        0xA9, (src >> 8) & 0xFF, 0x8D, 0x05, 0xDF,
        0xA9, (src >> 16) & 0xFF, 0x8D, 0x06, 0xDF,
        0xA9, n & 0xFF, 0x8D, 0x07, 0xDF,
        0xA9, n >> 8, 0x8D, 0x08, 0xDF,
        0xA9, 0x00, 0x8D, 0x0A, 0xDF,
        0xA9, 0x91, 0x8D, 0x01, 0xDF,          # REU -> C64, execute
        0x20, rec_dirty & 0xFF, rec_dirty >> 8,
        0x60,
    ])


class Guest:
    def __init__(self, sess, labels):
        from tests.harness_util import lookup_label
        self.sess = sess
        self.rec_dirty = lookup_label(labels, "mem_rec_dirty")

    def read(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes, write_bytes
        from tests.harness_util import call_runner
        t = self.sess.target
        out = bytearray()
        while n > 0:
            k = min(n, PEEK_MAX)
            write_bytes(t.transport, PEEK_CODE_AT,
                        peek_routine(addr, k, self.rec_dirty))
            call_runner(t, PEEK_CODE_AT, self.sess.timeout)
            out += read_bytes(t.transport, PEEK_DST, k)
            addr, n = addr + k, n - k
        return bytes(out)


# --- Checks -----------------------------------------------------------------------

def check_scenario(sess, g: Guest, sc: Scenario, res) -> None:
    tag = sc.name
    sess.check(f"{tag}: halted HALT_OK", res.halt != 0 and res.timeout == 0
               and res.reason == 0,
               f"halt={res.halt:02X} reason={res.reason:02X} "
               f"timeout={res.timeout} PC={res.pc:08X}")
    raw = g.read(RESULTS, STEP_SIZE * len(sc.steps))
    for i, st in enumerate(sc.steps):
        d0, ioerr, byte, d1 = struct.unpack_from(">IIII", raw, i * STEP_SIZE)
        what = f"{tag}[{i}] {st.fn}"
        if st.d0 is not None:
            sess.check(f"{what} D0={st.d0:08X}", d0 == st.d0,
                       f"got {d0:08X} (D1={d1:08X})")
        if st.ioerr is not None:
            sess.check(f"{what} IoErr={st.ioerr}", ioerr == st.ioerr,
                       f"got {ioerr}")
        if st.byte is not None:
            sess.check(f"{what} [D2]={st.byte:02X}", byte == st.byte,
                       f"got {byte:02X}")
    for addr, want in sc.expect_buf.items():
        got = g.read(addr, len(want))
        bad = [i for i in range(len(want)) if got[i] != want[i]]
        sess.check(f"{tag}: guest buffer ${addr:06X} ({len(want)} bytes)",
                   not bad, f"{len(bad)} byte(s) differ, first at +{bad[0]:X}"
                   if bad else "")
    if sc.expect_files:
        try:
            files = de.parse_directory(g.read)
        except ValueError as e:
            sess.check(f"{tag}: RAMDISK readable", False, str(e))
            files = {}
        for name, want in sc.expect_files.items():
            ent = files.get(name.lower())
            sess.check(f"{tag}: file {name} = {len(want)} bytes",
                       ent is not None and ent.data == want,
                       "missing" if ent is None else
                       f"size {ent.size}, data {'ok' if ent.data == want else 'differs'}")
    for slot, kind in sc.expect_fh_args.items():
        got = struct.unpack(">I", g.read(de.fh_addr(slot) + de.FH_ARGS_OFF, 4))[0]
        sess.check(f"{tag}: slot {slot} fh_Args={kind:08X}", got == kind,
                   f"got {got:08X}")
    if sc.expect_d0 is not None:
        sess.check(f"{tag}: exit D0={sc.expect_d0:02X} (patched code ran)",
                   res.regs["D0"] == sc.expect_d0, f"got {res.regs['D0']:08X}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_dos",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--labels")
    ap.add_argument("--only", help="comma-separated scenario names")
    ap.add_argument("--budget", type=int, default=200_000)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args(argv)

    prg = os.path.abspath(args.prg)
    labels_path = (os.path.abspath(args.labels) if args.labels
                   else os.path.splitext(prg)[0] + ".lbl")
    for p in (prg, labels_path):
        if not os.path.exists(p):
            print(f"error: not found: {p}", file=sys.stderr)
            return 2
    scs = scenarios()
    if args.only:
        want = set(args.only.split(","))
        scs = [s for s in scs if s.name in want]

    from c64_test_harness import Labels, UnifiedManager, ViceConfig, read_bytes
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_program import Session
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(labels_path)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=args.timeout)
            g = Guest(sess, labels)
            for sc in scs:
                chunks, manifest = scenario_chunks(sc)
                print(f"[{sc.name}] {len(sc.steps)} step(s)")
                sess.stage(chunks)
                if sc.init:
                    try:
                        sess._call("hle_dos_init")
                    except KeyError as e:
                        sess.check(f"{sc.name}: hle_dos_init exported", False,
                                   str(e))
                res = sess.run_program(manifest["entry"], manifest["stack_top"],
                                       args.budget)
                res.print_state()
                check_scenario(sess, g, sc, res)
            try:
                be = read_bytes(target.transport,
                                lookup_label(labels, "dosio_backend"), 1)[0]
                sess.check("backend = RAMDISK (0) under VICE (no UCI)", be == 0,
                           f"got {be}")
            except KeyError as e:
                sess.check("dosio_backend exported", False, str(e))
        finally:
            mgr.release(target)

    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    if n_bad:
        for f in sess.failures[:60]:
            print(f"FAIL  {f}")
        if n_bad > 60:
            print(f"... {n_bad - 60} more")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
