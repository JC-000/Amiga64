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
