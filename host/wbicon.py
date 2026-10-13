#!/usr/bin/env python3
"""W16-D: Workbench .info files (DiskObject): a parser and a tool-icon builder.

docs/WAVE16_BRIEF.md "W16-D" (2).  The layout is the public one (RKRM
Libraries 3rd ed., "Workbench and Icon Library": struct DiskObject, struct
Gadget, struct Image; the on-disk order of the parts as described by the
public icon-format notes, e.g. the Amiga Development wiki "Icon (.info)
file format"); all big-endian:

  +0   do_Magic $E310, +2 do_Version 1
  +4   do_Gadget (struct Gadget, 44 bytes): +4 NextGadget, +8 LeftEdge,
       +10 TopEdge, +12 Width, +14 Height, +16 Flags, +18 Activation,
       +20 GadgetType, +22 GadgetRender, +26 SelectRender, +30 GadgetText,
       +34 MutualExclude, +38 SpecialInfo, +42 GadgetID, +44 UserData
       (low byte: the icon revision, 1 = OS 2.x)
  +48  do_Type (1 disk, 2 drawer, 3 tool, 4 project, 5 garbage), +49 pad
  +50  do_DefaultTool, +54 do_ToolTypes, +58 do_CurrentX, +62 do_CurrentY
       ($80000000 = no position), +66 do_DrawerData, +70 do_ToolWindow,
       +74 do_StackSize                                    (78 bytes)
then, each present only when its pointer above is non-zero:
  DrawerData: NewWindow (48) + dd_CurrentX/Y (8)            (56 bytes)
  the GadgetRender Image (20: LeftEdge, TopEdge, Width, Height, Depth,
    ImageData, PlanePick, PlaneOnOff, NextImage) + its plane data:
    Depth planes of ((Width + 15) / 16) words x Height
  the SelectRender Image + data (likewise)
  DefaultTool: u32 length (with the NUL) + the bytes
  ToolTypes: u32 (entries + 1) * 4, then each entry as DefaultTool
  ToolWindow: as DefaultTool
  DrawerData2 (a drawer with revision 1): dd_Flags u32, dd_ViewModes u16

    python3 -m host.wbicon FILE.info...      (a summary of each)
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field

MAGIC, VERSION = 0xE310, 1
WBDISK, WBDRAWER, WBTOOL, WBPROJECT, WBGARBAGE = 1, 2, 3, 4, 5
NO_POSITION = -0x80000000
GADGIMAGE, GADGHCOMP, GADGHIMAGE = 0x0004, 0x0000, 0x0002
RELVERIFY, GADGIMMEDIATE, BOOLGADGET = 0x0001, 0x0002, 0x0001
DO_LEN, DD_LEN, IMG_LEN = 78, 56, 20


class IconError(ValueError):
    pass


@dataclass
class Image:
    width: int
    height: int
    depth: int
    planes: list[bytes]                 # depth planes, row-major, word-padded rows
    left: int = 0
    top: int = 0
    pick: int = 3
    onoff: int = 0

    @property
    def row_bytes(self) -> int:
        return ((self.width + 15) // 16) * 2

    def pixel(self, x: int, y: int) -> int:
        """The pen at (x, y) (planes combined)."""
        o = y * self.row_bytes + (x >> 3)
        return sum(((p[o] >> (7 - (x & 7))) & 1) << i for i, p in enumerate(self.planes))


@dataclass
class DiskObject:
    type: int
    image: Image
    select: Image | None = None
    x: int = NO_POSITION
    y: int = NO_POSITION
    stack: int = 4096
    default_tool: str | None = None
    tool_types: list[str] = field(default_factory=list)
    drawer: bytes | None = None         # DrawerData (56 bytes), kept raw
    drawer2: bytes | None = None
    tool_window: str | None = None
    flags: int = GADGIMAGE | GADGHCOMP
    revision: int = 1


def _str(d: bytes, o: int, what: str) -> tuple[str, int]:
    if o + 4 > len(d):
        raise IconError(f"{what}: truncated")
    n = struct.unpack_from(">I", d, o)[0]
    if not 1 <= n <= 4096 or o + 4 + n > len(d):
        raise IconError(f"{what}: bad length {n}")
    return d[o + 4:o + 4 + n].split(b"\0")[0].decode("latin-1"), o + 4 + n


def _image(d: bytes, o: int, what: str) -> tuple[Image, int]:
    if o + IMG_LEN > len(d):
        raise IconError(f"{what}: truncated Image")
    le, te, w, h, dep = struct.unpack_from(">hhHHH", d, o)
    ptr = struct.unpack_from(">I", d, o + 10)[0]
    pick, onoff = d[o + 14], d[o + 15]
    if not (0 < w <= 1024 and 0 < h <= 1024 and 0 < dep <= 8):
        raise IconError(f"{what}: {w}x{h}x{dep}")
    o += IMG_LEN
    rb = ((w + 15) // 16) * 2
    planes = []
    if ptr:
        for _ in range(dep):
            if o + rb * h > len(d):
                raise IconError(f"{what}: plane data truncated")
            planes.append(d[o:o + rb * h])
            o += rb * h
    return Image(w, h, dep, planes, le, te, pick, onoff), o


def parse(d: bytes) -> DiskObject:
    """Every part in order; the whole file must be consumed."""
    if len(d) < DO_LEN:
        raise IconError("shorter than a DiskObject")
    magic, ver = struct.unpack_from(">HH", d, 0)
    if magic != MAGIC or ver != VERSION:
        raise IconError(f"magic ${magic:04X} version {ver}")
    flags = struct.unpack_from(">H", d, 16)[0]
    render, select = struct.unpack_from(">II", d, 22)
    userdata = struct.unpack_from(">I", d, 44)[0]
    typ = d[48]
    dtool, ttypes, cx, cy, ddata, twin, stack = struct.unpack_from(">IIiiIII", d, 50)
    o = DO_LEN
    drawer = None
    if ddata:
        drawer = d[o:o + DD_LEN]
        o += DD_LEN
    if not render:
        raise IconError("no GadgetRender image")
    img, o = _image(d, o, "GadgetRender")
    sel = None
    if select:
        sel, o = _image(d, o, "SelectRender")
    tool = None
    if dtool:
        tool, o = _str(d, o, "DefaultTool")
    tts = []
    if ttypes:
        n = struct.unpack_from(">I", d, o)[0] // 4 - 1
        o += 4
        for i in range(n):
            t, o = _str(d, o, f"ToolTypes[{i}]")
            tts.append(t)
    tw = None
    if twin:
        tw, o = _str(d, o, "ToolWindow")
    d2 = None
    if ddata and userdata & 0xFF:
        d2, o = d[o:o + 6], o + 6
    if o != len(d):
        raise IconError(f"{len(d) - o} bytes left after the parts")
    return DiskObject(typ, img, sel, cx, cy, stack, tool, tts, drawer, d2, tw,
                      flags, userdata & 0xFF)


def build(do: DiskObject) -> bytes:
    """The .info bytes for *do* (pointer fields are 0 or a non-zero flag,
    as on disk)."""
    img = do.image
    if len(img.planes) != img.depth or any(len(p) != img.row_bytes * img.height for p in img.planes):
        raise IconError("image planes do not match width/height/depth")
    out = bytearray(DO_LEN)
    struct.pack_into(">HH", out, 0, MAGIC, VERSION)
    struct.pack_into(">hhHHHHH", out, 8, 0, 0, img.width, img.height, do.flags,
                     RELVERIFY | GADGIMMEDIATE, BOOLGADGET)
    struct.pack_into(">II", out, 22, 1, 1 if do.select else 0)
    struct.pack_into(">I", out, 44, do.revision)
    out[48] = do.type
    struct.pack_into(">IIiiIII", out, 50, 1 if do.default_tool is not None else 0,
                     1 if do.tool_types else 0, do.x, do.y, 1 if do.drawer else 0,
                     1 if do.tool_window is not None else 0, do.stack)
    if do.drawer:
        out += do.drawer
    for im in (img, do.select):
        if im:
            out += struct.pack(">hhHHHIBBI", im.left, im.top, im.width, im.height, im.depth,
                               1, im.pick, im.onoff, 0)
            out += b"".join(im.planes)

    def s(t: str) -> bytes:
        b = t.encode("latin-1") + b"\0"
        return struct.pack(">I", len(b)) + b
    if do.default_tool is not None:
        out += s(do.default_tool)
    if do.tool_types:
        out += struct.pack(">I", (len(do.tool_types) + 1) * 4) + b"".join(s(t) for t in do.tool_types)
    if do.tool_window is not None:
        out += s(do.tool_window)
    if do.drawer and do.revision:
        out += do.drawer2 or bytes(6)
    return bytes(out)


def image_from_rows(rows: list[str]) -> Image:
    """An Image from text rows: '.' pen 0, '#' pen 1, 'o' pen 2, '+' pen 3
    (2 planes)."""
    pens = {".": 0, "#": 1, "o": 2, "+": 3}
    h, w = len(rows), len(rows[0])
    if any(len(r) != w for r in rows):
        raise IconError("ragged rows")
    rb = ((w + 15) // 16) * 2
    planes = [bytearray(rb * h) for _ in range(2)]
    for y, r in enumerate(rows):
        for x, c in enumerate(r):
            v = pens[c]
            for p in range(2):
                if v >> p & 1:
                    planes[p][y * rb + (x >> 3)] |= 0x80 >> (x & 7)
    return Image(w, h, 2, [bytes(p) for p in planes])


# The player's icon (48 x 22): a pen-3 plate with a pen-1 frame and a thick
# beamed pair of eighth notes in pen 1, a pen-2 shadow; bold enough to stay
# readable after the C64's compressed hires conversion, and a distinctive
# bit pattern for the e2e's search.
PLAYER_ROWS = [
    "################################################",
    "#++++++++++++++++++++++++++++++++++++++++++++++#",
    "#++++++++++++++++++++++++++++++++++++++++++++++#",
    "#+++++++++++++++++#####################++++++++#",
    "#+++++++++++++++++#####################o+++++++#",
    "#+++++++++++++++++#####################o+++++++#",
    "#+++++++++++++++++###oooooooooooooo####o+++++++#",
    "#+++++++++++++++++###o+++++++++++++###oo+++++++#",
    "#+++++++++++++++++###o+++++++++++++###o++++++++#",
    "#+++++++++++++++++###o+++++++++++++###o++++++++#",
    "#+++++++++++++++++###o+++++++++++++###o++++++++#",
    "#+++++++++++++++++###o+++++++++++++###o++++++++#",
    "#+++++++++++++++++###o+++++++++++++###o++++++++#",
    "#++++++++++++++#######o+++++++++########o++++++#",
    "#+++++++++++++#########o+++++++##########o+++++#",
    "#+++++++++++++#########o+++++++##########o+++++#",
    "#++++++++++++++#######oo++++++++########oo+++++#",
    "#+++++++++++++++ooooooo++++++++++oooooooo++++++#",
    "#++++++++++++++++++++++++++++++++++++++++++++++#",
    "#++++++++++++++++++++++++++++++++++++++++++++++#",
    "#++++++++++++++++++++++++++++++++++++++++++++++#",
    "################################################",
]


def player_icon(x: int, y: int, stack: int = 8192,
                tool_types: list[str] | None = None) -> bytes:
    """The ModPlayer tool's .info at drawer position (x, y)."""
    return build(DiskObject(WBTOOL, image_from_rows(PLAYER_ROWS), x=x, y=y, stack=stack,
                            tool_types=list(tool_types or [])))


def main(argv=None) -> int:
    for path in (argv if argv is not None else sys.argv[1:]):
        with open(path, "rb") as f:
            do = parse(f.read())
        print(f"{path}: type {do.type} {do.image.width}x{do.image.height}x{do.image.depth} "
              f"at ({do.x}, {do.y}) select {bool(do.select)} tool {do.default_tool!r} "
              f"tooltypes {len(do.tool_types)} stack {do.stack}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
