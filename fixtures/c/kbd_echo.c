/* fixtures/c/kbd_echo.c — W6-K keyboard hardware validation.
 * docs/HLE_CON.md "Hardware validation"; driver tests/kbd_echo.py.
 *
 * Walks the operator through NSTEP steps.  Each step is one line read from
 * the console (dos Read: matrix scan, debounce, decode, line discipline,
 * echo).  For every character Read delivered it prints the character, its
 * hex code and the matrix position the KEY MAP gives for it (c7r6 =
 * column 7, row 6; +SH = with SHIFT; ?? = not on the map).  That is the
 * inverse of the map, not the scanned position: a guest program cannot
 * see the scanner's state (C64 BSS); tests/kbd_echo.py streams the scanned
 * positions.  Then PASS or FAIL against want[].  An empty line skips a
 * step; EOF (CTRL+D) quits.  A free mode then decodes any line until an
 * empty one.  STEPS shows P, F or S per step ('-' = not in this run).
 * Exit code = number of failed steps.
 *
 * Arguments "CONTROL <name> <first> <last>" (a positive-control run of a
 * deliberately broken runner): a banner says this build is SUPPOSED to
 * fail, only steps first..last run, and there is no free mode.
 *
 * kplain/kshift mirror runtime/ops/hle_con.s (hle_con_kplain/kshift) and
 * tests/run_con.py; want[] mirrors tests/kbd_echo.py STEPS.  Both are
 * pinned by host/tests/test_kbd_echo.py. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define NSTEP 9

static const char kplain[64] = {
    8, 13, 0, 0, 0, 0, 0, 0,                   /* DEL RET CRSR-R F7 F1 F3 F5 CRSR-D */
    '3', 'w', 'a', '4', 'z', 's', 'e', 0,      /* ... LSHIFT */
    '5', 'r', 'd', '6', 'c', 'f', 't', 'x',
    '7', 'y', 'g', '8', 'b', 'h', 'u', 'v',
    '9', 'i', 'j', '0', 'm', 'k', 'o', 'n',
    '+', 'p', 'l', '-', '.', ':', '@', ',',
    '\\', '*', ';', 0, 0, '=', '^', '/',       /* pound * ; HOME RSHIFT = up / */
    '1', '_', 0, '2', ' ', 0, 'q', 0           /* 1 left CTRL 2 SPACE C= Q STOP */
};
static const char kshift[64] = {
    0, 13, 0, 0, 0, 0, 0, 0,
    '#', 'W', 'A', '$', 'Z', 'S', 'E', 0,
    '%', 'R', 'D', '&', 'C', 'F', 'T', 'X',
    '\'', 'Y', 'G', '(', 'B', 'H', 'U', 'V',
    ')', 'I', 'J', '0', 'M', 'K', 'O', 'N',
    '{', 'P', 'L', '}', '>', '[', '`', '<',
    '|', '*', ']', 0, 0, '=', '~', '?',
    '!', '_', 0, '"', ' ', 0, 'Q', 0
};

static const char *const title[NSTEP] = {
    "HOLD Q", "HOLD ; AND /", "ROLLOVER", "RSHIFT, C=",
    "PORT-1 STICK UP", "PORT-1 STICK DOWN", "PORT-1 STICK LEFT",
    "PORT-1 STICK RIGHT", "PORT-1 FIRE"
};
static const char *const how[NSTEP][2] = {
    { "hold Q 2s, release, RETURN", "" },
    { "hold ; 2s, release;", "hold / 2s, release; RETURN" },
    { "press Z, keep it; add I, keep it;", "add P; release all; RETURN" },
    { "RSHIFT+A, RSHIFT+/, then C=+Q;", "no LEFT SHIFT; RETURN" },
    { "hold the stick UP, type HOSE,", "release the stick; RETURN" },
    { "hold the stick DOWN, type HOSE,", "release the stick; RETURN" },
    { "hold the stick LEFT, type HOSE,", "release the stick; RETURN" },
    { "hold the stick RIGHT, type HOSE,", "release the stick; RETURN" },
    { "hold FIRE, type HOSE,", "release FIRE; RETURN" }
};
static const char *const want[NSTEP] = {
    "q", ";/", "zip", "A?q", "hose", "hose", "hose", "hose", "hose"
};

