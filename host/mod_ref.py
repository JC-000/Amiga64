"""W15-M: the reference replay.  A MOD, tick by tick, into the Paula
register writes the player program issues and the ``mod_view`` it publishes.

docs/WAVE15_BRIEF.md "W15-M" M1 and interface I-1.  Written from public
descriptions of the MOD format and of the effect commands, and from the
Amiga Hardware Reference Manual's audio chapter (sources:
build/evidence/w15-mod/PROGRESS.md); no replay source was read.  Where the
public descriptions leave a choice open, this module DEFINES the
behaviour, and the 68k replay (fixtures/mod/replay.s) implements exactly
this; the choices are listed in :data:`SEMANTICS`.

The write protocol of one tick (the 68k replay issues exactly this order):

  1. DMACON clear (bit 15 = 0) of every channel triggered this tick, if any;
  2. for each triggered channel in order 0-3: AUDxLCH, AUDxLCL, AUDxLEN;
  3. for each channel 0-3: AUDxPER if the output period differs from the
     last one written to that channel or the channel was triggered, then
     AUDxVOL by the same rule (output volume);
  4. (CIA-B timer B one-shot, stage 1) DMACON set ($8000 | the triggered
     channels that play);
  5. (stage 2) for each channel that plays: AUDxLCH, AUDxLCL, AUDxLEN of
     its repeat block.

Steps 4-5 run from timer interrupts a few hundred E ticks after the tick;
they belong to the tick that triggered.  A trigger whose sample cannot
play (length 0, or a 9xx offset past the end of a one-shot) is in step 1
only: the channel stops.

Program start writes DMACON $000F and AUD0-3VOL 0 (:func:`init_writes`); the
end (song end with the stop flag, or ``max_ticks``) writes DMACON $000F.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field

from host.modgen import CHANNELS, HEADER_LEN, PERIODS, ROWS

DMACON = 0x096
AUD = [0x0A0 + 0x10 * c for c in range(CHANNELS)]     # AUDxLCH; +2 LCL, +4 LEN, +6 PER, +8 VOL
MIN_PER, MAX_PER = 113, 856
VU_DECAY = 2
VIEW_MAGIC = b"MVW1"
VIEW_LEN = 72
FLAG_PLAYING, FLAG_LOOPED, FLAG_CIA = 1, 2, 4
FRAME_E = 14188                  # E ticks per guest frame (CONTRACTS "Machine loop")

SEMANTICS = """
- Note lookup: a pattern period maps to the nearest finetune-0 period (ties:
  the lower index); the played period is PERIOD_TABLE[finetune][note].
- Finetune tables: row 0 = the 36 documented periods; row f (-8..7, f != 0)
  = round(P0[n] * 2^(-f/96)).
- An instrument number sets the channel's instrument, volume and finetune,
  with or without a note; with a note and no 3/5 effect the note triggers.
- E5x sets the finetune before this row's note lookup.
- 3xx/5xy with a note: the note becomes the tone-portamento target, no
  trigger; 3 with param 0 keeps the speed.
- Arpeggio uses the channel's note index (set at trigger, and to the
  target's note while a tone portamento sits on its target): tick % 3 =
  0/1/2 -> note, +x, +y, capped at 35.
- 1xx/2xx and E1x/E2x clamp to 113/856.
- Vibrato/tremolo: position 0-63, value = wave(pos & 31), negative for pos >=
  32; sine = floor(255 sin(pi i / 32)), 1 = ramp 255 - 8i, 2 = square 255,
  3 = sine.  Vibrato offset = value * depth >> 7, tremolo = value * depth
  >> 6.  Applied and advanced on ticks > 0 only; position reset on a
  trigger unless the wave's bit 2 is set.
- Axy: x != 0 slides up by x, else down by y; on ticks > 0; clamp 0-64.
- 9xx: start += 256 * xx bytes (xx = 0 reuses the last); past the end: a
  looping sample starts at its repeat block, a one-shot stops the channel.
- Bxx and Dxx: B sets the next position, D the next row (decimal x*10+y,
  > 63 -> 0); D alone advances the position by one; both on a row: B's
  position, D's row.  A row with B/D ignores its E6 jump.
- E6x per channel: E60 marks the row; E6x counts x jumps back to it.  The
  marks reset to row 0 on every new position.
