/* fixtures/c/con_scanf.c — W5-K: scanf of numbers from the console, then
 * scanf at EOF.  docs/HLE_CON.md. */
#include <stdio.h>

int main(void)
{
    int x = 0, y = 0, z = 0, r;

    fputs("x y? ", stdout);
    fflush(stdout);
    r = scanf("%d %d", &x, &y);
    printf("r=%d sum=%d\n", r, x + y);
    r = scanf("%d", &z);
    printf("r=%d z=%d\n", r, z);
    r = scanf("%d", &z);
    printf("r=%d\n", r);
    return 0;
}
