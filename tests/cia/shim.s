; ===========================================================================
; tests/cia/shim.s — host-set guest time for run_cia's "host-time" PRG
; (tests/run_cia.py --build-shim), never linked into runtime/.
;
; That PRG assembles runtime/ops/cia.s with CIA_HOSTTIME defined: its
; getnow reads shim_t (u32 LE, written by the host) instead of calling
; mach_etime.  Everything else is the real event seam (mach_arm/disarm/
; post/hold, mach_am/mach_dl, chip.s's INTREQ path).  It exists for what
; the live counter cannot do in a test: put guest E time anywhere (the u32
; wrap, 2^28-tick jumps) and step it exactly.
; ===========================================================================

.export shim_t

.segment "BSS"
shim_t:    .res 4
