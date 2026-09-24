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

Leg ``selection`` (review ruling 1) builds a TEST-ONLY ``DOSIO_FAKE_UCI``
variant whose detection always reports a UCI, and checks that the backend
stays RAMDISK unless the host wrote the opt-in key to ``dosio_want_uci``.

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
       "Seek": -66, "IoErr": -132, "Delete": -72, "ChangeMode": -450}


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
    files: dict[str, bytes] | list | None  # None: no RAMDISK (list: RdFile)
    names: list[str]
    steps: list[Step]
    zap_header: bool = False           # stage an invalid RAMDISK header
    inpage: bool = False
    init: bool = True                  # call hle_dos_init before the run
    expect_files: dict[str, bytes] = field(default_factory=dict)
    expect_buf: dict[int, bytes] = field(default_factory=dict)
    expect_d0: int | None = None       # final D0 (program exit)
    expect_fh_args: dict[int, int] = field(default_factory=dict)
    extra: list = field(default_factory=list)   # extra Chunks staged last
    expect_absent: list[str] = field(default_factory=list)   # not on RAMDISK
    expect_addr: dict[str, int] = field(default_factory=dict)  # extent address


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
    return [sc1, sc2, sc3, sc4] + fix1_scenarios()


def fix1_scenarios() -> list[Scenario]:
    """Review fix round 1 (adversarial review of 2e90271): extent limit,
    directory capacity, stale directory after auto-format, AmigaOS sharing,
    guest-corrupted fh_Args, Open(NULL)."""
    bp = de.fh_bptr
    O, R, W, S, C = "Open", "Read", "Write", "Seek", "Close"
    OLD, NEW, RW = de.MODE_OLDFILE, de.MODE_NEWFILE, de.MODE_READWRITE
    BEG, CUR, END = s32(de.OFFSET_BEGINNING), de.OFFSET_CURRENT, de.OFFSET_END
    E_NF, E_INV, E_USE, E_FULL = (de.ERROR_OBJECT_NOT_FOUND,
                                  de.ERROR_INVALID_LOCK,
                                  de.ERROR_OBJECT_IN_USE, de.ERROR_DISK_FULL)
    out = []

    # ---- diskfull: the 64 KB per-file extent returns ERROR_DISK_FULL -------
    n = ["big"]
    st = [
        Step(O, name_addr(n, "big"), NEW, d0=bp(0), ioerr=0),             # 0
        Step(W, d2=0x010000, d3=de.RD_NEW_CAP, ref=0, d0=de.RD_NEW_CAP,
             ioerr=0),                                                     # 1 fill
        Step(W, d2=0x010000, d3=1, ref=0, d0=NEG1, ioerr=E_FULL),          # 2 +1 byte
        Step(S, d2=0, d3=END, ref=0, d0=de.RD_NEW_CAP),                    # 3 nothing
        Step(S, d2=0, d3=CUR, ref=0, d0=de.RD_NEW_CAP),                    # 4 written
        Step(W, d2=0x010000, d3=NEG1, ref=0, d0=NEG1, ioerr=E_FULL),       # 5 len -1
        Step(S, d2=1, d3=END, ref=0, d0=NEG1,
             ioerr=de.ERROR_SEEK_ERROR),                                   # 6 END+1
        Step(S, d2=0, d3=BEG, ref=0, d0=de.RD_NEW_CAP),                    # 7
        Step(R, d2=BUF, d3=NEG1, ref=0, d0=de.RD_NEW_CAP),                 # 8 len -1
        Step(S, d2=0, d3=CUR, ref=0, d0=de.RD_NEW_CAP),                    # 9 at EOF
        Step(C, ref=0, d0=TRUE),                                           # 10
    ]
    out.append(Scenario("diskfull", None, n, st, zap_header=True))

    # ---- dirfull: all 32 directory entries allocatable (2 MB region) --------
    n = [f"f{i:02d}" for i in range(de.RD_ENTRIES + 1)]
    st = []
    for i in range(de.RD_ENTRIES):
        st.append(Step(O, name_addr(n, n[i]), NEW, d0=bp(0), ioerr=0))
        st.append(Step(C, ref=len(st) - 1, d0=TRUE))
    st.append(Step(O, name_addr(n, n[-1]), NEW, d0=0, ioerr=E_FULL))      # 33rd
    st.append(Step(O, name_addr(n, n[-2]), OLD, d0=bp(0), ioerr=E_FULL))  # last one
    st.append(Step(W, d2=SRC, d3=16, ref=len(st) - 1, d0=16))             # usable
    st.append(Step(C, ref=len(st) - 2, d0=TRUE))
    out.append(Scenario("dirfull", None, n, st, zap_header=True,
                        expect_files={n[0]: b"", n[-2]: SRC_DATA[:16]}))

    # ---- staledir: auto-format must wipe directory entries ---------------
    n = ["ghost", "fresh"]
    st = [
        Step(O, name_addr(n, "ghost"), OLD, d0=0, ioerr=E_NF),            # 0 wiped
        Step(O, name_addr(n, "fresh"), NEW, d0=bp(0), ioerr=E_NF),        # 1
        Step(W, d2=SRC, d3=3, ref=1, d0=3),                               # 2
        Step(C, ref=1, d0=TRUE),                                          # 3
        Step(O, name_addr(n, "ghost"), RW, d0=bp(0), ioerr=E_NF),         # 4 new, empty
        Step(S, d2=0, d3=END, ref=4, d0=0),                               # 5
        Step(S, d2=0, d3=CUR, ref=4, d0=0),                               # 6 size 0
        Step(C, ref=4, d0=TRUE),                                          # 7
    ]
    out.append(Scenario("staledir", {"ghost": b"BOO!"}, n, st, zap_header=True,
                        expect_files={"fresh": SRC_DATA[:3], "ghost": b""}))

    # ---- lock: MODE_NEWFILE exclusive, OLDFILE/READWRITE shared ----------
    n = ["a", "A", "b"]
    st = [
        Step(O, name_addr(n, "a"), OLD, d0=bp(0), ioerr=0),               # 0 shared
        Step(S, d2=0, d3=END, ref=0, d0=0),                               # 1 pos 4
        Step(O, name_addr(n, "A"), NEW, d0=0, ioerr=E_USE),               # 2 in use
        # fix2 item 2: the refused NEWFILE had ZERO side effects
        Step(S, d2=0, d3=END, ref=0, d0=4),                               # 3 old pos
        Step(S, d2=0, d3=BEG, ref=0, d0=4),                               # 4 size still 4
        Step(R, d2=BUF + 0x100, d3=4, ref=0, d0=4, byte=ord("A")),        # 5 bytes intact
        Step(W, d2=SRC, d3=2, ref=0, d0=2),                               # 6 untouched
        Step(O, name_addr(n, "a"), RW, d0=bp(1), ioerr=E_USE),            # 7 shares
        Step(O, name_addr(n, "a"), OLD, d0=bp(2), ioerr=E_USE),           # 8 shares
        Step(C, ref=0, d0=TRUE), Step(C, ref=7, d0=TRUE),                 # 9 10
        Step(O, name_addr(n, "A"), NEW, d0=0, ioerr=E_USE),               # 11 slot 2 open
        Step(S, d2=0, d3=END, ref=8, d0=0),                               # 12 old pos 0
        Step(S, d2=0, d3=BEG, ref=8, d0=6),                               # 13 size still 6
        Step(C, ref=8, d0=TRUE),                                          # 14
        Step(O, name_addr(n, "A"), NEW, d0=bp(0), ioerr=E_USE),           # 15 exclusive
        Step(O, name_addr(n, "a"), OLD, d0=0, ioerr=E_USE),               # 16
        Step(O, name_addr(n, "a"), RW, d0=0, ioerr=E_USE),                # 17
        Step(O, name_addr(n, "a"), NEW, d0=0, ioerr=E_USE),               # 18
        Step(O, name_addr(n, "b"), NEW, d0=bp(1), ioerr=E_USE),           # 19 other file
        Step(W, d2=SRC + 8, d3=1, ref=15, d0=1),                          # 20
        Step(C, ref=15, d0=TRUE),                                         # 21
        Step(O, name_addr(n, "a"), OLD, d0=bp(0), ioerr=E_USE),           # 22 released
        Step(C, ref=22, d0=TRUE), Step(C, ref=19, d0=TRUE),               # 23 24
    ]
    out.append(Scenario("lock", {"a": b"ABCD"}, n, st,
                        expect_files={"a": SRC_DATA[8:9], "b": b""},
                        expect_buf={BUF + 0x100: b"ABCD\0"}))

    # ---- fmtlock (fix2 item 1, reviewer adv_fmt): never auto-format while a
    # handle is open.  The guest clobbers the RAMDISK magic through a Read
    # while two handles are open; the next Open must fail 225 and leave the
    # header alone (no entry written into file data); once all handles are
    # closed the format happens and the new file really exists.
    n = ["a", "hdr", "n"]
    st = [
        Step(O, name_addr(n, "a"), OLD, d0=bp(0), ioerr=0),               # 0 fid 0
        Step(O, name_addr(n, "hdr"), OLD, d0=bp(1), ioerr=0),             # 1 fid 1
        Step(R, d2=de.RD_BASE, d3=4, ref=1, d0=4),                        # 2 magic := XXXX
        Step(O, name_addr(n, "n"), NEW, d0=0,
             ioerr=de.ERROR_NOT_A_DOS_DISK),                               # 3 refused
        Step(O, name_addr(n, "a"), OLD, d0=0,
             ioerr=de.ERROR_NOT_A_DOS_DISK),                               # 4 any mode
        Step(R, d2=BUF, d3=16, ref=0, d0=16, byte=ord("A")),              # 5 open fh ok
        Step(C, ref=0, d0=TRUE), Step(C, ref=1, d0=TRUE),                 # 6 7
        Step(O, name_addr(n, "n"), NEW, d0=bp(0),
             ioerr=de.ERROR_NOT_A_DOS_DISK),                               # 8 formats
        Step(W, d2=SRC, d3=5, ref=8, d0=5),                               # 9
        Step(C, ref=8, d0=TRUE),                                          # 10
        Step(O, name_addr(n, "n"), OLD, d0=bp(0),
             ioerr=de.ERROR_NOT_A_DOS_DISK),                               # 11 n exists
        Step(S, d2=0, d3=END, ref=11, d0=0),                              # 12
        Step(S, d2=0, d3=BEG, ref=11, d0=5),                              # 13 size 5
        Step(O, name_addr(n, "a"), OLD, d0=0,
             ioerr=de.ERROR_OBJECT_NOT_FOUND),                             # 14 wiped
        Step(C, ref=11, d0=TRUE),                                         # 15
    ]
    fmt_files = [de.RdFile("a", b"A" * 16), de.RdFile("hdr", b"XXXX")]
    out.append(Scenario(
        "fmtlock", fmt_files, n, st,
        extra=[Chunk(de.RD_DIR + 202 * de.RD_ENT_SIZE, bytes(64))],
        expect_files={"n": SRC_DATA[:5]},
        expect_buf={BUF: b"A" * 16,
                    # entry #202 would land in file data at +$32A0
                    de.RD_DIR + 202 * de.RD_ENT_SIZE: bytes(64)}))

    # ---- dirscribble (fix2 item 1b): the create path honours a refusal ----
    # The guest zeroes the "used" flag of the directory entry of a file it
    # holds EXCLUSIVELY (NEWFILE).  A second NEWFILE of that name no longer
    # finds it, creates at the lowest free index -- the same index, i.e. the
    # same file id -- and the lockcheck refuses: Open must fail 0/202 and
    # write nothing (before fix2 the refusal carry was ignored).
    n = ["d", "z"]
    st = [
        Step(O, name_addr(n, "d"), NEW, d0=bp(0), ioerr=0),               # 0 excl, idx 0
        Step(O, name_addr(n, "z"), OLD, d0=bp(1), ioerr=0),               # 1 idx 1
        Step(R, d2=de.RD_DIR, d3=1, ref=1, d0=1),                         # 2 flags[0]:=0
        Step(O, name_addr(n, "d"), NEW, d0=0, ioerr=E_USE),               # 3 refused
        Step(C, ref=1, d0=TRUE, ioerr=E_USE),                             # 4
        Step(C, ref=0, d0=TRUE, ioerr=E_USE),                             # 5
    ]
    out.append(Scenario("dirscribble", {"d": b"", "z": b"\0"}, n, st,
                        expect_buf={de.RD_DIR: b"\0\x01"}))

    # ---- fhargs: guest corrupts fh_Args; Close still releases the slot ----
    n = ["k"]
    fa = de.fh_addr(0) + de.FH_ARGS_OFF
    st = [
        Step(O, name_addr(n, "k"), OLD, d0=bp(0)),                        # 0
        Step(R, d2=fa, d3=4, ref=0, d0=4),                                # 1 fh_Args := $11
        Step(R, d2=BUF, d3=1, ref=0, d0=NEG1, ioerr=E_INV),               # 2 Read refuses
        Step(W, d2=SRC, d3=1, ref=0, d0=NEG1, ioerr=E_INV),               # 3 Write refuses
        Step(S, d2=0, d3=BEG, ref=0, d0=NEG1, ioerr=E_INV),               # 4 Seek refuses
        Step(C, ref=0, d0=TRUE, ioerr=E_INV),                             # 5 Close works
        Step(O, name_addr(n, "k"), OLD, d0=bp(0)),                        # 6 slot 0 free
        Step(C, ref=6, d0=TRUE),                                          # 7
        Step(C, ref=0, d0=FALSE, ioerr=E_INV),                            # 8 now closed
    ]
    out.append(Scenario("fhargs", {"k": b"\0\0\0\x11"}, n, st,
                        expect_fh_args={0: de.FH_KIND_DEAD}))

    # ---- wrap512 (item 11): no DMA may straddle a 512 KB REU block ----------
    # The REC increments only the low 19 address bits, so a transfer across
    # $x7FFFF -> $x80000 would wrap to the start of the same block.
    # (a) guest side: a buffer straddling $180000 (Read and Write);
    # (b) RAMDISK side: file data straddling $E80000 (an extent placed at
    #     $E7FF00, accessed through an odd guest buffer so a chunk spans it).
    gbuf = 0x17FF80
    wdata = pattern(512, 5, 0x11)
    gdata = pattern(256, 3, 0x77)
    sentinel = bytes((0xA5 ^ i) & 0xFF for i in range(0x80))
    pad_cap = 0xE7FF00 - de.RD_DATA           # places file "w" at $E7FF00
    files = [de.RdFile("pad", b"", capacity=pad_cap),
             de.RdFile("w", wdata), de.RdFile("g", gdata)]
    n = ["w", "g", "gw"]
    st = [
        Step(O, name_addr(n, "w"), OLD, d0=bp(0), ioerr=0),               # 0
        Step(R, d2=BUF + 0x80, d3=512, ref=0, d0=512, byte=wdata[0]),     # 1 disk straddle
        Step(S, d2=0, d3=BEG, ref=0, d0=512),                             # 2
        Step(W, d2=SRC + 0x80, d3=512, ref=0, d0=512),                    # 3 disk straddle
        Step(C, ref=0, d0=TRUE),                                          # 4
        Step(O, name_addr(n, "g"), OLD, d0=bp(0), ioerr=0),               # 5
        Step(R, d2=gbuf, d3=256, ref=5, d0=256, byte=gdata[0]),           # 6 guest straddle
        Step(C, ref=5, d0=TRUE),                                          # 7
        Step(O, name_addr(n, "gw"), NEW, d0=bp(0), ioerr=0),              # 8
        Step(W, d2=gbuf, d3=256, ref=8, d0=256),                          # 9 guest straddle
        Step(C, ref=8, d0=TRUE),                                          # 10
    ]
    out.append(Scenario(
        "wrap512", files, n, st,
        extra=[Chunk(0x100000, sentinel), Chunk(gbuf, bytes(256)),
               Chunk(0x180080, sentinel)],
        expect_files={"w": SRC_DATA[0x80:0x280], "g": gdata, "gw": gdata,
                      "pad": b""},
        expect_buf={BUF + 0x80: wdata, gbuf: gdata,
                    0x100000: sentinel, 0x180080: sentinel,
                    0xE00000: de.RD_MAGIC}))

    # ---- nullname: Open(NULL) ----------------------------------------------
    st = [Step(O, 0, NEW, d0=0, ioerr=E_NF),
          Step(O, 0, OLD, d0=0, ioerr=E_NF)]
    out.append(Scenario("nullname", None, [], st, zap_header=True))
    return out + post_scenarios()


