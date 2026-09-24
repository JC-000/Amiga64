# M2 — High-level emulation of AmigaOS

Owner: WS-B1 (m2-exec; M2-hle before it). Implements ARCHITECTURE.md
strategy S4: library calls of a guest Hunk program are intercepted at the
interpreter level and serviced natively on the C64. The current function
set is exactly what **vbcc-compiled** stdio/stdlib programs need (their
startup code + vc.lib), which is the M2 exit bar. dos.library *file* I/O
(Open/Close/Seek/IoErr, file-backed Read/Write) belongs to WS-B2 and is
documented in `docs/HLE_DOS.md`.

Run the demos (after `make runtime BUILD=<dir>`):

```sh
python3 -m tests.run_hello --prg <dir>/runner.prg          # hand-encoded v0 hello
tools/vbcc/install.sh                                      # once: vbcc into ~/.local/opt/vbcc
python3 -m tests.run_vbcc  --prg <dir>/runner.prg          # HLE units + vbcc C programs
python3 -m tests.run_vbcc  --prg <dir>/runner.prg --only hello
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

Index ranges (contract C3): `$00-$2F` WS-B1 (`hle.s` table, `HLE_FN_COUNT`),
`$30-$4F` WS-B2 (`hle.s` forwards to `hle_dos_dispatch`), `$50-$FF` free.

**Unimplemented LVOs** — every *other* slot of the exec table (LVO -30 to
-832, all of V40) and the dos table (-30 to -448) holds the sentinel word
`$A0FF`. Calling one halts `HALT_ALINE` with `PC = slot + 2`;
`tests/run_vbcc.py` decodes that to e.g. `exec.library/WaitPort (LVO -384)`.
(Before this, unstaged slots were zero bytes and the guest ran into
garbage — the vbcc red baseline ended `HALT_ILLEGAL`.) Any other
non-`$A1xx`, non-`$AFFF` A-line word also halts `HALT_ALINE`; `$AFFF` stays
the clean-HALT pseudo-op, handled entirely in the core.

## Guest env memory map

Env region `$000400-$000FFF` (WS-B1: `$000400-$000BFF`; WS-B2:
`$000C00-$000FFF`) — above the 68k vector page, below the stack and the
program. Every address is an equate in `runtime/rc/guest.inc`, mirrored in
`host/hle_env.py` (pinned by `host/tests/test_hle_m2next.py`). Staged as
ordinary image chunks through `test_load_chunk`, like the program itself.

| Guest addr | Contents |
|---|---|
| `$000004` | AbsExecBase: u32 BE pointer to ExecBase (`$000800`) |
| `$000400` | `ENV_STATE`: HLE state block (8 bytes BE): `+0` u32 heap_next, `+4` u16 cursor (0-1000), `+6` u8 column, `+7` pad |
| `$000410` | `ENV_LAUNCH`: CLI launch stub `lea ENV_ARGS.w,a0 / moveq #len,d0 / jmp entry.l` |
| `$000420` | `ENV_ARGS`: argument string (≤ 31 bytes + NUL, default `"\n"`) |
| `$000440` | `EXIT_ADDR`: `$AFFF` HALT word (program-exit landing site) |
| `$000444` | `ENV_CLI_NAME`: BSTR command name (60-byte buffer) |
| `$000480` | `ENV_CLI`: `struct CommandLineInterface` (64 bytes; only `cli_CommandName` set) |
| `$0004C0-$0007E7` | exec vector slots, LVO -832 … -30 |
| `$000800` | `EXEC_BASE`: `struct Library`, `lib_Version` = **40** |
| `$000840-$0009E7` | dos vector slots, LVO -448 … -30 |
| `$000A00` | `DOS_BASE`: `struct Library` (no field read) |
| `$000A40` | `ENV_PROCESS`: `struct Process` (228 bytes) — `ln_Type` NT_PROCESS, `tc_SPLower` `$001000`, `tc_SPUpper` `$00F000`, `pr_CIS`/`pr_COS` console BPTRs, `pr_CLI` → `ENV_CLI`, `pr_CES` 0 |
| `$000B30` | `FH_CONOUT`: FileHandle, `fh_Args` = 0 (console out) |
| `$000B60` | `FH_CONIN`: FileHandle, `fh_Args` = 1 (console in) |
| `$000B90` | `FH_NIL`: FileHandle, `fh_Args` = 2 (NIL:) |
| `$001000-$00EFFF` | guest stack (grows down from `$00F000`) |
| `[stack_top]` | u32 BE `EXIT_ADDR` — pre-staged program-exit return address |
| `$100000-$7FFFFF` | HLE heap (`HEAP_BASE`–`HEAP_END`, contract C4) |

