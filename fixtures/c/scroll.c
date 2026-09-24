/* fixtures/c/scroll.c — 30 lines through the 25-row console: the HLE
 * console must scroll instead of dropping output, and the row that
 * scrolls in must be blank (line lengths vary, so a stale tail from the
 * previous bottom row would show).  Built at test time; never commit
 * compiled output. */
#include <stdio.h>

int main(void)
{
    int n, k;
    for (n = 1; n <= 30; n++) {
        printf("line %02d", n);
        for (k = 0; k < (n * 3) % 7; k++)   /* (not %.*s: vbcc's printf */
            putchar('*');                    /*  ignores precision 0) */
        putchar('\n');
    }
    return 0;
}