def post_scenarios() -> list[Scenario]:
    """m2-post: DeleteFile, extent reuse, "RAM:" prefix, ChangeMode."""
    bp = de.fh_bptr
    O, R, W, C, D, CM = "Open", "Read", "Write", "Close", "Delete", "ChangeMode"
    OLD, NEW, RW = de.MODE_OLDFILE, de.MODE_NEWFILE, de.MODE_READWRITE
    E_NF, E_INV, E_USE, E_FULL, E_NAME, E_ACT = (
        de.ERROR_OBJECT_NOT_FOUND, de.ERROR_INVALID_LOCK,
        de.ERROR_OBJECT_IN_USE, de.ERROR_DISK_FULL,
        de.ERROR_INVALID_COMPONENT_NAME, de.ERROR_ACTION_NOT_KNOWN)
    EXCL = s32(de.EXCLUSIVE_LOCK)
    out = []

    # ---- delete: DeleteFile semantics + the RAM: prefix ----------------------
    n = ["a", "nosuch", "A", "RAM:b", "ram:C", "RAM:", "Ram:" + "y" * 31]
    na = lambda x: name_addr(n, x)              # noqa: E731
    st = [
        Step(D, na("nosuch"), d0=FALSE, ioerr=E_NF),                      # 0
        Step(O, na("a"), OLD, d0=bp(0), ioerr=E_NF),                      # 1
        Step(D, na("A"), d0=FALSE, ioerr=E_USE),                          # 2 open
        Step(C, ref=1, d0=TRUE, ioerr=E_USE),                             # 3
        Step(D, na("a"), d0=TRUE, ioerr=E_USE),                           # 4 sticky
        Step(O, na("a"), OLD, d0=0, ioerr=E_NF),                          # 5 gone
        Step(D, na("RAM:b"), d0=TRUE, ioerr=E_NF),                        # 6 prefix
        Step(O, na("ram:C"), OLD, d0=bp(0), ioerr=E_NF),                  # 7 prefix
        Step(R, d2=BUF, d3=4, ref=7, d0=1, byte=ord("C")),                # 8
        Step(C, ref=7, d0=TRUE),                                          # 9
        Step(D, na("RAM:"), d0=FALSE, ioerr=E_NAME),                      # 10 empty
        Step(D, 0, d0=FALSE, ioerr=E_NF),                                 # 11 NULL
        Step(O, na("RAM:"), NEW, d0=0, ioerr=E_NAME),                     # 12
        Step(O, na("Ram:" + "y" * 31), NEW, d0=bp(0), ioerr=E_NAME),      # 13 31 left
        Step(C, ref=13, d0=TRUE),                                         # 14
        Step(O, na("a"), NEW, d0=bp(0), ioerr=E_NAME),                    # 15 re-create
        Step(W, d2=SRC, d3=3, ref=15, d0=3),                              # 16
        Step(C, ref=15, d0=TRUE),                                         # 17
    ]
    # creates take the first free directory entry and reuse its extent:
    # "y"*31 gets a's (index 0), the new "a" gets b's (index 1)
    out.append(Scenario(
        "delete", {"a": b"AAAA", "b": b"BB", "c": b"C"}, n, st,
        expect_files={"a": SRC_DATA[:3], "c": b"C", "y" * 31: b""},
        expect_absent=["b", "RAM:b", "ram:c", "Ram:" + "y" * 31],
        expect_addr={"y" * 31: de.RD_DATA, "a": de.RD_DATA + de.RD_NEW_CAP,
                     "c": de.RD_DATA + 2 * de.RD_NEW_CAP}))

    # ---- delreuse: a full RAMDISK gets space back from a delete --------------
    n = [f"f{i:02d}" for i in range(de.RD_ENTRIES + 2)]
    st = []
    for i in range(de.RD_ENTRIES):
        st.append(Step(O, name_addr(n, n[i]), NEW, d0=bp(0), ioerr=0))
        st.append(Step(C, ref=len(st) - 1, d0=TRUE))
    st += [
        Step(O, name_addr(n, n[32]), NEW, d0=0, ioerr=E_FULL),            # full
        Step(D, name_addr(n, n[5]), d0=TRUE, ioerr=E_FULL),               # frees 5
        Step(O, name_addr(n, n[32]), NEW, d0=bp(0), ioerr=E_FULL),        # fits
    ]
    st += [
        Step(W, d2=SRC, d3=de.RD_NEW_CAP, ref=len(st) - 1,
             d0=de.RD_NEW_CAP),                                           # 64 KB
        Step(C, ref=len(st) - 1, d0=TRUE),
        Step(O, name_addr(n, n[33]), NEW, d0=0, ioerr=E_FULL),            # full again
    ]
    out.append(Scenario(
        "delreuse", None, n, st, zap_header=True,
        expect_files={n[4]: b"", n[6]: b""},
        expect_absent=[n[5], n[33]],
        expect_addr={n[32]: de.RD_DATA + 5 * de.RD_NEW_CAP}))

    # ---- changemode: CHANGE_FH + EXCLUSIVE_LOCK under the sharing rules ------
    n = ["m", "M"]
    st = [
        Step(O, name_addr(n, "m"), RW, d0=bp(0), ioerr=0),                # 0
        Step(CM, de.CHANGE_FH, bp(0), EXCL, d0=TRUE, ioerr=0),            # 1
        Step(O, name_addr(n, "M"), OLD, d0=0, ioerr=E_USE),               # 2 excl
        Step(O, name_addr(n, "m"), RW, d0=0, ioerr=E_USE),                # 3
        Step(CM, de.CHANGE_FH, bp(0), EXCL, d0=TRUE, ioerr=E_USE),        # 4 again
        Step(C, ref=0, d0=TRUE),                                          # 5
        Step(O, name_addr(n, "m"), OLD, d0=bp(0), ioerr=E_USE),           # 6 released
        Step(O, name_addr(n, "m"), OLD, d0=bp(1), ioerr=E_USE),           # 7 shares
        Step(CM, de.CHANGE_LOCK, bp(0), EXCL, d0=FALSE, ioerr=E_ACT),     # 8
        Step(CM, de.CHANGE_FH, bp(0), EXCL, d0=FALSE, ioerr=E_USE),       # 9 shared
        Step(CM, de.CHANGE_FH, bp(0), s32(de.SHARED_LOCK), d0=FALSE,
             ioerr=E_ACT),                                                 # 10
        Step(C, ref=7, d0=TRUE),                                          # 11
        Step(CM, de.CHANGE_FH, bp(1), EXCL, d0=FALSE, ioerr=E_INV),       # 12 closed
        Step(CM, de.CHANGE_FH, bp(0), EXCL, d0=TRUE, ioerr=E_INV),        # 13 alone
        Step(O, name_addr(n, "m"), OLD, d0=0, ioerr=E_USE),               # 14
        Step(W, d2=SRC, d3=2, ref=6, d0=2),                               # 15
        Step(C, ref=6, d0=TRUE),                                          # 16
        Step(O, name_addr(n, "m"), NEW, d0=bp(0), ioerr=E_USE),           # 17 free
        Step(C, ref=17, d0=TRUE),                                         # 18
    ]
    out.append(Scenario("changemode", {"m": b"MM"}, n, st,
                        expect_files={"m": b""}))

    # ---- extscribble: a forged "deleted, keeps extent" entry is not reused
    # unless its extent lies inside the RAMDISK data area.  The guest Reads
    # a forged entry (flags $02, extent = its own buffer BUF+$800) into the
    # free directory entry 1; the next create must allocate from free_ptr.
    forged = bytearray(de.RD_ENT_SIZE)
    struct.pack_into("<BBHIII", forged, 0, 0x02, 1, 0, 0, de.RD_NEW_CAP,
                     BUF + 0x800)
    forged[de.RD_NAME_OFF] = ord("q")
    n = ["s", "n"]
    st = [
        Step(O, name_addr(n, "s"), OLD, d0=bp(0), ioerr=0),               # 0
        Step(R, d2=de.RD_DIR + de.RD_ENT_SIZE, d3=64, ref=0, d0=64),      # 1 forge
        Step(C, ref=0, d0=TRUE),                                          # 2
        Step(O, name_addr(n, "n"), NEW, d0=bp(0), ioerr=0),               # 3 entry 1
        Step(W, d2=SRC, d3=16, ref=3, d0=16),                             # 4
        Step(C, ref=3, d0=TRUE),                                          # 5
    ]
    out.append(Scenario(
        "extscribble", {"s": bytes(forged)}, n, st,
        expect_files={"n": SRC_DATA[:16]},
        expect_addr={"n": de.RD_DATA + de.RD_NEW_CAP},
        expect_buf={BUF + 0x800: bytes(16)}))
    return out


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
    chunks += sc.extra
    return chunks, manifest


