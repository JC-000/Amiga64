; ===========================================================================
; runtime/ops/hle.s — M2: high-level emulation of AmigaOS library calls
; Owner: WS-B1 (m2-exec).  Contract: docs/CONTRACTS.md, docs/WAVE4_BRIEF.md
; C2/C3/C4, docs/HLE.md (ABI, env map, function table).
;
; Entered from op_aline (core) for every $Axxx opcode except $AFFF, with the
; opcode word in op_lo/op_hi.  $A1xx = HLE call xx (host/hle_env.py plants
; these words in the library vector slots at base-LVO, so a guest
; `jsr -LVO(A6)` lands here with the return address already pushed on the
; guest stack).  $A130-$A14F go to hle_dos.s (WS-B2).  Any other $Axxx —
; including the $A0FF sentinel hle_env.py plants in every unimplemented
; vector slot — halts with reason HALT_ALINE (PC = slot + 2).
;
; Function set (indices mirrored by host/hle_env.py; table in docs/HLE.md):
;   $00 exec OpenLibrary  -552   $08 exec AllocMem     -198
;   $01 exec CloseLibrary -414   $09 exec FreeMem      -210
;   $02 dos  Output        -60   $0A exec SetSignal    -306
;   $03 dos  Write         -48   $0B exec CreatePool   -696
;   $04 dos  Input         -54   $0C exec DeletePool   -702
;   $05 dos  Read          -42   $0D exec AllocPooled  -708
;   $06 dos  IsInteractive -216  $0E exec FreePooled   -714
;   $07 exec FindTask     -294   $0F exec AllocVec     -684
;                                $10 exec FreeVec      -690
;                                $11 exec StackSwap    -732
;   $12 dos  GetVar       -906   $13 dos  DateStamp    -192
;   $14 dos  SystemTagList -606
;
; HLE state lives in GUEST memory (ENV_STATE, staged fresh by hle_env.py
; before every run): heap bump pointer + console cursor.  That is the
; per-run re-initialisation — no runner hook, no C64 BSS that survives
; between runs.  Handlers only use the memory API (never the REC).
;
; Every HLE call retires by emulating RTS: pop the return address from the
; guest stack (A7 += 4, PC = popped value masked to 24 bits), then
; jmp dispatch_next — so one $A1xx word counts as one guest instruction.
; Only D0 (result) is written; D1/A0/A1 are left as the caller set them
; (AmigaOS scratch), D2-D7/A2-A6 are preserved.
; ===========================================================================

.include "../macros.inc"

.export hle_aline

.import mem_read8, mem_read32, mem_write32
.import dispatch_next, emu_halt
.import hle_dos_dispatch        ; runtime/ops/hle_dos.s (WS-B2), indices $30-$4F
.import hle_dos_read, hle_dos_write     ; WS-B2 file-handle I/O (C2)

HLE_FN_COUNT = 21               ; valid indices $A100-$A114 (hle_env.HLE_FN_COUNT)
LIB_VERSION  = 40               ; OpenLibrary: newer versions -> NULL
HLE_DOS_FIRST = $30             ; $A130-$A14F: file DOS (hle_dos.s, WAVE4 C3)
HLE_DOS_LAST  = $4F

; Guest addresses (ENV_*, EXEC_BASE, DOS_BASE, FH_*, HEAP_*) and SCREEN come
; from the resource contract via ../macros.inc; host/hle_env.py mirrors them.

; AmigaOS structure offsets (dos/dosextens.h, exec/tasks.h)
PR_CIS       = 156              ; struct Process pr_CIS (BPTR), $9C
PR_COS       = 160              ; struct Process pr_COS (BPTR), $A0
TC_SPLOWER   = 58               ; struct Task tc_SPLower, $3A
TC_SPUPPER   = 62               ; struct Task tc_SPUpper, $3E
FH_ARGS      = 36               ; struct FileHandle fh_Args = handle kind (C2)
FH_KIND_NIL  = 2                ; 0 console out, 1 console in, 2 NIL:
FH_KIND_FILE0 = $10             ; >= $10: WS-B2 file slots -> hle_dos_*
ST_HEAP      = 0                ; ENV_STATE +0: u32 heap_next
ST_CON       = 4                ; ENV_STATE +4: u16 cursor, u8 column, u8 pad
HEAP_TOP     = HEAP_END + 1     ; first byte past the pool ($800000)

