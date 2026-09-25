"""W5-K console input: dos Read on the console handles, end to end.

Usage:
    python3 -m tests.run_con --prg build/w5-con/runner.prg
        [--only NAME[,NAME]] [--legs units,script,matrix,batch,scan]
        [--timeout SECS] [--budget N] [--vbcc-module FILE]
        [--backend auto|vice|u64]

What runs (docs/HLE_CON.md "Tests"): the programs are the same kinds as
tests/run_vbcc.py (vasm units from ``fixtures/c/hle_unit/u_con*.s`` and
vbcc C programs ``fixtures/c/con_*.c``), staged and started the same way.
They differ in where the keys come from:

``units``   register-level units.  Their input is SCRIPTED: the host fills the
            console's BSS injection queue (``hle_con_q``) and sets
            ``hle_con_src`` = 1, so Read consumes only the queue and an
            empty queue is EOF.  This covers the line discipline (echo,
            backspace, remainder, EOF, overflow, argument checks) but not
            the matrix scan.
``script``  the C programs, with the same scripted queue.  This covers
            vc.lib's stdio over the line discipline.
``matrix``  the same C programs again, with ``hle_con_src`` = 0 (keyboard)
            and an EMPTY queue, so every character goes through the real
            CIA1 matrix scan.  VICE's binary monitor has no command to
            press a key in the matrix (its keyboard feed only fills the
            KERNAL buffer, which our Read never looks at), so the host does
            this through a snapshot.  It dumps a VICE snapshot, sets the
            key's bit in the snapshot's KEYBOARD module (``keyarr`` /
            ``rev_keyarr``: the matrix VICE's CIA1 emulation reads $DC01
            from) and undumps it.  Then it resumes, and waits until the
            scanner has counted the key (``hle_con_nkey``).  Releasing the
            key is the same edit in reverse.  The host then waits until the
            debounced matrix (``hle_con_mat``) is all up again.  The
            program runs asynchronously, from a trampoline in the cassette
            buffer, while this happens.  Each program's screen and exit
            code must match its scripted run exactly.  VICE only; on the
            u64 backend this leg is skipped (reported as SKIP).

``batch``   the batch-driver contract: tests/run_vbcc.py's own
            ``stage_and_run`` (which carries the one W5-K wiring line,
            ``con_stdin(sess)`` = empty scripted stdin) runs con_getchar and
            con_fgets and the unchanged run_vbcc units u_dos / u_dos3, with
            NO input staged by this driver and the console reset to its
            keyboard default first.  Each must see EOF at once.
            ``--vbcc-module FILE`` loads stage_and_run from another copy of
            run_vbcc.py: the teeth leg uses one with the wiring line deleted
            and must go red (a hang caught by the short --timeout).

``scan``    VICE only, the scanner on its own: ``scan/kernal`` cross-checks
            the key map against the KERNAL's decode tables read from the
            emulated ROM ($EB81 / $EBC2); ``scan/debounce`` calls
            ``hle_con_scan`` from a 6502 loop that waits 1, 4 or 16 raster
            lines between samples and checks the commit comes after ~64
            elapsed lines, not after 64 samples.

The matrix leg's con_keys also presses chords: RSHIFT + A, RSHIFT + /,
C= + Q, C= + A, T + H in one debounce window (both typed), and each
modifier alone (must count no key).

Every run has an instruction budget (--budget, default 2 000 000; the
largest program here needs < 100 000) and every harness call a short
timeout (--timeout, default 60 s).  A Read that waits for a key uses no
guest instructions, so the timeout is what bounds a hang there.

Not covered by any leg: real key switches on real hardware (bounce, a
physical debounce time, ghosting under multi-key rollover), because the
hardware test policy for this wave forbids device runs.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from dataclasses import dataclass, field

from host import hle_env, hunk, image
from tests.run_program import BUF_CASE, BUF_RESULT, HALT_OK, RESULT_LEN, \
    RunResult, Session
from tests.run_vbcc import (HALT_REASONS, HEAP_BASE, POISON_BYTE, POISON_LEN,
                            SCREEN, SPACE, assemble, check_screen, compile_c,
                            launch)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SRC_KEYBOARD = 0          # hle_con_src: queue first, then the CIA1 matrix
SRC_SCRIPT = 1            # queue only; an empty queue is EOF
QUEUE_LEN = 256           # hle_con_q ring (u8 head/tail)

EOF_KEY = "\x1c"          # Ctrl-\ (Ctrl + pound on the C64 keyboard)
CTRL_D = "\x04"
BS = "\x08"
DEL = "\x7f"
RET = "\r"

RES = HEAP_BASE           # unit result records (fixtures/c/hle_unit/u_con*.s)
NEG1 = 0xFFFF_FFFF

# --- key map (docs/HLE_CON.md "Key map"; mirror of hle_con.s tables) --------
# Index = column * 8 + row, column = CIA1 port A bit ($DC00, driven low),
# row = port B bit ($DC01, read).  Modifiers are named, not characters.
LSHIFT, RSHIFT, CTRL, CBM = 1 * 8 + 7, 6 * 8 + 4, 7 * 8 + 2, 7 * 8 + 5
_M = None   # no character
KEYMAP_PLAIN = [
    "\x08", "\r", _M, _M, _M, _M, _M, _M,          # DEL RET CRSR-R F7 F1 F3 F5 CRSR-D
    "3", "w", "a", "4", "z", "s", "e", _M,         # ... LSHIFT
    "5", "r", "d", "6", "c", "f", "t", "x",
    "7", "y", "g", "8", "b", "h", "u", "v",
    "9", "i", "j", "0", "m", "k", "o", "n",
    "+", "p", "l", "-", ".", ":", "@", ",",
    "\\", "*", ";", _M, _M, "=", "^", "/",         # pound * ; HOME RSHIFT = up /
    "1", "_", _M, "2", " ", _M, "q", _M,           # 1 left CTRL 2 SPACE C= Q STOP
]
KEYMAP_SHIFT = [
    _M, "\r", _M, _M, _M, _M, _M, _M,
    "#", "W", "A", "$", "Z", "S", "E", _M,
    "%", "R", "D", "&", "C", "F", "T", "X",
    "'", "Y", "G", "(", "B", "H", "U", "V",
    ")", "I", "J", "0", "M", "K", "O", "N",
    "{", "P", "L", "}", ">", "[", "`", "<",
    "|", "*", "]", _M, _M, "=", "~", "?",
    "!", "_", _M, '"', " ", _M, "Q", _M,
]
assert len(KEYMAP_PLAIN) == len(KEYMAP_SHIFT) == 64


def ctrl_code(idx: int) -> str | None:
    """CTRL + key: letters -> 1..26, pound -> $1C (Ctrl-\\ = EOF)."""
    ch = KEYMAP_PLAIN[idx]
    if ch is not None and "a" <= ch <= "z":
        return chr(ord(ch) - 0x60)
    if ch == "\\":
        return EOF_KEY
    return None


def key_for(ch: str) -> tuple[int, tuple[int, ...]]:
    """(matrix index, modifier indices) that types *ch*; prefers the plain
    layer, then shift, then CTRL."""
    if ch in KEYMAP_PLAIN:
        return KEYMAP_PLAIN.index(ch), ()
    if ch in KEYMAP_SHIFT:
        return KEYMAP_SHIFT.index(ch), (LSHIFT,)
    for idx in range(64):
        if ctrl_code(idx) == ch:
            return idx, (CTRL,)
    raise ValueError(f"no key types {ch!r}")


# --- what to run --------------------------------------------------------------

def _rec(d0: int, b0: bytes = b"....", b4: bytes = b"....",
         last: bytes | None = None, regs_ok: int = 1) -> tuple:
    """Expected u_con rd record: D0, buf[0..3], buf[4..7], last 4, flag."""
    word = lambda b: int.from_bytes(b, "big")
    return (d0, word(b0), word(b4), 0 if last is None else word(last), regs_ok)


@dataclass
class ConUnit:
    name: str
    queue: bytes
    records: list[tuple]
    lines: list[str]


def units() -> dict[str, ConUnit]:
    us = [
        ConUnit("u_con1", b"hello world\r\x1cz\rout\r", [
            _rec(5, b"hell", b"o...", b"ello"),
            _rec(5, b" wor", b"l...", b"worl"),
            _rec(2, b"d\n.."),
            _rec(0),                        # EOF key, input still queued
            _rec(0),                        # D3 = 0
            _rec(NEG1),                     # D3 = -1
            _rec(NEG1),                     # D3 = 16 MB
            _rec(2, b"z\n.."),
            _rec(4, b"out\n", b"....", b"out\n"),   # Read(Output())
            _rec(0),                        # queue dry (scripted EOF)
        ], ["hello world", "z", "out"]),
        ConUnit("u_con2", b"ab\x08\x08\x08c\x7fd\r" + b"x" * 130 + b"\r", [
            _rec(2, b"d\n.."),
            _rec(128, b"xxxx", b"xxxx", b"xxx\n"),
            _rec(0),
        ], ["d", "x" * 40, "x" * 40, "x" * 40, "x" * 7]),
        ConUnit("u_con3", b"abcd\x08\x08\x08q\r", [
            _rec(3, b"aq\n."),
            _rec(0),
        ], [""] * 23 + ["-" * 38 + "aq"]),
        ConUnit("u_con4", b"par\x1c\x04q\rtail", [
            _rec(3, b"par."),
            _rec(0),
            _rec(2, b"q\n.."),
            _rec(4, b"tail", b"....", b"tail"),
            _rec(0),
        ], ["parq", "tail"]),
        ConUnit("u_con5", b"a\r\nb\n\r\r\nc\r", [
            _rec(2, b"a\n.."),
            _rec(2, b"b\n.."),
            _rec(1, b"\n..."),
            _rec(1, b"\n..."),
            _rec(2, b"c\n.."),
            _rec(0),
        ], ["a", "b", "", "", "c"]),
    ]
    return {u.name: u for u in us}


@dataclass
class ConProg:
    name: str
    keys: str                 # typed input (RET, BS, EOF_KEY, ...)
    lines: list[str]          # expected screen rows from row 0
    exit_code: int = 0
    matrix: bool = True       # also run through the real key matrix
    nread: int | None = None  # expected console Read calls (hle_con_nread)
    # matrix leg: explicit chords instead of key_for(keys) — list of
    # (matrix indices held together, key-down events the scanner must count)
    chords: list[tuple[tuple[int, ...], int]] | None = None

    def matrix_script(self) -> list[tuple[tuple[int, ...], int]]:
        if self.chords is not None:
            return self.chords
        return [(key_for(ch)[1] + (key_for(ch)[0],), 1) for ch in self.keys]


def _hexrows(data: bytes) -> list[str]:
    return [" ".join(f"{b:02x}" for b in data[i:i + 12])
            for i in range(0, len(data), 12)]


# Every character the key map can produce, typed into con_keys (with
# a DEL that must erase the "#" in front of it).
KEYS_LINE = ("abcdefghijklmnopqrstuvwxyz0123456789"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             " +-.:@,\\*;=^/_!\"#$%&'()<>?[]{}|~`")
KEYS_TYPED = KEYS_LINE + "#\x08"
# Matrix-only chords (fix1, review items 2 and 5), typed after KEYS_LINE:
# RSHIFT + A, RSHIFT + / (RSHIFT shares column 6 with "/": the modifier
# mask must keep it from being the "first new key"), C= + Q (C= shares
# column 7 with Q and sits on a lower row), C= + A, and T + H going down
# in ONE debounce window (rollover: both typed, matrix order T then H).
# The scripted leg types the same characters.
KEYS_EXTRA = "A?qath"


def _k(ch: str) -> int:
    return key_for(ch)[0]


def keys_chords() -> list[tuple[tuple[int, ...], int]]:
    one = lambda ch: (key_for(ch)[1] + (key_for(ch)[0],), 1)
    return ([one(ch) for ch in KEYS_LINE]
            + [((RSHIFT, _k("a")), 1), ((RSHIFT, _k("/")), 1),
               ((CBM, _k("q")), 1), ((CBM, _k("a")), 1),
               ((_k("t"), _k("h")), 2)]
            # a modifier alone is not a keystroke: nothing may be counted
            + [((m,), 0) for m in (LSHIFT, RSHIFT, CTRL, CBM)]
            + [one("#"), one("\x08"), one("\r")])


def programs() -> dict[str, ConProg]:
    kl = KEYS_LINE + KEYS_EXTRA
    ks = (kl + "\n").encode()
    ps = [
        ConProg("con_fgets", "hello\rworld 42\r" + EOF_KEY,
                ["first? hello", "second? world 42", "a=6:hello",
                 "b=9:world 42", "eof=1"]),
        ConProg("con_getchar", "ab\rcde\r" + EOF_KEY,
                ["ab", "cde", "chars=7 lines=2 sum=515", "feof=1"]),
        # ends with Ctrl-D (the EOF alias; the others use Ctrl-\)
        ConProg("con_scanf", "12 30\r-5\r" + CTRL_D,
                ["x y? 12 30", "r=2 sum=42", "-5", "r=1 z=-5", "r=-1"]),
        ConProg("con_bs", BS + BS + "helX" + BS + "lo\r" + "wxyz" + BS * 3
                + "q\r",
                ["edit? hello", "got=6:hello", "." * 38 + "wq", "",
                 "got=3:wq"]),
        ConProg("con_long", "the quick brown fox jumps\rok\r" + EOF_KEY,
                ["the quick brown fox jumps", "1=26:the quick brown fox jumps",
                 "ok", "2=3:ok", "eof=1"], nread=11),
        ConProg("con_echo", "secret words\rbob\r",
                ["secret words", "Name: bob", "Hi bob"], exit_code=13),
        ConProg("con_keys", KEYS_LINE + KEYS_EXTRA + "#\x08\r",
                [kl[:40], kl[40:80], kl[80:], f"n={len(ks)}"] + _hexrows(ks),
                chords=keys_chords()),
        # a program that never reads stdin must not wait for a key
        ConProg("hello", "", ["Hello from vbcc!", "2 + 40 = 42"]),
    ]
    return {p.name: p for p in ps}


# --- console seam -----------------------------------------------------------

def label(sess: Session, name: str) -> int | None:
    from tests.harness_util import lookup_label
    try:
        return lookup_label(sess.labels, name)
    except KeyError:
        return None


SEAM_LABELS = ("hle_con_init", "hle_con_src", "hle_con_q", "hle_con_qh",
               "hle_con_qt", "hle_con_nkey", "hle_con_mat", "hle_con_nread")


def has_seam(sess: Session) -> bool:
    return all(label(sess, n) is not None for n in SEAM_LABELS)


def con_stdin(sess: Session, data: bytes = b"", src: int = SRC_SCRIPT) -> bool:
    """Reset the console (hle_con_init) and give it *data* as input.

    SRC_SCRIPT: Read consumes only *data*; when it is used up, Read returns
    0 (EOF) — a batch run's stdin, like ``prog </dev/null``.  SRC_KEYBOARD:
    *data* first, then the CIA1 matrix.  Returns False (and does nothing)
    when the PRG has no console seam (the pre-W5-K stub)."""
    from c64_test_harness import write_bytes
    if not has_seam(sess):
        return False
    if len(data) >= QUEUE_LEN:
        raise ValueError(f"queue holds at most {QUEUE_LEN - 1} bytes")
    sess._call("hle_con_init")
    t = sess.target.transport
    write_bytes(t, label(sess, "hle_con_q"), data)
    write_bytes(t, label(sess, "hle_con_qh"), bytes([0]))
    write_bytes(t, label(sess, "hle_con_qt"), bytes([len(data)]))
    write_bytes(t, label(sess, "hle_con_src"), bytes([src]))
    return True


def read_nread(sess: Session) -> int | None:
    from c64_test_harness import read_bytes
    a = label(sess, "hle_con_nread")
    if a is None:
        return None
    return int.from_bytes(read_bytes(sess.target.transport, a, 2), "little")


def guest_long(sess: Session, addr: int) -> int:
    return int.from_bytes(bytes(sess.peek_guest(addr + i) for i in range(4)),
                          "big")


# --- matrix key driver (VICE snapshot KEYBOARD module) ------------------------

class MatrixKeys:
    """Press/release keys in VICE's emulated keyboard matrix.

    VICE snapshot module "KEYBOARD" (v1.1) holds ``int keyarr[16]`` (per
    port-A line: the port-B bits whose keys are down) and ``int
    rev_keyarr[8]`` (the reverse map), 96 bytes LE.  CIA1 port reads are
    computed from exactly these arrays, so editing them in a snapshot and
    undumping is a real matrix key press as far as $DC00/$DC01 are
    concerned (verified: a column scan reads $FB in column 1 while "A" is
    down, $FF after release)."""

    def __init__(self, transport, scratch: str):
        self.t = transport
        self.path = os.path.join(scratch, "matrix.vsf")
        self.ops = 0
        self.seconds = 0.0

    def set_keys(self, idxs: tuple[int, ...]) -> None:
        t0 = time.monotonic()
        self.t.dump_snapshot(self.path, save_roms=False, save_disks=False)
        with open(self.path, "rb") as f:
            d = bytearray(f.read())
        at = d.find(b"KEYBOARD\0")
        if at < 0:
            raise RuntimeError("VICE snapshot has no KEYBOARD module")
        body = at + 16 + 2 + 4          # name[16], major, minor, u32 size
        if (d[at + 16], d[at + 17]) != (1, 1) or \
                struct.unpack_from("<I", d, at + 18)[0] != 22 + 96:
            raise RuntimeError("unexpected KEYBOARD module version/size")
        arr = [0] * 24
        for idx in idxs:
            col, row = divmod(idx, 8)
            arr[col] |= 1 << row
            arr[16 + row] |= 1 << col
        struct.pack_into("<24I", d, body, *arr)
        with open(self.path, "wb") as f:
            f.write(d)
        self.t.undump_snapshot(self.path)
        self.ops += 1
        self.seconds += time.monotonic() - t0


def _poll(sess: Session, what: str, pred, timeout: float) -> bool:
    """Resume the CPU and poll *pred()* (each read pauses the CPU)."""
    t = sess.target.transport
    deadline = time.monotonic() + timeout
    while True:
        if pred():
            return True
        if time.monotonic() > deadline:
            print(f"  timeout waiting for {what}")
            return False
        t.resume()
        time.sleep(0.02)


TRAMP = 0x0350            # cassette buffer: jsr test_run_program / flag
TRAMP_DONE = 0x035F
DONE_MAGIC = 0x5A


def start_program(sess: Session, pc: int, a7: int, budget: int) -> None:
    """Enter test_run_program from a trampoline and let it run (the
    harness's jsr() would block until it returns; key presses must happen
    while it runs).  The trampoline sets TRAMP_DONE and spins."""
    from c64_test_harness import write_bytes
    from tests.harness_util import lookup_label
    t = sess.target.transport
    trp = lookup_label(sess.labels, "test_run_program")
    spin = TRAMP + 8
    code = bytes([0x20, trp & 0xFF, trp >> 8,             # jsr test_run_program
                  0xA9, DONE_MAGIC,                        # lda #$5A
                  0x8D, TRAMP_DONE & 0xFF, TRAMP_DONE >> 8,  # sta DONE
                  0x4C, spin & 0xFF, spin >> 8])           # spin: jmp spin
    write_bytes(t, BUF_CASE, struct.pack("<III", pc, a7, budget))
    write_bytes(t, TRAMP_DONE, bytes([0]))
    write_bytes(t, TRAMP, code)
    t.set_registers({"PC": TRAMP})
    t.resume()


def program_done(sess: Session) -> bool:
    from c64_test_harness import read_bytes
    return read_bytes(sess.target.transport, TRAMP_DONE, 1)[0] == DONE_MAGIC


def type_keys(sess: Session, mk: MatrixKeys,
              chords: list[tuple[tuple[int, ...], int]],
              timeout: float) -> str | None:
    """Press each chord through the matrix; None on success, else the reason.

    A chord with n > 0 must make the scanner count exactly n key-down
    events.  A chord with n = 0 (a modifier alone) must be committed to the
    debounced matrix (hle_con_mat shows it) without any event counted."""
    from c64_test_harness import read_bytes
    t = sess.target.transport
    nkey_at = label(sess, "hle_con_nkey")
    mat_at = label(sess, "hle_con_mat")
    if nkey_at is None or mat_at is None:
        return "PRG has no matrix scanner (hle_con_nkey/hle_con_mat missing)"
    all_up = lambda: read_bytes(t, mat_at, 8) == bytes(8)
    for i, (idxs, n) in enumerate(chords):
        what = f"chord {i} {idxs}"
        if not _poll(sess, "debounced matrix all up", all_up, timeout):
            return f"{what}: scanner never saw all keys up"
        if program_done(sess):
            return f"{what}: program already finished"
        n0 = read_bytes(t, nkey_at, 1)[0]
        mk.set_keys(idxs)
        if n:
            got = _poll(sess, what, lambda: (read_bytes(t, nkey_at, 1)[0]
                                             - n0) & 0xFF >= n, timeout)
            cnt = (read_bytes(t, nkey_at, 1)[0] - n0) & 0xFF
            mk.set_keys(())
            if not got or cnt != n:
                return f"{what}: scanner counted {cnt} key(s), want {n}"
        else:
            want = bytearray(8)
            for idx in idxs:
                want[idx // 8] |= 1 << (idx % 8)
            got = _poll(sess, f"{what} committed",
                        lambda: read_bytes(t, mat_at, 8) == bytes(want),
                        timeout)
            cnt = (read_bytes(t, nkey_at, 1)[0] - n0) & 0xFF
            mk.set_keys(())
            if not got:
                return f"{what}: never committed to hle_con_mat"
            if cnt:
                return f"{what}: modifier alone counted as {cnt} key(s)"
    t.resume()
    return None


# --- runs ---------------------------------------------------------------------

def stage(sess: Session, tag: str, blob: bytes) -> tuple[int, dict]:
    """Stage a program the way tests/run_vbcc.py does (blank screen, env,
    $A5-poisoned heap, fresh WS-B2 handle pool) WITHOUT touching the
    console: the caller has already set its input with con_stdin().
    (run_vbcc.stage_and_run resets stdin to empty itself.)"""
    from c64_test_harness import write_bytes
    from host.image import Chunk
    exe = hunk.parse(blob)
    chunks, manifest = image.build_image(exe)
    env, pc = launch(manifest)
    print(f"[{tag}] {len(blob)}-byte hunk file, entry=${manifest['entry']:06X}")
    write_bytes(sess.target.transport, SCREEN, bytes([SPACE]) * 1000)
    sess.stage(chunks + env + [Chunk(HEAP_BASE,
                                     bytes([POISON_BYTE]) * POISON_LEN)])
    if label(sess, "hle_dos_init") is not None:
        sess._call("hle_dos_init")
    return pc, manifest


BUDGET = 2_000_000        # main() sets it from --budget


def run_sync(sess: Session, tag: str, blob: bytes):
    sess.con_tag = tag
    pc, manifest = stage(sess, tag, blob)
    res = sess.run_program(pc, manifest["stack_top"], BUDGET)
    res.print_state()
    why = HALT_REASONS.get(res.reason, f"${res.reason:02X}")
    if res.timeout:
        why = "TIMEOUT"
    elif not res.halt:
        why = "not halted"
    return res, manifest, why


def run_unit(sess: Session, u: ConUnit, blob: bytes) -> None:
    tag = f"units/{u.name}"
    seam = con_stdin(sess, u.queue, SRC_SCRIPT)
    sess.check(f"{tag}: console seam present", seam,
               "hle_con_* labels missing (stub console)")
    res, manifest, why = run_sync(sess, tag, blob)
    sess.check(f"{tag}: halted HALT_OK",
               res.halt != 0 and res.timeout == 0 and res.reason == HALT_OK,
               why)
    sess.check(f"{tag}: A7 back at stack_top+4",
               res.regs["A7"] == manifest["stack_top"] + 4,
               f"A7={res.regs['A7']:08X}")
    names = ("D0", "buf[0..3]", "buf[4..7]", "last4", "D1-D3 kept")
    for n, want in enumerate(u.records):
        got = tuple(guest_long(sess, RES + 20 * n + 4 * k) for k in range(5))
        for k, (w, g) in enumerate(zip(want, got)):
            sess.check(f"{tag}: read {n + 1} {names[k]}={w:08X}", w == g,
                       f"got {g:08X}")
    check_screen(sess, tag, u.lines)


def check_prog(sess: Session, tag: str, p: ConProg, res: RunResult,
               manifest: dict, why: str) -> None:
    halted = res.halt != 0 and res.timeout == 0
    sess.check(f"{tag}: halted HALT_OK", halted and res.reason == HALT_OK, why)
    sess.check(f"{tag}: exit code {p.exit_code}",
               res.regs["D0"] == p.exit_code, f"D0={res.regs['D0']:08X}")
    sess.check(f"{tag}: A7 back at stack_top+4",
               res.regs["A7"] == manifest["stack_top"] + 4,
               f"A7={res.regs['A7']:08X}")
    check_screen(sess, tag, p.lines)
    if p.nread is not None:
        got = read_nread(sess)
        sess.check(f"{tag}: {p.nread} console Reads", got == p.nread,
                   f"hle_con_nread={got}")


def run_script(sess: Session, p: ConProg, blob: bytes) -> None:
    tag = f"script/{p.name}"
    seam = con_stdin(sess, p.keys.encode("latin-1"), SRC_SCRIPT)
    sess.check(f"{tag}: console seam present", seam,
               "hle_con_* labels missing (stub console)")
    res, manifest, why = run_sync(sess, tag, blob)
    check_prog(sess, tag, p, res, manifest, why)


def run_matrix(sess: Session, p: ConProg, blob: bytes, mk: MatrixKeys,
               timeout: float) -> bool:
    """Returns False when the machine is left inside a hung run."""
    from c64_test_harness import read_bytes
    tag = f"matrix/{p.name}"
    seam = con_stdin(sess, b"", SRC_KEYBOARD)
    sess.check(f"{tag}: console seam present", seam,
               "hle_con_* labels missing (stub console)")
    pc, manifest = stage(sess, tag, blob)
    print(f"  {len(p.matrix_script())} chord(s) through the matrix")
    t = sess.target.transport
    t0 = time.monotonic()
    ops0, sec0 = mk.ops, mk.seconds
    start_program(sess, pc, manifest["stack_top"], BUDGET)
    script = p.matrix_script()
    err = type_keys(sess, mk, script, timeout) if script else None
    sess.check(f"{tag}: every key counted by the matrix scanner", err is None,
               err or "")
    done = _poll(sess, "program exit", lambda: program_done(sess), timeout)
    sess.check(f"{tag}: program finished", done, "still running (waiting "
               "for input?)")
    if not done:
        return False
    hdr = read_bytes(t, BUF_RESULT, RESULT_LEN)
    res = RunResult.from_bytes(hdr)
    res.print_state()
    print(f"  wall {time.monotonic() - t0:.1f}s, {mk.ops - ops0} snapshot "
          f"edits ({mk.seconds - sec0:.1f}s)")
    why = HALT_REASONS.get(res.reason, f"${res.reason:02X}")
    if res.timeout:
        why = "TIMEOUT"
    elif not res.halt:
        why = "not halted"
    check_prog(sess, tag, p, res, manifest, why)
    return True


# --- scan leg: the matrix scanner itself, called directly (VICE) ------------

SCRATCH = 0xC000          # buf_case: free between runs
SCRATCH_OUT = 0xC100


def _jsr_scratch(sess: Session, code: bytes) -> None:
    from c64_test_harness import jsr, write_bytes
    write_bytes(sess.target.transport, SCRATCH, code)
    jsr(sess.target.transport, SCRATCH, timeout=sess.timeout)


def kernal_expect(k: int, shifted: bool) -> str | None | bool:
    """What the W5-K map must give for a key whose KERNAL decode is PETSCII
    *k*: a character, None (no character), or True (a documented W5-K
    extension where the KERNAL has a graphics character)."""
    if 0x41 <= k <= 0x5A:
        return chr(k + 0x20)                  # unshifted letter -> lowercase
    if 0xC1 <= k <= 0xDA:
        return chr(k - 0x80)                  # shifted letter -> uppercase
    if 0x20 <= k <= 0x40 or 0x5B <= k <= 0x5F:
        return chr(k)                         # digits, punctuation, @ [ ] \ ^ _
    if k in (0x0D, 0x8D):
        return "\r"
    if k == 0x14:
        return "\x08"                         # DEL -> BS
    if k == 0xA0:
        return " "                            # shifted SPACE
    if k == 0xFF or k < 0x20 or 0x80 <= k < 0xA0:
        return None                           # modifier / cursor / F-key / INST
    return True                               # PETSCII graphics


# Documented W5-K characters on keys where the KERNAL types graphics
# (docs/HLE_CON.md "Key map").
KERNAL_GRAPHICS_OK = {"{", "}", "`", "|", "~", "*", "=", "_", None}


def run_kernal_map(sess: Session) -> None:
    """Cross-check hle_con's key map against the C64 KERNAL's own decode
    tables ($EB81 unshifted, $EBC2 shifted, 64 entries each, in the same
    column * 8 + row order), read from the emulated ROM."""
    from c64_test_harness import read_bytes
    sess.con_tag = "scan/kernal"
    # lda $01 / pha / lda #$37 / sta $01 / ldx #0 / lda $EB81,x /
    # sta SCRATCH_OUT,x / inx / cpx #130 / bne / pla / sta $01 / rts
    o = SCRATCH_OUT
    code = bytes([0xA5, 0x01, 0x48, 0xA9, 0x37, 0x85, 0x01, 0xA2, 0x00,
                  0xBD, 0x81, 0xEB, 0x9D, o & 0xFF, o >> 8, 0xE8, 0xE0, 130,
                  0xD0, 0xF5, 0x68, 0x85, 0x01, 0x60])
    _jsr_scratch(sess, code)
    rom = read_bytes(sess.target.transport, SCRATCH_OUT, 130)
    for layer, (tab, ours) in {"plain": (rom[0:64], KEYMAP_PLAIN),
                               "shift": (rom[65:129], KEYMAP_SHIFT)}.items():
        sess.check(f"scan/kernal: {layer} table terminator $FF",
                   rom[64 if layer == "plain" else 129] == 0xFF,
                   f"{rom[64]:02X}/{rom[129]:02X}")
        bad = []
        for idx in range(64):
            want = kernal_expect(tab[idx], layer == "shift")
            if want is True:
                if ours[idx] not in KERNAL_GRAPHICS_OK:
                    bad.append(f"{idx}: KERNAL ${tab[idx]:02X} ours {ours[idx]!r}")
            elif ours[idx] != want:
                bad.append(f"{idx}: KERNAL ${tab[idx]:02X} want {want!r} "
                           f"ours {ours[idx]!r}")
        sess.check(f"scan/kernal: {layer} map agrees with the KERNAL ROM "
                   f"(64 entries)", not bad, "; ".join(bad))


DEBOUNCE_WAITS = (1, 4, 16)   # raster lines between two scanner samples
DEBOUNCE_GIVEUP = 200         # samples: never committed (reported, no hang)


def run_debounce(sess: Session) -> None:
    """The debounce window is CON_STABLE (64) elapsed raster lines whatever
    the poll rate.  A 6502 loop calls hle_con_scan, then waits N raster-line
    changes, until the scanner commits the all-up matrix that follows
    hle_con_init's "all down" start; it counts the samples.  Sample 1 sees
    the change; commit needs >= 64 more lines."""
    from c64_test_harness import read_bytes
    scan = label(sess, "hle_con_scan")
    mat = label(sess, "hle_con_mat")
    if scan is None or mat is None:
        sess.check("scan/debounce: hle_con_scan exported", False, "missing")
        return
    cnt = SCRATCH_OUT
    sess.con_tag = "scan/debounce"
    for n in DEBOUNCE_WAITS:
        sess._call("hle_con_init")
        body = bytes([
            0xA9, 0x00, 0x8D, cnt & 0xFF, cnt >> 8,          # lda #0 / sta cnt
            0x20, scan & 0xFF, scan >> 8,                    # L: jsr scan
            0xEE, cnt & 0xFF, cnt >> 8,                      # inc cnt
            0xAD, cnt & 0xFF, cnt >> 8,                      # lda cnt
            0xC9, DEBOUNCE_GIVEUP,                           # cmp #GIVEUP
            0xF0, 0x15,                                      # beq done
            0xAD, mat & 0xFF, mat >> 8,                      # lda mat
            0xF0, 0x10,                                      # beq done
            0xA2, n,                                         # ldx #n
            0xAD, 0x12, 0xD0,                                # W: lda $D012
            0xCD, 0x12, 0xD0,                                # V: cmp $D012
            0xF0, 0xFB,                                      # beq V
            0xCA, 0xD0, 0xF5,                                # dex / bne W
            0x4C, (SCRATCH + 5) & 0xFF, (SCRATCH + 5) >> 8,  # jmp L
            0x60])                                           # done: rts
        _jsr_scratch(sess, body)
        got = read_bytes(sess.target.transport, cnt, 1)[0]
        # Sample 1 sees the change; the commit needs >= 64 more elapsed
        # lines.  Each later sample adds n (the wait) + S lines, S = the
        # sample itself: measured 18 lines at 1 MHz in VICE (10 samples =
        # 182 lines; the row-settle checks cost most of it), bounded here
        # by 0 < S <= 25.  A window that crosses the raster counter's frame
        # wrap counts that step as 1 line (conservative): +1 sample.
        #   at least 1 + ceil(64 / (n + 25)) samples (window not short);
        #   at most  2 + ceil(64 / n)        samples (time, not samples:
        #   counting one per sample would need 65 whatever n is).
        lo = 1 + -(-64 // (n + 25))
        hi = 2 + -(-64 // n)
        sess.check(f"scan/debounce: commit after {lo}..{hi} samples at "
                   f"{n} line(s) per poll", lo <= got <= hi, f"{got} samples")
        print(f"  [scan/debounce] {n:2d} line(s) per poll: {got} samples")


BATCH_PROGS = {   # run through run_vbcc.stage_and_run with no input staged
    "con_getchar": ConProg("con_getchar", "", ["chars=0 lines=0 sum=0",
                                               "feof=1"]),
    "con_fgets": ConProg("con_fgets", "", ["first? no a"], exit_code=1),
}
BATCH_UNITS = ("u_dos", "u_dos3")


def load_vbcc(path: str | None):
    """tests.run_vbcc, or the copy at *path* (teeth: wiring line removed)."""
    if path is None:
        from tests import run_vbcc
        return run_vbcc
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_vbcc_variant", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # dataclasses look the module up
    spec.loader.exec_module(mod)
    from tests import run_vbcc          # fixtures live in the real tree
    mod.FIXTURE_DIR, mod.UNIT_DIR = run_vbcc.FIXTURE_DIR, run_vbcc.UNIT_DIR
    return mod


def run_batch(sess: Session, vb, out: str) -> None:
    """Batch programs through run_vbcc's own staging: EOF, not a hang."""
    for name in list(BATCH_PROGS) + list(BATCH_UNITS):
        tag = f"batch/{name}"
        sess.con_tag = tag
        if has_seam(sess):
            sess._call("hle_con_init")   # keyboard source, empty queue
        blob = (vb.assemble(name, out) if name in BATCH_UNITS
                else vb.compile_c(name, out))
        n0 = len(sess.checks) + len(sess.failures)
        if name in BATCH_UNITS:
            vb.run_unit(sess, vb.units()[name], blob, BUDGET)
        else:
            p = BATCH_PROGS[name]
            res, manifest, why = vb.stage_and_run(sess, tag, blob, BUDGET,
                                                  poison=True)
            check_prog(sess, tag, p, res, manifest, why)
        print(f"  [{tag}] {len(sess.checks) + len(sess.failures) - n0} "
              f"check(s)")
        print()


