"""W6-K keyboard hardware validation: fixtures/c/kbd_echo.c, end to end.

Usage:
    # automated, VICE (keys pressed in the emulated matrix)
    python3 -m tests.kbd_echo --prg build/<pkg>/runner.prg \\
        [--legs guided,teeth,manner,patch]

    # automated smoke, no human (input from the injection queue): proves
    # load / run / stream / control patch round trip / restore on the device
    U64_HOST=<ip> python3 -m tests.kbd_echo --prg ... --backend u64 \\
        --smoke --mhz 1,48 --out-dir DIR

    # MANUAL, a human at the C64 keyboard (docs/HLE_CON.md
    # "Hardware validation"): the full plan, positive controls included
    U64_HOST=<ip> python3 -m tests.kbd_echo --prg ... --backend u64 \\
        --out-dir DIR

The program (``fixtures/c/kbd_echo.c``, vbcc through the HLE console) walks
the operator through nine steps, each one line typed at the console
(``dos.library/Read``: the whole ``hle_con.s`` path).  For every character
Read delivered it prints the character, its hex code and the position the
KEY MAP gives for it, then PASS or FAIL.  The program cannot see the
scanner (C64 BSS is outside guest memory), so the SCANNED positions come
only from this driver's stream: it polls ``hle_con_nkey``, ``hle_con_mat``
and ``hle_con_idle`` and prints every key that went down or up with the
column/row it was scanned at, the rows a port-1 stick holds low, and new
screen rows.  A key that goes down in column c while column c+1 on the same
row is held is flagged ``COLUMN BLEED?`` (the scan runs 7 -> 0).

How a step was performed matters, not only the characters (a queue-fed
smoke types every answer with no key pressed at all), so every stream
sample is tagged with the step on screen and :func:`judge_step` checks the
MANNER: Q (and ; and /) held >= 1.5 s, three keys down at once in the
rollover, RSHIFT / C= down when their keys go down and LEFT SHIFT never,
the port-1 stick's row seen held low while H O S E go down.  It also looks
for DEFECTS in the stream: another key down together with a held key, a
column bleed in a hold step, a key on the stick's row reading down.  A step
the stream cannot decide (poll period unknown, a stall, or a poll too
sparse for a tapped step) is NOT JUDGEABLE, never operator error.

Verdicts (:func:`judge_run`):
  fixed build   PASS only when every step is P, performed as asked, with no
                defect; FAIL on any F or defect; else INCOMPLETE (a SKIP is
                never a pass), or NOT JUDGEABLE when all that is missing is
                a step the stream could not decide.
  control build (a runner with the fix taken out, ``CONTROLS``): DEFECT
                SHOWN only when the stream shows that control's own defect
                SIGNATURE (nosettle: the c-1 phantom going down in the same
                sample as the held key; nojoymask: keys on the stick's row
                reading down); INCONCLUSIVE when the target steps were
                performed, all P and clean; anything else (an F, S or stray
                key without the signature, an unperformed step) is
                INCOMPLETE: operator error, redo the leg.
  session       VALIDATED only when the fixed build PASSes at 48 and at 1 MHz
                AND each control shows its defect at 48 MHz.  A clean
                control makes it INCONCLUSIVE: the defect does not occur on
                this device, so the fix is harmless but not proven.

Legs:

``guided`` (VICE)  presses every step's keys in VICE's matrix (snapshot
    KEYBOARD module) and checks count and SCANNED position of every key
    event, no repeat on a held key, modifiers alone count nothing, and each
    step's screen rows.  A port-2 stick (the only port a VICE snapshot can
    drive, see :class:`KbdMatrix`) is held while a key is down: that sample
    must be skipped.  The judge must find steps 1-4 performed and clean,
    and steps 5-9 NOT performed (no port-1 stick in VICE): the run is
    INCOMPLETE, never PASS.
``teeth`` (VICE)  a real phantom's shape (Q and the UP-ARROW key c6r6 in
    one commit): ``^q``, ``STEP 1 FAIL``, the bleed flagged, the judge: FAIL
    as a fixed build, DEFECT SHOWN as a control; an empty line is SKIP.
``manner`` (VICE)  the right characters typed the wrong way (Q tapped, Z I
    P one after another, LEFT SHIFT in step 4): the program says P, the
    judge must say not performed, run INCOMPLETE.
``patch`` (VICE)  the control patches applied to the running runner and
    removed again (readback), and the ``nosettle`` patch observably stops
    the settle routine.
``smoke`` (``--smoke``, VICE or U64)  no key presses: the idle scan stays
    silent, then the queue feeds every answer, every step PASSes, and the
    judge must NOT call it a validation (nothing was pressed); plus the
    control patch round trip.
``manual`` (U64 without ``--smoke``)  the plan (``--runs``, default
    ``MANUAL_PLAN``): the fixed build at 48 MHz, the ``nosettle`` control
    at 48 MHz (steps 1-2), the ``nojoymask`` control at 48 MHz (steps 5-9),
    the fixed build at 1 MHz (negative control).  Controls are the same
    runner with the fix NOP-ed out in C64 RAM (:func:`find_sites`), never a
    second upload; the program's banner and this driver both say that the
    control is SUPPOSED to show the defect.

U64 safety: a firmware gate first (DeviceLock + one bodyless GET
/v1/info; only the resting build runs; the smoke re-queues, the manual mode
fails fast and names the lock holder), then an operator confirmation on
the host before anything is written (manual), then ONE DeviceLock hold
with the exact full-config snapshot/restore of
``tests/bench/u64_session.u64_hold`` joined to the gate's lock, the
firmware re-checked, every setting the run depends on set explicitly, the
runner uploaded once, no storage writes (``dosio_want_uci`` zeroed and
RAMDISK proven; the program opens no files).  Session log:
``--out-dir``/session.log, written as it goes, one session per directory
(the default --out-dir is new per session; an existing session.log is
refused).  ``--replay LOG...`` re-judges logs offline, each session on its
own; a log's verdict is its worst session's.

Exit code 0 = all checks passed (manual: session VALIDATED).
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys
import time
from dataclasses import dataclass, field

from tests.run_con import (CBM, CTRL, LSHIFT, RSHIFT, SRC_KEYBOARD,
                           KEYMAP_PLAIN, KEYMAP_SHIFT, key_for)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROG = "kbd_echo"

MODS = (LSHIFT, RSHIFT, CTRL, CBM)
JOY2_UP = 0x01            # control port 2 "up": pulls port A bit 0 (column 0)
HOLD_MIN = 1.5            # s a "hold" step's key must stay down (manner)

# --- the program's steps (mirror of fixtures/c/kbd_echo.c) --------------------


@dataclass
class Step:
    title: str
    how: str                   # what the operator does (terminal checklist)
    want: str                  # the line Read must deliver (no "\n")
    fails_as: str              # what the defect looks like on screen
    kind: str                  # hold / rollover / mods / stick
    keys: str = ""             # hold: the keys held
    row: int | None = None     # stick: the port-B row it pulls low


def _stick(n: int, name: str, row: int, fails_as: str) -> Step:
    return Step(f"PORT-1 {name}",
                f"hold the stick in CONTROL PORT 1 {name.split()[-1]} and "
                "type HOSE; release the stick; RETURN", "hose", fails_as,
                "stick", row=row)


STEPS = [
    Step("HOLD Q", "tap LEFT SHIFT once and release it (wakes the "
         "keyboard), then hold Q to a count of 10 (5 s or more), release "
         "it, press RETURN", "q",
         "^q: the UP-ARROW glyph ($5E, c6r6, read in the same sample as Q) "
         "then q; or nothing (the phantom keeps restarting the debounce)",
         "hold", keys="q"),
    Step("HOLD ; AND /",
         "hold ; to a count of 10, release; hold / to a count of 10, "
         "release; RETURN", ";/",
         "l; (c5r2, bleed from ;) or ,/ (c5r7, bleed from /)", "hold",
         keys=";/"),
    Step("ROLLOVER",
         "press Z and keep it down, add I, add P (all three held), "
         "release all, RETURN", "zip",
         "a missing or doubled letter, or a letter nobody pressed",
         "rollover"),
    Step("RSHIFT, C=",
         "hold RIGHT SHIFT: type A and /, release; hold C=: type Q, "
         "release; RETURN (do not touch LEFT SHIFT)", "A?q",
         "a/ (RSHIFT not decoded), or an extra character from C=", "mods"),
    _stick(5, "STICK UP", 0,
           "1 3 5 7 9 + \\ appear (row 0), DEL rubs characters out"),
    _stick(6, "STICK DOWN", 1,
           "the step ends at once with an empty line (RETURN is on row 1): "
           "SKIP, then w r y i p * _ land in the next line"),
    _stick(7, "STICK LEFT", 2,
           "CTRL reads as held (row 2): letters turn into control codes, "
           "CTRL+D ends the program, CTRL+J ends the line; a d g j l ;"),
    _stick(8, "STICK RIGHT", 3, "4 6 8 0 - 2 appear (row 3)"),
    _stick(9, "FIRE", 4,
           "RSHIFT reads as held (row 4): HOSE comes out upper case, with "
           "Z C B M > and spaces"),
]
NSTEP = len(STEPS)
FREE_LINES = ("ok",)       # typed in free mode by the automated legs


def key_label(ch: str) -> str:
    """Matrix position the key map gives for *ch* (mirror of kbd_echo.c
    key_label): plain layer first, then SHIFT, then CTRL + letter."""
    if ch in KEYMAP_PLAIN:
        i = KEYMAP_PLAIN.index(ch)
        return f"c{i // 8}r{i % 8}"
    if ch in KEYMAP_SHIFT:
        i = KEYMAP_SHIFT.index(ch)
        return f"c{i // 8}r{i % 8}+SH"
    if 1 <= ord(ch) <= 26:
        i = KEYMAP_PLAIN.index(chr(ord(ch) + 0x60))
        return f"c{i // 8}r{i % 8}+CTRL"
    return "??"


def fmt_char(ch: str) -> str:
    """The row kbd_echo.c prints for one delivered character."""
    c = ord(ch)
    shown = ch if 0x20 <= c < 0x7F else "."
    return f" '{shown}' ${c:02x} {key_label(ch)}"


def step_rows(n: int, got: str, verdict: str) -> list[str]:
    """Screen rows of one finished step, from the echo to the verdict."""
    rows = [f"> {got}"] + [fmt_char(ch) for ch in got]
    if verdict == "FAIL":
        return rows + [f'STEP {n} FAIL, want "{STEPS[n - 1].want}"']
    return rows + [f"STEP {n} {verdict}"]


def result_rows(verdicts: str) -> list[str]:
    return [f"STEPS {verdicts}",
            f"RESULT pass={verdicts.count('P')} fail={verdicts.count('F')} "
            f"skip={verdicts.count('S')}"]


# --- key names, events --------------------------------------------------------

KEY_NAMES = [
    "DEL", "RETURN", "CRSR-R", "F7", "F1", "F3", "F5", "CRSR-D",
    "3", "W", "A", "4", "Z", "S", "E", "LSHIFT",
    "5", "R", "D", "6", "C", "F", "T", "X",
    "7", "Y", "G", "8", "B", "H", "U", "V",
    "9", "I", "J", "0", "M", "K", "O", "N",
    "+", "P", "L", "-", ".", ":", "@", ",",
    "POUND", "*", ";", "HOME", "RSHIFT", "=", "UPARROW", "/",
    "1", "LEFTARROW", "CTRL", "2", "SPACE", "C=", "Q", "RUN/STOP",
]
assert len(KEY_NAMES) == 64


def pos(idx: int) -> str:
    return f"c{idx // 8}r{idx % 8} {KEY_NAMES[idx]}"


def mat_bits(mat: bytes) -> set[int]:
    """hle_con_mat layout (per column, bit = row, 1 = down) -> indices."""
    return {c * 8 + r for c in range(8) for r in range(8)
            if mat[c] >> r & 1}


def bits_mat(idxs) -> bytes:
    m = bytearray(8)
    for i in idxs:
        m[i // 8] |= 1 << (i % 8)
    return bytes(m)


def bleed_suspect(idx: int, held: set[int]) -> int | None:
    """The scan goes column 7 -> 0: a row still low from column c+1 reads as
    a key in column c.  -> the held key at c+1 on the same row, or None."""
    c, r = divmod(idx, 8)
    src = (c + 1) * 8 + r
    # a held modifier (RSHIFT is c6r4 above '.' c5r4) is not a source: a
    # modifier held while its neighbour is typed is the normal case (fix3)
    return src if c < 7 and src in held and src != idx and \
        src not in MODS else None


def key_events(prev: set[int], now: set[int], dn: int) -> list[str]:
    """Text lines for one poll: keys that went down/up between two debounced
    matrices, *dn* = hle_con_nkey delta (key-down events decoded)."""
    out = []
    down = sorted(now - prev)
    for i in down:
        kind = "mod " if i in MODS else "key "
        line = f"{kind}DOWN {pos(i)}"
        src = bleed_suspect(i, now)
        if src is not None and i not in MODS:
            line += f"   <- COLUMN BLEED? {pos(src)} is held"
        out.append(line)
    for i in sorted(prev - now):
        out.append(f"{'mod ' if i in MODS else 'key '}UP   {pos(i)}")
    seen = len([i for i in down if i not in MODS])
    if dn > seen:
        out.append(f"(+{dn - seen} key event(s) released before this poll "
                   "saw them)")
    elif dn < seen:
        out.append(f"({seen - dn} key(s) down without an event: masked "
                   "or dropped)")
    return out


def joy_rows(idle: int) -> list[int]:
    """hle_con_idle = port B with no column selected: 0 bits = rows a port-1
    joystick holds low (masked out of the scan)."""
    return [r for r in range(8) if not idle >> r & 1]


# --- screen mirror --------------------------------------------------------------

ROWS, COLS = 25, 40


def scr_text(row: bytes) -> str:
    """One screen row as text: screen codes 0-31 are @ A-Z [ pound ] UP-ARROW
    LEFT-ARROW, shown as @ A-Z [ \\ ] ^ _ (so a phantom's UP-ARROW reads
    ^, not '.'), $20-$3F as themselves, anything else as '.'."""
    return "".join("@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_"[c] if c < 0x20 else
                   chr(c) if c < 0x40 else "." for c in row).rstrip()


def screen_rows(scr: bytes) -> list[str]:
    return [scr_text(scr[r * COLS:(r + 1) * COLS]) for r in range(ROWS)]


def row_codes(text: str) -> bytes:
    from tests.run_vbcc import ascii_to_screen
    return bytes(ascii_to_screen(ord(ch)) for ch in text.ljust(COLS))


def shown(text: str) -> str:
    """*text* as it reads back from the screen through :func:`scr_text`
    (letters come back upper case, trailing blanks stripped)."""
    return scr_text(row_codes(text))


def new_rows(old: list[str], new: list[str]) -> list[str]:
    """Non-empty rows of *new* the host has not printed yet.  The scroll k
    (0..24 rows) is the one that explains the change best: fewest rows of
    new[0:25-k] that differ from old[k:25], plus k (ties: smallest k).  Rows
    that differ after that shift, and the k rows scrolled in, are new."""
    def cost(k: int) -> int:
        return k + sum(new[r] != old[r + k] for r in range(ROWS - k))
    k = min(range(ROWS), key=cost)
    out = [new[r] for r in range(ROWS - k) if new[r] != old[r + k]]
    return [r for r in out + new[ROWS - k:] if r]


# --- running the program asynchronously -----------------------------------------

def start_async(sess, pc: int, a7: int, budget: int) -> None:
    """Enter test_run_program and return at once (keys are pressed while it
    runs).  VICE: run_con's cassette-buffer trampoline.  U64: the mailbox
    dispatcher's call, without waiting for DONE."""
    from c64_test_harness import write_bytes
    from tests.harness_util import lookup_label
    from tests.run_con import start_program
    from tests.run_program import BUF_CASE
    if sess.target.backend == "vice":
        start_program(sess, pc, a7, budget)
        return
    t = sess.target.transport
    disp = sess.target._u64_dispatch
    trp = lookup_label(sess.labels, "test_run_program")
    write_bytes(t, BUF_CASE, struct.pack("<III", pc, a7, budget))
    write_bytes(t, disp["jsr_operand"], bytes([trp & 0xFF, trp >> 8]))
    write_bytes(t, disp["mbox"], bytes([0x01, 0x00]))    # MBOX=1, DONE=0


