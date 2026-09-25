; ===========================================================================
; runtime/ops/hle_mem.s — exec memory allocator behind AllocMem/FreeMem,
; pools and AllocVec.  Owner: WAVE5 w5-heap.  Contract: docs/WAVE5_BRIEF.md C1;
; design and costs: docs/HLE.md "Heap".
;
; ABI (BSS vars exported by hle.s):
;   hle_alloc: in  hle_sz (u32 LE size), hle_flg (bit 0 = MEMF_CLEAR)
;              out hle_old (u32 LE block, 0 = failed)
;   hle_free:  in  hle_ptr (u32 LE block), hle_sz (u32 LE size)
;   hle_alloc clobbers A, X, Y, m_*, hle_sz (rounded), hle_cnt (MEMF_CLEAR);
;   hle_free clobbers A, X, Y, m_*, hle_sz — neither touches hle_ptr, and
;   hle_free leaves hle_old alone (C1 allows more; callers rely on nothing
;   else).  Both also use this file's own hle_h* BSS.
; Pool entries (wired from hle.s AllocPooled / DeletePool):
;   hle_pool_alloc:  as hle_alloc, the block is tagged with the pool in A0
;   hle_pool_delete: A0 = pool header; frees every block tagged with it,
;                    then the header itself.  Clobbers as hle_free plus
;                    hle_ptr (:= A0).
;
; First-fit allocator with boundary tags, all metadata in guest memory:
;   ENV_STATE+0  u32 top: start of the untouched "wilderness" above the last
;                block (staged = HEAP_BASE by host/hle_env.py = pristine heap;
;                the first call formats the control block)
;   HEAP_CTL+0   u32 head of the doubly linked free list (0 = empty)
;   HEAP_CTL+4   u32 count of rejected (invalid) frees
;   blocks tile [HEAP_FIRST, top); each is 8-aligned and starts with an
;   8-byte header, the payload (the address AllocMem returns) follows:
;     +0 u32 size (header included, multiple of 8, >= 16) | flags
;            bit 0 USED, bit 1 PFREE (the block below is free),
;            bit 2 SLACK (used: 8 bytes beyond the rounded request)
;     +4 u32 used: $5A << 24 | owner (pool header, 0 = AllocMem)
;            free: next free block
;     +8 u32 free: previous free block (0 = list head)
;     end-4 u32 free: size (footer, read through the PFREE bit)
; Invariants: no two free blocks touch (coalesced on free); no free block
; touches the wilderness (merged into it, top drops); so the block right
; below top is always used, and a free block's lower neighbour is used.
; ===========================================================================

.include "../macros.inc"
.macpack longbranch

.export hle_alloc, hle_free
.export hle_pool_alloc, hle_pool_delete
.import mem_read32, mem_write32
.import hle_sz, hle_flg, hle_old, hle_ptr, hle_cnt

ST_HEAP      = 0                ; ENV_STATE +0: u32 top (wilderness start)
HEAP_TOP     = HEAP_END + 1     ; first byte past the pool ($800000)
HC_HEAD      = 0                ; HEAP_CTL +0: u32 free-list head
HC_REJ       = 4                ; HEAP_CTL +4: u32 rejected frees
HB_USED      = $01              ; header word 0 flag bits
HB_PFREE     = $02
HB_SLACK     = $04
HB_FLAGS     = $07
HB_TAG       = $5A              ; header word 1 high byte of a live block
HB_MIN       = 16               ; smallest block (a free one needs 16)
HB_NEXT      = 4                ; free-list links
HB_PREV      = 8
WALK_MAX     = $038000          ; @value free-list walk guard (nodes): the most free
                                ; blocks a 7 MB heap can hold is 7 MB / 32 =
                                ; 229 376 (a free block needs a used neighbour)

.assert (HEAP_TOP - HEAP_BASE) = $700000, error, "size checks assume a 7 MB heap"

