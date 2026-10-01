"""Host reference models of the OCS chipset subset the S3 shim needs (S3.0).

docs/S3_PLAN.md §3-§4.  These classes were promoted out of
``tools/s3probe/aros_boot_model.py`` (which now imports them, and boots the
AROS ROM bit-identically to before) and are checked against FS-UAE (A500,
OCS) by ``tests/chip_oracle.py``.  They are **the reference** that the C64
side (S3.1 onwards) is tested against.  Pure Python, stdlib only.

API -- this is all of it:

``setclr(reg, val)``
    The SET/CLR rule of DMACON/INTENA/INTREQ/ADKCON: bit 15 set -> OR in
    bits 0-14, else clear them.  Result is 15 bits.
``Ram(size=0x200000)``
    Flat big-endian memory (``r8/r16/w8/w16/r_block/w_block``), the shape
    the blitter and copper expect (machine68k's memory has the same methods).
``Paula()``
    ``intena intreq dmacon adkcon`` and ``bzero`` (set by the last blit).
    ``write(off, val) -> bool`` (True for DMACON/INTENA/INTREQ/ADKCON),
    ``request(bit)`` (a hardware source raises an INTREQ bit),
    ``level(mask=0x3FFF) -> 0..6`` (autovector IPL: INTEN master, highest
    enabled pending source), ``dmaconr()``.
``CIA(irq=None)``
    An 8520: ``read(r)``/``write(r, v)`` on register number 0-15,
    ``eclock(n)`` (advance timers by n E-clock ticks), ``tod_tick(n)``,
    ``update()``.  ``irq()`` is called whenever the chip's /INT is asserted
    (ICR data & mask), i.e. after a timer event, a TOD alarm, a mask write.
    Inputs: ``pra_in``/``prb_in`` (port pins where DDR is 0).  A TODHI
    write halts TOD until the TODLO write; reading TODHI latches the
    value until TODLO is read.  Latch 0 underflows on every tick.
``Blitter(mem)``
    ``blit(regs, size, ecs=False) -> (kind, words)``: a synchronous blit
    (completes at once) with the register file ``regs`` (256 custom words,
    indexed by offset >> 1).  Normal mode (4 channels, shifts, masks,
    ascending/descending, modulos, inclusive/exclusive fill) is the
    reference.  **Line mode is an unverified approximation, logged only**
    (S3_PLAN §3.3: the boot uses none).  Pointers are written back to
    ``regs``, and so are the last fetched A/B/C words (BLTxDAT); ``zero``
    is BZERO of the last blit.  ``write_bdat(regs, v)`` is the CPU's
    BLTBDAT write: B is shifted once, at the write, with the BSH then in
    BLTCON1 (the B hold register a blit with B DMA off uses as is).  IFE
    and EFE both set = exclusive fill.  ``kind`` is a short description
    (channel letters, minterm, desc/fill/shift) for statistics.
``Copper(strict=True)``
    ``run(mem, regs, write, limit=4000) -> n``: interprets the list at COP1LC
    (read live from ``regs``) once, MOVEs through ``write(reg, val)``,
    COPJMP1/2 jump, WAITs are ignored (the model runs once per frame at
    vblank: the last value per frame wins), $FFFF,$FFFE ends it.  OCS
    danger rule: a MOVE below $080 stops it, or below $040 with COPCON
    CDANG.  **SKIP is not modelled**: strict raises CopperSkipError, else
    it is counted in ``skips`` and ignored.  Do not take this model as the
    truth for WAIT/SKIP timing.  Returns instructions run.
``Chipset(mem)``
    The wiring of the above, as the boot model and the oracle use it:
    ``regs``, ``paula``, ``ciaa`` (INTREQ PORTS, bit 3), ``ciab`` (EXTER,
    bit 13), ``blitter``, ``cop`` (the Copper), ``serial`` (SERDAT bytes),
    ``ipl``.
    ``custom_read(off)``/``custom_write(off, val)`` for a word at
    $DFF000+off (reads return the §3.4 fixed values), ``cia_select(addr)``
    decodes bank $BF (odd $BFExx1 = CIA-A, even $BFDxx0 = CIA-B),
    ``run_copper()``.  Hooks a subclass may override: ``beam() -> (v, h)``
    (from ``cycles``), ``update_irq()``, ``blit(size, ecs)``,
    ``dsklen(val)``.

``MachTime(s=4)``
    The S3.2 time base of the C64 machine loop (``runtime/ops/mach.s``,
    docs/CONTRACTS.md "Machine loop (S3.2)"): guest E clock = C64 phi2
    counter >> *s*, one guest frame = ``MACH_FRAME_E`` (14188) E ticks.
    ``frame_phi2()``, ``fps(phi2_hz)``, ``beam(t)`` -> ``(vposr, vhposr)``
    for *t* phi2 cycles into the current frame (clamped to the frame's last
    tick), ``pos(e)`` -> ``(v, h)`` for E tick *e*, and ``is_beam(v, h)``
    (the pair is one ``pos`` produces).

**Copper SKIP and WAIT beam timing are not modelled; later packages must
not derive SKIP/WAIT behaviour from this model.**  The copper runs once per
frame and never feeds back into CPU timing here: no DMA slots are stolen,
and no CPU-visible beam position depends on where the copper is.

Deliberate model limits (S3_PLAN §3.1/§3.6): time is the caller's (the
CIA only moves in ``eclock``/``tod_tick``); the blitter is synchronous
(BBUSY never reads 1) and the copper has no beam; byte writes to custom
registers write the byte to both halves (``aros_boot_model.c_w8``);
unmapped $DF0000-$DFEFFF reads 0.
"""
from __future__ import annotations