def is_done(sess) -> bool:
    from c64_test_harness import read_bytes
    from tests.run_con import program_done
    if sess.target.backend == "vice":
        return program_done(sess)
    disp = sess.target._u64_dispatch
    return read_bytes(sess.target.transport, disp["done"], 1)[0] == 0xFF


def result(sess):
    from c64_test_harness import read_bytes
    from tests.run_program import BUF_RESULT, RESULT_LEN, RunResult
    return RunResult.from_bytes(read_bytes(sess.target.transport, BUF_RESULT,
                                           RESULT_LEN))


def queue_keys(sess, data: bytes) -> None:
    """Append *data* to the injection queue while the program runs: the
    bytes first, then the tail (one-byte write), so the 6502 never sees a
    tail past written data."""
    from c64_test_harness import read_bytes, write_bytes
    from tests.run_con import QUEUE_LEN, label
    t = sess.target.transport
    q, qh, qt = (label(sess, n) for n in ("hle_con_q", "hle_con_qh",
                                          "hle_con_qt"))
    head, tail = read_bytes(t, qh, 1)[0], read_bytes(t, qt, 1)[0]
    if (tail - head) % QUEUE_LEN + len(data) >= QUEUE_LEN:
        raise ValueError("injection queue full")
    first = data[:QUEUE_LEN - tail]             # up to the ring's end
    write_bytes(t, q + tail, first)
    if len(first) < len(data):
        write_bytes(t, q, data[len(first):])
    write_bytes(t, qt, bytes([(tail + len(data)) % QUEUE_LEN]))


# --- console labels ---------------------------------------------------------------

STREAM_LABELS = ("hle_con_nkey", "hle_con_idle", "hle_con_mat",
                 "hle_con_raw", "hle_con_ras")
HLE_CON_SRC = os.path.join(PROJECT_ROOT, "runtime", "ops", "hle_con.s")


def bss_layout(text: str) -> dict[str, int]:
    """{name: offset from the first BSS symbol} from hle_con.s's
    ``name: .res N`` lines (N a number, an equate of the file, or
    ``EQUATE + k``), in declaration order (ca65 keeps it)."""
    eq = {m[0]: int(m[1]) for m in
          re.findall(r"^(\w+)\s*=\s*(\d+)\b", text, re.M)}
    bss = text.split('.segment "BSS"', 1)[1].split(".segment", 1)[0]
    out, off = {}, 0
    for name, expr in re.findall(r"^(\w+):\s*\.res\s+([^;\n]+)", bss, re.M):
        out[name] = off
        off += sum(int(t) if t.isdigit() else eq[t]
                   for t in (x.strip() for x in expr.split("+")))
    return out


def con_label(sess, name: str) -> int | None:
    """Address of a hle_con BSS symbol.  Exported ones come from the label
    file; the others (``hle_con_idle``) are derived from the BSS layout
    of runtime/ops/hle_con.s, anchored at hle_con_magic, and only when that
    layout reproduces EVERY exported BSS label (else None)."""
    from tests.run_con import label
    a = label(sess, name)
    if a is not None:
        return a
    with open(HLE_CON_SRC) as f:
        lay = bss_layout(f.read())
    base = label(sess, "hle_con_magic")
    if base is None or name not in lay or "hle_con_magic" not in lay:
        return None
    base -= lay["hle_con_magic"]
    for sym, off in lay.items():
        got = label(sess, sym)
        if got is not None and got != base + off:
            return None
    return base + lay[name]


# --- the stream -----------------------------------------------------------------

@dataclass
class Sample:
    t: float
    step: int | None           # the step on screen when this poll was taken
    keys: frozenset            # debounced matrix (indices)
    idle: int                  # hle_con_idle
    nkey: int | None = None    # hle_con_nkey (key-down events decoded)
    line: str | None = None    # the echo line on screen ("> ..." row)
    raw: frozenset | None = None   # hle_con_raw: last valid raw sample
    live: bool | None = None   # hle_con_ras moved within the last 3 polls
                               # (the scanner is sampling); None: unknown


# A program row can land after prompt residue ("> STEPS ..." when the free
# mode ends on a prompt row, fix3 BUG 1): tolerate "> " before these rows.
def echo_line(rows: list[str]) -> str | None:
    """The console line being typed: the text after "> " on the lowest
    prompt row on screen (None: no prompt row)."""
    for r in reversed(rows):
        if r.startswith(">"):
            return r[2:]
    return None


STEP_HDR = re.compile(r"(?:> )?STEP (\d+)/\d+")
STEP_END = re.compile(r"(?:> )?STEP (\d+) (PASS|FAIL|SKIP)")
STEPS_ROW = re.compile(rf"STEPS ([PFS-]{{{len(STEPS)}}})(?![PFS-])")


HB_EVERY = 1.0             # s between two heartbeat lines in the log
HB_LINE = re.compile(r"^hb polls \+(\d+) max gap ([\d.]+)s")
POLL_DEFAULT = 0.1         # s: --poll's default
POLL_OVERHEAD = 0.15       # s the REST reads of one poll add to --poll: the
                           # measured poll period is 0.24-0.26 s at --poll
                           # 0.1 and 1.14-1.15 s at --poll 1.0 (fix5 D1)
GAP_SLACK = 0.15           # s of jitter over the period before a gap is a
                           # stall


def poll_period(poll: float) -> float:
    """The real poll period of a stream run with --poll *poll*."""
    return poll + POLL_OVERHEAD


def gap_max(period: float) -> float:
    """The longest gap between two polls that is not a stall, at *period*:
    0.4 s at the default --poll (ECHO_GAP_MAX), more at a sparse poll."""
    return max(ECHO_GAP_MAX, period + GAP_SLACK)


def _log_only(line: str) -> None:
    """Write *line* to the session log only (not the operator's terminal):
    the Tee that run_u64 installs has the log file as .f."""
    f = getattr(sys.stdout, "f", None)
    if f is not None:
        f.write(line + "\n")


class Stream:
    """Poll the scanner state and the screen; print what changed; keep a
    step-tagged timeline for :func:`judge_step`.  Every HB_EVERY s a
    heartbeat "hb polls +N max gap Gs" goes to the session log (fix4), so
    a replay can prove that polling was continuous; :meth:`hb_flush` logs
    the last, partial window.  *period* is the poll period the judge
    assumes (leg_manual sets it from --poll); *confirmed* the steps whose
    hold the operator said yes / no to (fix6, :func:`hold_question_steps`).
    """

    def __init__(self, sess, out=print):
        self.sess = sess
        self.out = out
        self.at = {n: con_label(sess, n) for n in STREAM_LABELS}
        missing = [n for n, a in self.at.items() if a is None]
        if missing:
            raise RuntimeError(f"PRG lacks console labels {missing}")
        self.lo = min(self.at.values())
        self.span = max(self.at.values()) + 8 - self.lo
        self.t0 = self.hb_t = time.monotonic()
        self.last_poll = None             # the first gap is from poll 1
        self.hb_n, self.hb_gap, self.gap_worst = 0, 0.0, 0.0
        self.poll_s, self.period = POLL_DEFAULT, poll_period(POLL_DEFAULT)
        self.raw_known, self.confirmed, self.denied = True, set(), set()
        self.ras_seen: list[int] = []
        self.live: bool | None = None
        self.nkey = self.mat = self.idle = None
        self.rows = [""] * ROWS
        self.log: list[str] = []          # every line printed
        self.bleeds: list[tuple[int | None, str]] = []
        self.screen_log: list[str] = []
        self.samples: list[Sample] = []
        self.step: int | None = None
        self.last_hdr = 0                 # steps only move forward
        self.raw_extra: set[int] = set()

    def emit(self, line: str) -> None:
        line = f"[{time.monotonic() - self.t0:7.2f}] {line}"
        self.log.append(line)
        self.out(line)

    def read_state(self) -> dict:
        from c64_test_harness import read_bytes
        blk = read_bytes(self.sess.target.transport, self.lo, self.span)
        g = lambda n, k=1: blk[self.at[n] - self.lo:self.at[n] - self.lo + k]
        return {"nkey": g("hle_con_nkey")[0],
                "idle": g("hle_con_idle")[0], "mat": g("hle_con_mat", 8),
                "raw": g("hle_con_raw", 8), "ras": g("hle_con_ras")[0]}

    def screen_step(self, row: str) -> None:
        m = STEP_HDR.match(row)
        if m and int(m[1]) > self.last_hdr:
            self.step = self.last_hdr = int(m[1])
        m = STEP_END.match(row)
        if m and self.step == int(m[1]):
            self.step = None

    def poll(self, screen: bool = True) -> dict:
        from c64_test_harness import read_bytes
        from tests.run_vbcc import SCREEN
        now = time.monotonic()
        self.hb_n += 1
        if self.last_poll is not None:
            self.hb_gap = max(self.hb_gap, now - self.last_poll)
            self.gap_worst = max(self.gap_worst, now - self.last_poll)
        self.last_poll = now
        if now - self.hb_t >= HB_EVERY:
            self._hb(now)
        s = self.read_state()
        self._liveness(s.get("ras"))
        if self.nkey is None or self.mat == b"\xff" * 8:
            # first poll, or hle_con_init's "every key down": baseline only
            self.nkey, self.mat = s["nkey"], s["mat"]
        else:
            dn = (s["nkey"] - self.nkey) & 0xFF
            if dn or s["mat"] != self.mat:
                for line in key_events(mat_bits(self.mat), mat_bits(s["mat"]),
                                       dn):
                    if "COLUMN BLEED" in line:
                        self.bleeds.append((self.step, line))
                    self.emit(line)
            self.nkey, self.mat = s["nkey"], s["mat"]
        # hle_con_idle is only meaningful once the scanner has sampled
        # (hle_con_init's all-down matrix has been committed away)
        if s["mat"] != b"\xff" * 8:
            self.samples.append(Sample(now, self.step,
                                       frozenset(mat_bits(s["mat"])),
                                       s["idle"], s["nkey"],
                                       raw=frozenset(mat_bits(s["raw"])),
                                       live=self.live))
            # diagnostic (fix3): keys in the latest raw sample that the
            # debounce has not committed -- a key held but never settled
            # (the "nothing" form of a phantom) shows here, a key never
            # pressed does not
            extra = {i for i in mat_bits(s["raw"]) if i not in MODS} - \
                mat_bits(s["mat"])
            if extra != self.raw_extra:
                # "none" when it clears (fix5 F5): one long raw-only hold
                # and two taps then read differently in the log
                self.emit("raw (not debounced yet): " +
                          (", ".join(pos(i) for i in sorted(extra))
                           if extra else "none"))
                self.raw_extra = extra
            if s["idle"] != self.idle:
                rows = joy_rows(s["idle"])
                if rows or self.idle is not None:
                    self.emit("port-1 joystick: " + (
                        "rows " + " ".join(f"r{r}" for r in rows) +
                        " held low (masked)" if rows
                        else "released (no row held)"))
                self.idle = s["idle"]
        if screen:
            rows = screen_rows(read_bytes(self.sess.target.transport, SCREEN,
                                          ROWS * COLS))
            for r in new_rows(self.rows, rows):
                self.screen_log.append(r)
                self.screen_step(r)
                self.emit(f"  | {r}")
            self.rows = rows
            if self.samples and self.samples[-1].line is None:
                self.samples[-1].line = echo_line(rows)
        return s

    def _liveness(self, ras: int | None) -> None:
        """hle_con_ras (VIC raster at the last valid sample) changes on
        almost every sample, so the same value at 3 polls in a row means
        no valid sample in between (fix6: port 2 active, the ring pending,
        or no Read running): a raw that shows nothing then proves nothing.
        Logged on each change of state."""
        if ras is None:
            return
        self.ras_seen = (self.ras_seen + [ras])[-3:]
        if len(self.ras_seen) < 3:
            return
        live = len(set(self.ras_seen)) > 1
        if live != self.live and not (live and self.live is None):
            self.emit("scanner: sampling" if live else "scanner: not "
                      "sampling (hle_con_ras unchanged over 3 polls)")
        self.live = live

    def _hb(self, now: float) -> None:
        _log_only(f"[{now - self.t0:7.2f}] hb polls +{self.hb_n} max "
                  f"gap {self.hb_gap:.2f}s")
        self.hb_t, self.hb_n, self.hb_gap = now, 0, 0.0

    def hb_flush(self) -> None:
        """Log the last heartbeat window, however short (fix5 H1)."""
        if self.hb_n:
            self._hb(time.monotonic())

    def verdicts(self) -> str | None:
        """The program's own STEPS summary (nine P/F/S/- letters), wherever
        it landed on its row (after prompt residue too), else None."""
        for r in reversed(self.screen_log):
            m = STEPS_ROW.search(r)
            if m:
                return m[1]
        return None


# --- replaying a session log ----------------------------------------------------

LOG_LINE = re.compile(r"^\[\s*([\d.]+)\] (.*)$")
# a session's first line (run_u64; wall-clock stamp): one log may hold
# several sessions, each judged on its own (fix5 F7)
SESSION_HDR = re.compile(r"^\[\d\d:\d\d:\d\d\] kbd_echo (?:MANUAL|smoke)\b")
HDR_RUNS = re.compile(r", runs (\S+@\d+(?:, \S+@\d+)*)")
HDR_POLL = re.compile(r", poll ([\d.]+)s\b")
RUN_START = re.compile(r"^(\S+@\d+) started(?: \(--poll ([\d.]+)s\))?: ")
RUN_END = re.compile(r"^(\S+@\d+): (PASS|FAIL|INCOMPLETE|DEFECT SHOWN|"
                     r"INCONCLUSIVE|NOT JUDGEABLE) \(want")
KEY_LINE = re.compile(r"^(key|mod) (DOWN|UP)\s+c(\d)r(\d) ")
REL_LINE = re.compile(r"^\(\+(\d+) key event")
MASK_LINE = re.compile(r"^\((\d+) key\(s\) down without an event")
JOY_LINE = re.compile(r"^port-1 joystick: (?:rows ((?:r\d ?)+) held low|"
                      r"released)")
RAW_LINE = re.compile(r"^raw \(not debounced yet\): (.*)$")
FILL_MIN = 0.25            # s: synthetic samples are never denser than the
                           # default poll's period, so none lands between
                           # one poll's state and screen reads (0.11-0.14 s
                           # apart in the logs)
CONFIRM_LINE = re.compile(r"^operator confirms: step (\d+)\b")   # fix5
ANSWER_LINE = re.compile(r"^operator: step (\d+) held as asked .*\? "
                         r"(yes|no|no answer)$")
LIVE_LINE = re.compile(r"^scanner: (sampling|not sampling)")


def replay_stream() -> "Stream":
    """A Stream with no session: fed from a log by replay_log()."""
    st = Stream.__new__(Stream)
    st.samples, st.bleeds, st.screen_log, st.log = [], [], [], []
    st.step, st.last_hdr = None, 0
    st.rows = [""] * ROWS
    st.t0 = st.hb_t = time.monotonic()
    st.last_poll = None
    st.hb_n, st.hb_gap, st.gap_worst = 0, 0.0, 0.0
    st.poll_s, st.period = None, poll_period(POLL_DEFAULT)
    st.raw_known, st.confirmed, st.denied = True, set(), set()
    st.ras_seen, st.live = [], None
    st.nkey = st.mat = st.idle = None
    st.raw_extra = set()
    st.out = print
    st.session = 0
    st.continuity = "unproven (no heartbeat in this log)"
    return st


@dataclass
class LoggedRun:
    run: Run
    poll: float | None         # --poll from the run-start line or header
    fix5: bool                 # the run-start line carries --poll (fix5+)
    t0: float
    lines: list                # [(t, message)] between start and end
    logged: str                # the verdict the live driver logged


@dataclass
class LoggedSession:
    header: str | None         # None: the log has no session header
    plan: str | None           # its --runs ("fixed@48,nosettle@48")
    runs: list                 # [LoggedRun]


