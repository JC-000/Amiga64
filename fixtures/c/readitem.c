/* fixtures/c/readitem.c — a dos.library call deep in the vector table
 * (ReadItem, LVO -810) that the HLE does not implement must halt in the
 * DOS table's own sentinel slot, not land in another library's table
 * (b1-review F1). */
#include <stdio.h>
extern void *DOSBase;
long __ReadItem(__reg("a6") void *, __reg("d1") char *, __reg("d2") long,
                __reg("d3") void *) = "\tjsr\t-810(a6)";

int main(void)
{
    char buf[16];
    long r = __ReadItem(DOSBase, buf, 16, 0);
    printf("ReadItem=%lx\n", r);
    return 0;
}
