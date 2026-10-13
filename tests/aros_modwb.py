#!/usr/bin/env python3
"""W16-D: the MOD player launched from Workbench, end to end.

docs/WAVE16_BRIEF.md "W16-D" (3); the leg runs inside tests/aros_wb.py's
session (``--legs modwb --disk modwb``), on the disk that host/adf_files.
modwb_image makes from the user's ADF (the REU copy only):
Classes/DataTypes/amigaguide.datatype deleted (supervisor ruling (a),
2026-10-12), ModPlayer + ModPlayer.info + ModPlayer.mod added to the root.

Steps (scripted input throughout, inp_src = 1, the 1351 on the override):
  1. boot to the Workbench (aros_wb.boot_workbench: w1, w2, wbi, w3);
  2. the deleted file's drawer (Classes/DataTypes) is watched from here on
     (td_wa): the leg fails if any block of it is read (ruling (a));
  3. wait until td has served no read for TD_QUIET live frames (the volume
     icons drawn), double-click the volume icon, wait for td again, look
     for the "Workbench3.1:" window;
  4. find ModPlayer's icon in the guest's planes (the exact 2-plane image
     of host/wbicon.PLAYER_ROWS): first in the box its stored position
     gives (window + borders + do_CurrentX/Y), then in the whole window;
     double-click it;
  5. takeover, from LIVE C64-side state only (fix round 1, M2: no frozen
     guest read while the player runs -- a suspend on the Ultimate stops
     the guest ~10 s or more, and its resume re-arms spr and inpm, not
     W16-P's aud): COP1LC (chip_shd+$80) leaves the Workbench's value and
     stays away, the C64 picture changes; the player runs: VICE live reads
     the app anchor (ruling (c), $2DC040 -> W16-X's 'MWBS' struct: phase
     "running", frames advancing) through GuestPeek, which does not stop
     the guest's time; the frozen path (the Ultimate, --e2e-frozen) sees
     the stand-in's COLOR00 alternate in chip_shd instead;
  6. the music (the real player only: W16-P's aud counters, live; pending
     until the merge);
  7. exit: a left click (--modwb-exit click) or none (song end / the
     stand-in's MAXF frames: --modwb-exit end);
  8. return: the app anchor is withdrawn, COP1LC is the Workbench's or
     GfxBase->gb_copinit, the display's bitplane pointers and BPLCON0 are
     the Workbench's again, the window list and layer count are as before
     the launch, no ModPlayer task; and the C64 picture (fix round 1, M1;
     round 2, R2-1: read live, before the return's frozen look): VICE live
     converts by force and must give the picture of step 4 (forced on every
     path, twice: settled_conv);
     the Ultimate (and VICE --e2e-frozen) must come back to step 4's
     picture and hold it; every picture hash masks the screen title bar's
     character rows (its free-memory text changes with the player's
     allocations: diag-vice-live), so the takeover's "the picture
     changed" is a change below the title bar;
  steps 4-8 run twice: the second launch shows the Workbench answers and
  the first run's resources came back.

Every wait is bounded in LIVE guest frames (fix round 1, H1: u64e4 spent
5 min of wall time in five frozen looks and gave the guest 172 frames);
the wall clock bounds the whole leg (fix round 2, R2-2: t_start + --timeout,
checked at every wait and look; WALL_NET_S per wait).  On the frozen path the
guest runs LIVE_GAP frames between two looks, and a look reads its pages
in bulk (FrozenReader).

Until W16-X's program exists the tool is tests/modwb/standin.s (a
takeover without music); --modwb-prg PATH takes a real hunk executable.

Red (--modwb-mutant): noinfo (the disk without ModPlayer.info), noclick
(exit click never sent), norestore (the stand-in leaves its copper list),
wrongicon (double-clicks Prefs instead), noprof (the stand-in that checks
the profile block, in a session without one: no anchor), stuckpic (the
converter unhooked after the takeover: the C64 keeps the takeover
picture), keepanchor (the stand-in never withdraws the app anchor).  Each
must fail.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import time

from host import adf_files as F
from host import adf_overlay as O
from host import wbicon as W
from tests import aros_boot as AB
from tests import aros_wb as WB

ROOT = WB.ROOT
sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
import model_frames as MF  # noqa: E402
EVID = os.path.join(ROOT, "build", "evidence", "w16-d")
VASM = os.path.expanduser("~/.local/opt/vbcc/bin/vasmm68k_mot")
STANDIN_SRC = os.path.join(ROOT, "tests", "modwb", "standin.s")   # 68k: outside rescheck's 6502 globs
MUTANTS = ("noinfo", "noclick", "norestore", "wrongicon", "noprof", "stuckpic", "keepanchor")
SYSBASE_THISTASK, EXEC_LIBLIST, EXEC_TASKREADY, EXEC_TASKWAIT = 276, 378, 406, 420
LN_NAME = 10
GB_COPINIT = 38
# ruling (c): the APP ANCHOR at $2DC040 (W16-P's host/profile_block.py
# rules) -> W16-X's state struct ('MWBS': +6 state, +7 why, +56 frames)
ANCHOR_ADDR, ANCHOR_SIZE, ANCHOR_MAGIC = 0x2DC040, 16, b"MWBA"
RAM_SPANS = ((0x000000, 0x200000), (0xC00000, 0xDC0000))     # chip, slow
STATE_MAGIC, STATE_SIZE = b"MWBS", 60
A_STATE, A_WHY, A_FRAMES = 6, 7, 56
PHASE = {0: "loading", 1: "running", 2: "restored", 3: "done", 4: "failed"}
WHY = {0: None, 1: "lmb", 2: "song end", 3: "looped", 4: "mod_init refused"}
# fix round 1 (H1): every wait in LIVE guest frames; the wall clock is a net
TD_QUIET, TD_SETTLE_MAX = 120, 1500     # td served no read for TD_QUIET frames: reading is over
TD_FIRST_MAX = 600                      # after a user action: the first read comes within this
LIVE_GAP = 300                          # the frozen path: live frames between two looks
LIVE_STEP = 30                          # VICE live: a look costs nothing
VOL_WAIT_FRAMES = 1500                  # the volume icons drawn
OPEN_WAIT_FRAMES = 2000                 # the disk window open
ICON_WAIT_FRAMES = (900, 2400)          # the drawer filled (live, frozen); ~350 frames on VICE
TAKEOVER_FRAMES = 1500                  # COP1LC leaves the Workbench's
EXIT_FRAMES = (900, 15000)              # back after the click / the song's end
RETURN_PIC_FRAMES = 480                 # the Ultimate's return picture: h0, held, within this
WALL_NET_S = 900.0                      # per wait: sized for the U64E hold, never the bound
ICON_MARGIN = (16, 8)                   # the stored position's box: +- x, y screen pixels
log = AB.log


class LegDeadline(RuntimeError):
    """The leg ran past t_start + --timeout (fix round 2, R2-2)."""


class Deadline:
    """t_start + --timeout, the leg's whole budget (fix round 2, R2-2: a
    failing Ultimate run must not hold the device past the approved hold):
    call it at every wait and look; left() bounds a wait."""

    def __init__(self, t_start: float, timeout: float):
        self.at, self.timeout = t_start + timeout, timeout

    def left(self) -> float:
        return self.at - time.monotonic()

    def __call__(self, what: str) -> None:
        if self.left() < 0:
            raise LegDeadline(f"the leg's deadline (--timeout {self.timeout:.0f} s from the "
                              f"run start) passed at {what}")


def no_deadline(what: str) -> None:
    pass


# --------------------------------------------------------------------------
# the disk
# --------------------------------------------------------------------------

def build_standin(out_dir: str, defines: tuple = ()) -> bytes:
    """tests/modwb/standin.s as an AmigaOS hunk executable."""
    os.makedirs(out_dir, exist_ok=True)
    tag = "-".join(d.split("=")[0].lower() for d in defines) or "standin"
    out = os.path.join(out_dir, f"modwb_{tag}")
    cmd = [VASM, "-quiet", "-Fhunkexe", "-nosym", "-o", out]
    cmd += [f"-D{d}" if "=" in d else f"-D{d}=1" for d in defines]
    r = subprocess.run(cmd + [STANDIN_SRC], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"vasm: {r.stdout}{r.stderr}")
    with open(out, "rb") as f:
        return f.read()


class ModDisk(WB.Disk):
    """aros_wb.Disk with the player files (the same milestones: the boot
    never opens the root drawer, and Disk.info is unchanged)."""

    def __init__(self, raw: bytes, script: str, player: bytes, mod: bytes,
                 with_info: bool = True, standin: bool = True):
        super().__init__(raw, script)
        self.standin = standin          # tests/modwb/standin.s (flashes COLOR00)
        image = F.modwb_image(self.image, player, mod, with_info=with_info)
        self.ov = F.overlay_of(raw, image, f"{script}+modwb", self.ov.header, self.ov.data_blocks)
        self.image = O.apply(raw, self.ov)
        a, r = O.Adf(self.image), O.Adf(raw)
        self.dt_dir = r.lookup(os.path.dirname(F.MODWB_DROP))       # watched (ruling (a))
        self.drop_blocks = sorted([r.lookup(F.MODWB_DROP)] + r.file_blocks(r.lookup(F.MODWB_DROP)))
        self.icon = W.parse(a.read_file(a.lookup(F.MODWB_INFO))) if with_info else None
        self.files = {n: a.lookup(n) for n in (F.MODWB_TOOL, F.MODWB_INFO, F.MODWB_MOD)}


# --------------------------------------------------------------------------
# guest reads (public offsets: exec Node ln_Name +10; ExecBase ThisTask
# +276, LibList +378, TaskReady +406, TaskWait +420; Task tc_UserData +88;
# Window LeftEdge +4, TopEdge +6, Width +8, Height +10, Title +32,
# BorderLeft +54, BorderTop +55; GfxBase gb_copinit +38)
# --------------------------------------------------------------------------

def _cstr(read, a: int) -> str:
    return read(a, 40).split(b"\0")[0].decode("latin-1") if 0 < a < 1 << 24 else ""


def find_lib(read, name: str) -> int | None:
    sb = WB.l32(read, 4)
    n = WB.l32(read, sb + EXEC_LIBLIST)
    for _ in range(64):
        if not n or not WB.l32(read, n):
            return None
        if _cstr(read, WB.l32(read, n + LN_NAME)) == name:
            return n
        n = WB.l32(read, n)
    return None


def parse_anchor(raw: bytes, size: int = 1) -> dict | None:
    """The anchor as host/profile_block.parse_anchor (W16-P) reads it: None
    without the magic; ptr_ok False for a 0 or odd pointer or a *size*-byte
    struct not wholly inside one guest RAM span.  At the merge this body is
    replaced by an import of W16-P's (one reader, the ruling)."""
    try:
        from host import profile_block as PB
        return PB.parse_anchor(raw, size)
    except ImportError:
        pass
    if len(raw) < ANCHOR_SIZE or bytes(raw[:4]) != ANCHOR_MAGIC:
        return None
    ptr, seq, res = struct.unpack_from(">III", raw, 4)
    ok = (ptr != 0 and not ptr & 1 and size > 0
          and any(lo <= ptr and ptr + size <= hi for lo, hi in RAM_SPANS))
    return {"ptr": ptr, "seq": seq, "reserved": res, "ptr_ok": ok}


