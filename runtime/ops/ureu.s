; ===========================================================================
; runtime/ops/ureu.s — W12-T UCI REU I/O: load a file into a REU range and
; save a REU range to a file through the Ultimate Command Interface's DOS
; target.  Owner: W12-T (w12-reu).  Contract: docs/CONTRACTS.md "UCI REU
; I/O".  Host model: host/tests/test_snap_ureu.py.
;
; Firmware (1541ultimate software/filemanager, 1541u-315preview @871ad034;
; the brief relays dos.h/dos.cc as identical in the C64U 1.1.0 build):
;  - dos.h:29-30   DOS_CMD_LOAD_REU = $21, DOS_CMD_SAVE_REU = $22.
;  - dos.cc:254-286 LOAD_REU / 287-319 SAVE_REU: [t][cmd][addr u32 LE]
;    [len u32 LE] (message bytes 2-5 / 6-9); no open file -> "85,NO FILE
;    OPEN"; addr &= $FFFFFF, len &= $1FFFFFF, addr + len > 16 MB -> len
;    clamped + status "02,REQUEST TRUNCATED"; file->read/write at the
;    file's CURRENT position (consecutive commands continue the file);
;    reply (one block, last_part) "$%6x BYTES LOADED TO REU $%6x" /
;    "... SAVED FROM REU ..." with the TRANSFERRED count (a short file
;    still answers "00,OK"); a FatFS error replaces the status.  The
;    firmware's sprintf is software/system/small_printf.cc: "%6x" is
;    exactly 6 upper-case digits, zero-filled ("$040000"; see ureu_chk).
;  - dos.cc:16 the truncation status; dos.cc:111-134 OPEN [t][02][flags]
;    [name] / CLOSE [t][03] (dosio_uci.s, same transaction primitives).
; The count, not the status, is the truth: every command must answer
; "00" AND exactly the length asked for, else the transfer fails.
;
; Chunking: one command moves at most UREU_CHUNK bytes, so a reply always
; comes inside dosio_uci.s's poll budget (65536 fenced polls: ~8 s at
; 64 MHz, ~6 min at 1 MHz) without an abort mid-transfer.  The C64 waits
; for each reply: the guest is paused for the whole transfer.
;
; Entries:
;   MAIN (JSR from anywhere with I/O in; BASIC banked out around the call,
;   the caller's $01 restored; C=1 + ureu_err on failure):
;     ureu_ld   open dosio_name (FA_READ), LOAD_REU ureu_ra/ureu_rl, close
;     ureu_sv   open dosio_name (FA_WRITE|FA_CREATE_ALWAYS), SAVE_REU, close
;     ureu_bank A/X = an SCODE routine: call it with BASIC banked out
;   SCODE (BASIC out; snap.s): ureu_open (A = open flags), ureu_xfer
;     (A = $21/$22: ureu_ra/ureu_rl, adds to ureu_cnt), ureu_close.
; Names: dosio_name / dosio_nlen (hle_dos.s), sent verbatim to DOS target
; 1, relative to its current path (absolute paths work: "/SD/amiga64/x").
; dosio_uci_init runs first: handshake reset, both targets closed.
; ===========================================================================

.include "../macros.inc"

.export ureu_ld, ureu_sv, ureu_bank, ureu_bend
.export ureu_open, ureu_xfer, ureu_close
.export ureu_ra, ureu_rl, ureu_err, ureu_us
.exportzp UREU_LOAD, UREU_SAVE, UREU_FRD, UREU_FWR
.import uci_begin, uci_put, uci_exec, uci_ok, dosio_uci_init
.import dosio_uci_max, dosio_uci_rn, dosio_uci_st
.import dosio_buf, dosio_name, dosio_nlen
.import code_invall             ; W12-C (core.s): A1, after a load
UREU_PIX   = 105                ; @value disp_st's pixel epoch offset (CONTRACTS)

UREU_TGT   = 1                  ; DOS target 1 (dos.cc:11)
UREU_LOAD  = $21                ; dos.h:29
UREU_SAVE  = $22                ; dos.h:30
UREU_COPEN = $02                ; @value dos.h DOS_CMD_OPEN_FILE
UREU_CCLOSE = $03                ; @value dos.h DOS_CMD_CLOSE_FILE
UREU_FRD   = $01                ; FA_READ (fs_errors_flags.h:44)
UREU_FWR   = $0A                ; FA_WRITE | FA_CREATE_ALWAYS
UREU_RMAX  = 40                 ; reply bytes kept (the text is 35/36)
.ifndef UREU_CHUNK_HI
UREU_CHUNK_HI = $04             ; @value chunk = UREU_CHUNK_HI * 64 KB (256 KB)
.endif
.assert UREU_CHUNK_HI >= 1 && UREU_CHUNK_HI <= $7F, error, "chunk 64 KB .. 8 MB"

; ureu_err codes
UREU_E_OPEN  = 1                ; @value open refused / no reply
UREU_E_LOST  = 2                ; @value a transfer command got no reply
UREU_E_STAT  = 3                ; @value status not "00" (ureu_us holds it)
UREU_E_SHORT = 5                ; @value the reply is not "$<count asked for> "
UREU_E_CLOSE = 6                ; @value close refused (a save may be lost)

.assert (ureu_jv & $FF) <> $FF, lderror, "jmp (ureu_jv) must not straddle a page (6502 bug)"

.segment "BSS"
ureu_ra:  .res 3                ; REU address (u24 LE), advanced per chunk
ureu_rl:  .res 4                ; bytes left (u32 LE, <= $1000000)
ureu_err: .res 1                ; 0 or UREU_E_*
ureu_us:  .res 2                ; first two status bytes of the failing reply
ureu_jv:  .res 2                ; ureu_bank's target
ureu_bend:                      ; L6: the end of ALL MAIN BSS (host/snapfmt.py --stamp checks it)

.segment "SBSS"
ureu_cmd: .res 1
; Scratch in dosio_buf's tail: uci_exec keeps at most UREU_RMAX reply bytes
; at its start, so +$F0.. survives a transaction (ureu is never used while
; an HLE dos.library file is open: machine mode has none).
ureu_pl  = dosio_buf + $F0      ; command payload: addr u32, len u32 (LE)
ureu_n   = ureu_pl + 4          ; this chunk's length (u24; +3 = 0)
ureu_acc = dosio_buf + $F8      ; ureu_chk's shift register (u24)
.assert UREU_RMAX <= $F0, error, "the reply must not reach ureu_pl"

.segment "LOWCODE"              ; Wave 13 (supervisor): the STARTUP->DISPATCH slack

; ureu_bank — A/X = SCODE routine; BASIC out ($37 -> $36) around it.  C
; and A from the routine are returned; X/Y clobbered.
ureu_bank:
        sta ureu_jv
        stx ureu_jv+1
        lda CPU_PORT
        pha
        and #3
        cmp #3
        bne :+
        dec CPU_PORT            ; $37 -> $36 ($36/$35/$34 left as they are)
:       jsr @go
        tax
        pla                     ; pla/sta keep C
        sta CPU_PORT
        txa
        rts
@go:    jmp (ureu_jv)

ureu_ld:
        lda #<ureu_ldh
        ldx #>ureu_ldh
        jmp ureu_bank

ureu_sv:
        lda #<ureu_svh
        ldx #>ureu_svh
        jmp ureu_bank

.segment "SCODE"

ureu_ldh:
        lda #UREU_FRD
        ldx #UREU_LOAD
        jsr ureu_one
        php                     ; A1: whatever landed (a failure may be partial)
        jsr code_invall         ; guest code may have changed (W12-C, I1)
        inc disp_st + UREU_PIX  ; guest pixels may have changed (I2's epoch)
        plp
        rts
ureu_svh:
        lda #UREU_FWR
        ldx #UREU_SAVE
ureu_one:                       ; open (A flags) / xfer (X command) / close
        stx ureu_cmd
        ldx #0
        stx ureu_err
        jsr ureu_open
        bcs @r
        lda ureu_cmd
        jsr ureu_xfer
        php
        jsr ureu_close          ; always (a save is flushed at close)
        plp
        bcs @r                  ; the transfer's error wins
        lda ureu_err
        cmp #1                  ; C = 1 if the close failed
@r:     rts

; ureu_open — A = open flags; dosio_name/dosio_nlen.  C=1: UREU_E_OPEN.
ureu_open:
        pha
        jsr dosio_uci_init      ; handshake reset, targets 1/2 closed
        jsr ureu_nomax
        lda #UREU_TGT
        jsr uci_begin
        bcs @fp
        lda #UREU_COPEN
        jsr uci_put
        pla
        jsr uci_put
        ldy #0
@nm:    cpy dosio_nlen
        beq @ex
        lda dosio_name,y
        jsr uci_put             ; keeps Y
        iny
        bne @nm
@ex:    jsr uci_exec
        bcs @f
        jsr uci_ok
        bcc @r
@f:     lda #UREU_E_OPEN
        sta ureu_err
        sec
@r:     rts
@fp:    pla
        jmp @f

; ureu_close — C=1: UREU_E_CLOSE (ureu_err set only if it was 0).
ureu_close:
        jsr ureu_nomax
        lda #UREU_TGT
        jsr uci_begin
        bcs @f
        lda #UREU_CCLOSE
        jsr uci_put
        jsr uci_exec
        bcs @f
        jsr uci_ok
        bcc @r
@f:     lda ureu_err
        bne :+
        lda #UREU_E_CLOSE
        sta ureu_err
:       sec
@r:     rts

ureu_nomax:
        lda #0
        sta dosio_uci_max
        sta dosio_uci_max+1
        rts

ureu_keepst:
        lda dosio_uci_st
        sta ureu_us
        lda dosio_uci_st+1
        sta ureu_us+1
        rts

; ureu_xfer — A = UREU_LOAD / UREU_SAVE on the open file: ureu_rl bytes at
; REU ureu_ra, in chunks; ureu_cnt += every confirmed chunk.  C=1 +
; ureu_err on the first failing chunk (ureu_ra/ureu_rl then point at it).
; Payload per chunk (dos.cc:261-266): addr u32 LE, len u32 LE = ureu_pl.
ureu_xfer:
        sta ureu_cmd
@chunk: lda #0
        sta ureu_pl+3           ; addr bits 31-24, len bits 31-24: 0
        sta ureu_pl+7
        lda ureu_rl+3           ; n := min(rl, chunk) (a chunk fits 24 bits)
        bne @big
        lda ureu_rl+2
        cmp #UREU_CHUNK_HI
        bcs @big
        ora ureu_rl+1
        ora ureu_rl
        bne :+
        clc                     ; rl = 0: done
        rts
:       ldx #2
@cp:    lda ureu_rl,x
        sta ureu_n,x
        dex
        bpl @cp
        bmi @go                 ; always
@big:   lda #0
        sta ureu_n
        sta ureu_n+1
        lda #UREU_CHUNK_HI
        sta ureu_n+2
@go:    ldx #2
:       lda ureu_ra,x
        sta ureu_pl,x
        dex
        bpl :-
        lda #UREU_RMAX
        sta dosio_uci_max
        lda #0
        sta dosio_uci_max+1
        lda #UREU_TGT
        jsr uci_begin
        bcs @lost
        lda ureu_cmd
        jsr uci_put
        ldx #0
@pl:    lda ureu_pl,x
        jsr uci_put             ; keeps X (uci_fence saves it)
        inx
        cpx #8
        bne @pl
        jsr uci_exec
        bcs @lost
        jsr uci_ok
        bcc :+
        jsr ureu_keepst
        lda #UREU_E_STAT
        bne @err                ; always
:       jsr ureu_chk            ; the count must be the length asked for
        lda #UREU_E_SHORT
        bcs @err
        clc                     ; ra += n (u24)
        ldx #$FD                ; X = $FD..$FF: bytes 0..2 (inx keeps C)
@ra:    lda ureu_ra - $FD,x
        adc ureu_n - $FD,x
        sta ureu_ra - $FD,x
        inx
        bne @ra
        sec                     ; rl -= n (u32; ureu_n+3 = 0)
        ldx #$FC
@sub:   lda ureu_rl - $FC,x
        sbc ureu_n - $FC,x
        sta ureu_rl - $FC,x
        inx
        bne @sub
        jmp @chunk
@lost:  lda #UREU_E_LOST
@err:   sta ureu_err
        sec
        rts

.segment "LOWCODE"              ; (MAIN low slack: SNAPHI is full; runs with BASIC in or out)

; ureu_chk — the reply must begin "$" + the chunk length as EXACTLY 6
; upper-case hex digits, zero-filled + " " (dos.cc:276/309 "$%6x ...",
; formatted by the firmware's own sprintf, software/system/small_printf.cc:
; '%x'/'%X' -> _hex (178-184) writes exactly `width` digits from hexchar[]
; = "0123456789ABCDEF" (11, 54-63): zero-filled, upper case, truncated to
; the width; a chunk is < 2^24, so 6 digits always hold it).  Checked from
; the last digit up against ureu_n.  C=1: a short count, any other count,
; or not that text (UREU_E_SHORT covers all three).
ureu_chk:
        lda dosio_uci_rn+1
        bne :+
        lda dosio_uci_rn
        cmp #8
        bcc @bad
:       lda dosio_buf
        cmp #'$'
        bne @bad
        lda dosio_buf+7
        cmp #' '
        bne @bad
        ldx #2
@c:     lda ureu_n,x
        sta ureu_acc,x
        dex
        bpl @c
        ldx #6
@d:     lda ureu_acc
        and #$0F
        cmp #10
        bcc :+
        adc #'A' - '0' - 10 - 1 ; C = 1 here
:       adc #'0'                ; C = 0 here
        cmp dosio_buf,x
        bne @bad
        ldy #4
@s:     lsr ureu_acc+2
        ror ureu_acc+1
        ror ureu_acc
        dey
        bne @s
        dex
        bne @d
        clc
        rts
@bad:   sec
        rts
