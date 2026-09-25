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

**Unimplemented LVOs** — every *other* slot of each library's FULL vector
table (exec LVO -6 … -828, dos -6 … -1158: the deepest known LVOs,
`hle_env.LVO_DEPTH`) holds the sentinel word `$A0FF`. Calling one halts
`HALT_ALINE` with `PC = slot + 2` inside that library's own table;
`tests/run_vbcc.py` decodes that to e.g. `dos.library/ReadItem (LVO -810)`.
(58f9755 staged only exec -30 … -832 and dos -30 … -448 in the env region;
a deeper dos call such as ReadItem or PutStr landed in the *exec* table
and ran an exec HLE call — b1-review F1.)
(Before this, unstaged slots were zero bytes and the guest ran into
garbage — the vbcc red baseline ended `HALT_ILLEGAL`.) Any other
non-`$A1xx`, non-`$AFFF` A-line word also halts `HALT_ALINE`; `$AFFF` stays
the clean-HALT pseudo-op, handled entirely in the core.

## Guest env memory map

Env region `$000400-$000FFF` (WS-B1: `$000400-$000BFF`; WS-B2:
`$000C00-$000FFF`) — above the 68k vector page, below the stack and the
program — plus the **library region `hle $0F0000-$0FFFFF`** (C4 amendment
2026-09-24: library bases and their full vector tables; the program load
region is `$010000-$0EFFFF`). Every address is an equate in
`runtime/rc/guest.inc`, mirrored in `host/hle_env.py` (pinned by
`host/tests/test_hle_m2next.py` / `test_hle_libs.py`). Staged as ordinary
image chunks through `test_load_chunk`, like the program itself: the WS-B1
env part `$000400-$000BFF` is ONE chunk (every gap staged as zero) and the
library area `$0F0000-$0F0C67` is ONE chunk (tables, bases, names and all
padding). `$0F0C68-$0FFFFF` is reserved, not staged.
`hle_env.check_program_fits(manifest)` rejects a program image that
would reach past `$0EFFFF` (`host/image.py` does not bound the region).

