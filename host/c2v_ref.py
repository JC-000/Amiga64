"""Host reference for the planar -> VIC-II converter v0 (runtime/ops/c2v.s).

W7-V, docs/C2V.md, docs/S3_PLAN.md §2.  Three things live here:

* ``VIC_RGB`` / ``build_lut()``: the 4096-entry OCS-RGB -> VIC colour table
  (nearest VIC colour in CIE L*a*b*, CIE76 distance, ties to the lower VIC
  index).  Index = the 12-bit OCS colour ``$0RGB``; staged in the REU once.
* ``convert_mc2()`` / ``convert_hires()``: byte-exact models of what the
  C64 kernels write (bitmap, screen matrix, colour RAM, $D021 colour) for
  ``rows`` char rows (the module's C2V_ROWS: 25 = 200 lines by default;
  fewer rows leave a colour-0 band in the screen and colour RAM).
* ``pack_args()``: the c2v_args argument block, the module's host ABI.

Everything is pure Python and deterministic; tests/run_c2v.py compares VICE
output with it byte for byte, host/tests/test_c2v_ref.py pins it.
"""

from __future__ import annotations

import struct

# Layout (mirrors runtime/rc/ram.inc and runtime/ops/c2v.s defaults)
C2V_ROWS = 25                   # char rows converted
C2V_LINES = C2V_ROWS * 8        # 200 lines
BITMAP_LEN = C2V_ROWS * 320     # 8000 bytes from c2v_bitmap
SCREEN_LEN = 1000
LORES_ROW = 40                  # bytes per 320-px plane row
HIRES_ROW = 80                  # bytes per 640-px plane row
LUT_LEN = 4096
REU_LINE = 0x80000              # a plane or LUT must not cross one

# Argument block offsets (c2v.s c2v_a_*)
ARG_SRC, ARG_LUT, ARG_PAL, ARG_BGC = 0, 12, 16, 48

# VIC-II colours 0-15, Pepto's PAL measurement (pepto.de/projects/colorvic).
VIC_RGB = (
    (0x00, 0x00, 0x00), (0xFF, 0xFF, 0xFF), (0x68, 0x37, 0x2B), (0x70, 0xA4, 0xB2),
    (0x6F, 0x3D, 0x86), (0x58, 0x8D, 0x43), (0x35, 0x28, 0x79), (0xB8, 0xC7, 0x6F),
    (0x6F, 0x4F, 0x25), (0x43, 0x39, 0x00), (0x9A, 0x67, 0x59), (0x44, 0x44, 0x44),
    (0x6C, 0x6C, 0x6C), (0x9A, 0xD2, 0x84), (0x6C, 0x5E, 0xB5), (0x95, 0x95, 0x95),
)


def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """sRGB (0-255) -> CIE L*a*b* (D65)."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ocs_rgb(c: int) -> tuple[int, int, int]:
    """12-bit OCS colour $0RGB -> 8-bit RGB (each nibble * 17)."""
    return (((c >> 8) & 15) * 17, ((c >> 4) & 15) * 17, (c & 15) * 17)


def nearest_vic(rgb: tuple[int, int, int]) -> int:
    lab = _lab(rgb)
    best, best_d = 0, None
    for i, v in enumerate(VIC_RGB):
        d = sum((p - q) ** 2 for p, q in zip(lab, _lab(v)))
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def build_lut() -> bytes:
    """The 4096-entry OCS-RGB -> VIC colour LUT (index = $0RGB)."""
    return bytes(nearest_vic(ocs_rgb(c)) for c in range(LUT_LEN))


def _lookup(lut: bytes, pal: list[int], i: int) -> int:
    return lut[pal[i] & 0xFFF]


def _cell_offset(y: int, cx: int) -> int:
    return (y >> 3) * 320 + cx * 8 + (y & 7)


def _colours(scr: int, cr: int, c0: int, rows: int) -> tuple[bytes, bytes]:
    band, split = (c0 << 4) | c0, rows * 40
    screen = bytes([scr] * split + [band] * (SCREEN_LEN - split))
    colram = bytes([cr] * split + [c0] * (SCREEN_LEN - split))
    return screen, colram


def pairor(b: int) -> int:
    """4-bit pair-OR of a byte's pixel pairs, leftmost pair in bit 3."""
    return sum(((b >> (7 - 2 * k) | b >> (6 - 2 * k)) & 1) << (3 - k)
               for k in range(4))


