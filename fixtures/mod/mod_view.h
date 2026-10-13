/* fixtures/mod/mod_view.h — interface I-1 (docs/WAVE15_BRIEF.md): the
 * player state the UI shows.  Owner W15-M (the producer); W15-U includes it
 * read-only; any change goes through the supervisor.
 *
 * Guest chip RAM at MOD_VIEW_ADDR, big-endian (the 68000's own order), 72
 * bytes.  main() publishes one whole replay tick here between two
 * ui_frame() calls (copied under SR $2700), so the struct does not change
 * while ui_frame() runs.  The UI must tolerate any value (clamp what it
 * shows).
 *
 * Semantics (supervisor rulings, brief addendum):
 *   tick      +1 per replay tick
 *   vu        := volume on a note trigger, else max(0, vu - 2) per TICK
 *   elapsed   1/50 s: the high word of a 16.16 sum of (125 << 16) / BPM per
 *             tick (125 while the VBL tempo runs)
 *   period/volume: the values written to Paula this tick (after
 *             arpeggio, vibrato, tremolo)
 *   note/effect/param: the current row's command; note $FF = none
 */
#ifndef MOD_VIEW_H
#define MOD_VIEW_H

#define MOD_VIEW_ADDR   0x000400UL
#define MOD_VIEW_MAGIC  0x4D565731UL      /* 'MVW1' */
#define MOD_VIEW_SIZE   72

#define MV_PLAYING      0x01              /* flags; clear in the view published after the end */
#define MV_LOOPED       0x02              /* the song has wrapped or jumped back */
#define MV_CIA          0x04              /* the song's CIA tempo: an Fxx >= 32 was seen (in
                                             both timing modes; with the real-time flag every
                                             tick comes from the CIA timer regardless) */

#define MV_NOTE_NONE    0xFF

struct mod_chan {                         /* 8 bytes */
    unsigned short period;                /* +0 */
    unsigned char  instrument;            /* +2: 0 = none, 1-31 */
    unsigned char  volume;                /* +3: 0-64 */
    unsigned char  note;                  /* +4: 0-35 = C-1..B-3, $FF = none */
    unsigned char  effect;                /* +5: 0-15 */
    unsigned char  param;                 /* +6 */
    unsigned char  vu;                    /* +7: 0-64 */
};

struct mod_view {
    unsigned long  magic;                 /* +0  'MVW1' */
    unsigned long  tick;                  /* +4 */
    char           name[20];              /* +8  NUL-padded, maybe unterminated */
    unsigned char  song_length;           /* +28 positions */
    unsigned char  position;              /* +29 */
    unsigned char  pattern;               /* +30 */
    unsigned char  row;                   /* +31 0-63 */
    unsigned char  speed;                 /* +32 ticks/row */
    unsigned char  bpm;                   /* +33 */
    unsigned char  flags;                 /* +34 MV_* */
    unsigned char  pad;                   /* +35 0 */
    struct mod_chan ch[4];                /* +36 */
    unsigned long  elapsed;               /* +68 1/50 s */
};

#define MOD_VIEW ((const volatile struct mod_view *)MOD_VIEW_ADDR)

#endif
