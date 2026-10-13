; fixtures/modwb/mwdata.s — the replay's blocks in the launcher's own hunk
; (W16-X): mod_view (I-1, 72 bytes), the parameter and status blocks that
; fixtures/mod/mod.i puts at $000400/$000480/$0004C0 in the W15 builds.

        xdef    VIEW,PARAM,STATUS

        section BSS,bss
        cnop    0,4
VIEW:   ds.b    72
PARAM:  ds.b    64
STATUS: ds.b    64
