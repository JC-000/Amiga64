# C2V: planar → VIC-II converter v0

Wave 7, package W7-V. This is the converter half of `docs/S3_PLAN.md` §4
"S3.5". There is no copper, no display-register decoding and no
guest-visible behaviour change. Nothing calls the module yet. It is linked
into the runner PRG and driven directly by `tests/run_c2v.py`.

| File | Role |
|---|---|
| `runtime/ops/c2v.s` | the module: `c2v_init`, `c2v_pal_mc2`, `c2v_pal_hires`, `c2v_mc2`, `c2v_hires` |
| `host/c2v_ref.py` | 4096-entry OCS-RGB → VIC LUT, byte-exact reference conversions (any row count), argument packing |
| `host/tests/test_c2v_ref.py` | pytest for the reference (cross-checked against W6-D's probe models at 200, 176 and 160 lines) |
| `tests/run_c2v.py` | VICE direct-API test: green, NMI-during-fill, red (stub), teeth (mutants), layout-B variant, `--perf` |
| `host/rescheck.py` `@under` | resource-contract grammar for RAM under the I/O window (below) |

## Kernels

Both are the kernels measured in `tools/s3probe/c2v.s`. Their inner loops
are unchanged. The bitmap is in char-cell order: the byte for cell `cx`,
line `y` sits at `c2v_bitmap + (y>>3)*320 + cx*8 + (y&7)`.

- **`c2v_mc2`, 2-plane lores 320 px → multicolour 160 px, exact colours.**
  Each MC pixel is (plane 1, plane 0) of the left lores pixel of its pair,
  i.e. the Amiga colour index 0–3. `%00` → `$D021` (`c2v_a_bgc`), `%01` →
  screen high nibble, `%10` → screen low nibble, `%11` → colour RAM.
- **`c2v_hires`, 4-plane hires 640 px → hires 320 px silhouette.** A pixel
  is on iff the OR of the planes is nonzero, taken 2:1 by OR-ing pixel
  pairs (`c2v_pairhi`/`c2v_pairlo`, built by `c2v_init`). On = colour 1
  (screen high nibble), off = colour 0. The v1 per-palette boolean is still
  open (S3_PLAN §2.1). **W12-V:** the kernel also picks each cell's pen
  (`c2v_pick`: the leftmost set pixel of the cell's first line with one)
  and writes the pens of each finished char row to colour RAM
  (`c2v_hrow`), counting cells per pen in `c2v_v_cnt`. `c2v_hfill` then
  writes matrix cell = `c2v_v_col`[pen] << 4 | `c2v_v_col`[0]; disp.s sets
  `c2v_v_col` by the colour choice (CONTRACTS "Display refresh (S3.5)").

Each line fetches one row per plane (REC autoload, so only the C64 page and
the 24-bit REU address are stored per row) into a page-aligned row that the
kernel reads.

## Palette

`host/c2v_ref.py build_lut()` maps each OCS colour `$0RGB` (nibble × 17
per channel) to the nearest VIC colour. The metric is CIE L\*a\*b\* with
CIE76 distance, against Pepto's PAL VIC palette; ties go to the lower VIC
index. The 4096-byte table lives in the REU at a caller-supplied address
(a guest claim follows at S3.5 integration). `c2v_pal_*` look up colours
0–3 (mc2) or 0–1 (hires) with one byte DMA each. They then fill the screen
matrix and colour RAM and set `c2v_a_bgc`. In hires, colour RAM is unused
by the VIC and is filled with colour 0 (W12-V: `c2v_hires` then
overwrites the converted rows with the cell pens). `c2v_setmc2` (W12-V)
is `c2v_pal_mc2` with `c2v_v_col[0..3]` already set. Before W12-V,
palette changes were the only time the matrix was written; disp.s now
rewrites it per hires conversion (`c2v_hfill`).

## C64 memory: VIC bank 3, 200 lines (S3_PLAN §2.4)

All claims are in `runtime/rc/ram.inc` / `zp.inc` and labelled `; W7-V c2v`.
**No existing claim line changed**: only `free` lines were split, and the
screen matrix uses the new `@under` region kind.

| Range | Use |
|---|---|
| `$B900-$B9FF` / `$BA00-$BAFF` | `c2v_pairhi` / `c2v_pairlo` (page-aligned, under BASIC ROM) |
| `$BB00-$BEFF` | `c2v_stage`: one row per plane, page-aligned |
| `$BF00-$BFFF` | `c2v_args`: argument block + state |
| `$E000-$FF3F` | `c2v_bitmap`: 25 char rows (bank + `$2000`), under KERNAL |
| `$D000-$D3FF` (`@under`) | `c2v_screen`: matrix + sprite pointers in the RAM under I/O (bank + `$1000`) |
| `$D800-$DBE7` | colour RAM (`VIC_COLRAM`, sys) |
| `$FFFA-$FFFB` | `mach_vnmi` (rc `s3 $FFFA-$FFFF`, owned by S3.2): c2v_fill is a sanctioned writer and stores `mach_nmi` (identical bytes; `docs/CONTRACTS.md` "Machine loop (S3.2)") |
| `$01` | `CPU_PORT` (zp sys equate) |

`$A000-$B8FF` stays free, including S3_PLAN's planned plane-row staging at
`$A000-$A9FF`. So does `$FF40-$FFF9`; `$FFFA-$FFFF` is S3.2's vector claim. The work RAM moved
from the approved `$A000-$ACFF` to `$B900-$BFFF` because
`host/tests/test_rescheck.py::test_free_region_literal_fails` uses `$A000`
as its example of free space.

**$01 on entry.** The work RAM is under BASIC ROM, and the runner runs with
`$01 = $37`. So every entry banks BASIC out (`%x11 → %x10`: `$37 → $36`,
with I/O and KERNAL still in) and restores the caller's `$01` on exit. The
bitmap under KERNAL is only written, never read, so it needs no banking.
The VIC always reads RAM.

### Screen-fill protocol (matrix under I/O)

Only `c2v_pal_*` do this, i.e. only on a palette change:

1. Store `<mach_nmi`/`>mach_nmi` to `$FFFA/$FFFB` (W8-L: the one NMI
   handler, see "Handover to S3.2"). A store under ROM lands in RAM
   whatever `$01` is, so the RAM vector is valid **before** `$01` ever
   exposes it.
2. `php`, `sei`, save `$01`, then `$01 := ($01 & $F8) | $04` (`$34`: RAM
   everywhere, no I/O).
3. CPU stores to the 1000 matrix bytes.
4. Restore `$01` at once, then `plp`. Colour RAM is filled afterwards,
   with I/O mapped.

REU DMA never targets `$D000-$DFFF` (assembly-time asserts on `c2v_stage`
and `c2v_args`). A test checks the whole 4 KB of RAM under I/O for stray
writes.

**NMI.** `sei` does not mask NMI (RESTORE, CIA2), and with `$01=$34` the CPU
vectors through RAM `$FFFA`. Two choices were considered:

- Save the old RAM bytes, point them at an RTI, then restore them. This
  was rejected: reading RAM `$FFFA` needs KERNAL banked out, which exposes
  the unguarded vector during the read itself.
- Claim the two bytes as **W7-V interim** and write the RTI vector before
  every fill. This was chosen: it is race-free, costs 16 cycles, and no
  other module owns the bytes.

(W7-V history: the vector pointed at a c2v-own stub, `c2v_nmi_rti`; since
W8-L it points at `mach_nmi`, the one NMI handler.) The handler drops an
NMI taken during the fill but **acknowledges its source**: it maps I/O in
(`$01 & $F8 | $05`), reads the CIA2 ICR, restores `$01` and returns. So the
NMI is lost, not delayed. Reading the ICR releases CIA2's /NMI line, and the
next NMI edge (CIA2 or RESTORE) is delivered normally. Without the read,
/NMI would stay low and block every later NMI (W7 review F2, measured: ICR
`$83`, a later timer-B NMI not taken). It costs ~45 cycles per NMI and
counts the NMIs it takes in `mach_nmis`.

### Handover to S3.2

Done in Wave 8 (W8-L). Round 0 kept two handlers (option 1); the W8-L
review found the two byte-wise stores of different handlers into one
vector could tear (an NMI between them, with `$01=$35`, took half of each:
F1). Ruling: **one NMI handler**.

- **The claim.** The W7-V interim `c2v $FFFA-$FFFB` line is gone; rc
  `s3 $FFFA-$FFFF` (`mach_vnmi`, `mach_vrst`, `mach_virq`) owns the RAM
  vectors, and names `c2v_fill` as a sanctioned writer of `mach_vnmi`
  (identical bytes). `c2v.s`'s asserts (screen matrix and bitmap end below
  the vectors) test the same address.
- **The handler** is `mach_nmi` (`runtime/ops/mach.s`): runs from RAM below
  `$A000` (visible with `$01=$34`); saves A and `$01`; maps I/O in (`$01 &
  $F8 | $05`); reads the CIA2 ICR, so /NMI is never left latched; counts in
  `mach_nmis`; restores `$01` and A; `rti`. `c2v_nmi_rti` and its counter
  `c2v_v_nmis` (args +72) are deleted, and with them the stale-`c2v_v_port`
  hazard (review F2): nothing reads `c2v_v_port` outside the fill any more.
- **`c2v_fill`** stores `mach_nmi` into `$FFFA/$FFFB` before it sets
  `$01=$34`, so it stays correct when called outside `mach_run` (as
  `tests/run_c2v.py` does; the store then lands in RAM with the ROM vectors
  live). Inside `mach_run` (`$01=$35`) the vector already holds `mach_nmi`
  and the store rewrites identical bytes: no tear is possible
  (`docs/CONTRACTS.md` "Machine loop": vector invariant; test
  `tests/run_mach.py` leg `c2vnmi`, 1024 NMIs swept over the store and the
  fill: all counted by `mach_nmi`, 0 torn; red on the two-handler code:
  602 missed, 4 torn).
- **Dropped, acknowledged, not delayed**, as before: an NMI source that
  fires during the fill loses that one event. No S3.2 time-base source
  uses NMI. `tests/run_c2v.py`'s NMI leg checks `mach_nmis` (+1 per in-fill
  NMI) and its `nmiack` tooth is now `MACH_MUT_NMIACK` (in `mach.s`).
  S3_PLAN §1.5's "NMI → `rti`" reads "NMI → acknowledge CIA2, `rti`"
  (supervisor).

