/* fixtures/c/alloc.c — heap exercise for the HLE AllocMem/FreeMem path.
 * malloc/free of several sizes, including one block larger than 64 KB;
 * each block is filled with a per-block pattern and verified.  Blocks up
 * to 4 KB are written in full; the 70 000-byte block is written at a
 * stride of STRIDE bytes plus its first and last 256 bytes (a full pass
 * is ~6.8 M interpreted instructions, ~6 minutes in VICE warp, measured;
 * the stride keeps the whole range covered at ~1/10 of the cost).
 * Exit code 0 = all good, otherwise 10+/20+/30+ identifies the check.
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>
#include <stdlib.h>

#define NBLK 5
#define STRIDE 61
static const size_t sizes[NBLK] = { 16, 100, 1000, 4096, 70000 };

static unsigned char pat(size_t blk, size_t i)
{
    return (unsigned char)(blk * 37u + i * 7u + (i >> 8));
}

/* next index to touch in block b after index i */
static size_t step(size_t b, size_t i)
{
    size_t n = sizes[b];
    if (n <= 4096 || i < 256 || i >= n - 257)
        return i + 1;
    i += STRIDE;
    return i < n - 256 ? i : n - 256;
}

int main(void)
{
    unsigned char *p[NBLK];
    size_t b, i;
    unsigned long sum = 0;

    for (b = 0; b < NBLK; b++) {
        p[b] = malloc(sizes[b]);
        if (p[b] == NULL) {
            printf("malloc(%lu) failed\n", (unsigned long)sizes[b]);
            return 10 + (int)b;
        }
        for (i = 0; i < sizes[b]; i = step(b, i))
            p[b][i] = pat(b, i);
    }
    for (b = 0; b < NBLK; b++) {
        for (i = 0; i < sizes[b]; i = step(b, i)) {
            if (p[b][i] != pat(b, i)) {
                printf("block %lu byte %lu corrupt\n",
                       (unsigned long)b, (unsigned long)i);
                return 20 + (int)b;
            }
            sum += p[b][i];
        }
    }
    for (b = 0; b < NBLK; b++)
        free(p[b]);

    /* calloc must hand back zeroed memory */
    p[0] = calloc(3000, 1);
    if (p[0] == NULL)
        return 30;
    for (i = 0; i < 3000; i++)
        if (p[0][i] != 0) {
            printf("calloc byte %lu not zero\n", (unsigned long)i);
            return 31;
        }
    free(p[0]);

    printf("alloc ok %lu\n", sum);
    return 0;
}