CPU_HZ = 7_093_790          # PAL 68000 clock
LINE_CYC = 454              # CPU cycles per raster line
LINES = 313                 # lines per PAL frame
FRAME_CYC = LINE_CYC * LINES
ECLK_DIV = 10               # E clock = CPU / 10

CUSTOM_NAMES = {
    0x000: "BLTDDAT", 0x002: "DMACONR", 0x004: "VPOSR", 0x006: "VHPOSR",
    0x008: "DSKDATR", 0x00A: "JOY0DAT", 0x00C: "JOY1DAT", 0x00E: "CLXDAT",
    0x010: "ADKCONR", 0x012: "POT0DAT", 0x014: "POT1DAT", 0x016: "POTINP",
    0x018: "SERDATR", 0x01A: "DSKBYTR", 0x01C: "INTENAR", 0x01E: "INTREQR",
    0x020: "DSKPTH", 0x022: "DSKPTL", 0x024: "DSKLEN", 0x026: "DSKDAT",
    0x028: "REFPTR", 0x02A: "VPOSW", 0x02C: "VHPOSW", 0x02E: "COPCON",
    0x030: "SERDAT", 0x032: "SERPER", 0x034: "POTGO", 0x036: "JOYTEST",
    0x040: "BLTCON0", 0x042: "BLTCON1", 0x044: "BLTAFWM", 0x046: "BLTALWM",
    0x048: "BLTCPTH", 0x04A: "BLTCPTL", 0x04C: "BLTBPTH", 0x04E: "BLTBPTL",
    0x050: "BLTAPTH", 0x052: "BLTAPTL", 0x054: "BLTDPTH", 0x056: "BLTDPTL",
    0x058: "BLTSIZE", 0x05A: "BLTCON0L", 0x05C: "BLTSIZV", 0x05E: "BLTSIZH",
    0x060: "BLTCMOD", 0x062: "BLTBMOD", 0x064: "BLTAMOD", 0x066: "BLTDMOD",
    0x070: "BLTCDAT", 0x072: "BLTBDAT", 0x074: "BLTADAT", 0x07C: "DENISEID",
    0x07E: "DSKSYNC", 0x080: "COP1LCH", 0x082: "COP1LCL", 0x084: "COP2LCH",
    0x086: "COP2LCL", 0x088: "COPJMP1", 0x08A: "COPJMP2", 0x08C: "COPINS",
    0x08E: "DIWSTRT", 0x090: "DIWSTOP", 0x092: "DDFSTRT", 0x094: "DDFSTOP",
    0x096: "DMACON", 0x098: "CLXCON", 0x09A: "INTENA", 0x09C: "INTREQ",
    0x09E: "ADKCON", 0x100: "BPLCON0", 0x102: "BPLCON1", 0x104: "BPLCON2",
    0x106: "BPLCON3", 0x108: "BPL1MOD", 0x10A: "BPL2MOD", 0x10C: "BPLCON4",
    0x1DC: "BEAMCON0", 0x1FC: "FMODE",
}
for _i in range(4):
    for _j, _n in enumerate(("LCH", "LCL", "LEN", "PER", "VOL", "DAT")):
        CUSTOM_NAMES[0x0A0 + 16 * _i + 2 * _j] = f"AUD{_i}{_n}"
for _i in range(6):
    CUSTOM_NAMES[0x0E0 + 4 * _i] = f"BPL{_i + 1}PTH"
    CUSTOM_NAMES[0x0E2 + 4 * _i] = f"BPL{_i + 1}PTL"
    CUSTOM_NAMES[0x110 + 2 * _i] = f"BPL{_i + 1}DAT"
for _i in range(8):
    CUSTOM_NAMES[0x120 + 4 * _i] = f"SPR{_i}PTH"
    CUSTOM_NAMES[0x122 + 4 * _i] = f"SPR{_i}PTL"
    for _j, _n in enumerate(("POS", "CTL", "DATA", "DATB")):
        CUSTOM_NAMES[0x140 + 8 * _i + 2 * _j] = f"SPR{_i}{_n}"
for _i in range(32):
    CUSTOM_NAMES[0x180 + 2 * _i] = f"COLOR{_i:02d}"

CIA_REGS = ["PRA", "PRB", "DDRA", "DDRB", "TALO", "TAHI", "TBLO", "TBHI",
            "TODLO", "TODMID", "TODHI", "UNUSED", "SDR", "ICR", "CRA", "CRB"]

# Fixed custom read values the AROS boot relies on (S3_PLAN §3.4).
POTINP_VAL = 0xFF00         # right mouse / fire buttons up
SERDATR_VAL = 0x3000        # TBE | TSRE: the transmitter is always empty
DENISEID_VAL = 0xFFFF       # OCS has no DENISEID: the bus floats
CIAA_PRA_IN = 0xEB          # /FIR1 /FIR0 up, /RDY high, /TK0 low, /WPRO high, /CHNG low

# INTREQ bit -> autovector level, highest first
IRQ_LEVELS = ((13, 6), (12, 5), (11, 5), (10, 4), (9, 4), (8, 4), (7, 4),
              (6, 3), (5, 3), (4, 3), (3, 2), (2, 1), (1, 1), (0, 1))


def setclr(reg: int, val: int) -> int:
    """DMACON/INTENA/INTREQ/ADKCON write: bit 15 = SET (else CLR) bits 0-14."""
    return reg | (val & 0x7FFF) if val & 0x8000 else reg & ~val & 0x7FFF


