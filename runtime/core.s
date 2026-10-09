; ===========================================================================
; runtime/core.s — Amiga64 interpreter core (68000 user-mode subset on 6502)
; Owner: core agent.  Contract: docs/CONTRACTS.md (ZP map, ABI, dispatch).
;
; Conventions
;   - All multi-byte ZP values little-endian; the memory API converts to/from
;     guest big-endian at the REU boundary.
;   - Guest memory = REU, guest address A -> REU offset A (1:1, 24-bit).
;   - t_tmp ($70-$77) is free scratch; the memory API uses $70-$73 as its
;     REU DMA bounce buffer, fetch_word mirrors the fetched word to
;     t_tmp+0(lo)/t_tmp+1(hi) on return.
;   - $7A-$7F are interpreter-internal (ea_kind/ea_reg/ea_size/st_step/
;     w_zacc/w_ptmp) — opaque to group handlers.
;   - ea_calc/ea_load and the extension-word fetches CLOBBER op_lo/op_hi:
;     handlers must extract all opcode fields they need BEFORE fetching
;     extension words or calling ea_calc.
;   - Instruction-stream window: fetch_word serves words from a 256-byte
;     page-aligned C64-RAM copy (code_win) of the guest page PC is in,
;     refilled by ONE 256-byte REU DMA on a miss.  Pure fetch cache: data
;     reads still go to the REU; guest writes that overlap the cached page
;     invalidate it (see WINCHK_*), as does emu_reset.  Build with
;     FETCH_DIRECT defined to get the old one-DMA-per-word path (A/B
;     reference); WINDOW_NOINVAL (test-only!) disables write invalidation.
;   - REC shadow (docs/WAVE4_BRIEF.md C1): the data memory API keeps a copy
;     of the REC registers it programmed (m_rlen/m_rlo/m_rhi/m_rbk + the
;     validity flag m_rok) and issues every data DMA with AUTOLOAD, so the
;     REC holds exactly those values between calls and a warm call only
;     stores the registers that differ.  Anything else that touches the
;     REC calls mem_rec_dirty (the window refill does it inline).
;     REC_NOINVAL (test-only!) empties every invalidation -- used to prove
;     the C1 teeth test (tests/rec_teeth.py) catches a missing one; the
;     finer test-only mutants REC_MUT_DIRTY / REC_MUT_RESET / REC_MUT_REFILL
;     drop just the mem_rec_dirty body / the emu_reset / the window-refill
;     invalidation, one leg of that test per mutant.
; ===========================================================================

.include "macros.inc"

; ---------------------------------------------------------------------------
; Hot-path page-cross guards (docs/PERF.md (f)).  A taken 6502 branch whose
; target lies in another page costs +1 cycle; on the fetch/dispatch, memory
; API, EA and ALU/CCR paths that is per guest instruction.  BRNC op, target
; assembles to exactly `op target` plus a LINK-time assert that the taken
; path stays in the branch's page, so any layout change that makes a hot
; branch cross fails the production build.  HOTALIGN n is never-executed $EA
; fill to an n-byte boundary; boundaries are absolute because core.s's CODE
; starts page-aligned (asserted at core_code0).  Test-only variant builds
; (FETCH_DIRECT A/B reference, REC_*/WINDOW_NOINVAL/EXC_MUT_* teeth builds)
; lay out differently by design and only warn.
; ---------------------------------------------------------------------------
.if .defined(FETCH_DIRECT) .or .defined(REC_NOINVAL) .or .defined(REC_MUT_DIRTY) .or .defined(REC_MUT_RESET) .or .defined(REC_MUT_REFILL) .or .defined(WINDOW_NOINVAL) .or .defined(BRNC_WARN_ONLY)
.macro BRNC op, target
        op target
        .assert >* = >(target), ldwarning, "hot-path branch crosses a page (PERF.md (f))"
.endmacro
emu_brnc_enforced = 0           ; @value guards only warn (variant/mutant build)
.else
emu_brnc_enforced = 1           ; @value guards fail the link (production/gate)
.macro BRNC op, target
        op target
        .assert >* = >(target), lderror, "hot-path branch crosses a page (PERF.md (f))"
.endmacro
.endif
.export emu_brnc_enforced       ; checked by tests/run_super_vectors.py, perf_cycles
.macro HOTALIGN n
        .align n, $EA
.endmacro
.macro CW_INC32 c               ; W12-C counter build only
        .local done
        inc c
        bne done
        inc c+1
        bne done
        inc c+2
        bne done
        inc c+3
done:
.endmacro

; --- Contract ABI exports --------------------------------------------------
.export emu_reset, emu_step, emu_run, dispatch_next, fetch_word
.export mem_read8, mem_read16, mem_read32
.export mem_write8, mem_write16, mem_write32
.export mem_rec_dirty           ; REC-ownership seam (docs/WAVE4_BRIEF.md C1)
.export m_scmd                  ; W7-T: chip_split reads mio_split's command
.import __BSS_RUN__, __BSS_SIZE__   ; ld65 (c64.cfg BSS define = yes): boot clear
.export ea_calc, ea_load, ea_store
.export alu_add, alu_sub, alu_cmp, alu_and, alu_or, alu_eor
.export alu_not, alu_neg, alu_addx, alu_subx, alu_ext
.export op_aline, op_fline, op_illegal
.export emu_halt                ; convenience: A = HALT_* reason -> halt
; Supervisor mode & exceptions (docs/CONTRACTS.md, W6-S)
.export exc_group1, exc_group2  ; A = vector: raise (stacked PC = insn / next)
.export sr_write, sp_swap       ; full-SR write with SP banking / A7<->r_osp
.export emu_stop, emu_addrerr   ; halts with HALT_STOP / HALT_ADDRESS
.export emu_rst_hook            ; RESET hook (patchable jmp)
.export code_invall, code_invpg ; W12-C code-window cache (CONTRACTS "Code-window cache")
.ifndef FETCH_DIRECT
.export cw_tlo, cw_thi, cw_val, cw_hand, cw_mok  ; W12-C host-visible (tests/run_cwin.py)
.export CWIN_N, cwin_map        ; W12-C
.ifdef CWIN_COUNT
.export cw_nins, cw_nmis, cw_nref, cw_nkil, cw_nslw ; W12-C counter build
.endif
.endif

; ZP map / constants re-exported so they land in build/runner.lbl for the
; host harness.  Group agents: use runtime/macros.inc, do NOT .import these.
.exportzp r_d0, r_d1, r_d2, r_d3, r_d4, r_d5, r_d6, r_d7
.exportzp r_a0, r_a1, r_a2, r_a3, r_a4, r_a5, r_a6, r_a7
.exportzp r_pc, r_sr, op_lo, op_hi, m_addr, m_val
.exportzp t_ea, t_op1, t_op2, t_res, t_tmp, st_halt, st_reason
.exportzp m_rok                 ; label only: tests/rec_apisweep.py split-path check
.exportzp st_exc, r_osp, st_ipc, st_iop, st_sp6, st_exl
.export HALT_OK, HALT_ALINE, HALT_FLINE, HALT_ILLEGAL
.export HALT_STOP, HALT_ADDRESS

; Halt reasons added by W6-S (macros.inc holds $00-$03 and is frozen).
HALT_STOP    = $04      ; @value STOP #imm executed (exception mode)
HALT_ADDRESS = $05      ; @value odd PC from a vector / RTE (reason code only)
.export buf_case, buf_result

; --- Imports ---------------------------------------------------------------
.import dispatch_lo, dispatch_hi        ; generated: build/gen/dispatch.s
.import __DISPATCH_RUN__                ; Wave 13 (supervisor): DATA + LOWCODE share the slack before it
.assert __DISPATCH_RUN__ = $0900, lderror, "DATA/LOWCODE pushed DISPATCH off $0900"
.import main                            ; runner entry (tests/runner owns it)
.import hle_aline                       ; HLE A-line dispatch (runtime/ops/hle.s or gen stub)
.import chip_r8, chip_r16, chip_r32     ; W7-T I/O trap targets (runtime/ops/chip.s)
.import chip_w8, chip_w16, chip_w32, chip_split

; ===========================================================================
; Instruction-stream window state (core-internal; linker-assigned, outside
; the $10-$7F contract register file).  W12-C: w_pg1:w_pg0 (guest address
; bits 23-8) and w_inv describe the CURRENT window of the code-window cache
; (the one the hit-path loads point at; see "W12-C code-window cache" at
; the end of this file); w_inv nonzero forces the next fetch to miss.  An
; (undefined-behavior) odd-PC fetch at page offset $FF reads byte 256 of
; the window: the next window's byte 0, or cwin_map[0] for the last one --
; allocated memory, never a neighboring BSS variable.
; ===========================================================================
.segment "ZEROPAGE" : zeropage
w_pg0:  .res 1                  ; window guest page, low  (addr bits 15-8)
w_pg1:  .res 1                  ; window guest page, high (addr bits 23-16)
w_inv:  .res 1                  ; 0 = window valid, nonzero = invalid
; REC shadow (see the memory API below).  m_rok = 0: REC contents unknown,
; the next data call programs every register; nonzero: the REC holds
; C64 address t_tmp, length-high 0, address control 0 and the shadowed
; bytes below (true after any data DMA because it autoloads).
m_rok:  .res 1                  ; REC shadow valid flag
m_rlen: .res 1                  ; REC_LENLO   as last programmed (1/2/4)
m_rlo:  .res 1                  ; REC_REULO   as last programmed
m_rhi:  .res 1                  ; REC_REUHI   as last programmed
m_rbk:  .res 1                  ; REC_REUBANK as last programmed

; W12-C: the windows themselves live in BHI (cwin_base, rc ram.inc), so
; code_win names window 0, the assembled operand of the four hit-path loads
; that cw_sel re-points (high byte) at the selected window.
code_win = cwin_base            ; W12-C

.ifndef FETCH_DIRECT            ; W12-C cache directory (MAIN BSS)
.ifndef CWIN_N
CWIN_N = 7                      ; @value W12-C windows (1..7: cwin_map counts in 3 bits)
.endif
.assert CWIN_N >= 1, error, "W12-C CWIN_N: at least one window"
.assert CWIN_N <= 7, error, "W12-C CWIN_N: 1..7 windows fit $A900-$AFFF"
.segment "BSS"
cw_tlo: .res CWIN_N             ; W12-C window tag, guest address bits 15-8
cw_thi: .res CWIN_N             ; W12-C window tag, guest address bits 23-16
cw_val: .res CWIN_N             ; W12-C nonzero = window holds its tag's page
cw_hand: .res 1                 ; W12-C next window to refill (FIFO, 0..CWIN_N-1)
cw_mok: .res 1                  ; W12-C nonzero = cwin_map cleared since boot
cw_ys:  .res 1                  ; W12-C Y saved by the miss path / code_invpg
cw_xs:  .res 1                  ; W12-C X saved by the write-check slow path
cw_pg:  .res 2                  ; W12-C code_invpg: page (bits 15-8, 23-16)
.ifdef CWIN_COUNT               ; W12-C counter build (tests/run_cwin.py)
cw_nins: .res 4                 ; W12-C instructions (exception-mode loop)
cw_nmis: .res 4                 ; W12-C miss-path entries (page change/invalid)
cw_nref: .res 4                 ; W12-C refills (one 256-byte DMA each)
cw_nkil: .res 4                 ; W12-C windows dropped by a write
cw_nslw: .res 4                 ; W12-C write checks that took the slow path
.endif
.endif

