/* fixtures/c/con_echo.c — W5-K: the console echoes typed text itself.
 * The first line is read and never printed, so only the HLE echo can put
 * it on the screen; the exit code is its length.  The second prompt is
 * printed WITHOUT fflush: vc.lib flushes stdout before it reads an
 * interactive stdin, so "Name: " lands before the echo of the answer.
 * docs/HLE_CON.md. */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char a[64], n[32];

    if (!fgets(a, sizeof a, stdin))
        return 100;
    printf("Name: ");
    if (!fgets(n, sizeof n, stdin))
        return 101;
    printf("Hi %s", n);
    return (int)strlen(a);
}