.segment "BSS"
hle_hb:   .res 3                ; block being allocated / freed (header)
hle_hs:   .res 3                ; its size (flags stripped)
hle_hf:   .res 1                ; its flag bits
hle_hn:   .res 3                ; free-list next / prev of a node
hle_hp:   .res 3
hle_hx:   .res 3                ; upper neighbour (hb + hs)
hle_hxs:  .res 3                ; neighbour size / split remainder
hle_hxw:  .res 4                ; upper neighbour's header word 0
hle_ta:   .res 3                ; hm_mkfree / hm_footer: block, size
hle_ts:   .res 3
hle_hvn:  .res 3                ; hm_fix: new prev->next (or head), next->prev
hle_hvp:  .res 3
hle_hq:   .res 3                ; hm_inheap operand
hle_htop: .res 3                ; ENV_STATE top
hle_hhd:  .res 3                ; free-list head
hle_hown: .res 3                ; owner of a new block (pool, 0 = AllocMem)
hle_hnd:  .res 3                ; block size needed (header included)
hle_hcnt: .res 3                ; walk guard
hle_hwild: .res 1               ; release: block touches the wilderness
hle_hnu:  .res 1                ; release: upper neighbour is a used block
hle_hpool: .res 3               ; DeletePool: pool header address
hle_hw:   .res 3                ; DeletePool: walk cursor / next block
hle_hwn:  .res 3

.segment "CODE"

; --- 24-bit helpers (guest addresses and sizes; byte 3 is always 0) ----------
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

.macro AT v, off                ; m_addr := v + off (off 0..255)
        clc
        lda v
        adc #off
        sta m_addr
        lda v+1
        adc #0
        sta m_addr+1
        lda v+2
        adc #0
        sta m_addr+2
        lda #0
        sta m_addr+3
.endmacro

.macro VAL v                    ; m_val := v (24-bit, byte 3 = 0)
        lda v
        sta m_val
        lda v+1
        sta m_val+1
        lda v+2
        sta m_val+2
        lda #0
        sta m_val+3
.endmacro

.macro GET v                    ; v := m_val bytes 0-2
        lda m_val
        sta v
        lda m_val+1
        sta v+1
        lda m_val+2
        sta v+2
.endmacro

.macro MOV3 d, s
        lda s
        sta d
        lda s+1
        sta d+1
        lda s+2
        sta d+2
.endmacro

.macro CLR3 d
        lda #0
        sta d
        sta d+1
        sta d+2
.endmacro

.macro ADD3 dst, x1, x2        ; dst := x1 + x2
        clc
        lda x1
        adc x2
        sta dst
        lda x1+1
        adc x2+1
        sta dst+1
        lda x1+2
        adc x2+2
        sta dst+2
.endmacro

.macro SUB3 dst, x1, x2        ; dst := x1 - x2 (C = 0: borrow)
        sec
        lda x1
        sbc x2
        sta dst
        lda x1+1
        sbc x2+1
        sta dst+1
        lda x1+2
        sbc x2+2
        sta dst+2
.endmacro

.macro CMP3 x1, x2             ; C := (x1 >= x2), unsigned 24-bit
        lda x1
        cmp x2
        lda x1+1
        sbc x2+1
        lda x1+2
        sbc x2+2
.endmacro

.macro CMP3I v, k               ; C := (v >= k), k a 24-bit constant
        lda v
        cmp #<(k)
        lda v+1
        sbc #>(k)
        lda v+2
        sbc #^(k)
.endmacro

.macro IS0_3 v                  ; Z := (v == 0)
        lda v
        ora v+1
        ora v+2
.endmacro

.macro HDR0 v                   ; hle_hf/hle_hs := flags/size of word 0 in m_val
        lda m_val
        and #HB_FLAGS
        sta hle_hf
        lda m_val
        and #<~HB_FLAGS
        sta v
        lda m_val+1
        sta v+1
        lda m_val+2
        sta v+2
.endmacro

