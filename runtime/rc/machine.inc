; ---------------------------------------------------------------------------
; runtime/rc/machine.inc — guest (68k / REU) address space, MACHINE-MODE
; profile (S3.7, W11-A).  Owner: rc.
;
; The second guest profile of the resource contract (docs/S3_PLAN.md §3.5
; "Guest memory profile"; docs/CONTRACTS.md "Machine profile and AROS boot
; (S3.7)").  runtime/rc/guest.inc is the HLE profile (exec/dos tables,
; heap, RAMDISK); this one is the bare machine the AROS Kickstart
; replacement boots on: chip RAM from $000000, the two ROM banks, the
; trapped I/O banks, and nothing else.  The two profiles describe the SAME
; 16 MB REU at different times (one boot each); host/rescheck.py checks
; each profile on its own (overlap, coverage, equates in their regions) and
; that both reserve the I/O banks.
;
; NOT included by runtime/macros.inc (the runtime is profile-agnostic):
; consumers are the machine-mode boot driver tests/aros/mboot.s and the
; host loader host/machine.py (its mirror constants are cross-checked by
; rescheck).  The REU is flat RAM: no mirrors, no bus holes, no write
; protection (the boot model's memory model, S3_PLAN §3.1).  Every byte the
; loader does not stage is zero (host/machine.py fills the whole REU
; first): AROS's probes of the unmapped regions below then read zeros,
; exactly as in the boot model (no autoconfig board at $E80000, no
; cartridge ROM at $F00000, Gayle ID 0 at $DE1000, S3_PLAN §3.3).
; ---------------------------------------------------------------------------

; @space guest $000000-$FFFFFF

; @region aros $000000-$1FFFFF chip RAM, 2 MB (AROS sizes it to $200000 on the flat REU); the reset longs (SSP, PC) are staged at $000000-$000007 (overlay)
.ifndef AROS_CHIP
AROS_CHIP     = $000000 ; first chip-RAM byte; the ROM's first 8 bytes are copied here
.endif
.ifndef AROS_CHIP_END
AROS_CHIP_END = $1FFFFF ; last chip-RAM byte (inclusive)
.endif

; @region free $200000-$BEFFFF unmapped on the machine; zero-filled; AROS probes it and finds nothing (Akiko at $B80002 reads 0)
; @region io $BF0000-$BFFFFF CIA bank: trapped to chip.s/cia.s by the memory API, the REU under it unused

; @region aros $C00000-$DBFFFF slow RAM, 1.75 MB (AROS sizes it to $DC0000; SysBase lands here)
.ifndef AROS_SLOW
AROS_SLOW     = $C00000 ; first slow-RAM byte
.endif
.ifndef AROS_SLOW_END
AROS_SLOW_END = $DBFFFF ; last slow-RAM byte (inclusive)
.endif

; @region free $DC0000-$DEFFFF unmapped (no RTC; Gayle ID at $DE1000 reads 0: none); zero-filled
; @region disp $DF0000-$DF0FFF palette LUT in the REU under the trapped $DF bank (same as guest.inc: disp_init stashes it)
.ifndef disp_lut
disp_lut  = $DF0000     ; W10-V disp: OCS-RGB -> VIC LUT, 4096 bytes (index = $0RGB)
.endif
; @region io $DF1000-$DFFFFF custom-chip bank: trapped to chip.s by the memory API

; @region aros $E00000-$E7FFFF extension ROM bank, 512 KB (aros-ext.bin, staged by host/machine.py)
.ifndef AROS_EXT
AROS_EXT      = $E00000 ; aros-ext.bin load address
.endif
.ifndef AROS_EXT_END
AROS_EXT_END  = $E7FFFF ; last extension-ROM byte (inclusive)
.endif

; @region free $E80000-$F7FFFF unmapped: autoconfig at $E80000 reads zeros (no board), $F00000 holds no cartridge ROM; zero-filled

; @region aros $F80000-$FFFFFF Kickstart ROM bank, 512 KB (aros-rom.bin, staged by host/machine.py); reset SSP/PC are its first two longs
.ifndef AROS_ROM
AROS_ROM      = $F80000 ; aros-rom.bin load address
.endif
.ifndef AROS_ROM_END
AROS_ROM_END  = $FFFFFF ; last ROM byte (inclusive)
.endif

; --- Contract invariants (assemble-time) -----------------------------------
.assert AROS_ROM_END - AROS_ROM + 1 = $80000, error, "the Kickstart bank is 512 KB"
.assert AROS_EXT_END - AROS_EXT + 1 = $80000, error, "the extension bank is 512 KB"
.assert AROS_CHIP = 0,                        error, "chip RAM starts at 0 (the vector page and the reset longs)"
.assert disp_lut = $DF0000,                   error, "disp_lut sits where guest.inc puts it (disp.s is profile-agnostic)"
