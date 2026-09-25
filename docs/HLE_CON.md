# Console input (W5-K)

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
| any | — | a `COLUMN BLEED?` in this step |

In a control run, only the triggering action is required (the key held,
or the stick seen). The broken build may garble the typing itself.

**Run verdicts** (`judge_run`):
- **Fixed build:** PASS only when every step is P, was performed, and
  shows no defect. It is FAIL on any F or any defect. Otherwise it is
  INCOMPLETE. **A SKIP is never a pass.**
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
- **The follow-up, for INCONCLUSIVE only**, is to re-run just that control
  with sparse polling, holding each key for about 4 s, for example
  `--runs nosettle@48 --poll 1.0`. That is about 10 times fewer reads
  during the holds. A non-default plan is never VALIDATED, so its output
  is evidence for the supervisor's judgment, not a verdict.

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
U64_HOST=10.43.23.81 python3 -m tests.kbd_echo --prg build/<pkg>/runner.prg \
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
     `SESSION VERDICT: …`.

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
