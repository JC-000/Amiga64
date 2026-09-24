; FindTask, SetSignal, OpenLibrary of an unknown library, ExecBase fields.
        movea.l 4.w,a6
        suba.l  a1,a1
        jsr     -294(a6)            ; FindTask(NULL)
        move.l  d0,d2               ; = ENV_PROCESS
        lea     name(pc),a1
        jsr     -294(a6)            ; FindTask("nobody")
        move.l  d0,d3               ; 0
        move.l  #$1000,d0           ; newSignals = CTRL_C, mask 0: changes
        moveq   #0,d1               ; nothing, returns the old state
        jsr     -306(a6)            ; SetSignal($1000, 0)
        move.l  d0,d4               ; 0 (must be written: D0 was $1000)
        lea     libname(pc),a1
        moveq   #0,d0
        jsr     -552(a6)            ; OpenLibrary("intuition.library")
        move.l  d0,d5               ; 0
        moveq   #0,d6
        move.w  20(a6),d6           ; lib_Version = 40
        movea.l d2,a0
        move.l  $ac(a0),d7          ; pr_CLI = ENV_CLI>>2
        rts
name:   dc.b    "nobody",0
libname: dc.b   "intuition.library",0
        even
