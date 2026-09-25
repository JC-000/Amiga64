/* fixtures/c/con_long.c — W5-K: stdin with a 4-byte stdio buffer, so a
 * 26-byte line arrives through several dos Read calls of at most 4 bytes
 * and the console must keep the rest of the line between them.
 * tests/run_con.py also checks the Read count (hle_con_nread).
 * docs/HLE_CON.md. */
#include <stdio.h>
#include <string.h>

static char sbuf[4];

int main(void)
{
    char line[80];

    if (setvbuf(stdin, sbuf, _IOFBF, sizeof sbuf))
        puts("setvbuf failed");
    if (!fgets(line, sizeof line, stdin))
        return 1;
    printf("1=%d:%s", (int)strlen(line), line);
    if (!fgets(line, sizeof line, stdin))
        return 2;
    printf("2=%d:%s", (int)strlen(line), line);
    printf("eof=%d\n", fgets(line, sizeof line, stdin) == NULL);
    return 0;
}
