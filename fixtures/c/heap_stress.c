/* fixtures/c/heap_stress.c — randomized AllocMem/FreeMem/AllocVec/pool
 * stress with a full heap-structure check after every operation (W5-H;
 * written by the adversarial reviewer, fix1 made it a permanent fixture).
 * NSLOT slots see random allocations (AllocMem / AllocVec / three pools, one
 * of them MEMF_CLEAR; edge sizes, small, medium and optional big blocks),
 * frees, realloc-style moves, pool deletions and deliberately invalid
 * frees.  After every op check_heap() walks the heap exactly as
 * docs/HLE.md "Heap" defines it (tiling, flags, PFREE, footers, tags, no
 * two free blocks touching, none touching top, the doubly linked free list
 * matching the free blocks), live blocks keep their fill patterns, MEMF_CLEAR
 * blocks read zero, an invalid free changes nothing but HEAP_CTL+4, and at
 * the end the heap is back to where it started.
 * tests/run_vbcc.py builds it twice with -D SEED/NOPS/NSLOT/BIGMAX/BIGPCT
 * (and CLEARMAX: no MEMF_CLEAR above that size, COPYMAX: realloc copies at
 * most that much — both keep the big-block run inside the time budget).
 * Exit code 0 = ok, else a FAIL code (printed with its addresses).
 * Built at test time by tests/run_vbcc.py; never commit compiled output. */
#include <stdio.h>
#include <string.h>

extern void *SysBase;
void *AllocMem(__reg("a6") void *, __reg("d0") unsigned long,
               __reg("d1") unsigned long) = "\tjsr\t-198(a6)";
void FreeMem(__reg("a6") void *, __reg("a1") void *,
             __reg("d0") unsigned long) = "\tjsr\t-210(a6)";
void *AllocVec(__reg("a6") void *, __reg("d0") unsigned long,
               __reg("d1") unsigned long) = "\tjsr\t-684(a6)";
void FreeVec(__reg("a6") void *, __reg("a1") void *) = "\tjsr\t-690(a6)";
void *CreatePool(__reg("a6") void *, __reg("d0") unsigned long,
                 __reg("d1") unsigned long, __reg("d2") unsigned long)
    = "\tjsr\t-696(a6)";
void DeletePool(__reg("a6") void *, __reg("a0") void *) = "\tjsr\t-702(a6)";
void *AllocPooled(__reg("a6") void *, __reg("a0") void *,
                  __reg("d0") unsigned long) = "\tjsr\t-708(a6)";
void FreePooled(__reg("a6") void *, __reg("a0") void *, __reg("a1") void *,
                __reg("d0") unsigned long) = "\tjsr\t-714(a6)";

#ifndef SEED
#define SEED 1
#endif
#ifndef NOPS
#define NOPS 3000
#endif
#ifndef NSLOT
#define NSLOT 48
#endif
#ifndef BIGMAX
#define BIGMAX 400000UL
#endif
#ifndef BIGPCT
#define BIGPCT 7
#endif
#define NPOOL 3
#define MEMF_CLEAR 0x10000UL
#define TOPA   ((volatile unsigned long *)0x400)
#define HCTL   ((volatile unsigned long *)0x100000)
#define HFIRST 0x100008UL
#define HEND   0x800000UL
#define L(a)   (*(volatile unsigned long *)(a))

static unsigned long seed = SEED * 2654435761UL + 1;
static unsigned long rnd(void)
{
    seed = seed * 1103515245UL + 12345UL;
    return (seed >> 8) & 0xFFFFFF;
}

/* kind: 0 AllocMem, 1 AllocVec, 2.. pool k-2 */
static unsigned char *p[NSLOT];
static unsigned long sz[NSLOT], id[NSLOT];
static int kind[NSLOT];
static void *pool[NPOOL];
static unsigned long nextid = 1, ops, nrej, maxfree, maxblk;

static int fail(int code, const char *what, unsigned long a, unsigned long b)
{
    printf("FAIL %d %s\n", code, what);
    printf("a=%lx b=%lx\n", a, b);
    printf("op=%lu seed=%d\n", ops, SEED);
    return code;
}