class Ram:
    """Flat big-endian RAM with the machine68k memory methods the models use."""

    def __init__(self, size: int = 0x200000):
        self.b = bytearray(size)

    def r8(self, a):
        return self.b[a]

    def r16(self, a):
        return (self.b[a] << 8) | self.b[a + 1]

    def w8(self, a, v):
        self.b[a] = v & 0xFF

    def w16(self, a, v):
        self.b[a] = (v >> 8) & 0xFF
        self.b[a + 1] = v & 0xFF

    def r_block(self, a, n):
        return bytes(self.b[a:a + n])

    def w_block(self, a, data):
        self.b[a:a + len(data)] = data


class Paula:
    """INTENA/INTREQ/DMACON/ADKCON (the interrupt and DMA-enable half of the
    custom chips; the registers are Paula's and Agnus's on an A500)."""

    def __init__(self):
        self.intena = self.intreq = self.dmacon = self.adkcon = 0
        self.bzero = False

    def write(self, off: int, val: int) -> bool:
        if off == 0x096:
            self.dmacon = setclr(self.dmacon, val)
        elif off == 0x09A:
            self.intena = setclr(self.intena, val)
        elif off == 0x09C:
            self.intreq = setclr(self.intreq, val)
        elif off == 0x09E:
            self.adkcon = setclr(self.adkcon, val)
        else:
            return False
        return True

    def request(self, bit: int) -> None:
        self.intreq |= 1 << bit

    def dmaconr(self) -> int:
        """DMACONR: BLTPRI and the enables, BZERO of the last blit; BBUSY
        never (synchronous blitter).  (W6-D's model returned DMACON & $03FF;
        chip_oracle found BLTPRI and BZERO missing.)"""
        return (self.dmacon & 0x07FF) | (0x2000 if self.bzero else 0)

    def level(self, mask: int = 0x3FFF) -> int:
        """IPL presented to the CPU: 0 unless INTEN, else the level of the
        highest source that is enabled and pending (within ``mask``)."""
        if not self.intena & 0x4000:
            return 0
        act = self.intena & self.intreq & mask
        for b, lvl in IRQ_LEVELS:
            if act & (1 << b):
                return lvl
        return 0


class CIA:
    """MOS 8520: ports, timers A/B, TOD with alarm, ICR (S3_PLAN §3.1)."""

    def __init__(self, irq=None, name: str = ""):
        self.irq, self.name = irq, name
        self.pra_in, self.prb_in = 0xFF, 0xFF
        self.pra = self.prb = self.ddra = self.ddrb = 0
        self.ta = self.tb = 0xFFFF
        self.ta_latch = self.tb_latch = 0xFFFF
        self.cra = self.crb = 0
        self.icr_data = self.icr_mask = 0
        self.tod = 0
        self.alarm = 0
        self.tod_latched = None
        self.tod_halt = False
        self.sdr = 0

    def eclock(self, n):
        """Advance the timers by n E-clock ticks."""
        ta_under = 0
        if self.cra & 1 and not self.cra & 0x20:
            if n > self.ta:
                span = self.ta_latch + 1                # latch 0: underflow every tick
                left = n - self.ta - 1
                ta_under = 1 + left // span
                if self.cra & 8:            # one-shot
                    ta_under = 1
                    self.cra &= ~1
                    self.ta = self.ta_latch
                else:
                    self.ta = self.ta_latch - (left % span)
                self.icr_data |= 1
            else:
                self.ta -= n
        if self.crb & 1:
            mode = (self.crb >> 5) & 3
            steps = n if mode == 0 else (ta_under if mode >= 2 else 0)
            if steps:
                if steps > self.tb:
                    span = self.tb_latch + 1
                    left = steps - self.tb - 1
                    if self.crb & 8:
                        self.crb &= ~1
                        self.tb = self.tb_latch
                    else:
                        self.tb = self.tb_latch - (left % span)
                    self.icr_data |= 2
                else:
                    self.tb -= steps
        self.update()

    def tod_tick(self, n=1):
        if self.tod_halt:
            return
        for _ in range(n):
            self.tod = (self.tod + 1) & 0xFFFFFF
            if self.tod == self.alarm:
                self.icr_data |= 4
        self.update()

    def tod_ticks(self, n):
        """tod_tick(n) in O(1) (W9-C, S3.4): the alarm bit is set iff one of
        the n ticks lands on the alarm, i.e. k' <= n with
        k' = ((alarm - tod - 1) mod 2^24) + 1 (``cia_tod_kprime``)."""
        if self.tod_halt:
            return
        if cia_tod_kprime(self.tod, self.alarm) <= n:
            self.icr_data |= 4
        self.tod = (self.tod + n) & 0xFFFFFF
        self.update()

    def update(self):
        if self.icr_data & self.icr_mask & 0x1F and self.irq is not None:
            self.irq()

    def read(self, r):
        if r == 0:
            return (self.pra & self.ddra) | (self.pra_in & ~self.ddra & 0xFF)
        if r == 1:
            return (self.prb & self.ddrb) | (self.prb_in & ~self.ddrb & 0xFF)
        if r == 2:
            return self.ddra
        if r == 3:
            return self.ddrb
        if r == 4:
            return self.ta & 0xFF
        if r == 5:
            return self.ta >> 8
        if r == 6:
            return self.tb & 0xFF
        if r == 7:
            return self.tb >> 8
        if r in (8, 9, 10):
            if r == 10:
                self.tod_latched = self.tod
            v = self.tod_latched if self.tod_latched is not None else self.tod
            if r == 8:
                self.tod_latched = None
            return (v >> (8 * (r - 8))) & 0xFF
        if r == 12:
            return self.sdr
        if r == 13:
            v = self.icr_data | (0x80 if self.icr_data & self.icr_mask & 0x1F else 0)
            self.icr_data = 0
            return v
        if r == 14:
            return self.cra & 0xEF
        if r == 15:
            return self.crb & 0xEF
        return 0xFF

    def write(self, r, v):
        if r == 0:
            self.pra = v
        elif r == 1:
            self.prb = v
        elif r == 2:
            self.ddra = v
        elif r == 3:
            self.ddrb = v
        elif r == 4:
            self.ta_latch = (self.ta_latch & 0xFF00) | v
        elif r == 5:
            self.ta_latch = (self.ta_latch & 0xFF) | (v << 8)
            if not self.cra & 1:
                self.ta = self.ta_latch
            if self.cra & 8:                # one-shot: hi write starts
                self.ta = self.ta_latch
                self.cra |= 1
        elif r == 6:
            self.tb_latch = (self.tb_latch & 0xFF00) | v
        elif r == 7:
            self.tb_latch = (self.tb_latch & 0xFF) | (v << 8)
            if not self.crb & 1:
                self.tb = self.tb_latch
            if self.crb & 8:
                self.tb = self.tb_latch
                self.crb |= 1
        elif r in (8, 9, 10):
            sh = 8 * (r - 8)
            if self.crb & 0x80:
                self.alarm = (self.alarm & ~(0xFF << sh)) | (v << sh)
            else:
                self.tod = (self.tod & ~(0xFF << sh)) | (v << sh)
                if r == 10:
                    self.tod_halt = True        # TODHI write stops the clock ...
                elif r == 8:
                    self.tod_halt = False       # ... until TODLO (TODMID: neither)
        elif r == 12:
            self.sdr = v
        elif r == 13:
            if v & 0x80:
                self.icr_mask |= v & 0x1F
            else:
                self.icr_mask &= ~v & 0x1F
            self.update()
        elif r == 14:
            if v & 0x10:
                self.ta = self.ta_latch
            self.cra = v & 0xEF
        elif r == 15:
            if v & 0x10:
                self.tb = self.tb_latch
            self.crb = v & 0xEF


