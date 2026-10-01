; ---------------------------------------------------------------------------
; runtime/rc/ram.inc — C64 system-RAM / I/O resource contract.  Owner: rc.
;
; SINGLE SOURCE OF TRUTH for every C64 absolute address ($0100-$FFFF) this
; project claims ($00-$FF lives in runtime/rc/zp.inc).  Consumed via
; runtime/macros.inc or included directly (tests/runner/*.s).  Machine-
; checked by host/rescheck.py (`make rescheck`).
;
; Every equate is .ifndef-guarded (relocatable via `ca65 -D name=value`;
; pass 0x-hex through make/shell — see docs/RESOURCES.md for the `$` gotcha).
; Region grammar: docs/RESOURCES.md.  PRG-internal boundaries (STARTUP /
; DISPATCH / CODE...) are ld65-assigned inside MAIN; the split declared
; here matches runtime/c64.cfg + the current link — verify boundary drift
; against build/runner.map.
; ---------------------------------------------------------------------------

; @space ram $0100-$FFFF

; @region sys $0100-$01FF 6502 hardware stack
; @region sys $0200-$03FF KERNAL/BASIC workspace + vectors - not claimable while ROMs may run (loader/boot path)

; @region hle $0400-$07E7 C64 text screen - HLE console output (VIC bank 0)
.ifndef SCREEN
SCREEN     = $0400      ; 40x25 screen RAM; hle.s cursor offset 0-999
.endif

; @region free $07E8-$0800 screen-RAM tail + sprite pointers ($07F8-$07FF) - unused

; --- Runner PRG (MAIN memory area, c64.cfg: $0801 size $97FF) --------------
; @region core $0801-$08FF PRG start: BASIC SYS stub (STARTUP) + link slack before DISPATCH alignment
; @region core $0900-$0AFF DISPATCH jump tables - two page-aligned 256-byte tables (build/gen/dispatch.s)
; @region core $0B00-$9FFF PRG code/rodata/data/bss - CODE onward, incl. page-aligned code_win in BSS

; @region free $A000-$B8FF BASIC ROM / RAM underneath - unclaimed
; W7-V c2v: VIC converter v0 work RAM (RAM under BASIC ROM; runtime/ops/c2v.s, docs/C2V.md)
; @region c2v $B900-$BFFF c2v pair-OR tables, one staged plane row per plane, argument block
.ifndef c2v_pairhi
c2v_pairhi = $B900      ; W7-V c2v: byte -> pair-OR of its 4 pixel pairs, bits 7-4 (page-aligned)
.endif
.ifndef c2v_pairlo
c2v_pairlo = $BA00      ; W7-V c2v: same, bits 3-0 (page-aligned)
.endif
.ifndef c2v_stage
c2v_stage  = $BB00      ; W7-V c2v: 4 page-aligned plane rows ($BB00/$BC00/$BD00/$BE00)
.endif
.ifndef c2v_args
c2v_args   = $BF00      ; W7-V c2v: argument block + converter state (layout: docs/C2V.md)
.endif

; @region run $C000-$C7FF buf_case - harness writes test cases / load records here
.ifndef buf_case
buf_case   = $C000
.endif

; @region run $C800-$CDFF buf_result - runner writes final state here (case results span at most $C800-$C97F)
.ifndef buf_result
buf_result = $C800      ; contract range $C800-$CFFF; $CE00 page carved out below
.endif

; @region mb $CE00-$CEFF harness hardware mailbox page - RESERVED for the U64 hardware harness (in-flight H1 work); runner must not write $CE00-$CEFF
; @region run $CF00-$CFFF buf_result tail - READY_FLAG at $CFFF (runner.s, buf_result+$7FF)

; @region sys $D000-$DEFF VIC/SID/color/CIA I/O block - hardware, not claimable
.ifndef VIC_RASTER
VIC_RASTER  = $D012     ; VIC-II raster line low byte (read: speed-independent time base)
.endif
.ifndef CIA1_PRA
CIA1_PRA    = $DC00     ; CIA 1 port A: keyboard matrix column select (write)
.endif
.ifndef CIA1_PRB
CIA1_PRB    = $DC01     ; CIA 1 port B: keyboard matrix row read
.endif
.ifndef CIA1_DDRA
CIA1_DDRA   = $DC02     ; CIA 1 data direction A
.endif
.ifndef CIA1_DDRB
CIA1_DDRB   = $DC03     ; CIA 1 data direction B
.endif
.ifndef VIC_COLRAM
VIC_COLRAM  = $D800     ; W7-V c2v: VIC colour RAM (low nibbles), written by runtime/ops/c2v.s
.endif
.ifndef CIA2_ICR
CIA2_ICR    = $DD0D     ; W7-V c2v: CIA 2 interrupt control; read = acknowledge (releases /NMI)
.endif
.ifndef VIC_CTRL1
VIC_CTRL1   = $D011     ; W10-V disp: VIC control 1 (bitmap mode, display enable, 25 rows)
.endif
.ifndef VIC_CTRL2
VIC_CTRL2   = $D016     ; W10-V disp: VIC control 2 (multicolour, 40 columns)
.endif
.ifndef VIC_MEMPTR
VIC_MEMPTR  = $D018     ; W10-V disp: VIC screen matrix / bitmap base within the bank
.endif
.ifndef VIC_BORDER
VIC_BORDER  = $D020     ; W10-V disp: VIC border colour (Amiga colour 0)
.endif
.ifndef VIC_BG0
VIC_BG0     = $D021     ; W10-V disp: VIC background colour 0 (Amiga colour 0)
.endif
.ifndef CIA2_PRA
CIA2_PRA    = $DD00     ; W10-V disp: CIA 2 port A; bits 1-0 = NOT VIC bank (disp.s sets bank 3, other bits kept)
.endif
.ifndef CIA1_TALO
CIA1_TALO   = $DC04     ; W8-L mach: CIA 1 timer A latch low (the event one-shot)
.endif
.ifndef CIA1_TAHI
CIA1_TAHI   = $DC05     ; W8-L mach: CIA 1 timer A latch high
.endif
.ifndef CIA1_ICR
CIA1_ICR    = $DC0D     ; W8-L mach: CIA 1 interrupt control; read = acknowledge (releases /IRQ)
.endif
.ifndef CIA1_CRA
CIA1_CRA    = $DC0E     ; W8-L mach: CIA 1 control A (bits 7-6 TODIN/SPMODE kept)
.endif
.ifndef CIA2_TALO
CIA2_TALO   = $DD04     ; W8-L mach: CIA 2 timer A (time base, bits 15-0, counts down)
.endif
.ifndef CIA2_TAHI
CIA2_TAHI   = $DD05     ; W8-L mach
.endif
.ifndef CIA2_TBLO
CIA2_TBLO   = $DD06     ; W8-L mach: CIA 2 timer B (time base, bits 31-16: counts TA underflows)
.endif
.ifndef CIA2_TBHI
CIA2_TBHI   = $DD07     ; W8-L mach
.endif
.ifndef CIA2_CRA
CIA2_CRA    = $DD0E     ; W8-L mach: CIA 2 control A (bits 7-6 kept)
.endif
.ifndef CIA2_CRB
CIA2_CRB    = $DD0F     ; W8-L mach: CIA 2 control B (bit 7 kept)
.endif
; W7-V c2v: the RAM under this I/O window is a separate claimable resource (host/rescheck.py `@under`)
; @under c2v $D000-$D3FF c2v VIC screen matrix + sprite pointers in the RAM under VIC/SID I/O (bank 3 + $1000); CPU writes with $01=$34 under sei only, never REU DMA
.ifndef c2v_screen
c2v_screen = $D000      ; W7-V c2v: 40x25 screen matrix
.endif
; @region sys $DF00-$DF0A REU RAM-Expansion-Controller registers
.ifndef REC_STATUS
REC_STATUS  = $DF00
.endif
.ifndef REC_COMMAND
REC_COMMAND = $DF01
.endif
.ifndef REC_C64LO
REC_C64LO   = $DF02
.endif
.ifndef REC_C64HI
REC_C64HI   = $DF03
.endif
.ifndef REC_REULO
REC_REULO   = $DF04
.endif
.ifndef REC_REUHI
REC_REUHI   = $DF05
.endif
.ifndef REC_REUBANK
REC_REUBANK = $DF06
.endif
.ifndef REC_LENLO
REC_LENLO   = $DF07
.endif
.ifndef REC_LENHI
REC_LENHI   = $DF08
.endif
.ifndef REC_IMASK
REC_IMASK   = $DF09
.endif
.ifndef REC_ACTRL
REC_ACTRL   = $DF0A
.endif

; @region free $DF0B-$DF1B expansion I/O gap - unclaimed
; @region sys $DF1C-$DF1F Ultimate 64/II+ command interface registers (M0 loader / hardware harness)
.ifndef ULT_CMD_BASE
ULT_CMD_BASE = $DF1C    ; control/command/response/status window
.endif
; @region free $DF20-$DFFF expansion I/O - unclaimed

; W7-V c2v: VIC bank 3 output (RAM under KERNAL; writes need no banking, the VIC always reads RAM)
; @region c2v $E000-$FF3F c2v VIC hires/multicolour bitmap, 25 char rows (200 lines) in char-cell order (bank + $2000)
.ifndef c2v_bitmap
c2v_bitmap = $E000      ; W7-V c2v: VIC bank 3 bitmap
.endif
; @region free $FF40-$FFF9 KERNAL ROM / RAM underneath - unclaimed
; W8-L mach: the 6502 hardware vectors in the RAM under the KERNAL (mach_run runs with $01 = $35)
; @region s3 $FFFA-$FFFF 6502 NMI/RESET/IRQ vectors in RAM under KERNAL
.ifndef mach_vnmi
mach_vnmi = $FFFA       ; W8-L mach: NMI -> mach_nmi.  Sanctioned writers: mach_vec, and c2v_fill (stores mach_nmi: identical bytes inside mach_run, ROM vectors live outside)
.endif
.ifndef mach_vrst
mach_vrst = $FFFC       ; W8-L mach: RESET, never written (a reset maps the KERNAL in)
.endif
.ifndef mach_virq
mach_virq = $FFFE       ; W8-L mach: IRQ/BRK -> mach_irq
.endif

; --- Contract invariants (assemble-time; keep true under -D relocation) ----
.assert (buf_case & $FF) = 0,   error, "buf_case must be page-aligned (c64.cfg CASEBUF)"
.assert (buf_result & $FF) = 0, error, "buf_result must be page-aligned (c64.cfg RESBUF)"
.assert buf_result = buf_case + $800, error, "host harness assumes the 2KB result buffer directly follows the 2KB case buffer"