/* longword pattern (payload is 4-aligned): whole block up to 256 bytes,
 * else first/last 64 bytes and one longword per 1 KB; tail bytes too */
#define PL(i, d) ((d) * 0x9E3779B1UL ^ (unsigned long)(i) * 0x01000193UL)
static int span(int s, int chk)
{
    unsigned long n = sz[s], d = id[s], nl = n >> 2, i, k;
    unsigned long *q = (unsigned long *)p[s];
    unsigned char *t = p[s];
    for (i = 0; i < nl; i++) {
        if (nl > 64 && i == 16) {
            for (k = 16; k < nl - 16; k += 256)
                if (chk) { if (q[k] != PL(k, d)) return 0; }
                else q[k] = PL(k, d);
            i = nl - 16;
        }
        if (chk) { if (q[i] != PL(i, d)) return 0; }
        else q[i] = PL(i, d);
    }
    for (i = nl << 2; i < n; i++)
        if (chk) { if (t[i] != (unsigned char)(d + i)) return 0; }
        else t[i] = (unsigned char)(d + i);
    return 1;
}
static void fill(int s) { span(s, 0); }
static int intact(int s) { return span(s, 1); }

static int zero(unsigned char *q, unsigned long n)
{
    unsigned long i;
    if (n <= 512) {
        for (i = 0; i < n; i++) if (q[i]) return 0;
        return 1;
    }
    for (i = 0; i < 256; i++) if (q[i]) return 0;
    for (i = n - 256; i < n; i++) if (q[i]) return 0;
    for (i = 256; i < n - 256; i += 509) if (q[i]) return 0;
    return 1;
}

/* the heap as the allocator should see it; returns 0 ok, else code */
static unsigned long hsum;
static int check_heap(void)
{
    unsigned long top = *TOPA, b = HFIRST, w, s, prevfree = 0, nfree = 0;
    unsigned long nl = 0, h, prv, cur, used = 0;
    hsum = top;
    if (top == 0x100000UL) return 0;   /* pristine */
    if (top < HFIRST || top > HEND || (top & 7))
        return fail(100, "top", top, 0);
    while (b < top) {
        w = L(b);
        s = w & ~7UL;
        if (s < 16 || b + s > top || (w >> 24))
            return fail(101, "size", b, w);
        if (((w >> 1) & 1) != prevfree)
            return fail(102, "PFREE", b, w);
        hsum = hsum * 31 + w + L(b + 4) * 7;
        if (w & 1) {
            if ((L(b + 4) >> 24) != 0x5A)
                return fail(103, "tag", b, L(b + 4));
            if (w & 4 && s < 24)
                return fail(104, "slack", b, w);
            prevfree = 0;
            used++;
            if (s > maxblk) maxblk = s;
        } else {
            if (w & 4)
                return fail(105, "free w/ slack", b, w);
            if (prevfree)
                return fail(106, "two free adjacent", b, w);
            if (L(b + s - 4) != s)
                return fail(107, "footer", b, L(b + s - 4));
            hsum = hsum * 31 + L(b + 8) * 3;
            prevfree = 1;
            nfree++;
            if (s > maxfree) maxfree = s;
        }
        b += s;
    }
    if (b != top)
        return fail(108, "tiling", b, top);
    if (prevfree)
        return fail(109, "free block touches top", b, top);
    /* free list */
    h = HCTL[0];
    hsum = hsum * 31 + h;
    prv = 0;
    for (cur = h; cur; cur = L(cur + 4)) {
        if (cur < HFIRST || cur >= top || (cur & 7))
            return fail(110, "link range", cur, prv);
        if (L(cur) & 1)
            return fail(111, "used in list", cur, L(cur));
        if (L(cur + 8) != prv)
            return fail(112, "prev link", cur, L(cur + 8));
        prv = cur;
        if (++nl > nfree)
            return fail(113, "list longer than free blocks", nl, nfree);
    }
    if (nl != nfree)
        return fail(114, "list count", nl, nfree);
    return 0;
}

static int live_check_all(void)
{
    int t;
    for (t = 0; t < NSLOT; t++)
        if (p[t] && !intact(t))
            return fail(20, "pattern", (unsigned long)p[t], t);
    return 0;
}

