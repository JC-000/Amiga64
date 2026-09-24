/* fixtures/c/fio_big.c — a 3000-byte file through a stdio buffer that the
 * program places across guest $180000 (a 512 KB REU line): a big first
 * malloc pushes the heap there, then setvbuf makes that straddling block
 * the stream buffer, so every dos Read/Write uses it.  Exit code 0 = all
 * good; "straddle=1" proves the placement. */
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
    want = LINE - BUFSZ / 2;                /* buffer start */
    pad = malloc(want - probe - 64);        /* ends just below it */
    buf = malloc(BUFSZ);
    printf("straddle=%d\n", (unsigned long)buf < LINE
           && (unsigned long)buf + BUFSZ > LINE);
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
