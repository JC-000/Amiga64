/* tools/s3probe/wbsnap.c: periodic Intuition screen dump for wb_fsuae.py.
 *
 * WB-measure (docs/WB_BOOT.md).  FS-UAE cannot take screenshots headless on
 * this host, so the chain-boot mode runs this program in the background
 * before the floppy's own Startup-Sequence.  Every INTERVAL ticks it writes
 * the front screen's bitplanes and palette to PREFIX<n>.raw and the screen
 * and window titles to PREFIX<n>.txt; wb_fsuae.py turns the .raw into PNG.
 *
 * Usage: wbsnap INTERVAL COUNT PREFIX      (e.g. wbsnap 150 40 DH0:snaps/s)
 *
 * No NDK: the few public structure offsets are spelled out below and the
 * library calls are vbcc inline stubs.
 *   IntuitionBase.FirstScreen 60; Screen: NextScreen 0, FirstWindow 4,
 *   Width 12, Height 14, Title 22, ViewPort 44 (ColorMap at +4),
 *   RastPort 84 (BitMap at +4); Window: NextWindow 0, Title 32;
 *   BitMap: BytesPerRow 0, Rows 2, Depth 5, Planes 8.
 * .raw layout (big-endian): "WBS1", u16 width, u16 height, u16 depth,
 * u16 bytes per row, 32 x u16 RGB4 colours, then depth planes of
 * height rows of (width + 15) / 16 * 2 bytes.
 */
#include <stdio.h>
#include <stdlib.h>

void *OpenLib(__reg("a6") void *sb, __reg("a1") const char *n,
              __reg("d0") unsigned long v) = "\tjsr\t-552(a6)";
long GetRGB4f(__reg("a6") void *gb, __reg("a0") void *cm,
              __reg("d0") long e) = "\tjsr\t-582(a6)";
void DelayF(__reg("a6") void *db, __reg("d1") long t) = "\tjsr\t-198(a6)";
void ForbidF(__reg("a6") void *sb) = "\tjsr\t-132(a6)";
void PermitF(__reg("a6") void *sb) = "\tjsr\t-138(a6)";

#define PTR(p, o) (*(unsigned char **)((unsigned char *)(p) + (o)))
#define W(p, o) (*(unsigned short *)((unsigned char *)(p) + (o)))
#define B(p, o) (*(unsigned char *)((unsigned char *)(p) + (o)))

static void put16(FILE *f, unsigned v)
{
    fputc((v >> 8) & 255, f);
    fputc(v & 255, f);
}

static void title(FILE *f, const char *what, unsigned char *s)
{
    fprintf(f, "%s \"%s\"\n", what, s ? (char *)s : "");
}

int main(int argc, char **argv)
{
    void *sysbase = *(void **)4;
    unsigned char *ib, *scr, *win, *bm, *vp;
    void *gb, *db;
    long interval, count, n, i, p, y;
    unsigned w, h, d, bpr, rowbytes;
    char name[128];
    FILE *f;

    if (argc < 4)
        return 20;
    interval = atol(argv[1]);
    count = atol(argv[2]);
    ib = OpenLib(sysbase, "intuition.library", 0);
    gb = OpenLib(sysbase, "graphics.library", 0);
    db = OpenLib(sysbase, "dos.library", 0);
    if (!ib || !gb || !db)
        return 20;
    for (n = 0; n < count; n++) {
        DelayF(db, interval);
        sprintf(name, "%s%02ld.txt", argv[3], n);
        if (!(f = fopen(name, "w")))
            continue;
        ForbidF(sysbase);
        for (scr = PTR(ib, 60); scr; scr = PTR(scr, 0)) {
            title(f, "screen", PTR(scr, 22));
            for (win = PTR(scr, 4); win; win = PTR(win, 0))
                title(f, "  window", PTR(win, 32));
        }
        PermitF(sysbase);
        fclose(f);
        scr = PTR(ib, 60);
        if (!scr)
            continue;
        bm = PTR(scr, 88);
        vp = scr + 44;
        w = W(scr, 12);
        h = W(scr, 14);
        d = B(bm, 5);
        bpr = W(bm, 0);
        if (h > W(bm, 2))
            h = W(bm, 2);
        if (d > 8)
            d = 8;
        rowbytes = (w + 15) / 16 * 2;
        if (rowbytes > bpr)
            rowbytes = bpr;
        sprintf(name, "%s%02ld.raw", argv[3], n);
        if (!(f = fopen(name, "wb")))
            continue;
        fputs("WBS1", f);
        put16(f, w);
        put16(f, h);
        put16(f, d);
        put16(f, rowbytes);
        for (i = 0; i < 32; i++)
            put16(f, (unsigned)GetRGB4f(gb, PTR(vp, 4), i) & 0xFFF);
        for (p = 0; p < d; p++)
            for (y = 0; y < h; y++)
                fwrite(PTR(bm, 8 + 4 * p) + y * bpr, 1, rowbytes, f);
        fclose(f);
    }
    return 0;
}