; ===========================================================================
; PRG load address + BASIC SYS stub
; ===========================================================================
.segment "LOADADDR"
        .word $0801

.segment "STARTUP"
basic_stub:                             ; 10 SYS 2061
        .word @next                     ; line link
        .word 10                        ; line number
        .byte $9E, "2061", $00          ; SYS 2061 ($080D)
@next:  .word $0000                     ; end of program
        lda #0                          ; $080D: ZP comes up with garbage --
        sta m_rok                       ; REC shadow invalid at boot (always,
                                        ; even in REC_NOINVAL builds)
        sta st_exc                      ; exceptions off (emu_reset keeps it)
        sta st_exl                      ; latched copy matches the plain loop
        sta r_osp                       ; inactive SP = 0
        sta r_osp+1
        sta r_osp+2
        sta r_osp+3
        ; Clear BSS.  C64 RAM survives a PRG reload on hardware, and HLE
        ; state in BSS trusts it (hle_dos_magic "DOS!", dosio_want_uci
        ; "UCI!"), so stale bytes must never reach a run.  BSS is
        ; page-aligned (c64.cfg); whole pages are cleared, the tail of the
        ; last page is unused MAIN space.  The self-modified page operand is
        ; reset first so a restart at $080D works too.  tests/bss_boot.py.
        tay                             ; A = 0, Y = 0
        lda #>__BSS_RUN__
        sta @pg+2
        tya
        ldx #>(__BSS_SIZE__ + $FF)      ; page count (rounded up)
        beq @done
@pg:    sta __BSS_RUN__,y               ; high byte self-modified
        iny
        bne @pg
        inc @pg+2
        dex
        bne @pg
@done:  jmp main

.segment "CODE"
core_code0:                     ; first CODE byte of the PRG (core.s links first)
.assert <core_code0 = 0, lderror, "core.s CODE must start page-aligned (HOTALIGN, PERF.md (f))"

; ===========================================================================
; emu_reset — clear interpreter state ($10-$7F), st_halt = 0, and invalidate
; the instruction window (the harness rewrites guest code at the same guest
; addresses between runs, bypassing or preceding the interpreter's own
; write-invalidation) and the REC shadow (C1: host tooling may have left
; the REC in any state).
;
; It also LATCHES st_exc into st_exl (docs/CONTRACTS.md "Supervisor mode &
; exceptions"), which is the only copy the runtime ever tests:
; the three step-loop entry points (emu_step, emu_run and dispatch_next's
; run-mode branch) are patched to the exception-mode loop dispatch_gox when
; st_exc != 0 and back to the plain loop otherwise, so the flag costs the
; user-mode hot path nothing.  st_exc and r_osp ($88-$8C) are NOT cleared.
; ===========================================================================
.proc emu_reset
        lda #0
        ldx #$6F
@lp:    sta $10,x
        dex
        bpl @lp
.if .not (.defined(REC_NOINVAL) .or .defined(REC_MUT_RESET))
        sta m_rok               ; C1: emu_reset implies mem_rec_dirty
.endif
        jsr code_invall         ; W12-C every window (was: w_inv := 1)
        lda st_exc              ; latch: the loop patch below AND st_exl, which
        sta st_exl              ; every runtime flag test reads -- so a write to
        beq @plain              ; st_exc between resets changes nothing (F2)
        lda #<dispatch_gox      ; exception mode
        ldx #>dispatch_gox
        ldy #<(dispatch_gox - (nx_br + 2))
        bne @patch              ; always (offset asserted nonzero)
@plain: lda #<dispatch_go
        ldx #>dispatch_go
        ldy #<(dispatch_go - (nx_br + 2))
@patch: sta step_jv+1
        sta run_jv+1
        stx step_jv+2
        stx run_jv+2
        sty nx_br+1
        rts
.endproc

; ===========================================================================
; Step loop / dispatch
;
; emu_step: execute exactly one guest instruction (returns with st_halt set
;           if it halted/trapped).
; emu_run:  loop until st_halt != 0.
; dispatch_next: handlers jmp here when done retiring an instruction.
;
; Dispatch mechanism: two 256-byte tables (dispatch_lo/dispatch_hi, page-
; aligned in segment DISPATCH) indexed by op_hi, patched into a self-
; modifying "jmp abs".  Cost of the select itself:
;     ldx op_hi (3) + 2x lda abs,x (4+4) + 2x sta abs (4+4) + jmp abs (3)
;   = 22 cycles.  The opcode fetch inlines fetch_word's window hit path
; (saves the jsr/rts): 51 cycles on a hit.  Full per-instruction loop
; overhead (run mode) excluding the handler body: dispatch_next re-check
; (11) + halt check (5) + inlined fetch hit (51) + select (22) ~= 90
; cycles (was ~190 with the direct one-DMA-per-word fetch; a window miss
; adds a ~380-cycle refill, amortized over up to 128 in-page words).
; ===========================================================================
emu_step:
        lda #1                  ; 2
        sta st_step             ; 3
step_jv:
        jmp dispatch_go         ; 3 (was "bne dispatch_go", also 3): target
                                ;   patched by emu_reset (st_exc latch)
emu_run:
        lda #0
        sta st_step
run_jv: jmp dispatch_go         ; patched by emu_reset (st_exc latch)
dispatch_go:
        lda st_halt             ; 3
        BRNC bne, dispatch_done       ; 2 (not taken)
.ifdef FETCH_DIRECT
        jsr fetch_word          ; op_lo/op_hi <- (PC), PC += 2
.else
        ; inlined fetch_word window hit path (must match the routine below)
        lda r_pc+1              ; 3   PC page == window page, window valid?
        eor w_pg0               ; 3
        BRNC bne, dg_miss             ; 2
        lda r_pc+2              ; 3
        eor w_pg1               ; 3
        ora w_inv               ; 3   0 only if both pages match AND valid
        BRNC bne, dg_miss             ; 2
        ldx r_pc                ; 3   in-window offset = PC low byte
dg_w0:  lda code_win,x          ; 4   guest [PC]   = opcode high byte (W12-C: high byte patched by cw_sel)
        sta op_hi               ; 3
        sta t_tmp+1             ; 3
dg_w1:  lda code_win+1,x        ; 4   guest [PC+1] = opcode low byte (W12-C: patched)
        sta op_lo               ; 3
        sta t_tmp               ; 3
        inx                     ; 2   PC += 2
        inx                     ; 2
        stx r_pc                ; 3
        BRNC beq, dg_wrap             ; 2   PC crossed into the next page
.endif
dispatch_sel:
        ldx op_hi               ; 3
        lda dispatch_lo,x       ; 4 (tables page-aligned: never +1)
        sta jvec+1              ; 4
        lda dispatch_hi,x       ; 4
        sta jvec+2              ; 4
jvec:   jmp $FFFF               ; 3 (self-modified)

.ifndef FETCH_DIRECT
dg_wrap:                        ; PC page-cross retire (~1 in 128 fetches)
        inc r_pc+1
        BRNC bne, dispatch_sel
        inc r_pc+2
        jmp dispatch_sel
dg_miss:
        jsr fetch_word          ; miss/invalid: refill the window and serve
        jmp dispatch_sel
.endif

dispatch_next:                  ; <- handlers return here
        lda st_halt             ; 3
        BRNC bne, dispatch_done       ; 2
        lda st_step             ; 3
nx_br:  BRNC beq, dispatch_go         ; 3: run mode -> next instruction (offset
                                ;   patched by emu_reset: _go or _gox)
dispatch_done:
        rts

; ---------------------------------------------------------------------------
; dispatch_gox — the exception-mode step loop (entered only when emu_reset
; latched st_exc != 0).  Same contract as dispatch_go, plus a snapshot of
; the instruction's start taken before the opcode fetch: its PC (st_ipc,
; the stacked PC of group-1 exceptions), the opcode (st_iop, which the
; exception decode in emu_halt needs after handlers clobbered op_lo/op_hi)
; and the 6502 stack pointer (st_sp6: exc_raise unwinds to it, so an
; exception raised inside a nested jsr -- e.g. op_illegal from ea_calc --
; never returns into the half-done handler).  ~60 cycles more than the
; plain loop; the plain loop itself is untouched.
; ---------------------------------------------------------------------------
dispatch_gox:
        lda st_halt
        bne dispatch_done
        tsx
        stx st_sp6
        lda r_pc
        sta st_ipc
        lda r_pc+1
        sta st_ipc+1
        lda r_pc+2
        sta st_ipc+2
        lda r_pc+3
        sta st_ipc+3
.ifdef CWIN_COUNT               ; W12-C counter build: instructions
        inc cw_nins
        bne :+
        inc cw_nins+1
        bne :+
        inc cw_nins+2
        bne :+
        inc cw_nins+3
:
.endif
        jsr fetch_word
        lda op_lo
        sta st_iop
        lda op_hi
        sta st_iop+1
        jmp dispatch_sel

.assert dispatch_gox - (nx_br + 2) < $80, error, "nx_br cannot reach dispatch_gox"
.assert dispatch_go - (nx_br + 2) >= -$80, error, "nx_br cannot reach dispatch_go"
.assert dispatch_gox - (nx_br + 2) <> 0, error, "emu_reset patch uses bne"

