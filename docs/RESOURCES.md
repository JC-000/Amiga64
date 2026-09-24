# Resource Contract (ZP / RAM / guest allocations)

Every fixed address this project claims — zero page, C64 system RAM/I-O,
guest (REU) space — is declared in exactly ONE place and machine-checked.
The scheme adapts the c64-lib-contract idiom (`~/Documents/c64-lib-contract`,
SPEC §2/§3) to a single-repo multi-module codebase.

## The three declaration files (single source of truth)

| File | Space | Bounds |
|---|---|---|
| `runtime/rc/zp.inc` | `zp` | `$00-$FF` |
| `runtime/rc/ram.inc` | `ram` | `$0100-$FFFF` (C64 side) |
| `runtime/rc/guest.inc` | `guest` | `$000000-$FFFFFF` (guest addr = REU offset) |

Each allocation is a ca65 integer equate, one per line, `.ifndef`-guarded —
**the assembler is the schema**. `runtime/macros.inc` includes all three, so
`runtime/*.s` and `runtime/ops/*.s` get every name for free;
`tests/runner/*.s` include `rc/zp.inc` + `rc/ram.inc` directly (ca65 resolves
`.include` against the including file's directory). The host-side mirrors of
the guest constants (`host/hle_env.py`, `host/image.py`) are cross-checked
against `rc/guest.inc` by the checker. `.assert`-ed invariants at the bottom
of each rc file pin layout assumptions (contiguous register file, buffer
alignment, env-region fit) — use `lderror` instead of `error` the moment an
operand is an `.import`ed symbol (imports have no value until link; `.if` on
them does not even assemble).

## Region comment grammar (machine-parseable)

```
; @space <name> $<start>-$<end>              -- one per file
; @region <owner> $<start>-$<end> <role...>  -- claims [start,end] inclusive
```

A source-file equate that is a plain value (size, offset, flag bits) and
never an address may carry `; @value` (`NAME = $1000  ; @value`). The
checker then skips its address classification — but using a tagged name as
a memory operand anywhere is a violation, so the tag cannot hide a claim.

Equate lines below a `@region` belong to it and must lie inside its range.
Regions never overlap, and together they must COVER the space — unclaimed
space is declared explicitly with owner `free`, never left implicit.
Non-module owner tags: `sys` (hardware/OS, not claimable), `free`
(unallocated), `mb` (hardware-harness mailbox page `$CE00-$CEFF`, reserved
for in-flight Ultimate-64 harness work).

## Prefix registry (name ownership)

Duplicate-identifier link errors catch name collisions that numeric checks
miss — so one prefix family per module, registered here. Existing names are
registered as-is (no renames: `build/runner.lbl` consumers exist).

| Prefix family | Owner | Examples |
|---|---|---|
| `r_` `m_` `t_` `op_` `st_` `ea_` `w_` `alu_` `mem_` `emu_` `fetch_` `dispatch_` `HALT_` `REU_CMD_` | core | `r_d0`, `t_tmp`, `st_halt`, `w_pg0` |
| `fam_` + `g1_`..`g6_` file-locals | ops groups G1-G6 | `fam_move`, `g3_t` |
| `hle_` `SCREEN*` `ENV_` `EXEC_`/`DOS_`/`EXIT_`/`ABSEXECBASE_` `FH_` `HEAP_` | HLE (M2) | `hle_aline`, `SCREEN`, `DOS_BASE` |
| `run_` `mb_` `test_` `RES_` `CASE_` `RUN_` `LOAD_` `READY_` + the legacy scratch names `cp cnt nexp expp rbidx srcp lcnt gaddr icount nobud` | runner/harness | `test_run_case`, `RES_DONE` |
| `REC_` `ULT_` | sys (hardware regs, declared in `rc/ram.inc`) | `REC_COMMAND` |
| `rc_` | the resource contract itself | reserved for future contract symbols |

New symbols MUST take a registered prefix of their owning module (new
modules register a fresh family here first).

## The checker: `host/rescheck.py`

c64-lib-contract's recorded pain point: conformance was human review only,
and ld65 fails silently in several modes (an unresolvable `lderror` assert
degrades to a warning; `-D $40` through a shell becomes `0` with no
diagnostic). We close that gap mechanically:

```
make rescheck            # or: python3 -m host.rescheck   (exit 1 = violation)
pytest -q                # host/tests/test_rescheck.py incl. proven-to-fire negatives
```

Checks: (a) no overlapping regions per space; (b) regions in-bounds and full
coverage; (c) every rc equate inside its region block; (d) a conservative
scan of `runtime/` + `tests/runner/` sources for raw address literals
(direct ZP operands; absolute `$0400-$DFFF`; 5-6-digit guest literals) that
land in another owner's region, `free`, `sys`, or `mb` space — anything
ambiguous is reported UNCLASSIFIED, never guessed; (e) host mirror
constants match `rc/guest.inc`. Legitimate exceptions go in the
`ALLOWLIST` table in `host/rescheck.py` with a documented reason (currently
empty — the migration left no raw foreign literals).

## Claiming new space

1. Edit the owning rc file: adjust/split a `free` region into a `@region`
   with your owner tag, add `.ifndef`-guarded equates under it (registered
   prefix).
2. Run `make rescheck` (green) and `pytest -q`. That's the whole protocol —
   a claim that isn't in an rc file doesn't exist, and the scan will flag
   raw literals that try to bypass it.

## `-D` override idiom (future variants)

Every equate is `.ifndef`-guarded, so a variant build can relocate it:
`cl65 ... -D buf_case=0xB000`. **The `$`-quoting gotcha (measured by
c64-lib-contract, SPEC §2):** an unquoted `-D name=$40` lets the shell eat
`$4`, yielding `name=0` with no diagnostic from make, shell, ca65 or ld65;
through make the escape ladder is worse (`$$40` → 0, `$$$$40` → the shell's
PID). Therefore values passed through make/shell MUST be `$`-free — use
`0x` hex or decimal. Note `rescheck` validates the *default* values in the
rc files; a relocated variant relies on the rc `.assert` invariants and its
own link map.

## Differences from c64-lib-contract

Same spirit — declaration files as schema, prefix registry, `.ifndef`
overridability, assert-guarded invariants — minus the multi-repo machinery:

- **Single repo:** no versioning equates (`LIB_*_VERSION_*`/`ABI_VERSION`),
  no manifest/segment-prefix clauses, no bare-name deprecation gates. All
  modules build from one tree at one revision, so "which version am I
  linking" cannot arise.
- **Machine-checked:** their conformance was human review; here
  `rescheck` runs in pytest/make (same culture as `gen_dispatch.py`), with
  negative tests proving each guard fires.
- **When the ABI machinery WOULD be needed:** the moment `runtime/` is
  consumed outside this repo (e.g. the interpreter shipped as a library
  for another C64 project), adopt SPEC §1 versioning + §4 segment
  prefixes; the rc files already match its §2/§3 shape, so that step is
  additive.