- EEx: the row lasts speed * (1 + x) ticks; tick-indexed effects (arpeggio
  phase, E9, EC, ED) use the tick within the extended row.  The last
  channel with x != 0 wins.
- E9x retriggers at every tick t > 0 with t % x == 0; ECx sets the volume to
  0 at tick x (EC0 at once); EDx triggers the row's note at tick x (ED0 =
  no delay; a delay without a note does nothing).
- Fxx: 0 ignored (counted), < 32 speed (this row on), >= 32 BPM and the
  CIA tempo from the next tick on.
- Song end: position >= length wraps to the restart byte (0 if out of
  range) and sets the looped flag; a B to a position <= the current one
  sets it too.  With the stop flag the player halts instead of playing
  the looped row.
- Ignored (counted): E0, E3, E8, EF, 8xx, F00.
- Malformed input (M1): instrument numbers outside 1-31 are "no
  instrument"; samples and loops are clamped to the usable file (parse());
  a channel with period 0 (no note yet) gets no period effect (arpeggio,
  1/2/3/4/5/6 pitch parts, E1/E2, E9 retrigger) and keeps output period 0;
  every nonzero output period is clamped to 107-907 (the finetune table's
  range) before Paula and the view.
- The end: the published view is the last tick's with flags bit 0
  (playing) clear.  Flags bit 2 = the song's CIA tempo (an Fxx >= 32 was
  seen), in both timing modes; with PF_REALTIME every tick comes from the
  CIA timer whatever bit 2 says.
- mod_view: period/volume = the output values of the tick; note = the
  row's note (0-35) or $FF; effect/param = the row's command; vu := volume
  on a trigger, else max(0, vu - 2) per tick; elapsed (1/50 s) = the high
  word of a 16.16 sum of (125 << 16) // B per tick, B = the BPM of the
  interval that follows the tick (125 while the VBL tempo runs).
"""


def _pt_row(f: int) -> list[int]:
    if f == 0:
        return list(PERIODS)
    return [round(p * 2 ** (-f / 96)) for p in PERIODS]


PERIOD_TABLE = {f: _pt_row(f) for f in range(-8, 8)}
SINE = [math.floor(255 * math.sin(math.pi * i / 32)) for i in range(32)]


def nearest_note(period: int) -> int:
    best = 0
    for i, p in enumerate(PERIODS):
        if abs(p - period) < abs(PERIODS[best] - period):
            best = i
    return best


def wave_value(wave: int, pos: int) -> int:
    i = pos & 31
    w = wave & 3
    if w == 1:
        v = 255 - 8 * i
    elif w == 2:
        v = 255
    else:
        v = SINE[i]
    return -v if pos & 32 else v


def tick_period_e(bpm: int, tick_e: int = FRAME_E) -> int:
    """The E ticks of one tick at *bpm*: tick_e at 125."""
    return tick_e * 125 // bpm


def cia_split(period: int) -> tuple[int, int]:
    """(n, latch): CIA-B TA runs latch + 1 E ticks, n underflows per tick
    (a period above 16 bits is split, s = 0 at BPM < 47)."""
    n = (period + 0xFFFF) >> 16
    return n, period // n - 1


def elapsed_inc(bpm: int) -> int:
    return (125 << 16) // bpm


# --- the file -------------------------------------------------------------------------

@dataclass
class Instrument:
    length: int        # bytes
    finetune: int
    volume: int
    rep_start: int     # bytes
    rep_len: int       # bytes
    offset: int        # file offset of the sample data


@dataclass
class Mod:
    raw: bytes
    title: bytes
    instruments: list[Instrument]
    song_len: int
    restart: int
    order: list[int]
    npat: int

    def cell(self, pat: int, row: int, ch: int) -> tuple[int, int, int, int]:
        o = HEADER_LEN + pat * 1024 + row * 16 + ch * 4
        b0, b1, b2, b3 = self.raw[o:o + 4]
        return ((b0 & 0x0F) << 8 | b1, (b0 & 0xF0) | (b2 >> 4), b2 & 0x0F, b3)


class ModError(ValueError):
    """A MOD the player refuses; .code = the 68k's mod_init error code."""

    def __init__(self, msg: str, code: int):
        super().__init__(msg)
        self.code = code


MOD_END = 0x200000               # the MOD area ends at $1FFFFF (M0 map)
PER_LO, PER_HI = 107, 907         # output periods clamped to the finetune table's range


