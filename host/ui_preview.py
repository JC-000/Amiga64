"""W15-U: the MOD player's UI as the C64 shows it (docs/UI_READABLE.md).

Four things live here:

* the font (``fixtures/mod/ui/font8.txt``) and a host model of the 68k UI
  kit's drawing (``Screen``): 3-plane hires, 640x200, planes interleaved
  per line, every design pixel two hires pixels wide (the 320-px grid);
* ``player_screen()``: the player screen drawn from a ``mod_view`` (I-1);
* the conversion through ``host/c2v_ref.py`` (``disp_convert`` for the fit
  view, ``disp_convert_view`` for the 1:1 view) and ``vic_image()``, which
  renders the C64's bitmap, matrix and colour RAM exactly as the VIC shows
  them (Pepto's palette, border included);
* the readability checks R1-R7 (``check_all``) that docs/UI_READABLE.md
  defines, and the mutants that must fail them.

CLI: ``python3 -m host.ui_preview --out DIR`` writes the previews.
"""

from __future__ import annotations

import argparse
import os
import struct

from host import c2v_ref as R

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH = os.path.join(ROOT, "fixtures", "mod", "ui", "font8.txt")

# --- the display (docs/UI_READABLE.md "The display") -----------------------
COLS, ROWS = 40, 25             # 8x8 design cells
W, H = 320, 200                 # the design grid (C64 hires pixels)
PLANES = 3
ROWB = 80                       # bytes per hires plane row
STRIDE = PLANES * ROWB          # interleaved: plane p of line y at y*240 + p*80
UI_BASE = 0x070000              # proposed (U0): copper list, scratch, planes
UI_COP = UI_BASE                # copper list
UI_KICK = UI_BASE + 0x100       # the 1-word blit's target (pixel epoch)
UI_PLANES = UI_BASE + 0x1000    # 48,000 bytes, ends $07CB80
BPLCON0 = 0xB200                # hires, BPU 3, COLOR
DIWSTRT, DIWSTOP = 0x2C81, 0xF4C1   # 200 lines
DDFSTRT, DDFSTOP = 0x003C, 0x00D4   # 80 bytes per hires row

# Pens: OCS colour, the VIC colour it must become, its role.
PEN_BG, PEN_VAL, PEN_LBL, PEN_HEAD, PEN_LO, PEN_MID, PEN_HI, PEN_BOX = range(8)
PALETTE = (0x000, 0xFFF, 0x999, 0x7AA, 0x584, 0xBC7, 0x965, 0x66B)
INTENDED = (0, 1, 15, 3, 5, 7, 10, 14)       # black white lgrey cyan green yellow lred lblue
PEN_NAME = ("bg", "value", "label", "heading", "vu-low", "vu-mid", "vu-high", "box")

TEXT_MIN = 4.5                  # R4: WCAG AA contrast, text
SHAPE_MIN = 3.0                 # R4: WCAG non-text contrast (bars, boxes)
DELTA_E_MIN = 15.0              # R4: CIE76 between any two pens in use
DISTINCT_MIN = 4                # R3/F3: pixels between any two glyphs
ISOLATED_MAX = 2                # F2: pixels without a horizontal neighbour


# ---------------------------------------------------------------------------
# The font
# ---------------------------------------------------------------------------

def load_font(path: str = FONT_PATH) -> dict[int, list[int]]:
    """code -> 8 row bytes (bit 7 = leftmost pixel)."""
    font: dict[int, list[int]] = {}
    code, rows = None, []
    for ln in open(path, encoding="ascii"):
        ln = ln.rstrip("\n")
        if not ln or ln.startswith("# ") or ln == "#":
            continue
        if ln.startswith("= "):
            code, rows = int(ln.split()[1]), []
            continue
        assert len(ln) == 8 and set(ln) <= {".", "#"}, ln
        rows.append(sum(1 << (7 - i) for i, c in enumerate(ln) if c == "#"))
        if len(rows) == 8:
            font[code] = rows
    return font


GLYPH_PLAY, GLYPH_STOP, GLYPH_PAUSE = 0x80, 0x81, 0x82


def glyph_code(ch: int | str) -> int:
    """The font slot the kit shows for a byte (any value is legal):
    controls are spaces, 'a'-'z' fold to 'A'-'Z', anything else unknown
    is '?'."""
    c = ord(ch) if isinstance(ch, str) else ch
    if c < 32:
        return 32
    if 32 <= c <= 95 or c in (GLYPH_PLAY, GLYPH_STOP, GLYPH_PAUSE):
        return c
    if 97 <= c <= 122:
        return c - 32
    return ord("?")


def double(b: int) -> int:
    """A design row byte -> the 16 hires pixels (each pixel twice)."""
    return sum(3 << (2 * (7 - i)) for i in range(8) if b >> (7 - i) & 1)


# ---------------------------------------------------------------------------
# The kit model (mirrors fixtures/mod/ui/: what the 68k code writes)
# ---------------------------------------------------------------------------