def read_sessions(path: str) -> list[LoggedSession]:
    """Split a session log into its sessions and each session's finished
    runs.  A run with no end line (the driver died) is dropped."""
    out: list[LoggedSession] = []
    hdr_poll, cur = None, None

    def new(header):
        nonlocal hdr_poll
        m = HDR_RUNS.search(header or "")
        hp = HDR_POLL.search(header or "")
        hdr_poll = float(hp[1]) if hp else None
        out.append(LoggedSession(header, m[1].replace(", ", ",") if m
                                 else None, []))
    with open(path, errors="replace") as f:
        for raw in f:
            text = raw.rstrip("\n")
            if SESSION_HDR.match(text):
                new(text)
                cur = None
                continue
            m = LOG_LINE.match(text)
            if not m:
                continue
            t, msg = float(m[1]), m[2]
            if not out:
                new(None)
            ms = RUN_START.match(msg)
            if ms:
                cur = LoggedRun(parse_runs(ms[1])[0],
                                float(ms[2]) if ms[2] else hdr_poll,
                                ms[2] is not None, t, [], "")
                continue
            if cur is None:
                continue
            me = RUN_END.match(msg)
            if me:
                cur.logged = me[2]
                out[-1].runs.append(cur)
                cur = None
                continue
            cur.lines.append((t, msg))
    return out


