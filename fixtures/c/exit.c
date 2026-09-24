/* fixtures/c/exit.c — exit() through the vbcc runtime: stdio must be
 * flushed by the exit path and the status must reach the caller (D0 at the
 * final RTS, docs/HLE.md "Program exit").  Built at test time; never commit
 * compiled output. */
#include <stdio.h>
#include <stdlib.h>

int main(void)
{
    printf("exit(7) via stdio\n");
    exit(7);
    return 0;   /* not reached */
}
