/* fixtures/c/heap_pool.c — exec memory pools, created and deleted many
 * times (W5-H).  Each round: CreatePool, NBLK AllocPooled blocks of BLK
 * bytes (stamped), one FreePooled, then DeletePool with the rest still
 * allocated.  ROUNDS * NBLK * BLK is about 1.5 x the 7 MB HLE heap, so the
 * loop only survives when DeletePool really frees every allocation of the
 * pool.  Odd rounds use a MEMF_CLEAR pool: its blocks land on memory the
 * previous round dirtied and must read back zero.
 * Exit code 0 = ok; 10 = CreatePool failed, 20 = AllocPooled failed,
 * 30 = MEMF_CLEAR block not zero, 40 = stamp corrupt.
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>

extern void *SysBase;
void *CreatePool(__reg("a6") void *, __reg("d0") unsigned long,
                 __reg("d1") unsigned long, __reg("d2") unsigned long)
    = "\tjsr\t-696(a6)";
void DeletePool(__reg("a6") void *, __reg("a0") void *) = "\tjsr\t-702(a6)";
void *AllocPooled(__reg("a6") void *, __reg("a0") void *,
                  __reg("d0") unsigned long) = "\tjsr\t-708(a6)";
void FreePooled(__reg("a6") void *, __reg("a0") void *, __reg("a1") void *,
                __reg("d0") unsigned long) = "\tjsr\t-714(a6)";

#define MEMF_CLEAR 0x10000UL
#define ROUNDS 40
#define NBLK   5
#define BLK    52000UL

int main(void)
{
    unsigned long *b[NBLK];
    void *pool;
    int r, i;

    for (r = 0; r < ROUNDS; r++) {
        pool = CreatePool(SysBase, (r & 1) ? MEMF_CLEAR : 0, 8192, 8192);
        if (pool == NULL) {
            printf("round %d CreatePool failed\n", r);
            return 10;
        }
        for (i = 0; i < NBLK; i++) {
            b[i] = AllocPooled(SysBase, pool, BLK);
            if (b[i] == NULL) {
                printf("round %d AllocPooled %d failed\n", r, i);
                return 20;
            }
            if ((r & 1) && (b[i][0] != 0 || b[i][BLK / 8] != 0
                            || b[i][BLK / 4 - 1] != 0)) {
                printf("round %d block %d not clear\n", r, i);
                return 30;
            }
            b[i][0] = b[i][BLK / 8] = b[i][BLK / 4 - 1] = 0xD1D10000UL + r;
        }
        for (i = 0; i < NBLK; i++)
            if (b[i][0] != 0xD1D10000UL + r || b[i][BLK / 4 - 1] != b[i][0])
                return 40;
        FreePooled(SysBase, pool, b[2], BLK);
        DeletePool(SysBase, pool);
    }
    printf("pools ok %d rounds\n", ROUNDS);
    return 0;
}
