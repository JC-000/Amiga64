/* fixtures/c/heap_coalesce.c — fragmentation, then coalescing (W5-H).
 * Six 1.1 MB blocks nearly fill the 7 MB HLE heap.  Freeing blocks 1 and 3
 * leaves two separate 1.1 MB holes, so 2 MB does not fit; freeing block 2
 * between them must merge all three into one 3.3 MB hole that takes a 3 MB
 * block.  Finally everything is freed and a single 7 000 000-byte block
 * must fit again (all neighbours, and the top of the heap, merged).
 * Exit code 0 = ok; 10+i = setup malloc i failed, 20 = 2 MB fit into a
 * fragmented heap, 30 = 3 MB did not fit after the merge, 31 = not inside
 * the merged hole, 40 = guard corrupt, 50 = the final 7 MB block failed.
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>
#include <stdlib.h>

#define N     6
#define BLK   1100000UL

static unsigned char *a[N];

static void stamp(int i)
{
    a[i][0] = (unsigned char)(0x40 + i);
    a[i][BLK - 1] = (unsigned char)(0x80 + i);
}

static int intact(int i)
{
    return a[i][0] == (unsigned char)(0x40 + i)
        && a[i][BLK - 1] == (unsigned char)(0x80 + i);
}

int main(void)
{
    unsigned char *b;
    int i;

    printf("coalesce\n");      /* stdio's buffer now, not in a hole later */
    for (i = 0; i < N; i++) {
        a[i] = malloc(BLK);
        if (a[i] == NULL) {
            printf("setup malloc %d failed\n", i);
            return 10 + i;
        }
        stamp(i);
    }
    free(a[1]);
    free(a[3]);
    b = malloc(2000000UL);
    printf("fragmented 2MB=%s\n", b ? "fit" : "NULL");
    if (b != NULL)
        return 20;
    free(a[2]);
    b = malloc(3000000UL);
    printf("merged 3MB=%s\n", b ? "fit" : "NULL");
    if (b == NULL)
        return 30;
    if (b < a[1] || b + 3000000UL > a[3] + BLK)
        return 31;
    b[0] = 1;
    b[3000000UL - 1] = 2;
    if (!intact(0) || !intact(4) || !intact(5))
        return 40;
    free(b);
    free(a[0]);
    free(a[5]);
    free(a[4]);
    b = malloc(7000000UL);
    printf("all freed 7MB=%s\n", b ? "fit" : "NULL");
    if (b == NULL)
        return 50;
    b[0] = 3;
    b[7000000UL - 1] = 4;
    free(b);
    return 0;
}
