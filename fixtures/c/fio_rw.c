/* fixtures/c/fio_rw.c — stdio file round trip on the RAMDISK (m2-post
 * task 1): fopen "w" + fwrite + fclose, then fopen "r" + fread + fgetc;
 * fopen of a missing file returns NULL; a "RAM:" prefix names the same
 * flat RAMDISK (the host reads t.txt and ram.txt back).  Exit code 0 = all
 * good, 10+ identifies the failing check. */
#include <stdio.h>
#include <string.h>

int main(void)
{
    static const char msg[] = "hello file\n";
    char buf[64];
    FILE *f;
    size_t n;
    int c;

    f = fopen("t.txt", "w");
    if (!f) { printf("fopen w failed\n"); return 10; }
    n = fwrite(msg, 1, 11, f);
    printf("fwrite=%d\n", (int)n);
    if (fclose(f)) { printf("fclose failed\n"); return 11; }

    f = fopen("t.txt", "r");
    if (!f) { printf("fopen r failed\n"); return 12; }
    c = fgetc(f);
    n = fread(buf, 1, sizeof buf - 1, f);
    buf[n] = 0;
    printf("fgetc=%c fread=%d\n", c, (int)n);
    c = fgetc(f);
    printf("eof=%d\n", c == EOF);
    fclose(f);
    if (strcmp(buf, "ello file\n")) { printf("data differs\n"); return 13; }

    f = fopen("nosuch.txt", "r");
    printf("missing=%s\n", f ? "open" : "NULL");
    if (f) return 14;

    f = fopen("RAM:ram.txt", "w");
    if (!f) { printf("fopen RAM: failed\n"); return 15; }
    fputs("via RAM:", f);
    fclose(f);
    f = fopen("ram.txt", "r");
    if (!f) { printf("ram.txt not found\n"); return 16; }
    n = fread(buf, 1, sizeof buf - 1, f);
    buf[n] = 0;
    fclose(f);
    printf("ram=%s\n", buf);
    return 0;
}
