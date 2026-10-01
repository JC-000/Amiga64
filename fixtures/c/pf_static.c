/* fixtures/c/pf_static.c — M3 fixture: a static 2-plane lores playfield
 * (docs/S3_PLAN.md §4 "S3.5", docs/WAVE10_BRIEF.md C3).  HLE program,
 * built at test time by tests/run_pf.py (leg "fixture"); never commit
 * compiled output.
 *
 * Plane 0 and plane 1 live in two separate buffers whose rows carry junk
 * bytes after the 40 displayed ones, so the picture is right only if the
 * modulos are honoured (BPL1MOD = 40, BPL2MOD = 8) and the plane pointers
 * are taken in order.  The copper list sets BPL1PT/BPL2PT, COLOR00-03,
 * BPLCON0 $2200, BPL1MOD/BPL2MOD and DIW/DDF (320x200 at $2C81-$F4C1,
 * DDF $38-$D0); the CPU pokes COP1LC, strobes COPJMP1 (loads the copper
 * PC) and turns copper + bitplane DMA on (the copper runs: decoy palette).
 * It then writes the final palette into the same list and strobes COPJMP1
 * again (runs again), so the display must follow a change (gen).
 *
 * Before the display setup it stores $FFFFFFFF over guest $DF0000-$DF0FFF,
 * the REU bytes that hold the palette LUT (disp_lut): bank $DF traps to
 * the custom-chip open bus, so the picture's colours must not change.
 *
 * Prints the plane and copper-list addresses (the host builds its
 * reference from them and from the REU contents); exit code 0, or 10+ on
 * an allocation failure. */
#include <stdio.h>
#include <stdlib.h>

#define W 40                     /* bytes per displayed row */
#define H 200
#define S0 80                    /* plane 0 row stride: BPL1MOD = 40 */
#define S1 48                    /* plane 1 row stride: BPL2MOD = 8 */

#define CUSTOM ((volatile unsigned short *)0xDFF000)
#define REG(off) CUSTOM[(off) >> 1]
#define COP1LC  0x080
#define COPJMP1 0x088
#define DMACON  0x096
#define LUT_SHADOW ((volatile unsigned long *)0xDF0000)   /* 4 KB, trapped */

static const unsigned short final_pal[4] = { 0x0000, 0x0FFF, 0x0F00, 0x000F };
static const unsigned short decoy_pal[4] = { 0x0FFF, 0x000F, 0x0000, 0x0F00 };

int main(void)
{
    unsigned char *p0 = malloc(S0 * H);
    unsigned char *p1 = malloc(S1 * H);
    unsigned short *cl = malloc(64 * 2);
    unsigned short *c;
    unsigned short *colw;
    unsigned int x, y;
    unsigned long a0, a1;

    if (p0 == NULL || p1 == NULL || cl == NULL)
        return 10;
    for (y = 0; y < H; y++) {
        unsigned char *r0 = p0 + y * S0, *r1 = p1 + y * S1;
        for (x = 0; x < W; x++) {
            r0[x] = (unsigned char)(x * 7 + y * 13) ^ (unsigned char)(y >> 2);
            r1[x] = (unsigned char)(x * 11 + y * 5 + 0x5A);
        }
        for (x = W; x < S0; x++)
            r0[x] = (unsigned char)(0xC3 ^ x ^ y);    /* junk: modulo bytes */
        for (x = W; x < S1; x++)
            r1[x] = (unsigned char)(0x3C ^ x ^ y);
    }
    for (x = 0; x < 1024; x++)
        LUT_SHADOW[x] = 0xFFFFFFFFUL;                 /* must not reach the LUT */
    a0 = (unsigned long)p0;
    a1 = (unsigned long)p1;
    c = cl;
    *c++ = 0x0E0; *c++ = (unsigned short)(a0 >> 16);  /* BPL1PTH */
    *c++ = 0x0E2; *c++ = (unsigned short)a0;          /* BPL1PTL */
    *c++ = 0x0E4; *c++ = (unsigned short)(a1 >> 16);  /* BPL2PTH */
    *c++ = 0x0E6; *c++ = (unsigned short)a1;          /* BPL2PTL */
    *c++ = 0x108; *c++ = S0 - W;                      /* BPL1MOD */
    *c++ = 0x10A; *c++ = S1 - W;                      /* BPL2MOD */
    *c++ = 0x08E; *c++ = 0x2C81;                      /* DIWSTRT */
    *c++ = 0x090; *c++ = 0xF4C1;                      /* DIWSTOP */
    *c++ = 0x092; *c++ = 0x0038;                      /* DDFSTRT */
    *c++ = 0x094; *c++ = 0x00D0;                      /* DDFSTOP */
    *c++ = 0x102; *c++ = 0x0000;                      /* BPLCON1 */
    *c++ = 0x104; *c++ = 0x0000;                      /* BPLCON2 */
    colw = c;
    for (x = 0; x < 4; x++) {
        *c++ = 0x180 + 2 * x; *c++ = decoy_pal[x];    /* COLOR00-03 */
    }
    *c++ = 0x100; *c++ = 0x2200;                      /* BPLCON0: 2 planes, colour */
    *c++ = 0xFFFF; *c++ = 0xFFFE;                     /* end */

    *(volatile unsigned long *)&REG(COP1LC) = (unsigned long)cl;
    REG(COPJMP1) = 0;                                 /* strobe: loads the PC */
    REG(DMACON) = 0x8380;                             /* SET DMAEN BPLEN COPEN:
                                                         runs it, decoy picture */
    for (x = 0; x < 4; x++)
        colw[2 * x + 1] = final_pal[x];
    REG(COPJMP1) = 0;                                 /* strobe: final picture */

    printf("pf static\n");
    printf("P0=%06lx P1=%06lx\n", a0, a1);
    printf("CL=%06lx\n", (unsigned long)cl);
    return 0;
}
