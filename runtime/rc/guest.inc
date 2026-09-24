; ---------------------------------------------------------------------------
; runtime/rc/guest.inc — guest (68k / REU) address-space resource contract.
; Owner: rc.
;
; SINGLE SOURCE OF TRUTH for every fixed guest address (guest address A
; maps 1:1 to REU offset A, 24-bit space).  Consumed via runtime/macros.inc;
; the Python mirrors (host/hle_env.py, host/image.py) are cross-checked
; against this file by host/rescheck.py (`make rescheck`).
;
; Every equate is .ifndef-guarded (relocatable via `ca65 -D name=value`;
; pass 0x-hex through make/shell — see docs/RESOURCES.md for the `$`
; gotcha).  Region grammar: docs/RESOURCES.md.
; ---------------------------------------------------------------------------

; @space guest $000000-$FFFFFF

; @region sys $000000-$0003FF 68k vector page - AbsExecBase pointer at $000004 (host/hle_env.py stages it)
.ifndef ABSEXECBASE_ADDR
ABSEXECBASE_ADDR = $000004  ; 32-bit BE pointer to the fake ExecBase
.endif

; @region hle $000400-$000FFF HLE environment - library vector slots at base-LVO, staged by host/hle_env.py
.ifndef ENV_BASE
ENV_BASE  = $000400     ; env region base (region end $000FFF inclusive)
.endif
; --- WS-B1 part $000400-$000BFF (docs/WAVE4_BRIEF.md C4; layout docs/HLE.md)
.ifndef ENV_STATE
ENV_STATE = $000400     ; HLE state block, 8 bytes BE: heap_next.l cursor.w column.b
.endif
.ifndef ENV_CLOCK
ENV_CLOCK = $000408     ; HLE clock, 8 bytes BE: ds_Days.l ds_Minute.w ds_Tick.w (DateStamp)
.endif
.ifndef ENV_LAUNCH
ENV_LAUNCH = $000410    ; CLI launch stub, 12 bytes (lea args,a0/moveq/jmp)
.endif
.ifndef ENV_ARGS
ENV_ARGS  = $000420     ; CLI argument string buffer, 32 bytes
.endif
.ifndef EXIT_ADDR
EXIT_ADDR = $000440     ; $AFFF HALT word - final-RTS landing site
.endif
.ifndef ENV_CLI_NAME
ENV_CLI_NAME = $000444  ; BSTR command-name buffer, 60 bytes
.endif
.ifndef ENV_CLI
ENV_CLI   = $000480     ; struct CommandLineInterface, 64 bytes
.endif
.ifndef ENV_PROCESS
ENV_PROCESS = $000A40   ; struct Process, 228 bytes (FindTask(NULL) result)
.endif
.ifndef FH_CONOUT
FH_CONOUT = $000B30     ; struct FileHandle (44 bytes), fh_Args = 0 console out
.endif
.ifndef FH_CONIN
FH_CONIN  = $000B60     ; struct FileHandle (44 bytes), fh_Args = 1 console in
.endif
.ifndef FH_NIL
FH_NIL    = $000B90     ; struct FileHandle (44 bytes), fh_Args = 2 NIL:
.endif
; WS-B2 (docs/WAVE4_BRIEF.md C2/C4, docs/HLE_DOS.md): dos.library FileHandle
; pool in $000C00-$000FFF.  Slot n = FH_POOL + FH_SIZE*n, BPTR = addr >> 2,
; fh_Args (offset FH_ARGS_OFF) = FH_KIND_FILE0 + n while open.
.ifndef FH_POOL
FH_POOL   = $000C00     ; first FileHandle (longword aligned)
.endif
.ifndef FH_POOL_END
FH_POOL_END = $000FFF   ; last byte the pool may grow into (inclusive)
.endif

