/* fixtures/mod/link.ld — vlink script: one flat image at $010000, crt0 first
 * (_start = the image base); bss below $040000 (M0 ruling). */
SECTIONS {
  . = 0x10000;
  .text : { *crt0.o(CODE) *(CODE) *(code) *(.text) *(text) }
  .data : { *(DATA) *(data) *(.data) }
  .bss  : { __BSS_START = .; *(BSS) *(bss) *(.bss) *(COMMON) __BSS_END = .; }
  ASSERT(__BSS_END <= 0x40000, "image bss reaches $040000 (W15-U's area)")
}
