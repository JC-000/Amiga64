; tests/pf/shim.s — W10-V test shim, linked ONLY into tests/run_pf.py's
; shim builds (never into runtime/): a fake caller of the display hook
; (docs/WAVE10_BRIEF.md C3), so the host can drive disp_frame with a
; host-filled disp_st.  disp_st (rc) and cop_dhook (cop.s) are W10-P's.
;   shim_kick  a fake cop_frame end: calls through cop_dhook if set
.include "../../runtime/macros.inc"

.export shim_kick

.import cop_dhook

.segment "CODE"
shim_kick:
        lda cop_dhook+1
        beq @r
        sta @j+2
        lda cop_dhook
        sta @j+1
@j:     jmp $0000               ; operand patched
@r:     rts
