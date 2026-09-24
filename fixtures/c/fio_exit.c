/* fixtures/c/fio_exit.c — a file left open at exit must still be flushed
 * (vc.lib's exit closes every open stream; the host reads unclosed.txt
 * back).  The data stays in the stdio buffer until then: fewer bytes than
 * the buffer, no fflush. */
#include <stdio.h>

int main(void)
{
    FILE *f = fopen("unclosed.txt", "w");
    if (!f) { printf("fopen failed\n"); return 10; }
    fprintf(f, "left open %d\n", 42);
    printf("exit without fclose\n");
    return 0;
}