static int find(const char *map, int c)
{
    int i;

    for (i = 0; i < 64; i++)
        if (map[i] && map[i] == c)
            return i;
    return -1;
}

/* the key-map position of c: "c7r6", "c1r2+SH", "c1r2+CTRL" or "??" */
static void key_label(int c, char *out)
{
    int i;

    if ((i = find(kplain, c)) >= 0)
        sprintf(out, "c%dr%d", i / 8, i % 8);
    else if ((i = find(kshift, c)) >= 0)
        sprintf(out, "c%dr%d+SH", i / 8, i % 8);
    else if (c >= 1 && c <= 26 && (i = find(kplain, c + 0x60)) >= 0)
        sprintf(out, "c%dr%d+CTRL", i / 8, i % 8);
    else
        strcpy(out, "??");
}

/* one line from the console, without its "\n"; -1 on EOF */
static int get_line(char *a, int size)
{
    int n;

    fputs("> ", stdout);
    if (!fgets(a, size, stdin)) {
        puts("EOF");
        return -1;
    }
    n = (int)strlen(a);
    if (n > 0 && a[n - 1] == '\n')
        a[--n] = 0;
    return n;
}

static void decode(const char *a)
{
    char lab[16];
    int i, c;

    for (i = 0; a[i]; i++) {
        c = (unsigned char)a[i];
        key_label(c, lab);
        printf(" '%c' $%02x %s\n", c >= 0x20 && c < 0x7f ? c : '.', c, lab);
    }
}

int main(int argc, char **argv)
{
    char a[128], v[NSTEP + 1];
    int s, n, pass = 0, fail = 0, skip = 0, eof = 0;
    int first = 1, last = NSTEP, control = 0;

    puts("KBD-ECHO keyboard check (W6-K)");
    if (argc >= 5 && strcmp(argv[1], "CONTROL") == 0) {
        control = 1;
        first = atoi(argv[3]);
        last = atoi(argv[4]);
        if (first < 1)
            first = 1;
        if (last > NSTEP)
            last = NSTEP;
        printf("*** CONTROL BUILD: %s ***\n", argv[2]);
        puts("SUPPOSED TO FAIL: it lacks the fix,");
        puts("so it should show the defect.");
    }
    puts("empty line: skip step. CTRL+D: quit");
    for (s = 0; s < NSTEP; s++) {
        if (s + 1 < first || s + 1 > last) {
            v[s] = '-';
            continue;
        }
        if (eof) {
            v[s] = 'S';
            skip++;
            continue;
        }
        printf("STEP %d/%d: %s\n", s + 1, NSTEP, title[s]);
        puts(how[s][0]);
        if (how[s][1][0])
            puts(how[s][1]);
        n = get_line(a, sizeof a);
        if (n < 0)
            eof = 1;
        else
            decode(a);
        if (n <= 0) {
            printf("STEP %d SKIP\n", s + 1);
            v[s] = 'S';
            skip++;
        } else if (strcmp(a, want[s]) == 0) {
            printf("STEP %d PASS\n", s + 1);
            v[s] = 'P';
            pass++;
        } else {
            printf("STEP %d FAIL, want \"%s\"\n", s + 1, want[s]);
            v[s] = 'F';
            fail++;
        }
    }
    v[NSTEP] = 0;
    if (!eof && !control) {
        puts("FREE: type lines, empty line ends");
        while ((n = get_line(a, sizeof a)) > 0)
            decode(a);
    }
    printf("STEPS %s\n", v);
    printf("RESULT pass=%d fail=%d skip=%d\n", pass, fail, skip);
    return fail;
}
