; An LVO the HLE does not implement halts HALT_ALINE in its own slot.
        movea.l 4.w,a6
        jsr     -384(a6)            ; exec WaitPort
        rts
