; fixtures/modwb/os.i — the AmigaOS calls and structure offsets the launcher
; uses (W16-X).  Written by hand from public documentation (the Amiga ROM
; Kernel Reference Manual: Libraries and Includes & Autodocs, 3rd ed.; the
; Amiga Hardware Reference Manual, 3rd ed.); no NDK or AROS file was read.
; Citations: build/evidence/w16-x/PROGRESS.md.  host/tests/test_modwb.py pins
; every value against its own table.

CALL    macro
        jsr     _LVO\1(a6)
        endm

; exec.library
_LVOSupervisor          equ     -30
_LVODisable             equ     -120
_LVOEnable              equ     -126
_LVOForbid              equ     -132
_LVOPermit              equ     -138
_LVOSuperState          equ     -150
_LVOUserState           equ     -156
_LVOAllocMem            equ     -198
_LVOFreeMem             equ     -210
_LVOFindTask            equ     -294
_LVOGetMsg              equ     -372
_LVOReplyMsg            equ     -378
_LVOWaitPort            equ     -384
_LVOCloseLibrary        equ     -414
_LVOOpenDevice          equ     -444
_LVOCloseDevice         equ     -450
_LVOOpenResource        equ     -498
_LVOOpenLibrary         equ     -552
_LVOCreateMsgPort       equ     -666
_LVODeleteMsgPort       equ     -672

; dos.library
_LVOOpen                equ     -30
_LVOClose               equ     -36
_LVORead                equ     -42
_LVOSeek                equ     -66
_LVOCurrentDir          equ     -126

; graphics.library
_LVOLoadView            equ     -222
_LVOWaitBlit            equ     -228
_LVOWaitTOF             equ     -270
_LVOOwnBlitter          equ     -456
_LVODisownBlitter       equ     -462

; intuition.library
_LVODisplayBeep         equ     -96
_LVORethinkDisplay      equ     -390
_LVOEasyRequestArgs     equ     -588

; ciaa.resource / ciab.resource
_LVOAddICRVector        equ     -6
_LVORemICRVector        equ     -12
_LVOAbleICR             equ     -18
_LVOSetICR              equ     -24

MEMF_CHIP               equ     2
MODE_OLDFILE            equ     1005
OFFSET_BEGINNING        equ     -1
OFFSET_END              equ     1
NT_INTERRUPT            equ     2

; struct Node / Message / Task / Process
LN_TYPE                 equ     8
LN_PRI                  equ     9
LN_NAME                 equ     10
MN_REPLYPORT            equ     14
MN_LENGTH               equ     18
pr_MsgPort              equ     92
pr_CLI                  equ     172

; struct WBStartup / WBArg
sm_NumArgs              equ     28
sm_ArgList              equ     36
wa_Lock                 equ     0
wa_Name                 equ     4

; struct Interrupt (22 bytes)
IS_DATA                 equ     14
IS_CODE                 equ     18
IS_SIZE                 equ     22

; struct IOAudio (68 bytes)
IO_FLAGS                equ     30
IO_ERROR                equ     31
ioa_Data                equ     34
ioa_Length              equ     38
IOA_SIZE                equ     68
ADIOF_NOWAIT            equ     $40

; struct GfxBase
gb_ActiView             equ     34
gb_copinit              equ     38

; struct EasyStruct (20 bytes)
es_StructSize           equ     0
es_Flags                equ     4
es_Title                equ     8
es_TextFormat           equ     12
es_GadgetFormat         equ     16

; custom chips (HRM)
DMACONR                 equ     $002
ADKCONR                 equ     $010
INTENAR                 equ     $01c
COP1LC                  equ     $080
COPJMP1                 equ     $088
ADKCON                  equ     $09e

; CIAs (HRM appendix F): ICR bits
CIAICRB_TA              equ     0
CIAICRB_TB              equ     1
CIAA_PRA                equ     $bfe001
CIAA_CRA                equ     $bfee01
CIAB_PRA                equ     $bfd000