SCREEN_COLS  = 40
SCREEN_SIZE  = 1000             ; 40x25; the console scrolls at line 25

; --- File-local scratch (per call only; nothing here survives a run) --------
.segment "BSS"
hle_ptr:  .res 4                ; walking guest pointer / FreeMem address
hle_cnt:  .res 3                ; remaining byte count (Write) / longwords (clear)
hle_cur:  .res 2                ; console cursor 0-1000 (1000 = scroll pending)
hle_col:  .res 1                ; console column 0-39
hle_idx:  .res 1                ; OpenLibrary name-compare index
hle_sz:   .res 4                ; AllocMem/FreeMem size (rounded to 8)
hle_flg:  .res 1                ; AllocMem flags bits 16-23 (MEMF_CLEAR = bit 0)
hle_old:  .res 4                ; heap_next before the allocation = result
hle_tmp:  .res 4                ; AllocVec size / StackSwap scratch
hle_sa:   .res 3                ; hle_swap32 operand addresses (24-bit)
hle_sb:   .res 3

.segment "CODE"

; --- small helpers ------------------------------------------------------------
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

.macro D0_ZERO
        lda #0
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
.endmacro

.macro D0_MINUS1
        lda #$FF
        sta r_d0
        sta r_d0+1
        sta r_d0+2
        sta r_d0+3
.endmacro

; ===========================================================================
; hle_aline — dispatch on the A-line opcode word.
; ===========================================================================
hle_aline:
        lda op_hi
        cmp #$A1
        bne hle_trap
        lda op_lo
        cmp #HLE_DOS_FIRST      ; $30-$4F belong to hle_dos.s (WAVE4 C3)
        bcc @local
        cmp #HLE_DOS_LAST + 1
        bcs hle_trap
        jmp hle_dos_dispatch    ; op_lo = index, same entry state as here
@local: cmp #HLE_FN_COUNT
        bcs hle_trap
        tax
        lda hle_tab_lo,x
        sta hle_jv+1
        lda hle_tab_hi,x
        sta hle_jv+2
hle_jv: jmp $FFFF               ; self-modified (PRG is in RAM)

hle_trap:                       ; non-HLE A-line / unimplemented LVO sentinel
        lda #HALT_ALINE
        jmp emu_halt

; ===========================================================================
; $A100 OpenLibrary — name pointer in A1, version in D0.
; Compares the guest string against "dos.library" (exact, NUL included);
; match and D0 <= LIB_VERSION (40) -> D0 = DOSBase; anything else -> 0.
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
        D0_ZERO
        jmp hle_ret

@match:                         ; version asked (D0, unsigned) <= 40?
        lda r_d0+3
        ora r_d0+2
        ora r_d0+1
        bne @nomatch
        lda #LIB_VERSION
        cmp r_d0
        bcc @nomatch            ; 40 < D0
        lda #<DOS_BASE          ; D0 = DOSBase
        sta r_d0
        lda #>DOS_BASE
        sta r_d0+1
        lda #^DOS_BASE
        sta r_d0+2
        lda #0
        sta r_d0+3
        jmp hle_ret

; ===========================================================================
; $A101 CloseLibrary — library base in A1.  Nothing to release; all
; registers preserved (AmigaOS CloseLibrary returns nothing).
; ===========================================================================
hle_closelibrary:
        jmp hle_ret

; ===========================================================================
; $A102 Output / $A104 Input — D0 = the process's pr_COS / pr_CIS (a BPTR
; to a console FileHandle, staged by hle_env.py; contract C2).
; ===========================================================================
hle_output:
        SETADDR ENV_PROCESS, PR_COS
        jmp hle_d0_from_guest