### rescheck: `@under` regions

`; @under <owner> $lo-$hi <role>` claims the RAM under an I/O window. Such a
region:

- is allowed only inside `UNDER_WINDOWS`, i.e. space `ram`, `$D000-$DFFF`.
  So the stack, the KERNAL vectors, the guest vector page and the CPU port
  can't be claimed this way (review F1);
- must lie inside **one** `sys` region;
- must not overlap another `@under` region;
- must have a **registered module** owner (`FILE_OWNERS`/`OWNER_GLOBS`). A
  hardware prefix such as `VIC` is rejected, so `VIC_*` equates can't
  escape the prefix rule;
- requires every equate under it to carry the owner's prefix (`c2v_`), so an
  I/O register equate such as `VIC_RASTER` cannot move into it.

`@under` regions take no part in the overlap/coverage checks. The literal
scan still classifies `$D000-$DEFF` as `sys`, so a raw `sta $D000` stays a
violation. Tests: `host/tests/test_rescheck_under.py`. Seven of its first
nine tests fail on the base checker, which silently ignores `@under` lines.
Its seven F1 abuse tests failed before the fix round. The other two of the
first nine pin behaviour that was already right: a plain `@region` over I/O
is rejected, and so is a raw literal. `docs/RESOURCES.md` needs the grammar
line (supervisor).

