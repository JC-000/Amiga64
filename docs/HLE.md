# M2 — High-level emulation of AmigaOS (v0)

Owner: M2-hle. Implements ARCHITECTURE.md strategy S4: library calls of a
guest Hunk program are intercepted at the interpreter level and serviced
natively on the C64. v0 is exactly enough to run a hello-world console
program to completion with output on the C64 text screen.

Run the exit demo (after `make runtime BUILD=<dir>`):

```sh
python3 -m tests.run_hello --prg build/runner.prg
```

## How a call is trapped

AmigaOS library calls are `jsr -LVO(A6)` through a library base pointer.
The host-side environment builder (`host/hle_env.py`) places fake library
bases in guest memory whose vector slots (6 bytes at `base - LVO`, where a
real library holds `JMP routine`) instead start with an **A-line opcode
word `$A1xx`** (`xx` = HLE function index). The `jsr` executes normally
(return address pushed on the guest stack, PC = slot address); the next
`emu_step` fetches `$A1xx`, `op_aline` (core) routes every non-`$AFFF`
A-line word to `hle_aline` (`runtime/ops/hle.s`), which dispatches on the
index, services the call natively, and retires by **emulating RTS** (pop
PC from the guest stack, `A7 += 4`, mask to 24 bits, `jmp dispatch_next`).
One `$A1xx` word therefore counts as one guest instruction.

`$A1xx` with an unknown index, and any other non-`$AFFF` `$Axxx` word,
halt with reason `HALT_ALINE` (the pre-M2 trap semantics). `$AFFF` remains
the clean-HALT pseudo-op, handled entirely in the core.

## Guest env memory map

Reserved env region `$000400-$000FFF` — above the 68k vector page, below
the stack (`$00F000`) and the first program segment (`$010000`). Staged as
ordinary image chunks (`{u32 LE offset, u32 LE len, data}` framing,
composable with `host/image.py` output) through the same `test_load_chunk`
path as the program itself.

| Guest addr | Contents |
|---|---|
| `$000004` | AbsExecBase: u32 BE pointer to ExecBase (`$000800`) — the one absolute address AmigaOS guarantees |
| `$000440` | `EXIT_ADDR`: `$AFFF` HALT word (program-exit landing site) |
| `$0005D8` | ExecBase-552 vector slot: `$A100` (OpenLibrary) |
| `$000672` | ExecBase-414 vector slot: `$A101` (CloseLibrary) |
| `$000800` | `EXEC_BASE`: fake ExecBase structure (v0: no fields read) |
| `$0009C4` | DOSBase-60 vector slot: `$A102` (Output) |
| `$0009D0` | DOSBase-48 vector slot: `$A103` (Write) |
| `$000A00` | `DOS_BASE`: fake DOSBase structure (v0: no fields read) |
| `[stack_top]` | u32 BE `EXIT_ADDR` — pre-staged program-exit return address |

## Program exit

`test_run_program` sets `A7 = stack_top` ($00F000) and pushes nothing, and
`emu_reset` never touches guest memory — so the env simply pre-stages the
32-bit BE address of the `$AFFF` word at `[stack_top]` during chunk
staging. The program's outer `rts` pops it, the next fetch executes
`$AFFF`, and the run ends `HALT_OK` with `A7 = stack_top + 4` and
`PC = EXIT_ADDR + 2`. (Chosen over pushing from the driver so any driver
that stages `hle_env.env_chunks()` gets exit handling with unchanged
`test_run_program` semantics.)

## LVO → index table (v0)

| Library | Function | LVO | HLE index / opcode word |
|---|---|---|---|
| exec.library | OpenLibrary | -552 | `$00` / `$A100` |
| exec.library | CloseLibrary | -414 | `$01` / `$A101` |
| dos.library | Output | -60 | `$02` / `$A102` |
| dos.library | Write | -48 | `$03` / `$A103` |

Indices are assigned here and mirrored verbatim in `runtime/ops/hle.s`
(dispatch table) and `host/hle_env.py` (`EXEC_FUNCS`/`DOS_FUNCS`). Add new
functions by appending an index in both places and bumping
`HLE_FN_COUNT`.

## Calling convention / v0 semantics

Register conventions follow AmigaOS (args in registers, result in D0;
A6 = library base by convention but v0 never reads it — the slot address
itself selects the function).

- **OpenLibrary** (name `A1`, version `D0`): compares the guest string at
  `A1` (read via `mem_read8`) against `"dos.library"`. Match → `D0 =
  DOS_BASE`; anything else → `D0 = 0`. Version ignored.
- **CloseLibrary** (base `A1`): no-op, registers preserved.
- **Output**: `D0 =` magic console file handle `$0000C0DE` (`FH_MAGIC`,
  defined in both `hle.s` and `hle_env.py`). Also homes the screen cursor
  to `$0400` — Output is the mandatory first console call (the runner has
  no HLE init hook and BSS is not cleared at boot), so v0 programs must
  call Output before Write.
- **Write** (fh `D1`, buffer `D2`, length `D3`): copies guest bytes to the
  C64 text screen at `$0400`, converting ASCII to screen codes: `$20-$3F`
  direct, `$40-$5F` minus `$40` (`@`, A-Z, `[\]^_`), `$60-$7F` minus `$60`
  (a-z render uppercase in the default charset), `≥$80` → `?`, `$0A`
  advances to the next 40-column line, other control bytes ignored.
  Output stops silently at screen end (1000 cells, no scrolling); the fh
  and the upper 16 bits of the length are ignored. Returns `D0 = D3`.

## What v0 does not do (M2-next)

- **AllocMem/FreeMem** — needed by any compiler-generated startup code;
  a bump allocator over a reserved guest region is enough at first.
- **Real file handles** — Output's magic constant should become a fake
  FileHandle structure; Write should check `D1`, and Open/Close/Input
  belong with it (backed by the Ultimate DOS command interface on
  hardware).
- **vbcc/gcc-m68k compiled programs** — the real M2 exit bar. Their CRT0
  needs at least: correct `pr_CLI`-less process assumptions, AllocMem,
  and possibly `SetSignal`/stack handling; run one and implement what it
  traps on.
- **Scrolling + cursor state decoupled from Output** — give the runner an
  HLE init entry (or clear the cursor from `emu_reset`'s caller) so Write
  works without a prior Output, and scroll at line 25.
- **CloseLibrary bookkeeping** once OpenLibrary tracks anything.
