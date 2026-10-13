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


@pytest.mark.parametrize("bplcon0", [0x0200, 0x8200, 0x9200])   # BPU 0 lores/hires, BPU 1
def test_find_player_icon_needs_both_icon_planes(bplcon0):
    """fix round 3, F1 (fix2/red-noinfo): a disp_st with BPU 0 matched at
    the window's corner with no plane read (all() of nothing)."""
    mem = Mem()
    st = dict(screen(mem, 100, 30), bplcon0=bplcon0)
    assert MW.find_player_icon(mem.read, st, WIN) is None
    assert MW.find_player_icon(mem.read, st, WIN, (90, 20, 160, 60)) is None


def _disp_block(bplcon0: int, valid: int = 0xA5, color0: int = 0) -> bytes:
    from host import c2v_ref as R
    return R.pack_disp_st({"dmacon": 0x0300, "bplcon0": bplcon0, "bplpt": [PL0, PL1, 0, 0, 0, 0],
                           "color": [color0] + [0] * 31, "valid": valid}).ljust(256, b"\0")


def test_stable_disp_rereads_a_torn_block():
    """F1: a host read inside a copper run can see BPLCON0 half written;
    re-read until two reads agree (the colours may flicker: the stand-in
    alternates COLOR00 every frame)."""
    reads = iter([_disp_block(0x0200), _disp_block(0xA200, color0=1),
                  _disp_block(0xA200, color0=2), _disp_block(0x0200)])
    st = MW.stable_disp(lambda: next(reads))
    assert st["bplcon0"] == 0xA200 and st["bplpt"][:2] == [PL0, PL1]


def test_stable_disp_keeps_a_steady_bpu0_display():
    """norestore's st2 really is $0200: steady, so it is taken as it is."""
    st = MW.stable_disp(lambda: _disp_block(0x0200))
    assert st["bplcon0"] == 0x0200


@pytest.mark.parametrize("blocks", [
    [_disp_block(0x0200), _disp_block(0xA200)] * 4,          # never two alike
    [_disp_block(0xA200, valid=0)] * 8])                     # cop_reset never ran
def test_stable_disp_fails_clearly(blocks):
    it = iter(blocks)
    with pytest.raises(MW.DispUnstable, match="disp_st"):
        MW.stable_disp(lambda: next(it))


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


# -- fix round 1 (H1): the region search, the bulk frozen reader ---------------

def test_find_player_icon_in_a_region_and_the_window_fallback():
    mem = Mem()
    st = screen(mem, 100, 30)
    box = (84, 22, 164, 60)              # the stored position's box
    got = MW.find_player_icon(mem.read, st, WIN, box)
    assert got and (got["x0"], got["y0"]) == (100, 30)
    assert MW.find_player_icon(mem.read, st, WIN, (200, 22, 280, 60)) is None
    ic, how = MW.icon_search(mem.read, st, WIN, (200, 22, 280, 60))
    assert how == "window" and (ic["x0"], ic["y0"]) == (100, 30)
    assert MW.icon_search(mem.read, st, WIN, box)[1] == "box"
    assert MW.find_player_icon(mem.read, st, WIN, (100, 30, 147, 50)) is None   # too small


def test_the_region_search_reads_only_the_box_rows():
    mem = Mem()
    st = screen(mem, 100, 30)
    st["bplcon0"] = 0x2000 | 0x8000      # the Workbench's 2 planes
    seen = set()

    def rd(a, n):
        seen.update(range(a & ~0xFF, a + n, 0x100))
        return mem.read(a, n)
    MW.find_player_icon(rd, st, WIN, (84, 22, 164, 60))
    assert len(seen) <= 2 * 14          # 39 rows of 80 bytes per plane (~16 pages)


def test_expected_box_from_the_stored_position():
    class D:
        icon = W.parse(W.player_icon(299, 4))
    dw = {"x": 47, "y": 27, "w": 390, "h": 118, "bl": 4, "bt": 15}
    x0, y0, x1, y1 = MW.expected_box(dw, D)
    assert (x0 + MW.ICON_MARGIN[0], y0 + MW.ICON_MARGIN[1]) == (350, 47)    # vice1's find
    assert (x1 - x0, y1 - y0) == (48 + 2 * MW.ICON_MARGIN[0], 22 + 2 * MW.ICON_MARGIN[1])


