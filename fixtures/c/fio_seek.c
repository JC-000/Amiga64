/* fixtures/c/fio_seek.c — fseek/ftell on a host-staged RAMDISK file
 * (in.txt = "0123456789ABCDEFGHIJ", 20 bytes).  Exit code 0 = all good. */
#include <stdio.h>

int main(void)
{
    FILE *f = fopen("in.txt", "r");
    long a, b, c;
    int x, y, z;

    if (!f) { printf("fopen failed\n"); return 10; }
    fseek(f, 0, SEEK_END);
    a = ftell(f);
    fseek(f, 5, SEEK_SET);
    x = fgetc(f);
    b = ftell(f);
    fseek(f, 4, SEEK_CUR);
    y = fgetc(f);
    fseek(f, -1, SEEK_END);
    z = fgetc(f);
    c = ftell(f);
    printf("end=%ld\n", a);
    printf("at5=%c tell=%ld\n", x, b);
    printf("cur+4=%c\n", y);
    printf("last=%c tell=%ld\n", z, c);
    rewind(f);
    printf("first=%c\n", fgetc(f));
    fclose(f);
    return 0;
}
