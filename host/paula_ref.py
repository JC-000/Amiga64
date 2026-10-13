"""W15-P reference: Paula's four audio channels and their U64 sampler voices.

The model of the W15-P audio module (docs/CONTRACTS.md "Audio (Paula)", to
be written).  Paula facts are the Amiga Hardware Reference Manual's audio
chapter: AUDxLCH/L ($0A0/$0A2 + 16x) and AUDxLEN ($0A4) are back-up
latches; when DMA starts on a channel (DMACON DMAEN and AUDxEN both set)
Paula copies them into its working pointer and counter and raises the
channel's interrupt (INTREQ bit 7 + x, level 4), and at the end of every
block (LEN words = 2 * LEN samples, LEN 0 = 65536 words) it copies them
again and raises the interrupt again.  So the CPU writes the NEXT block's
location while the current one plays.  AUDxPER (colour clocks per sample)
and AUDxVOL (0-64; bit 6 = full volume) act on the sound at once.  DMA off stops the
channel.  Sampler facts are docs/HARDWARE.md "U64 sampler (Ultimate
Audio)": sample time = (rate + 1) * 160 ns + 118 ns, volume 0-63 (64 wraps
to 0), START = $1000000 + REU offset, REPEAT A/B relative to START, no IRQ
at a loop point, a stopped voice holds its last sample.

Deviation (W15-P, supervisor-approved for cost): a block's end time is
fixed when it starts, from the PER of that moment; a PER written mid-block
changes the pitch at once and the block length from the next block on.

Time.  Guest time is E ticks (docs/CONTRACTS.md "Event seam").  A colour
clock is R E ticks, R held as the u16 fraction ``r16`` = R * 65536
(Amiga nominal: E = CCK / 5, r16 = 13107; the U64E player profile: E =
phi2 >> s with the real NTSC phi2, see ``r16_for``).  Block deadlines are
kept in fixed point (E * 65536), so a periodic channel never drifts; the
event fires at the E tick ``ceil(deadline / 65536)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PAL_CCK = 3546895          # Hz, Amiga PAL colour clock (MOD periods assume PAL)
AMIGA_E = 709379           # Hz, PAL E clock = CCK / 5 (MACH_FRAME_E = 14188 = E / 50)
NTSC_PHI2 = 1022727        # Hz, the U64E's CIA phi2 at any turbo (HARDWARE W8-H)
PAL_PHI2 = 985248          # Hz, VICE -pal

AUD_BASE = 0x0A0           # AUD0LCH; channel x at + 16 x
AUD_LCH, AUD_LCL, AUD_LEN, AUD_PER, AUD_VOL, AUD_DAT = 0, 2, 4, 6, 8, 10
DMACON = 0x096
DMAEN = 1 << 9
INT_AUD0 = 7               # INTREQ bit of channel 0 (7-10), level 4
LC_MASK = 0x1FFFFE         # 2 MB chip RAM, word aligned (bit 0 ignored)

REU_BASE = 0x1000000       # sampler START of REU offset 0
PAN = (0, 15, 15, 0)       # Amiga channels 0/3 left, 1/2 right (pan 0 = left)
VOL_MAX_HW = 63            # sampler volume ceiling (64 wraps to 0)
CAP = (1 << 46) - 1        # longest block / remainder, E * 65536 (2^30 E: the
                           # seam compares deadlines by signed u32 difference)

# rate = (per * RATE_K - RATE_C) >> 24, clamped to 0..$FFFF: the integer form
# of rate + 1 = round((per * 281.937 ns - 118 ns) / 160 ns) (HARDWARE "What
# Paula needs").  281.937 / 160 = 1.7621075, 118 / 160 + 1 - 0.5 = 1.2375,
# both * 2^24 (16 fraction bits leave the nearest step by up to 0.5 rate
# at long periods; 24 bits only at exact ties, < 0.002).
RATE_K = 29563210
RATE_C = 20761805


def r16_for(e_hz: float) -> int:
    """E ticks per PAL colour clock as a u16 fraction, for an E rate in Hz."""
    r = round(e_hz / PAL_CCK * 65536)
    if not 0 < r < 65536:
        raise ValueError("E rate must be below the colour clock")
    return r


R16_AMIGA = r16_for(AMIGA_E)                 # 13107 (1/5)
R16_U64_S0 = r16_for(NTSC_PHI2)              # 18897: U64E, s = 0


def sampler_rate(per: int) -> int:
    """Sampler RATE for a Paula period (colour clocks per sample)."""
    v = (per * RATE_K - RATE_C) >> 24
    return max(0, min(v, 0xFFFF))


def sample_ns(rate: int) -> float:
    """The sampler's measured sample time law (HARDWARE W10-H)."""
    return (rate + 1) * 160.0 + 118.0


def paula_ns(per: int) -> float:
    return per * 1e9 / PAL_CCK


def eff_vol(v: int) -> int:
    """AUDxVOL: bits 6-0; bit 6 set = 64 whatever bits 5-0 hold."""
    v &= 0x7F
    return 64 if v & 0x40 else v


def hw_vol(v: int) -> int:
    return min(eff_vol(v), VOL_MAX_HW)


def words(n: int) -> int:
    return n if n else 0x10000


@dataclass
class Voice:
    """What the sampler voice was last told (the backend's view)."""
    enable: bool = False
    start: int = 0             # absolute sampler address
    length: int = 0            # samples
    rate: int = 0
    vol: int = 0
    pan: int = 0
    repeat: tuple[int, int] | None = None   # (A, B), relative to START
    programs: int = 0          # full (re)programs, the glitch count


