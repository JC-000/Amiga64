# Wave 4 Brief — REC-access reduction (WS-A) + M2 completion (WS-B)

Supervisor-authored, **binding**. Read `docs/CONTRACTS.md`, `docs/RESOURCES.md`
and your workstream's docs first (WS-A: `docs/HARDWARE.md`, `docs/PERF.md`;
WS-B: `docs/HLE.md`). Contracts C1–C5 below are fixed; changing one needs
supervisor sign-off — report the need, do not work around it.

## Packages and file ownership

Each package runs in its own git worktree. **Edit only files you own** (plus
new files you create under your own prefix). Never run git. Never touch
another package's files even if a fix there looks trivial — report it.

| Pkg | Agent | Owns |
|---|---|---|
| **WS-A** | `rec-lean` | `runtime/core.s` (memory API, fetch window, `mem_rec_dirty` body), `runtime/c64.cfg`, `runtime/rc/zp.inc` (core regions only), `tests/runner/*.s` (only to add C1 entry invalidation), `tests/bench/`, `tests/run_vectors.py` + `tests/harness_util.py` (only if the bench/red legs need it), new `host/tests/test_rec*.py`, `docs/PERF.md`, `docs/HARDWARE.md` |
| **WS-B1** | `m2-exec` | `runtime/ops/hle.s`, `host/hle_env.py`, `docs/HLE.md`, `tests/run_hello.py`, new `tests/run_vbcc.py`, new `tools/vbcc/`, new `fixtures/c/`, new `host/tests/test_hle*.py` (not `test_hle_dos*`), `runtime/rc/guest.inc` **equates inside the `hle` env region `$000400-$000BFF` and the heap region only** |
| **WS-B2** | `m2-dos` | `runtime/ops/hle_dos.s` (currently a supervisor stub), new `runtime/ops/dosio*.s`, `host/hle_dos_env.py`, new `host/tests/test_hle_dos*.py`, new `tests/run_dos.py`, new `docs/HLE_DOS.md`, `runtime/rc/guest.inc` **equates in `$000C00-$000FFF` and a new ramdisk region inside `$800000-$FFFFFF` only** |

Shared-but-frozen (supervisor only): `docs/CONTRACTS.md`, this brief,
`host/gen_dispatch.py`, `host/rescheck.py`, `Makefile`, `runtime/macros.inc`,
`runtime/rc/ram.inc`.

## C1 — REC ownership (the WS-A ↔ WS-B seam)

- The memory API (`mem_read*`/`mem_write*`) **may cache REC register state**
  between calls (a "REC shadow") to skip redundant register stores. That is
  the point of WS-A.
- **Anyone else** who programs REC registers `$DF02-$DF0A` or issues a REC
  command (`$DF01`) — the fetch-window refill, runner/loader staging, HLE DOS
  bulk I/O, any future DMA path — must `jsr mem_rec_dirty` (core export,
  clobbers **A only**) before the next memory-API call. `emu_reset` implies it.
- Every C64-side entry the **host** can reach between runs (`test_run_case`,
  `test_run_program`, `test_load_chunk`, the hardware mailbox dispatch path)
  must invalidate before its first memory-API call — host tooling, REST DMA
  and Ultimate firmware may leave the REC in any state.
- Guest-memory writes that bypass `mem_write*` must ALSO set the fetch-window
  invalid flag (existing CONTRACTS.md rule, unchanged).
- `mem_rec_dirty` is pre-landed as `rts` in `runtime/core.s`; WS-A owns its body.
- **Teeth required (WS-A):** a build flag `REC_NOINVAL` that empties the
  invalidation must make a dedicated test FAIL (same pattern as `WINDOW_NOINVAL`).

## C2 — FileHandle model (WS-B1 ↔ WS-B2)

- Every dos.library file handle (`Input()`, `Output()`, `Open()`) is a **BPTR**
  (guest address >> 2) to a longword-aligned 44-byte guest `struct FileHandle`
  with AmigaOS `dos/dosextens.h` offsets. HLE reads exactly one field:
  **`fh_Args` (offset 36, u32 BE) = handle kind**: `0` console out, `1` console
  in, `2` NIL:, `$10+n` WS-B2 file slot `n`. Unused fields are zero.
- `Read`/`Write` stay single HLE indices in `hle.s` (WS-B1). They fetch
  `fh_Args` from the handle; kind `>= $10` → `jmp hle_dos_read` /
  `jmp hle_dos_write` (WS-B2, entered with the untouched call state: opcode
  in `op_lo`/`op_hi`, D1–D3 as the caller set them, return address on the
  guest stack; WS-B2 retires the call). Kinds < `$10` are WS-B1's.
- `FH_MAGIC $C0DE` is retired by WS-B1 in favour of real console handles.

## C3 — HLE index allocation