# --- Bulk guest reads (test-only helper injected into buf_case) -----------------

PEEK_CODE_AT = 0xC000        # buf_case: free between staging records
PEEK_DST = 0xC800            # buf_result (runner result area, $C800-$CDFF)
PEEK_MAX = 0x400
REU_BLOCK = 0x80000          # REC address counter wraps within 512 KB


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
            # never let one REU DMA straddle a 512 KB block: the REC address
            # counter wraps inside the block (VICE reu.c, review item 11)
            k = min(n, PEEK_MAX, REU_BLOCK - (addr & (REU_BLOCK - 1)))
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
    if sc.expect_files or sc.expect_absent or sc.expect_addr:
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
        for name in sc.expect_absent:
            sess.check(f"{tag}: file {name} absent", name.lower() not in files,
                       "present")
        for name, addr in sc.expect_addr.items():
            ent = files.get(name.lower())
            sess.check(f"{tag}: file {name} extent at ${addr:06X}",
                       ent is not None and ent.addr == addr,
                       "missing" if ent is None else f"at ${ent.addr:06X}")
    for slot, kind in sc.expect_fh_args.items():
        got = struct.unpack(">I", g.read(de.fh_addr(slot) + de.FH_ARGS_OFF, 4))[0]
        sess.check(f"{tag}: slot {slot} fh_Args={kind:08X}", got == kind,
                   f"got {got:08X}")
    if sc.expect_d0 is not None:
        sess.check(f"{tag}: exit D0={sc.expect_d0:02X} (patched code ran)",
                   res.regs["D0"] == sc.expect_d0, f"got {res.regs['D0']:08X}")