def _timing(lr: LoggedRun, assume_poll: float | None
            ) -> tuple[float | None, str, float | None]:
    """-> (period, continuity text, worst logged gap) for one logged run.
    The period comes from the logged --poll, else from the heartbeat, else
    from --assume-poll; else it is unknown (None) and the replay adds no
    samples: only the logged polls count (fix5 F2).  A heartbeat gap over
    gap_max(period) is BROKEN, and the run is judged on the logged polls
    only as well: a synthetic sample could hide the stall."""
    hb = [(t, int(m[1]), float(m[2])) for t, msg in lr.lines
          if (m := HB_LINE.match(msg))]
    if lr.poll is not None:
        period, src = poll_period(lr.poll), f"--poll {lr.poll:g}s logged"
    elif hb:
        prev, per = lr.t0, []
        for t, n, _ in hb:
            if n:
                per.append((t - prev) / n)
            prev = t
        period = sorted(per)[len(per) // 2] if per else None
        src = "measured from the heartbeat"
    elif assume_poll is not None:
        period = poll_period(assume_poll)
        src = f"--assume-poll {assume_poll:g}s, NOT in the log"
    else:
        return None, ("unproven, poll period unknown (no --poll and no "
                      "heartbeat in this log): judged on the logged polls "
                      "only"), None
    if not hb:
        return period, (f"unproven (no heartbeat in this log); period "
                        f"{period:.2f}s from {src}"), None
    worst = max(g for *_, g in hb)
    if period is None or worst > gap_max(period):
        return None, (f"BROKEN (a poll gap of {worst:.2f}s > "
                      f"{gap_max(period or 0):.2f}s): judged on the logged "
                      "polls only"), worst
    return period, (f"proven ({len(hb)} heartbeat(s), max gap {worst:.2f}s; "
                    f"period {period:.2f}s from {src})"), worst


def _replay_run(lr: LoggedRun, assume_poll: float | None) -> "Stream":
    """Rebuild one run's stream: every state change the live stream printed
    (key DOWN/UP with the scanned position, +n events a poll missed, the
    port-1 rows, the raw diagnostic, the mirrored screen rows) at its
    timestamp.  With a known poll period, the polls between two logged
    changes are added too, one period apart counted back from the later
    change (the state is constant there, and the live stream's last poll
    before a change is one period earlier, so a hold is not
    over-measured).  With an unknown period nothing is added."""
    st = replay_stream()
    period, st.continuity, st.gap_worst = _timing(lr, assume_poll)
    st.period, st.poll_s, st.raw_known = period, lr.poll, lr.fix5
    keys, idle, nkey, line, extra, live = set(), 0xFF, 0, None, set(), None
    t_last = lr.t0

    def sample(t):
        st.samples.append(Sample(t, st.step, frozenset(keys), idle, nkey,
                                 line, frozenset(keys | extra), live))
    for t, msg in lr.lines:
        if HB_LINE.match(msg):
            continue
        mc = CONFIRM_LINE.match(msg)
        if mc:
            st.confirmed.add(int(mc[1]))
            continue
        ma = ANSWER_LINE.match(msg)
        if ma:
            if ma[2] == "yes":
                st.confirmed.add(int(ma[1]))
            elif ma[2] == "no":
                st.denied.add(int(ma[1]))
            continue
        if t > t_last:                           # a new poll
            if period is not None:
                step = max(period, FILL_MIN)
                fill, k = [], t - step
                while k > t_last + 1e-6:
                    fill.append(k)
                    k -= step
                for k in reversed(fill):
                    sample(k)
            t_last = t
            sample(t)
        if msg.startswith("  | "):
            row = msg[4:]
            st.screen_log.append(row)
            st.screen_step(row)
            if row.startswith(">"):
                line = row[2:] if len(row) > 1 else ""
            if st.samples:
                st.samples[-1].line = line
            continue
        mk = KEY_LINE.match(msg)
        if mk:
            idx = int(mk[3]) * 8 + int(mk[4])
            if mk[2] == "DOWN":
                keys.add(idx)
                nkey += mk[1] == "key"
                if "COLUMN BLEED" in msg:
                    st.bleeds.append((st.step, msg))
            else:
                keys.discard(idx)
        elif REL_LINE.match(msg):
            nkey += int(REL_LINE.match(msg)[1])
        elif MASK_LINE.match(msg):
            nkey -= int(MASK_LINE.match(msg)[1])
        elif JOY_LINE.match(msg):
            rows = JOY_LINE.match(msg)[1]
            idle = 0xFF
            for r in (rows or "").split():
                idle &= ~(1 << int(r[1:]))
        elif RAW_LINE.match(msg):
            txt = RAW_LINE.match(msg)[1]
            extra = set() if txt == "none" else \
                {int(c) * 8 + int(r) for c, r in re.findall(r"c(\d)r(\d)", txt)}
        elif LIVE_LINE.match(msg):
            live = LIVE_LINE.match(msg)[1] == "sampling"
        if st.samples:
            s0 = st.samples[-1]
            st.samples[-1] = Sample(s0.t, s0.step, frozenset(keys), idle,
                                    nkey, s0.line, frozenset(keys | extra),
                                    live)
    return st


def replay_log(path: str, assume_poll: float | None = None
               ) -> list[tuple[Run, "Stream", str]]:
    """Every finished run of a session log, rebuilt (_replay_run), in log
    order; st.session is the index of its session in the file.
    -> [(run, stream, verdict the live driver logged)]."""
    out = []
    for i, ls in enumerate(read_sessions(path)):
        for lr in ls.runs:
            st = _replay_run(lr, assume_poll)
            st.session = i
            out.append((lr.run, st, lr.logged))
    return out


@dataclass
class ReplaySession:
    header: str | None
    plan: str | None
    results: dict[str, str]    # run -> replayed result (last of each name)
    proven: bool               # every run's polling proven by a heartbeat
    verdict: str


VERDICT_RANK = {"FAIL": 0, "INCOMPLETE": 1, "INCONCLUSIVE": 2,
                "VALIDATED": 3}


def replay_session_verdict(results: dict[str, str], plan: str | None,
                           proven: bool) -> str:
    """session_verdict, as live: a session not on the full plan (or whose
    plan is unknown: no header) is INCOMPLETE; and a replay VALIDATES only
    when every run's polling was proven (fix5 F2)."""
    v = session_verdict(results)
    if v != "FAIL" and plan != MANUAL_PLAN:
        return "INCOMPLETE"
    if v == "VALIDATED" and not proven:
        return "INCOMPLETE"
    return v


def file_verdict(verdicts: list[str]) -> str:
    """One log's verdict: its WORST session's (fix5 F7), never better than
    any single session; no session at all is INCOMPLETE."""
    return min(verdicts, key=VERDICT_RANK.__getitem__, default="INCOMPLETE")


def step1_row(st, run: Run, got: str, session: int, logged: str = "-") -> str:
    """One --replay SUMMARY row: the run's logged and replayed verdicts and
    its step 1 (HOLD Q) as the A/B reads it (docs/HLE_CON.md)."""
    pol = st.continuity.split()[0].rstrip(",")
    head = f"{session:4d} {str(run):14s} {logged:14s} {got:14s} {pol:9s}"
    if 1 not in run.steps:
        return f"{head} {'-':30s} {'-':>6s} {'-':>6s} {'-':>5s} {'-':>4s} -"
    ss = [s for s in st.samples if s.step == 1]
    q = K("q")
    gmax = gap_max(st.period) if st.period is not None else ECHO_GAP_MAX
    j = judge_step(st, 1, control=run.kind == CONTROL)
    never = not any(q in s.keys for s in ss)
    raw_s, raw_n = _raw_only(ss, q, gmax) if st.raw_known else (0.0, 0)
    dead = sum(s.live is False for s in ss)
    form = "nothing" if never else "delayed commit"
    if any("phantom" in x for x in j.settle):
        cls = "phantom (^ with Q)"
    elif any("in raw or committed" in x for x in j.settle):
        cls = "settle: ^ while Q held"            # fix9 N3 (fix8 F1/F2)
    elif j.settle:
        cls = f"settle: {form}"
    elif any("not settle-specific" in x
             for x in j.defects + [j.not_judgeable]):
        cls = f"defect-like: {form}"
    elif not never:
        cls = "clean commit"
    elif dead >= 2:
        cls = "nothing: scanner dead"
    elif raw_n:
        cls = "nothing: raw brief"
    elif any(WAKE_REASON in x for x in j.notes):
        cls = "no Q at all: not awake?"
    else:
        cls = "nothing: no Q seen"
    if not st.raw_known:
        cls += " (no raw)"
    low = sum(6 in joy_rows(s.idle) for s in ss)
    ans = "yes" if 1 in st.confirmed else "no" if 1 in st.denied else "-"
    return (f"{head} {cls:30s} "
            f"{(f'{raw_s:.1f}s' if st.raw_known else 'n/a'):>6s} "
            f"{_runs(ss, q, gmax):5.1f}s {low:5d} {dead:4d} {ans}")


SUMMARY_HDR = (" ses run            logged         replayed       polling   "
               "step 1 (HOLD Q)                raw-only   held r6low dead "
               "operator")


@dataclass
class ABPair:
    """One A/B session (docs/HLE_CON.md "The settle A/B"): nosettle@48 and
    fixed@48, each {step: StepJudge} for steps 1-2."""
    label: str
    poll: float | None
    proven: bool
    fixed: dict
    nosettle: dict


AB_PLANS = ("nosettle@48,fixed@48", "fixed@48,nosettle@48")
AB_PAIRS = 5                  # kept pairs at --poll 1.0 for NOT REPRODUCED


def _ab_discard(p: ABPair) -> str:
    """Why an A/B pair is discarded ("" = kept)."""
    fx = [p.fixed[n] for n in (1, 2)]
    ns = [p.nosettle[n] for n in (1, 2)]
    if not p.proven:
        return "polling not proven by a heartbeat"
    asleep = [f"{b} step {n}" for b, js in (("fixed@48", p.fixed),
                                           ("nosettle@48", p.nosettle))
              for n in (1, 2) if any(WAKE_REASON in x for x in js[n].notes)]
    if asleep:          # a signature elsewhere stays visible (N1 below)
        return (f"{WAKE_REASON}: no Q seen at all (raw too) in "
                + ", ".join(asleep))
    if not all(j.performed for j in fx):
        return "fixed@48 steps 1-2 not held as asked (a tap is no hold)"
    if any(j.defects for j in fx):
        return "fixed@48 stray key: " + " | ".join(
            d for j in fx for d in j.defects)
    if any(j.settle for j in ns):
        return ""
    if any(j.defects for j in ns):
        # fix8 F2: a nosettle defect that is not the settle's is no clean
        # zero
        return "nosettle@48 stray key: " + " | ".join(
            d for j in ns for d in j.defects)
    nj = [j.not_judgeable for j in ns if j.not_judgeable]
    if nj:
        return "nosettle@48 not judgeable: " + " | ".join(nj)
    if not all(j.performed for j in ns):
        return "nosettle@48 steps 1-2 not held as asked"
    return ""


def ab_verdict(pairs: list[ABPair]) -> str:
    """The A/B reading rule (fix7, review6 section 5), automated:
    - STOP: a fixed@48 run shows a settle signature, the debounce
      restarting, or a dead scanner in steps 1-2 (a failure of the fix);
    - a pair is discarded (:func:`_ab_discard`) when its polling is not
      proven, its fixed run did not hold Q, ; and / or had a stray key, or
      its nosettle run, without a signature, was not done as asked or is
      NOT JUDGEABLE (e.g. defect-like, not settle-specific);
    - NECESSARY: a kept pair's nosettle step 1 has a settle-specific
      signature, or step 2 has one in 2 kept pairs or more (the ; and /
      phantoms carry R4);
    - REDO when a single step-2 signature or a defect-like nosettle run
      was seen (fix8 F3: never NOT REPRODUCED next to a signature), or a
      nosettle signature sits in a discarded pair (fix9 N1);
    - NOT REPRODUCED: AB_PAIRS kept pairs at --poll 1.0 and none of that,
      with its limits;
    - REDO otherwise, with the reasons.
    A nosettle stray key that is not the settle's discards its pair
    (fix8 F2).  Step 1 with no Q at all, raw included, is its own discard
    reason, WAKE_REASON (fix9)."""
    stop, kept, gone, nonspec = [], [], [], []
    for p in pairs:
        fx = [p.fixed[n] for n in (1, 2)]
        bad = [x for j in fx for x in j.settle] + \
            [d for j in fx for d in j.defects if "debounce" in d] + \
            [j.not_judgeable for j in fx if "not sampling" in j.not_judgeable]
        if bad:
            stop.append(f"{p.label}: fixed@48: " + " | ".join(bad))
            continue
        nonspec += [f"{p.label}: {p.nosettle[n].not_judgeable}"
                    for n in (1, 2)
                    if "not settle-specific" in p.nosettle[n].not_judgeable]
        why = _ab_discard(p)
        (gone if why else kept).append((p, why))
    if stop:
        return "STOP: " + "; ".join(stop) + " (report: the fix failed)"
    s1 = [f"{p.label} step 1: {p.nosettle[1].settle[0]}" for p, _ in kept
          if p.nosettle[1].settle]
    s2 = [f"{p.label} step 2: {p.nosettle[2].settle[0]}" for p, _ in kept
          if p.nosettle[2].settle]
    if s1 or len(s2) >= 2:
        return ("NECESSARY: settle-specific signature(s) " + "; ".join(
            s1 + (s2 if len(s2) >= 2 else [])) + f"; {len(kept)} kept "
            "pair(s), every kept fixed@48 run clean")
    discards = "".join(f"; discarded {p.label}: {w}" for p, w in gone)
    more = ("; at most 3 extra pairs at --poll 1.0 (AB 8-10), then report "
            "REDO (unresolved)")
    # fix9 N1: a nosettle signature in a DISCARDED pair (its partner
    # tapped, a stray key, unproven polling) is still a signature: the
    # line never reads NOT REPRODUCED beside it
    lost = [f"{p.label} step {n}: {p.nosettle[n].settle[0]}"
            for p, _ in gone for n in (1, 2) if p.nosettle[n].settle]
    if lost:
        n_slow = sum(p.poll == 1.0 for p, _ in kept)
        return (f"REDO: {n_slow} of {AB_PAIRS} kept pairs at --poll 1.0; a "
                "settle-specific signature in a discarded pair: redo that "
                "pair under a new name; " + "; ".join(lost) + discards)
    # fix8 F3: NOT REPRODUCED never with a signature or a defect-like run
    # anywhere: a single step-2 signature (R4) or a debounce that kept
    # restarting without settle evidence asks for more pairs
    slow = [p for p, _ in kept if p.poll == 1.0]
    if s2:
        return (f"REDO: {len(s2)} step-2 settle-specific signature, not yet "
                "repeated in another pair (R4): run more pairs" + more + "; "
                + "; ".join(s2) + discards)
    if nonspec:
        return ("REDO: defect-like, not settle-specific (the debounce kept "
                "restarting with no settle evidence): run more pairs" + more
                + "; " + "; ".join(nonspec) + discards)
    tail = ""
    if len(slow) >= AB_PAIRS:
        bound = 1 - 0.05 ** (1 / len(slow))
        return (f"NOT REPRODUCED: 0 of {len(slow)} kept pairs at --poll 1.0 "
                "showed a settle-specific signature: if the defect occurs, "
                f"its per-run rate under this detector is below "
                f"{bound:.0%} (95%), and delays under about 1.15 s are "
                "invisible at this poll; this is not 'the settle is "
                f"unnecessary'{tail}")
    return f"REDO: {len(slow)} of {AB_PAIRS} kept pairs at --poll 1.0" + \
        discards


def _ab_pair(path: str, i: int, ab: dict, proven: bool) -> ABPair:
    missing = {n: StepJudge(False, [], [], [], "run missing") for n in (1, 2)}
    fx, ns = ab.get("fixed@48"), ab.get("nosettle@48")
    label = os.path.basename(os.path.dirname(os.path.abspath(path)))
    return ABPair(f"{label}#{i + 1}", fx[0].poll_s if fx else None, proven,
                  fx[1] if fx else missing, ns[1] if ns else missing)


def replay_report(paths: list[str], out=print,
                  assume_poll: float | None = None
                  ) -> dict[str, list[ReplaySession]]:
    """Re-judge every run of each log, session by session."""
    res, pairs, seen = {}, [], set()
    for path in paths:
        real = os.path.realpath(path)
        if real in seen:
            # fix8 F5: a log passed twice would count its pairs twice
            out(f"== {path}: passed twice, skipped")
            continue
        seen.add(real)
        out(f"== {path}")
        res[path] = []
        rows = []
        for i, ls in enumerate(read_sessions(path)):
            out(f"-- session {i + 1}: {ls.header or '(no session header)'}")
            results, proven, ab = {}, bool(ls.runs), {}
            for lr in ls.runs:
                st = _replay_run(lr, assume_poll)
                run = lr.run
                judges = {n: judge_step(st, n, control=run.kind == CONTROL)
                          for n in run.steps}
                v = st.verdicts()
                got = judge_run(run.kind, v, judges, run.steps)
                results[str(run)] = got
                rows.append(step1_row(st, run, got, i + 1, lr.logged))
                ab[str(run)] = (st, judges)
                proven &= st.continuity.startswith("proven")
                out(f"{run}: logged {lr.logged}; replayed {got}; STEPS {v}; "
                    f"polling {st.continuity}")
                for n, j in judges.items():
                    out(f"   step {n} {STEPS[n - 1].title}: performed "
                        f"{'yes' if j.performed else 'NO'}; "
                        f"{'; '.join(j.notes)}"
                        + (f"; NOT JUDGEABLE: {j.not_judgeable}"
                           if j.not_judgeable else "")
                        + (f"; DEFECT {' | '.join(j.defects)}" if j.defects
                           else "")
                        + (f"; SIGNATURE {' | '.join(j.signature)}"
                           if j.signature else ""))
            if ls.plan in AB_PLANS:
                pr = _ab_pair(path, i, ab, proven)
                if any(q.label == pr.label for q in pairs):
                    out(f"A/B pair {pr.label} passed twice: counted once")
                else:
                    pairs.append(pr)
            sv = replay_session_verdict(results, ls.plan, proven)
            res[path].append(ReplaySession(ls.header, ls.plan, results,
                                           proven, sv))
            why = SESSION_MEANING.get(sv, "")
            if sv != session_verdict(results):
                why = ("not the full plan" if ls.plan != MANUAL_PLAN else
                       "polling not proven by a heartbeat") + \
                    f" (the runs alone: {session_verdict(results)})"
            out(f"SESSION VERDICT (replayed; the last run of each name "
                f"counts): {sv}: {why}")
        if len(res[path]) > 1:
            out(f"FILE VERDICT (the worst of {len(res[path])} sessions): "
                f"{file_verdict([s.verdict for s in res[path]])}")
        out(f"SUMMARY {path}: one row per run (raw-only = Q's longest "
            "raw-only run in step 1)")
        out(SUMMARY_HDR)
        for r in rows:
            out(r)
    if pairs:
        out(f"A/B (docs/HLE_CON.md 'The settle A/B'): {ab_verdict(pairs)}")
    return res


# --- judging the manner -----------------------------------------------------------

@dataclass
class StepJudge:
    performed: bool            # the operator did what the step asks
    defects: list[str]         # what the stream saw that a correct scan
    notes: list[str]           # never produces; notes = manner details
    # the defect a CONTROL build is expected to produce, seen in the stream
    # (nosettle: the column c-1 phantom going down in the same sample as
    # the held key; nojoymask: keys on the stick's row reading down).  A
    # program F or S without it is operator error, not the defect.
    signature: list[str] = field(default_factory=list)
    # why the stream cannot decide whether the step was performed (fix5:
    # the poll period is unknown, too sparse for a tapped step, or a poll
    # gap is a stall).  Only set on a step not proven performed; such a
    # step is NOT JUDGEABLE, not operator error.
    not_judgeable: str = ""
    # settle-specific evidence (fix7 K1): the phantom, or a debounce that
    # kept restarting with the column c-1 key in raw or the held key's row
    # low at idle.  Only this may count as "the settle fix is necessary";
    # in the fixed build it is a failure of the fix.
    settle: list[str] = field(default_factory=list)


NOT_JUDGEABLE = "NOT JUDGEABLE"


def _runs(samples: list[Sample], key: int, gmax: float | None = None,
          raw: bool = False) -> float:
    """Longest time *key* stayed down over consecutive samples, a run
    broken by a gap over *gmax* between two polls (fix5 F3: a hold is not
    credited across a stall).  *raw*: down in the raw sample only, never
    debounced (the settle "nothing" form)."""
    best, start, prev = 0.0, None, None
    for s in samples:
        on = key in ((s.raw or frozenset()) - s.keys if raw else s.keys)
        if on and start is not None and gmax is not None and \
                s.t - prev > gmax:
            start = None
        if on:
            start = s.t if start is None else start
            best = max(best, s.t - start)
        else:
            start = None
        prev = s.t
    return best


def _first_down(samples: list[Sample], key: int) -> Sample | None:
    return next((s for s in samples if key in s.keys), None)


ECHO_GAP_MAX = 0.4         # s: longest poll gap an echo credit may span
                           # (real sessions at --poll 0.1: period 0.25 s,
                           # 0.33 s at most; fix4)


def _echo_idx(samples: list[Sample]) -> list[tuple[str, int, int]]:
    """(character, index of the poll before, index of the poll after) for
    every character the echo line gained between two consecutive polls.

    - The line is tracked against a BASE: the last line accepted as real.
      A step's samples start at an empty prompt, so the base starts "".
      The new text is what follows the longest common prefix of the base
      and the polled line: DEL shrinks the line and types nothing (fix4
      G1; the base shrinks with it); DEL plus a new key between two polls
      types just that key.
    - The two polls may be at most ECHO_GAP_MAX apart (a stalled poll must
      not widen what "held at both polls" covers).
    - Every gain must be backed by decoded key events: hle_con_nkey must
      have grown by at least as many over the gain's interval widened by
      one poll on each side (the count and the echo are read at different
      moments of a poll, so either can lead by one; a replayed log also
      splits one poll's state and screen reads into two samples).  A row
      the screen mirror re-emitted from before (a scroll read mid-copy) has
      no events behind it and is not a keystroke.
    - A gain is tested only where the line changed between the two polls
      (fix5 F6): a rejected gain is not re-tested at later polls, where a
      burst of events that left the line as it was would back it."""
    out, base = [], ""
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        if b.line is None or b.line == base or b.line == a.line:
            continue
        n = 0
        while n < min(len(base), len(b.line)) and base[n] == b.line[n]:
            n += 1
        new = b.line[n:]
        if not new:                       # DEL: nothing typed
            base = b.line
            continue
        z = samples[i - 2] if i >= 2 else a
        c = samples[i + 1] if i + 1 < len(samples) else b
        if None not in (z.nkey, c.nkey) and \
                (c.nkey - z.nkey) & 0xFF < len(new):
            continue                      # no events: not a keystroke
        base = b.line
        if b.t - a.t > ECHO_GAP_MAX:
            continue                      # typed, but not provably when
        out += [(ch, i - 1, i) for ch in new]
    return out


def echo_typed(samples: list[Sample]) -> list[tuple[str, Sample, Sample]]:
    """(character, poll before, poll after) for every character the echo
    line gained between two polls (fix3 BUG 2; rules in _echo_idx).  A key
    tapped between two polls never shows in the debounced matrix at a poll,
    but its echo does; what was held during the tap is what both polls
    show."""
    return [(ch, samples[ia], samples[ib])
            for ch, ia, ib in _echo_idx(samples)]


def credit(samples: list[Sample], key: int, echo: str,
           cond) -> tuple[int, int] | None:
    """The polls (first, last) that prove *key* went down while *cond*
    held: its FIRST poll in the matrix, if that satisfies cond; else an
    echo gain of *echo* between two polls that both satisfy cond.  None:
    not proven."""
    f = next((i for i, s in enumerate(samples) if key in s.keys), None)
    if f is not None and cond(samples[f]):
        return f, f
    for ch, ia, ib in _echo_idx(samples):
        if ch == echo and cond(samples[ia]) and cond(samples[ib]):
            return ia, ib
    return None


def typed_with(samples: list[Sample], key: int, echo: str, cond) -> bool:
    return credit(samples, key, echo, cond) is not None


WAKE_REASON = "keyboard not awake? (Blingboard idle; tap SHIFT first)"
DELAY_MIN = 0.2            # s a held key may read raw-only before its commit
                           # (fix6 R2).  A healthy commit follows the press
                           # after CON_STABLE = 64 raster lines (~4 ms) plus
                           # switch bounce (a few ms, each bounce restarting
                           # the window), so a poll sees a key raw-only at
                           # most once.  0.2 s is 50x CON_STABLE, below the
                           # shortest poll period (0.24 s at --poll 0.1), and
                           # above the 0.11-0.14 s between one poll's state
                           # and screen reads that a replay splits in two.


def _raw_only(samples: list[Sample], key: int, gmax: float
              ) -> tuple[float, int]:
    """(longest raw-only run of *key* in s, polls it was raw-only): how
    long the debounce kept a press of it out of the matrix.  Any press of
    the step counts (a release never reads raw-only: raw drops the key
    before the matrix does)."""
    return (_runs(samples, key, gmax, raw=True),
            sum(key in (s.raw or frozenset()) - s.keys for s in samples))


def _settle_evidence(samples: list[Sample], key: int, ph: int | None
                     ) -> list[str]:
    """What only the missing row settle produces while *key* is held
    (fix7 K1, K3):
    - the column c-1 key on the same row in raw together with *key* (a
      row still low from *key*'s column read as the next column's);
    - *key*'s row low at the idle read, for rows 5-7 (r0-r4 are port-1
      joystick lines).  hle_con_scan settles before every column, so in
      the fixed build the settle before column c-1 already waited for the
      row after *key*'s column; only after column 0 is there no settle
      before the next sample's idle read.  In nosettle nothing waits, and
      a row still low at the idle read masks *key* out of that sample.
    A chattering switch gives neither.  The idle rows count only while
    the key is held (fix8 F6)."""
    out = []
    if ph is not None:
        n = sum(key in (s.raw or ()) and ph in (s.raw or ()) for s in samples)
        if n:
            out.append(f"{pos(ph)} in raw with {KEY_NAMES[key]} at {n} "
                       "poll(s)")
    # the idle read only while the key is held (fix8 F6): from its first
    # poll in raw or the matrix to the poll after its last (a masked
    # sample has it in neither)
    on = [i for i, s in enumerate(samples)
          if key in s.keys or key in (s.raw or ())]
    held = samples[on[0]:on[-1] + 2] if on else []
    r = key % 8
    if r >= 5:
        n = sum(r in joy_rows(s.idle) for s in held)
        if n:
            out.append(f"r{r} low at the idle read at {n} poll(s)")
    return out


def held_throughout(samples: list[Sample], spans, cond) -> float | None:
    """None when every poll from the first span's start to the last span's
    end satisfies cond, at most ECHO_GAP_MAX apart, else the time of the
    first poll that does not (fix4 continuity: letting go between two
    letters is not a hold; fix5: nor is a gap the stick could be let go
    in)."""
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    return next((samples[i].t for i in range(lo, hi + 1)
                 if not cond(samples[i]) or
                 (i > lo and samples[i].t - samples[i - 1].t > ECHO_GAP_MAX)),
                None)


def judge_step(stream: Stream, n: int, control: bool = False) -> StepJudge:
    """How step *n* was done, from the stream's samples tagged *n*.  A
    control run needs only the triggering action (e.g. the stick held):
    the broken build may garble the typing itself.

    Timing (fix5): stream.period is the poll period (None: unknown, e.g. a
    replayed log with no --poll and no heartbeat; then only the logged
    polls count).  A hold is not credited across a gap over gap_max, an
    echo or stick credit not across one over ECHO_GAP_MAX.  A step not
    proven performed is NOT JUDGEABLE (StepJudge.not_judgeable) instead
    of operator error when the period is unknown, a gap in the step is a
    stall, or, for a tapped step (3-9) of the fixed build, the period is
    over ECHO_GAP_MAX (decision (b)).

    The debounce restarting (fix6 R1/R2, fix7 K1-K3), measured on the
    held key's raw-only samples (:func:`_raw_only`): raw-only for
    DELAY_MIN or more, committed later ("delayed commit") or never ("the
    nothing form"), or at 2 polls or more when never committed.  With
    settle-specific evidence (:func:`_settle_evidence`) it is the signature
    in a control and a defect in the fixed build (StepJudge.settle);
    without, a control step is NOT JUDGEABLE ("defect-like, not
    settle-specific") and the fixed build FAILs "keyboard/debounce".
    The operator's answer (asked in every nosettle hold step) never
    changes a verdict the instrument supports (K5); only where the scanner
    saw no raw-only key does a "yes" make the step NOT JUDGEABLE, never a
    signature.  A frozen hle_con_ras (2 polls or more) with the key never
    seen is NOT JUDGEABLE too (K6)."""
    step = STEPS[n - 1]
    ss = [s for s in stream.samples if s.step == n]
    notes, defects, sig, settle = [], [], [], []
    ok = True
    period = getattr(stream, "period", poll_period(POLL_DEFAULT))
    gmax = gap_max(period) if period is not None else ECHO_GAP_MAX
    raw_known = getattr(stream, "raw_known", True)
    confirmed = getattr(stream, "confirmed", set())
    nj_hold = ""
    nonmod = lambda s: {i for i in s.keys if i not in MODS}
    if step.kind == "hold":
        for ch in step.keys:
            k = K(ch)
            held = _runs(ss, k, gmax)
            notes.append(f"{KEY_NAMES[k]} held {held:.1f}s")
            good = held >= HOLD_MIN
            name = KEY_NAMES[k]
            c, r = divmod(k, 8)
            ph = (c - 1) * 8 + r if c > 0 and (c - 1) * 8 + r not in MODS \
                else None
            never = not any(k in s.keys for s in ss)
            raw_s, raw_n = _raw_only(ss, k, gmax) if raw_known else (0.0, 0)
            if raw_n:
                notes.append(f"{name} raw-only (not debounced) at {raw_n} "
                             f"poll(s), longest {raw_s:.1f}s" +
                             (", never committed" if never else ""))
            # a healthy press is raw-only at one poll at most: the same
            # raw-only span counts committed or not (K2), and for a key
            # never committed so do raw-only polls that are not
            # consecutive (K3: the idle-mask flicker masks every other one)
            if raw_s >= DELAY_MIN or (never and raw_n >= 2):
                form = ("never committed (the 'nothing' form)" if never
                        else "then committed (delayed commit)")
                line = (f"{name} raw-only at {raw_n} poll(s), longest "
                        f"{raw_s:.1f}s, {form}: the debounce kept restarting")
                ev = _settle_evidence(ss, k, ph)
                if ev:
                    line += "; settle-specific: " + "; ".join(ev)
                    (sig if control else defects).append(line)
                    settle.append(line)
                    good = good or control     # raw proves the press
                elif control:
                    nj_hold = (line + "; defect-like, not settle-specific "
                               "(no column c-1 key in raw, no row low at "
                               "idle: a chattering switch looks the same)")
                    good = False
                else:
                    defects.append("keyboard/debounce, not settle-specific: "
                                   + line)
            elif never and ss:
                dead = sum(s.live is False for s in ss)
                if n == 1 and raw_known and not raw_n and dead < 2:
                    # fix9: the rig's keyboard LED board (Blingboard) hides
                    # the whole first hold after ~2 min idle, in either
                    # build (docs/HARDWARE.md); nothing is no evidence
                    notes.append(f"no {name} seen at all (raw too): "
                                 + WAKE_REASON)
                seen = (f"raw showed {name} at {raw_n} poll(s)" if raw_n
                        else "raw never showed it" if raw_known
                        else "raw is not in this log")
                if dead >= 2:
                    # K6: nothing seen proves nothing when nothing was sampled
                    nj_hold = (f"the scanner was not sampling at {dead} "
                               f"poll(s) (hle_con_ras frozen): {name} never "
                               f"seen proves nothing; {seen}")
                elif n in confirmed:
                    nj_hold = (f"the operator says {name} was held, but the "
                               f"scanner saw no {name} raw-only at 2 polls: "
                               f"{seen}")
            ok &= good
            n_settle = len(settle)
            extra = {i for s in ss if k in s.keys for i in nonmod(s)} - {k}
            if extra:
                defects.append(f"with {KEY_NAMES[k]} held, also down: "
                               + ", ".join(pos(i) for i in sorted(extra)))
            # nosettle signature: the key one column lower on the same row
            # goes down in the SAME sample as the held key
            first = next((i for i, s in enumerate(ss) if k in s.keys), None)
            if ph is not None and first is not None and \
                    ph in ss[first].keys and \
                    (first == 0 or ph not in ss[first - 1].keys):
                sig.append(f"phantom {pos(ph)} went down with "
                           f"{KEY_NAMES[k]} (settle signature)")
                settle.append(sig[-1])
            # fix8 F1/F2: the c-1 key at any poll while the key is held,
            # in raw (flickering, never committed) or committed later
            # (COLUMN BLEED?), even when the key itself committed cleanly:
            # the row still low from the key's column, read as column c-1
            both = lambda s: s.keys | (s.raw or frozenset())
            late = sum(k in both(s) and ph in both(s) for s in ss) \
                if ph is not None else 0
            if late and len(settle) == n_settle and not any(
                    pos(ph) in x for x in settle):
                line = (f"{pos(ph)} with {name} held at {late} poll(s), in "
                        "raw or committed (the row still low from "
                        f"{name}'s column: settle signature)")
                (sig if control else defects).append(line)
                settle.append(line)
    elif step.kind == "rollover":
        most = max((len(nonmod(s)) for s in ss), default=0)
        notes.append(f"at most {most} keys down at once")
        ok = most >= 3
        allowed = {K("z"), K("i"), K("p"), K("\r")}
        extra = {i for s in ss if len(nonmod(s)) >= 3
                 for i in nonmod(s)} - allowed
        if extra:
            defects.append("with Z I P held, also down (ghost?): "
                           + ", ".join(pos(i) for i in sorted(extra)))
    elif step.kind == "mods":
        spans = {}
        for ch, echo, mod in (("a", "A", RSHIFT), ("/", "?", RSHIFT),
                              ("q", "Q", CBM)):
            spans[ch] = credit(ss, K(ch), echo,
                               lambda s, mod=mod: mod in s.keys)
            good = spans[ch] is not None
            notes.append(f"{KEY_NAMES[K(ch)]} with {KEY_NAMES[mod]}: "
                         f"{'yes' if good else 'NO'}")
            ok &= good
        # no continuity across A and /: the C64 screen says "RSHIFT+A,
        # RSHIFT+/", so letting RSHIFT go in between is doing it right
        # (both attended fixed@1 runs did); each character's own credit
        # needs RSHIFT at both of its polls
        if any(LSHIFT in s.keys for s in ss):
            notes.append("LEFT SHIFT was used")
            ok = False
    elif step.kind == "stick":
        r = step.row
        held = [s for s in ss if r in joy_rows(s.idle)]
        notes.append(f"stick row r{r} seen held in {len(held)} poll(s)")
        if control:
            ok = bool(held)
        else:
            held_r = lambda s: r in joy_rows(s.idle)
            spans = []
            for ch in "hose":
                sp = credit(ss, K(ch), ch.upper(), held_r)
                ok &= sp is not None
                if sp is None:
                    notes.append(f"{ch.upper()} not typed with the stick held")
                else:
                    spans.append(sp)
            if len(spans) == 4:
                gap = held_throughout(ss, spans, held_r)
                if gap is not None:
                    notes.append("stick not held continuously from H to E "
                                 f"(not seen held, or a poll gap over "
                                 f"{ECHO_GAP_MAX}s, at t={gap:.2f})")
                    ok = False
        rowkeys = {i for s in held for i in s.keys if i % 8 == r}
        if rowkeys:
            line = (f"stick on r{r}: keys on that row read down: "
                    + ", ".join(pos(i) for i in sorted(rowkeys)))
            defects.append(line)
            sig.append(line + " (mask signature)")
    # a COLUMN BLEED? hint is a defect only in the hold steps (decision
    # (a)): elsewhere two neighbours going down between two polls (I and P
    # in the rollover) is normal typing
    for st, line in stream.bleeds:
        if st == n and step.kind == "hold":
            defects.append(line)
        elif st == n:
            notes.append(f"hint, not a defect outside hold steps: {line}")
    performed = ok and bool(ss)
    nj = ""
    if not performed and ss:
        stall = max((b.t - a.t for a, b in zip(ss, ss[1:])), default=0.0)
        if nj_hold:
            nj = nj_hold
        elif period is None:
            nj = ("poll period unknown (no --poll and no heartbeat in the "
                  "log, or a broken heartbeat): the logged polls alone do "
                  "not prove this step")
        elif stall > gmax:
            nj = (f"a poll gap of {stall:.2f}s > {gmax:.2f}s in this step: "
                  "the driver cannot vouch for what happened in it")
        elif not control and step.kind != "hold" and period > ECHO_GAP_MAX:
            nj = (f"--poll period {period:.2f}s > {ECHO_GAP_MAX}s: a tapped "
                  "step cannot be proven at this poll rate")
        elif not control and step.kind != "hold" and stall > ECHO_GAP_MAX:
            # fix6 R3: an echo credit needs polls <= ECHO_GAP_MAX apart,
            # whatever gap_max allows at this --poll
            nj = (f"a poll gap of {stall:.2f}s > {ECHO_GAP_MAX}s: a tapped "
                  "step cannot be proven across it")
    return StepJudge(performed, defects, notes, sig, nj, settle)


FIXED, CONTROL = "fixed", "control"


def judge_run(kind: str, verdicts: str | None, judges: dict[int, StepJudge],
              steps: range) -> str:
    """-> PASS / FAIL / INCOMPLETE / NOT JUDGEABLE (fixed build) or DEFECT
    SHOWN / INCONCLUSIVE / INCOMPLETE / NOT JUDGEABLE (control).
    *verdicts*: the program's STEPS string (None: it never printed one).
    NOT JUDGEABLE (fix5): nothing else is wrong, but a step's manner could
    not be decided (StepJudge.not_judgeable); an F or a defect is still
    FAIL, and a control's signature is still DEFECT SHOWN (a signature seen
    is a fact whatever the polling, F4)."""
    if verdicts is None or len(verdicts) != NSTEP:
        return "INCOMPLETE"
    v = {n: verdicts[n - 1] for n in steps}
    nj = [n for n in steps if judges[n].not_judgeable]
    undone = any(not judges[n].performed and not judges[n].not_judgeable
                 for n in steps)
    if kind == FIXED:
        if any(x == "F" for x in v.values()) or \
                any(judges[n].defects for n in steps):
            return "FAIL"
        if undone or any(x != "P" for x in v.values()):
            return "INCOMPLETE"
        return NOT_JUDGEABLE if nj else "PASS"
    # control: only the control's own signature counts as its defect; any
    # other F, S or stray key is operator error (redo the leg).  A
    # signature is an observed fact: another step undone never hides it
    # (fix7 K4)
    if any(judges[n].signature for n in steps):
        return "DEFECT SHOWN"
    if undone:
        return "INCOMPLETE"
    # a NOT JUDGEABLE step's own S or F may be the defect it could not
    # decide (fix6: the "nothing" form ends in S), so only the others count
    if any((v[n] != "P" and not judges[n].not_judgeable) or judges[n].defects
           for n in steps):
        return "INCOMPLETE"
    return NOT_JUDGEABLE if nj else "INCONCLUSIVE"


def hold_question_steps(run: Run) -> list[int]:
    """The steps whose hold the operator is asked about: every hold step
    of a nosettle run, whatever it showed (fix6 R6: not only after an
    empty line, so the answer is not primed by the outcome)."""
    return [n for n in run.steps
            if run.build == "nosettle" and STEPS[n - 1].kind == "hold"]


# --- control builds: the fix NOP-ed out in C64 RAM ---------------------------------

# The scan's column loop (hle_con.s hle_con_scan @col):
#   jsr hle_con_settle / bcc @none / lda hle_con_colsel,x / sta CIA1_PRA /
#   lda CIA1_PRB / eor #$FF / and hle_con_idle / sta hle_con_smp,x
# Located in the runner PRG by this byte pattern (None = any byte; the
# and-operand must be hle_con_idle), which must match exactly once.
SITE_PATTERN = [0x20, None, None, 0x90, None, 0xBD, None, None,
                0x8D, 0x00, 0xDC, 0xAD, 0x01, 0xDC, 0x49, 0xFF,
                0x2D, "idle_lo", "idle_hi", 0x9D]
# name -> (offset in the pattern, length): replaced by NOPs ($EA).  The
# same edits as build/evidence/w6-kbd/mutbuild.py's text mutants, without
# moving any code.
CONTROLS = {
    "nosettle": (0, 5),        # jsr hle_con_settle + bcc @none: no settle
    "nojoymask": (16, 3),      # and hle_con_idle: port-1 rows read as keys
}
CONTROL_STEPS = {"nosettle": range(1, 3), "nojoymask": range(5, 10)}


def find_site(prg: bytes, idle: int) -> int:
    """C64 address of SITE_PATTERN in a runner PRG (load address first)."""
    load = prg[0] | prg[1] << 8
    img = prg[2:]
    pat = [idle & 0xFF if p == "idle_lo" else idle >> 8 if p == "idle_hi"
           else p for p in SITE_PATTERN]
    hits = [i for i in range(len(img) - len(pat) + 1)
            if all(p is None or img[i + j] == p for j, p in enumerate(pat))]
    if len(hits) != 1:
        raise RuntimeError(f"scan column loop found {len(hits)} times in "
                           "the runner PRG (want exactly 1)")
    return load + hits[0]


class Patcher:
    """Apply / remove a control patch in the running runner's RAM, each
    write verified by readback.  Only while the runner is idle (the scan
    runs only inside a program's Read)."""

    def __init__(self, sess, prg_path: str):
        with open(prg_path, "rb") as f:
            prg = f.read()
        idle = con_label(sess, "hle_con_idle")
        if idle is None:
            raise RuntimeError("hle_con_idle address unknown")
        self.sess = sess
        self.site = find_site(prg, idle)
        load = prg[0] | prg[1] << 8
        self.orig = {n: prg[2 + self.site - load + o:2 + self.site - load + o
                            + ln] for n, (o, ln) in CONTROLS.items()}
        self.active: str | None = None

    def _put(self, addr: int, data: bytes, what: str) -> None:
        from c64_test_harness import read_bytes, write_bytes
        t = self.sess.target.transport
        write_bytes(t, addr, data)
        got = read_bytes(t, addr, len(data))
        if got != data:
            raise RuntimeError(f"{what}: readback {got.hex()} != {data.hex()}")

    def check_clean(self) -> bool:
        from c64_test_harness import read_bytes
        t = self.sess.target.transport
        return all(read_bytes(t, self.site + CONTROLS[n][0], len(b)) == b
                   for n, b in self.orig.items())

    def apply(self, name: str) -> None:
        off, ln = CONTROLS[name]
        if not self.check_clean():
            raise RuntimeError("runner code is not the original before "
                               f"patching {name}")
        self._put(self.site + off, bytes([0xEA] * ln), f"apply {name}")
        self.active = name

    def remove(self) -> None:
        if self.active is None:
            return
        off, _ = CONTROLS[self.active]
        self._put(self.site + off, self.orig[self.active],
                  f"remove {self.active}")
        self.active = None


# --- VICE: keys and joystick in the emulated matrix -----------------------------

class KbdMatrix:
    """Press keys and hold a CONTROL PORT 2 joystick in VICE via one
    snapshot edit: module KEYBOARD v1.1 (``keyarr[16]``, ``rev_keyarr[8]``,
    int LE; see tests/run_con.MatrixKeys) and module JOYSTICK0 v1.2 (u16:
    1 up, 2 down, 4 left, 8 right, 16 fire).

    Measured in this VICE (x64sc, harness default config; probe in
    build/evidence/w6-kbd/joystick_probe.txt): undumping JOYSTICK0 = v sets
    the value that the NEXT dump writes as JOYSTICK1, and CIA1 port A
    ($DC00, control port 2) then reads ~v; JOYSTICK1 = v reaches neither
    port.  So restore is shifted by one index against save, and control
    port 1 ($DC01) cannot be driven from a snapshot (the binary monitor's
    Joyport-set had no effect on either port either).  JOYSTICK0 is written
    on every edit, so an edit of the keys alone never drops the stick."""

    def __init__(self, transport, scratch: str):
        self.t = transport
        # per process: parallel runs of one PRG share *scratch*, and a
        # snapshot read back from another run rewinds this machine to it
        self.path = os.path.join(scratch, f"kbd-{os.getpid()}.vsf")
        self.ops = 0

    def close(self) -> None:
        if os.path.exists(self.path):
            os.remove(self.path)

    @staticmethod
    def _module(d: bytearray, name: bytes, ver: tuple[int, int],
                size: int) -> int:
        at = d.find(name + b"\0")
        if at < 0:
            raise RuntimeError(f"VICE snapshot has no {name.decode()} module")
        if (d[at + 16], d[at + 17]) != ver or \
                struct.unpack_from("<I", d, at + 18)[0] != size:
            raise RuntimeError(f"unexpected {name.decode()} module "
                               "version/size")
        return at + 22

    def set(self, idxs, joy2: int = 0) -> None:
        self.t.dump_snapshot(self.path, save_roms=False, save_disks=False)
        with open(self.path, "rb") as f:
            d = bytearray(f.read())
        kb = self._module(d, b"KEYBOARD", (1, 1), 22 + 96)
        js = self._module(d, b"JOYSTICK0", (1, 2), 22 + 2)
        arr = [0] * 24
        for idx in idxs:
            col, row = divmod(idx, 8)
            arr[col] |= 1 << row
            arr[16 + row] |= 1 << col
        struct.pack_into("<24I", d, kb, *arr)
        struct.pack_into("<H", d, js, joy2)
        with open(self.path, "wb") as f:
            f.write(d)
        self.t.undump_snapshot(self.path)
        self.ops += 1


def K(ch: str) -> int:
    return key_for(ch)[0]


RET = K("\r")
HOLD_WAIT = 0.4           # s (VICE warp: dozens of debounce windows)
LONG_HOLD = HOLD_MIN + 0.3


def press(*keys, joy: int = 0, n: int = 1):
    """A matrix state: keys held, port-2 stick, key events it must make."""
    return (tuple(keys), joy, n, HOLD_WAIT)


def hold(*keys, joy: int = 0, secs: float = HOLD_WAIT):
    """Same state again: wait *secs* (host time), nothing may be counted."""
    return (tuple(keys), joy, 0, secs)


def tap(ch: str, joy: int = 0, mods: tuple = ()) -> list:
    """Press one key (with held modifiers), release it (modifiers stay)."""
    return [press(*mods, K(ch), joy=joy), press(*mods, joy=joy, n=0)]


RETURN = [press(RET), press(n=0)]
HOSE = tap("h") + tap("o") + tap("s") + tap("e")

STEP_SCRIPTS = [
    [press(K("q")), hold(K("q"), secs=LONG_HOLD), press(n=0)] + RETURN,
    [press(K(";")), hold(K(";"), secs=LONG_HOLD), press(n=0),
     press(K("/")), hold(K("/"), secs=LONG_HOLD), press(n=0)] + RETURN,
    [press(K("z")), press(K("z"), K("i")), press(K("z"), K("i"), K("p")),
     hold(K("z"), K("i"), K("p")), press(n=0)] + RETURN,
    [press(RSHIFT, n=0)] + tap("a", mods=(RSHIFT,)) + tap("/", mods=(RSHIFT,))
    + [press(n=0), press(CBM, n=0)] + tap("q", mods=(CBM,))
    + [press(n=0)] + RETURN,
    # step 5: no port-1 stick in VICE; a port-2 stick held while X is down:
    # the port-A check skips every sample, so X is never typed; after
    # release the keyboard works again
    tap("h") + tap("o")
    + [press(K("x"), joy=JOY2_UP, n=0), press(joy=JOY2_UP, n=0), press(n=0)]
    + tap("s") + tap("e") + RETURN,
] + [HOSE + RETURN] * 4
FREE_SCRIPT = tap("o") + tap("k") + RETURN + [press(RET)]   # "ok", then ""
# a real phantom: Q and the UP-ARROW key (c6r6) in the SAME commit, held;
# then an empty line (SKIP) and CTRL+D (the program ends: no release after)
TEETH_SCRIPT = [press(K("q"), K("^"), n=2), hold(K("q"), K("^"),
                                               secs=LONG_HOLD),
                press(n=0)] + RETURN + RETURN \
    + [press(CTRL, n=0), press(CTRL, K("d"))]
# the right characters, typed the wrong way: Q tapped, step 2 skipped,
# Z I P one after another, LEFT SHIFT in step 4; then CTRL+D
MANNER_SCRIPT = tap("q") + RETURN + RETURN \
    + tap("z") + tap("i") + tap("p") + RETURN \
    + tap("a", mods=(LSHIFT,)) + tap("/", mods=(LSHIFT,)) + [press(n=0)] \
    + tap("q", mods=(CBM,)) + [press(n=0)] + RETURN \
    + [press(CTRL, n=0), press(CTRL, K("d"))]


def visible(idxs, joy2: int) -> bytes | None:
    """hle_con_mat once *idxs* are committed, or None when no commit can
    happen: a port-2 joystick pulls port-A lines low, so the scan skips
    every sample while it is held."""
    return None if joy2 else bits_mat(idxs)


class Driver:
    """Runs one VICE leg: states in, checks out."""

    def __init__(self, sess, mk: KbdMatrix, stream: Stream, timeout: float):
        self.sess, self.mk, self.st, self.timeout = sess, mk, stream, timeout
        self.held: tuple = ()
        self.joy = 0
        mk.set(())            # a previous leg may have ended with keys down

    def wait(self, what: str, pred, secs: float | None = None) -> bool:
        t = self.sess.target.transport
        deadline = time.monotonic() + (secs if secs is not None
                                       else self.timeout)
        while True:
            s = self.st.poll()
            if secs is None and pred(s):
                return True
            if time.monotonic() > deadline:
                if secs is not None:
                    return pred(s)
                self.st.emit(f"timeout waiting for {what}")
                return False
            t.resume()
            time.sleep(0.02)

    def run(self, tag: str, script) -> bool:
        """Every state; False (and a failed check) at the first problem."""
        check = self.sess.check
        for k, (keys, joy, n, secs) in enumerate(script):
            what = f"{tag} state {k} keys={[KEY_NAMES[i] for i in keys]} " \
                   f"joy={joy}"
            s0 = self.st.read_state()
            same = (keys, joy) == (self.held, self.joy)
            if not same:
                self.mk.set(keys, joy)
            added = set(keys) - set(self.held)
            self.held, self.joy = keys, joy
            want = visible(keys, joy)
            if n:
                ok = self.wait(what, lambda s: (s["nkey"] - s0["nkey"]) & 0xFF
                               >= n and s["mat"] == want)
                s1 = self.st.read_state()
                cnt = (s1["nkey"] - s0["nkey"]) & 0xFF
                new = mat_bits(s1["mat"]) - mat_bits(s0["mat"]) - set(MODS)
                exp = added - set(MODS)
                check(f"{what}: {n} key event(s), scanned at "
                      f"{sorted(pos(i) for i in exp)}",
                      ok and cnt == n and new == exp,
                      f"events {cnt}, new keys "
                      f"{sorted(pos(i) for i in new)}")
                if not ok:
                    return False
            else:
                if same or want is None:
                    self.wait(what, lambda s: False, secs=secs)
                    ok = True
                else:
                    ok = self.wait(what, lambda s: s["mat"] == want)
                s1 = self.st.read_state()
                cnt = (s1["nkey"] - s0["nkey"]) & 0xFF
                check(f"{what}: committed, no key event", ok and cnt == 0,
                      f"committed={ok} events={cnt}")
                if not ok:
                    return False
        return True


def wait_rows(drv: Driver, pattern: str) -> list[str] | None:
    """Poll until a screen row matches *pattern*; -> the screen rows."""
    rx = re.compile(pattern)
    found: list = []

    def pred(_s) -> bool:
        if any(rx.match(r) for r in drv.st.rows):
            found[:] = drv.st.rows
            return True
        return False
    return found if drv.wait(pattern, pred) else None


def check_block(sess, tag: str, rows: list[str], want: list[str]) -> None:
    """*want* must be on screen as consecutive rows ending at the last row
    that equals want[-1] (compared as screen codes)."""
    exp = [shown(w) for w in want]
    ends = [r for r in range(len(rows)) if rows[r] == exp[-1]]
    got = rows[max(0, ends[-1] - len(want) + 1):ends[-1] + 1] if ends else []
    sess.check(f"{tag}: screen shows {want[-1]!r} after {len(want) - 1} "
               "row(s) of echo/decode", got == exp,
               f"got {got!r}, want {exp!r}")


def stage_program(sess, blob: bytes, args: bytes = b"\n") -> tuple:
    """Stage the program as tests/run_con.stage does (blank screen, env,
    $A5-poisoned heap, fresh handle pool), with CLI *args*, keyboard stdin."""
    from c64_test_harness import write_bytes
    from host import hle_env, hunk, image
    from host.image import Chunk
    from tests.run_con import con_stdin, label
    from tests.run_vbcc import HEAP_BASE, POISON_BYTE, POISON_LEN, SCREEN, \
        SPACE
    seam = con_stdin(sess, b"", SRC_KEYBOARD)
    sess.check("console seam present", seam, "hle_con_* labels missing")
    exe = hunk.parse(blob)
    chunks, manifest = image.build_image(exe)
    hle_env.check_program_fits(manifest)
    env = hle_env.env_chunks(stack_top=manifest["stack_top"],
                             entry=manifest["entry"], args=args)
    print(f"[{PROG}] {len(blob)}-byte hunk file, args {args!r}")
    write_bytes(sess.target.transport, SCREEN, bytes([SPACE]) * 1000)
    sess.stage(chunks + env + [Chunk(HEAP_BASE,
                                     bytes([POISON_BYTE]) * POISON_LEN)])
    if label(sess, "hle_dos_init") is not None:
        sess._call("hle_dos_init")
    return hle_env.ENV_LAUNCH, manifest


def control_args(name: str) -> bytes:
    r = CONTROL_STEPS[name]
    return f"CONTROL {name} {r.start} {r.stop - 1}\n".encode()


def finish(sess, st: Stream, tag: str, exit_code: int, verdicts: str) -> bool:
    """Wait for the program to exit; check HALT_OK, the exit code and the
    final STEPS/RESULT rows.  False when it never exited."""
    ok = False
    deadline = time.monotonic() + sess.timeout
    while time.monotonic() < deadline:
        st.poll()
        if is_done(sess):
            ok = True
            break
        if sess.target.backend == "vice":
            sess.target.transport.resume()
        time.sleep(0.05)
    st.poll()
    sess.check(f"{tag}: program finished", ok, "still running")
    if not ok:
        return False
    res = result(sess)
    res.print_state()
    sess.check(f"{tag}: halted HALT_OK", res.halt != 0 and res.timeout == 0
               and res.reason == 0, f"halt={res.halt} timeout={res.timeout} "
               f"reason={res.reason:02X}")
    sess.check(f"{tag}: exit code {exit_code}", res.regs["D0"] == exit_code,
               f"D0={res.regs['D0']:08X}")
    check_block(sess, tag, st.rows, result_rows(verdicts))
    return True


def _start_leg(sess, blob, mk, timeout, budget, tag, args=b"\n"):
    pc, manifest = stage_program(sess, blob, args)
    st = Stream(sess)
    drv = Driver(sess, mk, st, timeout)
    start_async(sess, pc, manifest["stack_top"], budget)
    ok = drv.wait("scanner commits all keys up",
                  lambda s: s["mat"] == bytes(8))
    sess.check(f"{tag}: scanner started", ok, "never saw all keys up")
    return (st, drv) if ok else (None, None)


def leg_guided(sess, blob: bytes, mk: KbdMatrix, timeout: float,
               budget: int, prg: str) -> bool:
    st, drv = _start_leg(sess, blob, mk, timeout, budget, "guided")
    if st is None:
        return False
    for n, (step, script) in enumerate(zip(STEPS, STEP_SCRIPTS), 1):
        tag = f"guided/step{n}"
        if not drv.run(tag, script):
            return False
        rows = wait_rows(drv, rf"STEP {n} ")
        sess.check(f"{tag}: verdict printed", rows is not None, "no STEP row")
        if rows is None:
            return False
        check_block(sess, tag, rows, step_rows(n, step.want, "PASS"))
    if not drv.run("guided/free", FREE_SCRIPT):
        return False
    if not finish(sess, st, "guided", 0, "P" * NSTEP):
        return False
    free = [shown(f"> {FREE_LINES[0]}")] + [shown(fmt_char(c))
                                             for c in FREE_LINES[0]]
    sess.check("guided/free: decoded rows streamed",
               all(r in st.screen_log for r in free),
               f"stream: {st.screen_log[-12:]}")
    sess.check("guided: no column bleed flagged", not st.bleeds,
               "; ".join(b for _, b in st.bleeds))
    judges = {n: judge_step(st, n) for n in range(1, NSTEP + 1)}
    for n in range(1, 5):
        j = judges[n]
        sess.check(f"guided/judge: step {n} performed as asked, no defect "
                   f"({'; '.join(j.notes)})", j.performed and not j.defects,
                   f"performed={j.performed} defects={j.defects}")
    for n in range(5, NSTEP + 1):
        sess.check(f"guided/judge: step {n} NOT performed (no port-1 stick "
                   "in VICE)", not judges[n].performed, "; ".join(
                       judges[n].notes))
    got = judge_run(FIXED, st.verdicts(), judges, range(1, NSTEP + 1))
    sess.check("guided/judge: STEPS all P but steps 5-9 unperformed -> "
               "INCOMPLETE, never PASS", got == "INCOMPLETE", got)
    print(f"  [guided] {mk.ops} snapshot edits")
    return True


def leg_teeth(sess, blob: bytes, mk: KbdMatrix, timeout: float,
              budget: int, prg: str) -> bool:
    st, drv = _start_leg(sess, blob, mk, timeout, budget, "teeth")
    if st is None or not drv.run("teeth/step1", TEETH_SCRIPT):
        return False
    verdicts = "FS" + "S" * (NSTEP - 2)
    if not finish(sess, st, "teeth", 1, verdicts):
        return False
    check_block(sess, "teeth", st.rows, step_rows(1, "^q", "FAIL"))
    check_block(sess, "teeth", st.rows, step_rows(2, "", "SKIP"))
    sess.check("teeth: the driver flagged the column bleed at c6r6",
               any("DOWN c6r6 UPARROW" in b and "c7r6 Q is held" in b
                   for _, b in st.bleeds), f"bleeds: {st.bleeds}")
    j = judge_step(st, 1)
    sess.check("teeth/judge: step 1 performed (Q held) with a defect",
               j.performed and bool(j.defects), f"{j}")
    judges = {n: judge_step(st, n) for n in range(1, NSTEP + 1)}
    for kind, want in ((FIXED, "FAIL"), (CONTROL, "DEFECT SHOWN")):
        got = judge_run(kind, st.verdicts(), judges, range(1, 2))
        sess.check(f"teeth/judge: as a {kind} run: {want}", got == want, got)
    return True


def leg_manner(sess, blob: bytes, mk: KbdMatrix, timeout: float,
               budget: int, prg: str) -> bool:
    st, drv = _start_leg(sess, blob, mk, timeout, budget, "manner")
    if st is None or not drv.run("manner", MANNER_SCRIPT):
        return False
    verdicts = "PSPP" + "S" * (NSTEP - 4)
    if not finish(sess, st, "manner", 0, verdicts):
        return False
    judges = {n: judge_step(st, n) for n in range(1, NSTEP + 1)}
    for n, why in ((1, "Q tapped, not held"), (3, "Z I P one at a time"),
                   (4, "LEFT SHIFT used")):
        sess.check(f"manner/judge: step {n} P on screen but NOT performed "
                   f"({why})", not judges[n].performed,
                   "; ".join(judges[n].notes))
    got = judge_run(FIXED, st.verdicts(), judges, range(1, NSTEP + 1))
    sess.check("manner/judge: run INCOMPLETE (never PASS)",
               got == "INCOMPLETE", got)
    return True


def leg_patch(sess, blob: bytes, mk: KbdMatrix, timeout: float,
              budget: int, prg: str) -> bool:
    """Control patches in VICE: apply, readback, observable effect of
    nosettle (hle_con_settle writes hle_con_sn on every call), remove."""
    from c64_test_harness import read_bytes, write_bytes
    pt = Patcher(sess, prg)
    t = sess.target.transport
    sn = con_label(sess, "hle_con_sn")
    sess.check("patch: runner code clean at start", pt.check_clean())

    def settle_ran() -> bool:
        write_bytes(t, sn, bytes([0xA5]))
        sess._call("hle_con_init")
        sess._call("hle_con_scan")
        return read_bytes(t, sn, 1)[0] != 0xA5
    sess.check("patch: unpatched scan calls the settle routine", settle_ran())
    for name, (off, ln) in CONTROLS.items():
        pt.apply(name)
        sess.check(f"patch: {name} applied (NOPs read back)",
                   read_bytes(t, pt.site + off, ln) == bytes([0xEA] * ln))
        if name == "nosettle":
            sess.check("patch: nosettle scan never calls the settle routine",
                       not settle_ran())
        pt.remove()
        sess.check(f"patch: {name} removed, code clean", pt.check_clean())
    sess.check("patch: settle routine called again", settle_ran())
    return True


HOLD_Q = [press(K("q")), hold(K("q"), secs=LONG_HOLD), press(n=0)]
HOLD_2 = [press(K(";")), hold(K(";"), secs=LONG_HOLD), press(n=0),
          press(K("/")), hold(K("/"), secs=LONG_HOLD), press(n=0)]
LAST_RET = [press(RET)]     # the program ends: no release after it
# nosettle control in VICE (rows recover at once: a device where the
# phantom never happens) -> (name, script, program STEPS, exit, judge)
CONTROL_CASES = [
    ("honest", HOLD_Q + RETURN + HOLD_2 + LAST_RET, "PP", 0, "INCONCLUSIVE"),
    # operator error: a stray W after Q -> F, but no phantom signature
    ("typo", HOLD_Q + tap("w") + RETURN + HOLD_2 + LAST_RET, "FP", 1,
     "INCOMPLETE"),
    # the signature injected: UP-ARROW (c6r6) down in the same sample as Q
    ("phantom", [press(K("q"), K("^"), n=2),
                 hold(K("q"), K("^"), secs=LONG_HOLD), press(n=0)]
     + RETURN + HOLD_2 + LAST_RET, "FP", 1, "DEFECT SHOWN"),
]


def leg_control(sess, blob: bytes, mk: KbdMatrix, timeout: float,
                budget: int, prg: str) -> bool:
    """fix2 F1: a control run is judged by its own defect signature.  The
    nosettle control (real RAM patch, CONTROL arguments) run honestly must
    be INCONCLUSIVE, with a typo INCOMPLETE, and only with the phantom's
    signature DEFECT SHOWN."""
    pt = Patcher(sess, prg)
    r = CONTROL_STEPS["nosettle"]
    for name, script, verdicts, code, want in CONTROL_CASES:
        tag = f"control/{name}"
        pt.apply("nosettle")
        try:
            st, drv = _start_leg(sess, blob, mk, timeout, budget, tag,
                                 control_args("nosettle"))
            if st is None or not drv.run(tag, script):
                return False
            if not finish(sess, st, tag, code, verdicts + "-" * (NSTEP - 2)):
                return False
        finally:
            pt.remove()
        sess.check(f"{tag}: CONTROL banner on screen",
                   shown("*** CONTROL BUILD: nosettle ***") in st.screen_log)
        judges = {n: judge_step(st, n, control=True) for n in r}
        got = judge_run(CONTROL, st.verdicts(), judges, r)
        sess.check(f"{tag}: control verdict {want}", got == want,
                   f"{got}; " + "; ".join(f"step {n}: {j}"
                                          for n, j in judges.items()))
        sess.check(f"{tag}: runner clean after the patch removal",
                   pt.check_clean())
    return True


VICE_LEGS = {"guided": leg_guided, "teeth": leg_teeth, "manner": leg_manner,
             "patch": leg_patch, "control": leg_control}


# --- smoke: queue-fed, any backend ---------------------------------------------

SMOKE_IDLE = 5.0          # s the program waits in Read, matrix scanned


def smoke_input() -> bytes:
    lines = [s.want for s in STEPS] + list(FREE_LINES) + [""]
    return "".join(l + "\r" for l in lines).encode()


def at_prompt(rows: list[str], step: int) -> bool:
    """The screen shows step *step*'s header and, last, the empty prompt."""
    text = [r for r in rows if r]
    return bool(text) and text[-1] == ">" and \
        any(r.startswith(f"STEP {step}/") for r in text)


def leg_smoke(sess, blob: bytes, mhz: int | None, budget: int,
              poll: float) -> None:
    """No key presses: the idle scan must stay silent, then the queue feeds
    every answer and every step must PASS -- and the judge must not call
    that a validation."""
    tag = f"smoke/{mhz}MHz" if mhz else "smoke/vice"
    sess.con_tag = tag
    pc, manifest = stage_program(sess, blob)
    st = Stream(sess)
    vice = sess.target.backend == "vice"

    def spin(pred, secs: float) -> dict:
        end = time.monotonic() + secs
        while True:
            s = st.poll()
            if pred(s) or time.monotonic() > end:
                return s
            if vice:
                sess.target.transport.resume()
            time.sleep(poll)

    t0 = time.monotonic()
    start_async(sess, pc, manifest["stack_top"], budget)
    s = spin(lambda s: s["mat"] == bytes(8) and at_prompt(st.rows, 1),
             sess.timeout)
    ok = s["mat"] == bytes(8) and at_prompt(st.rows, 1)
    sess.check(f"{tag}: program reached step 1's Read, scanner running "
               "(all keys up committed)", ok and not is_done(sess),
               f"hle_con_mat={s['mat'].hex()} rows {st.rows[:6]}")
    if not ok:
        return
    st.emit(f"step 1 prompt after {time.monotonic() - t0:.1f}s; idle "
            f"{SMOKE_IDLE:.0f}s, nobody at the keyboard")
    n0 = s["nkey"]
    s = spin(lambda s: False, SMOKE_IDLE)
    sess.check(f"{tag}: idle {SMOKE_IDLE:.0f}s in Read, matrix scanned: no "
               "key event, still all up", s["nkey"] == n0 == 0
               and s["mat"] == bytes(8),
               f"hle_con_nkey {n0}->{s['nkey']} mat={s['mat'].hex()}")
    queue_keys(sess, smoke_input())
    st.emit("answers queued")
    finish(sess, st, tag, 0, "P" * NSTEP)
    st.emit(f"{tag}: run took {time.monotonic() - t0:.1f}s")
    for n, step in enumerate(STEPS, 1):
        want = [shown(r) for r in step_rows(n, step.want, "PASS")]
        sess.check(f"{tag}: step {n} rows streamed",
                   all(w in st.screen_log for w in want),
                   f"missing {[w for w in want if w not in st.screen_log]}")
    judges = {n: judge_step(st, n) for n in range(1, NSTEP + 1)}
    got = judge_run(FIXED, st.verdicts(), judges, range(1, NSTEP + 1))
    sess.check(f"{tag}: STEPS all P with no key pressed is NOT a validation "
               "(judge: INCOMPLETE)", got == "INCOMPLETE", got)


def leg_patch_roundtrip(sess, prg: str, tag: str) -> None:
    """Device-safe part of the control mechanism: apply and remove each
    control patch in RAM, verified by readback (no program runs)."""
    pt = Patcher(sess, prg)
    sess.check(f"{tag}: runner code clean", pt.check_clean())
    for name in CONTROLS:
        try:
            pt.apply(name)
            pt.remove()
            ok = pt.check_clean()
        except RuntimeError as e:
            ok = False
            print(f"  {e}")
        finally:
            if pt.active is not None:
                pt.remove()
        sess.check(f"{tag}: control patch {name} applied and removed "
                   "(readback)", ok)


# --- manual (U64) -----------------------------------------------------------------

@dataclass
class Run:
    build: str                 # "fixed" or a CONTROLS name
    mhz: int

    @property
    def kind(self) -> str:
        return FIXED if self.build == "fixed" else CONTROL

    @property
    def steps(self) -> range:
        return range(1, NSTEP + 1) if self.kind == FIXED \
            else CONTROL_STEPS[self.build]

    def __str__(self) -> str:
        return f"{self.build}@{self.mhz}"


MANUAL_PLAN = "fixed@48,nosettle@48,nojoymask@48,fixed@1"


def parse_runs(text: str) -> list[Run]:
    out = []
    for part in text.split(","):
        build, _, mhz = part.partition("@")
        if build != "fixed" and build not in CONTROLS:
            raise ValueError(f"--runs: unknown build {build!r}")
        if int(mhz) not in MHZ_OK:
            raise ValueError(f"--runs: {mhz} MHz not in {MHZ_OK}")
        out.append(Run(build, int(mhz)))
    return out


def session_verdict(results: dict[str, str]) -> str:
    """VALIDATED only with the fixed build PASS at 48 and at 1 MHz and each
    control DEFECT SHOWN at 48 MHz; a clean control -> INCONCLUSIVE; a run
    NOT JUDGEABLE counts as INCOMPLETE."""
    need_fixed = ("fixed@48", "fixed@1")
    need_ctl = tuple(f"{c}@48" for c in CONTROLS)
    if any(v == "FAIL" for k, v in results.items() if k.startswith("fixed")):
        return "FAIL"
    if any(results.get(k) != "PASS" for k in need_fixed) or \
            any(results.get(k) in (None, "INCOMPLETE", NOT_JUDGEABLE)
                for k in need_ctl):
        return "INCOMPLETE"
    if all(results[k] == "DEFECT SHOWN" for k in need_ctl):
        return "VALIDATED"
    return "INCONCLUSIVE"


SESSION_MEANING = {
    "VALIDATED": "both defects were visible under streaming, and the "
                 "fixed build was clean under the same conditions",
    "INCONCLUSIVE": "the fixed build passes, but a control's defect was not "
                    "seen under streaming (docs/HLE_CON.md: follow-up is a "
                    "sparse-poll re-run of that control)",
    "FAIL": "the fixed build shows a defect or a wrong character",
    "INCOMPLETE": "a step was skipped or not done as asked, a control "
                  "had no defect signature and an operator error (redo that "
                  "leg), or a run is missing: not validated",
}


def checklist(run: Run, poll: float = POLL_DEFAULT) -> list[str]:
    ctl = run.kind == CONTROL
    out = ["=" * 72]
    if ctl:
        out += [f"CONTROL RUN {run}: THIS BUILD IS SUPPOSED TO SHOW THE "
                "DEFECT", f"(the '{run.build}' fix is taken out of the "
                "runner in RAM; the C64 screen says CONTROL BUILD)"]
    else:
        out += [f"FIXED BUILD at {run.mhz} MHz: every step must PASS"]
    out += ["=" * 72,
            f"Steps {run.steps.start}-{run.steps.stop - 1} of {NSTEP}; each "
            "ends with RETURN and shows PASS or FAIL.",
            "Type ONLY while the '>' prompt shows (keys pressed while the "
            "program prints are lost).",
            "At the run's FIRST '>' prompt: tap LEFT SHIFT once and release "
            "it before anything else", "(the keyboard's LED board hides the "
            "first key hold after ~2 min idle).",
            "NO joystick in control port 2 (it locks the keyboard out by "
            "design).", "Do each step exactly as described: the driver "
            "checks HOW (held time, keys down",
            "at once, which SHIFT, the stick's row).  A skipped step is "
            "never a pass.", ""]
    for n in run.steps:
        s = STEPS[n - 1]
        out += [f"STEP {n}  {s.title}", f"   do:     {s.how}"]
        if ctl:
            out.append(f"   expect (defect): {s.fails_as}")
        else:
            out += [f"   expect: the line {s.want!r}, then STEP {n} PASS",
                    f"   defect: {s.fails_as}"]
    if not ctl and poll_period(poll) > ECHO_GAP_MAX:
        out += ["", f"At --poll {poll:g} steps 3-9 cannot be judged: only "
                "steps 1-2 are evidence.", "RETURN alone at steps 3-9 is "
                "fine (docs/HLE_CON.md, 'The settle A/B')."]
    if not ctl:
        out += ["", "Then FREE mode: type any lines (every key once, with "
                "and without SHIFT);", "an empty line ends the program."]
    out += ["", "This terminal shows what the SCANNER saw ('key DOWN c7r6 "
            "Q'); 'COLUMN BLEED?'", "marks the phantom the row settle must "
            "prevent.  Letters look the same in both", "cases on screen "
            "(upper-case charset); the hex code tells them apart.", "=" * 72]
    return out


class ProgramStuck(RuntimeError):
    """The program never exited, even after CTRL+D was fed: the runner is
    busy inside Read, so nothing more can run in this boot."""


def ask_operator(question: str) -> bool | None:
    """A yes/no question on the host terminal (CONFIRM_S to answer), keys
    typed ahead discarded first.  -> True / False; None: no terminal or no
    answer."""
    import select
    print(f"{question} [y/n] ", end="")
    sys.stdout.flush()
    if not sys.stdin or not sys.stdin.isatty():
        print("(stdin is not a terminal: no answer)")
        return None
    try:
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError):
        pass
    r, _, _ = select.select([sys.stdin], [], [], CONFIRM_S)
    if not r:
        return None
    a = sys.stdin.readline().strip().lower()
    return True if a in ("y", "yes") else False if a in ("n", "no") else None