; ===========================================================================
; fetch_word — read guest big-endian word at PC into op_lo/op_hi (mirrored
; to t_tmp+0=lo/t_tmp+1=hi), PC += 2.  Handlers use it for extension words.
;
; Windowed build (default): serves from the current window when PC is in
; its page; else the miss path (W12-C: cw_look) switches to the cached
; window of PC's page or refills one with a 256-byte REU DMA, then serves.
; An instruction or extension-word run may span a page end: each word is
; fetched independently at an even PC (bytes X and X+1, X <= $FE, inside
; the window), and the first word of the next page misses and comes from
; that page's own window -- no window needs a 257th byte.
; PC is architecturally even (odd PC = 68000 address error, outside the
; M1 subset); an odd PC at page offset $FF would fetch byte 256 (defined
; memory, no crash) and wrap PC within the page — harmless UB in an
; already-undefined state.
; Clobbers: A, X, t_tmp+0/+1 (m_addr/m_val NOT touched, unlike the direct
; build).  Cycles: hit 57 (+6 jsr); a window switch ~130, a refill ~590
; (DMA included; PERF.md (n)).
;
; FETCH_DIRECT build: the v0 path, one 2-byte REU DMA per word.
; Clobbers: A, m_addr, m_val, t_tmp.  ~150 cycles (incl. REU DMA).
; ===========================================================================
.ifdef FETCH_DIRECT
.proc fetch_word
        lda r_pc                ; m_addr = PC
        sta m_addr
        lda r_pc+1
        sta m_addr+1
        lda r_pc+2
        sta m_addr+2
        lda r_pc+3
        sta m_addr+3
        clc                     ; PC += 2 (bits 24-31 stay 0)
        lda r_pc
        adc #2
        sta r_pc
        BRNC bcc, @nc
        inc r_pc+1
        BRNC bne, @nc
        inc r_pc+2
@nc:    jsr mem_read16
        lda m_val
        sta op_lo
        sta t_tmp
        lda m_val+1
        sta op_hi
        sta t_tmp+1
        rts
.endproc
.else
.proc fetch_word
        lda r_pc+1              ; 3   PC page == window page, window valid?
        eor w_pg0               ; 3
        BRNC bne, miss                ; 2
        lda r_pc+2              ; 3
        eor w_pg1               ; 3
        ora w_inv               ; 3   0 only if both pages match AND valid
        BRNC bne, miss                ; 2
serve:  ldx r_pc                ; 3   in-window offset = PC low byte
fw_w0:  lda code_win,x          ; 4   guest [PC]   = high byte (W12-C: high byte patched by cw_sel)
        sta op_hi               ; 3
        sta t_tmp+1             ; 3
fw_w1:  lda code_win+1,x        ; 4   guest [PC+1] = low byte (W12-C: patched)
        sta op_lo               ; 3
        sta t_tmp               ; 3
        inx                     ; 2   PC += 2
        inx                     ; 2
        stx r_pc                ; 3
        BRNC beq, wrap                ; 2   PC crossed into the next page
        rts                     ; 6   hit total: 57
wrap:   inc r_pc+1
        BRNC bne, wdone
        inc r_pc+2
wdone:  rts

