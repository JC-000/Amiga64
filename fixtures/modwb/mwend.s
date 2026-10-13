; fixtures/modwb/mwend.s — linked last (W16-X): a nonzero long at the end of
; the merged CODE, DATA and mwchip sections, so vlink stores every byte of them.
; Without it vlink drops a section's trailing zero bytes from the file and
; only declares them in the hunk's size; whether LoadSeg zeroes that tail is
; not something this program relies on (leg S fills it with $A5).

        section CODE,code
        dc.l    'MWND'

        section DATA,data
        dc.l    'MWND'

        section mwchip,data_c
        dc.l    'MWND'