def app_state(read) -> dict:
    """THE accessor for the player's state (ruling (c)): the app anchor,
    then the struct it names.  {} while no anchor is published ("not
    running" / "has left"); "bad" when the anchor's pointer or the struct's
    magic is not acceptable (the host does not follow it)."""
    an = parse_anchor(read(ANCHOR_ADDR, ANCHOR_SIZE), STATE_SIZE)
    if an is None:
        return {}
    out = {"anchor": True, "ptr": an["ptr"], "seq": an["seq"]}
    if not an["ptr_ok"]:
        return {**out, "bad": "pointer"}
    st = read(an["ptr"], STATE_SIZE)
    if st[:4] != STATE_MAGIC:
        return {**out, "bad": "struct magic"}
    out.update(phase=PHASE.get(st[A_STATE], st[A_STATE]), why=WHY.get(st[A_WHY], st[A_WHY]),
               frames=struct.unpack_from(">I", st, A_FRAMES)[0])
    return out


def this_task(read) -> str:
    """Debug only: the name of SysBase->ThisTask."""
    sb = WB.l32(read, 4)
    task = WB.l32(read, sb + SYSBASE_THISTASK)
    return _cstr(read, WB.l32(read, task + LN_NAME)) if task else ""


def task_names(read) -> list[str]:
    """ThisTask and every task on TaskReady / TaskWait (exec lists)."""
    sb = WB.l32(read, 4)
    names = [_cstr(read, WB.l32(read, WB.l32(read, sb + SYSBASE_THISTASK) + LN_NAME))]
    for lh in (sb + EXEC_TASKREADY, sb + EXEC_TASKWAIT):
        n = WB.l32(read, lh)
        for _ in range(64):
            nxt = WB.l32(read, n)
            if not nxt:
                break
            names.append(_cstr(read, WB.l32(read, n + LN_NAME)))
            n = nxt
    return names


def windows(read) -> list[dict]:
    """The Workbench screen's windows with their boxes (screen pixels)."""
    sb = WB.l32(read, 4)
    ib = find_lib(read, "intuition.library")
    if not ib or not sb:
        return []
    scr = WB.l32(read, ib + 60)
    out, w = [], WB.l32(read, scr + 4)
    for _ in range(32):
        if not w or w >= 1 << 24:
            break
        le, te, wi, he = struct.unpack(">hhhh", read(w + 4, 8))
        bl, bt = read(w + 54, 2)
        out.append({"title": _cstr(read, WB.l32(read, w + 32)), "x": le, "y": te, "w": wi,
                    "h": he, "bl": bl, "bt": bt, "addr": w})
        w = WB.l32(read, w)
    return out