hle_input:
        SETADDR ENV_PROCESS, PR_CIS
hle_d0_from_guest:              ; D0 := guest u32 at m_addr
        jsr mem_read32
        ldx #3
@c:     lda m_val,x
        sta r_d0,x
        dex
        bpl @c
        jmp hle_ret

; ===========================================================================
; hle_fhkind — m_val := fh_Args of the FileHandle whose BPTR is in D1.
; Returns Z=1 if D1 is 0 (no handle; m_val then undefined).  Clobbers A/X,
; m_addr/m_val; leaves every register and op_lo/op_hi untouched (C2).
; ===========================================================================
hle_fhkind:
        lda r_d1
        ora r_d1+1
        ora r_d1+2
        ora r_d1+3
        beq @out
        lda r_d1                ; m_addr := (D1 << 2) + FH_ARGS
        asl a
        sta m_addr
        lda r_d1+1
        rol a
        sta m_addr+1
        lda r_d1+2
        rol a
        sta m_addr+2
        lda #0
        sta m_addr+3
        asl m_addr
        rol m_addr+1
        rol m_addr+2
        clc
        lda m_addr
        adc #FH_ARGS
        sta m_addr
        bcc @rd
        inc m_addr+1
        bne @rd
        inc m_addr+2
@rd:    jsr mem_read32
        lda #1                  ; Z=0: handle present
@out:   rts