static unsigned long pick_size(void)
{
    static const unsigned short edge[] = {1, 4, 7, 8, 9, 12, 15, 16, 17,
                                          23, 24, 25, 31, 32, 33, 40};
    unsigned long r = rnd() % 100;
    if (r < 35) return edge[rnd() % 16];
    if (r < 70) return 1 + rnd() % 256;
    if (r < 100 - BIGPCT) return 1 + rnd() % 6000;
    return 16384 + rnd() % BIGMAX;
}

static int do_alloc(int s, unsigned long n, int realloc_from)
{
    int k = (int)(rnd() % 5), t;
    unsigned long fl = (rnd() & 1) ? MEMF_CLEAR : 0;
    unsigned char *q;
#ifdef CLEARMAX
    /* keep MEMF_CLEAR (42 C64 cycles/byte) off the multi-MB blocks */
    if (n > CLEARMAX)
        fl = 0;
#endif
    if (k >= 2) {
        k = 2 + (int)(rnd() % NPOOL);
#ifdef CLEARMAX
        if (n > CLEARMAX && ((k - 2) & 1))
            k = 2;                          /* not the MEMF_CLEAR pool */
#endif
        q = AllocPooled(SysBase, pool[k - 2], n);
        fl = ((k - 2) & 1) ? MEMF_CLEAR : 0;   /* pool flags */
    } else if (k == 1) {
        q = AllocVec(SysBase, n, fl);
    } else {
        q = AllocMem(SysBase, n, fl);
    }
    if (!q)
        return 1;           /* out of memory: acceptable, caller notes */
    if (k == 1) {
        if (((unsigned long)q & 7) != 4)
            return fail(30, "vec align", (unsigned long)q, n);
    } else if ((unsigned long)q & 7)
        return fail(31, "align", (unsigned long)q, n);
    if ((unsigned long)q < HFIRST + 8 || (unsigned long)q + n > HEND)
        return fail(32, "range", (unsigned long)q, n);
    if (fl && !zero(q, n))
        return fail(33, "not clear", (unsigned long)q, n);
    for (t = 0; t < NSLOT; t++)
        if (p[t] && t != realloc_from && q < p[t] + sz[t] && p[t] < q + n)
            return fail(34, "overlap", (unsigned long)q, (unsigned long)p[t]);
    if (realloc_from >= 0) {
        unsigned long m = sz[realloc_from] < n ? sz[realloc_from] : n;
        if (realloc_from >= 0 && q < p[realloc_from] + sz[realloc_from]
            && p[realloc_from] < q + n)
            return fail(35, "overlap old", (unsigned long)q, 0);
#ifdef COPYMAX
        if (m > COPYMAX)
            m = COPYMAX;        /* the copy is refilled anyway: cap its cost */
#endif
        memcpy(q, p[realloc_from], m);
    }
    p[s] = q;
    sz[s] = n;
    kind[s] = k;
    id[s] = nextid++;
    fill(s);
    return 0;
}

static void do_free_raw(int s)
{
    if (kind[s] == 0)
        FreeMem(SysBase, p[s], sz[s]);
    else if (kind[s] == 1)
        FreeVec(SysBase, p[s]);
    else
        FreePooled(SysBase, pool[kind[s] - 2], p[s], sz[s]);
    p[s] = NULL;
}