## Parameters

`C2V_ROWS` (20–25, default 25) and the `c2v_bitmap` / `c2v_screen` bases
are `.ifndef` equates. The kernels, the row tables, the line count and the
blank band all derive from them. So does the under-I/O fill:
`C2V_SCR_UNDER_IO` is `(c2v_screen >> 12) = $D`, and a matrix elsewhere is
filled without banking or NMI guard. With fewer than 25 rows, the cells of
the unconverted rows get colour 0 in both nibbles and in colour RAM. The
VICE leg `--variant layout-b` (`C2V_ROWS=22`, `c2v_screen=$FC00`) must pass
too.

## Argument block (`c2v_args`, little-endian)

| Offset | Field |
|---|---|
| +0 | 4 × u24: REU address of each plane's first converted row |
| +12 | u24: LUT REU address; +15 pad |
| +16 | 16 × u16: OCS colours `$0RGB` (bits 15–12 ignored) |
| +48 | u8 out: VIC colour for `$D021` (colour 0) |
| +49…+71 | module state (opaque) |
| +92…+143 | W12-V state: `c2v_v_col` 16 × u8 (colour per pen), `c2v_v_cnt` 16 × u8 (hires cells per pen), `c2v_v_tab` 16 × u8 (`c2v_hfill`'s matrix byte per pen), `c2v_pick` scratch (CONTRACTS "Display refresh", "Colour choice") |

Preconditions:

- Rows are contiguous (BPLxMOD = 0: 40 B lores, 80 B hires).
- No plane crosses a 512 KB REU line (see below). The LUT is safe
  anywhere, because its lookups are 1-byte DMAs with a CPU-side 24-bit add.
  64 KB bank crossings are fine and tested.
- Call with I/O mapped in and no IRQ that uses the REC.

**Caller contract for S3.5 integration: the 512 KB REU line (review F6).**
Each row fetch is one DMA of 40/80 bytes. The REC does not carry a DMA
across a 512 KB line (the rule behind the memory API's STRADDLE split; VICE
behaves the same), so a plane row that straddles one reads the wrong bytes
for its part past the line. Only that row's pixels go wrong: in
VICE, an mc2 plane at `$17FFE8` gives 14 wrong bytes in row 0 and every
other row is correct. No C64 memory is at risk, because the DMA target is
always a fixed 256-byte stage page and a row is at most 80 B. The plane
pointer also does not wrap at the chip-RAM size the way Agnus does. A
guest's BPLxPT may legally cross a line (e.g. an ECS 1 MB chip-RAM layout).
So at S3.5 integration the caller must either split or clamp such a plane,
or the fetch must be split at the line (`mem_read*`'s STRADDLE rule). This
is an S3.5 entry criterion; v0 checks it on the host only
(`check_placement`).

Every entry ends with `jsr mem_rec_dirty` (C1).

## Performance [MEASURED VICE]

Measured with `python3 -m tests.run_c2v --prg … --perf [--variant nofetch]`:
a CIA2 TA→TB 32-bit φ2 count with the screen blanked. The frames are 200
lines, so they compare directly with S3_PLAN §2.3. The W6-D probe re-run in
the same VICE gives 573,820 / 327,620 exactly.

| Kernel | Frame (200 lines) | vs S3_PLAN | Kernel only (nofetch) | Fetch per line |
|---|---|---|---|---|
| hires | 675,439 | +17.71% | 569,727 (−0.71%) | 529 = 320 DMA + 209 setup |
| mc2 | 362,839 | +10.75% | 323,527 (−1.25%) | 197 = 80 DMA + 117 setup |

Palette refresh (LUT lookups + matrix + colour RAM): 22,972 (mc2) / 22,827
(hires) cycles.

Fix round 1 grew the NMI stub, which sits before the kernels, so the
kernels moved by 19 bytes. Their frame cycles moved by −1.1% / −1.3%
(first build: 682,647 / 367,505, kernel only +0.57% / +0.00%). The fetch
cost per line did not change. The kernels' loop branches and their
page-crossing reads depend on where the module links, so expect ±1.5%
between builds. Pinning that would need a page-aligned kernel, i.e. a
`c64.cfg` segment.

The kernels match S3_PLAN. The whole gap is the REU row fetch, which
S3_PLAN priced separately (§2.3): in VICE the DMA steals 1 cycle per byte,
plus ~52–60 cycles of register setup per plane row. Grouping 3 rows per DMA
(3 × 80 B fit one page without a crossing) and patching the source
low-byte operands would save an estimated ~2–3% [EST, static]. Not done in
v0.

## Link layout

`c2v.s` contributes only CODE, padded to whole pages (4 pages). It has no
RODATA, DATA, BSS or ZP. `core.s` does not move (100 labels unchanged), and
every module linked after `c2v.s` moves by exactly `$400` (105 labels).
`tests/perf_cycles.py --base <wave7/integration PRG> --max-delta 0` shows
+0.000 on every loop.

## Tests

- **Green**, 5 cases × 2 kernels:
  - the cases: random planes with the real LUT; line/column/plane-ID planes
    with a random LUT; sparse single bits with a random LUT; only the
    highest plane set with the real LUT; and bank-cross (review F3). In
    bank-cross, hires plane 0 crosses `$120000` mid-row, mc2 plane 0 crosses
    `$130000` in row 0, mc2 plane 1 ends at `$17FFFF`, and the LUT at
    `$14F800` is indexed on both sides of `$150000`;
  - what is compared: the whole RAM at `$E000-$FFFF` and under I/O
    `$D000-$DFFF` against the guard pattern with exactly bitmap, matrix and
    NMI vector overlaid; colour RAM; `c2v_a_bgc`;
  - guards: `$B800-$B8FF` and the VIC/CIA2 display registers;
  - after each entry: `$01`, `m_rok = 0`, and the **I flag** (review F4).
    Every entry is called with CIA1 IRQ sources masked and `cli`, and must
    return with I clear;
  - NMI leg: CIA2 NMIs at 1500/3000/5000 cycles, inside the fill, must be
    taken by `mach_nmi` (`mach_nmis` + 1) and acknowledged (ICR `$00`),
    with correct output. A **later** CIA2 timer-B NMI must then still reach
    the KERNAL. A control NMI at 100 cycles, before the fill, must go to the
    KERNAL (counter unchanged). The RAM vector is preset to a `jmp *` trap.
- **Red:** `--variant stub`, where every entry is `rts`: every run fails and
  the bitmap guard stays intact.
- **Teeth:** `--teeth`, each of which must fail:
  - `C2V_MUT_BITORDER`: planes swapped / pair tables swapped;
  - `C2V_MUT_LUT`: LUT index + 1;
  - `C2V_MUT_ROW`: source row advance one byte short;
  - `C2V_MUT_NMI`: vector not set, so the call hangs on the first in-fill
    NMI;
  - `C2V_MUT_NMIACK`: the stub doesn't read the ICR, so the later NMI is
    not delivered.

  The reviewer's two missed mutants (`fetch_bank_nocarry`,
  `fill_I_left_set`) now fail (`build/evidence/w7-c2v/fix1/teeth/`).
- **Variant builds** link in the Makefile's order (`makefile_sources()`,
  evaluated by make itself), so teeth, layout-b and nofetch measure the
  layout that ships (review F5).

