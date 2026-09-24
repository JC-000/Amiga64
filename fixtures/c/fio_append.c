/* fixtures/c/fio_append.c — fopen "a" and "r+" (vc.lib opens with
 * MODE_READWRITE / MODE_OLDFILE, then ChangeMode(CHANGE_FH, fh,
 * EXCLUSIVE_LOCK) — dos LVO -450).  Host-staged: log.txt = "one\n",
 * rp.txt = "abcdefgh".  Covers append to an existing and to a missing
 * file, r+ overwrite in place, and ChangeMode refusals under the sharing
 * rules (docs/HLE_DOS.md).  The host reads every file back. */
#include <stdio.h>

static void show(const char *name)
{
    char buf[40];
    size_t n;
    FILE *f = fopen(name, "r");
    if (!f) { printf("%s: NULL\n", name); return; }
    n = fread(buf, 1, sizeof buf - 1, f);
    buf[n] = 0;
    fclose(f);
    for (n = 0; buf[n]; n++)
        if (buf[n] == '\n') buf[n] = '|';
    printf("%s=%s\n", name, buf);
}

int main(void)
{
    FILE *f, *g;

    f = fopen("log.txt", "a");
    if (!f) { printf("append existing NULL\n"); return 10; }
    fputs("two\n", f);
    fclose(f);
    f = fopen("new.txt", "a");
    if (!f) { printf("append missing NULL\n"); return 11; }
    fputs("first\n", f);
    fclose(f);
    f = fopen("rp.txt", "r+");
    if (!f) { printf("r+ NULL\n"); return 12; }
    fseek(f, 2, SEEK_SET);
    fputs("XY", f);
    fclose(f);
    show("log.txt");
    show("new.txt");
    show("rp.txt");

    f = fopen("nosuch", "r+");
    printf("r+ missing=%s\n", f ? "open" : "NULL");
    f = fopen("rp.txt", "r");               /* shared reader ...           */
    g = fopen("rp.txt", "r+");              /* ... so no exclusive r+      */
    printf("r+ while shared=%s\n", g ? "open" : "NULL");
    g = fopen("rp.txt", "a");
    printf("a while shared=%s\n", g ? "open" : "NULL");
    fclose(f);
    f = fopen("log.txt", "a");              /* exclusive appender ...      */
    g = fopen("log.txt", "r");              /* ... refuses every Open      */
    printf("r while append=%s\n", g ? "open" : "NULL");
    fclose(f);
    g = fopen("log.txt", "r");
    printf("r after close=%s\n", g ? "open" : "NULL");
    fclose(g);
    return 0;
}
