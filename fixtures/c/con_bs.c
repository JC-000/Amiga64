/* fixtures/c/con_bs.c — W5-K: backspace editing, including stepping back
 * across a row boundary (the second prompt is 38 columns wide).
 * docs/HLE_CON.md. */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char a[64];

    fputs("edit? ", stdout);
    fflush(stdout);
    if (!fgets(a, sizeof a, stdin))
        return 1;
    printf("got=%d:%s", (int)strlen(a), a);
    fputs("......................................", stdout);
    fflush(stdout);
    if (!fgets(a, sizeof a, stdin))
        return 2;
    printf("got=%d:%s", (int)strlen(a), a);
    return 0;
}