def convert_mc2(planes: list[bytes], pal: list[int], lut: bytes,
                rows: int = C2V_ROWS) -> dict:
    """2-plane lores 320 px -> multicolour 160 px, exact colours.

    planes[p] holds rows * 8 contiguous 40-byte rows.  MC pixel k of a
    byte = (plane1, plane0) bits of the left lores pixel of pair k."""
    bmp = bytearray(rows * 320)
    for y in range(rows * 8):
        for cx in range(40):
            p0 = planes[0][y * LORES_ROW + cx]
            p1 = planes[1][y * LORES_ROW + cx]
            bmp[_cell_offset(y, cx)] = (p1 & 0xAA) | ((p0 & 0xAA) >> 1)
    c = [_lookup(lut, pal, i) for i in range(4)]
    screen, colram = _colours((c[1] << 4) | c[2], c[3], c[0], rows)
    return {"bitmap": bytes(bmp), "screen": screen, "colram": colram,
            "bgc": c[0]}


def convert_hires(planes: list[bytes], pal: list[int], lut: bytes,
                  rows: int = C2V_ROWS) -> dict:
    """4-plane hires 640 px -> hires 320 px silhouette (v0).

    Output pixel on iff either pixel of its pair has a nonzero colour
    index; on = colour 1 (screen high nibble), off = colour 0.  W12-V: the
    kernel also leaves each converted cell's pen (``cell_pens``) in colour
    RAM, and ``cnt`` (pen -> cells, saturating at 255) for ``separate``;
    the screen matrix stays c2v_pal_hires's fill (disp's c2v_hfill replaces
    it, ``disp_convert``)."""
    bmp = bytearray(rows * 320)
    for y in range(rows * 8):
        for cx in range(40):
            o = []
            for h in range(2):
                v = 0
                for p in planes[:4]:
                    v |= p[y * HIRES_ROW + 2 * cx + h]
                o.append(pairor(v))
            bmp[_cell_offset(y, cx)] = (o[0] << 4) | o[1]
    c = [_lookup(lut, pal, i) for i in range(2)]
    screen, colram = _colours((c[1] << 4) | c[0], c[0], c[0], rows)
    pens, cnt = cell_pens(planes, rows)
    colram = bytes(pens[:rows * 40]) + colram[rows * 40:]
    return {"bitmap": bytes(bmp), "screen": screen, "colram": colram,
            "bgc": c[0], "pens": pens, "cnt": cnt}


# ---------------------------------------------------------------------------
# W12-V: per-cell pens (hires) and the contrast-preserving colour choice.
# docs/CONTRACTS.md "Display refresh (S3.5)", "Colour choice (W12-V)".
# ---------------------------------------------------------------------------

def _luma(rgb: tuple[int, int, int]) -> float:
    return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]


# The VIC colours by Rec. 709 luma of VIC_RGB, darkest first (ties: lower
# index); disp.s carries it as disp_lad.  Pinned by host/tests/test_disp_colour.py.
LADDER = tuple(sorted(range(16), key=lambda i: (_luma(VIC_RGB[i]), i)))
CNT_MAX = 255                   # a pen's cell count saturates here


def cell_pen(planes: list[bytes], y0: int, cx: int) -> int:
    """The pen of hires cell (row y0 // 8, column cx): on the first of its 8
    lines with a set pixel, the colour index of the leftmost set pixel of
    the cell's 16 (plane bytes 2cx then 2cx+1, bit 7 first); 0 if none."""
    for y in range(y0, y0 + 8):
        for h in range(2):
            o = y * HIRES_ROW + 2 * cx + h
            v = 0
            for p in planes[:4]:
                v |= p[o]
            if v:
                m = 0x80
                while not v & m:
                    m >>= 1
                return sum(1 << p for p in range(4) if planes[p][o] & m)
    return 0


def cell_pens(planes: list[bytes], rows: int = C2V_ROWS) -> tuple[list[int], list[int]]:
    """Every converted cell's pen (row-major, rows * 40) and the cells per
    pen (saturating at CNT_MAX; pen 0 not counted)."""
    pens = [cell_pen(planes, r * 8, cx) for r in range(rows) for cx in range(40)]
    cnt = [0] * 16
    for k in pens:
        if k:
            cnt[k] = min(CNT_MAX, cnt[k] + 1)
    return pens, cnt


