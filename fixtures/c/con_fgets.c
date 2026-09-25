/* fixtures/c/con_fgets.c — W5-K: two fgets lines from the console, then
 * EOF.  Driven by tests/run_con.py (scripted queue and real key matrix);
 * docs/HLE_CON.md. */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char a[64], b[64];

    fputs("first? ", stdout);
    fflush(stdout);
    if (!fgets(a, sizeof a, stdin)) {
        puts("no a");
        return 1;
    }
    fputs("second? ", stdout);
    fflush(stdout);
    if (!fgets(b, sizeof b, stdin)) {
        puts("no b");
        return 2;
    }
    printf("a=%d:%s", (int)strlen(a), a);
    printf("b=%d:%s", (int)strlen(b), b);
    printf("eof=%d\n", fgets(a, sizeof a, stdin) == NULL);
    return 0;
}
