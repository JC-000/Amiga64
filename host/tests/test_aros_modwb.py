"""W16-D: tests/aros_modwb.py's guest readers on a synthetic guest memory
(the icon search in the screen planes, the state-block accessor, the task
and library walks).  No emulator."""

from __future__ import annotations

import struct

import pytest

from host import wbicon as W
from tests import aros_modwb as MW

PL0, PL1, PL2 = 0x10000, 0x20000, 0x30000
ROWB = 80


class Mem:
    def __init__(self):
        self.b = bytearray(0x2E0000)       # chip RAM .. the app anchor

    def read(self, a, n):
        return bytes(self.b[a:a + n])

    def l(self, a, v):
        struct.pack_into(">I", self.b, a, v)

    def s(self, a, t):
        self.b[a:a + len(t) + 1] = t.encode() + b"\0"


def screen(mem: Mem, x: int, y: int, flip: tuple | None = None) -> dict:
    img = W.image_from_rows(W.PLAYER_ROWS)
    rb = img.row_bytes
    for p, base in ((0, PL0), (1, PL1)):
        for r in range(img.height):
            row = int.from_bytes(img.planes[p][r * rb:(r + 1) * rb], "big") >> (rb * 8 - img.width)
            a = base + (y + r) * ROWB
            cur = int.from_bytes(mem.b[a:a + 80], "big")
            cur |= row << (640 - x - img.width)
            mem.b[a:a + 80] = cur.to_bytes(80, "big")
    if flip:
        fx, fy, p = flip
        a = (PL0, PL1, PL2)[p] + fy * ROWB + fx // 8
        mem.b[a] ^= 0x80 >> (fx % 8)
    return {"bplcon0": 0x3000 | 0x8000, "bpl1mod": 0, "bplpt": [PL0, PL1, PL2]}


WIN = {"x": 40, "y": 20, "w": 400, "h": 120}


@pytest.mark.parametrize("x,y", [(100, 30), (41, 21), (391, 117), (343, 60)])
def test_find_player_icon(x, y):
    mem = Mem()
    st = screen(mem, x, y)
    got = MW.find_player_icon(mem.read, st, WIN)
    assert got and (got["x0"], got["y0"], got["x1"], got["y1"]) == (x, y, x + 47, y + 21)
    assert (got["cx"], got["cy"]) == (x + 24, y + 11)


@pytest.mark.parametrize("flip", [(110, 35, 0), (147, 51, 1), (100, 30, 2)])
def test_find_player_icon_needs_every_pixel(flip):
    mem = Mem()
    st = screen(mem, 100, 30, flip)
    assert MW.find_player_icon(mem.read, st, WIN) is None


def test_find_player_icon_only_inside_the_window():
    mem = Mem()
    st = screen(mem, 500, 30)
    assert MW.find_player_icon(mem.read, st, WIN) is None


def _exec(mem: Mem, task_name: str) -> None:
    sb = 0x1000
    mem.l(4, sb)
    task = 0x2000
    mem.l(sb + MW.SYSBASE_THISTASK, task)
    mem.l(task + MW.LN_NAME, 0x2100)
    mem.s(0x2100, task_name)
    # TaskReady: one task "input.device"; TaskWait: empty (head -> tail node)
    rdy, n = sb + MW.EXEC_TASKREADY, 0x2200
    mem.l(rdy, n)
    mem.l(n, rdy + 4)                    # its successor: the list's tail
    mem.l(n + MW.LN_NAME, 0x2300)
    mem.s(0x2300, "input.device")
    wt = sb + MW.EXEC_TASKWAIT
    mem.l(wt, wt + 4)


def _anchor(mem: Mem, ptr: int, magic: bytes = b"MWBA") -> None:
    mem.l(MW.ANCHOR_ADDR + 4, ptr)
    mem.b[MW.ANCHOR_ADDR:MW.ANCHOR_ADDR + 4] = magic


def _struct(mem: Mem, at: int, state: int, why: int, frames: int) -> None:
    mem.b[at:at + 4] = b"MWBS"
    struct.pack_into(">H", mem.b, at + 4, 1)
    mem.b[at + 6], mem.b[at + 7] = state, why
    mem.l(at + 56, frames)


def test_app_state_reads_the_anchor_and_struct():
    mem = Mem()
    assert MW.app_state(mem.read) == {}                  # nothing published
    _struct(mem, 0x3000, 1, 0, 77)
    _anchor(mem, 0x3000)
    got = MW.app_state(mem.read)
    assert (got["phase"], got["frames"], got["why"]) == ("running", 77, None)
    _struct(mem, 0x3000, 3, 2, 80)
    assert MW.app_state(mem.read)["phase"] == "done" and MW.app_state(mem.read)["why"] == "song end"
    mem.b[MW.ANCHOR_ADDR] = 0                             # 'MWBA' cleared first: gone
    assert MW.app_state(mem.read) == {}


@pytest.mark.parametrize("ptr,bad", [(0, "pointer"), (0x3001, "pointer"),
                                     (0x1FFFF0, "pointer"),       # struct crosses chip's end
                                     (0x200000, "pointer"), (0x4000, "struct magic")])
def test_app_state_refuses_a_bad_anchor(ptr, bad):
    mem = Mem()
    _struct(mem, 0x3000, 1, 0, 5)
    _anchor(mem, ptr)
    got = MW.app_state(mem.read)
    assert got["anchor"] and got["bad"] == bad and "phase" not in got


def test_parse_anchor_slow_ram_span():
    raw = b"MWBA" + struct.pack(">III", 0xDBFFC0, 0, 0)
    assert MW.parse_anchor(raw, 60)["ptr_ok"] is True     # $DBFFC0 + 60 <= $DC0000
    raw = b"MWBA" + struct.pack(">III", 0xDBFFD0, 0, 0)
    assert MW.parse_anchor(raw, 60)["ptr_ok"] is False


def test_task_names_and_this_task():
    mem = Mem()
    _exec(mem, "ModPlayer")
    assert MW.task_names(mem.read) == ["ModPlayer", "input.device"]
    assert MW.this_task(mem.read) == "ModPlayer"


def test_find_player_icon_selected_form():
    mem = Mem()
    st = screen(mem, 100, 30)
    st["bplcon0"] = 0x2000 | 0x8000      # 2 planes
    for p in (PL0, PL1):                 # complement the box in both planes
        for y in range(30, 52):
            for x in range(100, 148):
                mem.b[p + y * ROWB + x // 8] ^= 0x80 >> (x % 8)
    got = MW.find_player_icon(mem.read, st, WIN)
    assert got and got["selected"] and (got["x0"], got["y0"]) == (100, 30)