def parse(raw: bytes, base: int = 0x080000) -> Mod:
    """The MOD as the player sees it at guest *base*.  Malformed input
    (M1, rev15-mod), identical in fixtures/mod/replay.s:
      * base odd or past the MOD area: rejected (err 5);
      * the usable length is min(file length, $200000 - base);
      * fewer than 1084 bytes or no 'M.K.': err 2; song length 0 or > 128:
        err 3; patterns past the usable length: err 4;
      * each sample is CLAMPED to the usable length (bytes past it are not
        played; a sample starting past it has length 0); a loop whose start
        lies past the clamped length is dropped (one-shot), a loop running
        past it is cut there; a loop of <= 2 bytes is no loop."""
    if base & 1 or base >= MOD_END:
        raise ModError(f"MOD address ${base:06X}", 5)
    avail = min(len(raw), MOD_END - base)
    if avail < HEADER_LEN or raw[1080:1084] != b"M.K.":
        raise ModError("not an M.K. module", 2)
    song_len = raw[950]
    if not 1 <= song_len <= 128:
        raise ModError(f"song length {song_len}", 3)
    order = list(raw[952:1080])
    npat = max(order) + 1
    off = HEADER_LEN + npat * 1024
    if off > avail:
        raise ModError("patterns truncated", 4)
    ins = []
    for i in range(31):
        r = raw[20 + 30 * i:50 + 30 * i]
        ln, ft, vol, rs, rl = struct.unpack(">HBBHH", r[22:30])
        ft &= 0x0F
        length = min(2 * ln, max(0, avail - off)) & ~1
        rs, rl = 2 * rs, 2 * rl
        if rl > 2 and rs < length:
            rl = min(rl, length - rs)
        else:
            rs, rl = 0, 2
        if rl <= 2:
            rs, rl = 0, 2
        ins.append(Instrument(length, ft - 16 if ft & 8 else ft, min(vol, 64), rs, rl, off))
        off += 2 * ln
    return Mod(raw, raw[:20], ins, song_len, raw[951], order, npat)


# --- the replay ------------------------------------------------------------------------

@dataclass
class Chan:
    inst: int = 0
    period: int = 0
    note: int = 0
    finetune: int = 0
    volume: int = 0
    out_period: int = 0
    out_volume: int = 0
    porta_target: int = 0
    porta_note: int = 0
    porta_speed: int = 0
    vib_speed: int = 0
    vib_depth: int = 0
    vib_pos: int = 0
    vib_wave: int = 0
    trem_speed: int = 0
    trem_depth: int = 0
    trem_pos: int = 0
    trem_wave: int = 0
    offset_mem: int = 0
    loop_row: int = 0
    loop_count: int = 0
    vu: int = 0
    # the current row
    r_period: int = 0
    r_inst: int = 0
    r_eff: int = 0
    r_par: int = 0
    r_note: int = 0xFF
    # last written (write-on-change)
    w_period: int = 0
    w_volume: int = 0


@dataclass
class Tick:
    n: int                                # 1-based tick count
    writes: list[tuple[int, int]]         # (register offset, word), in order
    view: bytes
    cia: bool                             # this tick came from the CIA timer
    bpm: int


def init_writes() -> list[tuple[int, int]]:
    return [(DMACON, 0x000F)] + [(AUD[c] + 8, 0) for c in range(CHANNELS)]


END_WRITES = [(DMACON, 0x000F)]


