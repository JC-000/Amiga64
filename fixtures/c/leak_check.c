/* fixtures/c/leak_check.c — run after leak_open.c in the same session:
 * a fresh program must get a file handle (b1-review F2). */
#include <stdio.h>
extern void *DOSBase;
long __Open(__reg("a6") void *, __reg("d1") const char *, __reg("d2") long)
    = "\tjsr\t-30(a6)";

int main(void)
{
    long fh = __Open(DOSBase, "fresh", 1006);
    printf("open %s\n", fh ? "ok" : "NULL");
    return fh ? 0 : 5;
}
