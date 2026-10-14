# Console input (W5-K)

> **Historical (Wave 17).** This is the Waves 5-6 design of the HLE console input, kept for its reasoning and
> measurements. The current description is [docs/emulation/HLE.md](emulation/HLE.md); where they differ,
> [docs/CONTRACTS.md](CONTRACTS.md) and the code win. It still describes program mode as built; the statements that are out of date are listed in that file.

Owner: w5-con (Wave 5). Code: `runtime/ops/hle_con.s`. Contract:
`docs/WAVE5_BRIEF.md` C2 and its amendment. Tests: `tests/run_con.py`,
`host/tests/test_hle_con.py`, `fixtures/c/con_*.c`,
`fixtures/c/hle_unit/u_con*.s`.

`dos.library/Read` on the console handles (`fh_Args` kind 0 console out and
kind 1 console in, see `docs/HLE.md` "FileHandles") returns keyboard input.
Before W5-K it always returned 0 (EOF). `hle.s` `hle_read` jumps to
`hle_con_read` with the call state untouched: D1 = handle BPTR, D2 =
buffer, D3 = length, return address on the guest stack. Only D0 is written,
and the call retires through `hle_ret`.

## Line discipline (cooked mode, like an AmigaOS `CON:` window)

| Key code | Effect |
|---|---|
| `$20-$7E` printable | appended to the line and echoed; once the line holds 127 characters, more are dropped |
| `$0D` RETURN (or `$0A`) | appends `"\n"`, echoes a newline, and the line is finished |
| `$08` BS / `$7F` DEL | removes the last character and rubs it out on screen; on an empty line it does nothing |
| `$1C` Ctrl-\ / `$04` Ctrl-D | EOF: see below |
| anything else | ignored |

- **Echo** goes through `hle.s`'s own console output (C2 amendment):
  `hle_con_load` → `hle_putc` → `hle_con_save`. That means the same cursor
  in guest `ENV_STATE+4`, the same screen-code mapping and the same lazy
  scroll as `Write`. So prompts and echo interleave exactly as the program
  wrote them. The rubout steps the shared cursor back one cell and blanks
  it: from column 0 it goes to column 39 of the row above, and from the
  scroll-pending position 1000 it goes to 999. A line that fills column 39
  wraps like any `Write` output, and its RETURN then leaves an empty row,
  which is the same rule as `Write` of 40 characters plus `"\n"`.
- **Read(fh, buf, len)** waits until a line is finished (or EOF). It then
  copies `min(len, rest of line)` bytes to `buf` through `mem_write8` and
  returns that count. **The rest stays** for the next Read, and no new key
  is read until the line is used up. A Read never returns more than one
  line. vbcc's vc.lib reads an interactive stdin in 1-byte Reads for
  `getchar`/`fgets` and in `size - 1`-byte Reads with a `setvbuf` buffer,
  so this path is used all the time.
- **Arguments**: D3 = 0 → 0, returned at once, with nothing consumed and no
  wait. D3 negative or ≥ 16 MB → -1, the same rule as `Write`.
- **EOF**: Ctrl-\ (the AmigaOS console EOF key, typed as CTRL + £ on the C64)
  or Ctrl-D (CTRL + D). On an empty line, Read returns **0**. On a
  non-empty line, the partial line is handed out without a newline and the
  next EOF returns 0 (the Unix `^D` rule). EOF is not echoed and does not
  move the cursor. It is a single event: the next Read waits for new input,
  and input already queued behind it is read normally (`u_con1`, `u_con4`).
  vc.lib then sets the stream's EOF flag, so `getchar`/`fgets`/`scanf`
  return EOF/NULL.
- **vc.lib flushes stdout before it reads an interactive stdin**, so
  `printf("Name: "); fgets(...)` shows the prompt before the echo, with no
  `fflush` needed (`con_echo`).

## Key sources

`hle_con_key` takes keys in this order:

1. **Injection queue** `hle_con_q` (256-byte BSS ring; `hle_con_qh` is the
   head the 6502 consumes, `hle_con_qt` the tail the host appends to). It
   holds the key codes of the table above. This is a **test seam**. A CR
   directly followed by LF in the queue is one RETURN, so a host `"\r\n"`
   gives one line; LF then CR gives two (`u_con5`).
2. If `hle_con_src` = 0 (**keyboard**, the default), the **CIA1 key
   matrix**. If `hle_con_src` = 1 (**scripted**), an empty queue is EOF. A
   batch run's stdin then behaves like `prog </dev/null`, and a program that
   reads more than it was given gets EOF instead of hanging.

`tests/run_vbcc.py` gives every batch program an empty scripted stdin (the
one line the C2 amendment allows, calling `tests.run_con.con_stdin`).
Without it, `u_dos` and `u_dos3`, which read the console and expect 0,
would now wait for a key forever (`build/evidence/w5-con/green/run_vbcc_nowire.txt`).
The `batch` leg of `run_con` pins this, and its teeth leg runs a copy of
`run_vbcc.py` with the line deleted (below).

**Outside the test drivers, keyboard is the default.** A program that reads
stdin, started by anything that does not stage a scripted queue, **waits
for real key presses**. That includes `tests.run_program --hunk`, the
hardware mailbox path and any future loader. On a real C64/U64 this is the
intended interactive behaviour. Under an unattended driver it looks like a
hang inside Read: no guest instructions run while Read waits, so an
instruction budget does not end it and only the driver's wall-clock
timeout does. Drivers that run programs unattended must call
`tests.run_con.con_stdin(sess)` (or write `hle_con_src` = 1 after
`hle_con_init`) first.

### Matrix scan (`hle_con_scan`)

- It runs only while a Read waits for a key: no IRQ and no KERNAL, because
  the runner runs with SEI and may have the ROMs banked out. Keys pressed
  while the program is computing are not seen. There is **no type-ahead**
  and **no auto-repeat**.
- It sets `CIA1_DDRA` = `$FF` and `CIA1_DDRB` = `$00`, drives one column low
  at a time on `CIA1_PRA`, and reads the rows on `CIA1_PRB` (low = down).
  **It leaves `CIA1_DDRA` = `$FF`, `CIA1_DDRB` = `$00` and `CIA1_PRA` =
  `$FF` (nothing selected)**, and nothing restores the previous values.
  All names come from `rc/ram.inc`.
- **Row settle (review fix1 item 1, UNVALIDATED ON HARDWARE).** Between two
  columns the scan deselects (`CIA1_PRA` = `$FF`), then waits until
  `CIA1_PRB` reads its idle level again before it selects the next column.
  - Why: a row that a key pulled low for the previous column returns high
    only through its pull-up resistor and the line capacitance, which can
    take on the order of a microsecond. At 1 MHz the next read comes at
    least 4 µs later, but at 48 MHz turbo it comes about 83 ns later. A row
    still low then reads as a key in the *next* column: a phantom key, for
    example holding Q (column 7, row 6) types `^` (column 6, row 6).
  - The wait is bounded: if the rows are not back at idle within
    `CON_SETTLE` = 2 changes of `VIC_RASTER` (at least one full raster line,
    63 µs), the whole sample is void and nothing from it is used.
  - VICE lets a line recover instantly, so no test here can show either the
    phantom or the fix. The `nosettle` mutant is **not killed** by any test.
    This needs a manual hold-key test on the U64E at 48 MHz: hold Q, then
    `;` and `/`, then SHIFT+letter, and check that no second character
    appears.
