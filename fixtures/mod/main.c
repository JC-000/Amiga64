/* fixtures/mod/main.c — the MOD player program's main() (W15-M).
 *
 * I-2 (docs/WAVE15_BRIEF.md): W15-M owns main(), the interrupts, the CIAs,
 * Paula and the replay (replay.s); W15-U owns the display through
 * ui_init()/ui_frame() (fixtures/mod/ui/; ui_stub.c until the merge).
 * ui_frame() runs outside the interrupt handlers, with the published
 * mod_view, which does not change while it runs: once per VERTB, or once
 * every UI_DIV VERTBs (parameter block P_UIDIV, fixtures/mod/mod.i;
 * default 1).  UI_DIV is the budget's safety valve (supervisor, M2): a
 * slow UI only lags the display, the replay runs from the interrupts and
 * never waits for it. */
#include "mod_view.h"

int mod_init(void);
void mod_start(void);
void mod_wait_frame(void);          /* returns every UI_DIV VERTBs */
void mod_publish(void);
extern volatile unsigned char mod_done;

void ui_init(void);
void ui_frame(const struct mod_view *v);

int main(void)
{
    int err = mod_init();

    if (err)
        return err;
    mod_publish();
    ui_init();
    mod_start();
    while (!mod_done) {
        mod_wait_frame();
        mod_publish();
        ui_frame((const struct mod_view *)MOD_VIEW_ADDR);
    }
    mod_publish();
    return 0;
}
