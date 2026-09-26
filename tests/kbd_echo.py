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
column bleed, a key on the stick's row reading down.

Verdicts (:func:`judge_run`):
  fixed build   PASS only when every step is P, performed as asked, with no
                defect; FAIL on any F or defect; else INCOMPLETE (a SKIP is
                never a pass).
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
``--out-dir``/session.log, written as it goes.

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
    Step("HOLD Q", "hold Q for 2 s, release it, press RETURN", "q",
         "^q: the UP-ARROW glyph ($5E, c6r6, read in the same sample as Q) "
         "then q; or nothing (the phantom keeps restarting the debounce)",
         "hold", keys="q"),
    Step("HOLD ; AND /",
         "hold ; for 2 s, release; hold / for 2 s, release; RETURN", ";/",
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
                 "hle_con_raw")
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
    a replay can prove that polling was continuous."""

    def __init__(self, sess, out=print):
        self.sess = sess
        self.out = out
        self.at = {n: con_label(sess, n) for n in STREAM_LABELS}
        missing = [n for n, a in self.at.items() if a is None]
        if missing:
            raise RuntimeError(f"PRG lacks console labels {missing}")
        self.lo = min(self.at.values())
        self.span = max(self.at.values()) + 8 - self.lo
        self.t0 = time.monotonic()
        self.hb_t = self.last_poll = self.t0
        self.hb_n, self.hb_gap = 0, 0.0
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
                "raw": g("hle_con_raw", 8)}

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
        self.hb_gap = max(self.hb_gap, now - self.last_poll)
        self.last_poll = now
        if now - self.hb_t >= HB_EVERY:
            _log_only(f"[{now - self.t0:7.2f}] hb polls +{self.hb_n} max "
                      f"gap {self.hb_gap:.2f}s")
            self.hb_t, self.hb_n, self.hb_gap = now, 0, 0.0
        s = self.read_state()
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
                                       raw=frozenset(mat_bits(s["raw"]))))
            # diagnostic (fix3): keys in the latest raw sample that the
            # debounce has not committed -- a key held but never settled
            # (the "nothing" form of a phantom) shows here, a key never
            # pressed does not
            extra = {i for i in mat_bits(s["raw"]) if i not in MODS} - \
                mat_bits(s["mat"])
            if extra != self.raw_extra:
                if extra:
                    self.emit("raw (not debounced yet): " +
                              ", ".join(pos(i) for i in sorted(extra)))
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
RUN_START = re.compile(r"^(\S+@\d+) started: ")
RUN_END = re.compile(r"^(\S+@\d+): (PASS|FAIL|INCOMPLETE|DEFECT SHOWN|"
                     r"INCONCLUSIVE) \(want")
KEY_LINE = re.compile(r"^(key|mod) (DOWN|UP)\s+c(\d)r(\d) ")
REL_LINE = re.compile(r"^\(\+(\d+) key event")
MASK_LINE = re.compile(r"^\((\d+) key\(s\) down without an event")
JOY_LINE = re.compile(r"^port-1 joystick: (?:rows ((?:r\d ?)+) held low|"
                      r"released)")
FILL = 0.1                 # s between synthetic samples
POLL_GUARD = 0.25          # s: a live poll period (0.1 s sleep + the REST
                           # reads); the live stream's last sample before a
                           # logged change is up to this much earlier, so no
                           # synthetic sample is placed there (holds are not
                           # over-measured)


def replay_stream() -> "Stream":
    """A Stream with no session: fed from a log by replay_log()."""
    st = Stream.__new__(Stream)
    st.samples, st.bleeds, st.screen_log, st.log = [], [], [], []
    st.step, st.last_hdr = None, 0
    st.rows = [""] * ROWS
    st.hb_seen, st.hb_worst = 0, 0.0
    st.t0 = st.hb_t = st.last_poll = time.monotonic()
    st.hb_n, st.hb_gap = 0, 0.0
    st.out = print
    st.continuity = "unproven (no heartbeat in this log)"
    return st


def _continuity(st) -> str:
    if not st.hb_seen:
        return "unproven (no heartbeat in this log)"
    if st.hb_worst > ECHO_GAP_MAX:
        return (f"BROKEN (a poll gap of {st.hb_worst:.2f}s > "
                f"{ECHO_GAP_MAX}s: the replay cannot vouch for the state "
                "between)")
    return f"proven ({st.hb_seen} heartbeat(s), max gap {st.hb_worst:.2f}s)"


def replay_log(path: str) -> list[tuple[Run, "Stream", str]]:
    """Rebuild each run's stream from a session.log: every state change
    the live stream printed (key DOWN/UP with the scanned position, +n
    events a poll missed, the port-1 rows, the mirrored screen rows) at its
    timestamp, plus synthetic samples every FILL s in between (the state is
    constant there).  -> [(run, stream, verdict the live driver logged)]."""
    runs, st, run = [], None, None
    keys, idle, nkey, line, t_last = set(), 0xFF, 0, None, 0.0

    def sample(t):
        st.samples.append(Sample(t, st.step, frozenset(keys), idle, nkey,
                                 line))
    with open(path, errors="replace") as f:
        for raw in f:
            m = LOG_LINE.match(raw.rstrip("\n"))
            if not m:
                continue
            t, msg = float(m[1]), m[2]
            if st is None:
                ms = RUN_START.match(msg)
                if ms:
                    run = parse_runs(ms[1])[0]
                    st = replay_stream()
                    keys, idle, nkey, line, t_last = set(), 0xFF, 0, None, t
                continue
            me = RUN_END.match(msg)
            if me:
                st.continuity = _continuity(st)
                runs.append((run, st, me[2]))
                st = None
                continue
            mh = HB_LINE.match(msg)
            if mh:
                st.hb_seen += 1
                st.hb_worst = max(st.hb_worst, float(mh[2]))
                continue
            if t > t_last:                       # a new poll
                k = t_last + FILL
                while k < t - POLL_GUARD:
                    sample(k)
                    k += FILL
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
            if st.samples:
                s0 = st.samples[-1]
                st.samples[-1] = Sample(s0.t, s0.step, frozenset(keys), idle,
                                        nkey, s0.line)
    return runs


def replay_report(paths: list[str], out=print) -> dict[str, dict[str, str]]:
    """Re-judge every run of each log; -> {path: {run: result}}."""
    res = {}
    for path in paths:
        out(f"== {path}")
        res[path] = {}
        for run, st, logged in replay_log(path):
            judges = {n: judge_step(st, n, control=run.kind == CONTROL)
                      for n in run.steps}
            v = st.verdicts()
            got = judge_run(run.kind, v, judges, run.steps)
            if st.continuity.startswith("BROKEN"):
                got = "INCOMPLETE"
            res[path][str(run)] = got
            out(f"{run}: logged {logged}; replayed {got}; STEPS {v}; "
                f"polling {st.continuity}")
            for n, j in judges.items():
                out(f"   step {n} {STEPS[n - 1].title}: performed "
                    f"{'yes' if j.performed else 'NO'}; {'; '.join(j.notes)}"
                    + (f"; DEFECT {' | '.join(j.defects)}" if j.defects
                       else "")
                    + (f"; SIGNATURE {' | '.join(j.signature)}"
                       if j.signature else ""))
        v = session_verdict(res[path])
        out(f"SESSION VERDICT (replayed; the last run of each name counts): "
            f"{v}: {SESSION_MEANING.get(v, '')}")
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


def _runs(samples: list[Sample], key: int) -> float:
    """Longest time *key* stayed down over consecutive samples."""
    best, start = 0.0, None
    for s in samples:
        if key in s.keys:
            start = s.t if start is None else start
            best = max(best, s.t - start)
        else:
            start = None
    return best


def _first_down(samples: list[Sample], key: int) -> Sample | None:
    return next((s for s in samples if key in s.keys), None)


ECHO_GAP_MAX = 0.4         # s: longest poll gap an echo credit may span
                           # (real sessions: 0.33 s at most; fix4)


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
      no events behind it and is not a keystroke."""
    out, base = [], ""
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        if b.line is None or b.line == base:
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