; ===========================================================================
; hle_alloc — hle_sz = requested size (u32 LE), hle_flg bit 0 = MEMF_CLEAR.
; Result in hle_old (0 = failed).  hle_pool_alloc: same, owner = A0.
; ===========================================================================
hle_pool_alloc:
        lda r_a0
        sta hle_hown
        lda r_a0+1
        sta hle_hown+1
        lda r_a0+2
        sta hle_hown+2
        jmp hm_alloc

hle_alloc:
        CLR3 hle_hown
hm_alloc:
        lda #0
        sta hle_old
        sta hle_old+1
        sta hle_old+2
        sta hle_old+3
        jsr hm_size             ; round hle_sz; C=1: 0 or > 7 MB
        bcc :+
        rts
:       clc                     ; hnd := size + header
        lda hle_sz
        adc #8
        sta hle_hnd
        lda hle_sz+1
        adc #0
        sta hle_hnd+1
        lda hle_sz+2
        adc #0
        sta hle_hnd+2
        jsr hm_state
        bcc :+
        rts                     ; corrupt heap state: fail
:
; --- first fit over the free list ------------------------------------------
        MOV3 hle_hb, hle_hhd
        CLR3 hle_hp
        lda #<WALK_MAX
        sta hle_hcnt
        lda #>WALK_MAX
        sta hle_hcnt+1
        lda #^WALK_MAX
        sta hle_hcnt+2
@walk:  IS0_3 hle_hb
        jeq @wild
        MOV3 hle_hq, hle_hb     ; a corrupt link ends the walk
        jsr hm_inheap
        jcs @wild
        lda hle_hcnt            ; guard--, give up at 0 (cycle in the list)
        bne @g0
        lda hle_hcnt+1
        bne @g1
        lda hle_hcnt+2
        jeq @wild
        dec hle_hcnt+2
@g1:    dec hle_hcnt+1
@g0:    dec hle_hcnt
        AT hle_hb, 0
        jsr mem_read32
        lda m_val+3
        jne @wild
        HDR0 hle_hs
        lda hle_hf
        and #HB_USED
        jne @wild               ; a used block in the free list: corrupt
        AT hle_hb, HB_NEXT
        jsr mem_read32
        GET hle_hn
        CMP3 hle_hs, hle_hnd
        bcs @take
        MOV3 hle_hp, hle_hb
        MOV3 hle_hb, hle_hn
        jmp @walk

@take:  SUB3 hle_hxs, hle_hs, hle_hnd   ; remainder
        CMP3I hle_hxs, HB_MIN
        bcc @whole
        ADD3 hle_ta, hle_hb, hle_hnd    ; split: the upper part stays free,
        MOV3 hle_ts, hle_hxs            ; in the list position of the old block
        jsr hm_mkfree
        MOV3 hle_hvn, hle_ta
        MOV3 hle_hvp, hle_ta
        jsr hm_fix
        MOV3 hle_hs, hle_hnd
        lda #HB_USED
        sta hle_hf
        jmp @hdr
@whole: MOV3 hle_hvn, hle_hn            ; unlink the whole block
        MOV3 hle_hvp, hle_hp
        jsr hm_fix
        ADD3 hle_hx, hle_hb, hle_hs     ; the block above: prev is used now
        CMP3 hle_hx, hle_htop
        bcs :+
        AT hle_hx, 0
        jsr mem_read32
        lda m_val
        and #<~HB_PFREE
        sta m_val
        jsr mem_write32
:       lda #HB_USED
        ldx hle_hxs                     ; remainder 0 or 8 (multiple of 8, < 16)
        beq :+
        ora #HB_SLACK
:       sta hle_hf
        jmp @hdr

; --- no free block fits: carve from the wilderness ---------------------------
@wild:  ADD3 hle_hx, hle_htop, hle_hnd
        CMP3I hle_hx, HEAP_TOP + 1
        bcc :+
        rts                             ; does not fit: hle_old = 0
:       MOV3 hle_hb, hle_htop
        MOV3 hle_hs, hle_hnd
        lda #HB_USED                    ; the block below top is always used
        sta hle_hf
        MOV3 hle_htop, hle_hx
        SETADDR ENV_STATE, ST_HEAP
        VAL hle_htop
        jsr mem_write32