def run_stub(code: bytes, org: int, ram: bytearray, reu: bytes, dirty: int) -> int:
    """The 6502 subset bulk_stub uses, with a REU at $DF0x (fetch on $DF01
    bit 7, REU -> C64); -> the number of fetches."""
    ram[org:org + len(code)] = code
    pc, x, a, z, n, sp, fetches = org, 0, 0, False, False, [], 0
    regs = bytearray(16)
    while True:
        op = ram[pc]
        w = ram[pc + 1] | ram[pc + 2] << 8
        if op == 0xA2:                    # ldx #
            x = ram[pc + 1]; z = x == 0; pc += 2          # noqa: E702
        elif op == 0xA9:                  # lda #
            a = ram[pc + 1]; pc += 2                       # noqa: E702
        elif op == 0xBD:                  # lda abs,x
            a = ram[w + x]; pc += 3                        # noqa: E702
        elif op == 0x8D:                  # sta abs
            if 0xDF00 <= w <= 0xDF0F:
                regs[w & 15] = a
                if w == 0xDF01 and a & 0x80:
                    assert a == 0x91 and regs[0x0A] == 0
                    c64 = regs[2] | regs[3] << 8
                    src = regs[4] | regs[5] << 8 | regs[6] << 16
                    ln = regs[7] | regs[8] << 8 or 0x10000
                    ram[c64:c64 + ln] = reu[src:src + ln]
                    fetches += 1
            else:
                ram[w] = a
            pc += 3
        elif op == 0xE8:                  # inx
            x = (x + 1) & 0xFF; z = x == 0; pc += 1        # noqa: E702
        elif op == 0xE0:                  # cpx #
            z = x == ram[pc + 1]; pc += 2                  # noqa: E702
        elif op == 0xD0:                  # bne
            off = ram[pc + 1]
            pc += 2 + (off - 256 if off & 0x80 else off) if not z else 2
        elif op == 0x20:                  # jsr
            assert w == dirty
            sp.append(pc + 2)
            pc = w if w in ram_routines else pc + 3
        elif op == 0x60:                  # rts
            return fetches
        else:
            raise AssertionError(f"opcode ${op:02X} at ${pc:04X}")


ram_routines: set = set()                 # jsr mem_rec_dirty: stepped over


@pytest.mark.parametrize("pages", [[0x20000], [0x1F00, 0x2000, 0xC80500], list(range(0x10000, 0x10000 + 31 * 256, 256))])
def test_bulk_stub_fetches_each_page_to_the_buffer(pages):
    reu = bytes((i * 7 + (i >> 8) * 3 + (i >> 16)) & 0xFF for i in range(0xC80600))
    ram = bytearray(0x10000)
    code = MW.bulk_stub(0xCC00, pages, 0xE0, 0x1234)
    assert len(code) == 256
    assert run_stub(code, 0xCC00, ram, reu, 0x1234) == len(pages)
    for i, pg in enumerate(pages):
        assert ram[0xE000 + 256 * i:0xE100 + 256 * i] == reu[pg:pg + 256]
    with pytest.raises(AssertionError):
        MW.bulk_stub(0xCC00, list(range(32)), 0xE0, 0x1234)


class FakeT:
    def __init__(self, ram):
        self.ram = ram

    def read_memory(self, a, n):
        assert n <= 1024
        return bytes(self.ram[a:a + n])


class FakeM:
    """A frozen Ultimate: C64 RAM, the REU = the guest's memory, the runner
    call executing the stub; *rom_at_e000*: the read-back sees ROM."""
    vice = False

    def __init__(self, reu, rom_at_e000=False):
        self.ram, self.reu, self.rom = bytearray(0x10000), reu, rom_at_e000
        self.t, self.tg = FakeT(self.ram), object()
        if rom_at_e000:
            self.t = FakeT(bytearray(b"\xEA" * 0x10000))

    def w(self, a, data):
        self.ram[a:a + len(data)] = data


class FakeSn:
    a_dirty = 0x1234

    def __init__(self, m):
        self.m, self.n = m, 0

    def reu_read(self, a, n):
        self.n += 1
        return bytes(self.m.reu[a:a + n])


def _reader(monkeypatch, rom=False):
    from tests import harness_util as HU
    reu = bytes((i * 13 + (i >> 9)) & 0xFF for i in range(0x40000))
    m = FakeM(reu, rom)
    monkeypatch.setattr(HU, "call_runner",
                        lambda tg, addr, to: run_stub(bytes(m.ram[addr:addr + 256]), addr,
                                                      m.ram, reu, FakeSn.a_dirty))
    monkeypatch.setattr(MW, "BULK_OK", [None])
    return MW.FrozenReader(m, FakeSn(m)), reu


