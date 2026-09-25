/* fixtures/c/con_getchar.c — W5-K: getchar() until EOF (Ctrl-\ on the
 * console); counts characters and lines.  docs/HLE_CON.md. */
#include <stdio.h>

int main(void)
{
    int c;
    long n = 0, lines = 0, sum = 0;

    while ((c = getchar()) != EOF) {
        n++;
        if (c == '\n')
            lines++;
        sum += c;
    }
    printf("chars=%ld lines=%ld sum=%ld\n", n, lines, sum);
    printf("feof=%d\n", feof(stdin) != 0);
    return 0;
}