UCI_OPT_IN = de.UCI_OPT_IN_KEY  # docs/HLE_DOS.md "Backend selection"


def build_fake_uci(prg: str) -> tuple[str, str]:
    """TEST-ONLY variant: dosio_uci_detect always reports a UCI
    (DOSIO_FAKE_UCI), with a tiny fence and poll budget so the UCI init's
    timeouts against VICE's open bus finish quickly."""
    import glob
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bdir = os.path.dirname(prg)
    disp = os.path.join(bdir, "gen", "dispatch.s")
    if not os.path.exists(disp):
        raise FileNotFoundError(f"{disp} (build the runtime with make first)")
    out = os.path.join(bdir, "fakeuci")
    os.makedirs(out, exist_ok=True)
    fprg, flbl = os.path.join(out, "runner.prg"), os.path.join(out, "runner.lbl")
    srcs = sorted(glob.glob(os.path.join(root, "runtime", "*.s"))
                  + glob.glob(os.path.join(root, "runtime", "ops", "*.s"))
                  + glob.glob(os.path.join(root, "tests", "runner", "*.s")))
    cmd = ["cl65", "-t", "none", "-C", os.path.join(root, "runtime", "c64.cfg"),
           "--asm-define", "DOSIO_FAKE_UCI=1",
           "--asm-define", "DOSIO_UCI_FENCE_OUTER=1",
           "--asm-define", "DOSIO_UCI_FENCE_INNER=1",
           "--asm-define", "DOSIO_UCI_POLLS_HI=1",
           "-o", fprg, "-Ln", flbl] + srcs + [disp]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=root)
    if r.returncode != 0:
        raise RuntimeError(r.stdout + r.stderr)
    return fprg, flbl


