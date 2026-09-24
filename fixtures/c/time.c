/* fixtures/c/time.c — time() goes through dos DateStamp (-192).  The HLE
 * clock is stopped at DateStamp 0/0/0 = 1978-01-01 00:00:00, which vc.lib
 * converts to Unix time 252460800 (2922 days after 1970-01-01). */
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
