# HLE: AmigaOS calls serviced natively (program mode)

In **program mode** there is no Amiga operating system in guest memory.
A Hunk executable runs on the interpreter, and its exec, dos and console
library calls are answered by 6502 code: high-level emulation (HLE). This
is how vbcc-compiled C programs run (milestone M2).

**Machine mode does not use any of this.** When AROS boots, AROS is the
operating system: its exec, dos and console are guest code, and the HLE
traps are never reached (CONTRACTS state inventory: "HLE / dos / con /
mem state … inert in machine mode"). The only "HLE" in machine mode is the
trackdisk ([STORAGE.md](STORAGE.md)), which works differently: it patches
a running AROS device rather than providing a fake library.

Detailed design records: [docs/HLE.md](../HLE.md) (exec and console
output, the heap), [docs/HLE_DOS.md](../HLE_DOS.md) (files),
[docs/HLE_CON.md](../HLE_CON.md) (keyboard input). They are kept as
historical records; the summary below says what is current, and the list
at the end says which of their statements are out of date.

## How a call is trapped

The host builds fake library bases in guest memory (`host/hle_env.py`):
ExecBase at `$0F0400` and DOSBase at `$0F0C00`, with their full vector
tables below them. Where a real library has `JMP routine` in a vector
slot, these slots start with an A-line word `$A1xx` (xx = an HLE function
index). The program's `jsr -LVO(A6)` executes normally; the next step
fetches `$A1xx`, the core routes it to `hle_aline` (`runtime/ops/hle.s`),
which services the call natively and returns by emulating RTS. One HLE call
counts as one guest instruction.

Every slot without an implementation holds `$A0FF`; calling it halts with
`HALT_ALINE` at a PC that names the slot, and `tests/run_vbcc.py` decodes
that into a message such as `dos.library/ReadItem (LVO -810)`. So an
unsupported program stops with a precise reason instead of running into
garbage.

## The environment

`host/hle_env.py` stages (guest map in [MEMORY.md](MEMORY.md)): a CLI
`Process` and `CommandLineInterface` at `$000A40`/`$000480`, console
FileHandles at `$000B30-$000B90`, a launch stub that enters the program
with A0 = the argument string and D0 = its length, the exit word `$AFFF`
at `EXIT_ADDR` (`$000440`), whose address is the final return address on
the stack, and the HLE clock. The program loads at
`$010000` (`host/hunk.py` parses, `host/image.py` lays out and relocates).
Its return code is D0 when it halts on the exit word.

ExecBase reports **version 40** (Kickstart 3.1, what vbcc's `+aos68k`
targets), so vc.lib uses exec's memory pools instead of walking MemHeader
lists (docs/HLE.md "What vbcc actually calls").

## What is implemented

| Library | Functions |
|---|---|
| exec | OpenLibrary (only `dos.library`, version ≤ 40; every other name, `exec.library` included, returns 0), CloseLibrary (no-op), FindTask(NULL), AllocMem/FreeMem (first-fit heap with coalescing and checked frees, 7 MB), CreatePool/DeletePool/AllocPooled/FreePooled, AllocVec/FreeVec, StackSwap, SetSignal (no signals) |
| dos, console | Input, Output, Read, Write, IsInteractive; console output to the C64 text screen with scrolling; console input from the real keyboard (cooked lines, Ctrl-D or Ctrl-\ for EOF) |
| dos, files | Open, Close, Read, Write, Seek, IoErr, DeleteFile, ChangeMode (the vc.lib case only) |
| dos, other | GetVar (always "not found"), DateStamp (a call counter: one tick per call), SystemTagList (no shell: returns -1) |

Index allocation and every function's semantics: docs/HLE.md "LVO → index
table", docs/HLE_DOS.md "Function table".

## Files: two backends

- **RAM disk** (the default): a flat store of 32 files in the REU at
  `$E00000-$FFFFFF` (2 MB), names up to 31 characters, a fixed 64 KB
  extent per guest-created file (docs/HLE_DOS.md "RAMDISK format"). The
  host can stage files into it before a run and read them back after
  (`host/hle_dos_env.ramdisk_chunks`, `parse_directory`). It is the
  program's current directory and `RAM:`.
- **ULTDOS** (opt-in): files on the Ultimate's own storage through the
  Command Interface. It is selected only when the host writes the key
  `"UCI!"` to `dosio_want_uci` before the first dos call and the interface
  answers. Two open files at most, names relative to the firmware's current
  path; about 61-114 B/s at 1 MHz and 2.7-5 KB/s at 48 MHz. Validated on the
  U64E (fw 3.15, 2026-09-24) (docs/HLE_DOS.md "Hardware validation").

Eight file handles at most. DOS bulk transfers refuse buffers in the I/O
banks (IoErr 212).

## Checked against a real AmigaOS-compatible system

`tests/aros_oracle.py` runs every vbcc test program on Amiga64 and on the
AROS ROM in FS-UAE and compares the outputs. The recorded result: 17
MATCH, 10 DIFF, 1 SKIP, every DIFF triaged, no bug of ours found
(docs/ORACLE.md "Results"). The DIFFs list the known HLE gaps: no ReadItem,
no PutStr, no shell for SystemTagList, 8 handles, 32 RAM-disk entries, no
utility.library, empty exec lists.

## Not implemented

Workbench startup (WaitPort/GetMsg/ReplyMsg), Forbid/Permit, message ports
and signals, AvailMem, Rename, Lock/Examine, NIL:/`*`/CONSOLE: through
Open, libraries other than dos (math libraries for `%f` printf would be
next), base-relative hunk relocations (rejected by `host/hunk.py`). A
program that calls one of the missing library functions halts with the
slot's name; OpenLibrary of another library returns 0; and `host/hunk.py`
refuses base-relative relocations before the run. Open takes names
verbatim (docs/HLE_DOS.md "Names"), so on the RAM disk `NIL:`, `*` or
`CONSOLE:` is an ordinary file name: MODE_NEWFILE creates a file with that
name, and MODE_OLDFILE fails with 205 when no such file exists. The full list
is docs/HLE.md "Still missing" and docs/HLE_DOS.md "Limitations".

## Out-of-date statements in the older HLE documents

| Document, place | Says | Now |
|---|---|---|
| docs/HLE.md "FileHandles", Read | console Read returns 0 (EOF): "there is no keyboard input yet" | keyboard input landed in Wave 5 (docs/HLE_CON.md) |
| docs/HLE.md "Still missing" | "Keyboard input: Read on the console returns EOF" | same: done (docs/HLE_CON.md) |
| `runtime/ops/hle.s` comment above `hle_read` | "kind 0/1/2 -> D0 = 0 (EOF: no keyboard input in this version; NIL:)" | console kinds 0/1 go to `hle_con_read` (the code below the comment); only the NIL kind returns 0 |
| docs/HLE_DOS.md heading "ULTDOS backend: UCI mapping (HARDWARE-UNVALIDATED)" | unvalidated | validated on the U64E on 2026-09-24 (the same file's "Hardware validation" section) |
| docs/HLE_DOS.md "Limitations", Integration | "needs WS-B1's hle.s routing for kinds ≥ $10" | merged in Wave 4: `hle.s` routes file kinds to `hle_dos_read`/`hle_dos_write` |
| docs/HLE.md LVO table (DeletePool) and "Still missing" | the pool calls "need the pool wiring", "a merge step" | wired: `runtime/ops/hle.s` dispatches CreatePool/DeletePool/AllocPooled/FreePooled |
| docs/HLE_CON.md "Not covered / limitations" | "no device runs this wave" | its own "Hardware validation (W6-K)" section records the later U64E runs |

Sources: docs/HLE.md, docs/HLE_DOS.md, docs/HLE_CON.md, docs/ORACLE.md;
`runtime/ops/hle.s`, `hle_mem.s`, `hle_con.s`, `hle_dos.s`, `dosio_ram.s`,
`dosio_uci.s`; `host/hle_env.py`, `host/hle_dos_env.py`; CONTRACTS state
inventory row "HLE / dos / con / mem state".
