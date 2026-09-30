# S3 plan: chipset shim, VIC-II mapping and the AROS boot surface

Wave 6, package W6-D (`docs/WAVE6_BRIEF.md` C5). This is a design document.
Nothing in `runtime/` changes. It answers C5's four questions with evidence,
and every number carries a tag that says where it comes from:

| Tag | Meaning |
|---|---|
| **[MEASURED HW]** | measured on the U64E; quoted from `docs/HARDWARE.md`. 48 MHz clocks are fw 3.15 (38.4–39.4 MHz effective) unless marked 3.14d |
| **[MEASURED VICE]** | measured in VICE: exact emulated cycles or exact event counts |
| **[MODEL]** | measured in the host-side boot model (§3.1): exact counts for that model, not an A500 trace |
| **[SOURCE]** | read from the AROS sources |
| **[EST]** | an estimate derived from tagged inputs; the derivation is given. "[EST, static]" = cycles added up from 6502 instruction timings |

Tools live in `tools/s3probe/`. Raw evidence is in
`build/evidence/w6-s3plan/`, which is git-ignored and holds no ROM bytes. The
commands are in §6.

## 0. Key conclusions

1. **Trap on bank change, not on every call.** The memory API already
   compares the guest bank with the REC shadow (`m_rbk`) on every call. If
   the check for the two I/O banks ($BF CIAs, $DF custom chips) sits only on
   the *bank-changed* branch, and a trapped access never loads `m_rbk`, then
   an I/O access can never take the same-bank fast path. The cost is
   **+0 cycles on same-bank calls** and **+8 cycles on bank switches**
   [EST, static]. Bank switches measured on real programs are **0.14–0.35 per
   instruction** for vbcc programs [MEASURED VICE] and **0.10 per instruction** for
   the AROS boot [MODEL] (~0.16 once cold calls are counted the same way,
   §1.3). That is ≤2.8 cycles per instruction, **≤0.55%** [EST] of the
   measured 518–674 cycles per instruction at 48 MHz [MEASURED HW].
2. **Guest time and interrupts can cost the hot path nothing.** A C64 CIA
   timer IRQ sets a bit in `st_halt`, which the dispatch loop already tests.
   Guest time must be *dilated*, though (§1.5). At A500 time rates, AROS's
   steady-state background load (11% of an A500 CPU [MODEL]) would take
   ~100% of our 48 MHz interpreter [EST].
3. **VIC-II conversion.** Two kernels were measured in VICE and checked
   against a Python reference. 4-plane hires 640 px → 320 px hires costs
   **573,820 cycles per frame**. 2-plane lores → multicolour costs
   **327,620** [MEASURED VICE]. With the REU fetch and the per-line row
   advance, a full refresh takes about **35 ms / 14 ms at 48 MHz** and
   **0.69 s / 0.37 s at 1 MHz** [EST].
   2-plane lores maps with **exact colours**. 16-colour hires can only be
   approximated.
4. **The AROS ROM reaches its "insert disk" (no-media) screen in 3.33 M
   instructions** in the boot model, using exactly the machine model this
   plan proposes: flat REU memory, trapped $BF/$DF, a synchronous blitter
   and a once-per-frame copper [MODEL]. The screen was rendered and
   checked (§3.2). At 48 MHz that is **about 70–95 s**, including
   ~17 s of code-window refills; at 1 MHz it is **about 40 minutes** [EST].
   Feasible, at turbo only.
5. **The minimum boot surface is small** (ablation, §3.4). The boot logic
   needs CIA-A (timers, TOD, ICR → IRQ 2), VERTB (IRQ 3), a moving beam
   and Paula INTENA/INTREQ. Copper and blitter are needed for the pixels
   only. CIA-B timers/TOD/IRQ, SOFTINT and the blitter IRQ are not needed.
   CPU side: one illegal and one F-line exception (CPU detection),
   RTE/STOP/MOVE USP/SR ops, autovectored IRQs; no TRAP #n.
6. **Next increment:** the trap mechanism with a stub chip file and serial
   capture (§4, step S3.1). It is independent of W6-S's instruction work,
   perf-critical, and gives every later step a test hook.

## 1. Minimum OCS surface and the trap mechanism

### 1.1 Surface for M3, a static playfield (`docs/ARCHITECTURE.md` M3)

| Area | Registers | Notes |
|---|---|---|
| Bitplanes | BPLCON0/1/2, BPL1–6PTH/L, BPL1MOD, BPL2MOD, DIWSTRT/STOP, DDFSTRT/STOP | read at conversion time; BPLCON1 scroll ignored in v0 |
| Palette | COLOR00–31 | → 16-colour VIC LUT (§2.2) |
| DMA control | DMACON (DMAEN, BPLEN, COPEN), DMACONR | BPLEN off → blank screen |
| Copper | COP1LCH/L, COP2LCH/L, COPJMP1/2, COPCON | MOVE interpreted once per guest frame; WAIT/SKIP ignored (last value wins) |
| Sync | VPOSR, VHPOSR, INTENA/INTREQ(R) VERTB | a `WaitTOF`-style poll must see the beam move |

Not needed for M3: sprites, audio, disk, blitter, serial. Unknown registers
in $DFF000–$DFF1FF read as 0 and log their writes; a write to a read-only
register is ignored. "Reads 0" is only the *default*: registers whose read
value matters (POTINP, SERDATR, DENISEID, VPOSR, CIA ports) get the fixed
values of §3.4 — POTINP = 0 would mean "right button down" to AROS.

### 1.2–1.3 Where and how to trap: a bank-change hook

`runtime/core.s` `MIO_PROG`, the inline REC setup of all six
`mem_read*/mem_write*` entries, ends by comparing the bank byte with the
shadow (same bank: 9 cycles; changed: 15). The cold path (`m_rok` = 0,
after every window refill and `mem_rec_dirty`) goes through `mio_full`; a
512 KB straddle through `mio_split`. The change S3.1 would make (not code
for `runtime/` now; the lines marked NEW are added):

