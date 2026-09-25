# Wave 5 Brief — Wave-4 follow-ups (heap, console input, tooling)

Supervisor-authored, **binding**. Read `docs/WAVE4_BRIEF.md` first: its
contracts C1–C5 (REC ownership, FileHandle model, HLE index ranges, resource
allocations, red→green + adversarial review) all still apply. Wave 5 adds the
contracts below. Changing one needs supervisor sign-off — report, don't work
around.

## Packages and file ownership

Each package works ONLY in its supervisor-created worktree (path given in
its brief). Edit only files you own plus new files under your own prefix.
Never run mutating git. Other packages' files: read-only.

| Pkg | Agent | Owns |
|---|---|---|
| **W5-H** | `w5-heap` | `runtime/ops/hle_mem.s`, `host/hle_env.py` (heap staging only), new `host/tests/test_hle_mem*.py`, new `fixtures/c/heap_*.c` + `fixtures/c/hle_unit/u_heap*.s`, `tests/run_vbcc.py` (registering its own fixtures only), `docs/HLE.md` (heap/pool/AllocVec sections only), `runtime/rc/guest.inc` (equates inside the heap region only) |
| **W5-K** | `w5-con` | `runtime/ops/hle_con.s`, new `tests/run_con.py`, new `host/tests/test_hle_con*.py`, new `fixtures/c/con_*.c` + `fixtures/c/hle_unit/u_con*.s`, new `docs/HLE_CON.md` |
| **W5-T** | `w5-tools` | `tests/run_dos.py`, `tests/bench/u64bench.py`, `tests/bench/u64_session.py`, `host/tests/test_hle_dos_uci.py`, `tests/harness_util.py` (only if the u64 backend needs it), `docs/HARDWARE.md` (new sections), `docs/HLE_DOS.md` (testing/hardware sections only) |

Frozen (supervisor only): `runtime/ops/hle.s`, `runtime/core.s`,
`runtime/ops/hle_dos.s`, `runtime/ops/dosio_*.s`, `host/rescheck.py`,
`host/image.py`, `Makefile`, `runtime/macros.inc`, `runtime/rc/zp.inc`,
`runtime/rc/ram.inc`, both brief files.

## C1 — Allocator ABI (hle.s ↔ hle_mem.s)

Pre-landed: the Wave-4 bump allocator moved verbatim into `hle_mem.s`.
`hle.s` exports the BSS scratch; `hle_mem.s` exports the two routines:

- `hle_alloc`: in `hle_sz` (u32 LE size), `hle_flg` (bit 0 = MEMF_CLEAR);
  out `hle_old` (u32 LE block address, 0 = failed). The block must be
  8-byte aligned, inside `[HEAP_BASE, HEAP_END]`, and never overlap a live
  block. MEMF_CLEAR zero-fills the whole returned block.
- `hle_free`: in `hle_ptr` (block), `hle_sz` (size as passed to FreeMem).
  Must never corrupt a live block. Invalid frees (NULL, outside the heap,
  wrong size, double free) must be survivable; which of them are detected
  and how is W5-H's design, documented and tested.
- Both may clobber A, X, Y, `m_*`, `hle_sz`, `hle_cnt`, `hle_ptr`,
  `hle_old` (free only). Callers in `hle.s` (AllocMem, FreeMem, CreatePool,
  AllocPooled, FreePooled, AllocVec, FreeVec, DeletePool) are FROZEN — if the
  pool semantics need a new entry (e.g. `hle_pool_delete` so DeletePool
  frees a whole pool), W5-H implements it in `hle_mem.s`, exports it, and
  the SUPERVISOR wires the one-line call in `hle.s` at merge (describe the
  exact wiring in your report; test it by building a scratch copy of
  `hle.s` with the wiring under `build/`).
- Heap metadata lives in guest memory inside the heap region and/or
  `ENV_STATE+0` (u32). `host/hle_env.py` stages it fresh every run.

## C2 — Console input seam (hle.s ↔ hle_con.s)

Pre-landed: `hle.s` `Read` on console kinds 0/1 does `jmp hle_con_read`
(stub: D0 = 0, EOF — the Wave-4 behaviour). Entry state: D1 = fh BPTR,
D2 = buffer, D3 = length, return address on the guest stack; retire with
`jmp hle_ret` after setting D0 (bytes read / 0 = EOF / -1 = error).
`hle_con.s` may call the memory API (C1 of Wave 4 applies to any direct REC
use). No KERNAL calls (ROMs may be banked out; the runner runs with SEI).
C64 RAM via BSS only; no new ZP.

## C3 — Hardware

**Only W5-T may touch the U64E**, via the harness (DeviceLock), with exact
config snapshot/restore and explicit CPU speed. **No storage writes** —
ULTDOS stays opt-in and is not exercised this wave. Write a session log to
`build/evidence/w5-tools/hw/` as you go, so the supervisor can check
progress directly.

## Local model

Same rules as Wave 4 (`docs/WAVE4_BRIEF.md` "Local model"). It produced
nothing usable in Wave 4; use it only for small, bounded drafting, and never
block on it.

## Report

What you built; red→green evidence paths (`build/evidence/<pkg>/`); mutation
results; full-gate exact counts (`sh tests/rec_gate.sh build/<pkg>
build/<pkg>-gate` + `run_dos` + `run_vbcc` + your suites); contract
deviations (should be none); open risks; exact changed-file list relative to
the base commit.
