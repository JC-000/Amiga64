/* fixtures/c/tspin.c — the HLE clock must move: time() goes through dos
 * DateStamp (-192), which advances ds_Tick by one per call (docs/HLE.md),
 * so a "wait for the next second" loop terminates after 49 more calls
 * (48 of them still return the old second: spins=48).
 * vc.lib's clock() is a constant -1 (it never calls the OS). */
#include <stdio.h>
#include <time.h>

int main(void)
{
    time_t t = time(0), u;
    long spins = 0;
    while ((u = time(0)) == t)
        if (++spins > 100000L) break;
    printf("spins=%ld delta=%ld\n", spins, (long)(u - t));
    printf("clock=%ld\n", (long)clock());
    return 0;
}