def leg_manual(sess, blob: bytes, run: Run, budget: int, poll: float,
               max_minutes: float, ask=None) -> str:
    """One operator run -> its judge_run result.  Raises ProgramStuck
    (after the checks and the judge lines) when the program never ended.
    The poll period goes into the run-start line and to the judge (fix5
    (b)).  Every nosettle hold step is put to the operator
    (:func:`hold_question_steps`; *ask*, default :func:`ask_operator`), in
    neutral words, and the answer is logged for the replay."""
    tag = f"manual/{run}"
    for line in checklist(run, poll):
        print(line)
    args = control_args(run.build) if run.kind == CONTROL else b"\n"
    pc, manifest = stage_program(sess, blob, args)
    st = Stream(sess)
    st.poll_s, st.period = poll, poll_period(poll)
    start_async(sess, pc, manifest["stack_top"], budget)
    st.emit(f"{run} started (--poll {poll:g}s): operator, follow the C64 "
            "screen" +
            ("  *** CONTROL: SUPPOSED TO SHOW THE DEFECT ***"
             if run.kind == CONTROL else ""))
    vice = sess.target.backend == "vice"      # dry runs only

    def tick() -> None:
        st.poll()
        if vice:
            sess.target.transport.resume()
        time.sleep(poll)

    deadline = time.monotonic() + max_minutes * 60
    why = "exited"
    try:
        while not is_done(sess):
            if time.monotonic() > deadline:
                why = f"timeout after {max_minutes:g} min"
                break
            tick()
    except KeyboardInterrupt:
        why = "^C"
    if why != "exited":
        st.emit(f"{why}: feeding CTRL+D through the queue to end the program")
        try:
            queue_keys(sess, b"\x04" * 8)
        except ValueError:
            pass
        end = time.monotonic() + 30
        while not is_done(sess) and time.monotonic() < end:
            tick()
    st.poll()
    st.hb_flush()
    done = is_done(sess)
    sess.check(f"{tag}: program ended ({why})", done and why == "exited",
               why if done else f"{why}; program still running")
    if done:
        res = result(sess)
        res.print_state()
        sess.check(f"{tag}: program halted HALT_OK", res.halt != 0 and
                   res.reason == 0 and res.timeout == 0,
                   f"halt={res.halt} reason={res.reason:02X}")
    verdicts = st.verdicts() if done else None
    for n in hold_question_steps(run) if done else []:
        held = " and ".join(KEY_NAMES[K(c)] for c in STEPS[n - 1].keys)
        yes = (ask or ask_operator)(
            f"{run} step {n}: did you hold {held} at the '>' prompt as the "
            "checklist asked?  (y = yes; n = no, or not sure; the answer "
            "is logged only, it does not change the verdict)")
        st.emit(f"operator: step {n} held as asked ({held})? "
                + {True: "yes", False: "no", None: "no answer"}[yes])
        if yes:
            st.confirmed.add(n)
        elif yes is False:
            st.denied.add(n)
    judges = {n: judge_step(st, n, control=run.kind == CONTROL)
              for n in run.steps}
    for n, j in judges.items():
        v = verdicts[n - 1] if verdicts else "?"
        st.emit(f"step {n} {STEPS[n - 1].title}: program {v}; performed "
                f"{'yes' if j.performed else 'NO'}; " + "; ".join(j.notes)
                + (f"; NOT JUDGEABLE: {j.not_judgeable}"
                   if j.not_judgeable else "")
                + (f"; DEFECT: {' | '.join(j.defects)}" if j.defects else ""))
    got = judge_run(run.kind, verdicts, judges, run.steps)
    want = "PASS" if run.kind == FIXED else "DEFECT SHOWN"
    st.emit(f"{run}: {got} (want {want}), STEPS {verdicts}")
    if got == NOT_JUDGEABLE:
        st.emit(f"{run}: a step could not be judged at this poll (see "
                "NOT JUDGEABLE above): not operator error, not a validation")
    if run.kind == CONTROL and got == "INCOMPLETE":
        st.emit(f"{run}: no defect signature, and a step was not done as "
                "asked or not P: operator error, REDO THIS LEG "
                f"(--runs {run})")
    sess.check(f"{tag}: {want}", got == want, got)
    if not done:
        raise ProgramStuck(f"{run}: the program never ended ({why})")
    return got


