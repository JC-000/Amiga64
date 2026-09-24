; ===========================================================================
; runtime/ops/hle_dos.s — M2: dos.library file I/O (Open/Close/Read/Write
; on file handles, backed by Ultimate DOS on hardware).
; Owner: WS-B2 (m2-dos).  Contract: docs/WAVE4_BRIEF.md C2/C3, docs/HLE.md.
;
; SUPERVISOR STUB — every entry halts HALT_ALINE until WS-B2 lands.
;
; Entry points (all entered by jmp, same entry state as an hle.s handler:
; opcode word in op_lo/op_hi, return address on the guest stack; each
; retires by emulating RTS or halting):
;   hle_dos_dispatch — HLE index $30-$4F in op_lo (routed by hle.s)
;   hle_dos_read     — dos.library/Read  on a FileHandle with fh_Args >= FH_KIND_FILE0
;   hle_dos_write    — dos.library/Write on a FileHandle with fh_Args >= FH_KIND_FILE0
; ===========================================================================

.include "../macros.inc"

.export hle_dos_dispatch, hle_dos_read, hle_dos_write

.import emu_halt

.segment "CODE"

hle_dos_dispatch:
hle_dos_read:
hle_dos_write:
        lda #HALT_ALINE
        jmp emu_halt
