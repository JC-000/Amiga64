; ===========================================================================
; runtime/ops/dosio_uci.s — WS-B2 ULTDOS backend: dos.library files on the
; Ultimate's file system through the Ultimate Command Interface (UCI).
; Owner: WS-B2 (m2-dos).  Docs: docs/HLE_DOS.md "ULTDOS backend".
;
; HARDWARE STATUS: UNVALIDATED.  Written against the firmware source and
; pinned byte-for-byte by host/tests/test_hle_dos_uci.py (6502 emulator +
; firmware model); never run on a device yet.
;
; Sources followed (exact):
;  - Register protocol: 1541ultimate fpga/io/command_interface/vhdl_source/
;    command_protocol.vhd (@880de4dd) — status bits 54-61, read mux 97-106
;    ($DF1D reads $C9, $49 with IRQ pending), control writes 148-170 (PUSH
;    bit 0 also latches bits 7-5 -> write exactly $01; DATA_ACC bit 1;
;    ABORT bit 2; bit 3 clears ERROR), queue pointers 176-185.
;  - Reply timing: software/io/command_interface/command_intf.cc 141-191 —
;    bit 0 clears (HANDSHAKE_ACCEPT_COMMAND) BEFORE copy_result fills the
;    queues, so we wait for state bit 5 (or ERROR), not for bit 0.  Same
;    rule as c64-test-harness uci_network.py 125-133 (issue #486).
;  - DOS target: software/filemanager/dos.h 11-38 (command codes), dos.cc
;    12-13 (targets 1 and 2, ONE open file each), 111-153 (OPEN [t][02]
;    [flags][name], CLOSE [t][03], SEEK [t][06][u32 LE]), 154-182
;    (FILE_INFO [t][07] -> u32 LE size first), 467-492 (READ_DATA
;    [t][04][u16 LE], WRITE_DATA [t][05][x][x][payload]), 784-802 (reads
;    arrive in <= 512-byte blocks; success status is EMPTY).
;  - Open flags: software/filesystem/fs_errors_flags.h 44-53; semantics
;    software/filemanager/filemanager.cc fopen_impl 668-689.
;  - 6502 transaction shape (hardware-tested): c64-https src/net/uci/
;    uci_cmd.s (drain_resp -> drain_status -> single DATA_ACC, header 27-29;
;    abort 69; push_wait 289) and uci_regs.inc 16-54 (bits), 93-109 (turbo
;    fence constants, tuned at 64 MHz).
;
; AmigaOS -> UCI mapping:
;   MODE_OLDFILE   -> FA_READ|FA_WRITE ($03); on failure retry FA_READ ($01)
;                     (read-only media).  Never creates.
;   MODE_NEWFILE   -> FA_READ|FA_WRITE|FA_CREATE_ALWAYS ($0B) create/truncate
;   MODE_READWRITE -> FA_READ|FA_WRITE|FA_OPEN_ALWAYS   ($13) create if missing
;   Open ok -> FILE_INFO for the size (cached per slot; kept current by
;   hle_dos.s via setsize).  At most 2 files open (DOS targets 1, 2); a
;   third Open fails ERROR_NO_FREE_STORE.  Names are sent verbatim,
;   relative to the target's current path (firmware default "/").
;
; Backend contract: see hle_dos.s.  X = dosio_slot on entry; C=1 + dosio_err
; on failure.  This file imports ONLY the dosio_* parameter block, so the
; host test can assemble it standalone.
; ===========================================================================

.include "../macros.inc"

.export dosio_uci_detect, dosio_uci_init, dosio_uci_open, dosio_uci_close
.export dosio_uci_size, dosio_uci_seek, dosio_uci_rdchunk, dosio_uci_wrchunk
.export dosio_uci_wcheck, dosio_uci_setsize

.import dosio_buf, dosio_name, dosio_nlen, dosio_mode, dosio_slot
.import dosio_len, dosio_arg, dosio_err

; --- registers ($DF1C-$DF1F, rc/ram.inc ULT_CMD_BASE) -----------------------
UCI_CTL = ULT_CMD_BASE + 0      ; read: status   write: control
UCI_CMD = ULT_CMD_BASE + 1      ; read: id $C9   write: command byte
UCI_RSP = ULT_CMD_BASE + 2      ; response data queue
UCI_STA = ULT_CMD_BASE + 3      ; status data queue

ST_DATA_AV = $80
ST_STAT_AV = $40
ST_STATE   = $30
ST_REPLY   = $20                ; state bit 5: reply valid (10 last / 11 more)
ST_MORE    = $10
ST_ERROR   = $08
ST_ABORT   = $04
ST_BUSY    = $01
CTL_PUSH   = $01                ; written as exactly $01
CTL_ACC    = $02
CTL_ABORT  = $04
CTL_CLRERR = $08
UCI_ID     = $49                ; $C9 / $49 with bit 7 masked

; --- DOS target ---------------------------------------------------------------
DOS_CMD_OPEN_FILE  = $02
DOS_CMD_CLOSE_FILE = $03
DOS_CMD_READ_DATA  = $04
DOS_CMD_WRITE_DATA = $05
DOS_CMD_FILE_SEEK  = $06
DOS_CMD_FILE_INFO  = $07
FA_READ          = $01
FA_WRITE         = $02
FA_CREATE_ALWAYS = $08
FA_OPEN_ALWAYS   = $10

ERROR_NO_FREE_STORE    = 103
ERROR_OBJECT_NOT_FOUND = 205
ERROR_SEEK_ERROR       = 219
ERROR_DISK_FULL        = 221

FH_SLOTS = 8                    ; must match hle_dos.s

; Turbo fence between UCI register accesses (c64-https uci_regs.inc 93-109).
.ifndef DOSIO_UCI_FENCE_OUTER
DOSIO_UCI_FENCE_OUTER = 5
.endif
.ifndef DOSIO_UCI_FENCE_INNER
DOSIO_UCI_FENCE_INNER = 217
.endif

.segment "BSS"
dosio_uci_tgt:  .res FH_SLOTS   ; per slot: DOS target 1/2
dosio_uci_s0:   .res FH_SLOTS   ; per slot: cached file size (32-bit LE)
dosio_uci_s1:   .res FH_SLOTS
dosio_uci_s2:   .res FH_SLOTS
dosio_uci_s3:   .res FH_SLOTS
dosio_uci_busy: .res 3          ; [1],[2]: target has an open file
dosio_uci_t:    .res 1          ; current target
dosio_uci_to:   .res 2          ; poll budget
dosio_uci_max:  .res 2          ; response bytes to keep in dosio_buf
dosio_uci_rn:   .res 2          ; response bytes received
dosio_uci_st:   .res 2          ; first two status bytes
dosio_uci_sn:   .res 1          ; status bytes received (saturating)
dosio_uci_i:    .res 1

.segment "CODE"

; ===========================================================================
; Low level
; ===========================================================================
uci_fence:                      ; preserves A, X, Y
        pha
        txa
        pha
        ldx #DOSIO_UCI_FENCE_OUTER
@o:     lda #DOSIO_UCI_FENCE_INNER
@i:     sec
        sbc #1
        bne @i
        dex
        bne @o
        pla
        tax
        pla
        rts

uci_status:                     ; A = status register (flags from A)
        lda UCI_CTL
        jsr uci_fence
        cmp #0
        rts

uci_to_reset:
        lda #0
        sta dosio_uci_to
        sta dosio_uci_to+1
        rts

uci_to_tick:                    ; Z=1 when the poll budget is exhausted
        inc dosio_uci_to
        bne @r
        inc dosio_uci_to+1
@r:     rts

; abort + wait for the firmware to reset the handshake (state 00, abort 0)
uci_abort:
        lda #CTL_ABORT
        sta UCI_CTL
        jsr uci_fence
        jsr uci_to_reset
@w:     jsr uci_status
        and #ST_ABORT | ST_STATE | ST_BUSY
        beq @done
        jsr uci_to_tick
        bne @w
@done:  lda #CTL_CLRERR
        sta UCI_CTL
        jmp uci_fence

; A = target: wait for idle (abort on timeout), send the target byte.
; C=1 if the interface never became idle.
uci_begin:
        sta dosio_uci_t
        jsr uci_to_reset
@w:     jsr uci_status
        and #ST_STATE | ST_BUSY
        beq @go
        jsr uci_to_tick
        bne @w
        jsr uci_abort
        jsr uci_status
        and #ST_STATE | ST_BUSY
        beq @go
        sec
        rts
@go:    lda dosio_uci_t
        ; fall through
uci_put:                        ; A = command byte
        sta UCI_CMD
        jsr uci_fence
        clc
        rts

; Push the command and collect the reply: response bytes into dosio_buf
; (first dosio_uci_max kept, dosio_uci_rn counts all), first two status
; bytes into dosio_uci_st, one DATA_ACC per block.  C=1: timeout / ERROR.
uci_exec:
        lda #0
        sta dosio_uci_rn
        sta dosio_uci_rn+1
        sta dosio_uci_sn
        sta dosio_uci_st
        sta dosio_uci_st+1
        lda #CTL_PUSH
        sta UCI_CTL
        jsr uci_fence
@wait:  jsr uci_to_reset        ; wait for reply valid (bit 5) or ERROR
@wl:    jsr uci_status
        and #ST_REPLY | ST_ERROR
        bne @ready
        jsr uci_to_tick
        bne @wl
        jsr uci_abort           ; firmware never answered
        sec
        rts
@ready: and #ST_ERROR
        beq @data
        lda #CTL_CLRERR         ; push refused (interface was busy)
        sta UCI_CTL
        jsr uci_fence
        sec
        rts
@data:  jsr uci_status          ; drain this block's response bytes
        and #ST_DATA_AV
        beq @blkend
        lda UCI_RSP
        jsr uci_fence
        ldy dosio_uci_rn+1      ; keep while rn < max
        cpy dosio_uci_max+1
        bcc @keep
        bne @cnt
        ldy dosio_uci_rn
        cpy dosio_uci_max
        bcs @cnt
@keep:  ldy dosio_uci_rn
        sta dosio_buf,y         ; max <= 256, so rn < 256 here
@cnt:   inc dosio_uci_rn
        bne @data
        inc dosio_uci_rn+1
        jmp @data
@blkend:
        jsr uci_status          ; state 11 = more blocks follow
        and #ST_STATE
        cmp #ST_REPLY | ST_MORE
        bne @stat
        lda #CTL_ACC            ; ask for the next block
        sta UCI_CTL
        jsr uci_fence
        jmp @wait
@stat:  jsr uci_status          ; drain status (keep the first two bytes)
        and #ST_STAT_AV
        beq @ack
        lda UCI_STA
        jsr uci_fence
        ldy dosio_uci_sn
        cpy #2
        bcs @sk
        sta dosio_uci_st,y
@sk:    cpy #$FF
        beq @stat
        inc dosio_uci_sn
        jmp @stat
@ack:   lda #CTL_ACC            ; single DATA_ACC ends the transaction
        sta UCI_CTL
        jsr uci_fence
        clc
        rts

uci_ok:                         ; C=0 iff the status line starts "00"
        lda dosio_uci_sn
        cmp #2
        bcc @no
        lda dosio_uci_st
        cmp #'0'
        bne @no
        lda dosio_uci_st+1
        cmp #'0'
        bne @no
        clc
        rts
@no:    sec
        rts

uci_nomax:
        lda #0
        sta dosio_uci_max
        sta dosio_uci_max+1
        rts

; A = command code: begin(target of slot X) + put(code).  C=1 on timeout.
uci_cmd_slot:
        pha
        lda dosio_uci_tgt,x
        jsr uci_begin
        pla
        bcs @r
        jmp uci_put
@r:     rts

; ===========================================================================
; Backend entry points
; ===========================================================================

; C=1 when the command interface identifies itself at $DF1D.
dosio_uci_detect:
        lda UCI_CMD
        and #$7F
        cmp #UCI_ID
        beq @yes
        clc
        rts
@yes:   sec
        rts

; Reset the handshake and close whatever a previous run left open.
dosio_uci_init:
        lda #0
        sta dosio_uci_busy+1
        sta dosio_uci_busy+2
        jsr uci_abort
        jsr uci_nomax
        lda #1
@t:     sta dosio_uci_i
        jsr uci_begin
        bcs @n
        lda #DOS_CMD_CLOSE_FILE
        jsr uci_put
        jsr uci_exec            ; "84,NO FILE TO CLOSE" is fine
@n:     lda dosio_uci_i
        clc
        adc #1
        cmp #3
        bne @t
        clc
        rts

dosio_uci_open:
        ldx dosio_slot          ; X = slot (also when called directly)
        lda #1                  ; free DOS target?
        ldy dosio_uci_busy+1
        beq @tgt
        lda #2
        ldy dosio_uci_busy+2
        beq @tgt
        lda #ERROR_NO_FREE_STORE
        sta dosio_err
        sec
        rts
@tgt:   sta dosio_uci_tgt,x
        ldy dosio_mode
        lda uci_mode_flags,y
        jsr uci_open_try
        bcc @opened
        ldy dosio_mode          ; MODE_OLDFILE: retry read-only
        bne @nf
        lda #FA_READ
        jsr uci_open_try
        bcc @opened
@nf:    lda #ERROR_OBJECT_NOT_FOUND
        sta dosio_err
        sec
        rts
@opened:
        ldx dosio_slot
        ldy dosio_uci_tgt,x
        lda #1
        sta dosio_uci_busy,y
        lda #4                  ; FILE_INFO -> size (u32 LE, first 4 bytes)
        sta dosio_uci_max
        lda #0
        sta dosio_uci_max+1
        lda #DOS_CMD_FILE_INFO
        jsr uci_cmd_slot
        bcs @unk
        jsr uci_exec
        bcs @unk
        jsr uci_ok
        bcs @unk
        lda dosio_uci_rn+1
        bne @sz
        lda dosio_uci_rn
        cmp #4
        bcc @unk
@sz:    ldx dosio_slot
        lda dosio_buf
        sta dosio_uci_s0,x
        lda dosio_buf+1
        sta dosio_uci_s1,x
        lda dosio_buf+2
        sta dosio_uci_s2,x
        lda dosio_buf+3
        sta dosio_uci_s3,x
        clc
        rts
@unk:   ldx dosio_slot          ; size unknown: reads run to the real EOF
        lda #$FF
        sta dosio_uci_s0,x
        sta dosio_uci_s1,x
        sta dosio_uci_s2,x
        lda #$7F
        sta dosio_uci_s3,x
        clc
        rts

; A = open flags: [t][02][flags][name...].  C=0 when the status is "00,OK".
uci_open_try:
        pha
        jsr uci_nomax
        ldx dosio_slot
        lda #DOS_CMD_OPEN_FILE
        jsr uci_cmd_slot
        pla
        bcs @r
        jsr uci_put
        ldy #0
@nm:    lda dosio_name,y
        jsr uci_put
        iny
        cpy dosio_nlen
        bne @nm
        jsr uci_exec
        bcs @r
        jmp uci_ok
@r:     rts

dosio_uci_close:
        ldx dosio_slot          ; X = slot (also when called directly)
        jsr uci_nomax
        lda #DOS_CMD_CLOSE_FILE
        jsr uci_cmd_slot
        bcs @free
        jsr uci_exec
@free:  ldx dosio_slot
        ldy dosio_uci_tgt,x
        lda #0
        sta dosio_uci_busy,y
        clc
        rts

dosio_uci_size:
        ldx dosio_slot          ; X = slot (also when called directly)
        lda dosio_uci_s0,x
        sta dosio_arg
        lda dosio_uci_s1,x
        sta dosio_arg+1
        lda dosio_uci_s2,x
        sta dosio_arg+2
        lda dosio_uci_s3,x
        sta dosio_arg+3
        clc
        rts

dosio_uci_setsize:
        ldx dosio_slot          ; X = slot (also when called directly)
        lda dosio_arg
        sta dosio_uci_s0,x
        lda dosio_arg+1
        sta dosio_uci_s1,x
        lda dosio_arg+2
        sta dosio_uci_s2,x
        lda dosio_arg+3
        sta dosio_uci_s3,x
dosio_uci_wcheck:               ; no extent limit known up front
        clc
        rts

; [t][06][p0][p1][p2][p3] — absolute position dosio_arg
dosio_uci_seek:
        ldx dosio_slot          ; X = slot (also when called directly)
        jsr uci_nomax
        lda #DOS_CMD_FILE_SEEK
        jsr uci_cmd_slot
        bcs @err
        ldy #0
@p:     lda dosio_arg,y
        jsr uci_put
        iny
        cpy #4
        bne @p
        jsr uci_exec
        bcs @err
        jsr uci_ok
        bcs @err
        rts
@err:   lda #ERROR_SEEK_ERROR
        sta dosio_err
        sec
        rts

; [t][04][len lo][len hi] -> dosio_buf; dosio_len := bytes received.
; (Success status is empty, dos.cc:800; a read error is not reported
; reliably by the firmware (dos.cc:795-798), so the count is the truth.)
dosio_uci_rdchunk:
        ldx dosio_slot          ; X = slot (also when called directly)
        lda dosio_len
        sta dosio_uci_max
        lda dosio_len+1
        sta dosio_uci_max+1
        lda #DOS_CMD_READ_DATA
        jsr uci_cmd_slot
        bcs @err
        lda dosio_len
        jsr uci_put
        lda dosio_len+1
        jsr uci_put
        jsr uci_exec
        bcs @err
        lda dosio_uci_rn        ; dosio_len := min(received, requested)
        cmp dosio_uci_max
        lda dosio_uci_rn+1
        sbc dosio_uci_max+1
        bcs @full               ; received >= requested: keep dosio_len
        lda dosio_uci_rn
        sta dosio_len
        lda dosio_uci_rn+1
        sta dosio_len+1
@full:  clc
        rts
@err:   lda #ERROR_SEEK_ERROR
        sta dosio_err
        sec
        rts

; [t][05][0][0][payload] — dosio_len (1-256) bytes from dosio_buf
dosio_uci_wrchunk:
        ldx dosio_slot          ; X = slot (also when called directly)
        jsr uci_nomax
        lda #DOS_CMD_WRITE_DATA
        jsr uci_cmd_slot
        bcs @err
        lda #0
        jsr uci_put
        lda #0
        jsr uci_put
        lda dosio_len           ; Y counts 0..len-1 (len 256 -> Y wraps)
        sta dosio_uci_i
        ldy #0
@b:     lda dosio_buf,y
        jsr uci_put
        iny
        cpy dosio_uci_i
        bne @b
        jsr uci_exec
        bcs @err
        jsr uci_ok
        bcs @err
        rts
@err:   lda #ERROR_DISK_FULL
        sta dosio_err
        sec
        rts

.segment "RODATA"
uci_mode_flags:                 ; indexed by dosio_mode
        .byte FA_READ | FA_WRITE                    ; MODE_OLDFILE
        .byte FA_READ | FA_WRITE | FA_CREATE_ALWAYS ; MODE_NEWFILE
        .byte FA_READ | FA_WRITE | FA_OPEN_ALWAYS   ; MODE_READWRITE
