/* fixtures/c/leak_open.c — opens every dos file slot through dos.library
 * directly and exits without closing (vc.lib's exit cannot close handles
 * it never saw).  Paired with leak_check.c: handle state must not leak
 * into the next program (b1-review F2; needs WS-B2's hle_dos.s). */
#include <stdio.h>
extern void *DOSBase;
long __Open(__reg("a6") void *, __reg("d1") const char *, __reg("d2") long)
    = "\tjsr\t-30(a6)";

int main(void)
{
    static const char *names[] = { "l0", "l1", "l2", "l3", "l4", "l5",
                                   "l6", "l7", "l8", "l9" };
    int i, n = 0;
    for (i = 0; i < 10; i++)
        if (__Open(DOSBase, names[i], 1006))   /* MODE_NEWFILE */
            n++;
    printf("opened %d\n", n);
    return 0;
}
