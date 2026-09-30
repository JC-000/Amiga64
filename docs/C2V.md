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
  open (S3_PLAN §2.1).

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
and is filled with colour 0. Palette changes are the only time the screen
matrix is written: never per frame.

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