miss:                           ; W12-C: page change or dropped window
.ifndef CWIN_MUT_NOBANK
        lda CPU_PORT            ; W12-C BASIC ROM visible (LORAM and HIRAM
        and #3                  ; W12-C set)?  The windows and cwin_map are
        cmp #3                  ; W12-C the RAM under it: bank it out and
        bne nobas               ; W12-C leave it out (CONTRACTS "Code-window
        dec CPU_PORT            ; W12-C cache"); $x7 -> $x6, as blt_w
nobas:
.endif
        jmp cw_look             ; W12-C the lookup (CWCODE)
.endproc
.endif

; ===========================================================================
; Memory API — one REU DMA per call through the REC at $DF00, with a REC
; register shadow.
; Guest 24-bit address in m_addr (LE, byte 3 ignored), value in m_val (LE).
; Big-endian conversion happens here: the DMA bounce buffer is t_tmp+0..3
; (holds guest byte order), m_val holds LE.
; Requires I/O mapped in at $DF00 (default bank state; core never touches $01).
;
; REC shadow (lever d.2, docs/PERF.md; ownership contract C1 in
; docs/WAVE4_BRIEF.md).  On the U64E every REC register access costs ~1 us
; at turbo regardless of CPU clock (docs/HARDWARE.md finding 2), so the
; data path programs only what changed:
;   - every data DMA is issued with AUTOLOAD ($20): afterwards the REC
;     reloads the address/length registers with the values last WRITTEN,
;     so the REC state between calls is exactly the programmed one
;     (verified on VICE and on the U64E by tests/bench/rec_probe.py);
;   - C64 address (= t_tmp), length-high (0) and address control (0) are
;     the same for every data call: written only by the cold path;
;   - length-low / REU address low / high / bank are compared against the
;     shadow (m_rlen/m_rlo/m_rhi/m_rbk) and stored only when different.
; REC accesses per call: cold (m_rok = 0) 9; warm 1 (same address and
; size, e.g. the store half of a read-modify-write) to 5 (everything
; differs); typical warm in-page call 2.  Anything else that programs the
; REC must call mem_rec_dirty before the next call here (C1).
; Cost per warm in-page call 47 cycles of REC setup (v0 ~59 incl. jsr),
; +7 for the 512K-straddle test on 16/32-bit calls, +1 cycle/byte DMA +
; copies; writes add the WINCHK_* window-overlap test (9-16 cycles).
;
; 512 KB straddles.  The REC's REU address counter carries only through
; bits 0-18 (a 512 KB 1750-style counter; VICE reu.c and the U64E alike,
; tests/bench/rec_probe.py): a DMA that runs past $x7FFFF continues at
; $x00000, not $(x+8)0000.  A 16/32-bit access whose bytes straddle such
; a line (guest address bits 18-0 >= $80000 - (len-1)) therefore goes to
; mio_split, which moves it one byte per DMA with the CPU doing the
; 24-bit address increment (so $FFFFFF wraps to $000000, as the 68000's
; 24-bit bus does).  Bank carries INSIDE a 512 KB block ($01FFFF ->
; $020000) are done correctly by the REC and autoload restores the bank
; register (probed), so those stay on the fast path.  Every other DMA in
; the tree is either 1 byte (test_load_chunk staging, mem_write8/read8)
; or a page-aligned 256-byte window refill -- neither can straddle.
;
; I/O trap (W7-T, docs/S3_PLAN.md §1.2-1.3, CONTRACTS.md "I/O trap").
; Accesses whose bank is $DF (custom chips) or $BF (CIAs) go to
; runtime/ops/chip.s instead of the REU.  The test sits only where the
; bank is about to be (re)programmed: MIO_PROG's bank-changed branch
; (+8 cycles there, +0 on the same-bank path) and its cold path, before
; any REC store.  INVARIANT: m_rbk never holds $BF or $DF, so an I/O
; access always arrives at the bank compare as a change and can never take
; the same-bank fast path.  A trapped call issues no DMA; the length/low/
; high bytes a warm call stored before the trap stay consistent with the
; shadow.  A 16/32-bit access that starts in $BE/$DE and carries into
; $BF/$DF is routed by STRADDLE to mio_split (only reached when the low
; bytes are $FFFD-$FFFF), and mio_split hands every I/O byte of a split
; access to chip_split (data in t_tmp, so the read copy stays correct).
; Lines added by W7-T carry a "; W7-T" tag (host/tests/test_chip_hotpath.py
; strips them and must get the base fingerprint back).
; ===========================================================================

; ---------------------------------------------------------------------------
; WINCHK_B / WINCHK_W — invalidate the instruction window when a guest
; write overlaps the cached page (self-modifying code, stack pushes landing
; in the code page, harness staging over old code).  The window is a pure
; fetch cache — the write itself still goes to the REU — so invalidation
; is all that is needed for coherence.
;
; WINCHK_B: a 1-byte write overlaps iff its page equals the window page
; (9 cycles when the page low bytes differ — the common case).
; WINCHK_W lastoff: a (lastoff+1)-byte write overlaps iff it STARTS in the
; window page, or starts at most lastoff bytes before it and spills across
; the page boundary (low byte >= $100-lastoff; the +1 page compare below
; reuses the carry from that cpx-style compare, and its carry-out keeps
; the test correct across the $FFxxxx -> $000000 guest address wrap).
; Clobbers A only.  Both are no-ops in FETCH_DIRECT builds; defining
; WINDOW_NOINVAL also empties them — TEST-ONLY, breaks correctness by
; design (used to prove the self-modifying-code test has teeth).
; ---------------------------------------------------------------------------
.if .defined(FETCH_DIRECT) .or .defined(WINDOW_NOINVAL)
.macro WINCHK_B
.endmacro
.macro WINCHK_W lastoff
.endmacro
.macro WINCHK_SLOW_B
.endmacro
.macro WINCHK_SLOW_W lastoff
.endmacro
.elseif .defined(CWIN_MUT_CURONLY)
; W12-C mutant (tests/run_cwin.py): the single-window check -- only the
; CURRENT window is dropped; a write into another cached page goes unseen.
.macro WINCHK_B
        .local done
        lda m_addr+1
        eor w_pg0
        bne done
        lda m_addr+2
        eor w_pg1
        bne done
        lda #1
        sta w_inv
done:
.endmacro
.macro WINCHK_W lastoff
        .local chkhi, kill, done
        lda m_addr+1
        eor w_pg0
        beq chkhi
        lda m_addr
        cmp #$100 - lastoff
        bcc done
        lda m_addr+1
        adc #0
        eor w_pg0
        bne done
        lda m_addr+2
        adc #0
        eor w_pg1
        bne done
        beq kill
chkhi:  lda m_addr+2
        eor w_pg1
        bne done
kill:   lda #1
        sta w_inv
done:
.endmacro
.macro WINCHK_SLOW_B
.endmacro
.macro WINCHK_SLOW_W lastoff
.endmacro
.else
; W12-C: the overlap test reads cwin_map[page-low of the write] through a
; self-modified absolute load (the memory API keeps X and Y, so there is
; no free index register).  B: bit 7 set -> the page may be cached; W:
; any bit -> the page or the next one (a spill) may be.  The precise test
; (tags, bank, spill) is out of line: WINCHK_SLOW_* after the entry's
; last rts, then cw_ww.  Common path 13 cycles (was 9 for B, 16 for W).
.macro WINCHK_B
        lda m_addr+1            ; 3
        sta cwlk+1              ; 4
cwlk:   lda cwin_map            ; 4   low byte self-modified
        bmi cwslow              ; 2   not taken = common path
cwback:
.endmacro

.macro WINCHK_W lastoff
        lda m_addr+1            ; 3
        sta cwlk+1              ; 4
cwlk:   lda cwin_map            ; 4   low byte self-modified
        bne cwslow              ; 2   not taken = common path
cwback:
.endmacro

.macro WINCHK_SLOW_B
cwslow: lda #0
        jsr cw_ww
        jmp cwback
.endmacro

.macro WINCHK_SLOW_W lastoff
cwslow: lda #lastoff
        jsr cw_ww
        jmp cwback
.endmacro
.endif

REU_CMD_FETCH_AL = REU_CMD_FETCH | $20    ; + autoload (see above)
REU_CMD_STASH_AL = REU_CMD_STASH | $20

; ---------------------------------------------------------------------------
; MIO_PROG len — program the REC for t_tmp <-> REU[m_addr], len bytes,
; touching only registers whose shadow differs.  Clobbers A.  Inlined in
; each entry (saves the jsr/rts of the v0 mio_setup).  Warm cycles: 41
; (nothing changes) .. 47 (low byte changes, the common case) .. ~68;
; a bank change pays the 8-cycle I/O test (W7-T).  `trap` is the calling
; entry's own label (jmp chip_*): taken for bank $DF/$BF, m_rbk untouched.
; ---------------------------------------------------------------------------
.macro MIO_PROG len
        .local warm, lenok, look, hiok, done
        lda m_rok               ; 3
        BRNC bne, warm                ; 3
        lda #len                ; cold: REC state unknown
        jsr mio_full
        jmp hiok                ; W7-T (was jmp done): I/O -> the bank test
warm:   lda #len                ; 2
        cmp m_rlen              ; 3
        BRNC beq, lenok               ; 3
        sta m_rlen
        sta REC_LENLO
lenok:  lda m_addr              ; 3
        cmp m_rlo               ; 3
        BRNC beq, look                ; 2
        sta m_rlo               ; 3
        sta REC_REULO           ; 4
look:   lda m_addr+1            ; 3
        cmp m_rhi               ; 3
        BRNC beq, hiok                ; 3
        sta m_rhi
        sta REC_REUHI
hiok:   lda m_addr+2            ; 3
        cmp m_rbk               ; 3
        BRNC beq, done                ; 3
        cmp #$DF                ; W7-T 2  bank changed: I/O?
        beq trap                ; W7-T 2  (never-taken branches: +8 total;
        cmp #$BF                ; W7-T 2   trap = the entry's jmp chip_*)
        beq trap                ; W7-T 2
        sta m_rbk
        sta REC_REUBANK
done:
.endmacro

; mio_full — A = transfer length.  Cold path: program every register the
; data path relies on, load the shadow, mark it valid.  8 REC stores.
; W7-T: bank $DF/$BF programs nothing (m_rok stays 0, m_rbk := 0) and
; returns; the caller's jmp hiok then takes the bank-changed branch, whose
; I/O test traps.  So the cold path pays the same 8-cycle test, before
; any REC access, plus the hiok compare (9 cycles) on every cold call.
.proc mio_full
        sta m_rlen
        lda m_addr+2            ; W7-T cold path: I/O bank?  Tested before
        cmp #$DF                ; W7-T any REC store, so a trapped cold
        beq io                  ; W7-T call touches no REC register
        cmp #$BF                ; W7-T
        beq io                  ; W7-T
        lda m_rlen              ; W7-T
        sta REC_LENLO
        lda #0
        sta REC_LENHI
        sta REC_C64HI           ; C64 base = $0070 (t_tmp)
        sta REC_ACTRL           ; both addresses increment
        lda #<t_tmp
        sta REC_C64LO
        lda m_addr
        sta m_rlo
        sta REC_REULO
        lda m_addr+1
        sta m_rhi
        sta REC_REUHI
        lda m_addr+2
        sta m_rbk
        sta REC_REUBANK
        lda #1
        sta m_rok
        rts
io:     lda #0                  ; W7-T m_rbk := a non-I/O bank (m_rok stays
        sta m_rbk               ; W7-T 0): MIO_PROG's jmp hiok then sees a
        rts                     ; W7-T bank change and traps
.endproc

; ---------------------------------------------------------------------------
; STRADDLE len, slow — branch to slow when a len-byte access at m_addr
; crosses a 512 KB REU block line ($x7FFFF -> $x80000).  Common path
; (address low byte < $100-(len-1)): 7 cycles, falls through.  Clobbers A.
; W7-T: also routes a bank carry $BE->$BF / $DE->$DF to slow (mio_split).
; ---------------------------------------------------------------------------
.macro STRADDLE len, slow
        .local no
        lda m_addr              ; 3
        cmp #$100-(len-1)       ; 2
        BRNC bcc, no                  ; 3 taken = common path
        lda m_addr+1
        cmp #$FF
        BRNC bne, no
        lda m_addr+2
        ora #$F8                ; bits 18-16 all set?
        cmp #$FF
        BRNC beq, slow
        lda m_addr+2            ; W7-T bank carry into $BF/$DF?
        cmp #$BE                ; W7-T (only reached at $xxFFFD-$xxFFFF:
        beq slow                ; W7-T  plain branches, not hot)
        cmp #$DE                ; W7-T
        beq slow                ; W7-T
no:
.endmacro

.segment "BSS"
m_scmd: .res 1                  ; mio_split: REC command
m_slen: .res 1                  ; mio_split: byte count
.segment "CODE"

; ---------------------------------------------------------------------------
; mio_split — slow path for a straddling access: A = byte count, m_scmd =
; REC command (no autoload needed).  Moves t_tmp[i] <-> REU[m_addr + i]
; one byte per DMA, every register programmed each time, the 24-bit
; address sum done by the CPU.  Leaves the shadow invalid (cleared before
; the first REC store), so the next call re-programs from scratch.
; W7-T: a byte in bank $BF/$DF goes to chip_split instead (no DMA).
; Clobbers A only (X and Y preserved, like the fast path).  ~9 REC
; accesses per byte -- rare by construction (one address in 2^19 per
; long, and only misaligned-for-the-block data).
; ---------------------------------------------------------------------------
.proc mio_split
        sta m_slen
        txa
        pha
        tya
        pha
        lda #0
        sta m_rok               ; shadow stale BEFORE the REC changes
        tay
loop:   lda #0
        sta REC_C64HI
        sta REC_LENHI
        sta REC_ACTRL
        lda #1
        sta REC_LENLO
        tya
        clc
        adc #<t_tmp
        sta REC_C64LO           ; t_tmp+i (never crosses page 0)
        tya
        clc
        adc m_addr
        sta REC_REULO
        lda m_addr+1
        adc #0
        sta REC_REUHI
        lda m_addr+2
        adc #0                  ; carry out of bit 23 dropped: 24-bit wrap
        cmp #$DF                ; W7-T split: I/O byte?  (The address
        beq io                  ; W7-T stores above are harmless: shadow
        cmp #$BF                ; W7-T already invalid, and no command.)
        beq io                  ; W7-T
        sta REC_REUBANK
        lda m_scmd
        sta REC_COMMAND
next:   iny
        cpy m_slen
        BRNC bne, loop
        pla
        tay
        pla
        tax
        rts
io:     jsr chip_split          ; W7-T I/O byte: chip.s, no DMA (keeps Y)
        jmp next                ; W7-T
.endproc

; ---------------------------------------------------------------------------
; mem_rec_dirty — REC-ownership seam (docs/WAVE4_BRIEF.md contract C1).
; Any code that programs the REC ($DF02-$DF0A) or issues a REC command
; outside the memory API calls this before the next memory-API call:
; the next call then reprograms every register.  Clobbers A only.
; REC_NOINVAL (test-only) empties it.
; ---------------------------------------------------------------------------
.proc mem_rec_dirty
.if .not (.defined(REC_NOINVAL) .or .defined(REC_MUT_DIRTY))
        lda #0
        sta m_rok
.endif
        rts
.endproc

.proc mem_read8
        MIO_PROG 1
        lda #REU_CMD_FETCH_AL
        sta REC_COMMAND         ; DMA executes here, CPU stalled
        lda t_tmp
        sta m_val
        rts
trap:   jmp chip_r8             ; W7-T
.endproc

        HOTALIGN 128
.proc mem_read16
        STRADDLE 2, split
        MIO_PROG 2
        lda #REU_CMD_FETCH_AL
        sta REC_COMMAND
copy:   lda t_tmp               ; guest [addr] = high byte
        sta m_val+1
        lda t_tmp+1             ; guest [addr+1] = low byte
        sta m_val
        rts
split:  lda #REU_CMD_FETCH
        sta m_scmd
        lda #2
        jsr mio_split
        jmp copy
trap:   jmp chip_r16            ; W7-T
.endproc

.proc mem_read32
        STRADDLE 4, split
        MIO_PROG 4
        lda #REU_CMD_FETCH_AL
        sta REC_COMMAND
copy:   lda t_tmp
        sta m_val+3
        lda t_tmp+1
        sta m_val+2
        lda t_tmp+2
        sta m_val+1
        lda t_tmp+3
        sta m_val
        rts
split:  lda #REU_CMD_FETCH
        sta m_scmd
        lda #4
        jsr mio_split
        jmp copy
trap:   jmp chip_r32            ; W7-T
.endproc

        HOTALIGN 128
.proc mem_write8
        WINCHK_B
        lda m_val
        sta t_tmp
        MIO_PROG 1
        lda #REU_CMD_STASH_AL
        sta REC_COMMAND
        rts
trap:   jmp chip_w8             ; W7-T
        WINCHK_SLOW_B           ; W12-C
.endproc

.proc mem_write16
        WINCHK_W 1
        lda m_val+1
        sta t_tmp               ; high byte first (guest BE)
        lda m_val
        sta t_tmp+1
        STRADDLE 2, split
        MIO_PROG 2
        lda #REU_CMD_STASH_AL
        sta REC_COMMAND
        rts
        WINCHK_SLOW_W 1         ; W12-C (in branch range)
split:  lda #REU_CMD_STASH
        sta m_scmd
        lda #2
        jmp mio_split
trap:   jmp chip_w16            ; W7-T
.endproc

        HOTALIGN 128
.proc mem_write32
        WINCHK_W 3
        lda m_val+3
        sta t_tmp
        lda m_val+2
        sta t_tmp+1
        lda m_val+1
        sta t_tmp+2
        lda m_val
        sta t_tmp+3
        STRADDLE 4, split
        MIO_PROG 4
        lda #REU_CMD_STASH_AL
        sta REC_COMMAND
        rts
        WINCHK_SLOW_W 3         ; W12-C (in branch range)
split:  lda #REU_CMD_STASH
        sta m_scmd
        lda #4
        jmp mio_split
trap:   jmp chip_w32            ; W7-T
.endproc

; ===========================================================================
; Effective-address resolution
;
; ea_calc: A = size (0=B,1=W,2=L), X = 6-bit mode|reg field (bits 5-3 mode,
;          bits 2-0 reg).  Leaves:
;            ea_kind = 0 -> memory operand, guest address in t_ea
;            ea_kind = 1 -> register operand, ea_reg = ZP offset from r_d0
;          Postinc/predec adjust An here (byte on A7 adjusts by 2).
;          May fetch extension words: op_lo/op_hi/t_tmp/m_* clobbered.
;          Illegal mode-7 combos (reg 5-7) jump to op_illegal (no return).
;
; ea_load:  A = size. Reads the operand of the last ea_calc into the sized
;           bytes of t_op1 (upper bytes of t_op1 UNCHANGED for B/W).
;           t_ea/ea_kind/ea_reg preserved -> read-modify-write works:
;           ea_calc, ea_load, alu_xxx, ea_store.
; ea_store: A = size. Writes the sized bytes of t_res to the operand of the
;           last ea_calc (register upper bytes preserved — for An results
;           sign-extend first and store with size L, per 68k semantics).
;
; NOTE for handlers: immediate mode is resolved as a "memory" operand whose
; t_ea points into the instruction stream (PC advanced past the data), so
; ea_load just works; ea_store to #imm is illegal (don't).
; ===========================================================================
ea_calc:
        sta ea_size
        txa
        and #7
        asl
        asl
        sta ea_reg              ; reg*4
        txa
        lsr
        lsr
        lsr                     ; mode 0-7
        tay
        lda ea_modetab_lo,y
        sta ea_jv+1
        lda ea_modetab_hi,y
        sta ea_jv+2
ea_jv:  jmp $FFFF               ; self-modified

ea_m0:                          ; Dn direct
        lda #1
        sta ea_kind
        rts
ea_m1:                          ; An direct: bias offset into the A-reg file
        lda #1
        sta ea_kind
        lda ea_reg
        clc
        adc #$20                ; r_a0 - r_d0
        sta ea_reg
        rts
ea_m2:                          ; (An)
        jmp ea_an_ind
ea_m3:                          ; (An)+
        jsr ea_an_ind           ; t_ea = An (pre-increment value)
        jsr ea_amount           ; A = 1/2/4 (byte on A7 -> 2)
        ldy ea_reg
        clc
        adc r_a0,y              ; An += amount (32-bit)
        sta r_a0,y
        lda r_a0+1,y
        adc #0
        sta r_a0+1,y
        lda r_a0+2,y
        adc #0
        sta r_a0+2,y
        lda r_a0+3,y
        adc #0
        sta r_a0+3,y
        rts
ea_m4:                          ; -(An)
        jsr ea_amount
        sta w_ptmp
        ldy ea_reg
        sec
        lda r_a0,y              ; An -= amount (32-bit)
        sbc w_ptmp
        sta r_a0,y
        lda r_a0+1,y
        sbc #0
        sta r_a0+1,y
        lda r_a0+2,y
        sbc #0
        sta r_a0+2,y
        lda r_a0+3,y
        sbc #0
        sta r_a0+3,y
        jmp ea_an_ind           ; t_ea = updated An
ea_m5:                          ; d16(An)
        jsr ea_an_ind
        jsr fetch_word
        jmp ea_add_sext16
ea_m6:                          ; d8(An,Xn) brief extension word
        jsr ea_an_ind
        jsr fetch_word
        jmp ea_add_brief
ea_m7:                          ; abs.W / abs.L / d16(PC) / d8(PC,Xn) / #imm
        txa                     ; X still = mode|reg field
        and #7
        BRNC beq, ea_absw
        cmp #1
        BRNC beq, ea_absl
        cmp #2
        BRNC beq, ea_pcd16
        cmp #3
        BRNC beq, ea_pcidx
        cmp #4
        BRNC beq, ea_imm
        jmp op_illegal

ea_absw:                        ; abs.W, sign-extended to 32 bits
        jsr fetch_word
        lda op_lo
        sta t_ea
        ldy #0
        lda op_hi
        sta t_ea+1
        BRNC bpl, @pos
        dey
@pos:   sty t_ea+2
        sty t_ea+3
        lda #0
        sta ea_kind
        rts
ea_absl:                        ; abs.L: high word first in the stream
        jsr fetch_word
        lda op_lo
        sta t_ea+2
        lda op_hi
        sta t_ea+3
        jsr fetch_word
        lda op_lo
        sta t_ea
        lda op_hi
        sta t_ea+1
        lda #0
        sta ea_kind
        rts
ea_pcd16:                       ; d16(PC): base = address of extension word
        jsr ea_pc_base
        jsr fetch_word
        jmp ea_add_sext16
ea_pcidx:                       ; d8(PC,Xn)
        jsr ea_pc_base
        jsr fetch_word
        jmp ea_add_brief
ea_imm:                         ; #imm: operand lives in the instruction
        jsr ea_pc_base          ; stream; t_ea points at the datum
        lda ea_size
        BRNC beq, @b
        cmp #2
        BRNC beq, @l
        lda #2                  ; word
        jmp ea_pc_add
@b:     inc t_ea                ; byte datum = LOW byte of the ext word
        BRNC bne, @b1
        inc t_ea+1
        BRNC bne, @b1
        inc t_ea+2
@b1:    lda #2
        jmp ea_pc_add
@l:     lda #4
        jmp ea_pc_add

; --- EA helpers ------------------------------------------------------------
ea_an_ind:                      ; t_ea = An (ea_reg = reg*4), kind = memory
        ldy ea_reg
        lda r_a0,y
        sta t_ea
        lda r_a0+1,y
        sta t_ea+1
        lda r_a0+2,y
        sta t_ea+2
        lda r_a0+3,y
        sta t_ea+3
        lda #0
        sta ea_kind
        rts

ea_pc_base:                     ; t_ea = PC (current, past opcode/prev ext)
        lda r_pc
        sta t_ea
        lda r_pc+1
        sta t_ea+1
        lda r_pc+2
        sta t_ea+2
        lda r_pc+3
        sta t_ea+3
        lda #0
        sta ea_kind
        rts

ea_pc_add:                      ; r_pc += A (bits 24-31 stay 0)
        clc
        adc r_pc
        sta r_pc
        BRNC bcc, @d
        inc r_pc+1
        BRNC bne, @d
        inc r_pc+2
@d:     rts

ea_amount:                      ; A = operand byte count; byte on A7 -> 2
        ldx ea_size
        lda ea_szbytes,x
        cmp #1
        BRNC bne, @ok
        ldy ea_reg
        cpy #$1C                ; 7*4 = A7
        BRNC bne, @ok
        lda #2                  ; keep SP even (68k quirk)
@ok:    rts

ea_add_sext16:                  ; t_ea += sext16(op_hi:op_lo)
        ldy #0
        lda op_hi
        BRNC bpl, @pos
        dey                     ; Y = $FF
@pos:   clc
        lda t_ea
        adc op_lo
        sta t_ea
        lda t_ea+1
        adc op_hi
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        rts

; Brief extension word (68000): bit15 D/A, bits14-12 reg, bit11 W/L,
; bits10-9 scale (ignored on 68000, always *1), bits7-0 d8.
ea_add_brief:                   ; t_ea += sext8(op_lo) + index(op_hi)
        ldy #0
        lda op_lo
        BRNC bpl, @p
        dey
@p:     clc
        adc t_ea
        sta t_ea
        tya
        adc t_ea+1
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        lda op_hi               ; top 4 bits = register index 0-15
        and #$F0
        lsr
        lsr                     ; index*4 = ZP offset from r_d0
        tax
        lda op_hi
        and #$08                ; W/L bit
        BRNC bne, @long
        ldy #0                  ; word index: sext16(Xn.w)
        lda r_d0+1,x
        BRNC bpl, @wp
        dey
@wp:    clc
        lda r_d0,x
        adc t_ea
        sta t_ea
        lda r_d0+1,x
        adc t_ea+1
        sta t_ea+1
        tya
        adc t_ea+2
        sta t_ea+2
        tya
        adc t_ea+3
        sta t_ea+3
        rts
@long:  clc                     ; long index: Xn.l
        lda r_d0,x
        adc t_ea
        sta t_ea
        lda r_d0+1,x
        adc t_ea+1
        sta t_ea+1
        lda r_d0+2,x
        adc t_ea+2
        sta t_ea+2
        lda r_d0+3,x
        adc t_ea+3
        sta t_ea+3
        rts

; --- ea_load ---------------------------------------------------------------
ea_load:
        ldy ea_kind
        BRNC bne, @reg
        tax                     ; memory operand: m_addr = t_ea
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        txa
        BRNC beq, @m8
        cmp #2
        BRNC beq, @m32
        jsr mem_read16
        lda m_val
        sta t_op1
        lda m_val+1
        sta t_op1+1
        rts
@m8:    jsr mem_read8
        lda m_val
        sta t_op1
        rts
@m32:   jsr mem_read32
        lda m_val
        sta t_op1
        lda m_val+1
        sta t_op1+1
        lda m_val+2
        sta t_op1+2
        lda m_val+3
        sta t_op1+3
        rts
@reg:   tax                     ; register operand
        ldy ea_reg
        lda r_d0,y
        sta t_op1
        txa
        BRNC beq, @rd
        lda r_d0+1,y
        sta t_op1+1
        cpx #2
        BRNC bne, @rd
        lda r_d0+2,y
        sta t_op1+2
        lda r_d0+3,y
        sta t_op1+3
@rd:    rts

; --- ea_store --------------------------------------------------------------
        HOTALIGN 128
ea_store:
        ldy ea_kind
        BRNC bne, @reg
        tax                     ; memory operand: m_addr = t_ea
        lda t_ea
        sta m_addr
        lda t_ea+1
        sta m_addr+1
        lda t_ea+2
        sta m_addr+2
        lda t_ea+3
        sta m_addr+3
        txa
        BRNC beq, @m8
        cmp #2
        BRNC beq, @m32
        lda t_res
        sta m_val
        lda t_res+1
        sta m_val+1
        jmp mem_write16
@m8:    lda t_res
        sta m_val
        jmp mem_write8
@m32:   lda t_res
        sta m_val
        lda t_res+1
        sta m_val+1
        lda t_res+2
        sta m_val+2
        lda t_res+3
        sta m_val+3
        jmp mem_write32
@reg:   tax                     ; register operand: sized bytes only,
        ldy ea_reg              ; upper register bytes preserved
        lda t_res
        sta r_d0,y
        txa
        BRNC beq, @rd
        lda t_res+1
        sta r_d0+1,y
        cpx #2
        BRNC bne, @rd
        lda t_res+2
        sta r_d0+2,y
        lda t_res+3
        sta r_d0+3,y
@rd:    rts

; ===========================================================================
; Sized ALU primitives.  ALL CCR logic lives here.
;
; Inputs:  t_op1 = destination operand, t_op2 = source operand, A = size
;          (0=B,1=W,2=L).  Result: sized bytes of t_res (upper bytes
;          unchanged for B/W).  Convention: t_res = t_op1 OP t_op2
;          (e.g. alu_sub/alu_cmp compute t_op1 - t_op2 = dst - src).
; CCR ($54) per 68000 PRM: N = msb of sized result, Z = sized result == 0,
;          V = signed overflow, C = carry/borrow; X = copy of C for
;          add/sub/neg/addx/subx; cmp/logic/ext leave X.  ADDX/SUBX only
;          CLEAR Z on a nonzero result (never set it).
; Clobbers: A, X, Y, w_zacc, w_ptmp.  alu_cmp also writes t_res.
; ===========================================================================

; Shared add/sub body: size dispatch (tax/dex — never touches C, so the
; caller's carry-in survives), byte-wise op, Z-accumulation in w_zacc,
; then jmp to the flag builder with A = final sized byte and the 6502 P
; from the final adc/sbc still live.
.macro ALUADDSUB oper, flagtgt
        .local w, l, b
        tax
        dex
        BRNC beq, w
        dex
        BRNC beq, l
b:      lda #0
        sta w_zacc
        lda t_op1
        oper t_op2
        sta t_res
        jmp flagtgt
w:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        jmp flagtgt
l:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        oper t_op2+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        oper t_op2+3
        sta t_res+3
        jmp flagtgt
.endmacro

alu_add:
        clc
        ALUADDSUB adc, flags_add
alu_sub:
        sec
        ALUADDSUB sbc, flags_sub
        HOTALIGN 128
alu_cmp:
        sec
        ALUADDSUB sbc, flags_cmp
alu_addx:                       ; carry-in = X flag
        tay                     ; save size
        lda r_sr
        and #$10
        cmp #$01                ; C = X
        tya                     ; restore size (tya keeps C)
        ALUADDSUB adc, flags_addx
alu_subx:                       ; borrow-in = X flag (6502 sense inverted)
        tay
        lda r_sr
        and #$10
        eor #$10
        cmp #$01                ; C = !X
        tya
        ALUADDSUB sbc, flags_subx

        HOTALIGN 128
alu_neg:                        ; t_res = 0 - t_op1 (t_op2 unused)
        tax
        dex
        BRNC beq, @w
        dex
        BRNC beq, @l
        lda #0                  ; byte
        sta w_zacc
        sec
        lda #0
        sbc t_op1
        sta t_res
        jmp flags_sub
@w:     sec
        lda #0
        sbc t_op1
        sta t_res
        sta w_zacc
        lda #0
        sbc t_op1+1
        sta t_res+1
        jmp flags_sub
@l:     sec
        lda #0
        sbc t_op1
        sta t_res
        sta w_zacc
        lda #0
        sbc t_op1+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda #0
        sbc t_op1+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda #0
        sbc t_op1+3
        sta t_res+3
        jmp flags_sub

; Logic body: like ALUADDSUB but no carry concerns; flags_logic wants
; A = final sized byte, w_zacc = OR of the lower bytes.
.macro ALULOGIC oper
        .local w, b
        cmp #1
        BRNC beq, w
        BRNC bcc, b
        lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        oper t_op2+2
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        oper t_op2+3
        sta t_res+3
        jmp flags_logic
w:      lda t_op1
        oper t_op2
        sta t_res
        sta w_zacc
        lda t_op1+1
        oper t_op2+1
        sta t_res+1
        jmp flags_logic
b:      lda #0
        sta w_zacc
        lda t_op1
        oper t_op2
        sta t_res
        jmp flags_logic
.endmacro

alu_and:
        ALULOGIC and
alu_or:
        ALULOGIC ora
        HOTALIGN 128
alu_eor:
        ALULOGIC eor

alu_not:                        ; t_res = ~t_op1 sized (t_op2 unused)
        cmp #1
        BRNC beq, @w
        BRNC bcc, @b
        lda t_op1
        eor #$FF
        sta t_res
        sta w_zacc
        lda t_op1+1
        eor #$FF
        sta t_res+1
        ora w_zacc
        sta w_zacc
        lda t_op1+2
        eor #$FF
        sta t_res+2
        ora w_zacc
        sta w_zacc
        lda t_op1+3
        eor #$FF
        sta t_res+3
        jmp flags_logic
@w:     lda t_op1
        eor #$FF
        sta t_res
        sta w_zacc
        lda t_op1+1
        eor #$FF
        sta t_res+1
        jmp flags_logic
@b:     lda #0
        sta w_zacc
        lda t_op1
        eor #$FF
        sta t_res
        jmp flags_logic

; alu_ext — sign extension.  A = RESULT size: 1 = byte->word (EXT.W),
; 2 = word->long (EXT.L).  t_res = sext(t_op1); N/Z from the result,
; V = C = 0, X unchanged.
alu_ext:
        cmp #2
        BRNC beq, @l
        ldy #0                  ; byte -> word
        lda t_op1
        sta t_res
        BRNC bpl, @p1
        dey
@p1:    sty t_res+1
        sta w_zacc              ; low byte
        tya                     ; A = extension byte (final byte)
        jmp flags_logic
@l:     lda t_op1               ; word -> long
        sta t_res
        sta w_zacc
        ldy #0
        lda t_op1+1
        sta t_res+1
        BRNC bpl, @p2
        dey
@p2:    sty t_res+2
        sty t_res+3
        ora w_zacc              ; A = byte1; zacc = b0|b1
        sta w_zacc
        tya                     ; A = extension byte = final byte (sign)
        jmp flags_logic

; ---------------------------------------------------------------------------
; CCR builders.  Entry (add/sub/cmp/addx/subx): A = final sized result byte,
; 6502 P from the final adc/sbc still live, w_zacc = OR of lower result
; bytes.  flags_logic: same but P not needed.
; 68k CCR layout: bit4 X, bit3 N, bit2 Z, bit1 V, bit0 C.
; ---------------------------------------------------------------------------
flags_add:                      ; N Z V C from op, X = C
        php
        ora w_zacc
        sta w_zacc
        pla
        jsr fl_setx
        jmp fl_insert_z
flags_addx:                     ; like flags_add, Z sticky-clear
        php
        ora w_zacc
        sta w_zacc
        pla
        jsr fl_setx
        jmp fl_insert_z_sticky
flags_sub:                      ; 68k C = borrow = !6502C, X = C
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr fl_setx
        jmp fl_insert_z
flags_subx:
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr fl_setx
        jmp fl_insert_z_sticky
flags_cmp:                      ; like flags_sub but X unchanged
        php
        ora w_zacc
        sta w_zacc
        pla
        eor #$01
        jsr p_to_nvc
        sta w_ptmp
        lda r_sr
        and #$10
        ora w_ptmp
        sta w_ptmp
        jmp fl_insert_z
flags_logic:                    ; N Z from result, V = C = 0, X unchanged
        tay                     ; Y = final byte
        ora w_zacc
        sta w_zacc
        lda #0
        cpy #$80                ; C = 1 iff final byte has bit7 set
        BRNC bcc, @nn
        lda #$08
@nn:    sta w_ptmp
        lda r_sr
        and #$10
        ora w_ptmp
        sta w_ptmp
        jmp fl_insert_z

fl_setx:                        ; A = P (C already 68k-sense): w_ptmp = XNVC
        jsr p_to_nvc
        sta w_ptmp
        and #$01
        BRNC beq, @nx
        lda w_ptmp
        ora #$10
        sta w_ptmp
@nx:    rts

p_to_nvc:                       ; A = 6502 P -> A = 68k N($08)|V($02)|C($01)
        tay
        and #$80
        lsr
        lsr
        lsr
        lsr                     ; N -> $08
        sta w_ptmp
        tya
        and #$40
        BRNC beq, @nv
        lda #$02
        ora w_ptmp
        sta w_ptmp
@nv:    tya
        and #$01
        ora w_ptmp
        rts

fl_insert_z:                    ; w_ptmp = X/N/V/C; Z from w_zacc; -> CCR
        lda w_zacc
        BRNC bne, @nz
        lda w_ptmp
        ora #$04
        sta w_ptmp
@nz:    lda w_ptmp
        sta r_sr
        rts
fl_insert_z_sticky:             ; ADDX/SUBX/NEGX: Z cleared, never set
        lda w_zacc
        BRNC bne, @nz
        lda r_sr                ; result zero -> keep previous Z
        and #$04
        ora w_ptmp
        sta w_ptmp
@nz:    lda w_ptmp
        sta r_sr
        rts

; ===========================================================================
; Halt / trap handlers (core-owned dispatch targets)
;
; With st_exc clear every path below is today's halt, bit for bit (the
; only addition is the st_exc test on the halting path).  With st_exc set
; (exception mode, docs/CONTRACTS.md "Supervisor mode & exceptions") the
; trap-class halts become 68000 exceptions through the guest vector table
; at 0 (VBR fixed at 0 on the 68000):
;   HALT_ALINE (non-HLE $Axxx)          -> vector 10, stacked PC = insn
;   HALT_FLINE                          -> vector 11, stacked PC = insn
;   op_illegal (illegal encodings/EAs)  -> vector  4, stacked PC = insn
;   HALT_ILLEGAL from the G3 #imm,SR    -> executed here (sys_srimm), or
;     forms (ANDI/ORI/EORI to SR)          vector 8 in user mode
;   HALT_ILLEGAL from G6 DIVU/DIVS /0   -> vector 5, stacked PC = insn
;                                          (upstream vectors), NZVC = 0
; Everything else (HALT_OK $AFFF, HALT_STOP, HALT_ADDRESS, and the legal
; but unimplemented TAS/MOVEP) stays a halt.  G3/G6/HLE are frozen and
; halt through here unchanged; the exception-mode loop's snapshot
; (st_ipc/st_iop/st_sp6) supplies what their halt sites no longer know.
; ===========================================================================
emu_halt:                       ; A = HALT_* reason
        ldx st_exl              ; 3   latched flag (never live st_exc)
        bne exc_halt            ; 2
halt_now:
        sta st_reason
        lda #1
        sta st_halt
        jmp dispatch_next

op_aline:                       ; $AFFF = clean HALT; other $Axxx = trap hook
        lda op_hi
        cmp #$AF
        bne @trap
        lda op_lo
        cmp #$FF
        bne @trap
        lda #HALT_OK
        jmp emu_halt
@trap:  jmp hle_aline           ; HLE dispatch (stub = halt ALINE until runtime/ops/hle.s lands)

op_fline:
        lda #HALT_FLINE
        jmp emu_halt

op_illegal:
        ldx st_exl              ; latched flag
        bne ill_x
        lda #HALT_ILLEGAL
        jmp emu_halt
ill_x:  lda st_iop              ; MOVEP ($0108|Dn<<9|size<<6|An, mask
        and #$38                ; $F138) is legal but unimplemented (G3
        cmp #$08                ; stub): keep halting rather than raise a
        bne @vec4               ; wrong illegal-instruction exception
        lda st_iop+1
        and #$F1
        cmp #$01
        bne @vec4
        lda #HALT_ILLEGAL
        jmp halt_unwind
@vec4:  lda #4
        jmp exc_group1

; --- exception-mode halt decode (A = reason) --------------------------------
exc_halt:
        cmp #HALT_ALINE
        bne @nal
        lda #10
        jmp exc_group1
@nal:   cmp #HALT_FLINE
        bne @nfl
        lda #11
        jmp exc_group1
@nfl:   cmp #HALT_ILLEGAL
        bne halt_unwind         ; OK / STOP / ADDRESS / others: halt
        ldx st_iop              ; #imm,SR: $007C ORI, $027C ANDI, $0A7C EORI
        cpx #$7C
        bne @ndiv0
        ldx st_iop+1
        beq sys_srimm
        cpx #$02
        beq sys_srimm
        cpx #$0A
        beq sys_srimm
        bne halt_unwind         ; always
@ndiv0: tax                     ; A = HALT_ILLEGAL kept in X
        lda st_iop              ; DIVU $80C0 / DIVS $81C0 (mask $F1C0)
        and #$C0
        cmp #$C0
        bne @keep
        lda st_iop+1
        and #$F0
        cmp #$80
        bne @keep
        lda r_sr                ; divide by zero: NZVC cleared, X kept
        and #$10
        sta r_sr
        lda #5
        jmp exc_group1
@keep:  txa                     ; TAS etc.: legal, unimplemented -> halt
halt_unwind:                    ; exception-mode halt: drop any nested
        ldx st_sp6              ; 6502 frames first, so the handler that
        txs                     ; halted is not resumed by the rts
        jmp halt_now

; --- ANDI/ORI/EORI #imm,SR (entered from exc_halt; PC = opcode + 2) ---------
sys_srimm:
        lda r_sr+1
        and #$20
        bne @sup
        lda #8                  ; user mode: privilege violation
        jmp exc_group1
@sup:   jsr fetch_word          ; op_hi:op_lo = immediate word
        lda st_iop+1
        beq @or
        cmp #$02
        beq @and
        lda r_sr                ; EORI
        eor op_lo
        pha
        lda r_sr+1
        eor op_hi
        jmp @wr
@and:   lda r_sr
        and op_lo
        pha
        lda r_sr+1
        and op_hi
        jmp @wr
@or:    lda r_sr
        ora op_lo
        pha
        lda r_sr+1
        ora op_hi
@wr:    tax
        pla
        jsr sr_write
        jmp dispatch_next

; ---------------------------------------------------------------------------
; sr_write — SR := X:A (X = high/system byte, A = low byte), masked to the
; bits a 68000 implements ($A71F: T, S, I2-I0, XNZVC).  When S goes 1 -> 0
; the active stack switches: A7 <-> r_osp.  (S cannot go 0 -> 1 here: only
; supervisor code writes the SR, and exc_raise handles the exception entry.)
; Clobbers A, X, Y.
; ---------------------------------------------------------------------------
sr_write:
        and #$1F
        sta r_sr
        txa
        and #$A7
        tax                     ; X = new high byte
        lda r_sr+1              ; old high byte
        stx r_sr+1
        and #$20
        beq @done               ; was user (cannot happen, but harmless)
        txa
        and #$20
        bne @done               ; still supervisor
.ifndef EXC_MUT_NOSWAP
        jmp sp_swap             ; S 1 -> 0: A7 becomes the USP
.endif
@done:  rts

; sp_swap — exchange A7 and r_osp (4 bytes).  Clobbers A, X, Y.
sp_swap:
        ldx #3
@lp:    lda r_a7,x
        ldy r_osp,x
        sta r_osp,x
        sty r_a7,x
        dex
        bpl @lp
        rts

; ---------------------------------------------------------------------------
; exc_group1 / exc_group2 — raise exception A (vector number 0-63).
; group1: stacked PC = start of the current instruction (st_ipc): illegal,
;         privilege violation, A-line, F-line; divide-by-zero too (the
;         upstream vectors stack the DIVU address, not the next one).
; group2: stacked PC = r_pc (the next instruction): TRAP, TRAPV, CHK.
; exc_raise (68000 group 1/2 processing): unwind the 6502 stack to the
; instruction start; save SR; S := 1, T := 0, switching A7 to the SSP when
; coming from user mode; push PC (long) then SR (word) -- 6-byte frame at
; SSP-6 -- and load PC from the vector at vector*4.  An odd vector halts
; with HALT_ADDRESS (a 68000 would take an address error: reason code
; only, docs/CONTRACTS.md).  Only reachable in exception mode.
; Scratch: t_op2 (stacked PC), t_res+0/1 (stacked SR), t_res+2 (vector).
; ---------------------------------------------------------------------------
exc_group1:
        ldx #3
@cp:    ldy st_ipc,x
        sty t_op2,x
        dex
        bpl @cp
        jmp exc_raise
exc_group2:
        ldx #3
@cp:    ldy r_pc,x
        sty t_op2,x
        dex
        bpl @cp
exc_raise:
        sta t_res+2             ; vector
        ldx st_sp6              ; unwind nested handler frames
        txs
        lda r_sr
        sta t_res               ; stacked SR = SR before the exception
        lda r_sr+1
        sta t_res+1
        and #$20
        bne @sup
.ifndef EXC_MUT_NOSWAP
        jsr sp_swap             ; user -> supervisor: A7 := SSP
.endif
@sup:   lda t_res+1
        ora #$20                ; S = 1
        and #$7F                ; T = 0
        sta r_sr+1
        ; push PC (long) at A7-4
        sec
        lda r_a7
        sbc #4
        sta r_a7
        sta m_addr
        lda r_a7+1
        sbc #0
        sta r_a7+1
        sta m_addr+1
        lda r_a7+2
        sbc #0
        sta r_a7+2
        sta m_addr+2
        lda r_a7+3
        sbc #0
        sta r_a7+3
        sta m_addr+3
.ifdef EXC_MUT_FRAME
        lda t_res               ; mutant: PC and SR swapped in the frame
        sta m_val
        lda t_res+1
        sta m_val+1
        lda #0
        sta m_val+2
        sta m_val+3
.else
        ldx #3
@pc:    lda t_op2,x
        sta m_val,x
        dex
        bpl @pc
.endif
        jsr mem_write32
        ; push SR (word) at A7-2
        sec
        lda r_a7
        sbc #2
        sta r_a7
        sta m_addr
        lda r_a7+1
        sbc #0
        sta r_a7+1
        sta m_addr+1
        lda r_a7+2
        sbc #0
        sta r_a7+2
        sta m_addr+2
        lda r_a7+3
        sbc #0
        sta r_a7+3
        sta m_addr+3
.ifdef EXC_MUT_FRAME
        lda t_op2
        sta m_val
        lda t_op2+1
        sta m_val+1
.else
        lda t_res
        sta m_val
        lda t_res+1
        sta m_val+1
.endif
        jsr mem_write16
        ; PC := long at vector*4
        lda t_res+2             ; vector < 64: vector*4 fits one byte
        asl
        asl
        sta m_addr
        lda #0
        sta m_addr+1
        sta m_addr+2
        sta m_addr+3
        jsr mem_read32
        lda m_val
        sta r_pc
        lda m_val+1
        sta r_pc+1
        lda m_val+2
        sta r_pc+2
        lda #0                  ; PC bits 24-31 always 0 (contract)
        sta r_pc+3
        lda m_val
        lsr
        bcc @even
        lda #HALT_ADDRESS
        jmp halt_now
@even:  jmp dispatch_next

; --- STOP / RESET / RTE support for G5 -------------------------------------
emu_stop:                       ; STOP #imm retired: halt, reason STOP
        lda #HALT_STOP
        jmp halt_now
emu_addrerr:                    ; odd RTE target: halt, reason ADDRESS
        lda #HALT_ADDRESS
        jmp halt_now

; RESET hook: G5's RESET (supervisor, exception mode) does jsr emu_rst_hook.
; Default: nothing (the guest's peripherals are not modelled).  An embedder
; that wants a callback patches the jmp operand (emu_rst_hook+1/+2) to a
; routine ending in rts; it runs with the instruction retired except PC.
emu_rst_hook:
        jmp rst_nop
rst_nop:
        rts



; ===========================================================================

; ===========================================================================
.segment "RODATA"
ea_szbytes:
        .byte 1, 2, 4
ea_modetab_lo:
        .byte <ea_m0, <ea_m1, <ea_m2, <ea_m3
        .byte <ea_m4, <ea_m5, <ea_m6, <ea_m7
ea_modetab_hi:
        .byte >ea_m0, >ea_m1, >ea_m2, >ea_m3
        .byte >ea_m4, >ea_m5, >ea_m6, >ea_m7

.import __ZP_LAST__
.assert __ZP_LAST__ <= st_exc, lderror, "ZEROPAGE .res pool grew into the W6-S equates at $88 (rc/zp.inc)"
.assert (__BSS_RUN__ & $FF) = 0, lderror, "boot BSS clear assumes page-aligned BSS (c64.cfg align = $100)"

.segment "CODE"

; ===========================================================================
; W12-C code-window cache (docs/CONTRACTS.md "Code-window cache").
;
; CWIN_N page-aligned 256-byte windows at cwin_base (CWIN area, RAM under
; BASIC ROM), a directory in MAIN BSS (cw_tlo/cw_thi tags, cw_val, the
; FIFO hand cw_hand) and the page-low map cwin_map:
;   bit 7     some valid window has page-low p
;   bit 6     some valid window has page-low p+1
;   bits 5-3  hint: the window last filled with page-low p
;   bits 2-0  how many valid windows have page-low p (<= CWIN_N <= 7)
; The write check (WINCHK_*) reads ONE map byte: a byte write may hit a
; window only if bit 7 is set, a word/long write (which may spill into
; the next page) only if bit 7 or 6 is.  The lookup tries the hint's
; window first.  Invariant: bits 7, 6, 2-0 describe exactly the valid
; windows; the hint is only a guess (always checked against the tags).
; Replacement is FIFO (cw_hand): on the AROS boot's page trace FIFO
; leaves 1.0% more refills than LRU at N = 7 (90,631 vs 89,717 to m3,
; build/evidence/w12-cwin/policy.txt) and costs nothing on a switch.
;
; Banking.  Everything that reads a window or the map runs with BASIC ROM
; out: the miss path banks it out (and leaves it out) before the first
; window can become valid, so a valid window implies BASIC out for the
; rest of that run (every $01 restorer inside a run restores a value it
; saved during the run).  A caller that maps BASIC in again after a run
; calls emu_reset or code_invall before the next run (contract text).
; Code that may run with BASIC in stays in MAIN (the miss prologue in
; fetch_word, cw_ww, code_invpg, cw_kill, cw_drop, code_invall); the
; lookup and refill (cw_look) is in CWCODE (CWIN area, $A800).
; ===========================================================================
.ifndef FETCH_DIRECT

; cw_ww — the out-of-line half of WINCHK_B/WINCHK_W, entered only when the
; map byte says a window may overlap.  A = lastoff (bytes after the first:
; 0/1/3).  Clobbers A only, like the macros; $01 comes back as it came in.
; With BASIC ROM in (a call from outside a run, e.g. host staging through
; test_load_chunk at $37) the map byte that sent us here was a ROM byte:
; BASIC is banked out for the duration of the check (code_invpg reads the
; real map) and the caller's $01 restored -- such calls pay the slow path
; for every write whose ROM byte at cwin_map+p is "maybe" (PERF.md (n)).
.proc cw_ww
.ifdef CWIN_COUNT
        CW_INC32 cw_nslw
.endif
        stx cw_xs
        tax                     ; X = lastoff
        lda CPU_PORT
        pha                     ; the caller's $01
        and #3
        cmp #3
        bne :+
        dec CPU_PORT            ; BASIC out while the map is read
:       txa
        clc
        adc m_addr              ; C = the write spills into the next page
        lda #0
        rol
        pha
        lda m_addr+1
        ldx m_addr+2
        jsr code_invpg          ; the page it starts in
        pla
.ifndef CWIN_MUT_NOSPILL
        beq done
        lda m_addr+2
        sta cw_pg+1
        lda m_addr+1
        clc
        adc #1                  ; the next page; 24-bit wrap $FFFFxx ->
        bne :+                  ; $0000xx (bank $FF + 1 = 0)
.ifndef CWIN_MUT_NOCARRY
        inc cw_pg+1
.endif
:       ldx cw_pg+1
        jsr code_invpg
.endif
done:   pla
        sta CPU_PORT            ; the caller's $01
        ldx cw_xs
        rts
.endproc

; cw_kill — X = a valid window a guest write overlapped: drop it, and mark
; the current window invalid if it is this one.  Clobbers A, Y; keeps X.
.proc cw_kill
.ifdef CWIN_COUNT
        CW_INC32 cw_nkil
.endif
        lda cw_tlo,x            ; valid tags are unique, so the current
        cmp w_pg0               ; window is the one tagged w_pg
        bne notcur
        lda cw_thi,x
        cmp w_pg1
        bne notcur
        lda #1
        sta w_inv
notcur: jmp cw_drop
.endproc

; cw_drop — X = a valid window: invalid, its page-low leaves cwin_map (while
; another window shares the page-low the hint may keep pointing at this one:
; a guess; with none left the entry keeps only bit 6).  Clobbers A, Y; keeps X.
.proc cw_drop
        lda #0
        sta cw_val,x
        ldy cw_tlo,x
        lda cwin_map,y
        sec
        sbc #1                  ; count - 1 (bits 2-0 >= 1: no borrow out)
        sta cwin_map,y
        and #$07
        bne done                ; another window has this page-low
        lda cwin_map,y          ; the last one: bit 7 and the hint go
.ifdef CWIN_MUT_STALEHINT
        and #$7F                ; (mutant: the hint stays)
.else
        and #$40                ; (a stale hint would make the byte nonzero
.endif
        sta cwin_map,y          ; and send every word write here slow)
        dey                     ; page-low - 1 (wraps $00 -> $FF)
        lda cwin_map,y
        and #$BF
        sta cwin_map,y
done:   rts
.endproc

.endif  ; .ifndef FETCH_DIRECT

; ---------------------------------------------------------------------------
; code_invpg — (W12-C) A = guest page bits 15-8, X = bits 23-16: drop
; every cached window of that page.  For guest-memory writes that bypass
; mem_write* (a blitter D row).  Reads and updates cwin_map, so it needs
; BASIC ROM out (its callers: BCODE; cw_ww, which banks it out around the
; call).  Clobbers A, X; keeps Y; never touches $01.
; ---------------------------------------------------------------------------
.proc code_invpg
.ifndef FETCH_DIRECT
        sta cw_pg
        stx cw_pg+1
        tax
        lda cwin_map,x
        bmi some
        rts                     ; no valid window has this page-low
some:   pha
        and #$07
        cmp #1
        bne many
        pla                     ; one window has this page-low: if the hint
        and #$38                ; is it, its bank decides (the common case:
        lsr                     ; code and data share a page-low only)
        lsr
        lsr
        tax
        lda cw_val,x
        beq scan
        lda cw_tlo,x
        cmp cw_pg
        bne scan
        lda cw_thi,x
        cmp cw_pg+1
        beq kill
        rts
many:   pla
scan:   ldx #CWIN_N-1
lp:     lda cw_val,x
        beq nx
        lda cw_tlo,x
        cmp cw_pg
        bne nx
        lda cw_thi,x
        cmp cw_pg+1
        beq kill
nx:     dex
        bpl lp
        rts
kill:   sty cw_ys
        jsr cw_kill             ; valid tags are unique: one window
        ldy cw_ys
.endif
        rts
.endproc

; ---------------------------------------------------------------------------
; code_invall — (W12-C, interface I1) invalidate every cached window: the
; next fetch refills.  For anything that rewrote guest memory behind the
; memory API's back (snap.s resume, host staging); emu_reset calls it.
; Writes cwin_map but never reads it, so it is correct with BASIC ROM in
; (a store under ROM lands in the RAM).  The first call since boot clears
; the whole map (power-up garbage).  Clobbers A, X; keeps Y.
; ---------------------------------------------------------------------------
.proc code_invall
        lda #1
        sta w_inv
.ifndef FETCH_DIRECT
.ifndef CWIN_MUT_NOINVALL
        lda cw_mok
        bne some
        tax                     ; A = 0: first call since boot (the BSS
clr:    sta cwin_map,x          ; clear zeroed cw_val, cw_hand)
        inx
        bne clr
        inc cw_mok
        rts
some:   sty cw_pg               ; (cw_pg is free outside code_invpg)
        ldx #CWIN_N-1
lp:     lda cw_val,x
        beq nx
        lda #0                  ; every valid window goes, so its map
        sta cw_val,x            ; entries (page-low and page-low - 1) end
        ldy cw_tlo,x            ; up all zero
        sta cwin_map,y
        dey
        sta cwin_map,y
nx:     dex
        bpl lp
        ldy cw_pg
.endif
.endif
        rts
.endproc

.ifndef FETCH_DIRECT
.segment "CWCODE"               ; W12-C: CWIN area, runs with BASIC ROM out

; ---------------------------------------------------------------------------
; cw_look — fetch_word's miss path after the banking prologue (PC's page is
; not the current window, or the current window was dropped).  A valid
; window holding PC's page becomes current (no DMA: the map hint's window
; first, else a scan); otherwise the FIFO hand's window is refilled with
; one 256-byte REU DMA.  The current window is the one the four hit-path
; loads point at (dg_w0/dg_w1 in dispatch_go, fw_w0/fw_w1 in fetch_word:
; high bytes patched in sel).  Then the hit path serves the word.  Keeps Y.
; ---------------------------------------------------------------------------
.proc cw_look
.ifdef CWIN_COUNT
        CW_INC32 cw_nmis