def main(argv: list[str] | None = None) -> int:
    global BUDGET
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_con",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True, help="runner PRG")
    ap.add_argument("--labels", help="ld65 -Ln label file (default: PRG .lbl)")
    ap.add_argument("--only", help="comma-separated unit/program names")
    ap.add_argument("--legs", default="units,script,matrix,batch,scan",
                    help="comma-separated subset of units,script,matrix,"
                    "batch,scan")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="per-call / per-wait timeout in seconds (default 60)")
    ap.add_argument("--budget", type=int, default=BUDGET, metavar="N",
                    help=f"guest instructions per run (default {BUDGET})")
    ap.add_argument("--vbcc-module", metavar="FILE",
                    help="batch leg: load stage_and_run from this copy of "
                    "tests/run_vbcc.py (teeth: wiring line removed)")
    ap.add_argument("--backend", default="auto", choices=["auto", "vice", "u64"])
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "build", "vbcc"),
                    help="build dir for compiled programs (default build/vbcc)")
    args = ap.parse_args(argv)
    BUDGET = args.budget

    prg = os.path.abspath(args.prg)
    labels_path = (os.path.abspath(args.labels) if args.labels
                   else os.path.splitext(prg)[0] + ".lbl")
    for path in (prg, labels_path):
        if not os.path.exists(path):
            print(f"error: not found: {path}", file=sys.stderr)
            return 2
    legs = args.legs.split(",")
    utable, ptable = units(), programs()
    only = set(args.only.split(",")) if args.only else None
    for n in only or ():
        if n not in utable and n not in ptable:
            print(f"error: unknown name {n!r}", file=sys.stderr)
            return 2
    unames = [n for n in utable if only is None or n in only]
    pnames = [n for n in ptable if only is None or n in only]

    os.makedirs(args.out, exist_ok=True)
    blobs = {}
    if "units" in legs:
        blobs.update({n: assemble(n, args.out) for n in unames})
    if "script" in legs or "matrix" in legs:
        blobs.update({n: compile_c(n, args.out) for n in pnames})
    print(f"built {len(blobs)} program(s) into {args.out}")

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from c64_test_harness.transport import TransportError
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(labels_path)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    skips: list[str] = []
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=args.timeout)
            try:
                if "units" in legs:
                    for n in unames:
                        run_unit(sess, utable[n], blobs[n])
                        print()
                if "script" in legs:
                    for n in pnames:
                        run_script(sess, ptable[n], blobs[n])
                        print()
                if "batch" in legs and only is None:
                    run_batch(sess, load_vbcc(args.vbcc_module), args.out)
                if "scan" in legs and only is None:
                    if target.backend != "vice":
                        skips.append("scan leg: VICE only (jsr)")
                    else:
                        run_kernal_map(sess)
                        run_debounce(sess)
            except (TimeoutError, TransportError) as e:
                # a Read that never returns (e.g. EOF never signalled):
                # the machine is inside the hung run, nothing after it is
                # trustworthy
                sess.check(f"{getattr(sess, 'con_tag', '?')}: run finished "
                           "(no hang)", False, str(e))
                legs = []
                skips.append("remaining programs: machine hung in a run")
            if "matrix" in legs:
                if target.backend != "vice":
                    skips.append("matrix leg: VICE only (snapshot key "
                                  "presses); no key presses on hardware")
                else:
                    scratch = os.path.join(os.path.dirname(prg), "con-matrix")
                    os.makedirs(scratch, exist_ok=True)
                    mk = MatrixKeys(target.transport, scratch)
                    for n in pnames:
                        if not ptable[n].matrix:
                            continue
                        if not run_matrix(sess, ptable[n], blobs[n], mk,
                                          args.timeout):
                            skips.append(f"matrix leg aborted after {n}: "
                                         "the machine is inside a hung run")
                            sess.check("matrix: leg completed", False, n)
                            break
                        print()
        finally:
            mgr.release(target)

    for s in skips:
        print(f"SKIP  {s}")
    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    for f in sess.failures:
        print(f"FAIL  {f}")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