- **Joysticks** (review item 6):
  - Port 1 pulls port-B (row) lines low. The rows that read low while *no*
    column is selected are masked out of that sample only: they are
    unreadable and count as "up". The other rows still work, so a held
    joystick no longer locks out the whole keyboard. Keys on the masked
    rows cannot be typed while the joystick holds them.
    Also, pressing a key on a row the port-1 joystick holds low pulls that
    key's column low through the key switch, which the port-2 check below
    then sees: the whole sample is skipped while that key is held (keys on
    other rows are blocked only for that time; there is no permanent
    lockout). Found by con-review, delta round.
  - Port 2 pulls port-A (column) lines low, which selects columns behind
    the scan's back, so no row reading can be trusted. When the port-A pins
    read back anything other than `$FF` with every column deselected, the
    sample is **skipped**. While a port-2 joystick is held, the keyboard is
    ignored. That limitation is accepted.
- **Debounce (review item 4).** A sample that differs from the previous one
  restarts the window. Otherwise the *elapsed raster lines* since the last
  valid sample are added (the difference of `VIC_RASTER`, not the number
  of changes seen). After `CON_STABLE` = 64 lines (about 4 ms) the state is
  committed, whatever the poll rate or CPU speed.
  - `VIC_RASTER` is only the low 8 bits of the raster line. Across the
    frame wrap the 8-bit difference jumps by about 200 (PAL) or about 250
    (NTSC). A per-sample difference of 128 or more can only be that jump at
    these sample rates, and it is counted as 1 line. The window then gets
    slightly longer, never shorter.
  - Measured in VICE: one sample takes about 18 raster lines at 1 MHz
    (10 samples = 182 lines), most of it the row-settle checks.
- **Rollover (review item 5).** At a commit, *every* key that is down now
  but was not at the previous commit, except the modifiers, is decoded
  with the committed modifiers. The keys go into an 8-entry ring
  (`hle_con_pend`) in matrix order (column, then row), because the order of
  two presses inside one 4 ms window cannot be known. Each one counts in
  `hle_con_nkey`. When the ring is full, further keys are dropped. Ghost
  rectangles are not detected: with three keys held at the corners of a
  rectangle, the fourth corner reads as down and is typed.
- `hle_con_init` starts from "every key down". A key that is already held
  when input starts, such as the RETURN that launched the program, counts
  only after it has been released and pressed again.
- **RUN/STOP + RESTORE**: RESTORE is wired to the CPU's NMI, which SEI does
  not mask. With the KERNAL banked in, the KERNAL NMI handler sees RUN/STOP
  held and warm-starts BASIC. That kills the running program and the
  interpreter mid-run. Nothing in W5-K prevents or survives it.

### Key map

