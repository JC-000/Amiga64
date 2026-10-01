/* fixtures/c/pf_dosbuf.c — W10-V fix round 1 (F2): dos.library Read/Write
 * with a guest buffer in the trapped I/O banks $BF/$DF.  The HLE DOS moves
 * bulk data with the REC directly, so such a buffer would reach the REU
 * bytes under the trap (the palette LUT is at $DF0000).  Each must fail:
 * D0 = -1, IoErr = 212 (ERROR_OBJECT_WRONG_TYPE), nothing moved.  Normal
 * buffers still work.  The check covers the bytes the transfer moves (a
 * Read near EOF moves less than D3), so lut.bin is large enough that every
 * case below moves its full length.  Host-staged: lut.bin (66048 x $77).
 * Built at test time by tests/run_pf.py (leg "dosbuf"); never commit
 * compiled output. */
#include <stdio.h>
extern void *DOSBase;
long __Open(__reg("a6") void *, __reg("d1") const char *, __reg("d2") long) = "\tjsr\t-30(a6)";
long __Close(__reg("a6") void *, __reg("d1") long) = "\tjsr\t-36(a6)";
long __Read(__reg("a6") void *, __reg("d1") long, __reg("d2") void *, __reg("d3") long) = "\tjsr\t-42(a6)";
long __Write(__reg("a6") void *, __reg("d1") long, __reg("d2") const void *, __reg("d3") long) = "\tjsr\t-48(a6)";
long __IoErr(__reg("a6") void *) = "\tjsr\t-132(a6)";

#define MODE_OLDFILE 1005
#define MODE_NEWFILE 1006

static unsigned char buf[64];

static void rd(long fh, unsigned long a, long n)
{
    long r = __Read(DOSBase, fh, (void *)a, n);
    printf("r %06lx %ld %ld\n", a, r, r < 0 ? __IoErr(DOSBase) : 0L);
}

static void wr(long fh, unsigned long a, long n)
{
    long r = __Write(DOSBase, fh, (const void *)a, n);
    printf("w %06lx %ld %ld\n", a, r, r < 0 ? __IoErr(DOSBase) : 0L);
}

int main(void)
{
    long fh = __Open(DOSBase, "lut.bin", MODE_OLDFILE);
    long fo;

    if (fh == 0)
        return 10;
    rd(fh, 0xDF0000UL, 4096);              /* the LUT itself */
    rd(fh, 0xDEFF00UL, 0x200);             /* straddles into $DF */
    rd(fh, 0xBFE000UL, 16);                /* CIA bank */
    rd(fh, 0xBEFFF0UL, 0x10020);           /* covers all of $BF */
    rd(fh, (unsigned long)buf, 16);        /* a normal buffer: 16 */
    __Close(DOSBase, fh);
    fo = __Open(DOSBase, "out.bin", MODE_NEWFILE);
    if (fo == 0)
        return 11;
    wr(fo, 0xDF0800UL, 64);                /* from the LUT */
    wr(fo, 0xBEFFF0UL, 32);                /* straddles into $BF */
    wr(fo, (unsigned long)buf, 8);         /* a normal buffer: 8 */
    __Close(DOSBase, fo);
    printf("buf %02x\n", buf[0]);
    return 0;
}
