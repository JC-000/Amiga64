/* fixtures/c/heap_churn.c — malloc/free in pseudo-random order (W5-H).
 * NSLOT slots; each step either frees the slot's block (after checking the
 * guard words at its start and end) or mallocs a new block of a random
 * size (16 B .. ~200 KB) and stamps it.  Every new block is checked not to
 * overlap any live block.  The run allocates more than twice the 7 MB HLE
 * heap in total while at most NSLOT blocks (< 2 MB) are live, so it only
 * passes when freed memory is reused in any order.
 * Exit code 0 = ok; 10 = malloc failed, 20 = overlap, 30 = guard corrupt.
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>
#include <stdlib.h>

#define NSLOT 10
#define GOAL  (16UL * 1024 * 1024)      /* bytes allocated in total */

static unsigned long seed = 12345;
static unsigned long rnd(void)
{
    seed = seed * 1103515245UL + 12345UL;
    return seed >> 8;
}

static unsigned char *p[NSLOT];
static unsigned long sz[NSLOT];

static unsigned long tag(int s, unsigned long n)
{
    return 0x5EED0000UL ^ ((unsigned long)s << 20) ^ n;
}

static void stamp(int s)
{
    unsigned long n = sz[s];
    *(unsigned long *)p[s] = tag(s, n);
    *(unsigned long *)(p[s] + n - 4) = ~tag(s, n);
}

static int intact(int s)
{
    unsigned long n = sz[s];
    return *(unsigned long *)p[s] == tag(s, n)
        && *(unsigned long *)(p[s] + n - 4) == ~tag(s, n);
}

int main(void)
{
    unsigned long total = 0, steps = 0, frees = 0, maxlive = 0;
    int s, t;

    while (total < GOAL) {
        steps++;
        s = (int)(rnd() % NSLOT);
        if (p[s]) {
            if (!intact(s)) {
                printf("slot %d guard corrupt\n", s);
                return 30;
            }
            free(p[s]);
            p[s] = NULL;
            frees++;
            continue;
        }
        sz[s] = 16 + (rnd() % 200000UL) / 4 * 4;
        p[s] = malloc(sz[s]);
        if (p[s] == NULL) {
            printf("malloc(%lu) failed\n", sz[s]);
            printf("after %lu bytes\n", total);
            return 10;
        }
        for (t = 0; t < NSLOT; t++)
            if (t != s && p[t] && p[s] < p[t] + sz[t] && p[t] < p[s] + sz[s]) {
                printf("slot %d overlaps %d\n", s, t);
                return 20;
            }
        stamp(s);
        total += sz[s];
        {
            unsigned long live = 0;
            for (t = 0; t < NSLOT; t++)
                if (p[t])
                    live += sz[t];
            if (live > maxlive)
                maxlive = live;
        }
    }
    for (s = 0; s < NSLOT; s++)
        if (p[s]) {
            if (!intact(s)) {
                printf("slot %d guard corrupt\n", s);
                return 30;
            }
            free(p[s]);
        }
    printf("churn ok\n");
    printf("total>2x heap=%d\n", total > 2 * 7340032UL);
    printf("live<2MB=%d frees>50=%d\n", maxlive < 2097152UL, frees > 50);
    return 0;
}
