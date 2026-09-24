/* fixtures/c/bigstack.c — vbcc's __stack startup path: a requested stack
 * larger than tc_SPUpper - tc_SPLower makes the startup AllocVec a new
 * stack and StackSwap to it (and back + FreeVec at exit) (b1-review F4). */
#include <stdio.h>
unsigned long __stack = 200000;

int main(void)
{
    char c;
    unsigned long sp = (unsigned long)&c;
    printf("stack %s\n", sp >= 0x100000UL ? "swapped" : "original");
    return 0;
}