| Range | Owner | Table |
|---|---|---|
| `$00-$2F` | WS-B1 (exec, dos console/process; `$00-$03` exist) | `hle.s` + `host/hle_env.py` |
| `$30-$4F` | WS-B2 (dos file I/O: Open/Close/Seek/…) | `hle_dos.s` (`hle_dos_dispatch`, index in `op_lo`) + `host/hle_dos_env.py` |
| `$50-$FF` | free | — |

`hle.s` already routes `$30-$4F` to `hle_dos_dispatch` and `hle_env.py`
already merges `DOS_FILE_FUNCS` (pre-landed). LVO slots for both halves live
in the one fake DOSBase; an LVO is claimed by exactly one table.

## C4 — Resource allocations (pre-declared in `runtime/rc/`)

- Guest: program load region is now `$010000-$0FFFFF`; **HLE heap
  `HEAP_BASE $100000`–`HEAP_END $7FFFFF`** (WS-B1, AllocMem/FreeMem);
  `$800000-$FFFFFF` free (WS-B2 may claim a ramdisk sub-region there).
- **C4 amendment (2026-09-24, b1-review F1):** the env region cannot hold
  real library tables (exec negatives ~830 B + ExecBase 632 B + dos
  negatives 1158 B). New region **`hle $0F0000-$0FFFFF` — library bases and
  their full vector tables** (WS-B1); program load region shrinks to
  `$010000-$0EFFFF`. Every LVO slot down to the deepest known LVO of each
  library is staged (implemented or sentinel); no two tables overlap.
- Env region split: WS-B1 `$000400-$000BFF`, WS-B2 `$000C00-$000FFF`
  (file-handle pool etc.) — equates only, the region line stays as is.
- ZP: only WS-A may claim new ZP (from `$83` up, via `rc/zp.inc`). WS-B uses
  `.res` in BSS (as `hle.s` does today). C64 RAM: BSS via the linker only.
- New symbols take a registered prefix (`docs/RESOURCES.md`); WS-B2's new
  `dosio_` prefix is pre-approved (HLE family). `make rescheck` must be green.

## C5 — Test discipline (red → green, adversarial)

1. **Red first.** Before implementing, write the tests that pin the new
   behaviour and RUN them against the unmodified base. Record the red output
   (command + tail) under `build/evidence/<pkg>/red/`. A test that cannot go
   red proves nothing — say so and add a teeth leg instead.
2. **Green.** Implement until green, record under `build/evidence/<pkg>/green/`.
3. **Full gate** (`mem:task_completion`): `make host-test`, `make runtime
   BUILD=build/<pkg>`, FULL `python3 -m tests.run_vectors` (5150/5150) + hand
   vectors (48/48), `python3 -m tests.run_program`, `python3 -m tests.run_hello`,
   `make rescheck`. Always the full suite, never only filtered stems.
4. An independent **adversarial reviewer** (fresh agent) will attack your
   package after you report; the supervisor re-runs every gate before merge.
   Report honestly: skipped steps, flaky results, and anything you could not
   verify go in the report, not under the rug.

## Hardware (U64E at 192.0.2.81, `U64_HOST=192.0.2.81`)

**WS-A is the only package allowed to touch the device this wave.** Always
through the c64-test-harness (`DeviceLock` is automatic). Record the device
config before, restore it exactly after (see `docs/HARDWARE.md` — CPU Speed
`" 1"`, Turbo Control `"Manual"`, REU Enabled 512 KB, Cartridge preset
`"REU"`, reset to BASIC). No firmware or persistent-storage changes, ever.
WS-B2's Ultimate DOS path is hardware-validated in a later supervisor-
scheduled leg; in this wave it must be VICE-testable via its RAMDISK backend.

## Local model (Qwen3.6-35B-A3B on the Air lab box)

Available for **drafting and second opinions**, never as an authority:

```sh
LA_MAX_TOKENS=8192 LA_TIMEOUT=1200 \
  ~/Documents/ebullientprism/tools/local-agent/invoke.sh --system "..." "prompt"
```

- 3 server slots shared by every agent on this machine; the server queues a
  4th request natively. **At most ONE request in flight per agent**; expect
  waits of minutes; ~40 t/s single, ~16 t/s under load. It is a reasoning
  model (`<think>` eats tokens) — budget `LA_MAX_TOKENS` accordingly.
- Good uses: drafting test tables / host-side pytest scaffolds, extracting
  LVO/trap lists from disassembly, a first-pass diff scan whose every claim
  you then verify, doc prose drafts. `reference/apply_edits.py` applies
  SEARCH/REPLACE blocks if you ask for that format.
- Rules: send only repo code/docs (never keys, never device credentials);
  treat output as untrusted; nothing it writes lands without you reading it
  and the gates passing. Say in your report where you used it and what it
  caught or got wrong (we are building evidence on where it helps).
- If the endpoint is down or slow, proceed without it — never block on it.

## Report (end of your run)

What you built; red→green evidence paths with the key numbers; full-gate
results with exact counts; contract deviations (should be none); open risks;
local-model usage notes; the exact list of files changed.