@dataclass
class Chan:
    lc: int = 0                # latches (AUDxLC, AUDxLEN)
    len: int = 0
    per: int = 0
    vol: int = 0
    on: bool = False
    cur_lc: int = 0            # the block being played
    cur_len: int = 0
    dl: int = 0                # block end, E * 65536
    nblk: int = 0              # blocks started (interrupts raised)


@dataclass
class Paula:
    r16: int = R16_AMIGA
    zero_reu: int = 0xDF4000   # REU offset of >= 2 zero bytes (DC-hold silence)
    ch: list[Chan] = field(default_factory=lambda: [Chan() for _ in range(4)])
    vc: list[Voice] = field(default_factory=lambda: [Voice(pan=p) for p in PAN])
    dmacon: int = 0
    posts: list[tuple[int, int]] = field(default_factory=list)  # (E, channel)
    ndat: int = 0              # AUDxDAT writes (CPU mode: not supported)

    # --- timing -------------------------------------------------------------
    def dur(self, c: Chan) -> int:
        """One block of the current block's length at the current period,
        in E * 65536."""
        return min(2 * words(c.cur_len) * max(c.per, 1) * self.r16, CAP)

    @staticmethod
    def due(dl: int) -> int:
        """E tick of a fixed-point deadline (never early)."""
        return -(-dl // 65536)

    def next_due(self) -> int | None:
        """The earliest armed deadline in E ticks (the slot's arm time)."""
        d = [self.due(c.dl) for c in self.ch if c.on]
        return min(d) if d else None

    # --- the backend --------------------------------------------------------
    def _program(self, x: int) -> None:
        """Play channel x's current block from its start; loop in hardware
        when the latches already name the same block."""
        c, v = self.ch[x], self.vc[x]
        v.enable = True
        v.start = REU_BASE + c.cur_lc
        v.length = 2 * words(c.cur_len)
        v.rate = sampler_rate(c.per)
        v.vol = hw_vol(c.vol)
        v.repeat = self._loop(x)
        v.programs += 1

    def _loop(self, x: int) -> tuple[int, int] | None:
        """REPEAT A/B when the next block (the latches) ends where the
        voice's programmed span ends and starts inside it: the hardware
        loop then plays exactly Paula's next blocks.  Otherwise None: the
        voice stops at its end and the 6510 reprograms it at the block
        end."""
        c, v = self.ch[x], self.vc[x]
        a = REU_BASE + (c.lc & LC_MASK) - v.start
        n = 2 * words(c.len)
        if 0 <= a and a + n == v.length:
            return (a, a + n)
        return None

    def _silence(self, x: int) -> None:
        v = self.vc[x]
        v.enable = True
        v.start = REU_BASE + self.zero_reu
        v.length = 2
        v.repeat = None
        v.programs += 1

    def _seamless(self, x: int) -> bool:
        """Does the voice's hardware loop already play the new block?"""
        c, v = self.ch[x], self.vc[x]
        if v.repeat is None:
            return False
        a, b = v.repeat
        return v.start + a == REU_BASE + c.cur_lc and b - a == 2 * words(c.cur_len)

    # --- guest writes -------------------------------------------------------
    def write(self, now: int, off: int, val: int) -> bool:
        """A word write to custom register ``off`` at E time ``now``.
        DMACON takes the register's new value (after SET/CLR)."""
        if off == DMACON:
            self._dmacon(now, val & 0x7FFF)
            return True
        if not AUD_BASE <= off < AUD_BASE + 0x40:
            return False
        x, r = divmod(off - AUD_BASE, 16)
        c, v = self.ch[x], self.vc[x]
        val &= 0xFFFF
        if r == AUD_LCH:
            c.lc = (val << 16 | c.lc & 0xFFFF) & LC_MASK
        elif r == AUD_LCL:
            c.lc = (c.lc & 0xFFFF0000 | val) & LC_MASK
        elif r == AUD_LEN:
            c.len = val
        elif r == AUD_PER:
            if c.on:                        # heard at once; the block end
                v.rate = sampler_rate(val)  # moves from the next block on
            c.per = val
        elif r == AUD_VOL:
            c.vol = val
            if c.on:
                v.vol = hw_vol(val)
        elif r == AUD_DAT:
            self.ndat += 1
        else:
            return False
        if r in (AUD_LCH, AUD_LCL, AUD_LEN) and c.on:
            v.repeat = self._loop(x)      # the voice's loop follows the latches
        return True

    def _dmacon(self, now: int, new: int) -> None:
        old, self.dmacon = self.dmacon, new
        for x in range(4):
            was = bool(old & DMAEN and old & 1 << x)
            on = bool(new & DMAEN and new & 1 << x)
            if on and not was:
                self._start(now, x, now * 65536)
            elif was and not on:
                self.ch[x].on = False
                self._silence(x)

    def _start(self, now: int, x: int, t0: int) -> None:
        c = self.ch[x]
        c.on = True
        c.cur_lc, c.cur_len = c.lc & LC_MASK, c.len
        c.dl = t0 + self.dur(c)
        c.nblk += 1
        self.posts.append((now, x))
        self._program(x)

    # --- block ends -----------------------------------------------------------
    def fire(self, now: int) -> list[int]:
        """Service every channel whose block end is due at E time ``now``
        (one pass: each channel at most once).  Returns the channels."""
        out = []
        for x, c in enumerate(self.ch):
            if not c.on or self.due(c.dl) > now:
                continue
            c.cur_lc, c.cur_len = c.lc & LC_MASK, c.len
            c.dl += self.dur(c)
            c.nblk += 1
            self.posts.append((now, x))
            if not self._seamless(x):
                self._program(x)
            out.append(x)
        return out

    def run_until(self, t: int) -> None:
        """Fire every deadline up to E time t, each at its own due tick."""
        while (d := self.next_due()) is not None and d <= t:
            self.fire(d)
