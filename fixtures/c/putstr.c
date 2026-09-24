/* fixtures/c/putstr.c — the deepest-area dos call PutStr (LVO -948) is
 * unimplemented: it must halt in the dos sentinel slot after the first
 * line was printed (b1-review F1). */
#include <stdio.h>
extern void *DOSBase;
long __PutStr(__reg("a6") void *, __reg("d1") const char *) = "\tjsr\t-948(a6)";

int main(void)
{
    printf("before\n");
    __PutStr(DOSBase, "PutStr works\n");
    printf("after\n");
    return 3;
}