| Guest addr | Contents |
|---|---|
| `$000004` | AbsExecBase: u32 BE pointer to ExecBase (`$0F0400`) |
| `$000400` | `ENV_STATE`: HLE state block (8 bytes BE): `+0` u32 heap top (see Heap), `+4` u16 cursor (0-1000), `+6` u8 column, `+7` pad |
| `$000408` | `ENV_CLOCK`: HLE clock (8 bytes BE): `+0` u32 ds_Days, `+4` u16 ds_Minute (0-1439), `+6` u16 ds_Tick (0-2999); staged 0/0/0 (`hle_env.clock_block`), see Clock |
| `$000410` | `ENV_LAUNCH`: CLI launch stub `lea ENV_ARGS.w,a0 / moveq #len,d0 / jmp entry.l` |
| `$000420` | `ENV_ARGS`: argument string (≤ 31 bytes + NUL, default `"\n"`) |
| `$000440` | `EXIT_ADDR`: `$AFFF` HALT word (program-exit landing site) |
| `$000444` | `ENV_CLI_NAME`: BSTR command name (60-byte buffer) |
| `$000480` | `ENV_CLI`: `struct CommandLineInterface` (64 bytes; `cli_CommandName`, `cli_DefaultStack` = `$3801` longwords) |
| `$000A40` | `ENV_PROCESS`: `struct Process` (228 bytes) — `ln_Type` NT_PROCESS, `tc_SPLower` `$001000`, `tc_SPUpper` `$00F004`, `pr_StackSize` `$E004`, `pr_CIS`/`pr_COS` console BPTRs, `pr_CLI` → `ENV_CLI`, `pr_CES` 0 |
| `$000B30` | `FH_CONOUT`: FileHandle, `fh_Args` = 0 (console out) |
| `$000B60` | `FH_CONIN`: FileHandle, `fh_Args` = 1 (console in) |
| `$000B90` | `FH_NIL`: FileHandle, `fh_Args` = 2 (NIL:) |
| `$001000-$00F003` | guest stack (A7 starts at `$00F000` pointing at the exit longword; `tc_SPUpper` = `$00F004` so that longword is inside the stack) |
| `[stack_top]` | u32 BE `EXIT_ADDR` — pre-staged program-exit return address |
| `$0F0000-$0F00C3` | padding (zero) |
| `$0F00C4-$0F03FF` | exec vector slots, LVO -828 … -6 |
| `$0F0400` | `EXEC_BASE`: full `struct ExecBase` (632 bytes, zero except): every List header an empty list (`lh_Head` = &`lh_Tail`, `lh_TailPred` = &`lh_Head`, `lh_Type` set — MemList, ResourceList, DeviceList, IntrList, LibList, PortList, TaskReady, TaskWait, the 5 SoftInts, SemaphoreList; `ex_MemHandlers` MinList), `ln_Type` NT_LIBRARY, `ln_Name` → "exec.library", `lib_NegSize` 828, `lib_PosSize` 632, `lib_Version` **40**, `MaxLocMem` (62) `$200000` (nominal 2 MB chip top — all guest RAM is alike), `ThisTask` (276) = `ENV_PROCESS`, `AttnFlags` (296) 0 (plain 68000, no FPU) |
| `$0F0678-$0F0779` | padding (zero) |
| `$0F077A-$0F0BFF` | dos vector slots, LVO -1158 … -6 |
| `$0F0C00` | `DOS_BASE`: `struct DosLibrary` (70 bytes): `ln_Name` → "dos.library", `lib_NegSize` 1158, `lib_Version` **40**; everything else 0 (it has no List headers) |
| `$0F0C48` | library name strings (32 bytes) |
| `$100000-$7FFFFF` | HLE heap (`HEAP_BASE`–`HEAP_END`, contract C4): `HEAP_CTL` + blocks, see Heap |

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
(`hle_env.env_chunks(stack_top, entry=entry, args=...)` builds the stub
into the env chunk; `launch_chunks` returns it separately);
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
| exec | OpenLibrary | -552 | `$00` | name A1 == `"dos.library"` (exact) and version D0 ≤ 40 (unsigned) → D0 = `DOS_BASE`, else 0 |
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
| exec | DeletePool | -702 | `$0C` | frees every block of the pool and the header (see Heap; needs the pool wiring) |
| exec | AllocPooled | -708 | `$0D` | AllocMem(D0, pool memFlags), block owned by the pool; NULL pool → 0 |
| exec | FreePooled | -714 | `$0E` | FreeMem(A1, D0) |
| exec | AllocVec | -684 | `$0F` | as exec: AllocMem(D0+4, D1), size+4 stored in the first longword, D0 = block+4 (8n+4); D0 ≥ 16 MB or no room → 0 |
| exec | FreeVec | -690 | `$10` | NULL → no-op; else FreeMem(A1-4, [A1-4]) |
| exec | StackSwap | -732 | `$11` | A0 = StackSwapStruct: tc_SPLower/Upper ⇄ stk_Lower/Upper, A7 ⇄ stk_Pointer; the return address is popped from the old stack, written at new-A7 − 4 (exec returns via RTS on the new stack) and the call returns on the new one (a second call swaps back) |
| dos | GetVar | -906 | `$12` | no environment/local variables: D0 = -1 (not found), buffer untouched, **IoErr = ERROR_OBJECT_NOT_FOUND (205)** — stored into WS-B2's exported `hle_dos_ioerr` after `jsr hle_dos_ensure` (`docs/HLE_DOS.md`). vc.lib's `getenv()` → NULL |
| dos | DateStamp | -192 | `$13` | D1 = `struct DateStamp *` (address): the HLE clock advances by **one tick per call** and the advanced ds_Days/ds_Minute/ds_Tick are stored there; D0 = D1. See Clock below |
| dos | SystemTagList | -606 | `$14` | no shell: D0 = -1, so `system("cmd")` returns -1. `system(NULL)` does not call dos: vc.lib at V36+ reports a shell (returns 1) |

### Clock (DateStamp)