; hle_kindfile — after hle_fhkind: C=1 iff kind >= FH_KIND_FILE0 (any of
; the upper bytes set counts, e.g. WS-B2's closed marker $FFFFFFFF).
hle_kindfile:
        lda m_val+1
        ora m_val+2
        ora m_val+3
        bne @file
        lda m_val
        cmp #FH_KIND_FILE0      ; C = (kind >= $10)
        rts
@file:  sec
        rts

; ===========================================================================
; $A103 Write — fh BPTR in D1, buffer in D2, length in D3.
;   kind >= $10          -> jmp hle_dos_write (WS-B2), call state untouched
;   kind 0/1 (console)   -> text to the C64 screen, D0 = D3
;   kind 2 (NIL:)        -> discarded, D0 = D3
;   D1 = 0, kind 3-$0F, or D3 outside 0..$FFFFFF -> D0 = -1 (a length
;   of 16 MB or more cannot describe a buffer in the 24-bit guest space)
; Console: ASCII -> screen codes ($20-$3F direct, $40-$5F minus $40,
; $60-$7F minus $60, >= $80 '?'), $0A newline, other controls ignored,
; scrolls at line 25.
; ===========================================================================
hle_write:
        jsr hle_fhkind
        beq @fail
        jsr hle_kindfile
        bcc @local
        jmp hle_dos_write       ; WS-B2 file slot (C2)
@local: lda r_d3+3              ; negative or >= 16 MB length -> error
        bne @fail
        lda m_val
        cmp #FH_KIND_NIL
        beq @ok                 ; NIL: swallows everything
        bcs @fail               ; kinds 3-$0F: not a handle we know
        jsr hle_con_load
        ldx #3                  ; walking pointer := D2 (buffer)
@init:  lda r_d2,x
        sta hle_ptr,x
        dex
        bpl @init
        lda r_d3
        sta hle_cnt
        lda r_d3+1
        sta hle_cnt+1
        lda r_d3+2
        sta hle_cnt+2
@loop:  lda hle_cnt
        ora hle_cnt+1
        ora hle_cnt+2
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
@cnt:   lda hle_cnt             ; 24-bit count--
        bne @d0
        lda hle_cnt+1
        bne @d1
        dec hle_cnt+2
@d1:    dec hle_cnt+1
@d0:    dec hle_cnt
        jmp @loop
@done:  jsr hle_con_save
@ok:    ldx #3                  ; D0 = D3 (bytes written)
@ret:   lda r_d3,x
        sta r_d0,x
        dex
        bpl @ret
        jmp hle_ret
@fail:  D0_MINUS1
        jmp hle_ret

; ===========================================================================
; $A105 Read — fh BPTR in D1, buffer in D2, length in D3.
;   kind >= $10 -> jmp hle_dos_read (WS-B2), call state untouched
;   kind 0/1/2  -> D0 = 0 (EOF: no keyboard input in this version; NIL:)
;   D1 = 0 or kind 3-$0F -> D0 = -1
; ===========================================================================
hle_read:
        jsr hle_fhkind
        beq @fail
        jsr hle_kindfile
        bcc @local
        jmp hle_dos_read        ; WS-B2 file slot (C2)
@local: lda m_val
        cmp #FH_KIND_NIL + 1
        bcs @fail
        D0_ZERO
        jmp hle_ret
@fail:  D0_MINUS1
        jmp hle_ret

; ===========================================================================
; $A106 IsInteractive — fh BPTR in D1.  D0 = -1 (DOSTRUE) for the console
; kinds 0/1, else 0 (NIL:, files, no handle).  Not forwarded to WS-B2:
; file handles are never interactive.
; ===========================================================================
hle_isinteractive:
        jsr hle_fhkind
        beq @no
        jsr hle_kindfile
        bcs @no
        lda m_val
        cmp #FH_KIND_NIL
        bcs @no
        D0_MINUS1
        jmp hle_ret
@no:    D0_ZERO
        jmp hle_ret

; ===========================================================================
; $A107 FindTask — name in A1.  NULL -> D0 = our Process (ENV_PROCESS);
; any name -> D0 = 0 (there are no other tasks).
; ===========================================================================
hle_findtask:
        lda r_a1
        ora r_a1+1
        ora r_a1+2
        ora r_a1+3
        bne @none
        lda #<ENV_PROCESS
        sta r_d0
        lda #>ENV_PROCESS
        sta r_d0+1
        lda #^ENV_PROCESS
        sta r_d0+2
        lda #0
        sta r_d0+3
        jmp hle_ret
@none:  D0_ZERO
        jmp hle_ret

; ===========================================================================
; $A10A SetSignal — newSignals D0, mask D1.  No signals are ever raised
; (no Ctrl-C): D0 = 0 (old signal state), nothing stored.
; ===========================================================================
hle_setsignal:
        D0_ZERO
        jmp hle_ret

; ===========================================================================
; $A108 AllocMem — byteSize D0, requirements D1.  Bump allocation from the
; HLE heap [HEAP_BASE, HEAP_END]: size rounded up to 8 (MEM_BLOCKSIZE),
; result 8-byte aligned; MEMF_CLEAR (bit 16) zero-fills the rounded block;
; other flags are ignored (all guest memory is alike).  D0 = 0 for size 0
; or when the heap cannot hold the block.
; ===========================================================================
hle_allocmem:
        ldx #3
@c:     lda r_d0,x
        sta hle_sz,x
        dex
        bpl @c
        lda r_d1+2
        sta hle_flg
        jsr hle_alloc
hle_ret_old:                    ; D0 := hle_old (0 on failure)
        ldx #3
@r:     lda hle_old,x
        sta r_d0,x
        dex
        bpl @r
        jmp hle_ret

; ===========================================================================
; $A109 FreeMem — memoryBlock A1, byteSize D0.  Bump-allocator semantics:
; if the (8-rounded) block is the most recent allocation still live — it
; ends exactly at heap_next — heap_next drops back to its start (LIFO
; reclaim).  Anything else (NULL, not the top block, outside the heap) is
; accepted and leaked: memory is never reused while a live block could
; overlap it, so a FreeMem can never corrupt another allocation.
; ===========================================================================
hle_freemem:
        ldx #3
@c:     lda r_a1,x
        sta hle_ptr,x
        lda r_d0,x
        sta hle_sz,x
        dex
        bpl @c
        jsr hle_free
        jmp hle_ret

; ===========================================================================
; $A10B CreatePool — memFlags D0, puddleSize D1, threshSize D2.
; A pool is an 8-byte header allocated from the heap holding memFlags (so
; AllocPooled can honour MEMF_CLEAR); puddles are not modelled — every
; AllocPooled is a plain heap allocation.  threshSize > puddleSize -> 0
; (exec autodoc).  D0 = pool header or 0.
; ===========================================================================
hle_createpool:
        lda r_d1                ; unsigned: thresh (D2) > puddle (D1)?
        cmp r_d2
        lda r_d1+1
        sbc r_d2+1
        lda r_d1+2
        sbc r_d2+2
        lda r_d1+3
        sbc r_d2+3
        bcs @ok                 ; D1 >= D2
        D0_ZERO
        jmp hle_ret
@ok:    lda #8
        sta hle_sz
        lda #0
        sta hle_sz+1
        sta hle_sz+2
        sta hle_sz+3
        sta hle_flg             ; header itself: no clear needed
        jsr hle_alloc
        lda hle_old+2
        ora hle_old+1
        ora hle_old
        beq @out                ; out of heap -> D0 = 0
        ldx #3                  ; [header] := memFlags (D0)
@a:     lda hle_old,x
        sta m_addr,x
        lda r_d0,x
        sta m_val,x
        dex
        bpl @a
        jsr mem_write32
@out:   jmp hle_ret_old

; ===========================================================================
; $A10C DeletePool — poolHeader A0.  Releasing a pool frees all its
; allocations at once; with the bump heap that is a no-op (the memory is
; leaked until the next run re-stages ENV_STATE).
; ===========================================================================
hle_deletepool:
        jmp hle_ret

; ===========================================================================
; $A10D AllocPooled — poolHeader A0, memSize D0.  AllocMem(memSize,
; pool memFlags); D0 = 0 for a NULL pool.
; ===========================================================================
hle_allocpooled:
        lda r_a0
        ora r_a0+1
        ora r_a0+2
        ora r_a0+3
        bne @go
        D0_ZERO
        jmp hle_ret
@go:    ldx #3
@a:     lda r_a0,x
        sta m_addr,x
        lda r_d0,x
        sta hle_sz,x
        dex
        bpl @a
        jsr mem_read32          ; pool memFlags
        lda m_val+2
        sta hle_flg
        jsr hle_alloc
        jmp hle_ret_old

; ===========================================================================
; $A10E FreePooled — poolHeader A0, memory A1, memSize D0: FreeMem(A1, D0).
; ===========================================================================
hle_freepooled:
        jmp hle_freemem

; ===========================================================================
; $A10F AllocVec — byteSize D0, requirements D1 (exec V36).  As exec does:
; allocate byteSize+4 bytes, store that size in the first longword, return
; the address after it (so results are 8n+4).  0 on failure.
; ===========================================================================
hle_allocvec:
        lda r_d0+3              ; >= 16 MB can never fit (and D0+4 must
        bne @fail               ; not wrap)
        clc                     ; hle_sz = hle_tmp = D0 + 4 (24-bit + carry)
        lda r_d0
        adc #4
        sta hle_sz
        sta hle_tmp
        lda r_d0+1
        adc #0
        sta hle_sz+1
        sta hle_tmp+1
        lda r_d0+2
        adc #0
        sta hle_sz+2
        sta hle_tmp+2
        lda #0
        adc #0
        sta hle_sz+3            ; 1 only for D0 >= $FFFFFC: alloc rejects
        sta hle_tmp+3
        lda r_d1+2
        sta hle_flg
        jsr hle_alloc
        lda hle_old+2
        ora hle_old+1
        ora hle_old
        beq @fail
        ldx #3                  ; [block] := byteSize + 4
@w:     lda hle_old,x
        sta m_addr,x
        lda hle_tmp,x
        sta m_val,x
        dex
        bpl @w
        jsr mem_write32
        clc                     ; D0 = block + 4
        lda hle_old
        adc #4
        sta r_d0
        lda hle_old+1
        adc #0
        sta r_d0+1
        lda hle_old+2
        adc #0
        sta r_d0+2
        lda #0
        sta r_d0+3
        jmp hle_ret
@fail:  D0_ZERO
        jmp hle_ret

; ===========================================================================
; $A110 FreeVec — memoryBlock A1 (from AllocVec; NULL is a no-op).
; FreeMem(A1 - 4, [A1 - 4]).
; ===========================================================================
hle_freevec:
        lda r_a1
        ora r_a1+1
        ora r_a1+2
        ora r_a1+3
        beq @out
        sec                     ; hle_ptr := A1 - 4
        lda r_a1
        sbc #4
        sta hle_ptr
        sta m_addr
        lda r_a1+1
        sbc #0
        sta hle_ptr+1
        sta m_addr+1
        lda r_a1+2
        sbc #0
        sta hle_ptr+2
        sta m_addr+2
        lda r_a1+3
        sbc #0
        sta hle_ptr+3
        sta m_addr+3
        jsr mem_read32          ; stored size
        ldx #3
@c:     lda m_val,x
        sta hle_sz,x
        dex
        bpl @c
        jsr hle_free
@out:   jmp hle_ret

; ===========================================================================
; $A111 StackSwap — newStack A0 -> struct StackSwapStruct {stk_Lower,
; stk_Upper, stk_Pointer} (exec V37).  Exchanges the task's tc_SPLower/
; tc_SPUpper with stk_Lower/stk_Upper and A7 with stk_Pointer, returning on
; the NEW stack: the return address is popped from the old stack first and
; the old A7 saved (as exec does), so a second StackSwap with the same
; struct swaps back.  Retires without hle_ret (PC = popped address).
; ===========================================================================
hle_stackswap:
        ldx #3                  ; hle_old := return address, A7 += 4
@sp:    lda r_a7,x
        sta m_addr,x
        dex
        bpl @sp
        jsr mem_read32
        ldx #3
@ra:    lda m_val,x
        sta hle_old,x
        dex
        bpl @ra
        clc
        lda r_a7
        adc #4
        sta r_a7
        bcc @pop
        inc r_a7+1
        bne @pop
        inc r_a7+2
        bne @pop
        inc r_a7+3
@pop:   lda #<(ENV_PROCESS + TC_SPLOWER)       ; tc_SPLower <-> stk_Lower
        sta hle_sb
        lda #>(ENV_PROCESS + TC_SPLOWER)
        sta hle_sb+1
        lda #^(ENV_PROCESS + TC_SPLOWER)
        sta hle_sb+2
        lda #0
        jsr hle_sa_a0
        jsr hle_swap32
        lda #<(ENV_PROCESS + TC_SPUPPER)       ; tc_SPUpper <-> stk_Upper
        sta hle_sb
        lda #>(ENV_PROCESS + TC_SPUPPER)
        sta hle_sb+1
        lda #^(ENV_PROCESS + TC_SPUPPER)
        sta hle_sb+2
        lda #4
        jsr hle_sa_a0
        jsr hle_swap32
        lda #8                  ; A7 <-> stk_Pointer
        jsr hle_sa_a0
        jsr hle_setsa
        jsr mem_read32
        ldx #3
@np:    lda m_val,x
        sta hle_tmp,x
        lda r_a7,x
        sta m_val,x
        dex
        bpl @np
        jsr mem_write32         ; stk_Pointer := old A7 (m_addr unchanged)
        ldx #3
@na:    lda hle_tmp,x
        sta r_a7,x
        dex
        bpl @na
        sec                     ; [new A7 - 4] := return address, as exec
        lda r_a7                ; does (it returns with RTS on the new stack)
        sbc #4
        sta m_addr
        lda r_a7+1
        sbc #0
        sta m_addr+1
        lda r_a7+2
        sbc #0
        sta m_addr+2
        lda #0
        sta m_addr+3
        ldx #3
@rw:    lda hle_old,x
        sta m_val,x
        dex
        bpl @rw
        jsr mem_write32
        lda hle_old             ; PC = return address (24-bit)
        sta r_pc
        lda hle_old+1
        sta r_pc+1
        lda hle_old+2
        sta r_pc+2
        lda #0
        sta r_pc+3
        jmp dispatch_next

; ===========================================================================
; $A112 GetVar — name D1, buffer D2, size D3, flags D4 (dos V36).  There
; are no environment/local variables: D0 = -1 ("not found"), the buffer is
; not touched.  IoErr is NOT set: the file-DOS IoErr lives in WS-B2's
; hle_dos_ioerr (docs/HLE.md).
; $A114 SystemTagList — command D1, tags D2 (dos V36).  No shell: D0 = -1
; (the command could not be run), so system("cmd") returns -1.
; ===========================================================================
hle_getvar:
hle_systemtaglist:
        D0_MINUS1
        jmp hle_ret

; ===========================================================================
; $A113 DateStamp — struct DateStamp *ds in D1 (an address, not a BPTR).
; The HLE clock is stopped at ds_Days = ds_Minute = ds_Tick = 0
; (1978-01-01 00:00:00); D0 = D1.
; ===========================================================================
hle_datestamp:
        ldx #3
@c:     lda r_d1,x
        sta hle_ptr,x
        sta r_d0,x
        dex
        bpl @c
        lda #3
        sta hle_idx
@w:     ldx #3
@a:     lda hle_ptr,x
        sta m_addr,x
        lda #0
        sta m_val,x
        dex
        bpl @a
        jsr mem_write32
        clc                     ; next longword
        lda hle_ptr
        adc #4
        sta hle_ptr
        bcc @n
        inc hle_ptr+1
        bne @n
        inc hle_ptr+2
@n:     dec hle_idx
        bne @w
        jmp hle_ret

; hle_sa := A0 + A (A = 0..8), 24-bit
hle_sa_a0:
        clc
        adc r_a0
        sta hle_sa
        lda r_a0+1
        adc #0
        sta hle_sa+1
        lda r_a0+2
        adc #0
        sta hle_sa+2
        rts

hle_setsa:                      ; m_addr := hle_sa
        lda hle_sa
        sta m_addr
        lda hle_sa+1
        sta m_addr+1
        lda hle_sa+2
        sta m_addr+2
        lda #0
        sta m_addr+3
        rts

hle_setsb:                      ; m_addr := hle_sb
        lda hle_sb
        sta m_addr
        lda hle_sb+1
        sta m_addr+1
        lda hle_sb+2
        sta m_addr+2
        lda #0
        sta m_addr+3
        rts

; hle_swap32 — exchange the guest longwords at hle_sa and hle_sb.
hle_swap32:
        jsr hle_setsa
        jsr mem_read32
        ldx #3
@a:     lda m_val,x
        sta hle_tmp,x
        dex
        bpl @a
        jsr hle_setsb
        jsr mem_read32          ; m_val = [sb]
        jsr hle_setsa
        jsr mem_write32         ; [sa] := old [sb]
        ldx #3
@b:     lda hle_tmp,x
        sta m_val,x
        dex
        bpl @b
        jsr hle_setsb
        jmp mem_write32         ; [sb] := old [sa]

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

; ===========================================================================
; Console state: hle_con_load / hle_con_save move the cursor between the
; guest ENV_STATE block (u16 cursor BE, u8 column) and hle_cur/hle_col.
; A corrupt cursor (> 1000 or column > 39) restarts at the top left.
; ===========================================================================
hle_con_load:
        SETADDR ENV_STATE, ST_CON
        jsr mem_read32          ; BE cur_hi cur_lo col pad -> m_val+3..0
        lda m_val+2
        sta hle_cur
        lda m_val+3
        sta hle_cur+1
        lda m_val+1
        sta hle_col
        cmp #SCREEN_COLS
        bcs @bad
        lda hle_cur+1           ; cursor <= 1000?
        cmp #>SCREEN_SIZE
        bcc @ok
        bne @bad
        lda #<SCREEN_SIZE
        cmp hle_cur
        bcs @ok
@bad:   lda #0
        sta hle_cur
        sta hle_cur+1
        sta hle_col
@ok:    rts

hle_con_save:
        SETADDR ENV_STATE, ST_CON
        lda hle_cur+1
        sta m_val+3
        lda hle_cur
        sta m_val+2
        lda hle_col
        sta m_val+1
        lda #0
        sta m_val
        jmp mem_write32

; ===========================================================================
; hle_putc — A = ASCII byte.  Convert to a screen code and store at the
; cursor.  $0A advances to the next 40-column line; other control bytes are
; ignored; >= $80 prints '?'.  Scrolling is lazy: a line that ends at the
; bottom leaves the cursor at 1000 ("pending") and the screen scrolls only
; when the next character or newline needs the row.  Clobbers A/X/Y and
; t_tmp+4/5 (ZP pointer; the memory API only uses t_tmp+0..3 and is not
; called from here).
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

@store: pha
        jsr hle_ensure_row
        lda hle_cur             ; t_tmp+4/5 := SCREEN + cursor
        sta t_tmp+4             ; (<SCREEN = 0, so lo byte = cursor lo)
        lda hle_cur+1
        clc
        adc #>SCREEN
        sta t_tmp+5
        pla
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
        jsr hle_ensure_row
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

; hle_ensure_row — if the cursor is past the last row (1000), scroll the
; screen up one line and move the cursor to the start of row 24.
hle_ensure_row:
        lda hle_cur+1
        cmp #>SCREEN_SIZE
        bcc @ok
        bne @scroll
        lda hle_cur
        cmp #<SCREEN_SIZE
        bcc @ok
@scroll:
        ldx #0                  ; rows 1-24 -> 0-23: 960 bytes as four
@mv0:   lda SCREEN+40,x         ; 240-byte forward copies, run strictly in
        sta SCREEN,x            ; order (a later block's source must not be
        inx                     ; overwritten before it is read)
        cpx #240
        bne @mv0
        ldx #0
@mv1:   lda SCREEN+280,x
        sta SCREEN+240,x
        inx
        cpx #240
        bne @mv1
        ldx #0
@mv2:   lda SCREEN+520,x
        sta SCREEN+480,x
        inx
        cpx #240
        bne @mv2
        ldx #0
@mv3:   lda SCREEN+760,x
        sta SCREEN+720,x
        inx
        cpx #240
        bne @mv3
        ldx #SCREEN_COLS - 1    ; blank row 24
        lda #$20
@bl:    sta SCREEN+SCREEN_SIZE-SCREEN_COLS,x
        dex
        bpl @bl
        lda #<(SCREEN_SIZE - SCREEN_COLS)
        sta hle_cur
        lda #>(SCREEN_SIZE - SCREEN_COLS)
        sta hle_cur+1
        lda #0
        sta hle_col
@ok:    rts

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
; one handler per HLE index $00..HLE_FN_COUNT-1, in index order
hle_tab_lo:
        .byte <hle_openlibrary, <hle_closelibrary, <hle_output, <hle_write
        .byte <hle_input, <hle_read, <hle_isinteractive, <hle_findtask
        .byte <hle_allocmem, <hle_freemem, <hle_setsignal, <hle_createpool
        .byte <hle_deletepool, <hle_allocpooled, <hle_freepooled
        .byte <hle_allocvec, <hle_freevec, <hle_stackswap
        .byte <hle_getvar, <hle_datestamp, <hle_systemtaglist
hle_tab_hi:
        .byte >hle_openlibrary, >hle_closelibrary, >hle_output, >hle_write
        .byte >hle_input, >hle_read, >hle_isinteractive, >hle_findtask
        .byte >hle_allocmem, >hle_freemem, >hle_setsignal, >hle_createpool
        .byte >hle_deletepool, >hle_allocpooled, >hle_freepooled
        .byte >hle_allocvec, >hle_freevec, >hle_stackswap
        .byte >hle_getvar, >hle_datestamp, >hle_systemtaglist
.assert * - hle_tab_hi = HLE_FN_COUNT, error, "hle_tab_hi size != HLE_FN_COUNT"
dos_name:
        .byte "dos.library", 0
