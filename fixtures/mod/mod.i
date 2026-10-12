; fixtures/mod/mod.i — the player program's fixed addresses and blocks
; (W15-M, docs/WAVE15_BRIEF.md: I-1 and the M0 rulings).  Host mirror:
; tests/run_mod.py (PARAM/STATUS/TRACE constants), checked by
; host/tests/test_mod_prog.py.

CUSTOM          equ     $dff000
VPOSR           equ     $004
DMACON          equ     $096
INTENA          equ     $09a
INTREQ          equ     $09c
AUD0LC          equ     $0a0            ; +$10 per channel: LC, +4 LEN, +6 PER, +8 VOL

CIAA_TODLO      equ     $bfe801
CIAA_TODMID     equ     $bfe901
CIAA_TODHI      equ     $bfea01
CIAA_TBLO       equ     $bfe601
CIAA_TBHI       equ     $bfe701
CIAA_ICR        equ     $bfed01
CIAA_CRB        equ     $bfef01
CIAB_TALO       equ     $bfd400
CIAB_TAHI       equ     $bfd500
CIAB_TBLO       equ     $bfd600
CIAB_TBHI       equ     $bfd700
CIAB_ICR        equ     $bfdd00
CIAB_CRA        equ     $bfde00
CIAB_CRB        equ     $bfdf00

VIEW            equ     $000400         ; I-1 mod_view, 72 bytes
VIEW_LEN        equ     72

; parameter block, host -> program
PARAM           equ     $000480
P_MAGIC         equ     0               ; 'MPAR'
P_MOD           equ     4               ; u32 guest address of the MOD
P_LEN           equ     8               ; u32 its length
P_FLAGS         equ     12              ; u16: PF_*
P_TICKE         equ     14              ; u16 E ticks per tick at 125 BPM (0 = 14188)
P_MAXT          equ     16              ; u32 stop after this many ticks (0 = no limit)
P_START         equ     20              ; u8 start position
P_UIDIV         equ     21              ; u8 UI_DIV: publish + ui_frame every Nth VERTB (0, 1 = every one)
P_MAXF          equ     24              ; u32 stop after this many frames (0 = no limit; MOD_TRACE builds only)
PF_STOPEND      equ     0               ; bit: halt at the song end instead of looping
PF_TRACE        equ     1               ; bit: write the trace (MOD_TRACE builds)
PF_REALTIME     equ     2               ; bit: every tick from CIA-B TA (tick_e scaled)

; status block, program -> host
STATUS          equ     $0004c0
S_MAGIC         equ     0               ; 'MSTA'
S_STATE         equ     4               ; u8: 1 running, 2 done, 3 error, 4 fault
S_ERR           equ     5               ; u8 error code (main's return value)
S_FAULT         equ     6               ; u16 vector number of an unexpected exception
S_TICKS         equ     8               ; u32
S_FRAMES        equ     12              ; u32 VERTBs seen
S_IGN           equ     16              ; 6 x u16: E0 E3 E8 EF 8 F00
S_MAXCU         equ     28              ; u8 most ticks run by one run_loop (MOD_TRACE builds)
S_TOVF          equ     29              ; u8 trace overflow
S_FLIM          equ     30              ; u8 the frame limit ended the run
S_TWP           equ     32              ; u32 trace write-stream end
S_TTP           equ     36              ; u32 trace tick-record end
S_FLUSH         equ     40              ; u16 stage flushes (a tick met pending stages)
S_L6            equ     42              ; u16 level-6 entries

; trace (MOD_TRACE builds; slow RAM)
TR_W            equ     $c00000         ; u32 entries: reg << 16 | value; $FFFF:tick = tick marker, $FFFE0000 = end
TR_WEND         equ     $c80000
TR_T            equ     $c80000         ; 80-byte tick records: view(72), CIA-A TOD(4), VPOSR:VHPOSR(4) at the tick's start
TR_TEND         equ     $dc0000
TR_REC          equ     80

STAGE_E         equ     300             ; E ticks between the trigger and each DMA stage
