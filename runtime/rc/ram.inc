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

; @region free $A000-$BFFF BASIC ROM / RAM underneath - unclaimed

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

; @region free $E000-$FFFF KERNAL ROM / RAM underneath - unclaimed

; --- Contract invariants (assemble-time; keep true under -D relocation) ----
.assert (buf_case & $FF) = 0,   error, "buf_case must be page-aligned (c64.cfg CASEBUF)"
.assert (buf_result & $FF) = 0, error, "buf_result must be page-aligned (c64.cfg RESBUF)"
.assert buf_result = buf_case + $800, error, "host harness assumes the 2KB result buffer directly follows the 2KB case buffer"