@hdr:   AT hle_hb, 0                    ; header: size | flags, tag | owner
        VAL hle_hs
        lda m_val
        ora hle_hf
        sta m_val
        jsr mem_write32
        AT hle_hb, HB_NEXT
        VAL hle_hown
        lda #HB_TAG
        sta m_val+3
        jsr mem_write32
        clc                             ; hle_old := payload = hb + 8
        lda hle_hb
        adc #8
        sta hle_old
        lda hle_hb+1
        adc #0
        sta hle_old+1
        lda hle_hb+2
        adc #0
        sta hle_old+2
        lda hle_flg
        and #$01                        ; MEMF_CLEAR?
        jne hm_clear
        rts

; hm_clear — zero [hle_old, hle_old + hle_sz) through the memory API, one
; mem_write32 per longword (hle_sz is a multiple of 8).  mem_write32 keeps
; m_addr and m_val, so the loop only steps the address.
hm_clear:
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
        sta m_addr,x
        lda #0
        sta m_val,x
        dex
        bpl @a
@loop:  jsr mem_write32
        clc                     ; address += 4
        lda m_addr
        adc #4
        sta m_addr
        bcc @cnt
        inc m_addr+1
        bne @cnt
        inc m_addr+2
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

; hm_size — hle_sz := (hle_sz + 7) & ~7.  C=1 (hle_sz untouched) when the
; size is 0 or larger than the whole heap (no rounding overflow past that).
hm_size:
        lda hle_sz+3
        bne @no
        lda hle_sz+2
        cmp #((HEAP_TOP - HEAP_BASE) >> 16)
        bcc @ok
        bne @no
        lda hle_sz+1
        ora hle_sz
        bne @no
@ok:    lda hle_sz
        ora hle_sz+1
        ora hle_sz+2
        beq @no
        clc
        lda hle_sz
        adc #7
        and #$F8
        sta hle_sz
        bcc :+
        inc hle_sz+1
        bne :+
        inc hle_sz+2
:       clc
        rts
@no:    sec
        rts

; hm_state — load top and the free-list head; format the control block on
; the first call of a run (top still = HEAP_BASE as staged).  C=1: the
; state is corrupt (top or head out of range) — callers then do nothing.
hm_state:
        SETADDR ENV_STATE, ST_HEAP
        jsr mem_read32
        lda m_val+3
        jne @bad
        GET hle_htop
        lda hle_htop
        and #7
        bne @bad
        lda hle_htop
        cmp #<HEAP_BASE
        bne @run
        lda hle_htop+1
        cmp #>HEAP_BASE
        bne @run
        lda hle_htop+2
        cmp #^HEAP_BASE
        beq @init
@run:   CMP3I hle_htop, HEAP_FIRST
        bcc @bad
        CMP3I hle_htop, HEAP_TOP + 1
        bcs @bad
        SETADDR HEAP_CTL, HC_HEAD
        jsr mem_read32
        lda m_val+3
        bne @bad
        GET hle_hhd
        IS0_3 hle_hhd
        beq @ok
        MOV3 hle_hq, hle_hhd
        jmp hm_inheap           ; C from the range check
@ok:    clc
        rts
@bad:   sec
        rts
@init:  lda #<HEAP_FIRST        ; top := HEAP_FIRST, head := 0, rejects := 0
        sta hle_htop
        lda #>HEAP_FIRST
        sta hle_htop+1
        lda #^HEAP_FIRST
        sta hle_htop+2
        CLR3 hle_hhd
        SETADDR ENV_STATE, ST_HEAP
        VAL hle_htop
        jsr mem_write32
        SETADDR HEAP_CTL, HC_HEAD
        VAL hle_hhd
        jsr mem_write32
        SETADDR HEAP_CTL, HC_REJ
        jsr mem_write32
        clc
        rts

