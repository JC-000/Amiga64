/* fixtures/c/getenv.c — vc.lib's getenv() calls dos GetVar (-906) at V36+;
 * there are no environment variables, so it must return NULL and the
 * program must carry on (b1-review R1). */
#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    char *e = getenv("HOME");
    printf("HOME=%s\n", e ? e : "NULL");
    printf("after getenv\n");
    return 0;
}