```
hiok:   lda m_addr+2        ; 3
        cmp m_rbk           ; 3
        beq done            ; 3   same bank: UNCHANGED (+0)
        cmp #$DF            ; 2   NEW: custom chips
        beq trap            ; 2   NEW
        cmp #$BF            ; 2   NEW: CIAs
        beq trap            ; 2   NEW  -> bank-change path +8 cycles
        sta m_rbk
        sta REC_REUBANK
done:   ...
trap:   jmp chip_r32        ; per entry: chip_r8/r16/r32/w8/w16/w32
```

- **Invariant: `m_rbk` never holds $BF or $DF.** The warm path traps before
  it stores `m_rbk`; the cold path runs the same 8-cycle test inside
  `mio_full`, before any REC store, and then re-enters the bank compare
  (`jmp hiok`), so a cold call costs +23 cycles, not +8. With that, the
  vbcc programs pay 1.9–4.1 cycles/instruction [MEASURED VICE counts], not
  1.1–2.8 (as built in W7-T; `docs/PERF.md` (g)). So every access whose *first byte* is in $BF/$DF
  arrives at the compare as a bank change and cannot take the same-bank
  fast path. A trapped call leaves the REC consistent with the shadow (the
  length/low bytes stored before the trap are harmless) and issues no DMA.
- **Bank-carry hole (review M2) and its fix.** A 16/32-bit access that
  *starts* in $BE/$DE and carries into $BF/$DF (`move.l $BEFFFE,d0`,
  `$DEFFFE`) is not a 512 KB straddle, so today it would go through bank
  $BE and the REC would carry its last bytes into REU $BF0000. Fix, free
  on the hot path: STRADDLE's slow branch is only reached when the low
  byte is ≥ $100−(len−1) and `m_addr+1` = $FF; there, also route
  bank+1 ∈ {$BF,$DF} to `mio_split`, which classifies each byte's bank and
  traps per byte (open-bus/RAM for the non-I/O bytes).
- **Split path (review L5).** `mem_read16/32` do `jsr mio_split` then `jmp
  copy` (t_tmp → m_val), which would overwrite a trapped `m_val`. The trap
  decision is therefore taken *before* the `jsr` (the split routine only
  moves bytes), or trapped bytes are written into `t_tmp` so `copy` stays
  correct. A long at `$DFFFFE` spans $DF and $E0: the $DF half goes to the
  chip handler, the $E0 half to REU RAM through the byte loop; nothing is
  dropped.
- **Chip ABI** = the memory API's contract: `m_addr` in, `m_val` LE in or
  out, X/Y preserved (as `tests/rec_apisweep.py` checks today), `rts` to
  the API's caller. Custom chips: `m_addr` & $1FE inside $DFF000–$DFF1FF,
  the rest of the bank is open bus (reads 0). CIA-A = odd bytes with
  A12 = 0, CIA-B = even bytes with A13 = 0, register = `(m_addr+1)` & $0F.
  A 32-bit access is two 16-bit register accesses; a byte write to a
  custom register lands on both halves, as on the OCS bus.
- **Scope.** The window refill (instruction fetch) is not checked;
  executing from $DFxxxx is not a real case, and HLE code never addresses
  the I/O banks.

**Cost.** [EST, static] from the listing above. The call and bank-switch rates
are [MEASURED VICE] / [MODEL] measurements (`bank_probe.json`,
`bankstats/boot_report.json`).

| Workload | Calls/instr | Bank switches/instr | Hook +cyc/instr | Unconditional check +cyc/instr |
|---|---|---|---|---|
| vbcc `hello` | 0.716 | 0.221 | 1.8 | 5.7 |
| vbcc `scroll` | 0.640 | 0.154 | 1.2 | 5.1 |
| vbcc `sysbase` | 0.686 | 0.194 | 1.6 | 5.5 |
| vbcc `exit` | 0.829 | 0.353 | 2.8 | 6.6 |
| vbcc `alloc` (922k instr) | 0.465 | 0.142 | 1.1 | 3.7 |
| AROS boot, 6.0 s bankstats run (3.37M instr) [MODEL] | 0.565 | 0.101 (~0.16 comparable) | 0.8 (~1.3) | 4.5 |

The AROS row is not directly comparable: `bank_probe` also counts the
`mio_full` site (cold calls after every window refill or `mem_rec_dirty`,
37% of the vbcc switches: hello 230/628, alloc 48,346/131,140 in
`bank_probe.json`), while the model has no window and counts real bank
changes only. With the same cold share AROS is ~0.16/instr, ~1.3
cycles/instr [EST]. "Unconditional" = the same two compares on every call. Against the
measured 518–674 cycles/instr at 48 MHz [MEASURED HW] the hook costs **0.12–0.55%**,
the unconditional check 0.55–1.3%. The `tests/rec_budget.py` loops (MEM,
STREAM, RMW, ALU) never switch bank, so the hook predicts **0.0%** for the
HARDWARE.md benches. It adds **no REC accesses** (the ~1 µs hardware
ceiling [MEASURED HW]), so `rec_budget` stays bit-identical.

**Trapped-access volume.** 19,464 I/O calls in 1.9 M data calls (1.0%)
[MODEL]; at ~200–400 cycles each, ~0.1–0.2 s of the boot [EST].

### 1.4 DMA channels in scope

| Channel | S3 treatment |
|---|---|
| Bitplane | the source of the VIC conversion (§2); no per-line DMA emulation |
| Copper | interpreted at guest vblank, MOVEs only. AROS boot: ~85 MOVEs per frame (30,108 over 353 frames), 15% of them `$1FE` no-ops [MODEL]. ~100 cycles/MOVE ≈ 9k cycles per guest frame [EST] |
| Blitter | synchronous at the BLTSIZE write. BBUSY always reads 0, and INTREQ BLIT is set at the end |
| Sprites, audio, disk | registers accepted and ignored; the pointer sprite could later become a VIC sprite |