**Per-run initialisation.** All mutable HLE state (heap pointer, console
cursor) lives in guest memory at `ENV_STATE`, so staging the env *is* the
HLE init: no runner hook, no C64 BSS that must survive or be cleared, and
Write works without a prior Output call. The host can also read it back
(`tests/run_vbcc.py` checks `heap_next` after the allocator units). Files
in C64 BSS (`hle_ptr`, `hle_cur`, …) are per-call scratch only.

## Program entry and exit

A CLI command is entered with A0 = argument string (ending in `\n`) and
D0 = its length. `test_run_program` zeroes every register, so drivers
that need the ABI (`tests/run_vbcc.py`) start at `ENV_LAUNCH`
(`hle_env.launch_chunks(entry, args)` returns the stub chunks and the PC);
the stub's JMP keeps the stack as the runner set it. Hand-encoded
programs that ignore A0/D0 (`tests/run_hello.py`) can still start at the
entry directly.

`test_run_program` sets `A7 = stack_top` and pushes nothing; the env
pre-stages the 32-bit BE address of the `$AFFF` word at `[stack_top]`.
The program's outer `rts` pops it and the run ends `HALT_OK` with
`A7 = stack_top + 4` and `PC = EXIT_ADDR + 2`. **The program's return code
is D0 at that HALT** (vbcc's startup returns `main()`'s value or the
`exit()` status in D0; `tests/run_vbcc.py` checks it — `fixtures/c/exit.c`
exits 7).

## LVO → index table

| Library | Function | LVO | Index | Semantics |
|---|---|---|---|---|
| exec | OpenLibrary | -552 | `$00` | name A1 == `"dos.library"` → D0 = `DOS_BASE`, else 0; version ignored |
| exec | CloseLibrary | -414 | `$01` | no-op |
| dos | Output | -60 | `$02` | D0 = `pr_COS` (BPTR of `FH_CONOUT`) |
| dos | Write | -48 | `$03` | see FileHandles |
| dos | Input | -54 | `$04` | D0 = `pr_CIS` (BPTR of `FH_CONIN`) |
| dos | Read | -42 | `$05` | see FileHandles |
| dos | IsInteractive | -216 | `$06` | D0 = -1 for console kinds 0/1, else 0 |
| exec | FindTask | -294 | `$07` | A1 = NULL → D0 = `ENV_PROCESS`; any name → 0 |
| exec | AllocMem | -198 | `$08` | see Heap |
| exec | FreeMem | -210 | `$09` | see Heap |
| exec | SetSignal | -306 | `$0A` | D0 = 0 (no signals, Ctrl-C never set), nothing stored |
| exec | CreatePool | -696 | `$0B` | 8-byte header from the heap holding memFlags; thresh > puddle → 0 |
| exec | DeletePool | -702 | `$0C` | no-op (pool memory leaked until the next run) |
| exec | AllocPooled | -708 | `$0D` | AllocMem(D0, pool memFlags); NULL pool → 0 |
| exec | FreePooled | -714 | `$0E` | FreeMem(A1, D0) |

Indices are mirrored verbatim in `runtime/ops/hle.s` (dispatch table,
`HLE_FN_COUNT` = 15) and `host/hle_env.py` (`EXEC_FUNCS`/`DOS_FUNCS`,
`HLE_FN_COUNT`); `host/tests/test_hle_m2next.py` checks density, range,
uniqueness across packages and the table order in `hle.s`.

