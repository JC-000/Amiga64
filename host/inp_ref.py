"""W13-I reference: the C64 joystick (port 2) as the Amiga port-0 mouse.

The model of ``runtime/ops/inp.s`` ``inp_frame``, frame by frame
(docs/CONTRACTS.md "Input (joystick -> mouse)").  Public Amiga facts used:
JOY0DAT ($DFF00A) = Y counter in bits 15-8, X counter in bits 7-0, each
8-bit and wrapping; the left button of port 0 is CIA-A PRA bit 6 (/FIR0),
active low; the right button is POTGOR/POTINP ($DFF016) bit 10 (DATLY),
active low (Amiga Hardware Reference Manual, "Controllers"/"Mouse").

``Inp.frame(joy, key)`` takes one hooked frame's input (joy: bits 0-4 =
up/down/left/right/fire, 1 = pressed; key: F1 = $10, C= = $20, 1 = down)
and returns the observable state.  ``scan()`` models the C64 side: the
keyboard matrix and port 2 sharing CIA1 port A, read the way inp.s reads
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CAP = 32                  # step cap (inp.s INP_CAP; W14-M: 16 -> 32)
NMAX = CAP - 1            # n saturates here (inp.s INP_NMAX)
F1, CBM = 0x10, 0x20      # inp_key bits (the keys' matrix rows)
UP, DOWN, LEFT, RIGHT, FIRE = 1, 2, 4, 8, 16
PIN_RESET = 0xEB          # CIA-A PRA input pins after cia_reset


def m1351_delta(old: int, new: int) -> int:
    """The 1351 proportional protocol: a pot carries the position mod 64
    in bits 1-6 (bit 0 is noise); the move since the last read is the
    difference mod 128 as a signed 7-bit value, halved: -32..31 counts
    (a move of more than 31 counts between two reads aliases)."""
    d = ((new & 0x7E) - (old & 0x7E)) & 0x7F
    if d & 0x40:
        d -= 0x80
    return d >> 1 if d >= 0 else -((-d + 1) >> 1)


def step(n: int) -> int:
    """Counter units moved in a frame after n frames of the same hold."""
    return min(1 + n, CAP)


@dataclass
class Inp:
    y: int = 0            # JOY0DAT bits 15-8
    x: int = 0            # JOY0DAT bits 7-0
    n: int = 0            # inp_n
    kp: int = 0           # inp_kp
    view: int = 0         # inp_view
    pin: int = PIN_RESET  # CIA-A pins (cia_st+CIA_PIN)
    key: int = 0          # inp_key after the frame
    mb: int = 0           # inp_mb: the mouse port's lines (bit 4 left, 0 right)
    mold: list = None     # inp_mold (POTY, POTX), bits 1-6
    mok: bool = False     # inp_mok
    macc: list = field(default_factory=lambda: [0, 0])   # inp_macc (Y, X), s8

    def frame(self, joy: int, key: int, mb: int = 0) -> dict:
        self.macc = [0, 0]                    # inp.s module: a new frame (clamp)
        self.key, self.mb = key, mb
        if key & ~self.kp & F1:
            self.view |= 1
        self.kp = key
        fire = bool((joy | mb) & FIRE)
        self.pin = (self.pin & 0x3F) | 0x80 | (0 if fire else 0x40)
        d = joy & 0x0F
        if not d:
            self.n = 0
        s = step(self.n)
        if self.n < NMAX:
            self.n += 1
        if d & UP:
            self.y = (self.y - s) & 0xFF
        if d & DOWN:
            self.y = (self.y + s) & 0xFF
        if d & LEFT:
            self.x = (self.x - s) & 0xFF
        if d & RIGHT:
            self.x = (self.x + s) & 0xFF
        return self.obs()

    @property
    def joy0dat(self) -> int:
        return self.y << 8 | self.x

    @property
    def potinp(self) -> int:
        return 0xFB00 if self.key & CBM or self.mb & UP else 0xFF00

    def msamp(self, poty: int, potx: int) -> dict:
        """One 1351 sample (inp.s inp_msamp): pots -> JOY0DAT."""
        new = [poty & 0x7E, potx & 0x7E]
        if self.mok:
            dy, dx = (m1351_delta(o, n) for o, n in zip(self.mold, new))
            for i, d in ((0, -dy), (1, dx)):  # AROS reads s8 per frame: clamp
                acc = max(-127, min(127, self.macc[i] + d))
                step, self.macc[i] = acc - self.macc[i], acc
                if i == 0:
                    self.y = (self.y + step) & 0xFF
                else:
                    self.x = (self.x + step) & 0xFF
        self.mold, self.mok = new, True
        return self.obs()

    def obs(self) -> dict:
        return {"joy0dat": self.joy0dat, "pra": self.pin, "potinp": self.potinp,
                "view": self.view}


# --------------------------------------------------------------------------
# The C64 side: CIA1 port A (columns, port 2) and port B (rows, port 1)
# --------------------------------------------------------------------------

@dataclass
class Matrix:
    """Ideal switches: *keys* = {(column, row)} held; *joy2*/*joy1* =
    active-high stick bits (port 2 pulls PA0-4, port 1 PB0-4)."""
    keys: set = field(default_factory=set)
    joy2: int = 0
    joy1: int = 0

    def read(self, pa_out: int | None, ) -> tuple[int, int]:
        """(PA pins, PB pins) with port A driving *pa_out* (None = inputs)
        and port B as inputs.  A pressed key joins its column and row line;
        a line is low if anything pulls it low (wired AND, solved to a fixed
        point)."""
        pa = (0xFF if pa_out is None else pa_out) & ~(self.joy2 & 0x1F) & 0xFF
        pb = 0xFF & ~(self.joy1 & 0x1F) & 0xFF
        # a driven-high column line is still pulled low by a switch to ground
        # (the joystick); a key shorts a column and a row: propagate lows
        while True:
            npa, npb = pa, pb
            for c, r in self.keys:
                if not (pa >> c) & 1 or not (pb >> r) & 1:
                    npa &= ~(1 << c)
                    npb &= ~(1 << r)
            if (npa, npb) == (pa, pb):
                return pa, pb
            pa, pb = npa, npb


def scan(m: Matrix, prev_key: int, stuck: int = 0) -> tuple[int, int]:
    """inp.s's real-source scan: (inp_joy, inp_key); *stuck* = inp_stuck
    (stick bits read as released: a switch stuck closed)."""
    pa, _ = m.read(None)                      # 1. nothing driven
    joy = ~(pa | stuck) & 0x1F
    _, idle = m.read(0xFF)                    # 2. no column selected
    _, b0 = m.read(0xFE)                      # 3. column 0 (F1 = row 4)
    _, b7 = m.read(0x7F)                      #    column 7 (C= = row 5)
    u = (b0 | (~F1 & 0xFF)) & (b7 | (~CBM & 0xFF))
    new = ~u & 0xFF
    key = ((new ^ prev_key) & idle) ^ prev_key
    return joy, key


def scan_mouse_lines(m: Matrix, mport: int = 0, resting: bool = False) -> int:
    """W14-M: the mouse port's lines (1 = closed: bit 4 left, bit 0 right)
    as inp.s step 1 reads them: DDRA = DDRB = $00, nothing driven.  With
    *resting* the read is taken (wrongly) with the pot select still driving
    PA7-6 (port 1 select: PA7 low = keyboard column 7 selected)."""
    pa_out = (0x7F if mport == 0 else 0xBF) if resting else None
    pa, pb = m.read(pa_out)
    lines = pb if mport == 0 else pa
    return ~lines & 0x1F


@dataclass
class Queue:
    """W14-M M2 (tests/aros/inpm.s): the LEFT button's edge queue.
    look(lines) is the module's step 1 (every sample and every frame);
    frame(lines) then delivers one left transition and returns the
    delivered lines (left from the queue, right = the lines now)."""
    last: int = 0
    q: int = 0
    md: int = 0

    def look(self, lines: int) -> None:
        lines &= 0x11
        if lines & ~self.last & FIRE:
            self.q += 1
        self.last = lines

    def frame(self, lines: int) -> int:
        self.look(lines)
        if self.md:
            if self.q or not lines & FIRE:
                self.md = 0
        elif self.q:
            self.q -= 1
            self.md = FIRE
        return self.md | (lines & UP)
