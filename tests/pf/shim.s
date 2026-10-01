; tests/pf/shim.s — W10-V test shim, linked ONLY into tests/run_pf.py's
; shim builds (never into runtime/).  Until W10-P's rc claims exist it
; stands in for the display-state block and the hook vector
; (docs/WAVE10_BRIEF.md C1, C3); afterwards it only adds shim_kick.
;   disp_st    the C1 block (103 bytes, host-filled through runner.lbl)
;   cop_dhook  the RAM vector disp_init fills (high byte 0 = none)
;   shim_kick  a fake cop_frame end: calls through cop_dhook if set
.include "../../runtime/macros.inc"

.export shim_kick

.ifndef disp_st                 ; before W10-P lands (it brings both)
.export disp_st, cop_dhook
.segment "BSS"
disp_st:   .res 104             ; C1 + valid (+103)
cop_dhook: .res 2
.else
.import cop_dhook
.endif

.segment "CODE"
shim_kick:
        lda cop_dhook+1
        beq @r
        sta @j+2
        lda cop_dhook
        sta @j+1
@j:     jmp $0000               ; operand patched
@r:     rts
