/* fixtures/c/fio_remove.c — remove() = dos DeleteFile (LVO -72).
 * Host-staged: gone.txt, keep.txt.  Deleting an open file fails with
 * IoErr 202 (ERROR_OBJECT_IN_USE), a missing one with 205; a delete is
 * visible to a later fopen; a "RAM:" prefix works too.  The host checks
 * the RAMDISK directory afterwards. */
#include <stdio.h>
extern void *DOSBase;
long __IoErr(__reg("a6") void *) = "\tjsr\t-132(a6)";

int main(void)
{
    FILE *f;
    int r;

    r = remove("gone.txt");
    printf("rm=%d\n", r);
    r = remove("gone.txt");
    printf("rm again=%d ioerr=%ld\n", r, __IoErr(DOSBase));
    f = fopen("gone.txt", "r");
    printf("reopen=%s\n", f ? "open" : "NULL");
    f = fopen("keep.txt", "r");
    r = remove("keep.txt");
    printf("rm open=%d ioerr=%ld\n", r, __IoErr(DOSBase));
    fclose(f);
    f = fopen("tmp.txt", "w");
    fputs("x", f);
    fclose(f);
    r = remove("RAM:TMP.txt");
    printf("rm RAM:=%d\n", r);
    return 0;
}