; @region run $001000-$00FFFF program stack - grows down from STACK_TOP; exit-address longword staged at $00F000-$00F003
.ifndef STACK_TOP
STACK_TOP = $00F000     ; initial A7 (host/image.py STACK_TOP); [A7] = EXIT_ADDR
.endif

; @region run $010000-$0EFFFF program load region - first Hunk segment at $010000 (host/image.py BASE_ADDR), 4-byte aligned segments follow
.ifndef PROG_BASE
PROG_BASE = $010000     ; guest load address of the first program segment
.endif
.ifndef PROG_END
PROG_END  = $0EFFFF     ; last byte of the program load region (host/image.py PROG_END)
.endif

; @region hle $0F0000-$0FFFFF HLE library bases + their full vector tables (WS-B1, docs/WAVE4_BRIEF.md C4 amendment 2026-09-24)
.ifndef ENV_LIBS
ENV_LIBS  = $0F0000     ; start of the staged library area (padding from here)
.endif
.ifndef EXEC_BASE
EXEC_BASE = $0F0400     ; ExecBase (632 bytes); slots LVO -6..-828 below it
.endif
.ifndef DOS_BASE
DOS_BASE  = $0F0C00     ; DOSBase (70 bytes); slots LVO -6..-1158 below it
.endif

; @region hle $100000-$7FFFFF HLE heap - exec AllocMem/FreeMem pool (WS-B1, docs/WAVE4_BRIEF.md C4)
.ifndef HEAP_BASE
HEAP_BASE = $100000     ; first byte of the AllocMem pool
.endif
.ifndef HEAP_END
HEAP_END  = $7FFFFF     ; last byte of the AllocMem pool (inclusive)
.endif

; @region free $800000-$DFFFFF UNALLOCATED guest space - claim via this file + rescheck green

; @region hle $E00000-$FFFFFF HLE DOS RAMDISK - dos.library file store, 2 MB (WS-B2, docs/HLE_DOS.md: header + 32-entry directory, data from +$1000)
.ifndef dosio_rd_base
dosio_rd_base = $E00000 ; RAMDISK header (magic "RDSK"); directory at +$20
.endif
.ifndef dosio_rd_end
dosio_rd_end  = $FFFFFF ; last RAMDISK byte (inclusive)
.endif

; --- Contract invariants (assemble-time; keep true under -D relocation) ----
.assert EXIT_ADDR >= ENV_BASE,        error, "exit stub must sit inside the HLE env region"
.assert EXEC_BASE - 828 >= ENV_LIBS,  error, "deepest exec.library vector slot (LVO -828) must stay inside the library region"
.assert DOS_BASE - 1158 >= EXEC_BASE + 632, error, "dos.library vector table must not overlap ExecBase"
.assert DOS_BASE + 70 <= $100000,     error, "DOSBase must end inside the library region"
.assert PROG_END < ENV_LIBS,          error, "program load region must end below the library tables"
.assert STACK_TOP < PROG_BASE,        error, "guest stack must sit below the program load base"
.assert HEAP_BASE > PROG_BASE,        error, "AllocMem pool must sit above the program load region"
.assert FH_POOL >= $000C00 .and FH_POOL_END <= $000FFF, error, "FileHandle pool must stay inside the WS-B2 env sub-region $000C00-$000FFF"
.assert (FH_POOL & 3) = 0,            error, "FileHandles must be longword aligned (BPTR)"
.assert dosio_rd_base > HEAP_END,     error, "RAMDISK must sit above the AllocMem pool"
.assert (HEAP_BASE & 7) = 0,          error, "AllocMem pool must be 8-byte aligned (MEM_BLOCKSIZE)"
.assert FH_NIL + 44 <= $000C00,       error, "WS-B1 env structures must stay below the WS-B2 part ($000C00)"
.assert ((FH_CONOUT | FH_CONIN | FH_NIL | ENV_PROCESS | ENV_CLI | ENV_CLI_NAME) & 3) = 0, error, "BPTR targets must be longword aligned"
