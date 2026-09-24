/* fixtures/c/scroll.c — 30 lines through the 25-row console: the HLE
 * console must scroll instead of dropping output.  Built at test time;
 * never commit compiled output. */
#include <stdio.h>

int main(void)
{
    int n;
    for (n = 1; n <= 30; n++)
        printf("line %02d\n", n);
    return 0;
}