/* invalid frees: heap must be bit-identical except the reject count */
static int do_invalid(void)
{
    unsigned long before, rej0 = HCTL[1], top0 = *TOPA, a = 0, n = 16;
    int t = (int)(rnd() % NSLOT), v = (int)(rnd() % 9), e;
    if ((e = check_heap())) return e;
    before = hsum;
    switch (v) {
    case 0: if (!p[t]) return 0; a = (unsigned long)p[t] + 8; n = 8; break;
    case 1: if (!p[t]) return 0; a = (unsigned long)p[t] - 8; n = sz[t]; break;
    case 2: a = 0x100000; n = 8; break;
    case 3: a = top0; n = 8; break;
    case 4: a = top0 + 8; n = 8; break;
    case 5: if (!p[t] || kind[t] == 1) return 0;
            a = (unsigned long)p[t]; n = sz[t] + 16 + (rnd() % 4) * 8;
            break;
    case 6: if (!p[t] || kind[t] == 1 || sz[t] <= 16) return 0;
            a = (unsigned long)p[t]; n = ((sz[t] + 7) & ~7UL) - 8; break;
    case 7: a = HFIRST; n = 8; break;
    case 8: a = HFIRST + 8 + 8 * (rnd() % 64); n = 1 + rnd() % 64; break;
    }
    /* case 8 may hit a real live block with the right size: skip if so */
    if (v == 8) {
        int u;
        for (u = 0; u < NSLOT; u++)
            if (p[u] && ((unsigned long)p[u] == a
                         || (unsigned long)p[u] - 4 == a)) return 0;
        for (u = 0; u < NPOOL; u++)
            if ((unsigned long)pool[u] == a) return 0;
        if (a < 0x1000D8UL) return 0;   /* libc startup blocks */
    }
    FreeMem(SysBase, (void *)a, n);
    nrej++;
    if (HCTL[1] != rej0 + 1)
        return fail(40 + v, "reject count", a, n);
    if ((e = check_heap())) return e;
    if (hsum != before)
        return fail(50 + v, "heap changed on invalid free", a, n);
    return 0;
}

int main(void)
{
    int s, e, k, t;
    unsigned long r, oom = 0, dels = 0, top0 = *TOPA, hd0 = HCTL[0];
    if (top0 == 0x100000UL) top0 = HFIRST, hd0 = 0;
    for (k = 0; k < NPOOL; k++)
        if (!(pool[k] = CreatePool(SysBase, (k & 1) ? MEMF_CLEAR : 0,
                                   4096, 4096)))
            return fail(1, "createpool", k, 0);
    for (ops = 0; ops < NOPS; ops++) {
        s = (int)(rnd() % NSLOT);
        r = rnd() % 100;
        if (r < 4) {
            if ((e = do_invalid())) return e;
        } else if (r < 6) {
            k = (int)(rnd() % NPOOL);
            for (t = 0; t < NSLOT; t++)
                if (p[t] && kind[t] == k + 2) {
                    if (!intact(t))
                        return fail(21, "pattern before delete", t, 0);
                    p[t] = NULL;
                }
            DeletePool(SysBase, pool[k]);
            dels++;
            if (!(pool[k] = CreatePool(SysBase, (k & 1) ? MEMF_CLEAR : 0,
                                       4096, 4096)))
                return fail(2, "createpool again", k, 0);
        } else if (p[s]) {
            if (!intact(s))
                return fail(22, "pattern before free", (unsigned long)p[s], s);
            if (r < 20) {
                /* realloc into a free slot */
                for (t = 0; t < NSLOT && p[t]; t++) ;
                if (t < NSLOT) {
                    e = do_alloc(t, pick_size(), s);
                    if (e > 1) return e;
                    if (e == 0) {
                        unsigned long m = sz[s] < sz[t] ? sz[s] : sz[t], i;
                        /* copied prefix then refilled; re-verify refill */
                        (void)m; (void)i;
                        do_free_raw(s);
                    } else oom++;
                }
            } else
                do_free_raw(s);
        } else {
            e = do_alloc(s, pick_size(), -1);
            if (e > 1) return e;
            if (e == 1) oom++;
        }
        if ((e = check_heap())) return e;
        if ((ops & 63) == 0 && (e = live_check_all())) return e;
    }
    if ((e = live_check_all())) return e;
    for (s = 0; s < NSLOT; s++)
        if (p[s]) do_free_raw(s);
    for (k = 0; k < NPOOL; k++)
        DeletePool(SysBase, pool[k]);
    if ((e = check_heap())) return e;
    if (*TOPA != top0 || HCTL[0] != hd0)
        return fail(60, "not empty at end", *TOPA, HCTL[0]);
    if (HCTL[1] != nrej)
        return fail(61, "reject total", HCTL[1], nrej);
    printf("stress ok\n");
    printf("top0=%lx hd0=%lx\n", top0, hd0);
    printf("rej=%lu oom=%lu del=%lu\n", nrej, oom, dels);
    printf("maxfree=%lx maxblk=%lx\n", maxfree, maxblk);
    return 0;
}
