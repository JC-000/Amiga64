/* fixtures/c/sysbase.c — ExecBase/DOSBase fields that real programs read
 * through SysBase, and OpenLibrary's version check (b1-review F1/F3). */
#include <stdio.h>
extern void *SysBase;
extern void *DOSBase;
void *__OpenLibrary(__reg("a6") void *, __reg("a1") const char *,
                    __reg("d0") unsigned long) = "\tjsr\t-552(a6)";

static const char *nz(void *p) { return p ? "ok" : "NULL"; }

int main(void)
{
    unsigned char *s = SysBase, *d = DOSBase;
    printf("ThisTask=%lx\n", *(unsigned long *)(s + 276));
    printf("AttnFlags=%x\n", *(unsigned short *)(s + 296));
    printf("MaxLocMem=%lx\n", *(unsigned long *)(s + 62));
    printf("exec ver=%u\n", *(unsigned short *)(s + 20));
    printf("dos ver=%u\n", *(unsigned short *)(d + 20));
    printf("dos v40 %s\n", nz(__OpenLibrary(SysBase, "dos.library", 40)));
    printf("dos v41 %s\n", nz(__OpenLibrary(SysBase, "dos.library", 41)));
    printf("dos v50 %s\n", nz(__OpenLibrary(SysBase, "dos.library", 50)));
    printf("dos.fake %s\n", nz(__OpenLibrary(SysBase, "dos.fake", 0)));
    printf("utility %s\n", nz(__OpenLibrary(SysBase, "utility.library", 37)));
    return 0;
}
