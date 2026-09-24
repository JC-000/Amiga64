/* fixtures/c/fio_big.c — a 3000-byte file through a stdio buffer that the
 * program places across guest $180000 (a 512 KB REU line) and NOT page
 * aligned: a big first malloc pushes the heap there, then setvbuf makes
 * that block the stream buffer, so every dos Read/Write uses it.
 * What this covers (m2-post fix1 F3):
 *  - REU side: tests/run_vbcc.py stages a pad file so that big.bin's
 *    extent starts at $E7FE00, $200 below the 512 KB line $E80000.  hle_dos
 *    moves data in chunks bounded by GUEST pages; with an odd buffer the
 *    chunk holding file offset $200 spans $E7FFxx-$E800xx on the RAMDISK
 *    side, and only dosio_dma's split keeps it from wrapping to $E00000
 *    (the header).  The nosplit mutant turns this program red.
 *  - Guest side: the buffer crossing $180000 is regression coverage only;
 *    guest chunks never cross a 256-byte page, so no guest-side DMA can
 *    straddle $180000 with or without the split.
 * Prints "straddle=1 odd=1" to prove both placements; exit 0 = all good. */
#include <stdio.h>
#include <stdlib.h>

#define LINE 0x180000UL
#define BUFSZ 1024
#define N 3000

static unsigned char pat(unsigned i) { return (unsigned char)(i * 7 + (i >> 8) + 1); }

int main(void)
{
    unsigned long probe, want;
    char *pad, *buf;
    FILE *f;
    unsigned i, bad = 0;
    int c;

    probe = (unsigned long)malloc(16);
    want = LINE - BUFSZ / 2 + 0x48;         /* buffer start: not page aligned */
    pad = malloc(want - probe - 64);        /* ends just below it */
    buf = malloc(BUFSZ);
    printf("straddle=%d odd=%d\n", (unsigned long)buf < LINE
           && (unsigned long)buf + BUFSZ > LINE,
           ((unsigned long)buf & 0xFF) != 0);
    if (!pad || !buf) return 10;

    f = fopen("big.bin", "w");
    if (!f) { printf("fopen w failed\n"); return 11; }
    setvbuf(f, buf, _IOFBF, BUFSZ);
    for (i = 0; i < N; i++)
        fputc(pat(i), f);
    fclose(f);

    f = fopen("big.bin", "r");
    if (!f) { printf("fopen r failed\n"); return 12; }
    setvbuf(f, buf, _IOFBF, BUFSZ);
    for (i = 0; i < N; i++) {
        c = fgetc(f);
        if (c != pat(i)) bad++;
    }
    c = fgetc(f);
    fclose(f);
    printf("read %u bad=%u eof=%d\n", i, bad, c == EOF);
    return bad ? 13 : 0;
}