Register convention (AmigaOS): args in registers, result in D0; handlers
write only D0, so D1/A0/A1 keep the caller's values and D2-D7/A2-A6 are
preserved. A6 is never read — the slot address selects the function.

### What vbcc actually calls (and why exec says V40)

Traced from `vc +aos68k` output (vbcc 0.9h P3, disassembled with capstone
from the Hunk code, then confirmed by running): the startup's CLI path
calls **FindTask(NULL), OpenLibrary("dos.library"), AllocMem (argv block,
`MEMF_PUBLIC|MEMF_CLEAR`), Input, Output, CloseLibrary, FreeMem**; vc.lib
adds **IsInteractive** (×3, stdio buffering), **Write** (stdout flush) and
**SetSignal** (Ctrl-C check); malloc/free go through exec memory pools
**CreatePool / AllocPooled / FreePooled / DeletePool** when
`SysBase->lib_Version >= 39`. With an older exec, vc.lib instead emulates
pools with AllocMem + `Allocate`/`Deallocate` on MemHeaders plus
`AddHead`/`AddTail`/`Remove`/`RemHead` — six list/MemHeader primitives
that would have to walk guest structures through the memory API. The fake
ExecBase therefore advertises V40 (Kickstart 3.1, what `+aos68k` targets):
four trivial native pool calls instead. The V36+ side effects are benign:
stderr = `pr_CES` (0 → falls back to Output()), and the StackSwap path is
not taken because `tc_SPUpper - tc_SPLower` (56 KB) ≥ vbcc's `__stack`
(4 KB). Not called on the CLI path (and not implemented): the Workbench
branch (WaitPort, GetMsg, ReplyMsg, Forbid, CurrentDir, Open("NIL:")),
AllocVec/FreeVec/StackSwap, Close (the startup closes `4(argv block)`,
which MEMF_CLEAR leaves 0).

## FileHandles (contract C2)

Every handle is a **BPTR** (address >> 2) to a longword-aligned 44-byte
`struct FileHandle`; the HLE reads only `fh_Args` (offset 36) = kind:
`0` console out, `1` console in, `2` NIL:, `$10+n` WS-B2 file slot n.
`FH_MAGIC $C0DE` is retired.