.endif
        sty cw_ys
        ldy r_pc+1
        lda cwin_map,y          ; bit 7: some valid window has this
        bmi hinted              ; page-low (else no tag can match)
        jmp fill
hinted: and #$38                ; the hint
        lsr
        lsr
        lsr
        tax
        tya
        cmp cw_tlo,x
        bne scan
        lda cw_thi,x
        cmp r_pc+2
        bne scan
.ifdef CWIN_MUT_HINTNOVAL
        jmp sel                 ; (mutant: the hint's window, valid or not)
.else
        lda cw_val,x
        bne sel                 ; the hint's window: switch, no DMA
.endif
scan:   ldx #CWIN_N-1           ; another bank with the same page-low
lp:     lda cw_val,x
        beq nx
        tya
        cmp cw_tlo,x
        bne nx
        lda cw_thi,x
        cmp r_pc+2
        beq sel
nx:     dex
        bpl lp

        jmp fill

sel:    lda r_pc+1              ; X = the window: current from now on
        sta w_pg0
        lda r_pc+2
        sta w_pg1
        lda #0
        sta w_inv
        txa                     ; re-point the four hit-path loads
        clc
        adc #>cwin_base
        sta dg_w0+2
        sta dg_w1+2
.ifndef CWIN_MUT_NOPATCH
        sta fetch_word::fw_w0+2
        sta fetch_word::fw_w1+2
.endif
        ldy cw_ys
        jmp fetch_word::serve

fill:   ldx cw_hand             ; FIFO: the oldest fill goes (the hand
        dex                     ; counts down, CWIN_N-1 .. 0)
        bpl :+
        ldx #CWIN_N-1
:       stx cw_hand
        lda cw_val,x
        beq dma
        jsr cw_drop             ; evict: its tag leaves cwin_map
dma:
.ifdef CWIN_COUNT
        CW_INC32 cw_nref
.endif
        lda #0                  ; one 256-byte REU DMA into the window (a
.if .not (.defined(REC_NOINVAL) .or .defined(REC_MUT_REFILL))
        sta m_rok               ; page never straddles a 512 KB REU block)
