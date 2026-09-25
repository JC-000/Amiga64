/* fixtures/c/heap_realloc.c — realloc churn (W5-H).  vc.lib's realloc
 * always mallocs the new size, copies min(old, new) bytes and frees the
 * old block (a shrink moves too — measured on the base allocator).  Each
 * round grows a 64-byte block to ~400 KB and shrinks it back, checking that
 * the first 64 bytes survive both moves; a small block allocated each
 * round (and freed two rounds later) keeps the holes uneven.  ROUNDS
 * rounds allocate ~1.6 x the 7 MB HLE heap in total, so the run only
 * passes when realloc's free() makes the memory reusable.
 * Exit code 0 = ok; 10 = realloc failed, 20 = contents lost, 30 = small
 * block corrupt.
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>
#include <stdlib.h>

#define ROUNDS 30
#define HEAD   64

static int check(unsigned char *p, int r)
{
    int i;
    for (i = 0; i < HEAD; i++)
        if (p[i] != (unsigned char)(r * 3 + i))
            return 0;
    return 1;
}

int main(void)
{
    unsigned char *p, *n, *small[ROUNDS];
    unsigned long big;
    int r, i;

    p = realloc(NULL, HEAD);            /* realloc(NULL, n) == malloc(n) */
    if (p == NULL)
        return 10;
    for (r = 0; r < ROUNDS; r++) {
        for (i = 0; i < HEAD; i++)
            p[i] = (unsigned char)(r * 3 + i);
        big = 400000UL + (unsigned long)r * 1000;
        n = realloc(p, big);
        if (n == NULL) {
            printf("round %d grow failed\n", r);
            return 10;
        }
        if (!check(n, r))
            return 20;
        n[big - 1] = 0x77;
        p = realloc(n, HEAD);
        if (p == NULL) {
            printf("round %d shrink failed\n", r);
            return 10;
        }
        if (!check(p, r))
            return 20;
        small[r] = malloc(24 + r * 8);
        if (small[r] == NULL)
            return 10;
        small[r][0] = (unsigned char)r;
        if (r >= 2) {
            if (small[r - 2][0] != (unsigned char)(r - 2))
                return 30;
            free(small[r - 2]);
        }
    }
    free(small[ROUNDS - 1]);
    free(small[ROUNDS - 2]);
    free(p);
    printf("realloc ok %d rounds\n", ROUNDS);
    return 0;
}