The matrix index is column × 8 + row. The column is the port-A bit and the
row is the port-B bit. The tables are `hle_con_kplain` / `hle_con_kshift`
in `hle_con.s`, mirrored in `tests/run_con.py` (`KEYMAP_PLAIN` /
`KEYMAP_SHIFT`) and cross-checked by `host/tests/test_hle_con.py`. The
mirror is also checked against the **C64 KERNAL's own decode tables**
(`$EB81` unshifted, `$EBC2` shifted, the same column × 8 + row order), read
from the emulated ROM by the `scan` leg (`scan/kernal`). Every letter
(case-folded), digit and ASCII punctuation entry must equal the KERNAL's,
with RETURN and DEL mapped to `$0D` and BS. Where the KERNAL types a PETSCII
graphics character, W5-K's value must be one of the documented extensions
(`{ } `` ` `` | ~`) or none. Letters
come out lowercase unshifted and uppercase with SHIFT (the Amiga
convention). The console screen shows both cases alike, because the default
charset is uppercase (`docs/HLE.md`).

| Key | plain | SHIFT | | Key | plain | SHIFT |
|---|---|---|---|---|---|---|
| A-Z | `a`-`z` | `A`-`Z` | | `+` | `+` | `{` |
| 1 2 3 4 5 | `1`-`5` | `! " # $ %` | | `-` | `-` | `}` |
| 6 7 8 9 0 | `6`-`0` | `& ' ( ) 0` | | `@` | `@` | `` ` `` |
| `,` `.` `/` | `, . /` | `< > ?` | | `*` | `*` | `*` |
| `:` `;` | `: ;` | `[ ]` | | `=` | `=` | `=` |
| £ | `\` | `\|` | | ↑ | `^` | `~` |
| ← | `_` | `_` | | SPACE | space | space |
| RETURN | `$0D` | `$0D` | | DEL | `$08` (BS) | (INST: none) |

- **CTRL** + letter gives `$01-$1A` (Ctrl-D = EOF, Ctrl-H = BS, Ctrl-J/M =
  RETURN, the rest are ignored), and **CTRL + £** gives `$1C` (Ctrl-\ =
  EOF). Other CTRL combinations give nothing.
- SHIFT (left or right), CTRL and C= are modifiers only. C= does nothing. A
  modifier on its own is never a keystroke: the matrix leg presses each of
  the four alone and requires `hle_con_nkey` to stay unchanged.
  The cursor keys, F1-F8, HOME and RUN/STOP give nothing. `{ } | ~ `` ` ``
  are on shifted keys the C64 would use for graphics.

## State and initialisation

All state is in C64 BSS (`hle_con_*`), not in guest memory: the queue, the
128-byte line buffer, the read position, the matrix state and the telemetry
counters (`hle_con_nkey`, `hle_con_nread` = console Reads serviced, u16).
BSS is not cleared at load, so `hle_con_magic` = `"CON!"` guards it. The
first Read runs `hle_con_init` lazily, and the host can call it directly
(`con_stdin` does this before every test program). State therefore
survives between programs unless a host resets it. On a real machine, an
unread rest of a line is handed to the next program that reads the console
(type-ahead, as a real CON: window does).

## Tests: what each layer is covered by

| Layer | Covered by | How keys get in |
|---|---|---|
| Read ABI (D0 only, D1-D3 kept, D3 = 0 / negative / ≥ 16 MB, kind 0 and kind 1) | `u_con1` | scripted queue |
| Line discipline: remainder across Reads, EOF variants, BS/DEL, overflow at 127, echo, rubout across a scroll and a row boundary, CR LF = one RETURN | `u_con1`-`u_con5` (register level: 5 longs per Read written to `$100000`) | scripted queue |
| vc.lib stdio over it: `fgets`, `getchar` to EOF, `scanf`, backspace editing, 3-byte `setvbuf` Reads (`hle_con_nread` = 11 pinned), echo without program output, prompt flush | `script/con_*` (full 25-row screen + exit code + A7) | scripted queue |
| **CIA1 matrix scan, modifiers (L/R SHIFT, CTRL, C=), key map (every character of the map), both EOF keys, RSHIFT sharing column 6 with `/`, C= sharing column 7 with Q, two keys in one debounce window (T+H → `th`), each modifier alone → no keystroke** | `matrix/con_*`: the same programs; the screen and exit code must be identical to the scripted run | **real key matrix**: VICE snapshot edit (below) |
| Key map vs the C64 KERNAL's decode tables | `scan/kernal` (reads `$EB81`/`$EBC2` from the emulated ROM) | none |
| Debounce counts elapsed raster lines, not samples | `scan/debounce`: a 6502 loop calls `hle_con_scan` with 1, 4 or 16 raster lines between calls and counts samples to commit (bounds in `run_con.run_debounce`) | none (VICE raster, no key) |
| Programs that never read stdin do not wait | `script/hello`, `matrix/hello`, and `run_vbcc` (whole suite, with the empty-stdin wiring) | none |
| Batch drivers: `run_vbcc.stage_and_run` alone gives EOF (`con_getchar` → 0 chars, `con_fgets` → NULL) and the unchanged units `u_dos`/`u_dos3` pass | `batch/*` (console reset to keyboard default first; this driver stages no input) | none (the run_vbcc wiring) |
| Host key-map mirror = 6502 tables, seam symbols exported | `host/tests/test_hle_con.py` | none (static) |
| **Row settle between columns** | **nothing**: VICE rows recover instantly. UNVALIDATED ON HARDWARE (manual U64E test pending) | — |
| **Joystick handling (port-1 row mask, port-2 skip)** | **nothing**: harness `inject_joystick` had no visible effect on `$DC00`/`$DC01` in this VICE setup | — |
| **Real key bounce, and the 4 ms window against real switches** | **nothing** in VICE: snapshot presses are clean. The window's *length* is tested (`scan/debounce`); whether 4 ms is enough for real switches is not | — |

**How the matrix leg presses a key.** VICE's binary monitor cannot press a
key in the matrix. Its "keyboard feed" writes the KERNAL buffer at `$0277`,
and this Read never looks at that buffer. VICE's snapshot does carry the
matrix, though: module `KEYBOARD` v1.1 holds `keyarr[16]` (per port-A line,
the port-B bits that are down) and `rev_keyarr[8]`, and VICE computes CIA1
port reads from exactly those arrays. `tests/run_con.py` `MatrixKeys`
dumps a snapshot, sets the bits of every key in a chord (a key plus its
modifiers, or two keys at once), undumps it and resumes. It then waits
until `hle_con_nkey` has moved by exactly the expected count. For a
modifier alone, which must count 0, it waits until the chord shows in the
debounced `hle_con_mat` and then requires the count unchanged. It releases
the chord the same way and waits until `hle_con_mat` is all up before the
next chord. The program runs asynchronously from a trampoline at `$0350` in
the cassette buffer while this happens: `jsr test_run_program`, then a done
flag, then a spin. Each edit round-trips the full ~17 MB snapshot with a
16 MB REU, about 0.2 s. `con_keys` (107 chords) takes about a minute.

**Mutants.** The flag mutants are `CON_MUT_*` flags in `hle_con.s`, built
with `python3 -m tests.rec_build --out DIR CON_MUT_x`:

- `NOECHO`, `NOBS`, `NOREM` (rest of the line dropped) and `NOEOF` (EOF
  never signalled) each turn `run_con` red.
- `NOSCAN` (the matrix is never read) fails only the matrix leg, which
  shows that leg does not reach the queue.
- `NORASTER` (one "line" per sample) and `NORSHIFT` (right SHIFT not
  decoded) are also red.

The text-patch mutants are built by
`build/evidence/w5-con/fix1/mutbuild.py`:

- The review's combined `mutA` (short window, no joystick guards, no raster
  gating, no RSHIFT decode, RSHIFT and C= unmasked) is red.
- So are `mutR` (the rubout wraps to column 0), `norollover` and `nocrlf`.
- **`nosettle` and `nojoymask` survive, by construction**: VICE cannot
  produce a slow row or a joystick here.

`NOWIRE` is the `batch` leg run with `--vbcc-module` pointing at a copy of
`run_vbcc.py` without the wiring line. It fails `batch/con_getchar` as a
hang, caught by the 20 s `--timeout`, in 22 s.

Every run has an instruction budget (`--budget`, default 2 000 000; the
largest program here needs about 94 000) and every harness call a 60 s
default timeout. Results: `build/evidence/w5-con/mutants.txt` and
`build/evidence/w5-con/fix1/mutants.txt`.

## Not covered / limitations

- **Real key presses on real hardware** are not tested: no device runs this
  wave. Not measured on the U64E:
  - the row-settle fix (UNVALIDATED ON HARDWARE, see "Row settle");
  - physical bounce against the 64-line window;
  - ghost rectangles with three or more keys held.
- The **joystick handling** has no test (see the coverage table). A
  joystick held in port 2 makes the scan ignore the keyboard.
- No type-ahead (keys are seen only while a Read waits) and no
  auto-repeat. Keys that go down in the same 4 ms window arrive in matrix
  order, not press order.
- RUN/STOP + RESTORE kills the run (NMI, see "Matrix scan").
- The line is at most 127 characters. No history, no cursor movement
  inside the line, no Ctrl-X line kill.
- The PETSCII graphics keys and the cursor/function keys give nothing.
  RUN/STOP is not Ctrl-C: SetSignal still never reports a break.
- `IsInteractive` is unchanged (console = DOSTRUE), and that is what makes
  vc.lib use small Reads and flush stdout.

## Hardware validation (W6-K)

Owner: w6-kbd (Wave 6, `docs/WAVE6_BRIEF.md` C1; fix round 1 after the
adversarial review). Program: `fixtures/c/kbd_echo.c`. Driver:
`tests/kbd_echo.py`. Host tests: `host/tests/test_kbd_echo.py`,
`host/tests/test_kbd_u64_mock.py`. This section is about the items marked
UNVALIDATED or "nothing" above: the row settle at 48 MHz, the port-1
joystick mask, and real switches.

### The program and the stream

`kbd_echo` is a vbcc C program that reads through the normal console path
(`Read` → `hle_con_read` → matrix scan, debounce, decode, line discipline,
echo). It walks the operator through nine steps. Each step is one line
ending in RETURN:

1. hold Q;
2. hold ; then /;
3. a 3-key rollover;
4. RSHIFT and C=;
5. to 9. a port-1 joystick held UP, DOWN, LEFT and RIGHT, then FIRE,
   while typing HOSE.

For every character that `Read` delivered, it prints a row like
` 'q' $71 c7r6` and then `STEP n PASS`, `FAIL` or `SKIP`. An empty line
skips a step, and CTRL+D quits. A free mode follows. The program ends with
`STEPS …` (P, F or S per step, `-` for a step that is not part of this
run) and `RESULT`. The exit code is the number of failed steps.

**The column and row on screen come from the key map, not from the scan.**
The program prints the inverse of the key map for each delivered
character. A guest program cannot read the scanner's state, because it is
in C64 BSS, which is outside guest memory. `hle_con.s` is not W6-K's file,
so no seam could be added. This is a deviation from the brief ("the raw
column/row it scanned"), reported as such. The scanned position exists
only in the driver's **stream**:
- it polls `hle_con_nkey`, `hle_con_mat` and `hle_con_idle`;
- it prints `key DOWN c7r6 Q` and the rows a port-1 stick holds low, and
  mirrors the screen;
- it flags `COLUMN BLEED?` when a key goes down in column c while column
  c+1 of the same row is held (the scan runs from column 7 down to 0).

The human session therefore always streams. There is no quiet mode.
`hle_con_idle` is not exported. Its address is derived from the BSS
declarations in `hle_con.s`, and used only if that layout reproduces every
exported BSS label.

**A real settle phantom reads `^q`, not `q^`.** The phantom is read in the
same sample as Q, and a commit decodes in matrix order, so column 6 comes
before column 7. The screen shows the ↑ glyph (the `^` key, hex `$5E`,
`c6r6`) and then Q. The driver's screen mirror prints ↑ as `^` (and £ as
`\`, ← as `_`).

### Judging how a step was done, not only the characters

The characters alone prove little. The queue-fed smoke types every answer
with no key pressed at all and still gets `STEPS PPPPPPPPP`. So every
stream sample is tagged with the step on screen, and `judge_step` checks
the **manner** of each step and looks for **defects**:

| Step | Performed means | A defect is |
|---|---|---|
| 1, 2 | Q (then ; and /) held continuously for at least 1.5 s | any other key down while it is held |
| 3 | 3 or more non-modifier keys down at the same time | any key other than Z, I, P or RETURN down while three are held (a ghost) |
| 4 | RSHIFT down when A and / go down; C= down when Q goes down; LEFT SHIFT never down | — |
| 5-9 | the stick's row (UP r0, DOWN r1, LEFT r2, RIGHT r3, FIRE r4) seen held low in `hle_con_idle` when each of H, O, S and E goes down | any key on that row reading down while the stick holds it |
| 1, 2 | — | a `COLUMN BLEED?` in this step (in steps 3-9 it is only a note: I and P, neighbours on row 1, going down between two polls is normal rollover typing; fix round 5, decision (a)) |
| 1, 2 (fixed build) | — | the debounce kept restarting on the held key: raw-only (in the raw samples, not the debounced matrix) for 0.2 s or more, committed later or never, or at 2 polls or more when never committed; "settle" when the column c-1 key is in raw or the key's row (5-7) is low at the idle read, else "keyboard/debounce, not settle-specific" (fix rounds 6-7) |
| 1, 2 | the column c-1 key (↑ for Q, L for ;, `,` for /) in raw or committed at any poll while the key is held (fix round 8): the settle signature in `nosettle`, a settle defect in the fixed build | |

In a control run, only the triggering action is required (the key held,
or the stick seen). The broken build may garble the typing itself.

**"Typed with X held" (fix round 3, from the attended sessions).** The
stream polls about every 0.25 s at the default `--poll 0.1` (the REST reads
add about 0.15 s to `--poll`: measured 0.24-0.26 s between polls at
`--poll 0.1`, 1.14-1.15 s at `--poll 1.0`; the 0.11-0.14 s spacing in the
logs is between one poll's state read and its screen read). A letter
tapped faster than that is
counted by `hle_con_nkey`, but it is never down in the debounced matrix at
a poll. So a letter counts as typed with the stick (or a modifier) held in
either of two cases:
- its first poll in the matrix shows the condition;
- its **echo** appeared on the prompt row between two polls that **both**
  show the condition.

An echo gain counts only if it is backed by decoded key events. The
`hle_con_nkey` count must grow by at least as much over the gain's
interval, widened by one poll on each side. A row that the screen mirror
re-emitted while it read a scroll mid-copy has no events behind it, so it
never counts.

Fix round 4 (the fix3 review) tightened these rules:
- **The line is compared with a base.** The base is the last line
  accepted as real, and it starts empty at each step, because each step
  opens a new prompt. What was typed is whatever follows the common
  prefix of the base and the polled line. So DEL types nothing, and DEL
  plus a new key between two polls types only that key (G1).
- **At most 0.4 s between the two polls of an echo credit.** The attended
  sessions never had a gap over 0.33 s, so a normal poll has about 0.15 s
  of margin.
- **The matrix path uses the key's first down poll.** A key pressed
  without the stick and held until the stick arrives does not count.
- **The stick must be held at every poll** from the first HOSE letter's
  credit to the last one's. Letting go between letters is not a hold. All
  20 replayed fixed-build stick steps meet this.
  - Modifiers get no such rule across A and /. The screen says
    "RSHIFT+A, RSHIFT+/", so releasing RSHIFT in between is correct, and
    both attended 1 MHz runs did so.
  - Each character's own credit still needs its modifier at both of its
    polls.
- **Heartbeat.** About once a second the stream writes
  `hb polls +N max gap Gs` to the session log (not to the terminal), and
  the last, partial window when the run ends.

Fix round 5 (the fix4 review) made the timing explicit:
- **The poll is logged.** The session header and each run-start line
  carry it (`fixed@48 started (--poll 0.1s): …`), and the judge uses the
  period `--poll` + 0.15 s. A gap over max(0.4 s, period + 0.15 s) is a
  **stall**; the live judge sees every poll, so it bounds gaps too:
  - a hold is not credited across a stall;
  - an echo credit, and the stick's continuity from H to E, need polls
    at most 0.4 s apart at any poll rate.
- **NOT JUDGEABLE, not operator error.** A step not proven performed is
  NOT JUDGEABLE when the stream could not have proven it:
  - the poll period is unknown (a replayed log with no `--poll` and no
    heartbeat, or with a broken heartbeat), or
  - a gap in the step is a stall, or
  - the step is a tapped one (3-9) of the fixed build and the period is
    over 0.4 s (decision (b): at `--poll 1.0` letters are tapped between
    polls, so nothing can prove them "typed with the stick held").

  A run with nothing else wrong is then **NOT JUDGEABLE**. An F or a
  defect is still FAIL, and a control's signature is still DEFECT SHOWN.
  The session counts NOT JUDGEABLE as INCOMPLETE. A fixed run at a sparse
  poll can never PASS.

`hle_con_idle` is refreshed on every sample that is not voided, whatever
keys are down (`hle_con_scan`), so it is a continuous "stick held" signal.
The false "not typed with the stick held" in the attended sessions came
from missed taps, not from the idle signal.

**Other stream details:**
- **Bleed.** A held modifier is never a bleed source. RSHIFT (c6r4) sits
  directly above `.` (c5r4), so typing `>` used to be flagged.
- **Raw diagnostic.** The stream also reads `hle_con_raw`, the latest valid
  raw sample. `raw (not debounced yet): …` shows a key that is held but
  never committed, and `raw (not debounced yet): none` when that clears
  (fix round 5, so one long raw hold and two taps read differently). So
  from fix round 3 on, the "nothing" form of the phantom (Q held, the
  debounce restarting) is distinguishable from Q never pressed, even at
  `--poll 1.0`. Before, it was not: only the debounced matrix and the
  event count were streamed.
- **The debounce restarting, and what is settle-specific (fix rounds 6
  and 7, review5 R1/R2, review6 K1-K3).** Measured on the held key's
  **raw-only** samples (in raw, not in the debounced matrix; a release
  never reads raw-only). A healthy press is raw-only at **one poll at
  most**: the commit follows after `CON_STABLE` (about 4 ms) plus switch
  bounce, sampling never stops with a key raw-only, and a commit sets the
  matrix to raw. So the debounce **kept restarting** when the key was:
  - raw-only for 0.2 s or more (`DELAY_MIN`), then committed (**delayed
    commit**) or never committed (**"nothing" form**); the same span
    counts either way (K2). 0.2 s is 50 times `CON_STABLE`, below the
    shortest poll period (0.24 s), and above the 0.11-0.14 s between one
    poll's state and screen reads, which a replay splits into two samples
    (review6 guards G1, G2: the replay worst case is 0.15 s);
  - or, never committed, raw-only at **2 polls or more**, consecutive or
    not (K3: the idle-mask flicker masks every other sample).

  A restarting debounce alone is **not settle-specific**: a chattering
  switch or a noisy line gives the same stream in either build (review6
  A4). It is the **settle signature** only with evidence that only the
  missing settle produces (`_settle_evidence`):
  - the **column c-1 key in raw** together with the held key (↑ for Q,
    L for ;, `,` for /): a row still low from the held key's column,
    read as the next column's;
  - the held key's **row low at the idle read**, for rows 5-7 (r0-r4
    are the port-1 joystick lines, so r2 for ; proves nothing). The
    scan settles before **every** column, so in the fixed build the
    settle before column c-1 already waits for the row after the held
    key's column; only after column 0 is there no settle before the next
    sample's idle read. In `nosettle` nothing waits, and a row still low
    at the idle read masks the held key out of that sample. (Fix round 6
    called the idle read unsettled in both builds; that holds only for
    keys in column 0. Q is column 7.)

  The column c-1 key is settle evidence **whenever** it shows while the
  key is held, in raw or committed, even when the held key itself
  committed cleanly (fix round 8, review7 F1/F2): in the marginal regime
  Q commits alone and ↑ then keeps flickering into raw, or commits a few
  polls later (`COLUMN BLEED? c7r6 Q is held`). Fix round 7 looked for it
  only when Q was raw-only, so such runs read "clean commit".

  So:
  - with that evidence: **FAIL** in the fixed build (a failure of the
    fix), **DEFECT SHOWN** in `nosettle` (`StepJudge.settle`, and the
    phantom is there too);
  - without it: the fixed build FAILs "keyboard/debounce, not
    settle-specific", and a `nosettle` step is **NOT JUDGEABLE**,
    "defect-like, not settle-specific". It never counts toward "the fix
    is necessary".
- **The operator's answer never changes a verdict the instrument
  supports (fix round 7, K5).** In every `nosettle` run the driver asks,
  for **each** hold step and whatever it showed, "did you hold Q at the
  '>' prompt as the checklist asked?". Keys typed ahead are discarded
  first, and the answer is logged (`operator: step 1 held as asked (Q)?
  yes|no|no answer`). The same rule holds for every form, phantom,
  delayed commit and "nothing" alike: **"no" changes nothing**, and
  "yes" changes nothing either, except in one case, where it can never
  score: when the scanner saw no raw-only Q at 2 polls, a "yes" makes the
  step NOT JUDGEABLE ("the scanner saw no Q") instead of INCOMPLETE. Both
  mean redo. A log without raw (before fix round 5) is the same: a "yes"
  is NOT JUDGEABLE, never DEFECT SHOWN.
- **A signature is never buried (K4).** A control's signature makes the
  run DEFECT SHOWN even when another step is undone (at `--poll 1.0` a
  short hold of ; is easily "undone").
- **Scanner liveness (fix rounds 6 and 7).** The stream also reads
  `hle_con_ras`, the raster line of the last valid sample. It moves on
  almost every sample, so the same value at 3 polls in a row logs
  `scanner: not sampling` (and `scanner: sampling` when it moves again).
  That also happens, correctly, whenever no `Read` waits (the program
  printing). A hold step whose key was never seen, with the scanner not
  sampling at 2 polls or more, is **NOT JUDGEABLE** "scanner not
  sampling" (K6): nothing seen proves nothing when nothing was sampled.
  In the fixed build that is a possible failure of the fix (every sample
  void): the A/B stops on it.
- **Replay summary.** `--replay` ends each log with one row per run: the
  **logged** (live) and the **replayed** verdicts, the polling, step 1
  classified (`phantom`, `settle: delayed commit` / `settle: nothing`,
  `defect-like: …`, `clean commit`, `nothing: raw brief / no Q seen /
  scanner dead`), Q's longest raw-only run, the hold, row-6 idle polls,
  dead-scanner polls, and the operator's answer. A replay of A/B sessions
  ends with one `A/B:` line (see "The settle A/B"). A log passed twice is
  replayed once ("passed twice, skipped"), and an A/B pair counts once.
- **Replay.** `python3 -m tests.kbd_echo --replay LOG…` re-judges the runs
  of session logs offline. It rebuilds each stream from the logged events.
  - **Each session on its own.** A log may hold several sessions (before
    fix round 5, `session.log` was appended to). Each session header
    starts a new one, each gets its own verdict, and a session not on the
    full plan is INCOMPLETE, as live. A log's verdict is its **worst**
    session's, and `--replay` exits 0 only when every log is VALIDATED.
  - **Timing.** With a known period (the logged `--poll`, else the
    heartbeat, else `--assume-poll S` for logs from before fix round 5),
    it adds the polls between two logged changes, one period apart,
    counted back from the later change, and never denser than 0.25 s. So
    it never over-measures a hold. With an unknown period, or a BROKEN
    heartbeat (a gap over the stall bound), it adds nothing: only the
    logged polls count, and a step they do not prove is NOT JUDGEABLE.
  - **Heartbeat tolerance (fix round 10).** With `--poll` logged, a run
    is BROKEN only at a gap over max(`gap_max`, 1.5 x the smaller of the
    measured median and the nominal period): 1.72 s at `--poll 1.0`,
    0.4 s at 0.1. (It was `gap_max`, 1.30 s at 1.0: one 1.31 s gap, in
    normal gaps of 1.14-1.23 s, broke a whole attended run.) A heartbeat
    window with a gap over `gap_max` but under that bound stays proven,
    but gets no synthetic polls, so a fill cannot land in the real gap and
    stretch a raw-only sighting past `DELAY_MIN` (review6 G2's margin).
    The live judge's own gap bounds are unchanged; a 2 s stall at 1.0
    (review4 F3) stays BROKEN.
  - **Proof.** "proven" polling needs a heartbeat. A replay with unproven
    polling (no heartbeat, or an assumed poll) is never VALIDATED.
  - On the pre-fix judge it reproduced all 11 run verdicts and all 70
    step judgments of the three attended sessions. The logs are in
    `tests/kbd/replay/` (with the 19:31 sparse session,
    `kbd-hw-sparse2`), verbatim except for relative prg paths.

**Run verdicts** (`judge_run`):
- **Fixed build:** PASS only when every step is P, was performed, and
  shows no defect. It is FAIL on any F or any defect. It is NOT JUDGEABLE
  when all that is missing is a step the stream could not decide.
  Otherwise it is INCOMPLETE. **A SKIP is never a pass.**
- **Control build (fix round 2):** DEFECT SHOWN **only when the stream
  shows that control's own defect signature**:
  - `nosettle`: the key one column lower on the same row goes down in the
    **same sample** as the held key (c6r6 with Q, c5r2 with ;, c5r7
    with /);
  - `nojoymask`: keys on the stick's row read as down while the stick
    holds that row.

  INCONCLUSIVE when the target steps were performed, all P and clean.
  Anything else is **INCOMPLETE: operator error, redo the leg**. That
  covers an F or S from a typo, an early RETURN, a stray key without the
  signature, or an unperformed step. It is never DEFECT SHOWN. So a typo
  in a control run can no longer turn a clean control into a
  "validation".

### Positive controls

A PASS at 48 MHz alone cannot tell "the fix works" apart from "this U64E
never produces the phantom". So the session also runs the runner with the
fix taken out, which is expected to fail:

| Control | What is removed | Target steps |
|---|---|---|
| `nosettle` | the `jsr hle_con_settle` and `bcc` in the column loop (5 bytes of NOP) | 1, 2 |
| `nojoymask` | the `and hle_con_idle` (3 bytes of NOP): port-1 rows read as keys | 5-9 |

- **How a control is made.** The same uploaded runner is patched in C64
  RAM while it sits idle. There is no second upload.
  - The site is found in the runner PRG by the byte pattern of the scan's
    column loop, and the operand of that loop's `and` must be
    `hle_con_idle`. It must match exactly once.
  - Every write is verified by readback. The patch is removed in a
    `finally`, also verified by readback.
  - These are the same edits as the text mutants in
    `build/evidence/w6-kbd/mutbuild.py`, which survive VICE by
    construction.
- **How the operator knows.** The program gets the arguments
  `CONTROL <name> <first> <last>`. It prints `*** CONTROL BUILD: <name>
  ***` and "SUPPOSED TO FAIL", and runs only the target steps. The host
  output says `THIS BUILD IS SUPPOSED TO SHOW THE DEFECT`.
- **The session verdict.**
  - **VALIDATED** requires all four of: the fixed build PASS at 48 MHz,
    the fixed build PASS at 1 MHz (the negative control), `nosettle`
    DEFECT SHOWN at 48 MHz, and `nojoymask` DEFECT SHOWN at 48 MHz.
  - If the fixed build passes but a control comes out clean, the verdict
    is **INCONCLUSIVE**. That is a different claim from VALIDATED.
  - Any fixed-build defect or F is **FAIL**.
  - Anything missing, skipped or not done as asked is **INCOMPLETE**.
  - Only VALIDATED exits 0.

**What the verdicts mean, given the stream.** Every run streams. On the
U64 each stream poll is a REST memory read, which could stall the 6510
inside a scan and lengthen the settle time. So a stream could in principle
hide a phantom. The controls settle this by design: they run under
exactly the same streaming conditions as the fixed build.
- **VALIDATED** means that both defects were visible under streaming, and
  that the fixed build was clean under the same conditions. A control that
  shows its defect while streaming proves the stream does not mask that
  defect.
- **INCONCLUSIVE** means that a control's defect was **not seen under
  streaming**. That can be the device (the defect does not occur there, so
  the fix is harmless but not proven) or the stream.
- **The follow-up, for INCONCLUSIVE only**, is the settle A/B below
  ("The settle A/B"). At `--poll 1.0` a poll comes every 1.15 s instead
  of 0.25 s, about 5 times fewer reads during the holds. A hold is
  measured from the first poll that saw the key to the last, so a hold
  of T s reads as T - 2.3 s to T - 1.15 s (HOLD_MIN 1.5 s needs the key
  at 3 polls in a row: a 3 s hold is "not held" about half the time). A non-default plan is never VALIDATED, so its
  output is evidence for the supervisor's judgment, not a verdict. A
  fixed-build run at that poll can only FAIL or be NOT JUDGEABLE /
  INCOMPLETE.
- **Known limits (reviews 5 and 6, not fixed).**
  - R4: at `--poll 1.0` "the same sample" spans 1.15 s. A neighbour
    brushed within about 1 s of the held key (L before ;) reads as the
    settle signature. A brush in the nosettle run is not repeated in the
    fixed run, so nothing discards it; that is why the A/B counts a step-2
    (; or /) phantom only when it repeats in another pair. L and ;, and
    `,` and /, are two keys apart, so the risk is low; Q and ↑ are far
    apart.
  - A1: at `--poll 1.0` a delay is caught only when two polls fall inside
    it: always from about 2.3 s, sometimes from 1.15 s, never below. A
    delayed commit under about 1.15 s is invisible.
  - R7: in a replay with a BROKEN heartbeat, raw-only runs are measured on
    the logged polls only, so a fixed-build "nothing" form the live judge
    called FAIL can replay as NOT JUDGEABLE. It never validates.
  - Live compares `--runs` with the plan as typed, replay the header's
    normalised list. `parse_runs` rejects spaces, so they cannot differ.

### The settle A/B (attended, U64E)

This answers one question: **is the row settle necessary on this U64E?**
Each pair is one session, `nosettle@48` and `fixed@48`, in alternating
order. `--replay` of the pairs applies the rule below and ends with one
line, `A/B (docs/HLE_CON.md 'The settle A/B'): NECESSARY | NOT
REPRODUCED | REDO | STOP: <reason>` (`ab_verdict`).

**Evidence rule (review6 section 5, automated).**
- **Only settle-specific evidence counts** (see "The debounce
  restarting, and what is settle-specific"): the step-1 phantom (↑ with
  Q in one sample), or a debounce that kept restarting with ↑ in raw or
  the held key's row (r6 for Q, r7 for /) low at the idle read. A
  delayed commit or "nothing" form
  **without** that evidence is reported as **defect-like, not
  settle-specific** and never counts: a chattering Q must not prove the
  fix necessary. ↑ in raw or committed at any poll of Q's hold counts,
  even after Q committed cleanly. A step-2 (; or /) signature counts
  only when it repeats in another kept pair (R4); a single one gives
  REDO ("not yet repeated"), never NOT REPRODUCED.
- **NOT REPRODUCED is printed only when no kept pair shows any
  signature in any step and no nosettle run is defect-like** (fix round
  8, F3); otherwise the line is NECESSARY or REDO.
- A nosettle run with a stray key that is not the settle's (W with Q)
  is no clean zero: its pair is discarded.
- **Never the operator's answer.**
- **A signature counts even if its run is INCOMPLETE** for another step
  (K4).
- **The fixed partner must hold, cleanly**: steps 1 and 2 performed
  (Q, ; and / each held 1.5 s or more by the stream, so **a tap is not a
  hold**), no DEFECT and no stray key. Otherwise the pair is
  **discarded and redone**, not counted. A pair whose polling is not
  proven by a heartbeat, or whose nosettle run was not done as asked or
  is NOT JUDGEABLE without a signature, is discarded too.
- **STOP and report** when a fixed@48 run shows a settle signature, a
  debounce that kept restarting (settle-specific or not), or a scanner
  not sampling in steps 1-2: a failure of the fix, or a keyboard fault
  that invalidates the comparison.

**What may be claimed.**
- **"Necessary"** needs **at least one settle-specific signature** in a
  kept nosettle run, with every kept fixed@48 run clean. It supports:
  "on this U64E at 48 MHz, without the row settle the scan misreads a held
  key; with it, N of N clean under the same operator and poll". It
  supports no rate, and it does not show the fixed build defect-free in
  general: 5 clean fixed runs only bound the fixed build's per-run defect
  rate below about 45% (95%, one-sided).
- **0 of 5 means only "not reproduced"**: if the defect occurs, its
  per-run rate under this detector is below about 45% (95%), and delays
  under about 1.15 s are invisible at `--poll 1.0` (A1). It must **not**
  be written as "the settle is unnecessary". Add the earlier
  `--poll 0.1` nosettle INCONCLUSIVE (kbd-hw-2).
- A signature at `--poll 1.0` but none at `--poll 0.1` means that polling
  itself hides the defect: record that as a limit of the default plan.

**Operator script.** The supervisor runs this with the user at the C64.
Each session takes about 5 minutes (gate, boot and restore about 2 min,
then two runs), so 5 pairs take about 25 minutes before any redo.

Tell the operator before the first session:
- **No joystick in either port. SHIFT LOCK up.**
- **Wake the keyboard first, in every run.** This C64's keyboard carries
  a Blingboard (an RGB LED board) that hides the whole first key hold
  after about 2 minutes idle (see "The Blingboard" below). At the run's
  first `>` prompt (step 1; the screen says
  `TAP SHIFT, THEN HOLD Q (COUNT 10),`), **tap LEFT SHIFT once and
  release it**, then hold Q.
  Release SHIFT before pressing Q, or Q comes out upper case. A lone
  SHIFT tap is neither a stray key nor a hold problem in either build.
- Each session first waits for **Enter on the host terminal** (the
  operator confirmation): press Enter when you sit at the C64.
- In **every** run, at step 1, after the SHIFT tap, hold
  **Q to a count of 10** (at least 5 s by the clock, so the stream sees it
  at 4-5 polls), release, RETURN. At step 2 hold **; to a count of 10**,
  release, hold **/ to a count of 10**, release, RETURN. (A driver older
  than fix round 8 printed `HOLD Q 2S`: ignore that, tap SHIFT and count
  to 10.)
- **While holding Q, ; or /, touch no other key.**
- In the fixed run, press **RETURN alone** at steps 3-9 and at the free
  mode prompt. At `--poll 1.0` the checklist says so; at `--poll 0.1`
  (AB 6/7) it does not, but RETURN alone is fine there too: the run's own
  verdict is then FAIL or INCOMPLETE, and the A/B reads only steps 1-2.
- After each nosettle run the host asks a **y/n question** per hold step.
  Answer as you like: it is logged and does not count.

```sh
cd <your checkout>
export U64_HOST=192.0.2.81
PRG=build/w6-int/runner.prg
D=build/evidence/kbd-ab-$(date +%Y%m%d-%H%M%S)   # new per A/B (review9 F1)
AB() {  # AB <pair> <runs> <poll>
  python3 -m tests.kbd_echo --prg $PRG --backend u64 --runs "$2" \
      --poll "$3" --out-dir "$D/pair$1"
}
AB 1 nosettle@48,fixed@48 1.0
AB 2 fixed@48,nosettle@48 1.0
AB 3 nosettle@48,fixed@48 1.0
AB 4 fixed@48,nosettle@48 1.0
AB 5 nosettle@48,fixed@48 1.0
# optional: the same at the default poll
AB 6 nosettle@48,fixed@48 0.1
AB 7 fixed@48,nosettle@48 0.1
# the table (logged and replayed verdicts) and the A/B line; the glob
# passes each pair directory once
python3 -m tests.kbd_echo --replay $D/pair*/session.log | \
    grep -E '^(SUMMARY| ses|  +[0-9]+ |A/B)'
```

**Reading the A/B line.** NECESSARY and STOP are final. REDO names every
discarded pair and why:
- `keyboard not awake? (Blingboard idle; tap SHIFT first)`: step 1 saw
  no Q at all, raw included. That is the keyboard, not the scan: redo the
  pair and tap SHIFT first. It is never a signature.
- a signature in a discarded pair (fix round 9, N1): redo that pair; the
  line never reads NOT REPRODUCED beside it. The discarded pair stays in
  the glob, so the redo cannot clear it: the same stop rule applies (at
  most 3 extra pairs, then report **REDO (unresolved)** with the
  signature; review9 F3). A sleeping nosettle step 1 no longer discards a
  pair whose step 2 shows a signature (review9 F2), and a Q masked by its
  row at the idle read is not labelled "not awake" (F4).
- `not yet repeated` (one step-2 signature) or `defect-like`: run **at
  most 3 extra pairs** at `--poll 1.0`, named `AB 8`, `AB 9`, `AB 10`.
  If the line still says REDO, stop and report **REDO (unresolved)**
  with the signatures it lists: the detector cannot decide, and more
  pairs will not change that (review8 N2).

Each `--out-dir` must be new (a non-empty one is refused). Pass each pair
directory to `--replay` **once** (a log passed twice is skipped, but a
copy under another name would count twice). When the A/B line says REDO
for a pair, run it again under a new name (`AB 3b …`); the discarded one
stays on disk and its reason is in the A/B line. On STOP, stop and
report. Report back: the SUMMARY rows (logged and replayed), the A/B
line, and the `session.log` files.

### The Blingboard (the rig's keyboard, found 2026-09-28)

This C64's keyboard carries a **Blingboard**, an RGB LED board. After
about 2 minutes idle, **the whole first key hold is invisible at the
matrix**: the user held Q to a count of 10 on stock BASIC (none of this
project's code running) and nothing appeared. A SHIFT tap first wakes it,
and Q then types normally; after only a few seconds idle it is fine.
(docs/HARDWARE.md, "Test-rig note".)

- **In the attended A/B of 2026-09-28** (`tests/kbd/replay/kbd-ab-20260928`,
  pairs 1, 2, 4 and 5), every session's **first** run had step 1 "no Q
  seen" with raw empty too, in **both** builds (nosettle first in pairs 1
  and 5, fixed first in 2 and 4), and every second run was clean. That is
  the wake artefact, not the settle. Replayed, all four pairs are REDO
  with the reason `keyboard not awake? (Blingboard idle; tap SHIFT
  first)`.
- **The judge (fix round 9):** step 1 with the held key never seen, in
  raw or the matrix, raw logged and the scanner sampling, gets the note
  `no Q seen at all (raw too): keyboard not awake? …`, its own SUMMARY
  class (`no Q at all: not awake?`) and its own A/B discard reason. It is
  never a signature and never STOP: the Blingboard behaves the same in
  both builds.

**W6-K history: the 2026-09-26 "nothing form" sessions.** The sparse
sessions (`kbd-hw-sparse`, 19:01, and `kbd-hw-sparse2`, 19:31), where Q
"vanished" in nosettle step 1 (no key DOWN, program S), were first runs
of their sessions or came after a 30-minute idle. They are explained by
the Blingboard, and they are **no evidence for the settle defect**. (They
predate the raw diagnostic, so the logs themselves could never show it
either way; review4 section 3(c).)

**W6-K result: the settle A/B of 2026-09-28 (`kbd-ab-20260928-b`).**
Run by the supervisor with the user at the C64, after the Blingboard was
found, with the SHIFT tap at every run's first prompt: pairs 1-5 at
`--poll 1.0`, plus 2b (redoing pair 2, whose heartbeat then read BROKEN
on one 1.31 s gap) and 5b (redoing pair 5, whose nosettle run had RETURN
down with /). The logs are in `tests/kbd/replay/kbd-ab-20260928-b`.
- **Replayed (fix round 10): NOT REPRODUCED, 0 of 6 kept pairs at
  `--poll 1.0`** (with the fix-round-10 heartbeat tolerance pair 2 is
  kept too; before, 0 of 5). Only pair 5 is discarded (the RETURN
  overlap).
- **7 nosettle runs with the keyboard awake, no phantom in any**: every
  step 1 a clean commit of Q (held about 8 s), no ↑, L or `,` in raw or
  committed, no row low at the idle read, no debounce restarting. Add the
  earlier dense-poll run: kbd-hw-2 nosettle@48 at `--poll 0.1`
  (2026-09-25) was clean too (INCONCLUSIVE).
- **What may be claimed**: the settle defect was **not reproduced** on
  this U64E at 48 MHz. If it occurs, its per-run rate under this detector
  is below about 39% (95%, one-sided, 6 kept pairs), and delayed commits
  under about 1.15 s are invisible at `--poll 1.0`. It is **not** "the
  settle is unnecessary": the fix stays, harmless and unproven.
- **Operator input**: the session confirmations appear in the logs as
  `confirmation: operator ready`. The supervisor sent that Enter through a
  pty, at the user's request, while the user sat at the C64; the y/n hold
  questions after each nosettle run were left unanswered (`no answer`),
  which does not change any verdict (K5).

### What VICE covers (`--backend vice`, automated)

| Leg | What it checks |
|---|---|
| `guided` | Presses every step's keys in VICE's matrix (snapshot `KEYBOARD` module) and checks the count and scanned position of every key event, that a held key does not repeat, that a modifier alone counts nothing, and each step's screen rows. A **control port 2** stick held while X is down must skip the sample. The judge must find steps 1-4 performed and clean, and steps 5-9 NOT performed (VICE has no port-1 stick), so the run is INCOMPLETE and never PASS. |
| `teeth` | The shape of a real phantom: Q and ↑ in one commit give `^q`, `STEP 1 FAIL` and a flagged bleed. The judge gives FAIL as a fixed-build run and DEFECT SHOWN as a control. An empty line gives SKIP. |
| `manner` | The right characters typed the wrong way: Q tapped, Z I P one at a time, LEFT SHIFT in step 4. The program says P, the judge says not performed, and the run is INCOMPLETE. |
| `patch` | Each control patch is applied and removed with readback. `nosettle` observably stops the settle routine: `hle_con_sn` is left untouched by a scan. |
| `control` | The `nosettle` control, with the real RAM patch and the CONTROL arguments, on VICE. VICE rows recover at once, so this is a device where the phantom never occurs. Run honestly, the verdict is INCONCLUSIVE. With a stray W after Q (an F), it is INCOMPLETE. Only with the signature injected (↑ down in the same sample as Q) is it DEFECT SHOWN. |
| `--smoke` | No key presses. The idle scan must stay silent, then the queue feeds every answer and every step must PASS. The judge must say INCOMPLETE ("not a validation"). The control patch round trip is also checked. This leg also runs on the U64. |

VICE can drive only control port 2 from a snapshot (evidence:
`build/evidence/w6-kbd/joystick_probe.txt`):
- undumping `JOYSTICK0` sets the value that the next dump writes as
  `JOYSTICK1`, and CIA1 port A reads that value;
- `JOYSTICK1` reaches neither port;
- the binary monitor's Joyport-set had no visible effect.

So `nojoymask` and `nosettle` survive VICE, and **only the hardware
controls can kill them**. The mutant totals are in
`build/evidence/w6-kbd/fix1/`.

### The manual session (U64E, a person at the keyboard)

The supervisor runs this with the user:

```sh
U64_HOST=192.0.2.81 python3 -m tests.kbd_echo --prg build/<pkg>/runner.prg \
    --backend u64 --out-dir build/evidence/kbd-hw
```

1. **Firmware gate, fail fast.** The driver tries the DeviceLock for
   10 s. If the lock is busy, it exits non-zero and names the holder (PID,
   lockfile age). Otherwise it does one bodyless GET `/v1/info`. On
   anything but the resting build (git `c47155b7`, core `1.50`; override
   with `--expect-git` and `--expect-core`), it releases the lock in a
   `finally` and exits non-zero, with nothing written. The manual mode
   never re-queues. Only the unattended `--smoke` re-queues, every 5 min
   for up to `--fw-wait-hours` (default 12), and it retries on a transient
   GET error. On the resting build the driver keeps the lock, and
   `u64_hold` joins that same hold (`allow_nested`).
2. **Operator confirmation.** The driver prints the plan and waits up to
   5 min for Enter on the host terminal (stdin must be a terminal). Only
   then does it upload the runner (`run_prg`). No answer means it aborts
   with nothing written. No command-line switch skips this step.
3. **The hold.** It takes the exact full-config snapshot and restore of
   `u64_hold`, with the config JSON in `--out-dir`. It re-checks the
   firmware in the hold's own snapshot before anything else. It sets every
   item the run depends on explicitly (a flash resets the device
   settings): the CPU speed per run (Turbo Control through `set_speed`) and
   REU enabled at 16 MB (`set_reu`). It boots the runner at 1 MHz, zeroes
   the ULTDOS opt-in key and proves RAMDISK. The program opens no files,
   so there are no storage writes.
4. **The plan (`--runs`).** The default is
   `fixed@48,nosettle@48,nojoymask@48,fixed@1`. Only this plan can be
   VALIDATED.
   - Each run prints its checklist and streams until the program exits.
   - On `--max-minutes` (default 30 per run) or ^C, the driver feeds
     CTRL+D through the queue. The patch removal and the config restore
     still run.
   - **A program that still has not ended stops the session.** The driver
     does not rewrite a control patch under a CPU that may be executing
     it. The restore's C64 reset drops the patch together with the runner.
   - Before each fixed-build run the runner code is re-checked as the
     original, with no patch left.
   - The log is `--out-dir/session.log`, and the last line is
     `SESSION VERDICT: …`. Each session needs its own `--out-dir` (the
     default is a new `build/evidence/kbd_echo-u64-<date>-<time>-<pid>-<n>`);
     a directory that is not empty is refused, and the log is created,
     never appended to.

**Expected duration: about 35-45 minutes** with the lock held throughout:
- gate, confirmation, boot and restore: about 2 min;
- the fixed build at 48 MHz, 9 steps plus the free mode: about 10-12 min;
- the `nosettle` control: about 2 min;
- the `nojoymask` control, 5 stick steps: about 4-5 min;
- the fixed build at 1 MHz: about 12-15 min, because the interpreter runs
  about 1,600 68k instructions per second there, so each output row takes
  about a second.

**Before starting:**
- There must be no joystick in control port 2 (by design it locks the
  keyboard out).
- A joystick must be in control port 1. Steps 5-9 need it, and a skipped
  step makes the session INCOMPLETE.
- **Type only while the `>` prompt shows.** There is no type-ahead.
- On screen, letters show in one case. The hex code tells them apart:
  `$71` is q, `$51` is Q.

| Step | Do | Fixed build: expect | The defect (what a control should show) | Validates |
|---|---|---|---|---|
| 1 HOLD Q | hold Q for 2 s, release, RETURN | `> q`, ` 'q' $71 c7r6`, `STEP 1 PASS`; stream `key DOWN c7r6 Q`, held ≥ 1.5 s | `^q`: ↑ (`$5E`, c6r6) read in the same sample as Q, with the stream flagging `c6r6 UPARROW <- COLUMN BLEED? c7r6 Q is held`; or no character at all (the phantom keeps restarting the debounce) | the row settle at 48 MHz; debounce on a real switch |
| 2 HOLD ; AND / | hold ; for 2 s, release; hold / for 2 s, release; RETURN | `;/` | `l;` (c5r2, bled from ;) or `,/` (c5r7, bled from /) | the settle on rows 2 and 7 |
| 3 ROLLOVER | press Z and keep it down, add I, add P (all three held), release all, RETURN | `zip` in press order; stream shows three keys down at once | a letter missing, doubled or never pressed | 3-key rollover (Z, I and P form no ghost rectangle) |
| 4 RSHIFT, C= | hold RIGHT SHIFT and type A and /, release; hold C= and type Q, release; RETURN; never LEFT SHIFT | `A?q` | `a/` (RSHIFT not decoded), or an extra character from C= | the RSHIFT decode; C= as a modifier only |
| 5 STICK UP | stick in port 1 UP, type HOSE, release the stick, RETURN | `hose`; stream `rows r0 held low (masked)` | `1 3 5 7 9 + \` appear, and DEL rubs characters out | the port-1 row mask, row 0 |
| 6 STICK DOWN | … DOWN … | `hose`, r1 | the step ends at once with an empty line (RETURN is on row 1): SKIP, then `w r y i p * _` land in the next line | row 1 |
| 7 STICK LEFT | … LEFT … | `hose`, r2 | CTRL reads as held (row 2): letters become control codes, CTRL+D ends the program, CTRL+J ends the line; `a d g j l ;` | row 2 |
| 8 STICK RIGHT | … RIGHT … | `hose`, r3 | `4 6 8 0 - 2` appear | row 3 |
| 9 FIRE | hold FIRE, type HOSE, release FIRE, RETURN | `hose`, r4 | RSHIFT reads as held (row 4): HOSE comes out upper case, with `Z C B M >` and spaces | row 4 |
| free | every key once, with and without SHIFT; an empty line ends | one row per character, matching the key map | `??`, a wrong position, a missing or doubled character | the whole key map on real hardware |

**Report back:** the `SESSION VERDICT` line, the per-run lines
`<run>: <verdict>`, the per-step judge lines (performed, notes, defects),
and `session.log`.