### 1.5 Guest time, events and interrupts

- **Time base.** A C64 CIA-2 TA→TB cascade runs free as a 32-bit φ2
  counter (its ports keep driving the VIC bank, its NMI stays off). CIA #1
  timer A, one-shot, is armed for the next guest event (VERTB, CIA
  underflow, TOD alarm). The C64 CIAs are assumed to count φ2 (~0.985 MHz)
  whatever the turbo setting: **unverified here** (§5, risk 4).
- **The event path costs 0 cycles per instruction.** The CIA #1 IRQ
  handler (~40 cycles [EST, static]) sets `ev_pend` and ORs $80 into
  `st_halt`; `dispatch_go` and W6-S's `dispatch_gox` both already begin
  with `lda st_halt / bne dispatch_done`. A new machine loop (`mach_run`,
  S3.2) wraps `emu_run`. On return it
  services `ev_pend` (guest time, VERTB, CIA underflows, copper, a display
  refresh within its duty budget). Then, in exception mode only, if
  `INTENA & INTREQ` gives a level above SR's IPL, it raises vector
  24+level through W6-S's `exc_raise` frame code (stacked PC = `r_pc`,
  I2–I0 := level) and re-enters `emu_run`. `ev_pend` is separate from
  `st_halt`, so a halt that overwrites `st_halt` loses no event. The
  handler never touches the REC; the converter's bulk DMA calls
  `mem_rec_dirty` (C1).