def held_throughout(samples: list[Sample], spans, cond) -> float | None:
    """None when every poll from the first span's start to the last span's
    end satisfies cond, else the time of the first poll that does not
    (fix4 continuity: letting go between two letters is not a hold)."""
    lo, hi = min(a for a, _ in spans), max(b for _, b in spans)
    return next((samples[i].t for i in range(lo, hi + 1)
                 if not cond(samples[i])), None)


def judge_step(stream: Stream, n: int, control: bool = False) -> StepJudge:
    """How step *n* was done, from the stream's samples tagged *n*.  A
    control run needs only the triggering action (e.g. the stick held):
    the broken build may garble the typing itself."""
    step = STEPS[n - 1]
    ss = [s for s in stream.samples if s.step == n]
    notes, defects, sig = [], [], []
    ok = True
    nonmod = lambda s: {i for i in s.keys if i not in MODS}
    if step.kind == "hold":
        for ch in step.keys:
            k = K(ch)
            held = _runs(ss, k)
            notes.append(f"{KEY_NAMES[k]} held {held:.1f}s")
            ok &= held >= HOLD_MIN
            extra = {i for s in ss if k in s.keys for i in nonmod(s)} - {k}
            if extra:
                defects.append(f"with {KEY_NAMES[k]} held, also down: "
                               + ", ".join(pos(i) for i in sorted(extra)))
            # nosettle signature: the key one column lower on the same row
            # goes down in the SAME sample as the held key
            c, r = divmod(k, 8)
            ph = (c - 1) * 8 + r if c > 0 else None
            first = next((i for i, s in enumerate(ss) if k in s.keys), None)
            if ph is not None and ph not in MODS and first is not None and \
                    ph in ss[first].keys and \
                    (first == 0 or ph not in ss[first - 1].keys):
                sig.append(f"phantom {pos(ph)} went down with "
                           f"{KEY_NAMES[k]} (settle signature)")
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
                                 f"(released at t={gap:.2f})")
                    ok = False
        rowkeys = {i for s in held for i in s.keys if i % 8 == r}
        if rowkeys:
            line = (f"stick on r{r}: keys on that row read down: "
                    + ", ".join(pos(i) for i in sorted(rowkeys)))
            defects.append(line)
            sig.append(line + " (mask signature)")
    defects += [line for st, line in stream.bleeds if st == n]
    return StepJudge(ok and bool(ss), defects, notes, sig)