class Screen:
    """3 interleaved hires planes plus the layout's static pen map.

    Every cell has one pen (``pen[row][col]``), fixed by the layout: the
    kit writes only the planes whose bit is set in that pen, so changing a
    cell's contents never needs the other planes (docs/UI_READABLE.md
    "Drawing").  ``kind`` records what a cell is for R4: "text" (pen on
    bg), "inv" (bg on pen: inverse text), "shape" (bars, boxes)."""

    def __init__(self, font: dict | None = None, palette=PALETTE,
                 widen: int = 2):
        self.font = font or load_font()
        self.palette = list(palette)
        self.widen = widen              # mutant hook: 1 = native hires glyphs
        self.mem = bytearray(H * STRIDE)
        self.pen = [[0] * COLS for _ in range(ROWS)]
        self.kind = [[None] * COLS for _ in range(ROWS)]
        self.lines = H                  # DIW height
        self.bplcon0 = BPLCON0
        self.st_override = None         # a guest's display state (run_modui M2)

    # -- low level
    def _word(self, y: int, cx: int, plane: int, v: int) -> None:
        o = y * STRIDE + plane * ROWB + 2 * cx
        self.mem[o], self.mem[o + 1] = v >> 8 & 255, v & 255

    def cell(self, col: int, row: int, rows8: list[int], pen: int,
             kind: str, inverse: bool = False) -> None:
        """One 8x8 design cell: 8 doubled rows into the pen's planes."""
        if not (0 <= col < COLS and 0 <= row < ROWS):
            return                      # clamp: off-screen draws nothing
        self.pen[row][col], self.kind[row][col] = pen, kind
        for y in range(8):
            b = rows8[y] ^ (0xFF if inverse else 0)
            v = double(b) if self.widen == 2 else b << 8
            for p in range(PLANES):
                self._word(row * 8 + y, col, p, v if pen >> p & 1 else 0)

    # -- the kit's entry points (U1)
    def text(self, col: int, row: int, s: str | bytes, pen: int,
             inverse: bool = False) -> None:
        if self.widen == 1:             # mutant: glyphs at native hires width
            self._thin_text(col, row, s, pen)
            return
        for i, ch in enumerate(s):
            self.cell(col + i, row, self.font[glyph_code(ch)], pen,
                      "inv" if inverse else "text", inverse)

    def _thin_text(self, col: int, row: int, s, pen: int) -> None:
        """Mutant 'thin': Topaz-style, one design pixel = one hires pixel,
        so two glyphs share a cell's 16 hires pixels (80 columns)."""
        chars = [glyph_code(c) for c in s]
        for k in range(0, len(chars), 2):
            pair = chars[k:k + 2] + [32]
            c = col + k // 2
            self.pen[row][c], self.kind[row][c] = pen, "text"
            for y in range(8):
                v = self.font[pair[0]][y] << 8 | self.font[pair[1]][y]
                for p in range(PLANES):
                    self._word(row * 8 + y, c, p, v if pen >> p & 1 else 0)

    def fill(self, col: int, row: int, n: int, pen: int, kind: str = "shape") -> None:
        for i in range(n):
            self.cell(col + i, row, [0xFF] * 8, pen, kind)

    def bar(self, col: int, row: int, level: int, pens: list[int],
            offset: int = 0) -> None:
        """A VU bar: len(pens) cells, 2 design pixels per unit of level
        (0-64 -> 0-128 px at 16 cells), lines 1-6 of the row.  Cell i has
        pen pens[i]; offset (mutant hook) shifts the colour segments."""
        n = len(pens)
        level = max(0, min(64, level))
        px = level * 8 * n // 64
        for i in range(n):
            on = max(0, min(8, px - 8 * i))
            b = (0xFF << (8 - on)) & 0xFF
            pen = pens[min(n - 1, max(0, (i * 8 + offset) // 8))]
            rows8 = [0, b, b, b, b, b, b, 0]
            if offset:                  # mutant: two pens share a cell
                rows8 = [0] + [b & 0xF0] * 3 + [b & 0x0F] * 3 + [0]
                self.cell(col + i, row, rows8, pen, "shape")
                for y in (4, 5, 6):
                    nxt = pens[min(n - 1, i + 1)]
                    for p in range(PLANES):
                        o = (row * 8 + y) * STRIDE + p * ROWB + 2 * (col + i)
                        v = double(b & 0x0F)
                        self.mem[o] = (v >> 8) if nxt >> p & 1 else 0
                        self.mem[o + 1] = (v & 255) if nxt >> p & 1 else 0
                continue
            self.cell(col + i, row, rows8, pen, "shape")

    # -- the picture
    def planes(self) -> list[bytes]:
        """Plane p as 200 contiguous 80-byte rows (for checks)."""
        return [bytes(b for y in range(H) for b in
                      self.mem[y * STRIDE + p * ROWB:y * STRIDE + p * ROWB + ROWB])
                for p in range(PLANES)]

    def design(self) -> list[list[int]]:
        """The Amiga picture as pens, 640 x lines (what an Amiga shows)."""
        pl = self.planes()
        out = []
        for y in range(H):
            row = []
            for x in range(640):
                o, m = y * ROWB + x // 8, 0x80 >> (x & 7)
                row.append(sum(1 << p for p in range(PLANES) if pl[p][o] & m))
            out.append(row)
        return out

    def disp_st(self) -> dict:
        """The display state the UI's copper list + ui_init leave (C1), or
        the guest's own (st_override)."""
        if self.st_override is not None:
            return self.st_override
        vstop = (DIWSTRT >> 8) + self.lines
        diwstop = ((vstop & 0xFF) << 8) | (DIWSTOP & 0xFF)
        return {"dmacon": 0x03C0, "bplcon0": self.bplcon0,
                "bplpt": [UI_PLANES + p * ROWB for p in range(PLANES)],
                "bpl1mod": STRIDE - ROWB, "bpl2mod": STRIDE - ROWB,
                "diwstrt": DIWSTRT, "diwstop": diwstop,
                "ddfstrt": DDFSTRT, "ddfstop": DDFSTOP,
                "color": list(self.palette) + [0] * (32 - len(self.palette))}

    def read(self, addr: int, n: int) -> bytes:
        """Guest memory as disp_convert reads it (planes; zero elsewhere)."""
        out = bytearray(n)
        for i in range(n):
            o = addr + i - UI_PLANES
            if 0 <= o < len(self.mem):
                out[i] = self.mem[o]
        return bytes(out)


# ---------------------------------------------------------------------------
# The player screen (U2; fixtures/mod/ui/ui.s draws the same pixels)
# ---------------------------------------------------------------------------

NOTES = ("C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-")
VU_PENS = [PEN_LO] * 10 + [PEN_MID] * 4 + [PEN_HI] * 2
VU_COL, CH_ROW0, RULE1, RULE2 = 22, 10, 9, 17
CADENCE = 12                    # ui.s UI_CADENCE: VERTBs per draw cycle (ruling 3)
TRAIL_EVERY = 2                 # ui.s TRAIL_EVERY: cycles per trail refresh
RING = 8                        # captured rows kept (>= every variant's lines)
# Trail variants (supervisor U0 ruling 6): lines, line pitch in cells,
# older lines dimmed (pen 2).  The newest row is the bottom line (row 24).
TRAIL = {"A": (4, 2, False), "B": (7, 1, True)}
TRAIL_DEFAULT = "A"


def trail_rows(variant: str) -> list[int]:
    n, pitch, _ = TRAIL[variant]
    return [24 - pitch * (n - 1 - j) for j in range(n)]


def note_name(n: int) -> str:
    if not 0 <= n <= 35:
        return "---"
    return f"{NOTES[n % 12]}{n // 12 + 1}"


def fx_text(e: int, p: int, empty: str = "---") -> str:
    """An effect as shown: empty for 000; an effect above F shows '?'."""
    if (e, p) == (0, 0):
        return empty
    return ("?" if e > 15 else f"{e:X}") + f"{p:02X}"


def mod_view_sample(tick: int = 0) -> dict:
    """A plausible mod_view (I-1) for previews."""
    return {
        "magic": b"MVW1", "tick": 1234 + tick, "name": b"amiga64 demo tune",
        "songlen": 12, "pos": 3, "pattern": 7, "row": 21, "speed": 6,
        "bpm": 125, "flags": 1 | 4, "time": 50 * 83 + 17,
        "ch": [
            {"period": 428, "inst": 1, "vol": 64, "note": 12, "fx": 0xC, "param": 0x20, "vu": 58},
            {"period": 0, "inst": 0, "vol": 0, "note": 0xFF, "fx": 0, "param": 0, "vu": 0},
            {"period": 214, "inst": 5, "vol": 40, "note": 24, "fx": 0xA, "param": 0x04, "vu": 31},
            {"period": 856, "inst": 17, "vol": 25, "note": 3, "fx": 0x4, "param": 0x46, "vu": 12},
        ],
    }


def pack_mod_view(v: dict) -> bytes:
    """I-1: the 72-byte big-endian struct at guest $000400."""
    out = v.get("magic", b"MVW1")[:4].ljust(4, b"\0")
    out += struct.pack(">I", v["tick"] & 0xFFFFFFFF)
    out += v["name"][:20].ljust(20, b"\0")
    out += bytes([v["songlen"] & 255, v["pos"] & 255, v["pattern"] & 255,
                  v["row"] & 255, v["speed"] & 255, v["bpm"] & 255,
                  v["flags"] & 255, 0])
    for c in v["ch"]:
        out += struct.pack(">HBBBBBB", c["period"] & 0xFFFF, c["inst"] & 255,
                           c["vol"] & 255, c["note"] & 255, c["fx"] & 255,
                           c["param"] & 255, c["vu"] & 255)
    out += struct.pack(">I", v["time"] & 0xFFFFFFFF)
    assert len(out) == 72
    return out


class Ring:
    """ui.s's trail capture: every ui_frame call whose (position, row)
    differs from the last captured one stores the row number and each
    channel's (note, effect, param); the last RING kept, newest last."""

    def __init__(self):
        self.last = None
        self.rows: list = []

    def capture(self, v: dict) -> None:
        pr = (v["pos"] & 255, v["row"] & 255)
        if pr == self.last:
            return
        self.last = pr
        self.rows.append((v["row"] & 255, [(c["note"] & 255, c["fx"] & 255,
                                            c["param"] & 255) for c in v["ch"]]))
        self.rows = self.rows[-RING:]


def player_static(s: Screen, variant: str = TRAIL_DEFAULT) -> None:
    """ui_init's part: everything that never changes."""
    s.fill(0, 0, COLS, PEN_HEAD, "shape")
    s.text(1, 0, "AMIGA64 MOD PLAYER", PEN_HEAD, inverse=True)
    s.text(1, 2, "SONG", PEN_LBL)
    for col, lbl in ((1, "POS"), (12, "PAT"), (20, "ROW"), (29, "TIME")):
        s.text(col, 4, lbl, PEN_LBL)
    s.text(7, 4, "/", PEN_VAL)
    s.text(36, 4, ":", PEN_VAL)
    for col, lbl in ((1, "SPD"), (12, "BPM"), (20, "TMR")):
        s.text(col, 6, lbl, PEN_LBL)
    s.text(1, 8, "CH INS NOTE VOL FX", PEN_LBL)
    s.text(VU_COL, 8, "VU", PEN_LBL)
    for r in (RULE1, RULE2):             # rule lines: 2 lines of pen 7
        for c in range(COLS):
            s.cell(c, r, [0, 0, 0, 0xFF, 0xFF, 0, 0, 0], PEN_BOX, "shape")
    for k in range(4):
        s.text(1, CH_ROW0 + 2 * k, str(k + 1), PEN_HEAD)
    s.text(0, 24, chr(GLYPH_PLAY), PEN_HEAD)


def name_text(name: bytes) -> str:
    """The song name as shown: bytes up to the first NUL, 20 cells."""
    b = bytes(name[:20]).split(b"\0")[0]
    return "".join(chr(glyph_code(c)) for c in b).ljust(20)


def trail_line(entry) -> tuple[str, str]:
    """(row number, the rest from column 4) of one captured row."""
    if entry is None:
        return "  ", " " * 34
    row, cells = entry
    t = ""
    for k, (n, e, p) in enumerate(cells):
        t += f"{note_name(n)} {fx_text(e, p, '   ')}" + ("  " if k < 3 else "")
    return f"{min(row, 99):02d}", t


def player_frame(s: Screen, v: dict, ring: Ring | None = None,
                 variant: str = TRAIL_DEFAULT) -> None:
    """ui_frame's part once it has settled: the fields (clamped, any value
    is legal) and the trail from the ring."""
    flags = v["flags"]
    s.text(36, 0, chr(GLYPH_PLAY) if flags & 1 else chr(GLYPH_STOP), PEN_HEAD, inverse=True)
    s.text(6, 2, name_text(v["name"]), PEN_VAL)
    s.text(5, 4, f"{min(v['pos'], 99):02d}", PEN_VAL)
    s.text(8, 4, f"{min(v['songlen'], 99):02d}", PEN_VAL)
    s.text(16, 4, f"{min(v['pattern'], 99):02d}", PEN_VAL)
    s.text(24, 4, f"{min(v['row'], 99):02d}", PEN_VAL)
    t = min(v["time"] // 50, 99 * 60 + 59)
    s.text(34, 4, f"{t // 60:02d}", PEN_VAL)
    s.text(37, 4, f"{t % 60:02d}", PEN_VAL)
    s.text(5, 6, f"{min(v['speed'], 99):02d}", PEN_VAL)
    s.text(16, 6, f"{v['bpm']:3d}", PEN_VAL)
    s.text(24, 6, "CIA" if flags & 4 else "VBL", PEN_VAL)
    s.text(29, 6, "LOOP" if flags & 2 else "    ", PEN_VAL)
    for k, c in enumerate(v["ch"]):
        r = CH_ROW0 + 2 * k
        s.text(4, r, f"{min(c['inst'], 99):02d}" if c["inst"] else "--", PEN_VAL)
        s.text(8, r, note_name(c["note"]), PEN_VAL)
        s.text(13, r, f"{min(c['vol'], 64):02d}", PEN_VAL)
        s.text(17, r, fx_text(c["fx"], c["param"]), PEN_VAL)
        s.bar(VU_COL, r, c["vu"], VU_PENS)
    n, _, dim = TRAIL[variant]
    rows = ring.rows if ring else []
    for j, r in enumerate(trail_rows(variant)):
        age = n - 1 - j
        entry = rows[-1 - age] if age < len(rows) else None
        num, rest = trail_line(entry)
        s.text(1, r, num, PEN_LBL)
        s.text(4, r, rest, PEN_LBL if dim and age else PEN_VAL)


def sample_ring(cur_row: int = 21) -> Ring:
    """Seven played rows, the newest last (what the kit captures)."""
    import random
    rnd = random.Random(15)
    ring = Ring()
    v = mod_view_sample()
    for i in range(7):
        cells = []
        for k in range(4):
            if rnd.random() < 0.45:
                cells.append({"note": rnd.randrange(36),
                              "fx": rnd.choice([0, 0, 0xC, 0xA, 4, 0xE]),
                              "param": rnd.randrange(256)})
            else:
                cells.append({"note": 0xFF, "fx": 0, "param": 0})
        ring.capture({**v, "row": cur_row - 6 + i, "ch": cells})
    return ring


def sample_trail(cur_row: int = 21) -> Ring:      # previews and tests
    return sample_ring(cur_row)


# --- the 68k includes (generated; pinned by host/tests/test_ui_rules.py) ----

FONT_INC = os.path.join(ROOT, "fixtures", "mod", "ui", "font.i")
TAB_INC = os.path.join(ROOT, "fixtures", "mod", "ui", "ui_tab.i")


def font_inc(font: dict | None = None) -> str:
    """ui_font: 256 glyphs x 8 doubled rows (16 bytes), indexed by the byte
    itself, glyph_code's mapping baked in."""
    font = font or load_font()
    out = ["; fixtures/mod/ui/font.i - GENERATED by host/ui_preview.py font_inc()",
           "; from fixtures/mod/ui/font8.txt; do not edit.  256 x 16 bytes: the",
           "; glyph of byte b at ui_font + 16*b, each row doubled (320-px grid).",
           "ui_font:"]
    for b in range(256):
        g = font[glyph_code(b)]
        out.append("        dc.w    " + ",".join(f"${double(r):04X}" for r in g)
                   + f"    ; ${b:02X}")
    return "\n".join(out) + "\n"


def tab_inc() -> str:
    """dec2 (\"00\"..\"99\"), the 36 note names + \"---\", hex digits, and
    the VU cell patterns (on = 0..8 doubled pixels from the left)."""
    out = ["; fixtures/mod/ui/ui_tab.i - GENERATED by host/ui_preview.py tab_inc();",
           "; do not edit."]
    def db(label: str, data: bytes) -> None:
        out.append(f"{label}:")
        for i in range(0, len(data), 20):
            out.append("        dc.b    " + ",".join(f"${x:02X}" for x in data[i:i + 20]))
    db("ui_dec2", "".join(f"{i:02d}" for i in range(100)).encode())
    db("ui_notes", "".join(note_name(n) for n in range(37)).encode())
    db("ui_hex", b"0123456789ABCDEF?")           # 16: an effect above F
    out.append("        even")
    out.append("ui_vupat:")
    out.append("        dc.w    " + ",".join(
        f"${double((0xFF << (8 - on)) & 0xFF):04X}" for on in range(9)))
    return "\n".join(out) + "\n"


def write_includes() -> None:
    open(FONT_INC, "w").write(font_inc())
    open(TAB_INC, "w").write(tab_inc())



def font_sample(s: Screen) -> None:
    """Every glyph, then sample strings in each text pen and inverse."""
    s.fill(0, 0, COLS, PEN_HEAD)
    s.text(1, 0, "W15-U FONT SAMPLE 8X8 / 320 GRID", PEN_HEAD, inverse=True)
    codes = list(range(33, 96)) + [GLYPH_PLAY, GLYPH_STOP, GLYPH_PAUSE]
    for i, c in enumerate(codes):
        s.text(1 + 2 * (i % 19), 2 + 2 * (i // 19), chr(c), PEN_VAL)
    lines = [
        ("THE QUICK BROWN FOX JUMPS", PEN_VAL, False),
        ("OVER THE LAZY DOG 0123456789", PEN_LBL, False),
        ("O0 Il1 B8 S5 Z2 G6 DO QO MN UV", PEN_HEAD, False),
        ("C-2 01 C20  D#3 1F A0F  ---", PEN_VAL, False),
        ("POS 03/12 PAT 07 ROW 21  01:23", PEN_LBL, False),
        (" INVERSE ON YELLOW  ", PEN_MID, True),
        (" INVERSE ON CYAN    ", PEN_HEAD, True),
    ]
    for i, (t, pen, inv) in enumerate(lines):
        if inv:
            s.fill(1, 11 + 2 * i, len(t), pen)
        s.text(1, 11 + 2 * i, t, pen, inverse=inv)
    s.text(1, 24, "VU", PEN_LBL)
    s.bar(4, 24, 50, VU_PENS)


# ---------------------------------------------------------------------------
# Conversion and the VIC picture
# ---------------------------------------------------------------------------

LUT = None


def lut() -> bytes:
    global LUT
    if LUT is None:
        LUT = R.build_lut()
    return LUT


def convert(s: Screen, view: int = 0, px: int = R.PX_CENTRE) -> dict:
    """What disp.s leaves for the UI's display state (c2v_ref's model)."""
    return R.disp_convert_view(s.disp_st(), s.read, lut(), view=view, px=px)


def vic_pixels(out: dict) -> list[list[int]]:
    """320x200 VIC colour indices, exactly as the VIC reads the bitmap,
    matrix and colour RAM (hires: 1 -> matrix high nibble, 0 -> low
    nibble; multicolour: %00 $D021, %01 high, %10 low, %11 colour RAM)."""
    if out["decide"]["mode"] == R.MODE_BLANK:
        return [[out["border"]] * W for _ in range(H)]
    hires = out["vic"][0xD016] & 0x10 == 0
    sc, cr, bmp = out["screen"], out.get("colram"), out["bitmap"]
    bgc = out["vic"][0xD021] & 15
    img = [[0] * W for _ in range(H)]
    for c in range(1000):
        r, cx = divmod(c, 40)
        for y in range(8):
            b = bmp[r * 320 + cx * 8 + y]
            for k in range(8):
                if hires:
                    col = sc[c] >> 4 if b >> (7 - k) & 1 else sc[c] & 15
                else:
                    v = b >> (6 - 2 * (k // 2)) & 3
                    col = [bgc, sc[c] >> 4, sc[c] & 15, (cr or bytes(1000))[c] & 15][v]
                img[r * 8 + y][cx * 8 + k] = col
    return img


def vic_image(out: dict, scale: int = 3, border: bool = True):
    """The VIC picture as a PIL image (border = $D020, 32/36 px)."""
    from PIL import Image
    pix = vic_pixels(out)
    bx, by = (32, 36) if border else (0, 0)
    bcol = out["vic"][0xD020] & 15 if "vic" in out else out["border"]
    im = Image.new("RGB", (W + 2 * bx, H + 2 * by), R.VIC_RGB[bcol])
    px = im.load()
    for y in range(H):
        for x in range(W):
            px[bx + x, by + y] = R.VIC_RGB[pix[y][x]]
    return im.resize((im.width * scale, im.height * scale), Image.NEAREST)


def amiga_image(s: Screen, scale: int = 3):
    """What an Amiga shows (hires: a pixel is half a C64 pixel wide)."""
    from PIL import Image
    d = s.design()
    im = Image.new("RGB", (640, H))
    px = im.load()
    for y in range(H):
        for x in range(640):
            px[x, y] = R.ocs_rgb(s.palette[d[y][x]] if d[y][x] < len(s.palette) else 0)
    return im.resize((W * scale, H * scale), Image.NEAREST)


# --- The alternative L2 (lores 2-plane, the fit view's mc2 path) ------------

L2_PALETTE = (0x000, 0xFFF, 0x999, 0x7AA)


def lores_sample(lines: list[tuple[str, int]]) -> dict:
    """L2, for comparison only: lores 2 planes, one design pixel = one
    multicolour pixel (2 lores pixels), the same 8x8 font at 20 columns,
    4 colours for the whole screen.  Returns disp_convert's output."""
    font = load_font()
    mem = bytearray(2 * 40 * H)                 # plane 0, then plane 1

    def put(col: int, row: int, text: str, pen: int) -> None:
        for i, ch in enumerate(text):
            g = font[glyph_code(ch)]
            for y in range(8):
                v = double(g[y])
                for p in range(2):
                    if pen >> p & 1:
                        o = p * 40 * H + (row * 8 + y) * 40 + 2 * (col + i)
                        mem[o], mem[o + 1] = v >> 8, v & 255
    for r, (t, pen) in enumerate(lines):
        put(0, r, t[:20], pen)
    st = {"dmacon": 0x0380, "bplcon0": 0x2200, "bplpt": [UI_PLANES, UI_PLANES + 40 * H],
          "bpl1mod": 0, "bpl2mod": 0, "diwstrt": DIWSTRT, "diwstop": DIWSTOP,
          "ddfstrt": 0x0038, "ddfstop": 0x00D0,
          "color": list(L2_PALETTE) + [0] * 28}

    def read(a: int, n: int) -> bytes:
        o = a - UI_PLANES
        return bytes(mem[o:o + n]) if 0 <= o <= len(mem) - n else bytes(n)
    return R.disp_convert(st, read, lut())


L2_LINES = [("AMIGA64 MOD PLAYER", 3), ("", 0), ("SONG", 2), ("AMIGA64 DEMO TUNE", 1),
            ("", 0), ("POS 03/12  ROW 21", 1), ("PAT 07  SPD 06", 1),
            ("BPM 125  01:23", 1), ("", 0), ("1 01 C-2 64 C20", 1),
            ("2 -- --- 00 ---", 1), ("3 05 C-3 40 A04", 1), ("4 17 D#1 25 446", 1),
            ("", 0), ("THE QUICK BROWN FOX", 2), ("O0 IL1 B8 S5 Z2 G6", 3)]


# ---------------------------------------------------------------------------
# Readability checks (docs/UI_READABLE.md R1-R7).  Each returns failures.
# ---------------------------------------------------------------------------

def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rel_luma(rgb) -> float:
    r, g, b = (_lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: int, b: int) -> float:
    """WCAG 2 contrast ratio of two VIC colours (Pepto sRGB)."""
    la, lb = sorted((rel_luma(R.VIC_RGB[a]), rel_luma(R.VIC_RGB[b])), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def delta_e(a: int, b: int) -> float:
    return sum((p - q) ** 2 for p, q in zip(R._lab(R.VIC_RGB[a]), R._lab(R.VIC_RGB[b]))) ** 0.5


def check_mode(s: Screen) -> list[str]:
    """R1: hires, 1-3 planes, no HAM/DPF; R6: DIW <= 200 lines (nothing
    cropped by the fit view)."""
    d = R.disp_decide(s.disp_st())
    f = []
    if d["mode"] != R.MODE_HIRES:
        f.append(f"R1: disp mode {d['mode']} (why {d['why']}), not hires")
    vstop = (s.disp_st()["diwstop"] >> 8) | ((~s.disp_st()["diwstop"] >> 7) & 0x100)
    lines = vstop - (DIWSTRT >> 8)
    if lines > R.C2V_LINES:
        f.append(f"R6: DIW is {lines} lines; the fit view crops lines 200-{lines - 1}")
    return f


def check_grid(s: Screen) -> list[str]:
    """R2: every hires pixel pair is equal (the 320-px design grid), so the
    fit view's pair-OR loses nothing."""
    bad = 0
    for p, pl in enumerate(s.planes()):
        for b in pl:
            if ((b >> 1) ^ b) & 0x55:
                bad += 1
    return [f"R2: {bad} plane bytes with unequal pixel pairs"] if bad else []


def check_cells(s: Screen) -> list[str]:
    """R5: every 8x8 cell (16 hires px x 8 lines) has at most one pen
    besides pen 0."""
    pl = s.planes()
    f = []
    for r in range(ROWS):
        for cx in range(COLS):
            pens = set()
            for y in range(r * 8, r * 8 + 8):
                for h in range(2):
                    o = y * ROWB + 2 * cx + h
                    for k in range(8):
                        m = 0x80 >> k
                        pen = sum(1 << p for p in range(PLANES) if pl[p][o] & m)
                        if pen:
                            pens.add(pen)
            if len(pens) > 1:
                f.append(f"R5: cell ({cx},{r}) has pens {sorted(pens)}")
    return f[:8] + ([f"R5: ... {len(f) - 8} more"] if len(f) > 8 else [])


def check_colours(s: Screen, out: dict | None = None) -> list[str]:
    """R4: each pen in use keeps its intended VIC colour; text >= 4.5:1
    against its cell's other colour, shapes >= 3:1; pens in use pairwise
    >= DELTA_E_MIN apart."""
    out = out or convert(s)
    vic = out["vic_pens"]
    f = []
    pl = s.planes()

    def lit(r: int, c: int) -> bool:
        return any(pl[p][y * ROWB + 2 * c + h] for p in range(PLANES)
                   for y in range(r * 8, r * 8 + 8) for h in range(2))
    shown = [[bool(s.kind[r][c]) and lit(r, c) for c in range(COLS)] for r in range(ROWS)]
    used = {s.pen[r][c] for r in range(ROWS) for c in range(COLS) if shown[r][c]}
    used.add(0)
    for p in sorted(used):
        if vic[p] is None:
            f.append(f"R4: pen {p} ({PEN_NAME[p]}) in use but got no colour")
        elif s.palette[p] == PALETTE[p] and vic[p] != INTENDED[p]:
            f.append(f"R4: pen {p} ({PEN_NAME[p]}) shows VIC {vic[p]}, intended {INTENDED[p]}")
        elif lut()[s.palette[p] & 0xFFF] != vic[p]:
            f.append(f"R4: pen {p} ({PEN_NAME[p]}) lost its nearest colour "
                     f"(LUT {lut()[s.palette[p] & 0xFFF]}, shows {vic[p]})")
    for r in range(ROWS):
        for c in range(COLS):
            k = s.kind[r][c]
            if not shown[r][c] or vic[s.pen[r][c]] is None:
                continue
            p = s.pen[r][c]
            cr = contrast(vic[p], vic[0])
            need = SHAPE_MIN if k == "shape" else TEXT_MIN
            if cr < need:
                f.append(f"R4: cell ({c},{r}) {k} pen {p} ({PEN_NAME[p]}) on bg: "
                         f"contrast {cr:.2f} < {need}")
    us = sorted(used)
    for i, a in enumerate(us):
        for b in us[i + 1:]:
            if vic[a] is None or vic[b] is None:
                continue
            de = delta_e(vic[a], vic[b])
            if de < DELTA_E_MIN:
                f.append(f"R4: pens {a} and {b} too close after the LUT (dE {de:.1f})")
    seen, out_f = set(), []
    for x in f:                         # one line per distinct failure kind
        key = x.split(":")[0] + x.split(" pen ")[-1][:12] if "cell" in x else x
        if key not in seen:
            seen.add(key)
            out_f.append(x)
    return out_f


def check_exact(s: Screen, out: dict | None = None, view: int = 0,
                px: int = R.PX_CENTRE) -> list[str]:
    """R3/R7: the VIC picture equals the design, pixel for pixel, with each
    pen in its VIC colour (fit: design pixel x at C64 x; 1:1: hires pixel
    8*px + x at C64 x)."""
    out = out or convert(s, view, px)
    vic = out["vic_pens"]
    img = vic_pixels(out)
    d = s.design()
    bad, first = 0, None
    for y in range(H):
        for x in range(W):
            hx = 2 * x if not view else 8 * px + x
            want = vic[d[y][hx]]
            if img[y][x] != want:
                bad += 1
                first = first or (x, y, want, img[y][x])
    if bad:
        return [f"R3: {'1:1' if view else 'fit'} view differs from the design in "
                f"{bad} pixels (first at {first[:2]}: want {first[2]}, VIC {first[3]})"]
    return []


def converted_glyph(font: dict, code: int, widen: int = 2) -> list[int]:
    """A glyph as the fit view shows it (pair-OR of its hires pixels)."""
    rows = []
    for b in font[code]:
        v = double(b) if widen == 2 else b << 8
        rows.append(sum(1 << (7 - k) for k in range(8) if v >> (14 - 2 * k) & 3))
    return rows


def font_codes(font: dict, space: bool = False) -> list[int]:
    return [c for c in sorted(font) if space or c != 32]


def check_font(font: dict, widen: int = 2) -> list[str]:
    """F1-F3: glyph box, stroke width, and distinctness after conversion."""
    f = []
    for c in font_codes(font):
        g = font[c]
        if c not in (ord(","), ord("_"), ord(";")) and g[7]:
            f.append(f"F1: glyph {c} uses row 7 (the line gap)")
        iso = 0
        for b in g:
            for k in range(8):
                m = 0x80 >> k
                if b & m and not (b & (m << 1) & 0xFF or b & (m >> 1)):
                    iso += 1
        if iso > ISOLATED_MAX:
            f.append(f"F2: glyph {c} ({chr(c) if c < 127 else c}) has {iso} 1-px stroke pixels")
    conv = {c: converted_glyph(font, c, widen) for c in font_codes(font, space=True)}
    if widen == 1:                       # native hires: a glyph is half a cell
        conv = {c: [((font[c][y] >> 4) | (font[c][y] & 0x0F)) for y in range(8)]
                for c in font_codes(font, space=True)}
    for c in font_codes(font):
        if conv[c] != font[c]:
            f.append(f"F3: glyph {c} changes in the conversion")
            break
    cs = font_codes(font, space=True)    # M3: the space too (a minimum ink)
    worst = (99, None, None)
    for i, a in enumerate(cs):
        for b in cs[i + 1:]:
            dist = sum(bin(x ^ y).count("1") for x, y in zip(conv[a], conv[b]))
            if dist < worst[0]:
                worst = (dist, a, b)
    if worst[0] < DISTINCT_MIN:
        f.append(f"F3: glyphs {worst[1]} '{chr(worst[1])}' and {worst[2]} "
                 f"'{chr(worst[2])}' differ in {worst[0]} pixels after conversion "
                 f"(< {DISTINCT_MIN})")
    return f


def min_distance(font: dict) -> tuple[int, int, int]:
    cs = font_codes(font, space=True)
    best = (99, 0, 0)
    for i, a in enumerate(cs):
        for b in cs[i + 1:]:
            d = sum(bin(x ^ y).count("1") for x, y in zip(font[a], font[b]))
            best = min(best, (d, a, b))
    return best


def check_all(s: Screen, views=(0, 1)) -> list[str]:
    f = check_mode(s) + check_grid(s) + check_cells(s) + check_font(s.font, s.widen)
    if R.disp_decide(s.disp_st())["mode"] == R.MODE_BLANK:
        return f
    out = convert(s)
    f += check_colours(s, out)
    for v in views:
        f += check_exact(s, out if v == 0 else None, view=v)
    return f


# ---------------------------------------------------------------------------
# Mutants (each must fail check_all; tests/run_modui.py and
# host/tests/test_ui_rules.py)
# ---------------------------------------------------------------------------

def _thin_font(font: dict) -> dict:
    """1-px strokes on the design grid: every horizontal run cut to its
    first pixel."""
    out = {}
    for c, g in font.items():
        out[c] = [b & ~(b >> 1) & 0xFF for b in g]
    return out


def mutant(name: str) -> Screen:
    font = load_font()
    if name == "thin":                  # Topaz-like: native hires, 80 columns
        s = Screen(font, widen=1)
    elif name == "stroke1":             # 1-px strokes on the 320 grid
        s = Screen(_thin_font(font))
    elif name == "close":               # labels $333: dark grey on black
        s = Screen(font, palette=PALETTE[:2] + (0x333,) + PALETTE[3:])
    elif name == "close2":              # vu-mid $585, next to vu-low $584
        s = Screen(font, palette=PALETTE[:5] + (0x585,) + PALETTE[6:])
    elif name == "same":                # 'O' drawn as '0'
        font = dict(font)
        font[ord("O")] = list(font[ord("0")])
        s = Screen(font)
    elif name == "lores":
        s = Screen(font)
        s.bplcon0 = 0x2200
    elif name == "tall":
        s = Screen(font)
        s.lines = 256
    else:
        s = Screen(font)
    player_static(s)
    player_frame(s, mod_view_sample(), sample_trail())
    if name == "cell":                  # VU colour segments off the cell grid
        for k in range(4):
            s.bar(VU_COL, CH_ROW0 + 2 * k, 64, VU_PENS, offset=4)
    return s


MUTANTS = ("thin", "stroke1", "close", "close2", "same", "lores", "tall", "cell")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    shots = []

    def save(s: Screen, tag: str) -> None:
        f = check_all(s)
        print(f"{tag}: {'PASS' if not f else 'FAIL'}")
        for x in f:
            print("   ", x)
        for view in (0, 1):
            out = convert(s, view)
            p = os.path.join(a.out, f"{tag}_{'fit' if not view else '1to1'}.png")
            vic_image(out).save(p)
            shots.append(p)
        p = os.path.join(a.out, f"{tag}_amiga.png")
        amiga_image(s).save(p)
        shots.append(p)

    s = Screen()
    font_sample(s)
    save(s, "font_sample")
    s = Screen()
    player_static(s)
    player_frame(s, mod_view_sample(), sample_trail())
    save(s, "player")
    for m in MUTANTS:
        f = check_all(mutant(m))
        print(f"mutant {m}: {'KILLED' if f else 'SURVIVED'}  ({f[0] if f else ''})")
    p = os.path.join(a.out, "alt_l2_fit.png")
    vic_image(lores_sample(L2_LINES)).save(p)
    shots.append(p)
    s = mutant("thin")
    out = convert(s)
    p = os.path.join(a.out, "mutant_thin_fit.png")
    vic_image(out).save(p)
    shots.append(p)
    print("\n".join(shots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