- **Who enables the IRQ (review L4).** CIA #1 timer interrupts are enabled
  only inside `mach_run` (ICR mask set on entry, cleared and `sei` on
  exit). `tests/runner/run_program.s`, `runner.s` and every existing driver
  keep today's `sei` and treat any nonzero `st_halt` as a halt, so HLE and
  vector runs never see an event and the Wave-5 counts (762/433/280/42/63)
  are unaffected. No other code reads `st_halt` (runtime/, host/, tests/,
  and W6-S's tree).
- **Race-free clear.** On return from `emu_run`, `mach_run` clears bit 7
  of `st_halt` under `sei` (the read-modify-write must not race the
  handler's `ora`), then tests `st_halt & $7F`, not `= $80`: $81 is a real
  halt (`emu_halt` stored #1) with an event pending — the halt goes back
  to the caller and `ev_pend` stays set for the next `mach_run`. Otherwise
  it copies and clears `ev_pend` (still under `sei`), `cli`, services.
- **Vectoring.** `$0314` sits in rc region "sys $0200–$03FF, not
  claimable", so the KERNAL path is out. `mach_run` runs with KERNAL
  banked out (`$01` = $35; the interpreter never calls it) and owns the
  RAM vectors at $FFFA–$FFFF: IRQ → the event handler, NMI → `rti`
  (RESTORE key). S3.2 claims them in `runtime/rc/ram.inc` as
  `@region s3 $FFFA-$FFFF 6502 NMI/RESET/IRQ vectors in RAM under KERNAL`
  (the bitmap of §2.4 ends at $FF3F), and restores `$01` on exit.
- **Recognition latency.** A CPU write to INTENA/INTREQ or an IPL drop
  acts only at the next event. The reviewer's `defer_irq.py` defers every
  CPU-originated raise in the model and still reaches the no-media frame
  at 3,328,539 [MODEL]. If needed later, the chip handler can force an
  early `mach_run` pass via `st_halt`.
- **STOP.** On `HALT_STOP` (W6-S), `mach_run` waits for `ev_pend` or
  fast-forwards guest time to the next armed event (idle skip). The ROM
  idles only 0.15 s of 5.5 s before the screen [MODEL].
- **Time dilation, a key design parameter.** Guest E-clock = φ2 counter
  >> *s*.
  An A500 E tick is 1.41 µs, so *s* = 4 makes guest time run k ≈ 11.5×
  slower than wall time. Why: after the no-media screen is up, AROS runs
  ~58k instructions per virtual second (11% busy: VBL servers,
  timer.device, the 2 fps animation) [MODEL], while our interpreter
  manages ~35–50k effective IPS at 48 MHz including refills [EST, §3.5];
  undilated, background work alone saturates it. With k ≈ 10 the guest sees an A500-like
  CPU/interrupt ratio, at the price of k× longer delays in wall time. *s*
  is a runtime setting (0 real time, ~4 at 48 MHz, ~9 at 1 MHz).
  Calibration stays consistent because every guest clock derives from one
  counter: AROS's TOD-vs-timer-A probe (`kernel/kernel_timer.c` [SOURCE])
  still reads 14,188 → 50 Hz, and the VHPOSR PAL probe
  (`timer/initcustom.c`) still sees >300 lines.
- **Determinism.** VICE is cycle-exact, which is enough for the gate; on
  the U64E tests assert outcomes (serial log, screen hash), never
  interleavings. A per-instruction virtual clock (`dec`/`beq`, +7
  cycles/instr ≈ 1.2% [EST, static]) is the fallback.

## 2. Mapping to the VIC-II

### 2.1 Modes

| Guest playfield | VIC mode | Mapping | Colour fidelity |
|---|---|---|---|
| lores, 1–2 planes (320 px) | multicolour 160×200 | MC pixel = (plane1, plane0) of the left pixel of each pair: `(P1 & $AA) \| (P0 & $AA) >> 1`, no tables | **exact** for ≤4 colours: %00→$D021, %01/%10→screen RAM nibbles, %11→colour RAM, set once per palette change |
| hires, 1–2 planes (640 px) | hires 320×200 | 2:1 pair-OR per plane, then "index ≠ 0" | 2 colours (bg + dominant fg) |
| any depth 3–5 (e.g. AROS insert disk: 640×256×4) | hires 320×200 | v0: pixel on iff colour index ≠ 0 (OR of planes, pair-OR 2:1) | silhouette. v1: per-palette boolean of the planes (a minterm-style sum of products, chosen from the palette's luminance), ~2–4× the v0 cost [EST] |
| vertical | crop | 256 → 200 lines: centre crop by default, top-aligned option; no scaling in v0 | – |

HAM, dual playfield, interlace and BPLCON1 scroll fall back to v0 OR-mode
with a logged warning.

### 2.2 Palette reduction

A 4096-entry OCS-RGB → VIC-colour table is built on the host (nearest
colour in a perceptual metric) and staged in REU once. On a palette change
(COLORxx writes, by CPU or copper) the converter re-reads up to 32 entries:
one byte DMA each, ~50 cycles warm, 3–5k cycles in total. It then refills
screen RAM and colour RAM, 2×1000 bytes, ~12k cycles [EST].

### 2.3 Conversion cost

`tools/s3probe/c2v.s` converts one 200-line frame from plane rows in C64
RAM to a char-cell-order VIC bitmap; `c2v_measure.py` times it with the
emulated CIA2 timers (screen blanked) and compares all 8000 bytes with a
Python model. Teeth: `--mutant` (hires table) gives 4600 wrong bytes,
exit 1; the reviewer's mc2 mutant gives 3400.

| Kernel | Cycles per frame [MEASURED VICE] | Cycles per output byte |
|---|---|---|
| hires4 640→320 (OR of 4 planes + pair-OR tables) | 573,820 | 71.7 |
| mc2 320→160 (2 planes, exact colours) | 327,620 | 41.0 |

The kernels convert one fixed source row 200 times (the Python reference
does too). A real converter re-patches 8 (hires) or 2 (mc2) source
operands per line, ~50–100 cycles, i.e. **+~3%** per frame [EST, static];
the table includes it. The REU fetch of the plane rows uses 0.30 µs/B at
48 MHz (bulk rate measured on fw 3.14d; not re-measured on 3.15) and
1.05 µs/B at 1 MHz [MEASURED HW]. Rows are fetched 8 lines per DMA; ~100
DMAs × ~9 REC accesses ≈ 1 ms at 48 MHz, ~4 ms at 1 MHz [EST].

| Full refresh [EST] | CPU at 48 MHz (38.9 eff., fw 3.15) | DMA | **Total 48 MHz** | CPU at 1 MHz (0.96 eff.) | DMA | **Total 1 MHz** |
|---|---|---|---|---|---|---|
| hires4, 64,000 B in | 14.8 ms (+3% rows: 15.2) | ~20 ms | **~35 ms** | 0.62 s | ~71 ms | **~0.69 s** |
| mc2, 16,000 B in | 8.4 ms (8.7) | ~5 ms | **~14 ms** | 0.35 s | ~21 ms | **~0.37 s** |

CPU columns = [MEASURED VICE] cycles × 1.03 ÷ the [MEASURED HW] effective
clock; pure CPU work on C64 RAM scales linearly (HARDWARE.md finding 5),
and the effective clock already folds in badline DMA (0.96–0.98 MHz at
1 MHz, i.e. 2–4%).

**Budget.** At 48 MHz with *s* = 4 (≈4.3 guest frames/s), refreshing
every guest frame costs 15% (hires4) or 6% (mc2) of the CPU [EST],
capped by a duty limit (default 20%). At 1 MHz a full hires4 refresh takes
34 C64 frames, so the policy is refresh-on-change with a minimum interval:
fine for a *static* playfield (M3), not for animation. Dirty tracking v0:
blitter writes (free: we execute the blit), palette and pointer changes
mark rows. CPU writes into bitplanes are not tracked (a range check costs
~+10 cycles per write [EST, static]); a rolling 1/8-screen refresh per
guest frame covers them.

### 2.4 C64 memory

VIC bank 3: bitmap at $E000–$FF3F (currently `free`, RAM under the
KERNAL; the vectors at $FFFA+ stay outside it). Screen matrix at
$C000–$C3E7 (claiming `buf_case` in the machine profile only) or in RAM
under I/O at $D000 (writes need `$01`=$34 under SEI). The HLE console at
$0400 stays in bank 0; switching is one `$DD00` store. Plane-row staging:
4×640 B at $A000–$A9FF (free). All become `runtime/rc/ram.inc` claims
checked by `make rescheck`.

## 3. The AROS boot surface

### 3.1 Method

- **Static** [SOURCE]: a sparse clone at `~/.cache/amiga64/aros-src`
  (`2f881971e9b4`, 2026-09-25). `aros_hwscan.py` lists every custom, CIA
  and privileged reference (`aros_hwscan.md`, over-approximate); the boot
  path (`rom_init.S`, `start.c`, `cpu_detect.S`, `memorytest.S`,
  `readgayle.S`, `amiga_irq.c`, `kernel_timer.c`, `initcustom.c`,
  amigavideo, disk/trackdisk, dosboot) was read by hand.
- **Dynamic.** `aros_boot_model.py` boots the **actual nightly ROM** from
  W6-O's cache (AROS-20260921, archive SHA `981817b3…`; the source is four
  days newer, so the model's numbers are the authority).
  CPU: Musashi 68000 (machine68k, built outside the repo by
  `setup_venv.sh`, which adds `set_irq`). Memory: 16 MB flat RAM, i.e.
  **the REU model** (ROMs are plain RAM at $F80000/$E00000, no mirrors or
  bus holes); only banks $BF and $DF are trapped. Chipset in Python: Paula
  INTENA/INTREQ with autovectored levels 1–6; CIA timers, TOD, ICR, alarm
  (CIA-A TOD = vsync, CIA-B TOD = hsync, E = CPU/10); a beam from the
  cycle count (454 × 313); the copper at vblank; a synchronous blitter.
  DF0 present and empty (CIA-A PRA inputs $EB). Timing is virtual (7.09
  MHz Musashi cycles, no DMA contention); counts are exact *for this
  model*.
- **Screen check.** The playfield is rendered to PNG every 0.5 virtual s.
  The no-media animation (640×256×4 hires) starts in the **5.5 s frame
  (icount ≤3,333,413)**, which shows only the AROS "eyes"; the "waiting
  for bootable media" logo and icons first appear in the **7.0 s frame
  (icount 3,429,641, +2.9%)** [MODEL]. The five animation frames are
  hashed in `boot/insert_disk_frames.txt`; the reviewer viewed them
  independently (hashes alone would be circular). No FS-UAE trace (§3.6).

### 3.2 Boot sequence [MODEL], first CPU touch of landmark registers

| Instructions | Event | Source |
|---:|---|---|
| 0 | reset: SSP/PC from the ROM image (overlay emulated by staging the ROM's first 8 bytes at 0) | `rom_entry.S` |
| 0–524k | **simulated ROM checksum**: `add.l (a0)+` over 512 KB, result unused | `rom_init.S` `normal_init` |
| ~524k | cartridge-ROM check at $F00000 | `rom_init.S` |
| 562,232 | CIA-A PRA/DDRA: overlay off, LED; INTENA/INTREQ/DMACON = $7FFF; SERPER, BPLCON0, BPL1DAT, COLOR00; then the Gayle probe | `rom_init.S`, `readgayle.S` |
| 562,316 / 562,325 | **MOVEC → illegal-instruction exception, FNOP → F-line exception** (68000 CPU detection) | `cpu_detect.S` |
| after 562k | memory tests: slow RAM $C00000–$DC0000, chip RAM 0–$200000 (INTENAR-mirror probe through INTENA writes); then `exec_boot` | `memorytest.S`, `start.c` |
| 1,226,562 | CIA-A TOD vs timer A calibration (busy-wait on TODLO) | `kernel_timer.c` |
| 1,255,311 | serial debug output starts (`ROMInfo…`, `romtaginit`) | nightly build |
| 1,270,303 | kernel IRQ init: autovectors 25–31, DIW/DDF defaults, INTENA VERTB\|SOFTINT | `amiga_irq.c` |
| 1,462,519 | disk.resource: drive-ID bit-bang on CIA-B PRB / CIA-A PRA | `disk_intern_init.c` |
| 1,677,694 | timer: PAL/NTSC beam probe (3,921 VHPOSR reads) | `timer/initcustom.c` |
| 2,193,444 | amigavideo: VPOSR/DENISEID chip probe (OCS: DENISEID floats → $FFFF) | `amigavideo_chipset.c` |
| 2,195,064 | first copper list (COP2LC per frame) | amigavideo |
| 2,919,770 | first blit | dosboot / graphics |
| 2,980,394 | boot-screen palette and bitplane pointers via copper | dosboot |
| **≤3,333,413** | **no-media animation starts** (eyes; 5.5 s virtual; not yet at 3,024,981 / 5.0 s) | `rom/dosboot/bootanim_nomedia.c` |
| 3,429,641 | "insert disk" logo and icons on screen (7.0 s frame) | same |

- Serial log in the model: `ROMInfo: 1MiB ROM detected`, two ROM regions,
  `callroms`, the memory list, then `romtaginit done`. The memory list is:
  chip `00000400–00200000`, slow `00c00000–00dc0000`, ROMs.
  The nightly build **has serial debug output**, which makes a free
  boot-progress oracle (§4).
- Hot spots [MODEL], from `pc_64b_blocks_top` in `boot/boot_report.json`
  (64-byte blocks, loops identified by disassembly); both run entirely
  before the screen, so shares are of 3,333,413:
  - the checksum block $F800C0–$F800FF: 562,228 = **16.9%** (524,288 of
    them the loop itself);
  - the ROMtag scan (`cmpi.w #$4AFC`, word by word over 1 MB of ROM),
    blocks $FEAB80 + $FEABC0: 507,288 = **15.2%**.
- Mix: MOVE 29%, Bcc 24%, $4x misc 14% (JSR/RTS/LINK 6%, MOVEM 2.6%),
  ADDQ/SUBQ/DBcc 10%, CMP 7.6%, ADD 5.3% (`boot/summary.md`). This is a
  MOVE/branch-heavy mix, the cheap end of PERF.md's range.

### 3.3 What the ROM touches before "insert disk"

- **Custom chips** [MODEL] (per-register table with first touch and R/W
  counts: `boot/boot_report.json`). CPU: INTENA/R, INTREQ/R, DMACON/R,
  VPOSR, VHPOSR, VPOSW, SERPER, SERDAT, SERDATR, ADKCON/R, BPLCON0–3,
  BPL1DAT, DIW/DDF, COLOR00, DENISEID, FMODE, POTGO, POTINP, JOY0DAT,
  DSKLEN, DSKSYNC, COP1LC, COP2LC, BLTCON0/1, BLTAFWM/ALWM, BLTA/B/CDAT,
  BLTB/C/DPT, BLTB/C/DMOD, BLTSIZE. Copper: BPL1–4PT, BPL1/2MOD,
  BPLCON0–3, DIW/DDF, COLOR00–19, SPR0–7PT/POS. Blits: 76 in 12 s, all
  normal-mode cookie-cut (`lf=$CA`, B/C/D, 323,720 words); no line, fill,
  BLTSIZH or A-channel read.
- **CIAs.** CIA-A: PRA, DDRA, DDRB, TA/TB, TOD (3 bytes), ICR, CRA/CRB.
  CIA-B: PRA/DDRA (serial lines), PRB/DDRB (drive select/motor), TOD, ICR,
  CRA/CRB. SDR is never touched (no keyboard traffic needed).
- **Interrupts.** Autovector levels 1 (20×), 2 (164×) and 3 (1,119×), plus
  one level-6 delivery, in 12 virtual s. There are no TRAP #n.
- **CPU and supervisor** (12 s): RTE 871, STOP 416, MOVE USP 324,
  ORI/ANDI/EORI to SR 884, MOVE to SR 163, MOVE from SR 6,029; one
  illegal-instruction exception (MOVEC) and one F-line exception.
  Supervisor() on a 68000 uses the privilege-violation vector (`start.c`
  sets `trap[8]` [SOURCE]). All inside W6-S's scope, plus interrupts.
- **Memory probing and odd spaces**, all harmless as flat RAM: chip RAM
  to $200000 (2 MB, since the REU has no mirrors); slow RAM
  $C00000–$DBFFFF (1.75 MB; SysBase lands there); Gayle ID at $DE1000
  (0 → none); Akiko at $B80002; autoconfig at $E80000 (zeros →
  `er_Reserved03` ≠ 0 → no board, `readexpansionrom.c` [SOURCE]); $F00000
  (no ROM). **No write ever goes to the ROM images**, so no write
  protection is needed (`bankstats`: `rom_writes` 0).

### 3.4 Ablation: which features are load-bearing [MODEL]

`tools/s3probe/ablate.sh` turns one feature off per run: 12 virtual s,
same deterministic baseline, screen matched by hash (`ablate/matrix.txt`).

| Off | Insert-disk screen | Failure mode |
|---|---|---|
| none (baseline) | **yes**, at 3,333,413 | – |
| blitter (no-op) / copper | no pixels, **boot logic completes** (same 3.72M instructions) | display only |
| CIA-A TOD frozen | no | busy-wait forever in the TOD calibration (8.4M instructions, no progress) |
| beam frozen | no | hangs in the VHPOSR probe |
| VERTB never raised | no | stalls just before the screen (timer.device VBLANK unit) |
| CIA timers frozen | no | stalls at ROMtag init (timer.device MICROHZ) |
| all IRQs / level 2 (CIA-A) | no | stalls early |
| CIA-B timers / CIA-B TOD / level 6 / SOFTINT / blitter IRQ | **yes** | not needed |

So the **minimum surface** to "insert disk" is: CIA-A timers, TOD and
ICR; Paula INTENA/INTREQ with IRQ levels 2 and 3; a moving beam; a
synchronous normal-mode blitter and a copper for the pixels; the W6-S CPU
work. CIA-B needs only its ports and a quiet ICR.

It also includes **fixed read values** the model supplies and the boot
relies on (review L8); the S3.1/S3.7 stub must return them, not the "reads
0" default:

| Read | Value | Why |
|---|---|---|
| SERDATR | $3000 (TBE, TSRE) | serial debug output polls TBE; a SERDAT write also raises INTREQ TBE |
| POTINP | $FF00 | bit 10 = 1: right button up (0 would be "pressed") |
| CIA-A PRA inputs | $EB | bit 6 = 1 (left button up; with POTINP bit 10 it keeps AROS out of the boot menu, `rom/dosboot/menu.c:900` [SOURCE]); /RDY high, /TK0 low (track 0), /WPRO high, /CHNG low (no disk) |
| DENISEID | $FFFF | OCS has no DENISEID; amigavideo then takes the OCS path |
| VPOSR | $8000 \| V8 | LOF set, Agnus ID 0 (OCS PAL) |
| DMACONR | (DMACON & $07FF) | BZERO | BBUSY never set (synchronous blitter); BLTPRI and BZERO as FS-UAE shows (W7-M oracle, `host/chipset_ref.py`); the boot counts are the same either way |

Unmapped space is a deliberate divergence (review L7): the plan and the
model return 0 for $DF0000–$DFEFFF (an A500 mirrors the custom registers
there), and only bank $BF decodes CIAs (an A500 also decodes
$A00000–$BEFFFF). AROS touches none of it (`odd_page_hits` = {}).

### 3.5 Feasibility and boot-time estimate

- **Speed.** Measured at 48 MHz (fw 3.15, 38.9 MHz effective): 518–674
  cycles/instr, i.e. 57.8k–75.2k IPS on the MEM/STREAM/RMW/ALU loops
  [MEASURED HW]; those loops never leave their code page. The boot runs in
  exception mode, which W6-S says adds ~60 cycles/instr (its figure, not
  measured here): **~52–68k IPS** before refills [EST]. At 1 MHz ~1.5–1.7k
  [MEASURED HW], ~1.4–1.5k in exception mode [EST].
- **Work.** 3.33 M instructions to the screen [MODEL], plus [EST]: ~275k
  blitter words at ~3.3 µs/word with row-wise bulk DMA (6 B × 0.30 µs +
  ~60 cycles compute) ≈ 1 s at 48 MHz, ~20 s at 1 MHz; ~19k trapped
  accesses, ~0.2 s; 0.15 s guest idle × k (0 with idle skip); a few screen
  conversions. The ~275 VERTBs' work is already in the instruction
  count.
- **Code-window refills (review M1).** The boot changes 256-byte code page
  193,692 times in its first 3,333,413 instructions, **0.0581 per
  instruction** [MODEL]. I re-measured it (`code_page_changes` in
  `boot/boot_report.json`) and got exactly the reviewer's number. It is a
  lower bound: extension words crossing a page are not counted. At 48 MHz
  a refill is a 256-byte REU DMA (~77 µs at 0.30 µs/B) plus ~9 REC stores,
  and the next data call goes cold (~8 more): **~90 µs** [EST], ≈ 3,500
  cycles, i.e. ~+200 cycles/instr averaged (+30–40%). That is **~17 s**
  over the boot. At 1 MHz a refill is ~380 cycles (~75 s in total, +3%).

| Host | Estimate to "insert disk" |
|---|---|
| U64E 48 MHz | **≈ 70–95 s** (3.33M / 52–68k IPS = 49–64 s, + ~17 s refills, + ~2 s; upper end allows for the refill count being a lower bound and the fw-3.14d DMA rate) [EST] |
| 1 MHz (stock C64 speed) | **≈ 40 min** [EST]: correctness vehicle only |
| VICE warp (gate/nightly) | ≈ 5–8 min: the vbcc runs in this wave ran at 7–12k instr/s in warp [MEASURED VICE] |

- 32% of the boot is the checksum loop plus the ROMtag scan: ~1 MB of
  ROM read through the memory API. It costs ~17–20 s at 48 MHz [EST] and cannot be
  avoided without modifying or special-casing the ROM, which this plan
  does not propose.
- **Verdict: feasible at 48 MHz.** It needs the W6-S work (in flight) and
  the S3.1–S3.7 increments below. No MMU, FPU, 68010+ opcode or CIA-B
  timer is needed.

**Guest memory profile.** AROS cannot share `runtime/rc/guest.inc`'s HLE
layout: both ROMs ($E00000–$E7FFFF, $F80000–$FFFFFF) fall inside the HLE
RAMDISK ($E00000–$FFFFFF), and the exec/dos tables at $0F0000 and the heap
at $100000 would shadow AROS chip RAM.

S3.7 therefore adds a second, machine-mode profile to the resource
contract (`rescheck` per profile). Both profiles reserve $BF and $DF as
`io`. The HLE profile's $800000–$DFFFFF `free` region must shrink to exclude
$BF0000–$BFFFFF and $DF0000–$DFFFFF.

### 3.6 Limits of the evidence

- Not cycle-exact: no DMA contention, 1-line granularity, copper WAITs
  ignored, no code window (§3.5 adds it). Beam-racing code would differ;
  AROS's boot does not. The reviewer's `speed_sens.py` scaled guest CPU
  speed vs guest time by 0.5–4×: instructions to the no-media frame moved
  from 3.32M to 3.67M (≤+10%) [MODEL].
- 2 MB of detected chip RAM (not 512 KB) is deliberate: it is the machine
  we would build.
- FS-UAE was not traced; the model runs the same ROM on the planned
  machine and renders the target screen. The W6-O oracle is still the
  right check for the S3.0 reference models.
- The ablation shows what is *necessary* in this model; S3.3–S3.6 test
  that the C64 side matches its semantics.

## 4. Increment plan (after W6-S)

Each step is its own package with a red → green leg (Wave 4 C5). "Model"
means the Python reference classes, which S3.0 promotes out of
`aros_boot_model.py` into `host/` with pytest.

**S3.0: host reference models** (no 6502 work). Promote CIA, Paula,
blitter and copper from `aros_boot_model.py` into `host/chipset_ref.py`
with unit tests; run blitter/CIA test programs through W6-O's FS-UAE
oracle and diff memory dumps against the model. Red: an injected wrong
minterm or one-shot rule fails the diff. Green: all oracle cases match.

**S3.1: trap mechanism + stub chip file + serial capture.** Recommended
next.
- Work: the §1.3 hook in `MIO_PROG`, the cold path, STRADDLE's bank+1
  route and `mio_split`; a new `runtime/ops/chip.s` where writes land in a
  256-word shadow and log; reads return the §3.4 fixed values (SERDATR
  $3000, POTINP $FF00, DENISEID $FFFF, VPOSR $8000; CIA-A PRA inputs $EB
  in the CIA stub), 0 for write-only registers, else the shadow; SERDAT
  appends to a C64-side ring the host reads.
- Red: the host seeds the REU at $DFF180 with $A5A5. A runner program does
  `move.w #$1234,$DFF180` and then `move.w $DFF180,d0`.
  - Today the REU byte pair becomes $1234 and d0 = $1234.
  - Green: the REU still holds $A5A5, the stub's shadow or log holds
    ($180, $1234), and d0 = 0 (COLOR00 is write-only; open bus reads 0).
    A `SERDAT` string shows up in the ring.
- Teeth: the same access as the *first* data call after a window refill
  (cold path); a long at `$DFFFFE` (split path: the $DF half trapped, the
  $E0 half in REU, `m_val` not clobbered by `copy`); `move.l $BEFFFE,d0`
  and `$DEFFFE` (bank carry into $BF/$DF, review M2); a byte access; CIA
  odd/even selection. Four mutants (no cold check, no split check, no
  bank+1 route in STRADDLE, `m_rbk` stored before the trap) must each fail
  one leg.
- Perf gate: `rec_budget` bit-identical; `bank_probe` counts unchanged;
  VICE cycle delta 0 on the rec_budget loops and exactly +8 per switch on
  a bank-alternating loop; one ABBA 48 MHz session for the hardware
  owner, which also measures **the cost of one code-window refill at
  48 MHz** (a loop that jumps between two pages every iteration), the
  number that dominates the AROS estimate (§3.5, ~90 µs assumed).

**S3.2: machine loop, time base, events.** `mach_run`, the CIA #1/#2 time
base, `ev_pend`, the dilation shift *s*, and the RAM IRQ/NMI vectors at
$FFFA–$FFFF with KERNAL banked out (new `ram.inc` claim, §1.5); the IRQ is
enabled only inside `mach_run`. Red: a guest loop polling VHPOSR for a
change times out (nothing advances the beam). Green: it terminates,
VPOSR/VHPOSR are monotonic within a frame, and guest frames/s ≈ 69.4/2^s
(4.3 at *s* = 4) within ±10% in VICE. Teeth: handler disabled → red
returns; a halt racing an event (`st_halt` = $81) still returns the halt;
the Wave-5 suites run unchanged.

**S3.3: Paula interrupts + autovector delivery** (needs W6-S). Red: a
supervisor-mode guest installs a level-3 handler (vector 27) that counts
and acknowledges VERTB, sets INTENA = $C020, drops IPL to 0 and spins; the
counter stays 0. Green: counter > 0, with stacked SR/PC and IPL checked in
the handler. Teeth: IPL = 7 → count stays 0; a missing INTREQ acknowledge
→ re-entry storm detected; hand vectors pin the frame (SR stacked, S = 1,
I2–I0 = level). Hardware-free.

**S3.4: CIA-A/B.** Timers (continuous, one-shot, force load, one-shot
start on the high-byte write, TB counting TA underflows), ICR read-clear
and mask, TOD latch/halt/alarm (CIA-A per vsync, CIA-B per hsync).
Red/green: direct-API sweeps (as `rec_apisweep`) diffed against the S3.0
model, plus a guest copy of AROS's `kernel_timer.c` probe that must report
50 Hz (red: 0 or a hang). Teeth: one mutant per ICR/one-shot rule.

**S3.5: copper + display registers + VIC converter v0.**
- Work: the mc2 and hires-OR kernels from `c2v.s`, the palette LUT, the
  refresh policy.
- Fixture `fixtures/c/pf_static.c`: an HLE program that allocates a 2-plane
  320×200 buffer, draws a known pattern, builds a copper list (BPLxPT,
  COLOR00–03, BPLCON0 $2200, DIW/DDF), and pokes COP1LC, COPJMP1 and
  DMACON.
- Red: the VIC bitmap and colour memory stay empty.
- Green: `$E000` bitmap, screen RAM and `$D800` equal the host reference
  conversion of the guest planes, byte for byte. VICE only; a U64E photo
  check for the hardware owner.
- Teeth: a bit-order mutant in the kernel; a palette-LUT off-by-one.

**S3.6: blitter v0** (synchronous, normal mode + fill; line mode logged).
Red: a guest blit test (copy, cookie-cut `$CA`, clear, descending shifted
copy) leaves the destination unchanged. Green: it matches the S3.0 model
and the FS-UAE oracle dump. Perf: words/s at 1 and 48 MHz vs the §3.5
assumption of ~3.3 µs/word.

**S3.7: AROS machine profile + boot milestones.**
- Work: the ROM staged from W6-O's cache into the REU (never into the
  repo), the reset vectors, `st_exc` = 1, the machine-mode `rc` profile,
  and CIA-A PRA inputs $EB.
- Milestones, as `tests/aros_boot.py` (VICE, nightly, not the gate):
  serial `ROMInfo: 1MiB ROM detected`; `romtaginit done`; the VIC
  conversion of the no-media screen, hashed against the model's frame
  converted by the same kernel on the host.
- Red: each milestone fails before its enabling step. Skips cleanly when
  the ROM cache is absent, like the W6-O oracle.

Dependency order: S3.0 ∥ S3.1 → S3.2 → S3.3 (needs W6-S) → S3.4 → S3.5 →
S3.6 → S3.7. M3's static playfield is done at S3.5. It needs neither
interrupts nor the AROS profile, so S3.5 can move ahead of S3.3–S3.4 if M3
is the priority.

## 5. Risks

1. **Interrupt load.** Undilated, AROS's background work saturates the
   interpreter (§1.5); dilation makes guest delays k× longer (a 3 s dosboot
   retry ≈ 35 s [EST]), mostly recovered by idle skip on STOP.
2. **Code-window refills at turbo** (~90 µs each [EST]) are the largest
   single AROS cost after the instructions themselves (§3.5); S3.1
   measures one.
3. **1 MHz is not viable for AROS** (~40 min [EST]); fine for M3 (~0.4 s
   per static refresh [EST]).
4. **16-colour hires fidelity:** a 1-bit silhouette in v0; v1 costs 2–4×
   [EST].
5. **CIA φ2 at turbo is unverified.** If the U64E clocks the C64 CIAs with
   the turbo clock, calibrate the time base against the VIC raster
   instead; a 10-line probe for the device owner.
6. **Merge contention in `core.s`:** S3.1 follows W6-S's merge and re-runs
   its perf proof on the merged tree.
7. **Model-vs-ROM drift:** pin the ROM hash (W6-O does); re-run the model
   when the pin moves.
8. **Blitter-heavy software** will be far slower than the boot's 275k
   words suggest; out of scope for M3.
9. **REU "chip RAM" is 2 MB:** harmless for AROS, a possible surprise for
   trackloaders that size chip RAM by mirrors.

## 6. Reproduce

```sh
E=build/evidence/w6-s3plan; PY=~/.cache/amiga64/s3probe-venv/bin/python
make runtime BUILD=build/w6-s3plan/rt                                   # Q1 (VICE)
VBCC=~/.local/opt/vbcc python3 tools/s3probe/bank_probe.py --prg build/w6-s3plan/rt/runner.prg --json-out $E/bank_probe.json
python3 tools/s3probe/c2v_measure.py --json-out $E/c2v_measure.json     # Q2 (VICE)
python3 tools/s3probe/c2v_measure.py --mutant                           # must exit 1
python3 tools/s3probe/aros_hwscan.py --md $E/aros_hwscan.md             # Q3
sh tools/s3probe/setup_venv.sh                                          # machine68k + set_irq, ~/.cache
$PY tools/s3probe/aros_boot_model.py --seconds 12 --snap-every 0.5 --out $E/boot --expect $E/boot/insert_disk_frames.txt
$PY tools/s3probe/aros_boot_model.py --seconds 6 --bank-stats --out $E/bankstats
python3 tools/s3probe/boot_summary.py $E/boot/boot_report.json --screen-icount 3333413
sh tools/s3probe/ablate.sh
```

AROS sources, ROM and machine68k/Musashi are used from `~/.cache/amiga64/`
only. The AROS screen PNGs live only under the git-ignored `build/`
(delete before sharing any `build/` archive). No proprietary material.