# --- main ---------------------------------------------------------------------------

class Tee:
    def __init__(self, path: str, stream):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.f = open(path, "x", buffering=1)   # one log per session
        self.s = stream

    def write(self, data: str) -> int:
        self.s.write(data)
        self.f.write(data)
        return len(data)

    def flush(self) -> None:
        self.s.flush()
        self.f.flush()


MHZ_OK = (1, 48)


_OUT_SEQ = iter(range(1, 1 << 30))


def default_out_dir(stamp: str | None = None) -> str:
    """A new evidence directory per u64 session (fix5 F7), unique per call
    (fix6 R5: time, process and a counter)."""
    return os.path.join(PROJECT_ROOT, "build", "evidence", "kbd_echo-u64-"
                        + (stamp or f"{time.strftime('%Y%m%d-%H%M%S')}-"
                           f"{os.getpid()}-{next(_OUT_SEQ)}"))


def parse_mhz(text: str | None) -> list[int]:
    if not text:
        return []
    out = []
    for part in text.split(","):
        v = int(part)
        if v not in MHZ_OK:
            raise ValueError(f"--mhz: {v} not in {MHZ_OK}")
        out.append(v)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.kbd_echo",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", help="runner PRG (required unless --replay)")
    ap.add_argument("--replay", nargs="+", metavar="LOG",
                    help="re-judge the runs of session.log files offline, "
                    "each session on its own (no emulator, no device)")
    ap.add_argument("--assume-poll", type=float, metavar="S",
                    help="--replay: the --poll of logs that record neither "
                    "--poll nor a heartbeat (before fix4); without it their "
                    "timing is unknown.  Never enough to VALIDATE")
    ap.add_argument("--labels", help="ld65 -Ln label file (default: PRG .lbl)")
    ap.add_argument("--backend", default="vice", choices=["vice", "u64"])
    ap.add_argument("--legs", default=",".join(VICE_LEGS),
                    help=f"VICE legs, of {','.join(VICE_LEGS)} (--smoke runs "
                    "the smoke leg only)")
    ap.add_argument("--smoke", action="store_true",
                    help="queue-fed smoke, no key presses (VICE or U64)")
    ap.add_argument("--mhz", help="smoke on u64: CPU speed(s), comma-"
                    "separated, each 1 or 48 (required there)")
    ap.add_argument("--runs", default=MANUAL_PLAN,
                    help=f"manual u64: BUILD@MHZ,... (BUILD fixed or a "
                    f"control: {', '.join(CONTROLS)}; default {MANUAL_PLAN}; "
                    "only the default plan can be VALIDATED)")
    ap.add_argument("--timeout", type=float,
                    help="per call / per wait, seconds (default 60 VICE, "
                    "600 u64: ~1 600 68k instructions/s at 1 MHz)")
    ap.add_argument("--budget", type=int, default=50_000_000,
                    help="guest instructions per run (Read waits cost none)")
    ap.add_argument("--poll", type=float, default=0.1,
                    help="u64: stream poll period, seconds (default 0.1)")
    ap.add_argument("--max-minutes", type=float, default=30.0,
                    help="manual: wall-clock limit per run (default 30)")
    ap.add_argument("--expect-git", default=EXPECT_GIT,
                    help=f"u64: refuse to run unless /v1/info reports this "
                    f"git_commit_hash (default {EXPECT_GIT}, the resting "
                    "build)")
    ap.add_argument("--expect-core", default=EXPECT_CORE,
                    help=f"u64: ... and this core_version (default "
                    f"{EXPECT_CORE})")
    ap.add_argument("--fw-wait-hours", type=float, default=12.0,
                    help="u64 smoke: how long to keep re-queueing while the "
                    "device is busy or has another firmware (default 12; "
                    "the manual mode never waits)")
    ap.add_argument("--out", default=os.path.join(
        PROJECT_ROOT, "build", "vbcc", f"kbd_echo-{os.getpid()}"),
        help="build dir for the compiled program (default per process)")
    ap.add_argument("--out-dir",
                    help="u64: session.log + config_before/after.json, one "
                    "session per directory (default: a new "
                    "build/evidence/kbd_echo-u64-<date>-<time>)")
    args = ap.parse_args(argv)
    if args.replay:
        res = replay_report(args.replay, assume_poll=args.assume_poll)
        # as live: only a VALIDATED session exits 0, and a log is only as
        # good as its worst session
        return 0 if all(file_verdict([s.verdict for s in r]) == "VALIDATED"
                        for r in res.values()) else 1
    if not args.prg:
        print("error: --prg is required", file=sys.stderr)
        return 2
    if args.timeout is None:
        args.timeout = 600.0 if args.backend == "u64" else 60.0
    try:
        mhzs = parse_mhz(args.mhz)
        runs = parse_runs(args.runs)
        legs = args.legs.split(",")
        for leg in legs:
            if leg not in VICE_LEGS:
                raise ValueError(f"--legs: unknown leg {leg!r}")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.backend == "u64":
        if args.smoke and not mhzs:
            print("error: --smoke on u64 needs --mhz (1, 48 or both)",
                  file=sys.stderr)
            return 2
        if not args.smoke and mhzs:
            print("error: the manual mode takes --runs, not --mhz",
                  file=sys.stderr)
            return 2
        if not os.environ.get("U64_HOST"):
            print("error: set U64_HOST", file=sys.stderr)
            return 2
        args.out_dir = args.out_dir or default_out_dir()
        if os.path.isdir(args.out_dir) and os.listdir(args.out_dir):
            # one log per session (fix5 F7), and no evidence of another
            # session overwritten (fix6 R5: config_before/after.json)
            print(f"error: {args.out_dir} is not empty; give each session "
                  "its own --out-dir", file=sys.stderr)
            return 2
    elif mhzs:
        print("error: --mhz is for --backend u64 (VICE has no CPU speed)",
              file=sys.stderr)
        return 2

    prg = os.path.abspath(args.prg)
    labels_path = (os.path.abspath(args.labels) if args.labels
                   else os.path.splitext(prg)[0] + ".lbl")
    for path in (prg, labels_path):
        if not os.path.exists(path):
            print(f"error: not found: {path}", file=sys.stderr)
            return 2
    from tests.run_vbcc import compile_c
    os.makedirs(args.out, exist_ok=True)
    blob = compile_c(PROG, args.out)
    print(f"compiled {PROG}: {len(blob)} bytes")

    from c64_test_harness import Labels
    from tests.run_program import Session
    from tests.run_vectors import prepare_target
    labels = Labels.from_file(labels_path)
    sess = Session(target=None, labels=labels, timeout=args.timeout)

    if args.backend == "vice":
        from c64_test_harness import UnifiedManager, ViceConfig
        from tests.harness_util import REU_VICE_ARGS
        cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                         extra_args=list(REU_VICE_ARGS))
        with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
            target = mgr.acquire()
            mk = None
            try:
                prepare_target(target, prg)
                sess.target = target
                if args.smoke:
                    leg_smoke(sess, blob, None, args.budget, 0.05)
                    leg_patch_roundtrip(sess, prg, "smoke/vice")
                else:
                    scratch = os.path.join(os.path.dirname(prg), "kbd-matrix")
                    os.makedirs(scratch, exist_ok=True)
                    mk = KbdMatrix(target.transport, scratch)
                    for leg in legs:
                        if not VICE_LEGS[leg](sess, blob, mk, args.timeout,
                                              args.budget, prg):
                            # the machine is inside a hung run: nothing
                            # after this is trustworthy
                            sess.check(f"{leg}: leg completed", False,
                                       "later legs not run")
                            break
            finally:
                if mk is not None:
                    mk.close()
                mgr.release(target)
    else:
        sess = run_u64(args, prg, labels, blob, mhzs, runs)

    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    for f in sess.failures:
        print(f"FAIL  {f}")
    return 1 if n_bad else 0