def separate(pal: list[int], lut: bytes, cnt: list[int]) -> list[int | None]:
    """The VIC colour of every pen in use (None for the others).

    Pens in use: 0 and every p >= 1 with cnt[p] > 0.  Order: pen 0, then
    by cnt descending, ties by lower pen.  Pass 1 gives each pen in order
    its nearest colour LUT[pal & $FFF] if no pen with a different OCS
    colour holds it; pass 2 walks LADDER outward from that colour's place
    (k, k+1, k-1, k+2, k-2, ...) to the first colour no pen with a
    different OCS colour holds.  Pen 0 always keeps its nearest colour;
    pens with equal OCS colours share one; pens with different OCS colours
    never do (at most 16 pens, 16 colours: pass 2 always finds one)."""
    ocs = [c & 0xFFF for c in list(pal) + [0] * (16 - len(pal))]
    order = [0] + sorted((p for p in range(1, 16) if cnt[p]), key=lambda p: (-cnt[p], p))
    vic: list[int | None] = [None] * 16
    own: list[int | None] = [None] * 16
    for maxd in (0, 15):
        for p in order:
            if vic[p] is not None:
                continue
            k = LADDER.index(lut[ocs[p]])
            for kk in [k] + [k + s * d for d in range(1, maxd + 1) for s in (1, -1)]:
                if 0 <= kk < 16:
                    x = LADDER[kk]
                    if own[x] is None or ocs[own[x]] == ocs[p]:
                        vic[p] = x
                        if own[x] is None:
                            own[x] = p
                        break
    return vic


def screen_sum(screen: bytes) -> int:
    """disp_mck: Fletcher sums mod 255 of the 1000 matrix bytes, as disp.s
    adds them: 8-bit add, then the carry added back (end-around), so a
    value of 255 can stand for 0.  s1 = sum b, s2 = sum of the running s1;
    returned s1 | s2 << 8."""
    s1 = s2 = 0
    for b in screen[:SCREEN_LEN]:
        t = s1 + b
        s1 = (t & 0xFF) + (t >> 8)
        t = s2 + s1
        s2 = (t & 0xFF) + (t >> 8)
    return s1 | s2 << 8


def check_placement(base: int, length: int, what: str) -> None:
    """The v0 precondition: a DMA source never crosses a 512 KB REU line."""
    if not 0 <= base <= 0xFFFFFF or base + length > 0x1000000:
        raise ValueError(f"{what} ${base:06X}+{length} outside the REU")
    if base // REU_LINE != (base + length - 1) // REU_LINE:
        raise ValueError(f"{what} ${base:06X}+{length} crosses a 512 KB line")


def pack_args(src: list[int], lut_addr: int, pal: list[int]) -> bytes:
    """The c2v_args block (48 bytes: 4 x u24 planes, u24 LUT, pad, 16 x u16)."""
    src = list(src) + [0] * (4 - len(src))
    pal = list(pal) + [0] * (16 - len(pal))
    out = b"".join(a.to_bytes(3, "little") for a in src)
    out += lut_addr.to_bytes(3, "little") + b"\0"
    out += struct.pack("<16H", *pal)
    assert len(out) == ARG_BGC
    return out


# ===========================================================================
# W10-V disp: the display refresh driver (runtime/ops/disp.s), S3.5.
# docs/C2V.md "Display refresh (S3.5)".  Additive: nothing above changed.
#
# disp_st (C1, docs/WAVE10_BRIEF.md; W10-P writes it) is decoded by
# ``disp_decide`` into a mode and the converter's arguments, and
# ``disp_convert`` produces what the C64 must show, byte for byte.
# ===========================================================================

DISP_LUT = 0xDF0000             # rc guest.inc disp_lut: REU under the trapped $DF bank
DISP_ST_LEN = 105               # C1: +0 gen ... +39 color[32] (u16 LE each), +103 valid, +104 dirty
COP_VALID = 0xA5                # disp_st+103 once W10-P's cop_reset ran
MODE_NONE, MODE_MC2, MODE_HIRES, MODE_BLANK = 0xFF, 1, 2, 0
(WHY_OK, WHY_DMA, WHY_HAMDPF, WHY_DEPTH, WHY_DIW, WHY_DDF, WHY_STRADDLE,
 WHY_NOSTATE) = range(8)