.endif                          ; C1: shadow stale BEFORE the REC changes
        sta REC_REULO
        sta REC_C64LO           ; windows are page-aligned
        sta REC_LENLO           ; length $0100
        sta REC_ACTRL           ; both addresses increment
        lda r_pc+1
        sta cw_tlo,x
        sta REC_REUHI           ; REU offset = page << 8
        lda r_pc+2
        sta cw_thi,x
        sta REC_REUBANK
        txa
        clc
        adc #>cwin_base
        sta REC_C64HI
        lda #1
        sta REC_LENHI
        sta cw_val,x
        lda #REU_CMD_FETCH
        sta REC_COMMAND         ; DMA executes here, CPU stalled ~256 cyc
.ifndef CWIN_MUT_NOMAPADD
        ldy r_pc+1              ; cwin_map[page-low]: count + 1, hint :=
        txa                     ; this window, bit 7; [page-low - 1]:
        asl                     ; bit 6
        asl
        asl
        sta cw_pg               ; (hint << 3; cw_pg is free here)
        lda cwin_map,y
        and #$C7
        ora cw_pg
        clc
        adc #1                  ; count <= CWIN_N - 1 before: no carry
        ora #$80                ; out of bits 2-0
        sta cwin_map,y
        dey
        lda cwin_map,y
        ora #$40
        sta cwin_map,y
.endif
        jmp sel
.endproc


.import __CWCODE_RUN__, __CWCODE_SIZE__
.assert __CWCODE_RUN__ + __CWCODE_SIZE__ <= cwin_base, lderror, "W12-C: CWCODE runs into the code windows"
.assert cwin_base + CWIN_N * $100 <= cwin_map, error, "W12-C: windows overlap cwin_map"
.assert (cwin_base & $FF) = 0 .and (cwin_map & $FF) = 0, error, "W12-C: windows and map page-aligned"
.segment "CODE"
.endif