The HLE has no time source that is both cheap and available in every
build: the only instruction counter lives in the test runner
(`tests/runner/run_program.s` `icount`, zero page shared with the loader),
not in the core, and the hardware mailbox path has none. So the clock is a
**call counter**: each DateStamp call first advances `ENV_CLOCK` by one
tick (1/50 s, `TICKS_PER_SECOND`), carrying at 3000 ticks into ds_Minute
and at 1440 minutes into ds_Days, then returns it. The carries test `>=`
(and the minute test runs on every call), so a guest-corrupted clock —
tick ≥ 3000 or minute ≥ 1440 — is normalised by the next call
(`u_clock2`). `ENV_CLOCK` is guest
memory staged 0/0/0 (1978-01-01 00:00:00) with the env, so every run starts
there; the first call of a run returns tick 1.

- Monotonic: two calls never return the same stamp, and the stamp never
  goes back within a run (days wrap only after 2^32 days).
- `time()` (vc.lib: `((days + 2922) * 1440 + minute + gmtoffset) * 60 +
  tick / 50`, gmtoffset 0 because OpenLibrary("locale.library") fails) is
  252 460 800 for the first 49 calls and then counts one second per 50
  calls. A `t = time(0); while (time(0) == t);` loop ends after 49 more
  calls (`fixtures/c/tspin.c`, run under a 3 M instruction budget).
- Guest code sees no relation to real or emulated time; programs that
  measure elapsed time get "number of clock reads / 50" seconds.
- **`clock()` stays -1**: vc.lib's `time/clock.c` is `moveq #-1,d0; rts`
  (disassembled, 4 bytes) and never calls the OS, so there is nothing for
  the HLE to service. `tspin` pins `clock=-1`.