@dataclass
class Replay:
    mod: Mod
    base: int = 0x080000
    stop_at_end: bool = True
    start_pos: int = 0
    ch: list[Chan] = field(default_factory=lambda: [Chan() for _ in range(CHANNELS)])
    ignored: dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.speed, self.bpm, self.cia = 6, 125, False
        self.pos = self.start_pos if self.start_pos < self.mod.song_len else 0
        self.row, self.t, self.row_ticks = 0, 0, 0
        self.ticks, self.acc = 0, 0
        self.looped, self.halted = False, False
        self.cia_next = False

    # -- helpers --
    def _ign(self, what: str) -> None:
        self.ignored[what] = self.ignored.get(what, 0) + 1

    def _trigger(self, c: int, offset: int = 0) -> tuple[int, int, int, int] | None:
        """(lc, len_words, rep_lc, rep_len_words) or None = the channel stops."""
        ch = self.ch[c]
        if ch.inst == 0:
            return None
        ins = self.mod.instruments[ch.inst - 1]
        if ins.length == 0:
            return None
        start = self.base + ins.offset
        looped = ins.rep_len > 2
        rep = (start + ins.rep_start, ins.rep_len // 2) if looped else (start, 1)
        if offset >= ins.length:
            if not looped:
                return None
            return (rep[0], rep[1], rep[0], rep[1])
        return (start + offset, (ins.length - offset) // 2, rep[0], rep[1])

    def _note_on(self, c: int, trig: dict, offset: int = 0) -> None:
        ch = self.ch[c]
        trig[c] = self._trigger(c, offset)
        if not ch.vib_wave & 4:
            ch.vib_pos = 0
        if not ch.trem_wave & 4:
            ch.trem_pos = 0
        ch.vu = -1                       # marks "triggered" for the vu rule

    # -- one row's tick 0 --
    def _row_start(self, trig: dict) -> None:
        pat = self.mod.order[self.pos]
        brk_row = jump_pos = None
        loop_to = None
        delay = 0
        for c in range(CHANNELS):
            ch = self.ch[c]
            per, ins, eff, par = self.mod.cell(pat, self.row, c)
            if ins > 31:
                ins = 0                    # M1: only 1-31 are instruments
            ch.r_period, ch.r_inst, ch.r_eff, ch.r_par = per, ins, eff, par
            ch.r_note = nearest_note(per) if per else 0xFF
            if ins:
                ch.inst = ins
                i = self.mod.instruments[ins - 1]
                ch.volume, ch.finetune = i.volume, i.finetune
            x, y = par >> 4, par & 0x0F
            if eff == 0xE and x == 5:
                ch.finetune = y - 16 if y & 8 else y
            if per:
                target = PERIOD_TABLE[ch.finetune][ch.r_note]
                if eff in (3, 5):
                    ch.porta_target, ch.porta_note = target, ch.r_note
                elif eff == 0xE and x == 0xD and y:
                    pass                                   # ED: at tick y
                else:
                    ch.period, ch.note = target, ch.r_note
                    off = 0
                    if eff == 9:
                        if par:
                            ch.offset_mem = par * 256
                        off = ch.offset_mem
                    self._note_on(c, trig, off)
            elif eff == 9 and par:
                ch.offset_mem = par * 256
            # tick-0 effects
            if eff == 3 and par:
                ch.porta_speed = par
            elif eff == 4:
                if x:
                    ch.vib_speed = x
                if y:
                    ch.vib_depth = y
            elif eff == 7:
                if x:
                    ch.trem_speed = x
                if y:
                    ch.trem_depth = y
            elif eff == 8:
                self._ign("8")
            elif eff == 0xB:
                jump_pos = par
            elif eff == 0xC:
                ch.volume = min(par, 64)
            elif eff == 0xD:
                r = x * 10 + y
                brk_row = r if r < ROWS else 0
            elif eff == 0xF:
                if par == 0:
                    self._ign("F00")
                elif par < 32:
                    self.speed = par
                else:
                    self.bpm = par
                    self.cia_next = True
            elif eff == 0xE:
                if x == 1 and ch.period:
                    ch.period = max(ch.period - y, MIN_PER)
                elif x == 2 and ch.period:
                    ch.period = min(ch.period + y, MAX_PER)
                elif x == 4:
                    ch.vib_wave = y
                elif x == 6:
                    if y == 0:
                        ch.loop_row = self.row
                    elif ch.loop_count == 0:
                        ch.loop_count = y
                        loop_to = ch.loop_row
                    else:
                        ch.loop_count -= 1
                        if ch.loop_count:
                            loop_to = ch.loop_row
                elif x == 7:
                    ch.trem_wave = y
                elif x == 0xA:
                    ch.volume = min(ch.volume + y, 64)
                elif x == 0xB:
                    ch.volume = max(ch.volume - y, 0)
                elif x == 0xC and y == 0:
                    ch.volume = 0
                elif x == 0xE:
                    if y:
                        delay = y
                elif x in (0, 3, 8, 0xF):
                    self._ign(f"E{x:X}")
        self.row_ticks = self.speed * (1 + delay)
        # where the next row comes from
        if jump_pos is not None or brk_row is not None:
            if jump_pos is not None:
                np_ = jump_pos
                if np_ <= self.pos:
                    self._loop_flag = True
            else:
                np_ = self.pos + 1
            self._next = (np_, brk_row or 0, True)
        elif loop_to is not None:
            self._next = (self.pos, loop_to, False)
        elif self.row + 1 >= ROWS:
            self._next = (self.pos + 1, 0, True)
        else:
            self._next = (self.pos, self.row + 1, False)

    # -- running effects (every tick; t > 0 for most) --
    def _effects(self, trig: dict) -> None:
        t = self.t
        for c in range(CHANNELS):
            ch = self.ch[c]
            eff, par = ch.r_eff, ch.r_par
            x, y = par >> 4, par & 0x0F
            ch.out_period, ch.out_volume = ch.period, ch.volume
            if eff == 0 and par:
                if ch.period:              # no note yet: no period effect
                    k = (0, x, y)[t % 3]
                    ch.out_period = PERIOD_TABLE[ch.finetune][min(ch.note + k, 35)]
                continue
            if t == 0:
                continue
            if not ch.period and eff in (1, 2, 3, 4):
                pass                       # no note yet: no period effect
            elif eff == 1:
                ch.period = max(ch.period - par, MIN_PER)
            elif eff == 2:
                ch.period = min(ch.period + par, MAX_PER)
            elif eff in (3, 5):
                if ch.period:
                    self._porta(ch)
            elif eff in (4, 6) and ch.period:
                ch.out_period = ch.period + (wave_value(ch.vib_wave, ch.vib_pos)
                                             * ch.vib_depth >> 7)
                ch.vib_pos = (ch.vib_pos + ch.vib_speed) & 63
            elif eff == 7:
                v = ch.volume + (wave_value(ch.trem_wave, ch.trem_pos) * ch.trem_depth >> 6)
                ch.out_volume = max(0, min(64, v))
                ch.trem_pos = (ch.trem_pos + ch.trem_speed) & 63
            elif eff == 0xE:
                if x == 9 and y and t % y == 0 and ch.period:
                    self._note_on(c, trig)
                elif x == 0xC and t == y:
                    ch.volume = 0
                elif x == 0xD and t == y and ch.r_period:
                    ch.period, ch.note = PERIOD_TABLE[ch.finetune][ch.r_note], ch.r_note
                    self._note_on(c, trig)
            if eff in (5, 6, 0xA):
                if x:
                    ch.volume = min(ch.volume + x, 64)
                else:
                    ch.volume = max(ch.volume - y, 0)
            if eff not in (4, 6):
                ch.out_period = ch.period
            if eff != 7:
                ch.out_volume = ch.volume
        for ch in self.ch:                 # M1: Paula never sees a wild period
            if ch.out_period:
                ch.out_period = max(PER_LO, min(PER_HI, ch.out_period))

    def _porta(self, ch: Chan) -> None:
        if not ch.porta_target:
            return
        if ch.period < ch.porta_target:
            ch.period = min(ch.period + ch.porta_speed, ch.porta_target)
        elif ch.period > ch.porta_target:
            ch.period = max(ch.period - ch.porta_speed, ch.porta_target)
        if ch.period == ch.porta_target:
            ch.note = ch.porta_note

    # -- one tick --
    def step(self) -> Tick | None:
        """The next tick, or None once the player halted (the stop flag)."""
        if self.halted:
            return None
        trig: dict[int, tuple | None] = {}
        self.cia = self.cia or self.cia_next
        cia_now = self.cia
        if self.t == 0:
            self._loop_flag = False
            self._row_start(trig)
        self._effects(trig)
        writes = self._emit(trig)
        self.ticks += 1
        # the interval after this tick: BPM once the CIA tempo runs, else a frame
        dur = self.bpm if (self.cia or self.cia_next) else 125
        self.acc = (self.acc + elapsed_inc(dur)) & 0xFFFFFFFF
        for ch in self.ch:
            ch.vu = ch.volume if ch.vu == -1 else max(0, ch.vu - VU_DECAY)
        if any(c.vu == -1 for c in self.ch):
            raise AssertionError("vu marker leaked")
        view = self.view()
        tick = Tick(self.ticks, writes, view, cia_now, self.bpm)
        # advance
        self.t += 1
        if self.t >= self.row_ticks:
            self.t = 0
            np_, nr, newpos = self._next
            if np_ >= self.mod.song_len:
                np_ = self.mod.restart if self.mod.restart < self.mod.song_len else 0
                self._loop_flag = True
            if self._loop_flag:
                self.looped = True
                if self.stop_at_end:
                    self.halted = True
            if newpos:
                for ch in self.ch:
                    ch.loop_row = 0
            self.pos, self.row = np_, nr
        return tick

    def _emit(self, trig: dict) -> list[tuple[int, int]]:
        w: list[tuple[int, int]] = []
        mask = 0
        for c in trig:
            mask |= 1 << c
        if mask:
            w.append((DMACON, mask))
        play = [c for c in range(CHANNELS) if c in trig and trig[c] is not None]
        for c in play:
            lc, ln = trig[c][0], trig[c][1]
            w += [(AUD[c], lc >> 16), (AUD[c] + 2, lc & 0xFFFF), (AUD[c] + 4, ln)]
        for c in range(CHANNELS):
            ch = self.ch[c]
            if c in trig or ch.out_period != ch.w_period:
                w.append((AUD[c] + 6, ch.out_period))
                ch.w_period = ch.out_period
            if c in trig or ch.out_volume != ch.w_volume:
                w.append((AUD[c] + 8, ch.out_volume))
                ch.w_volume = ch.out_volume
        if play:
            m = 0
            for c in play:
                m |= 1 << c
            w.append((DMACON, 0x8000 | m))
            for c in play:
                lc, ln = trig[c][2], trig[c][3]
                w += [(AUD[c], lc >> 16), (AUD[c] + 2, lc & 0xFFFF), (AUD[c] + 4, ln)]
        return w

    def view(self) -> bytes:
        flags = FLAG_PLAYING | (FLAG_LOOPED if self.looped else 0) | (FLAG_CIA if self.cia else 0)
        v = bytearray(VIEW_MAGIC)
        v += struct.pack(">I", self.ticks)
        v += self.mod.title
        v += bytes([self.mod.song_len, self.pos, self.mod.order[self.pos], self.row,
                    self.speed, self.bpm, flags, 0])
        for ch in self.ch:
            v += struct.pack(">HBBBBBB", ch.out_period, ch.inst, ch.out_volume,
                             ch.r_note, ch.r_eff, ch.r_par, ch.vu)
        v += struct.pack(">I", self.acc >> 16)
        assert len(v) == VIEW_LEN
        return bytes(v)

    def final_view(self, last: bytes) -> bytes:
        """The view the program publishes after it stopped: the last tick's,
        flags bit 0 (playing) cleared (L6, rev15-mod)."""
        v = bytearray(last)
        v[34] &= ~FLAG_PLAYING & 0xFF
        return bytes(v)

    def run(self, max_ticks: int) -> list[Tick]:
        out = []
        while len(out) < max_ticks:
            t = self.step()
            if t is None:
                break
            out.append(t)
        return out


def play(raw: bytes, max_ticks: int, base: int = 0x080000, stop_at_end: bool = True,
         start_pos: int = 0) -> tuple[list[Tick], Replay]:
    r = Replay(parse(raw, base), base=base, stop_at_end=stop_at_end, start_pos=start_pos)
    return r.run(max_ticks), r


# --- the 68k replay's tables (fixtures/mod/tables.i, generated) ----------------------------

NOTETAB_LEN = 1024


def tables_asm() -> str:
    """fixtures/mod/tables.i: ptab (16 finetune rows -8..7 x 36 words),
    sine (32 bytes), notetab (nearest note of periods 0..1023)."""
    out = ["; fixtures/mod/tables.i -- GENERATED by `python3 -m host.mod_ref --tables`",
           "; from host/mod_ref.py (PERIOD_TABLE, SINE, nearest_note); do not edit.",
           "; host/tests/test_mod_ref.py checks it is current.", "",
           "ptab:"]
    for f in range(-8, 8):
        row = PERIOD_TABLE[f]
        for k in range(0, 36, 12):
            out.append("        dc.w    " + ",".join(str(p) for p in row[k:k + 12])
                       + (f"           ; finetune {f}" if k == 0 else ""))
    out.append("sine:")
    for k in range(0, 32, 16):
        out.append("        dc.b    " + ",".join(str(v) for v in SINE[k:k + 16]))
    out.append("notetab:")
    for k in range(0, NOTETAB_LEN, 32):
        out.append("        dc.b    " + ",".join(str(nearest_note(p)) for p in range(k, k + 32)))
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--tables"]:
        sys.stdout.write(tables_asm())
    else:
        print(SEMANTICS)
