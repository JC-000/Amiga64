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
    index; on = colour 1 (screen high nibble), off = colour 0."""
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
    return {"bitmap": bytes(bmp), "screen": screen, "colram": colram,
            "bgc": c[0]}


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


def lut_inc(lut: bytes | None = None) -> str:
    """runtime/ops/disp_lut.inc, the packed LUT as ca65 source."""
    packed = pack_lut(lut if lut is not None else build_lut())
    out = ["; runtime/ops/disp_lut.inc - GENERATED by host/c2v_ref.py lut_inc();",
           "; do not edit.  The 4096-entry OCS-RGB -> VIC LUT (build_lut), nibble-",
           "; packed: byte i = LUT[2i] | LUT[2i+1] << 4.  Pinned by",
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
      DEPTH    not (lores, BPU 2) and not (hires, BPU 4)
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
    elif (hires, bpu) == (True, 4):
        mode, width, n = MODE_HIRES, HIRES_ROW, 4
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
             lines=min(lines, C2V_LINES), width=width, fetch=fetch)
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
    n = 2 if d["mode"] == MODE_MC2 else 4
    planes = [disp_rows(read, d["src"][p], d["width"], d["skip"][p])
              for p in range(n)]
    pal = st["color"][:16]
    conv = convert_mc2 if d["mode"] == MODE_MC2 else convert_hires
    out = conv(planes, pal, lut)
    bmp = bytearray(out["bitmap"])
    for y in range(d["lines"], C2V_LINES):           # below DIWSTOP: colour 0
        for cx in range(40):
            bmp[_cell_offset(y, cx)] = 0
    out["bitmap"] = bytes(bmp)
    out["decide"] = d
    out["vic"] = {0xD011: VIC_D011_ON, 0xD016: VIC_D016[d["mode"]],
                  0xD018: VIC_D018, 0xD020: out["bgc"], 0xD021: out["bgc"]}
    return out


def pack_skip(skip: list[int]) -> bytes:
    """c2v_a_skip (c2v_args + 72): 4 x s16 LE."""
    skip = list(skip) + [0] * (4 - len(skip))
    return struct.pack("<4h", *skip)
