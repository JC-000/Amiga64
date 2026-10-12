/* fixtures/mod/ui_stub.c — the empty UI (W15-M before the W15-U merge, I-2).
 *
 * The display, and with it DMACON's DMAEN master bit, belongs to the UI.
 * Paula's audio DMA needs DMAEN too, so the stub SETs DMAEN alone (bit 15
 * SET semantics: no other bit changes), or a stub build would be silent on
 * hardware (rev15-mod M3).  W15-U's ui_init sets DMAEN with BPLEN, COPEN
 * and BLTEN. */
#include "mod_view.h"

void ui_init(void)
{
    *(volatile unsigned short *)0xDFF096 = 0x8200;    /* DMACON: SET DMAEN */
}

void ui_frame(const struct mod_view *v)
{
}