def test_frozen_reader_bulk_then_cache(monkeypatch):
    rd, reu = _reader(monkeypatch)
    rd.prefetch([(0x10010, 3000), (0x20000, 160)])
    assert rd.stats == {"single": 1, "bulk_calls": 1, "bulk_pages": 13}   # 1: the check
    assert MW.BULK_OK[0] is True
    assert rd(0x10010, 3000) == reu[0x10010:0x10010 + 3000]
    assert rd(0x1020000, 160) == reu[0x20000:0x20000 + 160]                 # 24-bit bus
    assert rd.stats["single"] == 1                                          # all cached
    rd.prefetch([(0x30000, 40 * 256)])
    assert rd.stats["bulk_calls"] == 3 and rd.stats["bulk_pages"] == 53     # 31 + 9


def test_frozen_reader_falls_back_when_the_read_back_is_not_ram(monkeypatch):
    rd, reu = _reader(monkeypatch, rom=True)
    rd.prefetch([(0x10000, 2048)])
    assert MW.BULK_OK[0] is False
    assert rd(0x10000, 2048) == reu[0x10000:0x10800]                       # page by page
    rd.prefetch([(0x20000, 2048)])
    assert rd.stats["bulk_calls"] == 1


class FakeE:
    def __init__(self, m):
        self.m = m

    def frames_wait(self, k, timeout=120.0):
        self.m.f += k


def test_settle_td_waits_for_a_quiet_read_count():
    class M:
        f = 0

        def frames(self):
            return self.f

        def b(self, name, n=1):
            assert name == "td_nrd"
            return struct.pack("<H", min(self.f // 20, 9))      # reads until frame 180
    m = M()
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500)
    assert out["quiet"] and out["reads"] == 9 and 300 <= out["frames"] <= 320
    m.f = 0
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=100)
    assert not out["quiet"] and out["frames"] <= 110


def test_settle_td_after_an_action_waits_for_the_first_read():
    """R2-3: reads start 200 frames after the action; quiet is measured
    from the first read, not from the action."""
    class M:
        f = 0

        def frames(self):
            return self.f

        def b(self, name, n=1):
            return struct.pack("<H", 0 if self.f < 200 else min((self.f - 200) // 20 + 1, 5))
    m = M()
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500)          # the old way: nothing seen
    assert out["quiet"] and out["reads"] == 0
    m.f = 0
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500, first=600)
    assert out["quiet"] and out["reads"] == 5 and 200 <= out["first_read_frame"] <= 210
    assert out["frames"] >= 280 + 120
    m.f = 0                                                          # no read at all: bounded
    m.b = lambda name, n=1: b"\0\0"
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500, first=600)
    assert out["first_read_frame"] is None and out["frames"] <= 600 + 130


def test_settle_td_counts_the_reads_from_before_the_action():
    """fix round 3, F5: n0 is sampled before the double-click, so reads
    that start during dclick()'s 8 frames count as the first read."""
    class M:
        f = 0

        def frames(self):
            return self.f

        def b(self, name, n=1):
            return struct.pack("<H", 3 if self.f >= 5 else 0)   # 3 reads at frames 5-7
    m = M()
    n0 = MW.td_reads(m)
    m.f = 8                                                      # dclick()
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500, first=600, n0=n0)
    assert out["reads"] == 3 and out["first_read_frame"] == 0 and out["frames"] <= 130
    m.f = 8                                                      # sampled after: missed
    out = MW.settle_td(m, FakeE(m), quiet=120, limit=1500, first=600)
    assert out["reads"] == 0 and out["first_read_frame"] is None


@pytest.mark.parametrize("mutant", ["norestore", "keepanchor", "noprof"])
def test_stand_in_mutants_refuse_a_real_player(mutant):
    """fix round 3, F3: these mutants are stand-in build defines; with
    --modwb-prg they would not apply and the "red" would run green."""
    from tests import aros_wb as WB
    with pytest.raises(SystemExit, match="stand-in"):
        WB.main(["--prg", "x.prg", "--legs", "modwb", "--disk", "modwb",
                 "--modwb-prg", "player", "--modwb-mutant", mutant])
    WB.check_modwb_mutant(None, mutant)                          # the stand-in: fine
    for other in ("noinfo", "noclick", "wrongicon", "stuckpic", None):
        WB.check_modwb_mutant("player", other)


@pytest.mark.parametrize("forced,hashes,ok", [
    (True, ["H0"], True), (True, ["H1"], False),
    (False, ["H0", "H0"], True), (False, ["H1", "W", "H0", "H0"], True),
    (False, ["H1"] * 11, False),
    (False, ["W"] * 11, False),                 # R2-1: stuckpic's frozen look picture
    (False, ["W", "H0", "W", "H0", "W", "H0", "W", "H0", "W", "H0", "W"], False)])