def guest_view(read, st: dict, win: dict | None, region: tuple | None = None,
               tasks: bool = True) -> dict:
    """One consistent read: windows, layers, the state block, gb_copinit,
    the tasks (if *tasks*: only the return needs them), and the player's
    icon (if *win*: searched in *region* first, then in the whole window)."""
    out = {"windows": windows(read), "screen": WB.wb_screen(read), "app": app_state(read)}
    if tasks:
        out.update(tasks=task_names(read), this_task=this_task(read))
    gb = find_lib(read, "graphics.library")
    out["copinit"] = WB.l32(read, gb + GB_COPINIT) & 0xFFFFFE if gb else None
    if win is not None:
        out["icon"], out["icon_how"] = icon_search(read, st, win, region)
    return out


def volume_icon(read, st: dict, title_lines: int) -> dict:
    """aros_wb.find_icon with its 17 rows of every plane prefetched."""
    _plane_rows(read, st, title_lines, title_lines + 17)
    return WB.find_icon(read, st, title_lines)


def icon_search(read, st: dict, win: dict, region: tuple | None) -> tuple:
    """The icon in *region* (~16 pages), else in the whole window (~75)."""
    ic = find_player_icon(read, st, win, region) if region else None
    if ic:
        return ic, "box"
    ic = find_player_icon(read, st, win)
    return ic, "window" if ic else None


def icon_view(read, st: dict, win: dict, region: tuple | None) -> dict:
    """The icon look of step 4: the icon, and only when it is there the
    state before the launch (the walks of guest_view cost a page each)."""
    ic, how = icon_search(read, st, win, region)
    if ic is None:
        return {"icon": None, "icon_how": None}
    return {**guest_view(read, st, None, tasks=False), "icon": ic, "icon_how": how}


def expected_box(dw: dict, disk: "ModDisk") -> tuple | None:
    """Where the stored position puts the icon (measured on VICE, vice1:
    window + (BorderLeft, BorderTop + 1) + do_CurrentX/Y), +- ICON_MARGIN;
    screen pixels (x0, y0, x1, y1)."""
    if not disk.icon:
        return None
    img = W.image_from_rows(W.PLAYER_ROWS)
    x = dw["x"] + dw["bl"] + disk.icon.x
    y = dw["y"] + dw["bt"] + 1 + disk.icon.y
    mx, my = ICON_MARGIN
    return x - mx, y - my, x + img.width + mx, y + img.height + my


class DispUnstable(RuntimeError):
    """disp_st did not read the same twice (or never valid) in DISP_TRIES reads."""


DISP_TRIES = 6


def stable_disp(read256, tries: int = DISP_TRIES) -> dict:
    """disp_st ($B800) read until two consecutive reads agree on everything
    but gen, the colours and dirty, with valid = COP_VALID (fix round 3,
    F1: the fields are written per copper move, so a host halt inside a
    copper run that writes BPLCON0 twice can see it half done; fix2/
    red-noinfo read BPU 0 once in about 45 looks).  The colours are left
    out because the stand-in alternates COLOR00 every frame.  A steady
    BPU 0 is taken as it is: norestore's $0200 is real."""
    from host import c2v_ref as R
    seen, prev = [], None
    for _ in range(tries):
        b = read256()
        st = R.unpack_disp_st(b)
        seen.append(f"${st['bplcon0']:04X}/{st['valid']:02X}")
        key = b[1:39] + b[103:104]       # dmacon .. ddfstop, valid
        if st["valid"] == R.COP_VALID and key == prev:
            return st
        prev = key
    raise DispUnstable(f"disp_st: no two equal valid reads in {tries} "
                       f"(BPLCON0/valid seen {seen})")


def _plane_rows(read, st: dict, y0: int, y1: int) -> list[list[int]]:
    """Planes 0..n-1 of the screen, rows y0..y1-1, each row a 640-bit int
    (a reader with prefetch() gets every page of them in one go)."""
    n = (st["bplcon0"] >> 12) & 7
    mod = st["bpl1mod"] - 0x10000 if st["bpl1mod"] & 0x8000 else st["bpl1mod"]
    rowb = 80 + (mod & ~1)
    pf = getattr(read, "prefetch", None)
    if pf and y1 > y0:
        pf([((st["bplpt"][p] & 0xFFFFFE) + y0 * rowb, (y1 - y0 - 1) * rowb + 80) for p in range(n)])
    return [[int.from_bytes(read((st["bplpt"][p] & 0xFFFFFE) + y * rowb, 80), "big")
             for y in range(y0, y1)] for p in range(n)]


