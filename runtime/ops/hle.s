; ===========================================================================
; runtime/ops/hle.s — M2: high-level emulation of AmigaOS library calls
; Owner: M2-hle.  Contract: docs/CONTRACTS.md, docs/HLE.md (ABI + env map).
;
; Entered from op_aline (core) for every $Axxx opcode except $AFFF, with the
; opcode word in op_lo/op_hi.  $A1xx = HLE call xx (the host-side environment
; builder, host/hle_env.py, plants these words in the library vector slots at
; base-LVO, so a guest `jsr -LVO(A6)` lands here with the return address
; already pushed on the guest stack).  Any other $Axxx halts with reason
; HALT_ALINE, preserving the pre-M2 A-line trap semantics.
;
; v0 function set (indices assigned in docs/HLE.md, mirrored by hle_env.py):
;   $A100 exec.library/OpenLibrary   (LVO -552)
;   $A101 exec.library/CloseLibrary  (LVO -414)
;   $A102 dos.library/Output         (LVO  -60)
;   $A103 dos.library/Write          (LVO  -48)
;
; Every HLE call retires by emulating RTS: pop the return address from the
; guest stack (A7 += 4, PC = popped value masked to 24 bits), then
; jmp dispatch_next — so one $A1xx word counts as one guest instruction.
; ===========================================================================

.include "../macros.inc"

.export hle_aline

.import mem_read8, mem_read32
.import dispatch_next, emu_halt

HLE_FN_COUNT = 4                ; valid indices $A100-$A103

; DOSBase guest address (docs/HLE.md env map; must match host/hle_env.py)
DOS_BASE     = $000A00
; Magic file handle returned by Output (must match hle_env.FH_MAGIC)
FH_MAGIC     = $0000C0DE

SCREEN       = $0400            ; C64 text screen (always RAM, VIC bank 0)
SCREEN_COLS  = 40
SCREEN_SIZE  = 1000             ; 40x25; v0 caps output here, no scrolling

; --- File-local scratch (survives core calls, unlike t_tmp) ----------------
.segment "BSS"
hle_ptr:  .res 4                ; walking guest pointer (string / Write buf)
hle_cnt:  .res 2                ; remaining Write byte count (v0: low 16 bits)
hle_cur:  .res 2                ; screen cursor, offset 0-999 from SCREEN
hle_col:  .res 1                ; cursor column 0-39 (for newline advance)
hle_idx:  .res 1                ; OpenLibrary name-compare index

.segment "CODE"

; ===========================================================================
; hle_aline — dispatch on the A-line opcode word.
; ===========================================================================
hle_aline:
        lda op_hi
        cmp #$A1
        bne hle_trap
        lda op_lo
        cmp #HLE_FN_COUNT
        bcs hle_trap
        tax
        lda hle_tab_lo,x
        sta hle_jv+1
        lda hle_tab_hi,x
        sta hle_jv+2
hle_jv: jmp $FFFF               ; self-modified (PRG is in RAM)

hle_trap:                       ; non-HLE A-line: pre-M2 semantics
        lda #HALT_ALINE
        jmp emu_halt

; ===========================================================================
; $A100 OpenLibrary — name pointer in A1, version in D0 (ignored, v0).
; Compares the guest string against "dos.library"; match -> D0 = DOSBase,
; anything else -> D0 = 0.
; ===========================================================================
hle_openlibrary:
        ldx #3                  ; walking pointer := A1
@init:  lda r_a1,x
        sta hle_ptr,x
        dex
        bpl @init
        lda #0
        sta hle_idx
@loop:  ldx #3                  ; m_addr := pointer (fresh each call)
@addr:  lda hle_ptr,x
        sta m_addr,x
        dex
        bpl @addr
        jsr mem_read8
        ldx hle_idx
        lda dos_name,x
        cmp m_val
        bne @nomatch
        cmp #0                  ; matched the terminator -> full match
        beq @match
        inc hle_idx
        inc hle_ptr             ; 32-bit pointer++
        bne @loop
        inc hle_ptr+1
        bne @loop
        inc hle_ptr+2
        jmp @loop

@nomatch:                       ; unknown library -> D0 = 0
        lda #0
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
        jmp hle_ret

@match:                         ; D0 = DOSBase
        lda #<DOS_BASE
        sta r_d0
        lda #>DOS_BASE
        sta r_d0+1
        lda #^DOS_BASE
        sta r_d0+2
        lda #0
        sta r_d0+3
        jmp hle_ret

; ===========================================================================
; $A101 CloseLibrary — library base in A1.  v0: nothing to release; all
; registers preserved (AmigaOS CloseLibrary returns nothing).
; ===========================================================================
hle_closelibrary:
        jmp hle_ret

; ===========================================================================
; $A102 Output — return the console file handle in D0 (magic constant).
; v0 also (re)homes the screen cursor here: Output is the mandatory first
; console call, and the runner has no HLE init hook (BSS is not cleared at
; boot).  Programs must call Output before Write.
; ===========================================================================
hle_output:
        lda #0
        sta hle_cur
        sta hle_cur+1
        sta hle_col
        lda #<FH_MAGIC
        sta r_d0
        lda #>FH_MAGIC
        sta r_d0+1
        lda #^FH_MAGIC
        sta r_d0+2
        lda #0
        sta r_d0+3
        jmp hle_ret