def selection_checks(prg: str, timeout: float,
                     budget: int) -> tuple[list[str], list[str]]:
    """Backend selection (review ruling 1): RAMDISK unless the host opted in.

    Runs on the DOSIO_FAKE_UCI build, where detection always says "UCI":
    boot state, want=0 and want=non-key must all give RAMDISK (and a real
    scenario must run on it); only the exact opt-in key gives ULTDOS."""
    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes, write_bytes)
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_program import Session
    from tests.run_vectors import prepare_target

    fprg, flbl = build_fake_uci(prg)
    labels = Labels.from_file(flbl)
    cfg = ViceConfig(prg_path=fprg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, fprg)
            sess = Session(target=target, labels=labels, timeout=timeout)
            t = target.transport
            want = lookup_label(labels, "dosio_want_uci")
            be = lookup_label(labels, "dosio_backend")

            def backend_after_init() -> int:
                sess._call("hle_dos_init")
                return read_bytes(t, be, 1)[0]

            got = backend_after_init()                     # boot BSS state
            sess.check("fakeuci: boot default backend = RAMDISK", got == 0,
                       f"got {got}")
            for val in (b"\0\0\0\0", b"\x01\x01\x01\x01", b"UCI?", b"\xFF" * 4):
                write_bytes(t, want, val)
                got = backend_after_init()
                sess.check(f"fakeuci: want={val.hex()} -> RAMDISK", got == 0,
                           f"got {got}")
            # a real scenario still runs on RAMDISK with detection saying UCI
            write_bytes(t, want, b"\0\0\0\0")
            sc = next(s for s in scenarios() if s.name == "writes")
            chunks, manifest = scenario_chunks(sc)
            sess.stage(chunks)
            sess._call("hle_dos_init")
            res = sess.run_program(manifest["entry"], manifest["stack_top"],
                                   budget)
            g = Guest(sess, labels)
            check_scenario(sess, g, Scenario("fakeuci-writes", sc.files,
                                             sc.names, sc.steps,
                                             expect_files=sc.expect_files),
                           res)
            got = read_bytes(t, be, 1)[0]
            sess.check("fakeuci: backend still RAMDISK after the run",
                       got == 0, f"got {got}")
            write_bytes(t, want, UCI_OPT_IN)               # explicit opt-in
            got = backend_after_init()
            sess.check("fakeuci: want='UCI!' -> ULTDOS (proves the flag "
                       "made detection say UCI)", got == 1, f"got {got}")
            write_bytes(t, want, b"\0\0\0\0")
            got = backend_after_init()
            sess.check("fakeuci: opt-out again -> RAMDISK", got == 0,
                       f"got {got}")
        finally:
            mgr.release(target)
    return sess.checks, sess.failures


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
    run_selection = True
    if args.only:
        want = set(args.only.split(","))
        scs = [s for s in scs if s.name in want]
        run_selection = "selection" in want

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

    if run_selection:
        print("[selection] DOSIO_FAKE_UCI build (second VICE instance)")
        try:
            ok, bad = selection_checks(prg, args.timeout, args.budget)
            sess.checks += ok
            sess.failures += bad
        except Exception as e:  # noqa: BLE001 - report, don't mask
            sess.check("selection: ran", False, f"{type(e).__name__}: {e}")

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