# The resting firmware the suites are validated on (docs/HARDWARE.md).  A
# session on anything else (a spike or test image of another project) would
# not be comparable, so the driver refuses to run on it.
EXPECT_GIT = "c47155b7"
EXPECT_CORE = "1.50"
FW_REQUEUE_S = 300.0      # wait before re-queueing (smoke)
LOCK_FAST_S = 10.0        # manual: how long to try for the lock
CONFIRM_S = 300.0         # manual: how long to wait for the operator


def firmware_ok(info: dict, git: str, core: str) -> bool:
    return info.get("git_commit_hash") == git and \
        info.get("core_version") == core


def fw_line(info: dict) -> str:
    return (f"{info.get('product')} fw {info.get('firmware_version')} git "
            f"{info.get('git_commit_hash')} fpga {info.get('fpga_version')} "
            f"core {info.get('core_version')}")


def firmware_gate(host: str, git: str, core: str, max_hours: float, log,
                  fail_fast: bool = False):
    """Hold the device's DeviceLock (nestable: u64_hold's manager joins
    this hold, so nobody can reflash between the check and the run) with
    the resting firmware on it.  Under the lock the only device call is
    one bodyless GET /v1/info.  Wrong firmware, or the GET failing: the
    lock is released at once (in a finally: nothing written, no reset) and
    logged.  *fail_fast* (manual): a busy lock or a wrong firmware returns
    at once, naming the holder.  Otherwise (smoke): wait FW_REQUEUE_S and
    queue again, until *max_hours*.
    -> (lock, info), or (None, reason dict)."""
    from c64_test_harness import (DeviceLock, DeviceLockTimeout,
                                  Ultimate64Client)
    from c64_test_harness.backends.device_lock import device_key
    deadline = time.monotonic() + max_hours * 3600
    while True:
        lock = DeviceLock(device_key(host), allow_nested=True)
        try:
            if fail_fast:
                lock.acquire_or_raise(timeout=LOCK_FAST_S,
                                      progress_window=None)
            else:
                lock.acquire_or_raise(timeout=max(
                    60.0, deadline - time.monotonic()))
        except DeviceLockTimeout as e:
            log(f"firmware gate: device lock busy: {e}")
            return None, {"error": f"lock busy: {e}",
                          "holder_pid": getattr(e, "holder_pid", None)}
        keep, info, err = False, {}, None
        try:
            client = Ultimate64Client(host)
            try:
                info = client.get_info()
            finally:
                client.close()
            if firmware_ok(info, git, core):
                log(f"firmware gate: {fw_line(info)} = resting build "
                    f"(git {git}, core {core}): proceeding")
                keep = True
                return lock, info
        except Exception as e:  # noqa: BLE001 - a transient network error
            err = e
        finally:
            if not keep:
                lock.release()
        why = (f"GET /v1/info failed: {err!r}" if err is not None else
               f"{fw_line(info)} is NOT the resting build (git {git}, "
               f"core {core})")
        if fail_fast:
            log(f"firmware gate: {why}: lock released, nothing run or "
                "written; not waiting (manual mode)")
            return None, info or {"error": why}
        log(f"firmware gate: {why}: lock released, nothing run or written; "
            f"re-queueing in {FW_REQUEUE_S:.0f}s")
        if time.monotonic() + FW_REQUEUE_S > deadline:
            log(f"firmware gate: giving up after {max_hours:g} h")
            return None, info or {"error": why}
        time.sleep(FW_REQUEUE_S)


