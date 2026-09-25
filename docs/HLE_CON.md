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
   holds the key codes of the table above. This is a **test seam**.
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
- It sets `CIA1_DDRA` = `$FF` and `CIA1_DDRB` = `$00`. It drives one column
  low at a time on `CIA1_PRA` and reads the rows on `CIA1_PRB` (low =
  down). Then it deselects (`CIA1_PRA` = `$FF`). All names come from
  `rc/ram.inc`.
- **Joystick guard**: a sample where port B is low while *no* column is
  selected comes from a joystick in port 1, not from keys, and is skipped.
- **Debounce**: a changed matrix must hold still for `CON_STABLE` = 64
  changes of `VIC_RASTER` (about 4 ms) before it is committed. This time
  base does not depend on CPU speed, so it is the same at 1 MHz and at
  48 MHz turbo. At a commit, a key that is down now but was not down at the
  previous commit is *new*. The first new non-modifier key (lowest column,
  then lowest row) is decoded, and `hle_con_nkey` counts it. If two keys go
  down within one debounce window, only one of them is taken.
- `hle_con_init` starts from "every key down". A key that is already held
  when input starts, such as the RETURN that launched the program, counts
  only after it has been released and pressed again.

### Key map

The matrix index is column × 8 + row. The column is the port-A bit and the
row is the port-B bit. The tables are `hle_con_kplain` / `hle_con_kshift`
in `hle_con.s`, mirrored in `tests/run_con.py` (`KEYMAP_PLAIN` /
`KEYMAP_SHIFT`) and cross-checked by `host/tests/test_hle_con.py`. Letters
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
- SHIFT (left or right), CTRL and C= are modifiers only. C= does nothing.
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
| Line discipline: remainder across Reads, EOF variants, BS/DEL, overflow at 127, echo, rubout across a scroll and a row boundary | `u_con1`-`u_con4` (register level: 5 longs per Read written to `$100000`) | scripted queue |
| vc.lib stdio over it: `fgets`, `getchar` to EOF, `scanf`, backspace editing, 3-byte `setvbuf` Reads (`hle_con_nread` = 11 pinned), echo without program output, prompt flush | `script/con_*` (full 25-row screen + exit code + A7) | scripted queue |
| **CIA1 matrix scan, debounce, modifiers, key map (every character of the map, both EOF keys)** | `matrix/con_*`: the same programs, and the screen and exit code must be identical to the scripted run | **real key matrix**: VICE snapshot edit (below) |
| Programs that never read stdin do not wait | `script/hello`, `matrix/hello`, and `run_vbcc` (whole suite, with the empty-stdin wiring) | none |
| Batch drivers: `run_vbcc.stage_and_run` alone gives EOF (`con_getchar` → 0 chars, `con_fgets` → NULL) and the unchanged units `u_dos`/`u_dos3` pass | `batch/*` (console reset to keyboard default first; this driver stages no input) | none (the run_vbcc wiring) |
| Host key-map mirror = 6502 tables, seam symbols exported | `host/tests/test_hle_con.py` | none (static) |

**How the matrix leg presses a key.** VICE's binary monitor cannot press a
key in the matrix. Its "keyboard feed" writes the KERNAL buffer at `$0277`,
and this Read never looks at that buffer. VICE's snapshot does carry the
matrix, though: module `KEYBOARD` v1.1 holds `keyarr[16]` (per port-A line,
the port-B bits that are down) and `rev_keyarr[8]`, and VICE computes CIA1
port reads from exactly those arrays. `tests/run_con.py` `MatrixKeys`
dumps a snapshot, sets the key's bits (plus SHIFT/CTRL), undumps it and
resumes. It then waits until `hle_con_nkey` moves, releases the key the
same way, and waits until the debounced `hle_con_mat` is all up before the
next key. The program runs asynchronously from a trampoline at `$0350` in
the cassette buffer while this happens: `jsr test_run_program`, then a done
flag, then a spin. Each edit round-trips the full ~17 MB snapshot with a
16 MB REU, about 0.2 s. `con_keys` (98 keys) takes about 55 s.

**Mutants** (`CON_MUT_*` flags in `hle_con.s`, built with
`python3 -m tests.rec_build --out DIR CON_MUT_x`): `NOECHO`, `NOBS`,
`NOREM` (rest of the line dropped), `NOEOF` (EOF never signalled) and
`NOSCAN` (the matrix is never read). Each one turns `run_con` red.
`NOSCAN` fails only the matrix leg, which shows that leg does not reach
the queue. `NOWIRE` is the `batch` leg run with `--vbcc-module` pointing at
a copy of `run_vbcc.py` without the wiring line. It fails
`batch/con_getchar` as a hang, caught by the 20 s `--timeout`, in 22 s.
Every run has an instruction budget (`--budget`, default 2 000 000; the
largest program here needs about 94 000) and every harness call a 60 s
default timeout. Results: `build/evidence/w5-con/mutants.txt`.

## Not covered / limitations

- **Real key presses on real hardware** are not tested: no device runs this
  wave. The CIA1 access pattern is the standard one, but physical bounce
  and the 64-line debounce window are unmeasured on the U64E, and so is
  ghosting when three or more keys are held.
- The **joystick guard** has no test. `inject_joystick` through the harness
  had no visible effect on `$DC01` in this VICE setup, so no leg drives it.
- No type-ahead (keys are seen only while a Read waits), no auto-repeat,
  and one key per debounce window.
- The line is at most 127 characters. No history, no cursor movement
  inside the line, no Ctrl-X line kill.
- The PETSCII graphics keys and the cursor/function keys give nothing.
  RUN/STOP is not Ctrl-C: SetSignal still never reports a break.
- `IsInteractive` is unchanged (console = DOSTRUE), and that is what makes
  vc.lib use small Reads and flush stdout.