; ===========================================================================
; $A103 Write — fh in D1 (ignored, v0), buffer in D2, length in D3.
; Copies guest bytes to the C64 text screen (ASCII -> screen codes, $0A =
; newline; output stops silently at screen end, no scrolling).
; Returns D0 = D3.  v0 limitation: only the low 16 bits of D3 are copied
; (the screen caps at 1000 characters anyway).
; ===========================================================================
hle_write:
        ldx #3                  ; walking pointer := D2 (buffer)
@init:  lda r_d2,x
        sta hle_ptr,x
        dex
        bpl @init
        lda r_d3
        sta hle_cnt
        lda r_d3+1
        sta hle_cnt+1
@loop:  lda hle_cnt
        ora hle_cnt+1
        beq @done
        ldx #3
@addr:  lda hle_ptr,x
        sta m_addr,x
        dex
        bpl @addr
        jsr mem_read8
        lda m_val
        jsr hle_putc
        inc hle_ptr             ; 32-bit pointer++
        bne @cnt
        inc hle_ptr+1
        bne @cnt
        inc hle_ptr+2
@cnt:   lda hle_cnt             ; 16-bit count--
        bne @declo
        dec hle_cnt+1
@declo: dec hle_cnt
        jmp @loop

@done:  ldx #3                  ; D0 = D3 (bytes written)
@ret:   lda r_d3,x
        sta r_d0,x
        dex
        bpl @ret
        jmp hle_ret

; ===========================================================================
; hle_putc — A = ASCII byte.  Convert to a screen code and store at the
; cursor.  $0A advances to the next 40-column line; other control bytes are
; ignored; >= $80 prints '?'.  Clobbers A/X/Y and t_tmp+4/5 (ZP pointer;
; the memory API only uses t_tmp+0..3 and is not called from here).
; ===========================================================================
hle_putc:
        cmp #$0A
        beq @newline
        cmp #$20
        bcc @out                ; other control chars: ignore
        cmp #$40
        bcc @store              ; $20-$3F (digits/punct/space): direct
        cmp #$60
        bcs @lower
        sec                     ; $40-$5F ('@','A'-'Z','[','\',']','^','_')
        sbc #$40
        jmp @store
@lower: cmp #$80
        bcs @high
        sec                     ; $60-$7F ('a'-'z' -> codes 1-26, uppercase
        sbc #$60                ;  in the default charset)
        jmp @store
@high:  lda #$3F                ; >= $80 -> '?'

@store: tax                     ; X = screen code
        lda hle_cur+1           ; cursor past screen end? -> drop
        cmp #>SCREEN_SIZE
        bcc @write
        bne @out
        lda hle_cur
        cmp #<SCREEN_SIZE
        bcs @out
@write: lda hle_cur             ; t_tmp+4/5 := SCREEN + cursor
        sta t_tmp+4             ; (<SCREEN = 0, so lo byte = cursor lo)
        lda hle_cur+1
        clc
        adc #>SCREEN
        sta t_tmp+5
        txa
        ldy #0
        sta (t_tmp+4),y
        inc hle_cur             ; cursor++
        bne @col
        inc hle_cur+1
@col:   inc hle_col
        lda hle_col
        cmp #SCREEN_COLS
        bcc @out
        lda #0
        sta hle_col
@out:   rts

@newline:                       ; cursor += 40 - column; column = 0
        sec
        lda #SCREEN_COLS
        sbc hle_col
        clc
        adc hle_cur
        sta hle_cur
        bcc @nc
        inc hle_cur+1
@nc:    lda #0
        sta hle_col
        rts

; ===========================================================================
; hle_ret — emulate RTS: PC = [A7] masked to 24 bits, A7 += 4, next insn.
; ===========================================================================
hle_ret:
        ldx #3                  ; m_addr := A7
@sp:    lda r_a7,x
        sta m_addr,x
        dex
        bpl @sp
        jsr mem_read32          ; m_val = return address
        clc                     ; A7 += 4
        lda r_a7
        adc #4
        sta r_a7
        lda r_a7+1
        adc #0
        sta r_a7+1
        lda r_a7+2
        adc #0
        sta r_a7+2
        lda r_a7+3
        adc #0
        sta r_a7+3
        lda m_val               ; PC = popped address, bits 24-31 zero
        sta r_pc
        lda m_val+1
        sta r_pc+1
        lda m_val+2
        sta r_pc+2
        lda #0
        sta r_pc+3
        jmp dispatch_next

; ===========================================================================
.segment "RODATA"
hle_tab_lo:
        .byte <hle_openlibrary, <hle_closelibrary, <hle_output, <hle_write
hle_tab_hi:
        .byte >hle_openlibrary, >hle_closelibrary, >hle_output, >hle_write
dos_name:
        .byte "dos.library", 0