def confirm(runs: list[Run], log) -> bool:
    """Manual: the operator must be at the C64 before anything is written
    (the runner upload is next).  Enter within CONFIRM_S; else abort."""
    import select
    print("\n".join(["", "=" * 72, "MANUAL KEYBOARD SESSION: runs in this "
                     "order: " + ", ".join(str(r) for r in runs),
                     "Controls are SUPPOSED to show the defect.",
                     "The runner will now be uploaded (run_prg) and the "
                     "config changed (restored at the end).",
                     f"Press Enter when the operator is at the C64 keyboard "
                     f"(abort in {CONFIRM_S:.0f}s)...", "=" * 72]))
    sys.stdout.flush()
    if not sys.stdin or not sys.stdin.isatty():
        log("confirmation: stdin is not a terminal: aborting (use --yes "
            "only for dry runs)")
        return False
    r, _, _ = select.select([sys.stdin], [], [], CONFIRM_S)
    if not r or not sys.stdin.readline():
        log("confirmation: none: aborting, nothing written")
        return False
    log("confirmation: operator ready")
    return True


def run_u64(args, prg: str, labels, blob: bytes, mhzs: list[int],
            runs: list[Run]):
    from tests.bench.u64_session import log, u64_hold
    from tests.run_dos import hold_checks, zero_dos_bss
    from tests.run_program import Session
    from tests.run_vectors import prepare_target
    out_dir = os.path.abspath(args.out_dir)
    sys.stdout = Tee(os.path.join(out_dir, "session.log"), sys.__stdout__)
    manual = not args.smoke
    log(f"kbd_echo {'MANUAL' if manual else 'smoke (queue-fed, no human)'} "
        f"on {os.environ['U64_HOST']}, " +
        (f"runs {', '.join(str(r) for r in runs)}" if manual
         else f"speeds {mhzs}") + f", poll {args.poll:g}s, prg {prg}")
    sess = Session(target=None, labels=labels, timeout=args.timeout)
    results: dict[str, str] = {}
    gate, info = firmware_gate(os.environ["U64_HOST"], args.expect_git,
                               args.expect_core, args.fw_wait_hours, log,
                               fail_fast=manual)
    if gate is None:
        sess.check("u64: device free and on the resting firmware", False,
                   info.get("error") or fw_line(info))
        return sess
    try:
        # no CLI switch bypasses this (args.yes exists only for offline
        # tests that build their own namespace)
        if manual and not getattr(args, "yes", False) and \
                not confirm(runs, log):
            sess.check("u64: operator confirmed", False, "no confirmation")
            return sess
        run_held(args, prg, labels, blob, mhzs, runs, out_dir, sess, results,
                 u64_hold, log, hold_checks, zero_dos_bss, prepare_target)
    finally:
        gate.release()
    if manual:
        for k, v in results.items():
            log(f"{k}: {v}")
        verdict = session_verdict(results) if args.runs == MANUAL_PLAN \
            else "INCOMPLETE (not the full plan)"
        log(f"SESSION VERDICT: {verdict}: "
            f"{SESSION_MEANING.get(verdict.split()[0], '')}")
        sess.check(f"session verdict VALIDATED", verdict == "VALIDATED",
                   verdict)
    return sess


def run_held(args, prg, labels, blob, mhzs, runs, out_dir, sess, results,
             u64_hold, log, hold_checks, zero_dos_bss, prepare_target):
    """Everything inside u64_hold (joins the firmware gate's lock).  Every
    config item the run depends on is set explicitly (a flash resets the
    device settings): CPU speed per run (Turbo Control through set_speed),
    REU enabled / 16 MB in prepare_target (set_reu)."""
    with u64_hold(out_dir) as hold:
        info = hold["info"]
        log(f"device: {fw_line(info)} (lock wait "
            f"{hold['lock_wait_s']:.0f}s)")
        target = hold["target"]
        sess.target = target
        todo = mhzs if args.smoke else runs
        if not firmware_ok(info, args.expect_git, args.expect_core):
            sess.check("u64: firmware re-checked under the hold", False,
                       fw_line(info))
            todo = []
        else:
            try:
                target.transport.set_speed(1)
                log("CPU speed 1 MHz for the boot; REU enabled 16 MB next "
                    "(prepare_target: set_reu)")
                prepare_target(target, prg)
                zero_dos_bss(sess, labels)  # RAMDISK only, no storage writes
                patcher = Patcher(sess, prg)
                sess.check("u64: runner code clean (no control patch)",
                           patcher.check_clean())
            except Exception as e:  # noqa: BLE001 - report, restore anyway
                sess.check("u64: runner booted, RAMDISK proven", False,
                           f"{type(e).__name__}: {e}")
                todo = []
        for item in todo:
            mhz = item if args.smoke else item.mhz
            target.transport.set_speed(mhz)
            time.sleep(0.5)
            log(f"CPU speed set to {mhz} MHz")
            try:
                if args.smoke:
                    leg_smoke(sess, blob, mhz, args.budget, args.poll)
                    leg_patch_roundtrip(sess, prg, f"smoke/{mhz}MHz")
                else:
                    if item.kind == CONTROL:
                        patcher.apply(item.build)
                        log(f"control patch {item.build} applied (readback "
                            "ok): THIS RUN IS SUPPOSED TO SHOW THE DEFECT")
                    elif not patcher.check_clean():
                        raise RuntimeError("runner code is not the original "
                                           f"before the fixed run {item}")
                    stuck = False
                    try:
                        results[str(item)] = leg_manual(
                            sess, blob, item, args.budget, args.poll,
                            args.max_minutes)
                    except ProgramStuck:
                        # the CPU may be executing the patched bytes: do not
                        # rewrite them; the C64 reset of the restore drops
                        # the RAM patch with the whole runner
                        stuck = True
                        raise
                    finally:
                        if patcher.active is not None and not stuck:
                            patcher.remove()
                            log("control patch removed (readback ok)")
                        elif patcher.active is not None:
                            log("control patch LEFT in RAM of a stuck "
                                "machine: the restore's C64 reset drops it")
            except Exception as e:  # noqa: BLE001 - report, restore anyway
                sess.check(f"{item}: ran", False, f"{type(e).__name__}: {e}")
                break
        target.transport.set_speed(1)
    hold_checks(sess, hold)


if __name__ == "__main__":
    sys.exit(main())