## Rejected alternatives

- **B (176 lines, all in free RAM):** bitmap `$E000-$FB7F` plus matrix at
  `$FC00`, overlapping bitmap rows 22–24, which were blanked. Fully built
  and gated first (evidence `build/evidence/w7-c2v/layoutB/`). It is still
  reachable as `C2V_ROWS=22 c2v_screen=$FC00`. Rejected by the user for
  losing 24 lines.
- **Bank 2** (matrix at `$8000` inside core's claim) and **matrix in
  `buf_case`:** these move existing claims.

## Row skip (W10-V, S3.5)

Added for the display driver: an Amiga plane's rows are `fetch + BPLxMOD`
bytes apart, not 40/80. The argument block gains an input and three state
arrays (the W7-V offsets do not move; `+70`, the old u8 row advance, is
unused):

| Offset | Field |
|---|---|
| +72 | `c2v_a_skip`, 4 × s16: bytes skipped after each converted row of plane p |
| +80 / +84 / +88 | `c2v_v_adl` / `adh` / `adb`, 4 × u8 each: row advance = row width + skip, sign-extended to 24 bits |

- **Entries.** `c2v_mc2s` / `c2v_hiress` convert with the caller's
  `c2v_a_skip`. `c2v_mc2` / `c2v_hires` zero it first and then do the same,
  so the W7-V entries behave exactly as before (contiguous rows):
  `tests/run_c2v.py` passes unchanged, teeth included.
- **Cost.** The two inner loops are unchanged. In `C2V_FETCH`, the high and
  bank adds read the per-plane advance (`adc abs`) instead of `adc #0`:
  +4 cycles per plane row, so +1,600 (mc2) / +3,200 (hires) per frame.
  `c2v_setup` computes the advances once per frame (~150 cycles).
- **Precondition, unchanged:** no single row crosses a 512 KB REU line.
  The rows of a plane may now lie anywhere, and the display driver checks
  every row it will fetch (below).

## Display refresh (S3.5)

Wave 10, package W10-V. `runtime/ops/disp.s` reads the display-state block
`disp_st`, which the copper and the CPU register paths of W10-P write
(`docs/WAVE10_BRIEF.md` C1). It decides a mode and shows the playfield
through the kernels above. Host model: `host/c2v_ref.py` `disp_decide`,
`disp_convert`. Contract: `docs/CONTRACTS.md` "Display refresh (S3.5)".

| File | Role |
|---|---|
| `runtime/ops/disp.s` | `disp_init`, `disp_frame` (the `cop_dhook` target), `disp_refresh` |
| `runtime/ops/disp_lut.inc` | the LUT, nibble-packed, 2 KB (GENERATED by `c2v_ref.lut_inc()`, pinned by a host test) |
| `host/c2v_ref.py` (additive) | `disp_decide`, `disp_convert`, `pack_disp_st`, `pack_lut`, `lut_inc`, `pack_skip` |
| `host/tests/test_disp_ref.py` | the model, the C1 offsets disp.s uses, the codes, the LUT include |
| `tests/run_pf.py` (+ `tests/pf/shim.s`) | VICE: the shim leg (host-filled `disp_st`), the fixture leg (`pf_static`), `--teeth`, `--perf` |
| `fixtures/c/pf_static.c` | the M3 fixture (vbcc, HLE) |

### The LUT in the REU

The guest claim is `disp_lut` = `$DF0000-$DF0FFF` (`rc/guest.inc`,
supervisor ruling). These are the REU bytes under the custom-chip bank.
Since S3.1 every guest access to bank `$DF` traps to `chip.s` and never
reaches the REU, so the guest can neither see nor corrupt the table, and
the range stays valid for the S3.7 AROS profile (on any Amiga bank `$DF`
is the custom chips; `$800000`, the first proposal, is Zorro-II fast RAM
there). The shim leg's `lut-trap` step checks it: 30 guest stores through
`mem_write8/16/32` across and around the range leave the stashed LUT intact
and count as open-bus accesses (red: a variant with `disp_lut=$800000`
loses 12 bytes and counts none). The HLE DOS moves bulk data with the REC
directly, so it refuses Read/Write buffers that touch bank `$BF`/`$DF`
(fix round 1, F2: IoErr 212, no DMA; leg `dosbuf`). The CPU's `$BF`/`$DF`
trap and the DOS buffer check together keep the guest out.

`disp_init` unpacks the 2 KB in-PRG table into `c2v_stage` 256 bytes at a
time and stashes it there (16 DMAs, BASIC banked out). `c2v_pal_*` and the
blank border look it up with one byte DMA each.

### Decision (`disp_decide`, in this order; the first failure blanks)

| Check | Blank reason (`disp_why`) |
|---|---|
| `disp_st` valid (+103) = `$A5`, i.e. W10-P's `cop_reset` ran | 7 NOSTATE |
| DMACON has DMAEN and BPLEN | 1 DMA |
| BPLCON0 has neither HAM (bit 11) nor DBLPF (bit 10) | 2 HAMDPF |
| lores with BPU = 2 → mc2; hires with BPU = 4 → hires silhouette | 3 DEPTH |
| L = DIWSTOP line − DIWSTRT line > 0 (stop V8 = NOT V7, start V8 = 0) | 4 DIW |
| fetch ≥ row (40 / 80 B) and every skip fits in s16 | 5 DDF |
| no fetched row crosses a 512 KB REU line or wraps past `$FFFFFF` | 6 STRADDLE |

- **Fetch width:** lores `2 × ((DDFSTOP&$F8 − DDFSTRT&$F8)/8 + 1)`, hires
  `2 × ((DDFSTOP&$FC − DDFSTRT&$FC)/4 + 2)` bytes. The converter takes the
  **first** 40 / 80 bytes of each fetched row, and skip = fetch − row + mod.
- **Modulo:** plane p (0-based) uses BPL1MOD if p is even and BPL2MOD if it
  is odd. Pointers and modulos ignore bit 0 (word DMA).
- **Vertical:** if L > 200, the picture is cropped about the centre
  (`(L − 200) / 2` rows skipped, by advancing each plane pointer). If L <
  200, all 200 lines are converted and lines L..199 are then cleared to 0,
  i.e. colour 0, which is what the Amiga shows below DIWSTOP.
- **The 512 KB line** (the S3.5 entry criterion from "Caller contract"
  above): every row the kernel will fetch is checked, 200 per plane
  (~25 cycles per row). A crossing row blanks the screen: the plane is
  clamped, not split. Splitting the fetch is backlog.
- Ignored in v0: BPLCON1 (scroll), BPLCON2 (priorities), LACE, the DIW
  horizontal start/stop, colours 4–31.

### What the C64 shows

- **Shown** (mc2 / hires): the bitmap, the matrix and colour RAM come from
  the kernels (`c2v_pal_*`, then `c2v_mc2s` / `c2v_hiress`). The VIC is
  then set: `$DD00` bits 1–0 select the bitmap's bank (3), `$D018` =
  `$48` (matrix `$D000`, bitmap `$E000`), `$D011` = `$3B` (bitmap mode,
  display on, 25 rows), `$D016` = `$18` (mc2) / `$08` (hires), and
  `$D020` = `$D021` = colour 0.
- **Blank after a shown picture:** `$D011` = `$0B` (display off, so the
  whole screen shows the border) and `$D020` = LUT[COLOR00]. The bitmap is
  left as it is.
- **Blank before anything was shown** (`disp_mode` = `$FF`): log only. The
  VIC is not touched, so the HLE console at `$0400` stays visible until the
  guest really turns a playfield on.

### Refresh policy v0

- `disp_st` is RAM under BASIC ROM (W10-P's claim at `$B800`). Every entry
  banks BASIC out (`$37 → $36`) before it reads the block.
- `disp_frame` returns at once when `gen` equals the last value it saw
  **and** the `dirty` byte (`disp_st+104`) is 0. W10-P sets `dirty` on
  every `gen` bump, so a `gen` that wrapped back to the old value (256 net
  changes) is still a change (fix round 1, F1; leg `wrap`).
- On a change it copies `disp_st+1..+70` (DMACON through COLOR15) and the
  valid byte, then clears `dirty`. If the copy equals the previous one,
  nothing more happens. W10-P bumps `gen` only on a net value change, so
  this costs one 71-byte compare at most.
- Inside `mach_run` (`mach_in` ≠ 0) a conversion is **deferred** until
  `DISP_N` × 14188 guest E ticks (24 frames' worth since W12-V, was 20; `docs/PERF.md` (j), (m))
  have passed since the previous conversion **ended** (F3: the round-0
  frame count from the start livelocked the guest at s ≤ 1). A deferred
  change leaves `gen`/`dirty` unseen, so a later frame converts it.
- Outside `mach_run` (the HLE runners never call it), every change converts
  at once: one conversion per COPJMP strobe.
- `disp_refresh` converts unconditionally.
- No dirty tracking of bitplane writes (backlog). A program that draws
  after the copper starts must change a display register or call
  `disp_refresh`.

### Host-visible state (`runner.lbl`, BSS)

`disp_mode` (`$FF` none / 0 blank / 1 mc2 / 2 hires), `disp_why` (1–7),
`disp_nconv` / `disp_nblank` (u16), `disp_seen`, `disp_ok`, `disp_cur` (the
71-byte snapshot), `disp_laste` (u32: E when the last conversion ended),
`disp_bud` (nonzero: that reference applies), `disp_lines`, `disp_crop`;
`DISP_N`, `disp_lut`.

### Tests (`tests/run_pf.py`)

- **Shim leg**: init (LUT readback, the hook, the state), `lut-trap`, and
  16 scripted steps (the first: valid ≠ `$A5`). The host writes W10-P's
  real `disp_st` (`$B800`, RAM under BASIC ROM) and calls `shim_kick`
  (`tests/pf/shim.s`, test builds only), a fake `cop_frame` end. The page
  `$B800-$B8FF` is checked exactly: the block as written, the spare tail
  unchanged. After each one: the whole RAM at `$E000-$FFFF` and `$D000-$DFFF`
  against the guard pattern with exactly the reference overlay (or
  untouched), colour RAM, `c2v_a_bgc`, the VIC registers, the state counters,
  `$01`, the I flag, and `m_rok` (0 after REC use, untouched otherwise).
- **Fixture leg**: `pf_static` through `cop_reset` + `disp_init`, then the
  HLE run. Its planes are read back from the REU and checked against the
  formula, and the conversion against `disp_convert`. `disp_nconv` must be
  2 (the decoy palette, then the final one), and the LUT must be intact
  after the program's stores over `$DF0000-$DF0FFF`. The fixture alone
  catches the five C3 mutants and `bank`. `straddle`, `clr` and `budget`
  need the shim leg's dedicated steps.
- **Red**: `--variant stub`, and the base PRG (no `disp.s`): the bitmap is
  left intact and the VIC is untouched.
- **Review legs (fix round 1):** `wrap` (F1: 255/256/512 CPU COLOR00
  changes, then a no-op COPJMP2: the display follows), `dosbuf` (F2:
  `fixtures/c/pf_dosbuf.c`, Read/Write with `$BF`/`$DF` buffers fail with
  IoErr 212 and the LUT stays intact), and `live` (F3 + TOD: a level-3
  handler edits COLOR01 every delivered VERTB under the live `mach_run`.
  At s = 4 static/edit and s = 2 edit, with the real CIA callbacks: the
  picture is byte-exact, deliveries = handler runs, CIA-A's TOD =
  `mach_frames`. At s = 0 edit, on the review's irq-driver rig, it must
  HALT. With editing, conversions ≤ frames / (`DISP_N` + c) + 2, c = one
  conversion in frames (≥ 380,000 φ2), because the budget runs from each
  conversion's end). Each is red on 498ab52.
- **Teeth** (`--teeth`, each mutant on the legs meant to catch it):
  `C2V_MUT_BITORDER`, `DISP_MUT_PAL`, `DISP_MUT_GEN`, `DISP_MUT_PPORD`,
  `DISP_MUT_MOD`, `DISP_MUT_STRADDLE`, `DISP_MUT_CLR`, `DISP_MUT_BUDGET`,
  `DISP_MUT_BANK` (BASIC ROM left in), `DISP_MUT_DIRTY` (F1),
  `DISP_MUT_EFROM` (F3: the budget from the start). Red variants: `stub`,
  `lutram`.

### Link layout

`disp.s` links after `cia.s` and before `mach.s` (Makefile). Its CODE
(including the 2 KB LUT) and its BSS are each padded to whole pages, and it
has no RODATA or DATA, so `mach.s` moves by whole pages only.

## 1:1 view (W13-D)

Wave 13, package W13-D (`docs/WAVE13_BRIEF.md` D1). This is a second
display mode next to the fit view, for reading text. **One Amiga hires
pixel is one C64 hires pixel and one Amiga line is one C64 line.** The
window is 320×200 of the Amiga screen; with LACE it is 200 *frame* lines,
so both fields are shown.

- Host reference: `c2v_ref.one_cells`, `convert_one`, `disp_decide_view`,
  `disp_convert_view`.
- Contract: `docs/CONTRACTS.md` "Display refresh (S3.5)", "1:1 view
  (W13-D)".
- The fit view's code paths are unchanged, so its output is
  byte-identical. `c2v_pick1` is a new label inside `c2v_pick`; no
  instruction changed.

### The kernel (`c2v_ones` / `c2v_ofin`, `c2v.s`)

- **Fetch.** `c2v_setup` runs with an 80-byte width, so a row advances
  80 + `c2v_a_skip[p]`, exactly as in the fit view. The REC length is then
  set to 40: each line fetches 40 bytes of each of the 4 planes from
  `c2v_a_src[p]`, which disp.s points at the window's first byte. The
  fetch is a loop, not `C2V_FETCH`'s unrolled form: about a quarter of the
  bytes, ~50 more cycles per line.
- **Per 8×8 cell:**
  - **bg** is the pen of the cell's top-left pixel (bit 7 of the cell's
    first line). `c2v_omask` takes it once per char row and also builds
    the cell's four plane masks ($FF where bg has the bit).
  - **A C64 pixel is set iff its pen ≠ bg**: OR over p of (plane p XOR
    mask p).
  - **fg** is the pen of the first set pixel in raster order. It is found
    by `c2v_pick`'s tail (`c2v_pick1`), once per cell. A cell with one pen
    has no fg ($FF) and shows bg in both nibbles.
- **Two-pen cells are pixel-exact**, whichever of the two pens the
  top-left pixel has. Examples: Topaz on the insert-disk screen, and the
  title bar's pen-1 text on pen 2.
- **Three or more pens: lossy.** The top-left pixel's pen is bg. Every
  other pixel shows in fg's colour (the first non-bg pixel's pen).
  `run_c2v` case `three-pen` and `host/tests/test_c2v_one.py` pin this.
- **Colours: the W12-V rule, two passes over the same rows.**
  1. `c2v_ones` converts and counts every cell's bg and every fg in
     `c2v_v_cnt`.
  2. disp.s runs `disp_cols` on all 16 pens, the fit view's rule.
  3. `c2v_ofin` sets `c2v_a_bgc` and converts again. At each char row's
     end (`c2v_orow`) it writes the row's 40 matrix bytes,
     `c2v_v_col[fg]` << 4 | `c2v_v_col[bg]`, at `$01` = `%xx100` under
     `sei` with the NMI guard (as `c2v_fill`).

  Nothing limits the number of (fg, bg) combinations. A first design with
  a 16-entry pair table was dropped: AROS's eyes used all 16 entries
  before the text rows, so the text lost its colour. The second pass
  doubles the kernel time (PERF (o)).
- **Colour RAM** is not written. The VIC does not read it in hires; it
  keeps whatever the fit view left.
- **Entries.** `c2v_ones` and `c2v_ofin` are called with BASIC banked
  out, as disp.s calls them. `run_c2v.py`'s `port_tramp` calls them at
  `$36`.
- **Work RAM:**
  - plane masks at `P_p + $40`;
  - bg pens at `P0 + $D0`;
  - fg pens at `C2V_CP`;
  - the pass flag at `c2v_args` + 144.

  All of these are in c2v's own regions, past the 40-byte rows.
- `C2V_ROWS` must be 25.

### Window, LACE and the toggle (disp.s)

- **Applies to:** a hires decision only (BPU 1-4, below). An mc2 (lores)
  picture stays in the fit view, which already shows every lores pixel's
  colour.
- **Window, without LACE:** bytes `disp_px` .. `disp_px` + 39 (0 ≤ px ≤
  40) of every 80-byte row, from line `disp_crop` = min(`disp_py`,
  `disp_ymax`), where `disp_ymax` = L − 200, or 0 if L ≤ 200.
- **With LACE (BPLCON0 bit 2):**
  - The 1:1 window is 200 frame lines from field line `disp_crop`, that is
    frame line 2·crop (or 2·crop + 1 on the odd field's pointers).
  - Each row advance is halved: skip := (skip + 80) / 2 − 80, which for
    BPLxMOD = 80 is advance 80. The two fields interleave in memory.
  - `disp_ymax` = L − 101 (fix round 1: the odd field's last frame line
    is ≤ 2L − 1).
  - `disp_lines` := min(2 · L, 200).
  - `disp_decide`'s 512 KB check covers only the current field's rows.
    The other field's rows in between are unchecked: a straddling row
    could only show wrong pixels in that row (the DMA target is fixed).
  - The fit view still shows one field (frozen).
- **Toggle:** F1 (`inp_view` bit 0, W13-I's I1 byte). Until W13-I lands,
  disp.s defines a stand-in under `DISP_INPV_LOCAL`.
  - It flips `disp_view` and centres the window: `disp_px` = 20,
    `disp_py` = `disp_ymax` / 2.
- **Rows past the picture** (frame line ≥ `disp_lines`) read 0 in all four
  stage rows: `disp_ozero` (disp.s, BHI), called by `c2v_ones` after each
  line's fetch, against `c2v_v_olim` (`c2v_args` + 145), which `disp_one1`
  sets. They add nothing to the counts and show pen 0, as the fit view
  does.
  - It converts at once, past the duty budget, because it is user-paced.
  - Only bit 0 is cleared.
- **A test sets the offset** with `disp_px`/`disp_py`. The next conversion
  (a field change, the pixel epoch, `disp_refresh`) uses it.
- **The pointer module** moves the window itself (re-centring: CONTRACTS
  "Pointer sprite (W13-D)").

## Hires with 1-3 planes (W13-D)

- **What converts.** `disp_decide` now converts hires with BPU 1, 2, 3 or
  4 (before: 4 only; the rest was DEPTH). This applies to both views.
  Example: 3.1's Workbench, BPLCON0 $A200.
- **Absent planes read the zero row.** For each plane past BPU,
  `disp_args` sets source = `disp_zero` and skip = −80, so the row advance
  is 0 and the plane reads `disp_zero`. That is 256 zero bytes in the REU
  under the trapped `$DF` bank (`$DF4000-$DF40FF`, rc `guest.inc` /
  `machine.inc`, split off the io region), stashed by `disp_init`. The
  kernels are unchanged, and no stale plane pointer is ever read.
- **Unchanged:** lores BPU 2, hires BPU 4, and every BLANK reason.
- **Changed expectations:**
  - `host/tests/test_disp_ref.py`'s DEPTH case "hires 2 planes" ($A200)
    is now "hires 0 planes" ($8200).
  - `host/tests/test_aros_profile.py` expects the io region to start at
    `$DF4100` and the `disp` zero row at `$DF4000`.

### Memory

- c2v CODE +1 page ($600 → $700, 9 bytes spare).
- disp CODE +1 page ($A00 → $B00).
- disp BSS: 12 bytes inside its page (`disp_view`, `disp_px`, `disp_py`,
  `disp_ymax`, `disp_one`, `inp_view`, `spr_vec`, `spr_id`).
- The F1 toggle, the `spr_vec` hook with its ID guard, and `disp_ozero`
  are in BHI's tail (`BCODE`, $A778-$A7DA).
- MAIN: BSS end $9C0B → $9E0B (+512 B; budget 544 B).
