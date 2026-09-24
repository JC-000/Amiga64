/* fixtures/c/hello.c — M2 exit-bar program: vbcc-compiled, stdio only.
 * Built at test time by tests/run_vbcc.py (vc +aos68k); never commit the
 * compiled output (it embeds vbcc's startup code and vc.lib). */
#include <stdio.h>

int main(void)
{
    printf("Hello from vbcc!\n");
    printf("%d + %d = %d\n", 2, 40, 2 + 40);
    return 0;
}
