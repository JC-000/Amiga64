/* fixtures/c/fio_fmt.c — fprintf / fscanf round trip through a RAMDISK
 * file.  Exit code 0 = all good. */
#include <stdio.h>

int main(void)
{
    FILE *f = fopen("fmt.txt", "w");
    int a, b, n;
    long l;
    char word[16];

    if (!f) { printf("fopen w failed\n"); return 10; }
    fprintf(f, "%d %s %ld\n%d\n", -123, "word", 70000L, 7);
    fclose(f);
    f = fopen("fmt.txt", "r");
    if (!f) { printf("fopen r failed\n"); return 11; }
    n = fscanf(f, "%d %15s %ld %d", &a, word, &l, &b);
    fclose(f);
    printf("n=%d a=%d w=%s\n", n, a, word);
    printf("l=%ld b=%d\n", l, b);
    return n == 4 ? 0 : 12;
}
