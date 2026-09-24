/* fixtures/c/system.c — system() at V36+: system(NULL) reports a shell
 * without calling dos (vc.lib returns 1); system("cmd") calls dos
 * SystemTagList (-606), which the HLE fails with -1 (b1-review R2). */
#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    printf("system(NULL)=%d\n", system(NULL));
    printf("system(cmd)=%d\n", system("echo hi"));
    return 0;
}