DMA_DISPLAY = 0x0300            # DMAEN | BPLEN must both be set
# VIC setup of a shown picture (bank 3: $DD00 bits 1-0 = %00; matrix $D000,
# bitmap $E000 -> $D018 = $48; bitmap mode, display on, 25 rows -> $D011)
VIC_D011_ON, VIC_D011_BLANK, VIC_D018 = 0x3B, 0x0B, 0x48
VIC_D016 = {MODE_MC2: 0x18, MODE_HIRES: 0x08}


def pack_lut(lut: bytes) -> bytes:
    """The LUT nibble-packed as disp.s carries it: byte i = lut[2i] | lut[2i+1] << 4."""
    assert len(lut) == LUT_LEN and max(lut) < 16
    return bytes(lut[2 * i] | lut[2 * i + 1] << 4 for i in range(LUT_LEN // 2))


def unpack_lut(packed: bytes) -> bytes:
    return bytes(v for b in packed for v in (b & 15, b >> 4))


def rle_lut(lut: bytes) -> bytes:
    """W12-V: the LUT as disp.s carries it: runs of equal colours, one byte
    each, (run - 1) << 4 | colour, run 1-16."""
    assert len(lut) == LUT_LEN and max(lut) < 16
    out, i = bytearray(), 0
    while i < LUT_LEN:
        j = i
        while j < LUT_LEN and j - i < 16 and lut[j] == lut[i]:
            j += 1
        out.append((j - i - 1) << 4 | lut[i])
        i = j
    return bytes(out)


def unrle_lut(packed: bytes) -> bytes:
    return bytes(v for b in packed for v in [b & 15] * ((b >> 4) + 1))


def lut_inc(lut: bytes | None = None) -> str:
    """runtime/ops/disp_lut.inc, the run-length LUT as ca65 source."""
    packed = rle_lut(lut if lut is not None else build_lut())
    out = ["; runtime/ops/disp_lut.inc - GENERATED by host/c2v_ref.py lut_inc();",
           "; do not edit.  The 4096-entry OCS-RGB -> VIC LUT (build_lut), run-",
           "; length packed (W12-V): byte = (run - 1) << 4 | colour.  Pinned by",
           "; host/tests/test_disp_ref.py."]
    for i in range(0, len(packed), 16):
        out.append("        .byte " + ",".join(f"${b:02X}" for b in packed[i:i + 16]))
    return "\n".join(out) + "\n"


def pack_disp_st(st: dict) -> bytes:
    """A C1 disp_st block from a dict (missing fields 0, valid COP_VALID,
    dirty 0)."""
    def u16(k):
        return struct.pack("<H", st.get(k, 0) & 0xFFFF)
    out = bytes([st.get("gen", 0) & 0xFF]) + u16("dmacon")
    out += u16("bplcon0") + u16("bplcon1") + u16("bplcon2")
    pts = list(st.get("bplpt", [])) + [0] * 6
    out += b"".join((p & 0xFFFFFF).to_bytes(3, "little") for p in pts[:6])
    out += u16("bpl1mod") + u16("bpl2mod")
    out += u16("diwstrt") + u16("diwstop") + u16("ddfstrt") + u16("ddfstop")
    cols = list(st.get("color", [])) + [0] * 32
    out += struct.pack("<32H", *[c & 0xFFFF for c in cols[:32]])
    out += bytes([st.get("valid", COP_VALID) & 0xFF, st.get("dirty", 0) & 0xFF])
    assert len(out) == DISP_ST_LEN
    return out


def unpack_disp_st(b: bytes) -> dict:
    u = lambda o: struct.unpack_from("<H", b, o)[0]       # noqa: E731
    return {"gen": b[0], "dmacon": u(1), "bplcon0": u(3), "bplcon1": u(5),
            "bplcon2": u(7),
            "bplpt": [int.from_bytes(b[9 + 3 * i:12 + 3 * i], "little")
                      for i in range(6)],
            "bpl1mod": u(27), "bpl2mod": u(29), "diwstrt": u(31),
            "diwstop": u(33), "ddfstrt": u(35), "ddfstop": u(37),
            "color": list(struct.unpack_from("<32H", b, 39)), "valid": b[103],
            "dirty": b[104]}


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def disp_decide(st: dict) -> dict:
    """Mode and converter arguments for one disp_st snapshot (disp.s decide).

    Checks, in order (the first failing one gives ``why``; mode BLANK):
      NOSTATE  valid (+103) is not COP_VALID: cop_reset never ran
      DMA      DMACON lacks DMAEN or BPLEN
      HAMDPF   BPLCON0 HAM (bit 11) or DBLPF (bit 10)
      DEPTH    not (lores, BPU 2) and not (hires, BPU 1-4) (W13-D: was BPU 4)
      DIW      DIWSTOP's line <= DIWSTRT's (V8 of stop = NOT V7; start V8 = 0)
      DDF      DDFSTOP < DDFSTRT, the fetch narrower than the kernel's row
               (40 B lores / 80 B hires), or a row skip outside s16
      STRADDLE a fetched row crosses a 512 KB REU line or $FFFFFF
    Fetch width: lores 2 * ((stop&$F8 - strt&$F8)/8 + 1) bytes, hires
    2 * ((stop&$FC - strt&$FC)/4 + 2).  Plane p (0-based) uses BPL1MOD if p
    is even, BPL2MOD if odd; pointers and modulos ignore bit 0 (word DMA).
    Row n+1 of plane p starts fetch + mod after row n; the converter takes
    the first 40/80 bytes of each row (skip = fetch - row + mod).  More than
    200 lines: centre crop ((L - 200) // 2 rows skipped); fewer: the
    converted lines L..199 are cleared to colour 0 afterwards."""
    r = {"mode": MODE_BLANK, "why": WHY_OK, "src": [0] * 4, "skip": [0] * 4,
         "lines": 0, "width": 0, "fetch": 0}
    c0 = st["bplcon0"]
    bpu, hires = (c0 >> 12) & 7, bool(c0 & 0x8000)
    if st.get("valid", COP_VALID) != COP_VALID:
        r["why"] = WHY_NOSTATE
        return r
    if st["dmacon"] & DMA_DISPLAY != DMA_DISPLAY:
        r["why"] = WHY_DMA
        return r
    if c0 & 0x0C00:
        r["why"] = WHY_HAMDPF
        return r
    if (hires, bpu) == (False, 2):
        mode, width, n = MODE_MC2, LORES_ROW, 2
    elif hires and 1 <= bpu <= 4:       # W13-D: BPU 1-3 too, absent planes 0
        mode, width, n = MODE_HIRES, HIRES_ROW, bpu
    else:
        r["why"] = WHY_DEPTH
        return r
    vstart = (st["diwstrt"] >> 8) & 0xFF
    vstop = ((st["diwstop"] >> 8) & 0xFF) | ((~st["diwstop"] >> 7) & 0x100)
    lines = vstop - vstart
    if lines <= 0:
        r["why"] = WHY_DIW
        return r
    s, e = st["ddfstrt"], st["ddfstop"]
    if hires:
        s, e = s & 0xFC, e & 0xFC
        fetch = 2 * ((e - s) // 4 + 2) if e >= s else -1
    else:
        s, e = s & 0xF8, e & 0xF8
        fetch = 2 * ((e - s) // 8 + 1) if e >= s else -1
    mods = [_s16(st["bpl1mod"] & 0xFFFE), _s16(st["bpl2mod"] & 0xFFFE)]
    skip = [fetch - width + mods[p & 1] for p in range(n)]
    if fetch < width or any(not -0x8000 <= k <= 0x7FFF for k in skip):
        r["why"] = WHY_DDF
        return r
    crop = max(0, (lines - C2V_LINES) // 2)
    src = [((st["bplpt"][p] & 0xFFFFFE) + crop * (width + skip[p])) & 0xFFFFFF
           for p in range(n)]
    for p in range(n):
        a = src[p]
        for _ in range(C2V_LINES):
            end = a + width - 1
            if end > 0xFFFFFF or a // REU_LINE != end // REU_LINE:
                r["why"] = WHY_STRADDLE
                return r
            a = (a + width + skip[p]) & 0xFFFFFF
    r.update(mode=mode, src=src + [0] * (4 - n), skip=skip + [0] * (4 - n),
             lines=min(lines, C2V_LINES), width=width, fetch=fetch, planes=n)
    return r


def disp_rows(read, src: int, width: int, skip: int,
              lines: int = C2V_LINES) -> bytes:
    """The rows the kernel fetches, concatenated (read(addr, n) -> bytes)."""
    out, a = bytearray(), src
    for _ in range(lines):
        out += read(a, width)
        a = (a + width + skip) & 0xFFFFFF
    return bytes(out)


def disp_convert(st: dict, read, lut: bytes) -> dict:
    """What disp.s leaves for one snapshot: the decision, and for a shown
    picture bitmap/screen/colram/bgc (C2V_ROWS = 25) plus the VIC values."""
    d = disp_decide(st)
    if d["mode"] == MODE_BLANK:
        return {"decide": d, "border": lut[st["color"][0] & 0xFFF]}
    n = d["planes"]
    planes = [disp_rows(read, d["src"][p], d["width"], d["skip"][p])
              for p in range(n)]
    if d["mode"] == MODE_HIRES:         # W13-D: absent planes read the zero row
        planes += [bytes(C2V_LINES * HIRES_ROW)] * (4 - n)
    pal = st["color"][:16]
    if d["mode"] == MODE_MC2:                       # W12-V: pens 0-3 in use
        vic = separate(pal, lut, [0, 1, 1, 1] + [0] * 12)
        out = convert_mc2(planes, list(range(4)), bytes(vic[:4]) + bytes(LUT_LEN - 4))
    else:                                           # W12-V: c2v_hfill
        out = convert_hires(planes, pal, lut)
        vic = separate(pal, lut, out["cnt"])
        n = C2V_ROWS * 40
        out["screen"] = bytes((vic[k] << 4) | vic[0] for k in out["pens"]) + \
            bytes([(vic[0] << 4) | vic[0]] * (SCREEN_LEN - n))
        out["colram"] = out["colram"][:n] + bytes(SCREEN_LEN - n)    # blank rows: pen 0
    out["vic_pens"] = vic
    bmp = bytearray(out["bitmap"])
    for y in range(d["lines"], C2V_LINES):           # below DIWSTOP: colour 0
        for cx in range(40):
            bmp[_cell_offset(y, cx)] = 0
    out["bitmap"] = bytes(bmp)
    out["decide"] = d
    out["mck"] = screen_sum(out["screen"])          # W12-V: disp_mck
    out["vic"] = {0xD011: VIC_D011_ON, 0xD016: VIC_D016[d["mode"]],
                  0xD018: VIC_D018, 0xD020: out["bgc"], 0xD021: out["bgc"]}
    return out


def pack_skip(skip: list[int]) -> bytes:
    """c2v_a_skip (c2v_args + 72): 4 x s16 LE."""
    skip = list(skip) + [0] * (4 - len(skip))
    return struct.pack("<4h", *skip)


# ===========================================================================
# W13-D: the 1:1 view (runtime/ops/c2v.s c2v_ones / c2v_ofin, disp.s
# disp_vcrop / disp_one1).  docs/C2V.md "1:1 view (W13-D)".  Additive.
# ===========================================================================

ONE_ROW = 40                    # bytes of each plane row in the window
LACE = 0x0004                   # BPLCON0 bit 2: interlace (1:1 view: both fields)
PX_CENTRE, PX_MAX = 20, 40      # disp_px: the window's first byte (centred / last)
NO_FG = 0xFF                    # c2v_ones: a cell without a second pen


def one_cells(planes: list[bytes], rows: int = C2V_ROWS):
    """The 1:1 kernel's pass: bitmap, every cell's (fg, bg) and the counts.

    Per 8x8 cell: bg = the pen of the cell's top-left pixel (bit 7 of the
    first line's byte); a pixel is set iff its pen <> bg; fg = the pen of
    the first set pixel in raster order (NO_FG if none).  cnt: every
    cell's bg and every fg, saturating at CNT_MAX."""
    bmp = bytearray(rows * 320)
    cnt = [0] * 16
    cells = []
    for r in range(rows):
        for cx in range(40):
            bg = sum(1 << p for p in range(4) if planes[p][r * 8 * ONE_ROW + cx] & 0x80)
            fg = NO_FG
            for y in range(r * 8, r * 8 + 8):
                o = y * ONE_ROW + cx
                v = 0
                for p in range(4):
                    v |= planes[p][o] ^ (0xFF if bg >> p & 1 else 0)
                bmp[_cell_offset(y, cx)] = v
                if v and fg == NO_FG:
                    m = 0x80
                    while not v & m:
                        m >>= 1
                    fg = sum(1 << p for p in range(4) if planes[p][o] & m)
                    cnt[fg] = min(CNT_MAX, cnt[fg] + 1)
            cnt[bg] = min(CNT_MAX, cnt[bg] + 1)
            cells.append((fg, bg))
    return bytes(bmp), cells, cnt


def convert_one(planes: list[bytes], pal: list[int], lut: bytes,
                rows: int = C2V_ROWS) -> dict:
    """1:1: 4 planes of rows * 8 window rows (40 bytes each) -> hires 320.

    one_cells, then separate() on its counts gives the pens' colours; the
    matrix cell = vic[fg] << 4 | vic[bg] (vic[bg] in both nibbles without
    an fg).  Colour RAM is not written ("colram": None)."""
    bmp, cells, cnt = one_cells(planes, rows)
    vic = separate(pal, lut, cnt)
    screen = bytes((vic[bg if fg == NO_FG else fg] << 4) | vic[bg] for fg, bg in cells)
    return {"bitmap": bmp, "screen": screen, "colram": None, "bgc": vic[0],
            "vic_pens": vic, "cnt": cnt, "cells": cells}


def disp_decide_view(st: dict, view: int = 0, px: int = PX_CENTRE,
                     py: int = 0) -> dict:
    """disp_decide plus W13-D's view: for a hires picture with view 1 the
    window starts at line min(py, ymax) and byte px of every row; "one"
    says the 1:1 kernel runs.  ymax = L - 200 (LACE: L - 100), clamped to
    0-255.  With LACE the 1:1 window is 200 frame lines from field line
    crop: each row advance is halved ((skip + 80) // 2 - 80, the two fields
    interleaved) and lines = min(2 * lines, 200).  "crop" is the first
    (field) line shown in either view (the pointer's vertical reference)."""
    d = disp_decide(st)
    d["one"] = False
    if d["mode"] == MODE_BLANK:
        return d
    vstart = (st["diwstrt"] >> 8) & 0xFF
    vstop = ((st["diwstop"] >> 8) & 0xFF) | ((~st["diwstop"] >> 7) & 0x100)
    lines = vstop - vstart
    lace = bool(st["bplcon0"] & LACE)
    d["ymax"] = min(255, max(0, lines - (C2V_LINES // 2 if lace else C2V_LINES)))
    d["crop"] = max(0, (lines - C2V_LINES) // 2)
    d["lace"] = lace
    if view and d["mode"] == MODE_HIRES:
        crop = min(py, d["ymax"])
        n = d["planes"]
        adv = [d["width"] + d["skip"][p] for p in range(n)]
        base = [(st["bplpt"][p] & 0xFFFFFE) for p in range(n)]
        d["src"] = [((base[p] + crop * adv[p]) & 0xFFFFFF) for p in range(n)]
        d["one"], d["crop"], d["px"] = True, crop, px
        if lace:                # 200 frame lines: the fields interleave
            d["skip"] = [(k + HIRES_ROW) // 2 - HIRES_ROW for k in d["skip"][:n]] \
                + d["skip"][n:]
            d["lines"] = min(2 * d["lines"], C2V_LINES)
    return d


def disp_convert_view(st: dict, read, lut: bytes, view: int = 0,
                      px: int = PX_CENTRE, py: int = 0) -> dict:
    """disp_convert with W13-D's view (the fit view: disp_convert itself)."""
    d = disp_decide_view(st, view, px, py)
    if not d["one"]:
        out = disp_convert(st, read, lut)
        out["decide"].update(one=False, crop=d.get("crop", 0), ymax=d.get("ymax", 0))
        return out
    planes = [disp_rows(read, (d["src"][p] + px) & 0xFFFFFF, ONE_ROW,
                        d["width"] + d["skip"][p] - ONE_ROW) for p in range(d["planes"])]
    planes += [bytes(C2V_LINES * ONE_ROW)] * (4 - d["planes"])
    out = convert_one(planes, st["color"][:16], lut)
    bmp = bytearray(out["bitmap"])
    for y in range(d["lines"], C2V_LINES):           # below DIWSTOP: 0
        for cx in range(40):
            bmp[_cell_offset(y, cx)] = 0
    out["bitmap"] = bytes(bmp)
    out["decide"] = d
    out["mck"] = screen_sum(out["screen"])
    out["vic"] = {0xD011: VIC_D011_ON, 0xD016: VIC_D016[MODE_HIRES],
                  0xD018: VIC_D018, 0xD020: out["bgc"], 0xD021: out["bgc"]}
    return out


# ===========================================================================
# W13-D D2: the pointer (Amiga sprite 0) as VIC sprites 0-2, the machine-mode
# module tests/aros/spr.s.  docs/CONTRACTS.md "Pointer sprite (W13-D)".
# ===========================================================================

SPR_LINES = 21                  # the VIC's sprite height
SPR_EDGE_X = (16, 144)          # lores: the 1:1 window's inner band (32 C64 px)
SPR_EDGE_Y = {False: (32, 168, 100), True: (16, 84, 50)}   # LACE: field lines


def spr_decode(pos: int, ctl: int) -> tuple[int, int, int]:
    """(HSTART, VSTART, VSTOP), 9 bits each, from SPRxPOS / SPRxCTL."""
    hstart = ((pos & 0xFF) << 1) | (ctl & 1)
    vstart = (pos >> 8) | ((ctl >> 2) & 1) << 8
    vstop = (ctl >> 8) | ((ctl >> 1) & 1) << 8
    return hstart, vstart, vstop


def spr_first_pixel(cur: dict, hires: bool) -> int:
    """hcount (lores) of the playfield's first fetched pixel: 2 * DDFSTRT + 9
    (hires, DDFSTRT & $FC) or + 17 (lores, & $F8)."""
    if hires:
        return 2 * (cur["ddfstrt"] & 0xFC) + 9
    return 2 * (cur["ddfstrt"] & 0xF8) + 17


def spr_place(cur: dict, pos: int, ctl: int, hires: bool, one: bool, px: int,
              crop: int, ymax: int) -> dict:
    """One frame of spr_frame's position logic.  Returns {"hidden": True},
    {"pan": (px, py)} (the window moves; no VIC write this frame), or the
    VIC values {"x", "y", "msb", "xexp", "yexp", "height"}."""
    hstart, vstart, vstop = spr_decode(pos, ctl)
    h = vstop - vstart
    if h <= 0:
        return {"hidden": True}
    sy = vstart - ((cur["diwstrt"] >> 8) & 0xFF)
    sx = hstart + 1 - spr_first_pixel(cur, hires)
    lace = one and bool(cur["bplcon0"] & LACE)
    wy = sy - crop
    wx = sx - (4 * px if one else 0)
    if one:
        npx, npy = px, crop
        if not SPR_EDGE_X[0] <= wx < SPR_EDGE_X[1]:
            npx = min(max(sx - 80, 0), 160) // 4
        lo, hi, half = SPR_EDGE_Y[lace]
        if not lo <= wy < hi:
            npy = min(max(sy - half, 0), ymax)
        if (npx, npy) != (px, crop):
            return {"pan": (npx, npy)}
    y = 50 + (2 * wy if lace else wy)
    x = 24 + (2 * wx if one else wx)
    if not 0 <= y <= 255 or not 0 <= x <= 0x1FF:
        return {"hidden": True}
    return {"x": x & 0xFF, "y": y, "msb": 7 if x > 255 else 0,
            "xexp": 7 if one else 0, "yexp": 7 if lace else 0,
            "height": min(h, SPR_LINES)}


def spr_settle(cur: dict, pos: int, ctl: int, hires: bool, one: bool, px: int,
               py: int, ymax: int, steps: int = 4) -> tuple[dict, int, int, int]:
    """spr_place frame after frame until the window stops moving (each pan
    converts: crop := min(py, ymax)).  Returns (placement, px, py, pans)."""
    pans = 0
    crop = min(py, ymax) if one else (cur.get("crop", 0))
    for _ in range(steps):
        r = spr_place(cur, pos, ctl, hires, one, px, crop, ymax)
        if "pan" not in r:
            return r, px, py, pans
        px, py = r["pan"]
        crop = min(py, ymax)
        pans += 1
    raise AssertionError("the window does not settle")


def spr_blocks(data: list[tuple[int, int]], height: int) -> bytes:
    """The three 64-byte VIC blocks: sprite 0 = DATA & ~DATB (colour 17),
    1 = ~DATA & DATB (18), 2 = DATA & DATB (19); 16 px in bytes 0-1 of
    each line, line >= height and the rest 0."""
    blk = [bytearray(64) for _ in range(3)]
    for ln in range(min(height, SPR_LINES)):
        a, b = data[ln]
        for k, v in enumerate((a & ~b, ~a & b, a & b)):
            v &= 0xFFFF
            blk[k][3 * ln], blk[k][3 * ln + 1] = v >> 8, v & 0xFF
    return bytes(blk[0] + blk[1] + blk[2])
