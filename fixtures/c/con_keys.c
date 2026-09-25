/* fixtures/c/con_keys.c — W5-K key map: reads one line and prints every
 * byte in hex (the screen shows upper and lower case alike, hex does
 * not), 12 bytes per row.  docs/HLE_CON.md "Key map". */
#include <stdio.h>
#include <string.h>

int main(void)
{
    char a[128];
    int i, n;

    if (!fgets(a, sizeof a, stdin))
        return 1;
    n = (int)strlen(a);
    printf("n=%d\n", n);
    for (i = 0; i < n; i++)
        printf("%02x%s", (unsigned char)a[i],
               (i % 12 == 11 || i == n - 1) ? "\n" : " ");
    return 0;
}
