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
.ifndef EXIT_ADDR
EXIT_ADDR = $000440     ; $AFFF HALT word - final-RTS landing site
.endif
.ifndef EXEC_BASE
EXEC_BASE = $000800     ; fake exec.library base (vector slots below it)
.endif
.ifndef DOS_BASE
DOS_BASE  = $000A00     ; fake dos.library base (vector slots below it)
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

; @region run $010000-$0FFFFF program load region - first Hunk segment at $010000 (host/image.py BASE_ADDR), 4-byte aligned segments follow
.ifndef PROG_BASE
PROG_BASE = $010000     ; guest load address of the first program segment
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
.assert EXEC_BASE - 552 >= ENV_BASE,  error, "deepest exec.library vector slot (LVO -552) must stay inside the env region"
.assert DOS_BASE - 60 >= ENV_BASE,    error, "deepest dos.library vector slot (LVO -60) must stay inside the env region"
.assert STACK_TOP < PROG_BASE,        error, "guest stack must sit below the program load base"
.assert HEAP_BASE > PROG_BASE,        error, "AllocMem pool must sit above the program load region"
.assert DOS_BASE - 132 >= ENV_BASE,   error, "deepest WS-B2 dos.library vector slot (IoErr, LVO -132) must stay inside the env region"
.assert FH_POOL >= $000C00 .and FH_POOL_END <= $000FFF, error, "FileHandle pool must stay inside the WS-B2 env sub-region $000C00-$000FFF"
.assert (FH_POOL & 3) = 0,            error, "FileHandles must be longword aligned (BPTR)"
.assert dosio_rd_base > HEAP_END,     error, "RAMDISK must sit above the AllocMem pool"
