/* fixtures/c/time.c — time() goes through dos DateStamp (-192).  The HLE
 * clock starts every run at DateStamp 0/0/0 = 1978-01-01 00:00:00 and
 * ticks once (1/50 s) per call, so the first two calls both give Unix
 * time 252460800 (2922 days after 1970-01-01; see tspin.c). */
#include <stdio.h>
#include <time.h>

int main(void)
{
    time_t t = time(0), u;
    time(&u);
    printf("time=%ld\n", (long)t);
    printf("same=%d\n", t == u);
    return 0;
}