Indices are mirrored verbatim in `runtime/ops/hle.s` (dispatch table,
`HLE_FN_COUNT` = 21) and `host/hle_env.py` (`EXEC_FUNCS`/`DOS_FUNCS`,
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
stderr = `pr_CES` (0 → falls back to Output()). When a program asks for
more stack than `tc_SPUpper - tc_SPLower` (`$E004` = 57 348 bytes) via
vbcc's `__stack` variable (default 4 KB), the startup takes its V36+ path:
**AllocVec** a new stack, **StackSwap** to it, and at exit StackSwap back
+ **FreeVec** — implemented (`fixtures/c/bigstack.c`, `__stack = 200000`,
runs on a heap stack). Not called on the CLI path (and not implemented):
the Workbench branch (WaitPort, GetMsg, ReplyMsg, Forbid, CurrentDir,
Open("NIL:")), Close (the startup closes `4(argv block)`, which MEMF_CLEAR
leaves 0; Close on the console handles is DOSTRUE, `docs/HLE_DOS.md`). Programs that read ExecBase fields directly get a full-size
ExecBase (ThisTask, AttnFlags, MaxLocMem, versions — `fixtures/c/sysbase.c`).

## FileHandles (contract C2)

Every handle is a **BPTR** (address >> 2) to a longword-aligned 44-byte
`struct FileHandle`; the HLE reads only `fh_Args` (offset 36) = kind:
`0` console out, `1` console in, `2` NIL:, `$10+n` WS-B2 file slot n.
`FH_MAGIC $C0DE` is retired.

- **Write** (fh D1, buf D2, len D3): kind ≥ `$10` (any upper byte set
  counts, so WS-B2's closed marker `$FFFFFFFF` routes too) → `jmp
  hle_dos_write` with the call state untouched (D1-D3, op word, return
  address on the stack). Kind 0/1 → console, D0 = D3. Kind 2 → discarded,
  D0 = D3. D1 = 0, kinds 3-`$0F`, or D3 outside `0 … $FFFFFF` (negative,
  or ≥ 16 MB — no such buffer fits the 24-bit guest space) → D0 = -1,
  nothing written.
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

## Heap (AllocMem/FreeMem, pools, AllocVec — WAVE5 C1)

`runtime/ops/hle_mem.s` (W5-H): a first-fit free-list allocator with
boundary tags over `HEAP_BASE $100000`–`HEAP_END $7FFFFF` (7 MB). All of
its state is guest memory; the host stages only `ENV_STATE+0`.

### Layout

| Guest addr | Contents |
|---|---|
| `ENV_STATE+0` | u32 **top**: start of the never-allocated space ("wilderness") above the last block. Staged `HEAP_BASE` by `hle_env.state_block()` = pristine: the first call of a run formats `HEAP_CTL` and sets top = `HEAP_FIRST` |
| `HEAP_CTL $100000` | +0 u32 free-list head (0 = empty), +4 u32 **rejected-free count** (see Invalid frees) |
| `HEAP_FIRST $100008` … top | blocks, back to back, each 8-aligned |

Every block starts with an 8-byte header; AllocMem returns the payload
after it (so the first result of a run is `$100010`):

| Offset | Used block | Free block |
|---|---|---|
| +0 u32 | size (header included, multiple of 8, ≥ 16) \| flags | same |
| +4 u32 | `$5A` << 24 \| owner (pool header, 0 = AllocMem/AllocVec) | next free block |
| +8 u32 | (payload) | previous free block (0 = list head) |
| end-4 u32 | (payload) | size (footer) |

Flags (low 3 bits of the size word): bit 0 USED, bit 1 PFREE (the block
below is free, its footer is valid), bit 2 SLACK (the block is 8 bytes
larger than the rounded request: a remainder < 16 cannot be split off).
Invariants: two free blocks never touch, and no free block touches the
wilderness (both are merged on free), so the block just below top is
always used.

### Calls

- **AllocMem(size D0, flags D1)**: size rounded up to 8; a block needs
  size + 8. First fit over the free list (LIFO order: the most recently
  freed block first); a block with ≥ 16 bytes to spare is split and the
  upper part stays free in the same list position, else it is taken whole
  (SLACK when 8 are left). If nothing fits, the block is carved from the
  wilderness (top moves up). Size 0, size > 7 MB, or no room → D0 = 0; the
  largest request that fits a fresh heap is `$6FFFF0` (7 MB − control
  block − one header; `u_heap5`). `MEMF_CLEAR` (bit 16) zero-fills the
  whole rounded request, also on reused memory (`u_heap3`); other flags
  are ignored — all guest memory is alike. Memory is **not** zero without
  MEMF_CLEAR (the unit tests pre-poison the heap with `$A5`).
- **FreeMem(block A1, size D0)**: validated (below), then merged with a
  free upper neighbour, a free lower neighbour (found through the PFREE
  bit + footer) and/or the wilderness (top drops back); a block that
  merges with nothing is pushed on the free list. FreeMem in any order
  makes the memory reusable (`heap_churn`: 16 MB through the 7 MB heap in
  random order; `heap_coalesce`: two separated holes plus the block
  between them take a 3 MB request).
- **Pools**: CreatePool allocates an 8-byte header (a 16-byte block,
  holding memFlags so MEMF_CLEAR pools clear); AllocPooled allocates a
  normal block whose header records the pool as owner (`hle_pool_alloc`);
  FreePooled = FreeMem. **DeletePool** (`hle_pool_delete`) walks every
  block from `HEAP_FIRST` to top (the blocks tile the heap, so size words
  are enough), releases each used block owned by the pool, then frees
  the header. A0 = NULL is a no-op; an A0 that is not a live 16-byte
  block is rejected like an invalid free and frees nothing (`u_heap4`;
  `heap_pool`: 40 × create / 5 × 52 000-byte AllocPooled / delete, 1.4×
  the heap). Puddles are not modelled.
- **AllocVec/FreeVec** (hle.s): AllocMem(size + 4) with the size stored in
  the first longword, result 8n+4; FreeVec = FreeMem(A1 − 4, [A1 − 4]).
- **AvailMem** is not implemented (no vbcc program calls it).

**Pool wiring (supervisor merge step).** `hle.s` is frozen, so the two
pool entries are exported by `hle_mem.s` and called from `hle.s` by:
`.import hle_pool_alloc, hle_pool_delete`; in `hle_allocpooled`
`jsr hle_alloc` → `jsr hle_pool_alloc`; in `hle_deletepool` a
`jsr hle_pool_delete` before its `jmp hle_ret`. Until then pooled blocks
are owned by nobody and DeletePool frees nothing; `tests/run_vbcc.py`
detects the missing calls in the PRG and SKIPs `u_heap4` / `heap_pool`
with a message (`build/evidence/w5-heap/build_wired.sh` builds the wired
variant from a scratch copy of `hle.s`).

### Invalid frees (C1: survivable, detected, counted)

Hidden headers are kept (8 bytes per allocation) instead of trusting the
size FreeMem passes: they make every free O(1) (boundary tags), carry the
pool owner DeletePool needs, and let FreeMem check the block exactly.
A free is **rejected** — nothing in the heap changes except
`HEAP_CTL+4` += 1 — unless all of these hold: A1 8-aligned and inside
`[HEAP_FIRST + 8, top)`; the header at A1 − 8 has USED set, a size ≥ 16
that ends at or below top, the tag byte `$5A`; and round8(D0) + 8 (+ 8
for SLACK) equals the block size. That catches NULL-adjacent and
out-of-heap pointers, the control block, blocks above top, misaligned and
interior pointers, a size of 0 / too small / too large, and **double
frees** — also after the block was merged: a block merged into its lower
neighbour or into the wilderness gets its tag word zeroed, a block that
became a free block has USED clear, and a stale free header left inside a
larger free block keeps USED clear (`u_heap1`, `u_heap2`). **NULL** is a
silent no-op and is not counted. Not detectable: a forged header (guest
data at A1 − 8 that happens to look like a live block of that size) or a
pointer into memory that has since been reallocated as a larger block —
the same class of bug corrupts a real exec MemHeader. Corrupt heap state
(top out of range or misaligned, a free-list head outside the heap) makes
AllocMem return 0 and FreeMem do nothing; a corrupt link ends the
first-fit walk (the wilderness is used instead), and the walk gives up
after `$80000` nodes, so a guest-corrupted list cannot hang the C64.

### Cost (measured in VICE, `python3 -m tests.heap_cost`)

Per call of `hle_alloc` / `hle_free` / `hle_pool_delete` (the HLE
dispatch around them is extra), C64 cycles and 6502 instructions (CIA2
timer and a VICE exec checkpoint, stub subtracted), and memory-API calls
(each one 4-byte REU DMA; on hardware ~1 µs per REC access):

| Case | cycles | 6502 instr | DMA (R + W) |
|---|---:|---:|---|
| AllocMem, first of a run (formats `HEAP_CTL`) | 1676 | 515 | 1 + 6 |
| AllocMem from the wilderness | 1459 | 415 | 2 + 3 |
| AllocMem reusing a free block whole | 2477 | 694 | 5 + 4 |
| AllocMem reusing a free block, split | 2764 | 855 | 4 + 7 |
| + per free block skipped by first fit | 586 | 165 | 2 + 0 |
| FreeMem, neighbours used (push on the list) | 2880 | 833 | 5 + 6 |
| FreeMem, upper neighbour free (merge) | 3447 | 984 | 7 + 6 |
| FreeMem, lower neighbour free (merge) | 2839 | 820 | 6 + 4 |
| FreeMem, lower free + wilderness | 2783 | 793 | 7 + 3 |
| FreeMem of the top block | 1742 | 490 | 4 + 2 |
| FreeMem rejected (double free) | 898 | 267 | 3 + 1 |
| DeletePool of an empty pool | 3017 | 916 | 8 + 2 |
| DeletePool, per block walked (avg., half released) | 1545 | 448 | 2.6 + 3.7 |
| MEMF_CLEAR, per byte | 42.3 | 12.3 | 0.25 W |

Every call first reads top and the list head (2 DMA). For comparison the
bump allocator it replaces needed 2 DMA per AllocMem and 1-2 per FreeMem,
but leaked every out-of-order free. At 1 MHz an AllocMem/FreeMem pair is
~3-6 ms.

- **MEMF_CLEAR cost**: one `mem_write32` per longword (the loop only steps
  `m_addr`; `mem_write32` keeps `m_addr`/`m_val`) ≈ 42 C64 cycles per byte
  (≈ 43 ms per KB at 1 MHz, ~1 ms/KB at 48 MHz). vbcc only clears the
  ~20-byte argv block (its pools are created with flags 0 and calloc
  zeroes in 68k code), so this is noise today. A REC fill (fixed C64
  source address) would be ~1 cycle/byte but must `jsr mem_rec_dirty`
  (C1) *and* invalidate the fetch window, whose flag `w_inv` is
  core-private — not done; needs a core export (e.g. a `mem_fill` API) if
  large cleared allocations show up.

## Tests

- `host/tests/test_hle.py` (v0), `test_hle_m2next.py`, `test_hle_libs.py`:
  env layout, the library region (full tables to the deepest LVO,
  sentinels, no overlap, padding staged, ExecBase/DOSBase fields),
  index allocation, Process/CLI/FileHandle and stack fields, launch stub
  encoding, guest.inc mirror, placement, `check_program_fits`.
- `host/tests/test_hunk_reloc32short.py`: `HUNK_RELOC32SHORT` (`$3FC`, and
  `$3F7` in load files — what vlink `-Rshort` emits) parses exactly like
  RELOC32 (fixturegen fixtures rewritten into the short form, padding,
  multiple targets, truncation/range errors, a real stock-`+aos68k` vbcc
  link); DREL16/DREL8/RELRELOC32/ABSRELOC16 still fail loudly.
- `python3 -m tests.run_vbcc` — VICE, two legs in one session:
  - **HLE units** (`fixtures/c/hle_unit/*.s`, assembled with vasm at test
    time, heap poisoned with `$A5`): register-level checks of every call —
    allocator rounding, MEMF_CLEAR vs poison, top-block reclaim,
    pools, AllocVec/FreeVec (`u_vec`), the W5-H allocator (`u_heap1`
    invalid and double frees + the rejected-free count, `u_heap2`
    coalescing both ways and into the wilderness, double frees after a
    merge, the SLACK case, `u_heap3` MEMF_CLEAR on reused dirty blocks
    whole and split + alignment of odd sizes, `u_heap4` DeletePool frees
    exactly its pool, `u_heap5` the largest block and the heap's last
    8 bytes),
    StackSwap there and back (`u_swap`), console/NIL:/NULL handles incl.
    Read(NIL:) (`u_dos3`), Write without Output, negative and ≥ 16 MB
    lengths, OpenLibrary exact name + version (`u_openlib`), FindTask,
    SetSignal (D0 must be overwritten), GetVar/DateStamp/SystemTagList
    (`u_dos4`), GetVar leaving a poisoned 32-byte buffer untouched + IoErr
    205, and Close on Output()/Input()/NIL: → DOSTRUE with IoErr and the
    console intact (`u_getvar`), DateStamp's tick → minute → day carries
    (`u_clock`), recovery from an out-of-range clock (`u_clock2`),
    GetVar as the first dos call after boot — `hle_dos_magic` zapped, no
    host init — so its IoErr must survive the lazy init
    (`u_getvar_lazy`), ChangeMode (`u_cmode`, `u_cmode2`, see
    `docs/HLE_DOS.md`), the unimplemented-LVO sentinel,
    and the C2 hand-off (`u_file`/`u_file2`/`u_file3`: kinds `$10`,
    `$FFFFFFFF`, `$00000100`). Against the WS-B2 stub the hand-off is
    pinned exactly (halt in the Write/Read slot, D1-D3 and the stack
    untouched); with WS-B2's real `hle_dos.s` linked the same units must
    end `HALT_OK` with D0 = -1 (the stack-built handle is not an open
    slot), `A7 = stack_top + 4` and a blank screen.
  - **vbcc C programs** (`fixtures/c/*.c`, compiled with vbcc's stock
    `vc +aos68k -O2` at test time into `build/vbcc/`; never committed):
    `hello`, `exit` (exit(7) → D0 = 7), `scroll` (30 lines of varying
    length through 25 rows — also proves the scrolled-in row is blank),
    `alloc` (malloc/free 16 B … 70 000 B, calloc), `heap_churn` /
    `heap_coalesce` / `heap_pool` / `heap_realloc` (W5-H reuse: see Heap;
    `heap_pool` needs the pool wiring), `sysbase` (ExecBase /
    DOSBase fields, OpenLibrary versions and names), `readitem` / `putstr`
    (deep dos LVOs -810 / -948 must halt in the DOS table's own slot),
    `bigstack` (`__stack` → AllocVec + StackSwap), `getenv` (GetVar →
    NULL), `time` (DateStamp → 252460800), `system` (NULL → 1, "cmd" →
    -1), and `leak_open` +
    `leak_check` (handle state must not leak between programs; needs
    WS-B2's real `hle_dos.s`, skipped with a SKIP line while it is the
    stub), `tspin` (the clock moves; `clock()` = -1). Checks: halt
    reason (+ decoded slot), exit code in D0, `A7 = stack_top + 4`, and
    the full 25-row screen.
  - **stdio file I/O end to end** (m2-post; RAMDISK backend). A program
    with `files=` gets a freshly formatted RAMDISK holding those files
    (`hle_dos_env.ramdisk_chunks`) staged with it; `expect_files=` reads the
    RAMDISK back after the run (`hle_dos_env.parse_directory` through
    `tests.run_dos.Guest`, which splits REU reads at 512 KB lines) and
    compares contents, `None` meaning "must not exist". `fio_rw` (fopen
    "w"/fwrite/fclose, fopen "r"/fgetc/fread/EOF, missing file → NULL,
    `RAM:` prefix), `fio_seek` (fseek/ftell/rewind on a staged file),
    `fio_exit` (a stream left open is flushed by exit), `fio_fmt`
    (fprintf/fscanf), `fio_big` (3000 bytes through a `setvbuf` buffer
    that is not page aligned; a staged pad file puts big.bin's extent at
    `$E7FE00`, so the chunk at file offset `$200` crosses the 512 KB REU
    line `$E80000` on the RAMDISK side and needs `dosio_dma`'s split — the
    nosplit mutant turns it red. The buffer also crosses guest `$180000`,
    but that part is regression coverage only: guest chunks are
    page-bounded, so no guest-side DMA can straddle it. The program prints
    `straddle=1 odd=1`), `fio_append` (fopen "a" on an existing and a missing
    file, "r+" in place, ChangeMode refusals), `fio_remove` (remove():
    202 while open, 205 when missing, `RAM:` prefix), `fio_reuse` (fill
    the RAMDISK, remove one, create again).
  - When the PRG exports `hle_dos_init` (WS-B2), it is called before every
    program (`--no-dos-init` turns that off: then `leak_check` goes red —
    evidence under `build/evidence/ws-b1/fix1/`). A runner-level HLE init
    hook is a supervisor follow-up.

## Still missing (next)

- **Keyboard input**: Read on the console returns EOF.
- **Clock / environment / shell**: DateStamp is a call counter (one tick
  per call, see Clock), GetVar finds nothing, SystemTagList runs nothing.
- **More libraries**: OpenLibrary knows only dos.library (math libraries
  for printf `%f` would be next for vbcc; not yet exercised).
- **Workbench startup path**, Forbid/Permit, message ports, signals
  (SetSignal stores nothing).
- **AvailMem** is not implemented (not called by vbcc); the pool wiring
  in `hle.s` is a merge step (see Heap).
- **Fast MEMF_CLEAR** via a core fill primitive (see Heap).
- **CloseLibrary bookkeeping** once OpenLibrary tracks anything.
- **Load-region bound in `host/image.py`**: drivers must call
  `hle_env.check_program_fits`; `run_vbcc` does, `run_program` does not.
- Base-relative hunk relocations (DREL16/DREL8, RELRELOC32, ABSRELOC16)
  are unsupported and rejected by `host/hunk.py`.
