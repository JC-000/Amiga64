/* fixtures/c/fio_reuse.c — RAMDISK space is reused after a delete.  On an
 * empty RAMDISK, create files until fopen fails (32 directory entries /
 * 2 MB of 64 KB extents), delete one, and a new file must fit again (its
 * directory entry and its extent come back); after that the disk is full
 * again.  Exit code 0 = all good. */
#include <stdio.h>

int main(void)
{
    char name[8];
    FILE *f;
    int n = 0, r;

    for (;;) {
        sprintf(name, "f%02d", n);
        f = fopen(name, "w");
        if (!f) break;
        fclose(f);
        if (++n > 40) break;
    }
    printf("created=%d\n", n);
    r = remove("f05");
    printf("rm=%d\n", r);
    f = fopen("again", "w");
    printf("again=%s\n", f ? "open" : "NULL");
    if (!f) return 10;
    fputs("reused", f);
    fclose(f);
    f = fopen("more", "w");
    printf("more=%s\n", f ? "open" : "NULL");
    return f ? 11 : 0;
}
