; ===========================================================================
; runtime/ops/hle_mem.s — exec memory allocator behind AllocMem/FreeMem,
; pools and AllocVec.  Owner: WAVE5 w5-heap.  Contract: docs/WAVE5_BRIEF.md C1.
;
; SUPERVISOR PRE-LAND: the Wave-4 bump allocator moved here VERBATIM from
; hle.s (behaviour unchanged).  ABI (BSS vars exported by hle.s):
;   hle_alloc: in  hle_sz (u32 LE size), hle_flg (bit 0 = MEMF_CLEAR)
;              out hle_old (u32 LE block, 0 = failed)
;   hle_free:  in  hle_ptr (u32 LE block), hle_sz (u32 LE size)
;   Both clobber A, X, Y, m_*, hle_sz, hle_cnt, hle_ptr (alloc), hle_old (free).
; Heap state: guest ENV_STATE+0 (u32 heap_next), staged by host/hle_env.py.
; ===========================================================================

.include "../macros.inc"

.export hle_alloc, hle_free
.import mem_read32, mem_write32
.import hle_sz, hle_flg, hle_old, hle_ptr, hle_cnt

ST_HEAP      = 0                ; ENV_STATE +0: u32 heap_next
HEAP_TOP     = HEAP_END + 1     ; first byte past the pool ($800000)

.macro SETADDR addr, off        ; m_addr := addr + off (24-bit constant)
        lda #<(addr + off)
        sta m_addr
        lda #>(addr + off)
        sta m_addr+1
        lda #^(addr + off)
        sta m_addr+2
        lda #0
        sta m_addr+3
.endmacro

.segment "CODE"

; ===========================================================================
; hle_alloc — hle_sz = requested size (u32 LE), hle_flg bit 0 = MEMF_CLEAR.
; Result in hle_old (0 = failed).  Updates ENV_STATE heap_next.
; ===========================================================================
hle_rts:
        rts

hle_alloc:
        lda #0
        sta hle_old
        sta hle_old+1
        sta hle_old+2
        sta hle_old+3
        lda hle_sz+3            ; > $700000 can never fit: reject before
        bne hle_rts                 ; rounding (no overflow below)
        lda hle_sz+2
        cmp #((HEAP_TOP - HEAP_BASE) >> 16)
        bcc @szok
        bne hle_rts
        lda hle_sz+1
        ora hle_sz
        bne hle_rts
@szok:  lda hle_sz              ; size 0 -> fail
        ora hle_sz+1
        ora hle_sz+2
        beq @no
        clc                     ; hle_sz := (size + 7) & ~7
        lda hle_sz
        adc #7
        and #$F8
        sta hle_sz
        bcc @rd
        inc hle_sz+1
        bne @rd
        inc hle_sz+2
@rd:    SETADDR ENV_STATE, ST_HEAP
        jsr mem_read32          ; m_val = heap_next
        clc                     ; m_val := heap_next + size (24-bit + carry)
        lda m_val
        sta hle_old
        adc hle_sz
        sta m_val
        lda m_val+1
        sta hle_old+1
        adc hle_sz+1
        sta m_val+1
        lda m_val+2
        sta hle_old+2
        adc hle_sz+2
        sta m_val+2
        lda #0
        sta hle_old+3
        adc #0
        bne @nofit              ; carried past 24 bits (corrupt state)
        lda m_val+3
        bne @nofit
        lda #^HEAP_TOP          ; new end must be <= HEAP_TOP ($800000)
        cmp m_val+2
        bcc @nofit
        bne @fits
        lda m_val+1
        ora m_val
        bne @nofit
@fits:  jsr mem_write32         ; heap_next := new end (m_addr still ENV_STATE)
        lda hle_flg
        and #$01                ; MEMF_CLEAR?
        beq @out
        jmp hle_clear
@nofit: lda #0
        sta hle_old
        sta hle_old+1
        sta hle_old+2
@no:
@out:   rts

; hle_clear — zero [hle_old, hle_old + hle_sz) through the memory API,
; one mem_write32 per longword (size is a multiple of 8).  ~30 cycles/byte
; on the C64 side; see docs/HLE.md "MEMF_CLEAR cost".
hle_clear:
        lda hle_sz+2            ; hle_cnt := size / 4
        lsr a
        sta hle_cnt+2
        lda hle_sz+1
        ror a
        sta hle_cnt+1
        lda hle_sz
        ror a
        sta hle_cnt
        lsr hle_cnt+2
        ror hle_cnt+1
        ror hle_cnt
        ldx #3
@a:     lda hle_old,x
        sta hle_ptr,x
        dex
        bpl @a
@loop:  ldx #3
@b:     lda hle_ptr,x
        sta m_addr,x
        lda #0
        sta m_val,x
        dex
        bpl @b
        jsr mem_write32
        clc                     ; pointer += 4
        lda hle_ptr
        adc #4
        sta hle_ptr
        bcc @cnt
        inc hle_ptr+1
        bne @cnt
        inc hle_ptr+2
@cnt:   lda hle_cnt             ; 24-bit count--, loop while != 0
        bne @d0
        lda hle_cnt+1
        bne @d1
        dec hle_cnt+2
@d1:    dec hle_cnt+1
@d0:    dec hle_cnt
        lda hle_cnt
        ora hle_cnt+1
        ora hle_cnt+2
        bne @loop
        rts

; ===========================================================================
; hle_free — hle_ptr = block, hle_sz = size.  Top-block reclaim, else leak
; (see hle_freemem).  Never writes guest memory other than heap_next.
; ===========================================================================
hle_rts2:
        rts

hle_free:
        lda hle_ptr+3           ; block must lie in [HEAP_BASE, HEAP_TOP)
        bne hle_rts2
        lda hle_ptr+2
        cmp #^HEAP_BASE
        bcc hle_rts2
        cmp #^HEAP_TOP
        bcs hle_rts2
        lda hle_sz+3            ; absurd size -> cannot be the top block
        bne hle_rts2
        lda hle_sz+2
        cmp #((HEAP_TOP - HEAP_BASE) >> 16)
        bcc @szok
        bne hle_rts2
        lda hle_sz+1
        ora hle_sz
        bne hle_rts2
@szok:  clc                     ; hle_sz := (size + 7) & ~7
        lda hle_sz
        adc #7
        and #$F8
        sta hle_sz
        bcc @sum
        inc hle_sz+1
        bne @sum
        inc hle_sz+2
@sum:   clc                     ; hle_old := block + rounded size
        lda hle_ptr
        adc hle_sz
        sta hle_old
        lda hle_ptr+1
        adc hle_sz+1
        sta hle_old+1
        lda hle_ptr+2
        adc hle_sz+2
        sta hle_old+2
        SETADDR ENV_STATE, ST_HEAP
        jsr mem_read32
        lda m_val               ; block end == heap_next?
        cmp hle_old
        bne @out
        lda m_val+1
        cmp hle_old+1
        bne @out
        lda m_val+2
        cmp hle_old+2
        bne @out
        lda m_val+3
        bne @out
        ldx #2                  ; heap_next := block (top reclaim)
@w:     lda hle_ptr,x
        sta m_val,x
        dex
        bpl @w
        jsr mem_write32         ; m_addr still ENV_STATE+ST_HEAP
@out:   rts