# ---------------------------------------------------------------------------
# S3.4 guest CIA timing on the C64 (W9-C, runtime/ops/cia.s): CIA-B's TOD
# counts PAL lines from guest E time on one fixed grid (line k starts at
# colour clock 227 k, 5 colour clocks per E tick, 15625 Hz); CIA-A's TOD
# counts VERTBs (MACH_FRAME_E E ticks, 50 Hz).  docs/CONTRACTS.md
# "CIA-A/B (S3.4)".
# ---------------------------------------------------------------------------

CIA_LINE_CC = 227           # colour clocks per line
CIA_CC_PER_E = 5            # colour clocks per E tick


def cia_lines(e: int) -> int:
    """Line starts passed after *e* E ticks of the grid (0 at e = 0)."""
    return CIA_CC_PER_E * e // CIA_LINE_CC


def cia_line_e(k: int) -> int:
    """First E tick at which cia_lines() reaches *k*: ceil(227 k / 5)."""
    return -(-CIA_LINE_CC * k // CIA_CC_PER_E)


def cia_tod_kprime(tod: int, alarm: int) -> int:
    """Ticks until the TOD next equals the alarm (1..2^24)."""
    return ((alarm - tod - 1) & 0xFFFFFF) + 1


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _ptr(r, hi):
    return ((r[hi >> 1] << 16) | r[(hi + 2) >> 1]) & 0x1FFFFE


class Blitter:
    """Synchronous OCS blitter over ``mem`` (r16/w16)."""

    def __init__(self, mem):
        self.mem = mem
        self.zero = True
        self.bprev = 0          # last raw B word (CPU-written or fetched)
        self.bhold = 0          # shifted B: what a blit with B DMA off uses

    def write_bdat(self, r, v):
        """CPU write of BLTBDAT: B is shifted once, now, with the current BSH."""
        bsh = r[0x042 >> 1] >> 12
        if r[0x042 >> 1] & 2:
            self.bhold = ((v << 16 | self.bprev) >> (16 - bsh)) & 0xFFFF if bsh else v
        else:
            self.bhold = ((self.bprev << 16 | v) >> bsh) & 0xFFFF
        self.bprev = v

    @staticmethod
    def kind(regs) -> str:
        con0, con1 = regs[0x040 >> 1], regs[0x042 >> 1]
        if con1 & 1:
            return "line"
        lf, ash, bsh = con0 & 0xFF, con0 >> 12, con1 >> 12
        chans = "".join(c for c, bit in zip("ABCD", (11, 10, 9, 8)) if con0 >> bit & 1)
        return f"{chans} lf={lf:02X}{' desc' if con1 & 2 else ''}" \
               f"{' fill' if con1 & 0x18 else ''}{' shift' if ash or bsh else ''}"

    def blit(self, r, size, ecs=False):
        """Run the blit ``size`` (BLTSIZE, or BLTSIZV/H when ``ecs``) with
        the register file ``r``.  Returns (kind, words); words is 0 for
        line mode."""
        con0, con1 = r[0x040 >> 1], r[0x042 >> 1]
        if ecs:
            h, w = (size >> 6) or 1024, size & 0x3F or 64
        else:
            h, w = (size >> 6) & 0x3FF or 1024, size & 0x3F or 64
        kind = self.kind(r)
        if con1 & 1:
            self.line(r, h)
            return kind, 0
        mem = self.mem
        usea, useb, usec, used = (con0 >> 11) & 1, (con0 >> 10) & 1, (con0 >> 9) & 1, (con0 >> 8) & 1
        lf = con0 & 0xFF
        ash, bsh = con0 >> 12, con1 >> 12
        desc = con1 & 2
        fill = con1 & 0x18
        apt, bpt, cpt, dpt = _ptr(r, 0x050), _ptr(r, 0x04C), _ptr(r, 0x048), _ptr(r, 0x054)
        amod, bmod, cmod, dmod = (_s16(r[x >> 1]) for x in (0x064, 0x062, 0x060, 0x066))
        step = -2 if desc else 2
        if desc:
            amod, bmod, cmod, dmod = -amod, -bmod, -cmod, -dmod
        afwm, alwm = r[0x044 >> 1], r[0x046 >> 1]
        adat, bdat, cdat = r[0x074 >> 1], r[0x072 >> 1], r[0x070 >> 1]
        aold = bold = 0
        bsh_v = self.bhold                      # B DMA off: the hold register, as written
        nz = 0
        for _y in range(h):
            fc = (con1 >> 2) & 1
            for x in range(w):
                if usea:
                    adat = mem.r16(apt)
                    apt += step
                if useb:
                    bdat = mem.r16(bpt)
                    bpt += step
                if usec:
                    cdat = mem.r16(cpt)
                    cpt += step
                a = adat
                if x == 0:
                    a &= afwm
                if x == w - 1:
                    a &= alwm
                if desc:
                    ash_v = ((a << 16 | aold) >> (16 - ash)) & 0xFFFF if ash else a
                    if useb:
                        bsh_v = ((bdat << 16 | bold) >> (16 - bsh)) & 0xFFFF if bsh else bdat
                else:
                    ash_v = ((aold << 16 | a) >> ash) & 0xFFFF
                    if useb:
                        bsh_v = ((bold << 16 | bdat) >> bsh) & 0xFFFF
                aold, bold = a, bdat
                d = 0
                na, nb, nc = ~ash_v & 0xFFFF, ~bsh_v & 0xFFFF, ~cdat & 0xFFFF
                if lf & 0x80: d |= ash_v & bsh_v & cdat
                if lf & 0x40: d |= ash_v & bsh_v & nc
                if lf & 0x20: d |= ash_v & nb & cdat
                if lf & 0x10: d |= ash_v & nb & nc
                if lf & 0x08: d |= na & bsh_v & cdat
                if lf & 0x04: d |= na & bsh_v & nc
                if lf & 0x02: d |= na & nb & cdat
                if lf & 0x01: d |= na & nb & nc
                if fill:                         # right to left, carry per row
                    out = 0
                    for bit in range(16):
                        s = (d >> bit) & 1
                        if con1 & 0x18 == 0x08:  # IFE alone: edges drawn (both = EFE)
                            o = fc | s
                            fc ^= s
                        else:                    # EFE: closing edge not drawn
                            fc ^= s
                            o = fc
                        out |= o << bit
                    d = out
                nz |= d
                if used:
                    mem.w16(dpt, d)
                    dpt += step
            apt += amod if usea else 0
            bpt += bmod if useb else 0
            cpt += cmod if usec else 0
            dpt += dmod if used else 0
        for reg, p in ((0x050, apt), (0x04C, bpt), (0x048, cpt), (0x054, dpt)):
            r[reg >> 1], r[(reg + 2) >> 1] = (p >> 16) & 0x1F, p & 0xFFFF
        # the data registers hold the last fetched words (FS-UAE: a_stale_dat, c_stale_dat)
        if usea:
            r[0x074 >> 1] = adat
        if usec:
            r[0x070 >> 1] = cdat
        if useb:
            r[0x072 >> 1] = self.bprev = bdat
            self.bhold = bsh_v
        self.zero = not nz
        return kind, w * h

    def line(self, r, h):
        """Blitter line mode.  UNVERIFIED approximation (HRM algorithm,
        single-pixel lines drawn with the minterm against C), kept only so
        the boot model counts and roughly draws line blits; not a reference."""
        con0, con1 = r[0x040 >> 1], r[0x042 >> 1]
        cpt = ((r[0x048 >> 1] << 16) | r[0x04A >> 1]) & 0x1FFFFE
        apt = ((r[0x050 >> 1] << 16) | r[0x052 >> 1])
        apt = apt - 0x100000000 if apt & 0x80000000 else apt
        amod = r[0x064 >> 1] - (0x10000 if r[0x064 >> 1] & 0x8000 else 0)
        bmod = r[0x062 >> 1] - (0x10000 if r[0x062 >> 1] & 0x8000 else 0)
        cmod = r[0x060 >> 1] - (0x10000 if r[0x060 >> 1] & 0x8000 else 0)
        ash = con0 >> 12
        sud, sul, aul = (con1 >> 4) & 1, (con1 >> 3) & 1, (con1 >> 2) & 1
        texture, bsh = r[0x072 >> 1], con1 >> 12
        lf = con0 & 0xFF
        err = apt
        mem = self.mem
        for _ in range(h):
            bit = 0x8000 >> ash
            c = mem.r16(cpt)
            a = bit
            b = 0xFFFF if (texture >> (15 - bsh)) & 1 else 0
            d = 0
            na, nb, nc = ~a & 0xFFFF, ~b & 0xFFFF, ~c & 0xFFFF
            for mt, t in ((0x80, a & b & c), (0x40, a & b & nc), (0x20, a & nb & c),
                          (0x10, a & nb & nc), (0x08, na & b & c), (0x04, na & b & nc),
                          (0x02, na & nb & c), (0x01, na & nb & nc)):
                if lf & mt:
                    d |= t
            mem.w16(cpt, d)
            bsh = (bsh + 1) & 15

            # step: major axis every pixel, minor when error >= 0
            def move(dx_major):
                nonlocal ash, cpt
                if dx_major == "x+":
                    ash += 1
                    if ash == 16:
                        ash = 0
                        cpt += 2
                elif dx_major == "x-":
                    ash -= 1
                    if ash < 0:
                        ash = 15
                        cpt -= 2
                elif dx_major == "y+":
                    cpt += cmod
                else:
                    cpt -= cmod
            # SUD: major is Y; SUL/AUL give directions
            if sud:
                move("y-" if sul else "y+")
                if err >= 0:
                    move("x-" if aul else "x+")
            else:
                move("x-" if sul else "x+")
                if err >= 0:
                    move("y-" if aul else "y+")
            err += amod if err >= 0 else bmod


class CopperSkipError(NotImplementedError):
    """A copper SKIP was met by a strict Copper (SKIP is not modelled)."""


class Copper:
    """Once-per-frame copper: MOVEs only (see the module docstring).

    SKIP is **not modelled**: whether it skips depends on the beam position
    at that point in the list, and a copper that runs once per frame with
    WAITs ignored has none.  Any rule we picked would be a guess, not a
    reference, so a strict Copper (the default) raises CopperSkipError, and
    a non-strict one counts the SKIPs in ``skips`` and treats them as no-ops.
    """

    def __init__(self, strict: bool = True):
        self.strict = strict
        self.skips = 0

    def run(self, mem, regs, write, limit=4000):
        pc = ((regs[0x080 >> 1] << 16) | regs[0x082 >> 1]) & 0x1FFFFE
        n = 0
        while n < limit:
            w1, w2 = mem.r16(pc), mem.r16(pc + 2)
            pc += 4
            n += 1
            if w1 & 1 == 0:                     # MOVE
                reg = w1 & 0x1FE
                if reg < (0x040 if regs[0x02E >> 1] & 2 else 0x080):
                    break                       # OCS danger rule: the copper stops
                if reg == 0x088:
                    pc = ((regs[0x080 >> 1] << 16) | regs[0x082 >> 1]) & 0x1FFFFE
                    continue
                if reg == 0x08A:
                    pc = ((regs[0x084 >> 1] << 16) | regs[0x086 >> 1]) & 0x1FFFFE
                    continue
                write(reg, w2)
            elif w1 == 0xFFFF and w2 == 0xFFFE:
                break
            elif w2 & 1:                        # SKIP
                self.skips += 1
                if self.strict:
                    raise CopperSkipError(f"copper SKIP ${w1:04X},${w2:04X} at ${pc - 4:06X}")
        return n


class Chipset:
    """Paula + CIA-A/B + blitter + copper wired as on an A500 (see API)."""

    def __init__(self, mem):
        self.mem = mem
        self.cycles = 0                          # virtual 68000 cycles (beam)
        self.regs = [0] * 256                    # custom word registers
        self.paula = Paula()
        self.ciaa = CIA(lambda: self.request(3), "CIAA")
        self.ciab = CIA(lambda: self.request(13), "CIAB")
        self.ciaa.pra_in = CIAA_PRA_IN
        self.blitter = Blitter(mem)
        self.cop = Copper()
        self.serial = bytearray()
        self.ipl = 0

    # ---- hooks -------------------------------------------------------------
    def beam(self):
        c = self.cycles % FRAME_CYC
        return c // LINE_CYC, (c % LINE_CYC) // 2

    def update_irq(self):
        self.ipl = self.paula.level()

    def blit(self, size, ecs=False):
        self.blitter.blit(self.regs, size, ecs)
        self.paula.bzero = self.blitter.zero
        self.request(6)                          # BLIT done

    def dsklen(self, val):
        pass

    # ---- wiring ------------------------------------------------------------
    def request(self, bit):
        self.paula.request(bit)
        self.update_irq()

    def custom_read(self, off):
        """Word read of $DFF000+off (off even, < $200)."""
        r = off & 0x1FE
        p = self.paula
        if r == 0x002:
            return p.dmaconr()
        if r == 0x004:
            return 0x8000 | (self.beam()[0] >> 8)       # LOF, Agnus ID 0 (OCS PAL)
        if r == 0x006:
            v, h = self.beam()
            return ((v & 0xFF) << 8) | (h & 0xFF)
        if r == 0x010:
            return p.adkcon
        if r == 0x016:
            return POTINP_VAL
        if r == 0x018:
            return SERDATR_VAL
        if r == 0x01C:
            return p.intena
        if r == 0x01E:
            return p.intreq
        if r == 0x07C:
            return DENISEID_VAL
        return 0

    def custom_write(self, off, val):
        """Word write of $DFF000+off (CPU or copper)."""
        r = off & 0x1FE
        self.regs[r >> 1] = val
        if self.paula.write(r, val):
            if r == 0x09A:
                self.update_irq()
            elif r == 0x09C:
                self.ciaa.update()               # a CIA still asserting /INT re-raises
                self.ciab.update()
                self.update_irq()
        elif r == 0x030:
            self.serial.append(val & 0xFF)
            self.request(0)                      # TBE
        elif r == 0x024:
            self.dsklen(val)
        elif r == 0x058:
            self.blit(val)
        elif r == 0x072:
            self.blitter.write_bdat(self.regs, val)
        elif r == 0x05E:                         # ECS BLTSIZH starts a blit
            self.blit(((self.regs[0x05C >> 1] & 0x3FF) << 6) | (val & 0x3F), ecs=True)

    @staticmethod
    def cia_select(addr):
        """Bank $BF decode: ("A", reg) for odd $BFExx1, ("B", reg) for even
        $BFDxx0, None for anything else."""
        off = addr & 0xFFFF
        reg = (off >> 8) & 0xF
        if off & 0x1000 == 0 and addr & 1:
            return "A", reg
        if off & 0x2000 == 0 and not addr & 1:
            return "B", reg
        return None

    def run_copper(self):
        """The per-frame copper pass (only with DMAEN and COPEN)."""
        if not (self.paula.dmacon & 0x0200 and self.paula.dmacon & 0x0080):
            return 0
        return self.cop.run(self.mem, self.regs, self.copper_write)

    def copper_write(self, reg, val):
        self.custom_write(reg, val)


# ---------------------------------------------------------------------------
# S3.2 machine-loop time base (W8-L).  The C64 side (runtime/ops/mach.s,
# runtime/ops/chip.s beam reads) is tested against this class by
# tests/run_mach.py; the formula is pinned in docs/CONTRACTS.md.
# ---------------------------------------------------------------------------

MACH_FRAME_E = 14188        # E ticks per guest frame (709379 Hz / 50)
MACH_CCK_PER_E = 5          # colour clocks per E tick (3546895 / 709379)
MACH_LINE_CCK = 227         # colour clocks per PAL line (H 0-226)
MACH_S_DEFAULT = 4          # runtime/ops/mach.s mach_s power-up value
MACH_S_MAX = 16             # mach_init clamps mach_s to this
VPOSR_STUB = 0x8000         # LOF, Agnus ID 0 (OCS PAL): S3.1 stub bits
C64_PAL_PHI2 = 985_248      # C64 PAL phi2 (Hz): VICE -pal
C64_NTSC_PHI2 = 1_022_727   # C64 NTSC phi2 (Hz): VICE -ntsc, the bench U64E


class MachTime:
    """Guest time and beam as a function of the C64 phi2 counter.

    E = phi2 >> s.  A frame is MACH_FRAME_E E ticks; E tick e of the frame
    is colour clock cc = 5 e, i.e. line v = cc // 227, h = cc % 227: 313
    lines (V 0-312), the last one partial (h 0-115, cc 70824-70939).
    VPOSR = $8000 | v >> 8 (LOF and Agnus ID of the stub, V8 in bit 0),
    VHPOSR = (v & $FF) << 8 | h.
    """

    def __init__(self, s: int = MACH_S_DEFAULT):
        self.s = min(s, MACH_S_MAX)

    def frame_phi2(self) -> int:
        """Frame length in phi2 cycles."""
        return MACH_FRAME_E << self.s

    def fps(self, phi2_hz: int = C64_PAL_PHI2) -> float:
        """Guest frames per wall-clock second on a C64 whose phi2 is
        *phi2_hz* (the runtime itself has no phi2 constant)."""
        return phi2_hz / self.frame_phi2()

    @staticmethod
    def pos(e: int) -> tuple[int, int]:
        """(v, h) of E tick *e* (0 <= e < MACH_FRAME_E) of a frame."""
        if not 0 <= e < MACH_FRAME_E:
            raise ValueError(f"E tick {e} outside the frame")
        return divmod(e * MACH_CCK_PER_E, MACH_LINE_CCK)

    def beam(self, t: int) -> tuple[int, int]:
        """(VPOSR, VHPOSR) *t* phi2 cycles into the frame.  A t past the
        frame's end (the VERTB not serviced yet) reads the last E tick."""
        e = min(t >> self.s, MACH_FRAME_E - 1) if t >= 0 else 0
        v, h = self.pos(e)
        return VPOSR_STUB | (v >> 8), (v & 0xFF) << 8 | h

    @staticmethod
    def is_beam(v: int, h: int) -> bool:
        """True if (v, h) is a position pos() can return."""
        cc = v * MACH_LINE_CCK + h
        return (0 <= h < MACH_LINE_CCK and cc % MACH_CCK_PER_E == 0
                and cc < MACH_FRAME_E * MACH_CCK_PER_E)


# ---------------------------------------------------------------------------
# S3.5 copper + display-register state (W10-P).  The C64 side
# (runtime/ops/cop.s, docs/CONTRACTS.md "Copper and display state (S3.5)")
# is tested against these by tests/run_cop.py.  Additive: Copper.run above
# is unchanged; run_at is the same interpreter started at a given PC (the
# PC a strobe loaded), with the C64's 24-bit pointer mask.
# ---------------------------------------------------------------------------

COP_LIMIT = 4000            # cop.s COP_LIMIT = Copper.run's default limit
COP_PC_MASK = 0xFFFFFE      # cop.s: 24-bit guest bus (Copper.run: $1FFFFE)
COP_END, COP_DANGER, COP_LIMITED, COP_IOBANK = 1, 2, 3, 4   # cop.s cop_why
COP_VALID = 0xA5            # disp_st+DISP_VALID once cop_reset ran

# disp_st (WAVE10_BRIEF C1 + the appended valid byte): field offsets,
# little-endian u16/u24 fields
DISP_GEN, DISP_DMACON, DISP_BPLCON, DISP_BPLPT = 0, 1, 3, 9
DISP_BPLMOD, DISP_DIW, DISP_COLOR, DISP_VALID, DISP_SZ = 27, 31, 39, 103, 104


class CopperAt(Copper):
    """``Copper`` (non-strict by default) run from a given PC:
    ``run_at(mem, regs, write, pc, limit, mask) -> n`` interprets exactly as
    ``Copper.run`` does from COP1LC (COPJMP1/2 pointers masked with *mask*).
    After it, ``why`` is the stop reason (COP_*) and ``pc`` the copper's PC:
    a copper stopped by the end marker or the danger rule stays on that
    instruction (the hardware waits there until the next restart); after
    the limit it points at the next one.  A PC in bank $BF/$DF stops it
    before the fetch (COP_IOBANK, cop.s: the C64 has no RAM there)."""

    def __init__(self, strict: bool = False):
        super().__init__(strict)
        self.why = 0
        self.pc = 0

    def run_at(self, mem, regs, write, pc, limit=COP_LIMIT, mask=COP_PC_MASK):
        def lc(r):
            return ((regs[r >> 1] << 16) | regs[(r + 2) >> 1]) & mask
        self.pc = pc & mask
        n = 0
        self.why = COP_LIMITED
        while n < limit:
            if (self.pc >> 16) & 0xFF in (0xBF, 0xDF):
                self.why = COP_IOBANK
                break
            w1, w2 = mem.r16(self.pc), mem.r16(self.pc + 2)
            here, self.pc = self.pc, (self.pc + 4) & 0xFFFFFF
            n += 1
            if not w1 & 1:                      # MOVE
                reg = w1 & 0x1FE
                floor = 0x040 if regs[0x02E >> 1] & 2 else 0x080
                if reg < floor:                 # the danger rule: stop on it
                    self.why, self.pc = COP_DANGER, here
                    break
                if reg in (0x088, 0x08A):
                    self.pc = lc(0x080 if reg == 0x088 else 0x084)
                    continue
                write(reg, w2)
            elif (w1, w2) == (0xFFFF, 0xFFFE):  # the end: stay on it
                self.why, self.pc = COP_END, here
                break
            elif w2 & 1:                        # SKIP
                if self.strict:                 # (as Copper.run)
                    raise CopperSkipError(f"copper SKIP ${w1:04X},${w2:04X} at ${here:06X}")
                self.skips += 1
        return n


class DisplayState:
    """The disp_st block: every display-register write lands in its field;
    ``gen`` (u8) is bumped once per write that changes a field's value.
    DMACON follows the SET/CLR rule (bits 0-14); BPLxPTH keeps its low
    byte (bits 23-16); every other field is the word as written.  ``valid``
    is COP_VALID (the state after cop_reset)."""

    def __init__(self, gen: int = 0):
        self.gen = gen & 0xFF
        self.f = bytearray(DISP_SZ)
        self.f[DISP_VALID] = COP_VALID

    def reset(self) -> None:
        """cop_reset: fields := 0, valid, gen bumped."""
        self.f = bytearray(DISP_SZ)
        self.f[DISP_VALID] = COP_VALID
        self._bump()

    def _bump(self) -> None:
        self.gen = (self.gen + 1) & 0xFF

    def _put(self, off: int, data: bytes) -> None:
        if self.f[off:off + len(data)] != data:
            self.f[off:off + len(data)] = data
            self._bump()

    @staticmethod
    def field(reg: int):
        """(disp offset, size) of display register *reg*, or None."""
        if 0x08E <= reg <= 0x094:
            return DISP_DIW + reg - 0x08E, 2
        if reg == 0x096:
            return DISP_DMACON, 2
        if 0x0E0 <= reg < 0x0F8:
            i, lo = divmod(reg - 0x0E0, 4)
            return (DISP_BPLPT + 3 * i, 2) if lo else (DISP_BPLPT + 3 * i + 2, 1)
        if 0x100 <= reg <= 0x104:
            return DISP_BPLCON + reg - 0x100, 2
        if reg in (0x108, 0x10A):
            return DISP_BPLMOD + reg - 0x108, 2
        if 0x180 <= reg < 0x1C0:
            return DISP_COLOR + reg - 0x180, 2
        return None

    def write(self, reg: int, val: int) -> None:
        fs = self.field(reg & 0x1FE)
        if fs is None:
            return
        off, n = fs
        if off == DISP_DMACON:
            val = setclr(self.dmacon, val)
        self._put(off, (val & 0xFFFF).to_bytes(2, "little")[:n])

    @property
    def dmacon(self) -> int:
        return int.from_bytes(self.f[DISP_DMACON:DISP_DMACON + 2], "little")

    def block(self) -> bytes:
        return bytes([self.gen]) + bytes(self.f[1:])


class DisplayChipset(Chipset):
    """Chipset + DisplayState + the S3.5 copper triggers of cop.s ("copper
    DMA on" = DMACON DMAEN and COPEN).  A CPU COPJMP1/COPJMP2 strobe loads
    the copper PC from COP1LC/COP2LC and, with copper DMA on, runs it at
    once; a CPU DMACON write that turns copper DMA on (off before, on after)
    runs it from the loaded PC (none loaded since reset: no run);
    ``frame()`` restarts it from COP1LC with
    copper DMA on (Chipset.run_copper's gate).  The copper's own writes
    start nothing.  ``runs`` counts copper runs, ``n`` is the last run's."""

    def __init__(self, mem, gen: int = 0):
        super().__init__(mem)
        self.cop = CopperAt()
        self.disp = DisplayState(gen)
        self.runs = 0
        self.n = 0
        self.loaded = False                      # a PC loaded since reset

    def custom_write(self, off, val):
        super().custom_write(off, val)
        self.disp.write(off & 0x1FE, val)

    def copper_on(self) -> bool:
        return self.paula.dmacon & 0x0280 == 0x0280

    def cpu_write(self, off, val):
        """A CPU word write (with the copper triggers)."""
        r = off & 0x1FE
        was = self.copper_on()
        self.custom_write(r, val)
        if r in (0x088, 0x08A):
            lc = 0x080 if r == 0x088 else 0x084
            self.cop.pc = ((self.regs[lc >> 1] << 16) | self.regs[(lc + 2) >> 1]) & COP_PC_MASK
            self.loaded = True
            if self.copper_on():
                self._run()
        elif r == 0x096 and not was and self.copper_on() and self.loaded:
            self._run()

    def _run(self) -> int:
        self.runs += 1
        self.n = self.cop.run_at(self.mem, self.regs, self.copper_write, self.cop.pc)
        return self.n

    def frame(self) -> int:
        if not self.copper_on():
            return 0
        self.cop.pc = ((self.regs[0x080 >> 1] << 16) | self.regs[0x082 >> 1]) & COP_PC_MASK
        self.loaded = True
        return self._run()