def find_player_icon(read, st: dict, win: dict, region: tuple | None = None) -> dict | None:
    """ModPlayer's image in the window's rectangle (and in *region*, screen
    pixels (x0, y0, x1, y1), when given): every plane equals the icon's
    bits over all 48 x 22 pixels (planes >= 2 zero), or every plane is
    their complement (selected: GADGHCOMP inverts all the screen's planes
    in the box).  -> its box in screen pixels, centre and "selected", or
    None."""
    img = W.image_from_rows(W.PLAYER_ROWS)
    iw, ih = img.width, img.height
    rb = img.row_bytes
    mask = (1 << iw) - 1
    base = [[int.from_bytes(img.planes[p][y * rb:(y + 1) * rb], "big") >> (rb * 8 - iw)
             for y in range(ih)] for p in range(2)]
    x0, x1 = max(0, win["x"]), min(640, win["x"] + win["w"])
    y0 = max(0, win["y"])
    y1 = y0 + max(0, win["h"])                  # the last row searched
    if region:
        x0, y0 = max(x0, region[0]), max(y0, region[1])
        x1, y1 = min(x1, region[2]), min(y1, region[3])
    if x1 - x0 < iw or y1 - y0 + 1 < ih:
        return None
    rows = _plane_rows(read, st, y0, y1 + 1)
    n = len(rows)
    if n < 2:                           # fix round 3, F1: the image has 2 planes; with
        return None                     # none, all() over no plane matched anywhere
    pats = []
    for inv in (0, mask):
        pats.append((bool(inv), [[(base[p][r] if p < 2 else 0) ^ inv for r in range(ih)]
                                 for p in range(n)]))
    for y in range(0, y1 - y0 - ih + 1):
        for x in range(x0, x1 - iw + 1):
            sh = 640 - x - iw
            for sel, pat in pats:
                if all(((rows[p][y + r] >> sh) & mask) == pat[p][r]
                       for r in range(ih) for p in range(n)):
                    return {"x0": x, "y0": y0 + y, "x1": x + iw - 1, "y1": y0 + y + ih - 1,
                            "cx": x + iw // 2, "cy": y0 + y + ih // 2, "selected": sel}
    return None


# --------------------------------------------------------------------------
# guest readers (fix round 1, H1): live on VICE, frozen in bulk elsewhere
# --------------------------------------------------------------------------

class LiveReader:
    """VICE: aros_wb.GuestPeek, addresses on the 68000's 24-bit bus (a
    stale or half-built pointer wraps)."""

    def __init__(self, g):
        self.g = g
        g.fresh()

    def __call__(self, a: int, n: int) -> bytes:
        return self.g.read(a & 0xFFFFFF, n)

    def prefetch(self, ranges) -> None:
        self.g.prefetch([(a & 0xFFFFFF, n) for a, n in ranges])


# The bulk buffer: the VIC bitmap pages $E000-$FEFF.  fresh_load scrubs
# them ($55, tests/aros_resume.SCRUB_PAGES) and the resume rebuilds them
# (the forced conversion, CONTRACTS "Display refresh" I2), so they are
# don't-care while the guest is frozen.  A REU fetch writes the RAM under
# the KERNAL ROM; the host reads it with $01 = $35 on VICE.  On the
# Ultimate the read-back is checked against the page-by-page path once
# per session (BULK_OK); on a mismatch every look falls back to it.
BULK_BUF, BULK_MAX = 0xE000, 31
BULK_OK: list = [None]


def bulk_stub(org: int, pages: list[int], buf_hi: int, dirty: int) -> bytes:
    """6502 at *org* (one page): REU -> C64 fetch of each 256-byte REU
    *page* to buf_hi.i * 256, then mem_rec_dirty (C1), rts.  The tables
    (C64 high byte, REU address lo/mid/bank) sit at org+$80, 32 each."""
    n = len(pages)
    assert 0 < n <= BULK_MAX and buf_hi + n <= 0xFF
    tc, tl, tm, tb = (org + 0x80 + 0x20 * i for i in range(4))

    def absx(t: int) -> bytes:              # lda t,x
        return bytes([0xBD, t & 0xFF, t >> 8])
    code = bytearray([0xA2, 0x00])                                  # ldx #0
    loop = len(code)
    code += absx(tc) + b"\x8D\x03\xDF"                             # C64 address hi
    code += b"\xA9\x00\x8D\x02\xDF"                              #             lo 0
    code += absx(tl) + b"\x8D\x04\xDF"                             # REU lo
    code += absx(tm) + b"\x8D\x05\xDF"                             #     mid
    code += absx(tb) + b"\x8D\x06\xDF"                             #     bank
    code += b"\xA9\x00\x8D\x07\xDF\xA9\x01\x8D\x08\xDF"             # 256 bytes
    code += b"\xA9\x00\x8D\x0A\xDF\xA9\x91\x8D\x01\xDF"             # both count; fetch now
    code += bytes([0xE8, 0xE0, n])                                  # inx / cpx #n
    code += bytes([0xD0, (loop - len(code) - 2) & 0xFF])            # bne loop
    code += bytes([0x20, dirty & 0xFF, dirty >> 8, 0x60])           # jsr mem_rec_dirty / rts
    assert len(code) <= 0x80
    tabs = [bytes(buf_hi + i for i in range(n)), bytes(p & 0xFF for p in pages),
            bytes((p >> 8) & 0xFF for p in pages), bytes((p >> 16) & 0xFF for p in pages)]
    return bytes(code.ljust(0x80, b"\0") + b"".join(t.ljust(0x20, b"\0") for t in tabs))


class FrozenReader:
    """The guest's memory while it is frozen (the Ultimate; VICE with
    --e2e-frozen): a page cache; prefetch() fetches up to BULK_MAX pages
    per runner call into BULK_BUF and reads them back in one go (u64e4: a
    look of ~150 single-page calls took 65-75 s); a miss is one page."""

    def __init__(self, m, sn):
        self.m, self.sn, self.cache = m, sn, {}
        self.stats = {"single": 0, "bulk_calls": 0, "bulk_pages": 0}

    def _one(self, pg: int) -> bytes:
        self.stats["single"] += 1
        return self.sn.reu_read(pg, 256)

    def _c64(self, addr: int, n: int) -> bytes:
        m, t = self.m, self.m.t
        if m.vice:                      # halted from the first read to the resume
            from c64_test_harness import read_bytes
            port = read_bytes(t, 0x0001, 1)
            t.write_memory(0x0001, b"\x35", override="aros_modwb: RAM under the KERNAL")
            try:
                return bytes(read_bytes(t, addr, n))
            finally:
                t.write_memory(0x0001, port, override="aros_modwb: restore $01")
                t.resume()
        out = bytearray()               # the Ultimate: <= 1024 bytes per GET (RS.fast_hash)
        while len(out) < n:
            k = min(1024, n - len(out))
            out += t.read_memory(addr + len(out), k)
        return bytes(out)

    def _bulk(self, pages: list[int]) -> dict:
        from tests import aros_resume as RS
        from tests.harness_util import call_runner
        self.m.w(RS.STUB, bulk_stub(RS.STUB, pages, BULK_BUF >> 8, self.sn.a_dirty))
        call_runner(self.m.tg, RS.STUB, 30.0)
        raw = self._c64(BULK_BUF, 256 * len(pages))
        self.stats["bulk_calls"] += 1
        self.stats["bulk_pages"] += len(pages)
        return {pg: raw[256 * i:256 * (i + 1)] for i, pg in enumerate(pages)}

    def prefetch(self, ranges) -> None:
        pages = sorted({pg for a, n in ranges for pg in range((a & 0xFFFFFF) & ~0xFF,
                                                              (a & 0xFFFFFF) + n, 0x100)}
                       - set(self.cache))
        if BULK_OK[0] is False:
            return                      # the reads fetch page by page
        for i in range(0, len(pages), BULK_MAX):
            got = self._bulk(pages[i:i + BULK_MAX])
            if BULK_OK[0] is None:      # once per session: the read-back is the REU's
                pg = pages[i]
                BULK_OK[0] = got[pg] == self._one(pg)
                log(f"  bulk guest reads: {'checked' if BULK_OK[0] else 'MISMATCH, page by page'}")
                if not BULK_OK[0]:
                    return
            self.cache.update(got)

    def __call__(self, a: int, n: int) -> bytes:
        a &= 0xFFFFFF
        out = bytearray()
        while len(out) < n:
            x = a + len(out)
            pg = x & ~0xFF
            if pg not in self.cache:
                self.cache[pg] = self._one(pg)
            k = min(n - len(out), pg + 256 - x)
            out += self.cache[pg][x - pg:x - pg + k]
        return bytes(out)


def frozen_look(m, rom, fn, rep: dict, what: str):
    """aros_wb.guest_read_frozen with a FrozenReader and the time of each
    phase (rep["looks"]); an exception in fn still resumes (u64e2)."""
    from tests import aros_resume as RS
    t0 = time.monotonic()
    sn = RS.Snap(m, m.prg, RS.rom_id(rom))
    WB._suspend(m, sn, None, rep)
    t1 = time.monotonic()
    RS.fresh_load(m, m.prg)
    t2 = time.monotonic()
    rd = FrozenReader(m, sn)
    try:
        ok, out = True, fn(rd)
    except Exception as ex:             # noqa: BLE001 - resume first, raise after
        ok, out = False, ex
    t3 = time.monotonic()
    WB.resume_workbench(m, rom, None, rep, bytes([1, 0, 0]))    # scripted, no key
    t4 = time.monotonic()
    look = {"what": what, "suspend_s": round(t1 - t0, 1), "load_s": round(t2 - t1, 1),
            "read_s": round(t3 - t2, 1), "resume_s": round(t4 - t3, 1), **rd.stats}
    rep.setdefault("looks", []).append(look)
    log(f"  frozen look ({what}): {look}")
    if not ok:
        raise out
    return out


def td_reads(m) -> int:
    """td's served-read counter (td_nrd, 16 bits)."""
    return struct.unpack("<H", m.b("td_nrd", 2))[0]


def settle_td(m, e, quiet: int = TD_QUIET, limit: int = TD_SETTLE_MAX, first: int = 0,
              due=no_deadline, n0: int | None = None) -> dict:
    """LIVE: wait until td has served no read for *quiet* guest frames (at
    most *limit*): Workbook has read what it was going to read (fix round
    1, H1: before the first frozen look, a C64-side counter that costs
    nothing to read).  *first* > 0 (after a user action): first wait up to
    *first* frames for the first read, then for the plateau (fix round 2,
    R2-3: u64e5 saw no read in the 128 frames after the disk double-click,
    so "quiet" had measured nothing).  *n0*: td_reads() sampled before
    the action (fix round 3, F5: reads that start during the double-click
    count)."""
    f0 = m.frames()
    last = td_reads(m)
    if n0 is None:
        n0 = last
    while first and last == n0 and m.frames() - f0 < first:
        due("the td settle (first read)")
        e.frames_wait(10)
        last = td_reads(m)
    first_f = m.frames() - f0 if last != n0 else None
    f1 = last_f = m.frames()
    while m.frames() - last_f < quiet and m.frames() - f1 < limit:
        due("the td settle")
        e.frames_wait(10)
        n = td_reads(m)
        if n != last:
            last, last_f = n, m.frames()
    out = {"frames": m.frames() - f0, "reads": (last - n0) & 0xFFFF,
           "quiet": m.frames() - last_f >= quiet}
    if first:
        out["first_read_frame"] = first_f
    log(f"  td settle: {out}")
    return out


# --------------------------------------------------------------------------
# the leg
# --------------------------------------------------------------------------

def leg_modwb(m, rom, disk: ModDisk, s: int, rep: dict, vol, timeout: float,
              t_start: float, mutant: str | None = None, exit_mode: str = "click") -> list[str]:
    try:
        return _leg(m, rom, disk, s, rep, vol, timeout, t_start, mutant, exit_mode)
    except Exception as ex:             # noqa: BLE001 - report what the guest said
        import traceback
        rep["exception"] = repr(ex)
        rep["traceback"] = traceback.format_exc()
        rep["serial_after_boot"] = WB.e2e_serial(m, rep.get("boot", {}))
        log(f"  modwb: {ex!r}; serial after the boot: {rep['serial_after_boot'][-400:]!r}")
        return [f"modwb: {ex!r}"]


def _leg(m, rom, disk: ModDisk, s: int, rep: dict, vol, timeout: float,
         t_start: float, mutant: str | None, exit_mode: str) -> list[str]:
    from tests import aros_resume as RS
    sn = RS.Snap(m, m.prg, RS.rom_id(rom))
    due = Deadline(t_start, timeout)

    def stage_hook():
        sn.declare_rid()
        m.w(m.lab("inp_src"), b"\x01\0\0")          # scripted, idle

    rep["disk"] = {"overlay_blocks": len(disk.ov.blocks), "files": disk.files,
                   "dt_dir": disk.dt_dir, "icon_at": disk.icon and [disk.icon.x, disk.icon.y]}
    b = rep.setdefault("boot", {})
    fails = WB.boot_workbench(m, rom, disk, s, b, vol, timeout, t_start=t_start,
                              after_stage=stage_hook)
    if fails:
        return fails
    m.watch_block(disk.dt_dir)          # ruling (a): from here to the end
    e = WB.E2E(m)
    g = WB.GuestPeek(m) if m.vice and not WB.E2E_FROZEN[0] else None   # --e2e-frozen: the Ultimate's path
    mf, _ = WB.model_wb(disk.script)
    title_lines = (mf or {}).get("title_lines", 15)

    pics: dict = {}                     # VICE: hash -> raw picture (the PNGs of a failed return)
    def hash_now() -> str:
        """The C64 picture with the screen title bar's character rows
        masked (fix round 2: its free-memory text changes when the player
        allocates and frees; diag-vice-live: h0 and the return differed
        only there, C64 rows 6-15)."""
        raw = c64_raw(m)
        h = picture_hash(raw, mask, m.vice)
        if m.vice:
            pics.setdefault(h, raw)
        return h
    hash_now.pics = pics

    def disp() -> dict:
        return stable_disp(lambda: m.r(0xB800, 256))
    mask = MF.title_mask_rows(disp(), title_lines)
    rep["picture_mask_rows"] = mask

    def look(fn, what: str):
        """fn(read) on the guest: live on VICE, frozen (in bulk) on the
        Ultimate and with --e2e-frozen."""
        due(f"the {what} look")
        if g:
            return fn(LiveReader(g))
        return frozen_look(m, rom, fn, rep.setdefault("frozen", {}), what)

    def cop1lc() -> int:
        return int.from_bytes(m.r(m.lab("chip_shd") + 0x80, 4), "big") & 0xFFFFFE

    def dclick() -> None:
        for _ in range(2):
            e.mouse_btn(WB.J_FIRE)
            e.frames_wait(2)
            e.mouse_btn(0)
            e.frames_wait(2)

    e.mouse_on(s)
    frozen = g is None
    gap = LIVE_GAP if frozen else LIVE_STEP

    def gap_wait(k: int) -> None:       # the guest's own time between two looks
        due("a gap between looks")
        try:
            e.frames_wait(k, timeout=min(max(120.0, k * 2.0), max(1.0, due.left())))
        except RuntimeError:
            due("a gap between looks")
            raise

    # -- 3. open the disk ---------------------------------------------------------
    od = rep.setdefault("open_disk", {})
    # the volume icons may still be drawing right after the boot (u64e run
    # 1: one cluster, RAM Disk's, 30 s after w3): td quiet first, then look
    od["settle"] = settle_td(m, e, due=due)
    f0, t0 = m.frames(), time.monotonic()
    while True:
        st = disp()
        try:
            icon = look(lambda rd: volume_icon(rd, st, title_lines), "volume icons")
            break
        except RuntimeError as ex:
            if "cluster" not in str(ex) or m.frames() - f0 > VOL_WAIT_FRAMES or \
                    time.monotonic() - t0 > WALL_NET_S:
                raise
            log(f"  volume icons not drawn yet ({ex}); looking again in {gap} frames")
            od.setdefault("volume_icon_retries", []).append(str(ex)[:120])
            gap_wait(gap)
    od["icon"] = {k: icon[k] for k in ("x0", "x1", "y0", "y1", "cx", "cy")}
    nav = e.mgoto(icon["cx"] // 2, icon["cy"])
    od["goto"] = nav
    if not nav["ok"]:
        return fails + [f"open disk: the pointer did not reach the volume icon ({nav})"]
    nrd0 = td_reads(m)                  # F5: before the double-click
    dclick()
    want_title = O.Adf(disk.image).bname(O.Adf(disk.image).block(O.ROOT)) + ":"
    od["settle_open"] = settle_td(m, e, first=TD_FIRST_MAX, due=due, n0=nrd0)   # the root drawer read
    f1, t1, wins, tries = m.frames(), time.monotonic(), [], 0
    while True:
        wins = look(windows, "disk window")
        tries += 1
        if any(w["title"] == want_title for w in wins) or m.frames() - f1 > OPEN_WAIT_FRAMES or \
                time.monotonic() - t1 > WALL_NET_S:
            break
        gap_wait(gap)
    od.update(windows=[w["title"] for w in wins], looks=tries, frames=m.frames() - f1,
              wall_s=round(time.monotonic() - t1, 1))
    dw = next((w for w in wins if w["title"] == want_title), None)
    if dw is None:
        return fails + [f"open disk: no {want_title!r} window ({od})"]
    od["quiet"] = e.quiet(hash_now)
    log(f"  disk window {dw}")

    # -- 4.-8. launch, takeover, exit, return; twice: the second launch is
    #    the proof that the Workbench answers and got its memory back --------
    for n in (1, 2):
        rep.setdefault("runs", []).append({})
        f = launch_cycle(m, e, disk, rep["runs"][-1], look, disp, cop1lc, hash_now, dclick,
                         dw, frozen, gap_wait, due, mutant if n == 1 else None, exit_mode)
        fails += [f"run {n}: {x}" for x in f]
        if f:
            break
    whit = m.td()["whit"]
    rep["dt_dir_reads"] = whit
    if whit:
        fails.append(f"ruling (a): Classes/DataTypes (block {disk.dt_dir}) was read {whit} time(s)")
    if mutant:
        rep["mutant"] = mutant
    rep["serial_after_boot"] = WB.e2e_serial(m, b)
    if "lert" in rep["serial_after_boot"] or "Failure" in rep["serial_after_boot"]:
        fails.append(f"modwb: alert text on the serial line: {rep['serial_after_boot'][-300:]!r}")
    return fails


def launch_cycle(m, e, disk: ModDisk, rep: dict, look, disp, cop1lc, hash_now, dclick,
                 dw: dict, frozen: bool, gap_wait, due, mutant: str | None,
                 exit_mode: str) -> list[str]:
    """Steps 4-8 once: the icon, the double-click, the takeover, the exit
    and the return (see the module doc)."""
    fails: list[str] = []
    # -- 4. find the player's icon, double-click it --------------------------------
    la = rep.setdefault("launch", {})
    # Workbook fills the drawer for a while after the window opens, and the
    # C64 picture is no measure of it (CPU-drawn pixels convert late,
    # CONTRACTS "Display refresh"; red/noclick 1st try): td quiet, then look
    # until the icon is there, the guest running LIVE between the looks
    la["settle"] = settle_td(m, e, due=due)
    region = expected_box(dw, disk)
    la["region"] = region
    limit = ICON_WAIT_FRAMES[frozen]
    f0, t0, tries = m.frames(), time.monotonic(), 0
    while True:
        st = disp()
        tl = time.monotonic()
        v0 = look(lambda rd: icon_view(rd, st, dw, region), "icon")
        tries += 1
        log(f"  icon look {tries}: {v0['icon_how'] or 'not yet'} "
            f"({time.monotonic() - tl:.1f} s, live frame +{m.frames() - f0}, "
            f"BPLCON0 ${st['bplcon0']:04X})")
        if v0["icon"] or m.frames() - f0 > limit or time.monotonic() - t0 > WALL_NET_S:
            break
        gap_wait(LIVE_GAP if frozen else LIVE_STEP)
    la.update(icon_wait_frames=m.frames() - f0, icon_looks=tries, icon_how=v0["icon_how"],
              icon_wait_s=round(time.monotonic() - t0, 1))
    if not v0["icon"]:                  # what the window held (diagnosis)
        la["window_rows"] = look(lambda rd: {"st": {k: st[k] for k in ("bplcon0", "bpl1mod")},
                                             "bplpt": st["bplpt"][:3],
                                             "planes": [[r.to_bytes(80, "big").hex() for r in pl]
                                                        for pl in _plane_rows(rd, st, dw["y"], dw["y"] + dw["h"])]},
                                 "window dump")
        return [f"launch: ModPlayer's icon is not in the disk window ({tries} looks, "
                f"{la['icon_wait_frames']} live frames)"]
    la["before"] = {k: v0[k] for k in ("windows", "screen", "app", "copinit")}
    la["icon"] = v0["icon"]
    if disk.icon:
        la["icon_stored"] = [disk.icon.x, disk.icon.y]
        la["icon_offset"] = [v0["icon"]["x0"] - dw["x"] - disk.icon.x,
                             v0["icon"]["y0"] - dw["y"] - disk.icon.y]
    log(f"  player icon {v0['icon']} by {v0['icon_how']} (stored {la.get('icon_stored')}, "
        f"offset {la.get('icon_offset')})")
    target = prefs_target(v0, dw, disk) if mutant == "wrongicon" else v0["icon"]
    forced = m.vice and not frozen      # M1's VICE rule; --e2e-frozen takes the Ultimate's (R2-1)
    settled_conv(m, la.setdefault("conv_trace", []), hash_now if m.vice else None)  # M1: the
                                        # reference picture, converted by force on
                                        # every path (CPU-drawn pixels get no epoch of their own;
                                        # vice-frozen run 2: a live h0 was stale)
    cop0, h0, st0 = cop1lc(), hash_now(), disp()
    la.update(cop1lc_before=f"${cop0:06X}", hash_before=h0[:12], diag=display_diag(m, disp))
    nav = e.mgoto(target["cx"] // 2, target["cy"])
    la["goto"] = nav
    if not nav["ok"]:
        return [f"launch: the pointer did not reach the icon ({nav})"]
    dclick()

    # -- 5. takeover: LIVE C64-side state only (M2) ------------------------------------
    tk = rep.setdefault("takeover", {})
    f2, t2 = m.frames(), time.monotonic()
    while cop1lc() == cop0 and m.frames() - f2 < TAKEOVER_FRAMES and \
            time.monotonic() - t2 < WALL_NET_S:
        due("the takeover")
        e.frames_wait(5)
    tk.update(cop1lc=f"${cop1lc():06X}", frames=m.frames() - f2,
              wall_s=round(time.monotonic() - t2, 1))
    if cop1lc() == cop0:
        if mutant == "wrongicon":       # fix round 3, F2: evidence that Prefs opened
            tk["windows_after"] = [w["title"] for w in look(windows, "windows after")]
            log(f"  wrongicon: windows after the double-click {tk['windows_after']}")
        return [f"takeover: COP1LC stayed ${cop0:06X} ({tk})"]
    # the converter starts a conversion at most once per DISP_N = 24 guest
    # frames (CONTRACTS "Display refresh"), so the new picture may come
    # that much later (vice2 run 2: a fixed 10-frame wait saw the old one)
    f0, h1 = m.frames(), h0
    while m.frames() - f0 < 120 and h1 == h0:
        due("the takeover picture")
        e.frames_wait(5)
        h1 = hash_now()
    tk["picture_frames"] = m.frames() - f0
    # the player keeps the machine: COP1LC stays away from the Workbench's
    # over 64 live frames, and something of it moves
    cops, cols, va, vb = [], [], {}, {}
    if not frozen:                      # VICE live: GuestPeek, the guest's time runs on
        va = look(app_state, "app")
    f0 = m.frames()
    while m.frames() - f0 < 64:
        due("the takeover")
        e.frames_wait(4)
        cops.append(cop1lc())
        cols.append(display_diag(m, disp)["shd_color00"])
    if not frozen:
        vb = look(app_state, "app")
    tk.update(hash=h1[:12], app=[va, vb], cop1lc_seen=sorted({f"${c:06X}" for c in cops}),
              color00_seen=sorted(set(cols)), diag=display_diag(m, disp))
    log(f"  takeover after {tk['wall_s']} s ({tk['frames']} frames): COP1LC {tk['cop1lc']}, "
        f"picture {h1[:12]}, {tk['diag']}, COLOR00 {tk['color00_seen']}, app {va} -> {vb}")
    if h1 == h0:
        fails.append("takeover: the C64 picture did not change")
    if cop0 in cops:
        fails.append(f"takeover: COP1LC went back to the Workbench's ${cop0:06X} while the "
                     f"player should run ({tk['cop1lc_seen']})")
    if not frozen:
        if va.get("phase") != "running" or vb.get("phase") != "running":
            fails.append(f"takeover: the state block does not say running ({va}, {vb})")
        elif not vb["frames"] > va["frames"]:
            fails.append(f"takeover: the player's frame counter did not advance ({va}, {vb})")
    elif disk.standin and len(set(cols)) < 2:
        fails.append(f"takeover: the stand-in's COLOR00 did not alternate ({tk['color00_seen']})")
    elif not disk.standin:
        tk["alive"] = "frozen path: the real player's music counters (W16-P aud, step 6)"
    if mutant == "stuckpic":            # red: the converter unhooked (cop_dhook high
        m.w(m.lab("cop_dhook") + 1, b"\0")     # byte 0 = none): the C64 keeps this picture

    # -- 6. the music: W16-X / W16-P state (pending) ------------------------------------------
    rep["music"] = "pending: W16-P aud counters, read live (not checked yet)"

    # -- 7. exit ---------------------------------------------------------------------------------
    ex = rep.setdefault("exit", {"mode": exit_mode})
    if exit_mode == "click" and mutant != "noclick":
        e.mouse_btn(WB.J_FIRE)
        e.frames_wait(3)
        e.mouse_btn(0)
    f3, t3 = m.frames(), time.monotonic()
    limit = EXIT_FRAMES[exit_mode != "click"]
    while cop1lc() != cop0 and cop1lc() != (v0["copinit"] or -1) and \
            m.frames() - f3 < limit and time.monotonic() - t3 < WALL_NET_S * 2:
        due("the exit")
        e.frames_wait(5)
    ex.update(frames=m.frames() - f3, wall_s=round(time.monotonic() - t3, 1),
              cop1lc=f"${cop1lc():06X}")

    # -- 8. return ---------------------------------------------------------------------------------
    rt = rep.setdefault("return", {})
    e.frames_wait(30)
    st2 = disp()
    # the picture first (fix round 2, R2-1): live C64 reads, the user's
    # view, not what a frozen look's fresh load and resume leave behind
    pic = return_picture(m, e, hash_now, h0, h1, rt, forced, due)
    v2 = look(lambda rd: guest_view(rd, st2, None), "return")
    rt.update(cop1lc=f"${cop1lc():06X}", copinit=v2["copinit"] and f"${v2['copinit']:06X}",
              app=v2["app"], windows=[w["title"] for w in v2["windows"]],
              layers=v2["screen"]["layers"], tasks=v2["tasks"],
              bplpt=[f"${p:06X}" for p in st2["bplpt"]], bplcon0=f"${st2['bplcon0']:04X}")
    fails += pic
    if pic and m.vice:                  # diagnosis: what the C64 showed
        rt["png"] = {}
        for k, h in (("h0", h0), ("h1", h1), *((f"seen{i}", x) for i, x in enumerate(hash_now.pics))):
            if h in hash_now.pics and h[:12] not in rt["png"].values():
                png = os.path.join(EVID, "diag", f"{time.strftime('%H%M%S')}_{k}_{h[:12]}.png")
                os.makedirs(os.path.dirname(png), exist_ok=True)
                rt["png"][png] = AB.m3_png(png, hash_now.pics[h], None) or h[:12]
    log(f"  return: {rt}")
    if cop1lc() not in (cop0, v2["copinit"]):
        fails.append(f"return: COP1LC ${cop1lc():06X} is neither the Workbench's ${cop0:06X} "
                     f"nor gb_copinit {rt['copinit']}")
    if v2["app"]:                       # 'MWBA' cleared first on exit: gone
        fails.append(f"return: the app anchor is still published ({v2['app']})")
    if F.MODWB_TOOL in v2["tasks"]:
        fails.append(f"return: a task named {F.MODWB_TOOL} still exists ({v2['tasks']})")
    if not display_back(st0, st2):     # W16-E: the enabled planes only
        fails.append(f"return: the display is not the Workbench's (bplpt/bplcon0 "
                     f"{rt['bplpt']} {rt['bplcon0']}, before {[hex(p) for p in st0['bplpt']]} "
                     f"${st0['bplcon0']:04X})")
    wb0 = [w["title"] for w in v0["windows"]]
    if rt["windows"] != wb0 or rt["layers"] != v0["screen"]["layers"]:
        fails.append(f"return: windows {rt['windows']} / layers {rt['layers']}, before "
                     f"{wb0} / {v0['screen']['layers']}")
    return fails


def display_back(st0: dict, st2: dict) -> bool:      # W16-E
    """The Workbench's display is back: BPLCON0 and the pointers of the
    planes it enables (BPU, bits 14-12).  A disabled plane's pointer is not
    fetched: the real player's UI (3 planes) leaves BPL3PT behind and the
    Workbench's copper list (2 planes) never reloads it (u64e-click,
    c64u-click run 1: [$3AB8, $8AB8, $1F44B8] at BPLCON0 $A200)."""
    bpu = min((st0["bplcon0"] >> 12) & 7, 6)
    return (st2["bplcon0"] == st0["bplcon0"]
            and list(st2["bplpt"][:bpu]) == list(st0["bplpt"][:bpu]))


def picture_hash(raw: dict, mask: list[int], vice: bool) -> str:
    """The C64 picture's hash with the character rows *mask* (model_frames.
    title_mask_rows: the screen title bar) zeroed; the Ultimate's variant
    leaves the matrix and disp_mck out (model_frames.mask_hashes).
    Known limit (fix round 3, F4): on the Ultimate the matrix colours are
    seen only through disp_mck, which masking drops, so a pen or
    matrix-only difference is invisible there; the bitmap, colour RAM and
    VIC registers are compared."""
    return MF.mask_hashes(raw, mask)["vice" if vice else "u64"]


def c64_raw(m) -> dict:
    """aros_boot.c64_hash's raw reads (bitmap, screen, colram, vic, mck),
    the Ultimate's in GETs of <= 1024 bytes as aros_resume.fast_hash does."""
    if m.vice:
        return AB.c64_hash(m)[1]
    import c64_test_harness as H
    real = H.read_bytes

    def fast(t, addr, n):
        out = bytearray()
        while len(out) < n:
            k = min(1024, n - len(out))
            b = t.read_memory(addr + len(out), k)
            if len(b) != k:
                return real(t, addr, n)
            out += b
        return bytes(out)
    H.read_bytes = fast
    try:
        return AB.c64_hash(m)[1]
    finally:
        H.read_bytes = real


def settled_conv(m, trace: list | None = None, hash_now=None) -> None:
    """Two forced conversions in a row: a conversion already under way at
    the first epoch bump (decided on the old state) can end the first
    wait; the second is decided after it (fix round 2, vice-live run 2:
    the stand-in's in-flight blue frame passed for the forced Workbench).
    *trace*: the converter's state before and after each (diagnosis)."""
    def st(what: str) -> None:
        if trace is not None and getattr(m, "vice", False):
            d = m.r(0xB800, 106)
            trace.append({"at": what, "frame": m.frames(),
                          "nconv": struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0],
                          "epoch": d[105], "gen": d[0], "dirty": d[104],
                          "bplcon0": f"${d[3] | d[4] << 8:04X}",
                          "color00": f"${d[39] | d[40] << 8:04X}",
                          "hash": hash_now()[:12] if hash_now else None})
    st("before")
    for i in range(2):
        WB.force_conv(m, timeout=60.0)
        st(f"after force {i + 1}")


def return_picture(m, e, hash_now, h0: str, h1: str, rt: dict, forced: bool,
                   due=no_deadline) -> list[str]:
    """M1: the picture the user sees after the return, read LIVE before the
    return's frozen look (fix round 2, R2-1).  *forced* (VICE live): a
    forced conversion gives exactly the picture before the launch (forced
    too, both settled_conv).  Otherwise (the Ultimate; VICE --e2e-frozen,
    the same rule; h0 forced, the return not): the
    hash, read every 48 live frames for at most RETURN_PIC_FRAMES, must be
    the pre-launch h0 twice in a row (u64e5: back to h0 exactly in both
    cycles; "not the takeover's" alone passed a stuck converter, R2-1)."""
    if forced:
        try:
            settled_conv(m, rt.setdefault("conv_trace", []), hash_now)
            rt["forced"] = True
        except RuntimeError as ex:      # red stuckpic: nothing converts
            rt["forced"] = f"none: {ex}"
        h2 = hash_now()
        rt["hash"] = h2[:12]
        if h2 != h0:
            return [f"return: the C64 picture {h2[:12]} is not the Workbench's {h0[:12]} "
                    f"(the takeover's: {h1[:12]}; forced conversion: {rt['forced']})"]
        return []
    f0, hs = m.frames(), [hash_now()]
    while hs[-2:] != [h0, h0] and m.frames() - f0 < RETURN_PIC_FRAMES:
        due("the return picture")
        e.frames_wait(48)
        hs.append(hash_now())
    rt["hash"] = [h[:12] for h in hs]
    rt["hash_frames"] = m.frames() - f0
    if hs[-2:] == [h0, h0]:
        return []
    still = " (still the takeover's)" if hs[-1] == h1 else ""
    return [f"return: the C64 picture {hs[-1][:12]}{still} is not the Workbench's {h0[:12]} "
            f"held over 48 frames within {RETURN_PIC_FRAMES} live frames (the takeover's: "
            f"{h1[:12]}; seen {rt['hash']})"]


def display_diag(m, disp) -> dict:
    """The guest's display registers as the C64 sees them (chip_shd:
    BPLCON0 $100, COLOR00 $180, COP1LC $080), the converter's state
    (disp_st BPLCON0) and its conversion count."""
    shd = m.r(m.lab("chip_shd"), 0x184)
    try:
        dbc = f"${disp()['bplcon0']:04X}"
    except DispUnstable as ex:          # diagnosis only: report, do not fail the leg
        dbc = str(ex)
    return {"shd_bplcon0": f"${shd[0x100] << 8 | shd[0x101]:04X}",
            "shd_color00": f"${shd[0x180] << 8 | shd[0x181]:04X}",
            "shd_cop1lc": f"${int.from_bytes(shd[0x80:0x84], 'big'):06X}",
            "disp_bplcon0": dbc,
            "nconv": struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]}


def prefs_target(v0: dict, dw: dict, disk: ModDisk) -> dict:
    """mutant wrongicon: the Prefs drawer icon's centre from its stored
    position, with the measured offset of ours (window -> icon)."""
    a = O.Adf(disk.image)
    do = W.parse(a.read_file(a.lookup("Prefs.info")))
    ic = v0["icon"] or {"x0": dw["x"] + disk.icon.x, "y0": dw["y"] + disk.icon.y}
    ox, oy = ic["x0"] - disk.icon.x, ic["y0"] - disk.icon.y
    return {"cx": ox + do.x + do.image.width // 2, "cy": oy + do.y + do.image.height // 2}