def test_return_picture(monkeypatch, forced, hashes, ok):
    from tests import aros_wb as WB
    monkeypatch.setattr(WB, "force_conv", lambda m, timeout: None)
    it = iter(hashes)

    class M:
        f = 0

        def frames(self):
            return self.f
    m = M()
    rt: dict = {}
    fails = MW.return_picture(m, FakeE(m), lambda: next(it) * 10, "H0" * 10, "H1" * 10, rt,
                              forced)
    assert (not fails) == ok, (fails, rt)
    if not forced:
        assert rt["hash_frames"] <= MW.RETURN_PIC_FRAMES + 48


def test_the_deadline_stops_every_wait(monkeypatch):
    """R2-2: t_start + --timeout; settle_td and return_picture consult it at
    each step, so a failing run reports instead of holding the device."""
    now = [1000.0]
    monkeypatch.setattr(MW.time, "monotonic", lambda: now[0])
    due = MW.Deadline(t_start=100.0, timeout=950.0)
    assert due.left() == 50.0
    due("ok")
    now[0] = 1051.0
    with pytest.raises(MW.LegDeadline, match="--timeout 950 s.*the td settle"):
        due("the td settle")

    class M:
        f = 0

        def frames(self):
            return self.f

        def b(self, name, n=1):
            return struct.pack("<H", self.f // 10)       # td never quiet
    m = M()
    with pytest.raises(MW.LegDeadline):
        MW.settle_td(m, FakeE(m), first=600, due=due)
    with pytest.raises(MW.LegDeadline):
        MW.return_picture(m, FakeE(m), lambda: "W" * 20, "H0" * 10, "H1" * 10, {}, False, due)


def test_the_forced_pictures_convert_twice(monkeypatch):
    """vice-live run 2: one forced conversion can be the in-flight one."""
    from tests import aros_wb as WB
    calls = []
    monkeypatch.setattr(WB, "force_conv", lambda m, timeout: calls.append(1))
    MW.settled_conv(object())
    assert len(calls) == 2
    calls.clear()
    MW.return_picture(object(), None, lambda: "H0" * 10, "H0" * 10, "H1" * 10, {}, True)
    assert len(calls) == 2


@pytest.mark.parametrize("vice", [True, False])
def test_the_picture_hash_masks_only_the_title_bar(vice):
    """diag-vice-live: h0 and the return differed only in the title bar
    (C64 rows 6-15, the free-memory text); a change below it still counts."""
    mask = MW.MF.title_mask_rows({}, 15)
    assert mask == [0, 1]
    raw = {"bitmap": bytes(8000), "screen": bytes(1000) if vice else None,
           "colram": bytes(1000), "vic": {a: 0 for a in MW.MF.VIC_MASK}, "mck": 0x1234}
    h0 = MW.picture_hash(raw, mask, vice)
    title = dict(raw, bitmap=bytes(6 * 40) + b"\xff" * 320 + bytes(8000 - 560))   # rows 0-1
    assert MW.picture_hash(title, mask, vice) == h0
    body = dict(raw, bitmap=bytes(2 * 320) + b"\x01" + bytes(8000 - 641))         # row 2
    assert MW.picture_hash(body, mask, vice) != h0
    pen = dict(raw, colram=bytes(80) + b"\x05" + bytes(919))                        # row 2's pens
    assert MW.picture_hash(pen, mask, vice) != h0


# W16-E: the return's display check counts only the planes BPLCON0 enables
_WB = {"bplcon0": 0xA200, "bplpt": [0x3AB8, 0x8AB8, 0, 0, 0, 0]}


def test_display_back_ignores_a_disabled_planes_stale_pointer():
    # W16-E u64e-click / c64u-click run 1: the UI's BPL3PT left behind at BPU 2
    st2 = {"bplcon0": 0xA200, "bplpt": [0x3AB8, 0x8AB8, 0x1F44B8, 0, 0, 0]}
    assert MW.display_back(_WB, st2)
    # the old rule (all six pointers) failed this real return
    assert (st2["bplpt"], st2["bplcon0"]) != (_WB["bplpt"], _WB["bplcon0"])


def test_display_back_still_fails_on_a_used_plane_or_bplcon0():
    for i in (0, 1):                    # red: an ENABLED plane's pointer differs
        pts = list(_WB["bplpt"])
        pts[i] = 0x1F44B8
        assert not MW.display_back(_WB, {"bplcon0": 0xA200, "bplpt": pts})
    # red: the UI's BPLCON0 (3 planes) left in place
    assert not MW.display_back(_WB, {"bplcon0": 0xB200,
                                     "bplpt": [0x3AB8, 0x8AB8, 0x1F44B8, 0, 0, 0]})
    # red norestore (fix2/red-norestore): the stand-in's BPLCON0 $0200 left in place
    assert not MW.display_back(_WB, {"bplcon0": 0x0200, "bplpt": list(_WB["bplpt"])})