- **Write** (fh D1, buf D2, len D3): kind ≥ `$10` (any upper byte set
  counts, so WS-B2's closed marker `$FFFFFFFF` routes too) → `jmp
  hle_dos_write` with the call state untouched (D1-D3, op word, return
  address on the stack). Kind 0/1 → console, D0 = D3. Kind 2 → discarded,
  D0 = D3. D1 = 0, kinds 3-`$0F`, or D3 < 0 → D0 = -1. Length: low 24
  bits.
- **Read** (fh D1, buf D2, len D3): kind ≥ `$10` → `jmp hle_dos_read`.
  Kinds 0/1/2 → D0 = 0 (EOF: there is no keyboard input yet). D1 = 0 or
  kinds 3-`$0F` → -1.
- Console output: ASCII → screen codes (`$20-$3F` direct, `$40-$5F`
  minus `$40`, `$60-$7F` minus `$60` — lowercase renders uppercase in the
  default charset — `≥$80` → `?`), `$0A` newline, other controls ignored.
  **Scrolls** at line 25 (lazily: a line ending on the last row leaves the
  cursor "pending" at 1000 and the screen moves up only when the next
  character or newline needs the row, so a final `\n` does not lose the
  top line).

## Heap (AllocMem/FreeMem, contract C4)

Bump allocator over `HEAP_BASE $100000`–`HEAP_END $7FFFFF` (7 MB), state =
`heap_next` in `ENV_STATE`.

- **AllocMem(size D0, flags D1)**: size rounded up to 8 (MEM_BLOCKSIZE);
  result 8-byte aligned. Size 0, size > 7 MB, or not enough room → D0 = 0.
  `MEMF_CLEAR` (bit 16) zero-fills the whole rounded block; every other
  flag (CHIP/FAST/PUBLIC/…) is accepted and ignored — all guest memory is
  alike. Fresh memory is **not** zero without MEMF_CLEAR (the unit tests
  pre-poison the heap with `$A5` to prove both).
- **FreeMem(block A1, size D0)**: if the rounded block ends exactly at
  `heap_next` (it is the most recent live allocation) `heap_next` drops
  back to its start — LIFO reclaim. Every other free (not the top block,
  NULL, outside the heap) is accepted and **leaked**. Correct by contract:
  memory is only ever handed out above `heap_next`, so no live block can
  be overlapped, and FreeMem never writes guest memory except `heap_next`.
  A program that frees out of order just uses more heap; the next run
  re-stages `heap_next = HEAP_BASE`.
- Pools map onto the same allocator (header = 8 bytes holding memFlags so
  MEMF_CLEAR pools clear); DeletePool leaks.
- **MEMF_CLEAR cost**: the clear goes through the memory API, one
  `mem_write32` per longword ≈ 45-50 C64 cycles per byte (≈ 50 ms per KB
  at 1 MHz, ~1 ms/KB at 48 MHz). vbcc only clears the ~20-byte argv block
  (its pools are created with flags 0 and calloc zeroes in 68k code), so
  this is noise today. A REC fill (fixed C64 source address) would be ~1
  cycle/byte but must `jsr mem_rec_dirty` (C1) *and* invalidate the fetch
  window, whose flag `w_inv` is core-private — not done; needs a core
  export (e.g. a `mem_fill` API) if large cleared allocations show up.

## Tests

- `host/tests/test_hle.py` (v0) and `host/tests/test_hle_m2next.py`: env
  layout, LVO slots, sentinels, index allocation, Process/CLI/FileHandle
  fields, launch stub encoding, guest.inc mirror, placement inside
  `$000400-$000BFF`.
- `python3 -m tests.run_vbcc` — VICE, two legs in one session:
  - **HLE units** (`fixtures/c/hle_unit/*.s`, assembled with vasm at test
    time): register-level checks of every call — allocator rounding,
    MEMF_CLEAR vs poison, top-block reclaim, exhaustion at exactly 7 MB,
    pools, console/NIL:/NULL handles, Write without Output, negative
    length, the C2 hand-off (`u_file`/`u_file2`: kind `$10` and
    `$FFFFFFFF` reach `hle_dos_write`/`hle_dos_read` with D1-D3 and the
    stack untouched — pinned against the WS-B2 stub; once WS-B2 lands the
    test instead requires the call NOT to be serviced as console: D0 = -1,
    blank screen), FindTask/SetSignal/unknown library, and the
    unimplemented-LVO sentinel.
  - **vbcc C programs** (`fixtures/c/*.c`, compiled with `vc +aos68k -O2`
    at test time into `build/vbcc/`; never committed): `hello` (printf),
    `exit` (exit(7) → D0 = 7), `scroll` (30 lines through 25 rows),
    `alloc` (malloc/free of 16 B … 70 000 B, fill + verify, calloc zero
    check). Checks: `HALT_OK`, exit code in D0, `A7 = stack_top + 4`, and
    the full 25-row screen. The first `$2000` bytes of the heap are
    poisoned with `$A5` before each run (`--no-poison` to disable).
  - The test derives `build/vbcc/aos68k-amiga64` from vbcc's `aos68k`
    config minus vlink's `-Rshort`: `host/hunk.py` does not parse
    `HUNK_RELOC32SHORT` (`$3F7`), which `-Rshort` emits.

## Still missing (next)

- **Keyboard input**: Read on the console returns EOF.
- **More libraries**: OpenLibrary knows only dos.library (math libraries
  for printf `%f` would be next for vbcc; not yet exercised).
- **Workbench startup path**, Forbid/Permit, message ports, signals.
- **Heap reuse**: out-of-order FreeMem leaks; a free-list allocator is the
  upgrade if long-running programs need it. AvailMem is not implemented
  (not called by vbcc).
- **Fast MEMF_CLEAR** via a core fill primitive (see Heap).
- **CloseLibrary bookkeeping** once OpenLibrary tracks anything.
- `HUNK_RELOC32SHORT` support in `host/hunk.py` (not WS-B1's file) would
  let the stock vbcc config be used unchanged.