; hm_inheap — C=0 when hle_hq is 8-aligned and HEAP_FIRST <= hq < top.
hm_inheap:
        lda hle_hq
        and #7
        bne @no
        CMP3I hle_hq, HEAP_FIRST
        bcc @no
        CMP3 hle_hq, hle_htop
        bcs @no
        clc
        rts
@no:    sec
        rts

; hm_mkfree — write free block hle_ta of size hle_ts: header (no flags: a
; free block's lower neighbour is used), links hle_hn / hle_hp, footer.
hm_mkfree:
        AT hle_ta, 0
        VAL hle_ts
        jsr mem_write32
        AT hle_ta, HB_NEXT
        VAL hle_hn
        jsr mem_write32
        AT hle_ta, HB_PREV
        VAL hle_hp
        jsr mem_write32
        ; fall through
; hm_footer — [hle_ta + hle_ts - 4] := hle_ts
hm_footer:
        clc
        lda hle_ta
        adc hle_ts
        sta m_addr
        lda hle_ta+1
        adc hle_ts+1
        sta m_addr+1
        lda hle_ta+2
        adc hle_ts+2
        sta m_addr+2
        sec
        lda m_addr
        sbc #4
        sta m_addr
        lda m_addr+1
        sbc #0
        sta m_addr+1
        lda m_addr+2
        sbc #0
        sta m_addr+2
        lda #0
        sta m_addr+3
        VAL hle_ts
        jmp mem_write32

; hm_fix — list surgery around a node whose neighbours are hle_hp (prev,
; 0 = head) and hle_hn (next, 0 = tail): prev->next (or head) := hle_hvn,
; next->prev := hle_hvp.  Unlink: hvn = hn, hvp = hp.  Replace: both = new.
hm_fix:
        IS0_3 hle_hp
        bne @pn
        MOV3 hle_hhd, hle_hvn
        SETADDR HEAP_CTL, HC_HEAD
        VAL hle_hhd
        jsr mem_write32
        jmp @nx
@pn:    AT hle_hp, HB_NEXT
        VAL hle_hvn
        jsr mem_write32
@nx:    IS0_3 hle_hn
        beq @out
        AT hle_hn, HB_PREV
        VAL hle_hvp
        jsr mem_write32
@out:   rts

; ===========================================================================
; hle_free — hle_ptr = block, hle_sz = size as passed to FreeMem.
; NULL is a silent no-op.  Every other invalid free (outside [first
; payload, top), misaligned, not a live block, size differs from the
; allocation, double free) changes nothing but HEAP_CTL+4 (+1).
; ===========================================================================
hle_free:
        jsr hm_state
        bcs @out
        lda hle_ptr
        ora hle_ptr+1
        ora hle_ptr+2
        ora hle_ptr+3
        beq @out
        jsr hm_check
        bcs hm_reject
        jmp hm_release
@out:   rts

hm_reject:
        SETADDR HEAP_CTL, HC_REJ
        jsr mem_read32
        inc m_val
        bne :+
        inc m_val+1
        bne :+
        inc m_val+2
        bne :+
        inc m_val+3
:       jmp mem_write32

; hm_check — is hle_ptr the payload of a live block allocated with size
; hle_sz?  C=0 yes (hle_hb/hs/hf loaded), C=1 no.
hm_check:
        lda hle_ptr+3
        jne @no
        jsr hm_size
        jcs @no
        sec                     ; hb := ptr - 8
        lda hle_ptr
        sbc #8
        sta hle_hb
        lda hle_ptr+1
        sbc #0
        sta hle_hb+1
        lda hle_ptr+2
        sbc #0
        sta hle_hb+2
        jcc @no
        MOV3 hle_hq, hle_hb
        jsr hm_inheap
        jcs @no
        AT hle_hb, 0
        jsr mem_read32
        lda m_val+3
        jne @no
        HDR0 hle_hs
        lda hle_hf
        and #HB_USED
        jeq @no                 ; free (double free) or garbage
        CMP3I hle_hs, HB_MIN
        jcc @no
        ADD3 hle_hx, hle_hb, hle_hs
        CMP3 hle_htop, hle_hx
        bcc @no                 ; runs past top
        clc                     ; expected size: request + header (+ slack)
        lda hle_sz
        adc #8
        sta hle_hxs
        lda hle_sz+1
        adc #0
        sta hle_hxs+1
        lda hle_sz+2
        adc #0
        sta hle_hxs+2
        lda hle_hf
        and #HB_SLACK
        beq :+
        clc
        lda hle_hxs
        adc #8
        sta hle_hxs
        bcc :+
        inc hle_hxs+1
        bne :+
        inc hle_hxs+2
:       lda hle_hxs
        cmp hle_hs
        bne @no
        lda hle_hxs+1
        cmp hle_hs+1
        bne @no
        lda hle_hxs+2
        cmp hle_hs+2
        bne @no
        AT hle_hb, HB_NEXT
        jsr mem_read32
        lda m_val+3
        cmp #HB_TAG
        bne @no
        clc
        rts
@no:    sec
        rts

; hm_release — free the valid live block hle_hb (size hle_hs, flags hle_hf):
; merge with a free upper neighbour, a free lower neighbour and/or the
; wilderness; otherwise push it on the free list.  A block whose header
; ends up inside a merged block gets its tag word zeroed, so a later
; double free of it is rejected by hm_check.
hm_release:
        lda #0
        sta hle_hwild
        sta hle_hnu
        ADD3 hle_hx, hle_hb, hle_hs
        CMP3 hle_hx, hle_htop
        bcc @nx
        inc hle_hwild           ; upper neighbour is the wilderness
        jmp @pv
@nx:    AT hle_hx, 0
        jsr mem_read32
        ldx #3
:       lda m_val,x
        sta hle_hxw,x
        dex
        bpl :-
        lda m_val
        and #HB_USED
        beq @nfree
        inc hle_hnu             ; used: gets PFREE at the end
        jmp @pv
@nfree: lda m_val               ; merge the free upper neighbour
        and #<~HB_FLAGS
        sta hle_hxs
        lda m_val+1
        sta hle_hxs+1
        lda m_val+2
        sta hle_hxs+2
        AT hle_hx, HB_NEXT
        jsr mem_read32
        GET hle_hn
        AT hle_hx, HB_PREV
        jsr mem_read32
        GET hle_hp
        MOV3 hle_hvn, hle_hn
        MOV3 hle_hvp, hle_hp
        jsr hm_fix
        ADD3 hle_hs, hle_hs, hle_hxs

@pv:    lda hle_hf
        and #HB_PFREE
        jeq @nopf
        sec                     ; lower neighbour free: its footer at hb - 4
        lda hle_hb
        sbc #4
        sta m_addr
        lda hle_hb+1
        sbc #0
        sta m_addr+1
        lda hle_hb+2
        sbc #0
        sta m_addr+2
        lda #0
        sta m_addr+3
        jsr mem_read32
        lda m_val+3
        jne @nopf               ; corrupt footer: do not merge down
        lda m_val
        and #7
        jne @nopf
        CMP3I m_val, HB_MIN
        jcc @nopf
        SUB3 hle_ta, hle_hb, m_val      ; ta := lower block
        jcc @nopf
        MOV3 hle_hq, hle_ta
        jsr hm_inheap
        jcs @nopf
        MOV3 hle_ts, m_val              ; its size
        AT hle_hb, HB_NEXT              ; scrub our tag (now inside a block)
        lda #0
        sta m_val
        sta m_val+1
        sta m_val+2
        sta m_val+3
        jsr mem_write32
        lda hle_hwild
        jeq @grow
        AT hle_ta, HB_NEXT              ; lower + us + wilderness: unlink the
        jsr mem_read32                  ; lower block, top drops to it
        GET hle_hn
        AT hle_ta, HB_PREV
        jsr mem_read32
        GET hle_hp
        MOV3 hle_hvn, hle_hn
        MOV3 hle_hvp, hle_hp
        jsr hm_fix
        MOV3 hle_htop, hle_ta
        jmp @wtop
@grow:  ADD3 hle_ts, hle_ts, hle_hs     ; the lower block grows in place
        AT hle_ta, 0                    ; (keeps its list position)
        VAL hle_ts
        jsr mem_write32
        jsr hm_footer
        jmp @succ

@nopf:  lda hle_hwild
        beq @new
        AT hle_hb, HB_NEXT              ; us + wilderness: top drops to us
        lda #0
        sta m_val
        sta m_val+1
        sta m_val+2
        sta m_val+3
        jsr mem_write32
        MOV3 hle_htop, hle_hb
@wtop:  SETADDR ENV_STATE, ST_HEAP
        VAL hle_htop
        jmp mem_write32

@new:   MOV3 hle_ta, hle_hb             ; a new free block at the list head
        MOV3 hle_ts, hle_hs
        MOV3 hle_hn, hle_hhd
        CLR3 hle_hp
        jsr hm_mkfree
        IS0_3 hle_hhd
        beq :+
        AT hle_hhd, HB_PREV
        VAL hle_hb
        jsr mem_write32
:       MOV3 hle_hhd, hle_hb
        SETADDR HEAP_CTL, HC_HEAD
        VAL hle_hhd
        jsr mem_write32

@succ:  lda hle_hnu                     ; used upper neighbour: PFREE
        beq @out
        AT hle_hx, 0
        ldx #3
:       lda hle_hxw,x
        sta m_val,x
        dex
        bpl :-
        lda m_val
        ora #HB_PFREE
        sta m_val
        jmp mem_write32
@out:   rts

; ===========================================================================
; hle_pool_delete — DeletePool(A0): A0 must be a live 8-byte block (what
; CreatePool allocates), else the call is rejected like an invalid free.
; Walks every block from HEAP_FIRST to top and releases the used ones
; tagged with this pool, then frees the header.  A0 = NULL: no-op.
; ===========================================================================
hle_pool_delete:
        lda r_a0
        ora r_a0+1
        ora r_a0+2
        ora r_a0+3
        bne :+
        rts
:       jsr hm_state
        bcc :+
        rts
:       jsr hm_pool_hdr
        jcs hm_reject
        MOV3 hle_hpool, hle_ptr
        lda #<HEAP_FIRST
        sta hle_hw
        lda #>HEAP_FIRST
        sta hle_hw+1
        lda #^HEAP_FIRST
        sta hle_hw+2
@walk:  CMP3 hle_hw, hle_htop
        jcs @hdr
        AT hle_hw, 0
        jsr mem_read32
        lda m_val+3
        jne @hdr                ; corrupt header: stop the walk
        HDR0 hle_hs
        CMP3I hle_hs, HB_MIN
        jcc @hdr
        ADD3 hle_hwn, hle_hw, hle_hs
        CMP3 hle_htop, hle_hwn
        bcc @hdr
        lda hle_hf
        and #HB_USED
        beq @next
        AT hle_hw, HB_NEXT
        jsr mem_read32
        lda m_val+3
        cmp #HB_TAG
        bne @next
        lda m_val
        cmp hle_hpool
        bne @next
        lda m_val+1
        cmp hle_hpool+1
        bne @next
        lda m_val+2
        cmp hle_hpool+2
        bne @next
        MOV3 hle_hb, hle_hw
        jsr hm_release          ; hwn stays valid: merges never move it
@next:  MOV3 hle_hw, hle_hwn
        jmp @walk
@hdr:   jsr hm_pool_hdr         ; the header itself (validated again)
        jcc hm_release
        rts

hm_pool_hdr:                    ; hle_ptr := A0, hle_sz := 8, hm_check
        ldx #3
:       lda r_a0,x
        sta hle_ptr,x
        dex
        bpl :-
        lda #8
        sta hle_sz
        lda #0
        sta hle_sz+1
        sta hle_sz+2
        sta hle_sz+3
        jmp hm_check