FIXED, CONTROL = "fixed", "control"


def judge_run(kind: str, verdicts: str | None, judges: dict[int, StepJudge],
              steps: range) -> str:
    """-> PASS / FAIL / INCOMPLETE (fixed build) or DEFECT SHOWN /
    INCONCLUSIVE / INCOMPLETE (control).  *verdicts*: the program's STEPS
    string (None: it never printed one)."""
    if verdicts is None or len(verdicts) != NSTEP:
        return "INCOMPLETE"
    v = {n: verdicts[n - 1] for n in steps}
    if kind == FIXED:
        if any(x == "F" for x in v.values()) or \
                any(judges[n].defects for n in steps):
            return "FAIL"
        if all(x == "P" for x in v.values()) and \
                all(judges[n].performed for n in steps):
            return "PASS"
        return "INCOMPLETE"
    # control: only the control's own signature counts as its defect; any
    # other F, S or stray key is operator error (redo the leg)
    if not all(judges[n].performed for n in steps):
        return "INCOMPLETE"
    if any(judges[n].signature for n in steps):
        return "DEFECT SHOWN"
    if any(v[n] != "P" or judges[n].defects for n in steps):
        return "INCOMPLETE"
    return "INCONCLUSIVE"


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
    control DEFECT SHOWN at 48 MHz; a clean control -> INCONCLUSIVE."""
    need_fixed = ("fixed@48", "fixed@1")
    need_ctl = tuple(f"{c}@48" for c in CONTROLS)
    if any(v == "FAIL" for k, v in results.items() if k.startswith("fixed")):
        return "FAIL"
    if any(results.get(k) != "PASS" for k in need_fixed) or \
            any(results.get(k) in (None, "INCOMPLETE") for k in need_ctl):
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


def checklist(run: Run) -> list[str]:
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


def leg_manual(sess, blob: bytes, run: Run, budget: int, poll: float,
               max_minutes: float) -> str:
    """One operator run -> its judge_run result.  Raises ProgramStuck
    (after the checks and the judge lines) when the program never ended."""
    tag = f"manual/{run}"
    for line in checklist(run):
        print(line)
    args = control_args(run.build) if run.kind == CONTROL else b"\n"
    pc, manifest = stage_program(sess, blob, args)
    st = Stream(sess)
    start_async(sess, pc, manifest["stack_top"], budget)
    st.emit(f"{run} started: operator, follow the C64 screen" +
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
    done = is_done(sess)
    sess.check(f"{tag}: program ended ({why})", done and why == "exited",
               why if done else f"{why}; program still running")
    if done:
        res = result(sess)
        res.print_state()
        sess.check(f"{tag}: program halted HALT_OK", res.halt != 0 and
                   res.reason == 0 and res.timeout == 0,
                   f"halt={res.halt} reason={res.reason:02X}")
    judges = {n: judge_step(st, n, control=run.kind == CONTROL)
              for n in run.steps}
    verdicts = st.verdicts() if done else None
    for n, j in judges.items():
        v = verdicts[n - 1] if verdicts else "?"
        st.emit(f"step {n} {STEPS[n - 1].title}: program {v}; performed "
                f"{'yes' if j.performed else 'NO'}; " + "; ".join(j.notes)
                + (f"; DEFECT: {' | '.join(j.defects)}" if j.defects else ""))
    got = judge_run(run.kind, verdicts, judges, run.steps)
    want = "PASS" if run.kind == FIXED else "DEFECT SHOWN"
    st.emit(f"{run}: {got} (want {want}), STEPS {verdicts}")
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
        self.f = open(path, "a", buffering=1)
        self.s = stream

    def write(self, data: str) -> int:
        self.s.write(data)
        self.f.write(data)
        return len(data)

    def flush(self) -> None:
        self.s.flush()
        self.f.flush()


MHZ_OK = (1, 48)


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
                    help="re-judge the runs of session.log files offline "
                    "(no emulator, no device)")
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
    ap.add_argument("--out-dir", default=os.path.join(
        PROJECT_ROOT, "build", "evidence", "kbd_echo-u64"),
        help="u64: session.log + config_before/after.json")
    args = ap.parse_args(argv)
    if args.replay:
        res = replay_report(args.replay)
        # as live: only a VALIDATED session exits 0
        return 0 if all(session_verdict(r) == "VALIDATED"
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
         else f"speeds {mhzs}") + f", prg {prg}")
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
