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

CAP = 16                  # step cap (inp.s INP_CAP)
NMAX = 2 * CAP - 2        # n saturates here (inp.s INP_NMAX)
F1, CBM = 0x10, 0x20      # inp_key bits (the keys' matrix rows)
UP, DOWN, LEFT, RIGHT, FIRE = 1, 2, 4, 8, 16
PIN_RESET = 0xEB          # CIA-A PRA input pins after cia_reset


def step(n: int) -> int:
    """Counter units moved in a frame after n frames of the same hold."""
    return min(1 + n // 2, CAP)


@dataclass
class Inp:
    y: int = 0            # JOY0DAT bits 15-8
    x: int = 0            # JOY0DAT bits 7-0
    n: int = 0            # inp_n
    kp: int = 0           # inp_kp
    view: int = 0         # inp_view
    pin: int = PIN_RESET  # CIA-A pins (cia_st+CIA_PIN)
    key: int = 0          # inp_key after the frame

    def frame(self, joy: int, key: int) -> dict:
        self.key = key
        if key & ~self.kp & F1:
            self.view |= 1
        self.kp = key
        fire = bool(joy & FIRE)
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
        return 0xFB00 if self.key & CBM else 0xFF00

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


def scan(m: Matrix, prev_key: int) -> tuple[int, int]:
    """inp.s's real-source scan: (inp_joy, inp_key)."""
    pa, _ = m.read(None)                      # 1. nothing driven
    joy = ~pa & 0x1F
    _, idle = m.read(0xFF)                    # 2. no column selected
    _, b0 = m.read(0xFE)                      # 3. column 0 (F1 = row 4)
    _, b7 = m.read(0x7F)                      #    column 7 (C= = row 5)
    u = (b0 | (~F1 & 0xFF)) & (b7 | (~CBM & 0xFF))
    new = ~u & 0xFF
    key = ((new ^ prev_key) & idle) ^ prev_key
    return joy, key
