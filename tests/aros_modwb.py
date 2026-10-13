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
  3. double-click the volume icon; the "Workbench3.1:" window opens; wait
     until the drawer is drawn (E2E.quiet);
  4. find ModPlayer's icon in the guest's planes (the exact 2-plane image
     of host/wbicon.PLAYER_ROWS, inside the disk window), double-click it;
  5. takeover: COP1LC (chip_shd+$80) leaves the Workbench's value, the
     C64 picture changes, and the player's state (app_state(), the ONE
     accessor: ruling (c)'s app anchor at $2DC040 -> W16-X's 'MWBS'
     struct) says "running" with its frame counter advancing;
  6. the music (the real player only: W16-X / W16-P state, pending);
  7. exit: a left click (--modwb-exit click) or none (song end / the
     stand-in's MAXF frames: --modwb-exit end);
  8. return: the app anchor is withdrawn, COP1LC is the Workbench's or
     GfxBase->gb_copinit, the display's bitplane pointers and BPLCON0 are
     the Workbench's again, the window list and layer count are as before
     the launch;
  steps 4-8 run twice: the second launch shows the Workbench answers and
  the first run's resources came back (its icon is then selected).

Until W16-X's program exists the tool is tests/modwb/standin.s (a
takeover without music); --modwb-prg PATH takes a real hunk executable.

Red (--modwb-mutant): noinfo (the disk without ModPlayer.info), noclick
(exit click never sent), norestore (the stand-in leaves its copper list),
wrongicon (double-clicks Prefs instead), noprof (the stand-in that checks
the profile block, in a session without one: no anchor).  Each must fail.
"""

from __future__ import annotations

import os
import struct
import subprocess
import time

from host import adf_files as F
from host import adf_overlay as O
from host import wbicon as W
from tests import aros_boot as AB
from tests import aros_wb as WB

ROOT = WB.ROOT
EVID = os.path.join(ROOT, "build", "evidence", "w16-d")
VASM = os.path.expanduser("~/.local/opt/vbcc/bin/vasmm68k_mot")
STANDIN_SRC = os.path.join(ROOT, "tests", "modwb", "standin.s")   # 68k: outside rescheck's 6502 globs
MUTANTS = ("noinfo", "noclick", "norestore", "wrongicon", "noprof")
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
ICON_WAIT_FRAMES = 900                  # the drawer is filled within ~350 frames on VICE
ICON_WAIT_S = 300.0                     # and the wall-clock bound (frozen reads stop frames)
log = AB.log


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
                 with_info: bool = True):
        super().__init__(raw, script)
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


def guest_view(read, st: dict, win: dict | None) -> dict:
    """One consistent read: windows, layers, the state block, the tasks,
    gb_copinit, and the player's icon (if *win*: searched in that window)."""
    out = {"windows": windows(read), "screen": WB.wb_screen(read), "app": app_state(read),
           "tasks": task_names(read), "this_task": this_task(read)}
    gb = find_lib(read, "graphics.library")
    out["copinit"] = WB.l32(read, gb + GB_COPINIT) & 0xFFFFFE if gb else None
    if win is not None:
        out["icon"] = find_player_icon(read, st, win)
    return out


def _plane_rows(read, st: dict, y0: int, y1: int) -> list[list[int]]:
    """Planes 0..n-1 of the screen, rows y0..y1-1, each row a 640-bit int."""
    n = (st["bplcon0"] >> 12) & 7
    mod = st["bpl1mod"] - 0x10000 if st["bpl1mod"] & 0x8000 else st["bpl1mod"]
    rowb = 80 + (mod & ~1)
    return [[int.from_bytes(read((st["bplpt"][p] & 0xFFFFFE) + y * rowb, 80), "big")
             for y in range(y0, y1)] for p in range(n)]


def find_player_icon(read, st: dict, win: dict) -> dict | None:
    """ModPlayer's image in the window's rectangle: every plane equals the
    icon's bits over all 48 x 22 pixels (planes >= 2 zero), or every plane
    is their complement (selected: GADGHCOMP inverts all the screen's
    planes in the box).  -> its box in screen pixels, centre and
    "selected", or None."""
    img = W.image_from_rows(W.PLAYER_ROWS)
    iw, ih = img.width, img.height
    rb = img.row_bytes
    mask = (1 << iw) - 1
    base = [[int.from_bytes(img.planes[p][y * rb:(y + 1) * rb], "big") >> (rb * 8 - iw)
             for y in range(ih)] for p in range(2)]
    y0, y1 = max(0, win["y"]), max(0, win["y"]) + max(0, win["h"])
    rows = _plane_rows(read, st, y0, y1 + 1)
    n = len(rows)
    pats = []
    for inv in (0, mask):
        pats.append((bool(inv), [[(base[p][r] if p < 2 else 0) ^ inv for r in range(ih)]
                                 for p in range(n)]))
    for y in range(0, y1 - y0 - ih + 1):
        for x in range(max(0, win["x"]), min(640, win["x"] + win["w"]) - iw + 1):
            sh = 640 - x - iw
            for sel, pat in pats:
                if all(((rows[p][y + r] >> sh) & mask) == pat[p][r]
                       for r in range(ih) for p in range(n)):
                    return {"x0": x, "y0": y0 + y, "x1": x + iw - 1, "y1": y0 + y + ih - 1,
                            "cx": x + iw // 2, "cy": y0 + y + ih // 2, "selected": sel}
    return None


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
    from host import c2v_ref as R
    from tests import aros_resume as RS
    sn = RS.Snap(m, m.prg, RS.rom_id(rom))

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

    def hash_now() -> str:
        return AB.c64_hash(m)[0] if m.vice else RS.fast_hash(m)

    def disp() -> dict:
        return R.unpack_disp_st(m.r(0xB800, 256))

    def paged(rd):
        """A page cache over a frozen reader: one 256-byte REU read per
        page (u64e3: every l32 and plane row was its own runner call, a
        guest_view took minutes)."""
        cache: dict[int, bytes] = {}

        def r(a: int, n: int) -> bytes:
            out = bytearray()
            while len(out) < n:
                x = a + len(out)
                pg = x & ~0xFF
                if pg not in cache:
                    cache[pg] = rd(pg, 256)
                k = min(n - len(out), pg + 256 - x)
                out += cache[pg][x - pg:x - pg + k]
            return bytes(out)
        return r

    def look(fn):
        """fn(read) on the guest: live on VICE, frozen on the Ultimate."""
        def a24(read):                  # the 68000's 24-bit bus: a stale or
            return lambda a, n: read(a & 0xFFFFFF, n)   # half-built pointer wraps
        if g:
            g.fresh()
            return fn(a24(g.read))

        def caught(rd):                 # guest_read_frozen resumes only after fn
            try:                        # returns: an exception in fn would leave
                return True, fn(a24(paged(rd)))   # the guest frozen (u64e2: the volume-
            except Exception as ex:     # noqa: BLE001  icon retry then waited on a
                return False, ex        # stopped guest)
        ok, out = WB.guest_read_frozen(m, rom, caught, rep.setdefault("frozen", {}))
        if not ok:
            raise out
        return out

    def cop1lc() -> int:
        return int.from_bytes(m.r(m.lab("chip_shd") + 0x80, 4), "big") & 0xFFFFFE

    def dclick() -> None:
        for _ in range(2):
            e.mouse_btn(WB.J_FIRE)
            e.frames_wait(2)
            e.mouse_btn(0)
            e.frames_wait(2)

    e.mouse_on(s)
    # -- 3. open the disk ---------------------------------------------------------
    od = rep.setdefault("open_disk", {})
    # the volume icons may still be drawing right after the boot (U64E,
    # u64e run 1: one cluster, RAM Disk's, 30 s after w3): look again
    f0, t0 = m.frames(), time.monotonic()
    while True:
        st = disp()
        try:
            icon, before = look(lambda rd: (WB.find_icon(rd, st, title_lines), WB.wb_screen(rd)))
            break
        except RuntimeError as ex:
            if "cluster" not in str(ex) or m.frames() - f0 > ICON_WAIT_FRAMES or \
                    time.monotonic() - t0 > ICON_WAIT_S:
                raise
            log(f"  volume icons not drawn yet ({ex}); looking again")
            od.setdefault("volume_icon_retries", []).append(str(ex)[:120])
            e.frames_wait(60)
    od["icon"] = {k: icon[k] for k in ("x0", "x1", "y0", "y1", "cx", "cy")}
    nav = e.mgoto(icon["cx"] // 2, icon["cy"])
    od["goto"] = nav
    if not nav["ok"]:
        return fails + [f"open disk: the pointer did not reach the volume icon ({nav})"]
    dclick()
    want_title = O.Adf(disk.image).bname(O.Adf(disk.image).block(O.ROOT)) + ":"
    t1, wins = time.monotonic(), []
    while time.monotonic() - t1 < min(timeout, 600):
        e.frames_wait(20)
        wins = look(windows)
        if any(w["title"] == want_title for w in wins):
            break
    od.update(windows=[w["title"] for w in wins], wall_s=round(time.monotonic() - t1, 1))
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
                         dw, timeout, mutant if n == 1 else None, exit_mode)
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
                 dw: dict, timeout: float, mutant: str | None, exit_mode: str) -> list[str]:
    """Steps 4-8 once: the icon, the double-click, the takeover, the exit
    and the return (see the module doc)."""
    fails: list[str] = []
    # -- 4. find the player's icon, double-click it --------------------------------
    la = rep.setdefault("launch", {})
    # Workbook fills the drawer for a while after the window opens, and the
    # C64 picture is no measure of it (CPU-drawn pixels convert late,
    # CONTRACTS "Display refresh"; red/noclick 1st try: E2E.quiet held after
    # 60 frames with the icon not drawn yet): look until the icon is there
    f0, t0, tries = m.frames(), time.monotonic(), 0
    while True:                         # bounded in guest frames AND wall time
        st = disp()                     # (a frozen read stops the frame count)
        tl = time.monotonic()
        v0 = look(lambda rd: guest_view(rd, st, dw))
        tries += 1
        log(f"  icon look {tries}: {'found' if v0['icon'] else 'not yet'} "
            f"({time.monotonic() - tl:.1f} s, frame +{m.frames() - f0})")
        if v0["icon"] or m.frames() - f0 > ICON_WAIT_FRAMES or \
                time.monotonic() - t0 > ICON_WAIT_S:
            break
        e.frames_wait(30)
    la.update(icon_wait_frames=m.frames() - f0, icon_looks=tries,
              icon_wait_s=round(time.monotonic() - t0, 1))
    if not v0["icon"]:                  # what the window held (diagnosis)
        la["window_rows"] = look(lambda rd: {"st": {k: st[k] for k in ("bplcon0", "bpl1mod")},
                                             "bplpt": st["bplpt"][:3],
                                             "planes": [[r.to_bytes(80, "big").hex() for r in pl]
                                                        for pl in _plane_rows(rd, st, dw["y"], dw["y"] + dw["h"])]})
    la["before"] = {k: v0[k] for k in ("windows", "screen", "app", "copinit")}
    la["icon"] = v0["icon"]
    if disk.icon:
        la["icon_stored"] = [disk.icon.x, disk.icon.y]
        if v0["icon"]:
            la["icon_offset"] = [v0["icon"]["x0"] - dw["x"] - disk.icon.x,
                                 v0["icon"]["y0"] - dw["y"] - disk.icon.y]
    log(f"  player icon {v0['icon']} (stored {la.get('icon_stored')}, offset {la.get('icon_offset')})")
    target = prefs_target(v0, dw, disk) if mutant == "wrongicon" else v0["icon"]
    if target is None:
        return ["launch: ModPlayer's icon is not in the disk window"]
    cop0, h0, st0 = cop1lc(), hash_now(), disp()
    la.update(cop1lc_before=f"${cop0:06X}", hash_before=h0[:12], diag=display_diag(m, disp))
    nav = e.mgoto(target["cx"] // 2, target["cy"])
    la["goto"] = nav
    if not nav["ok"]:
        return [f"launch: the pointer did not reach the icon ({nav})"]
    dclick()

    # -- 5. takeover ---------------------------------------------------------------------
    tk = rep.setdefault("takeover", {})
    t2 = time.monotonic()
    while time.monotonic() - t2 < min(timeout, 600) and cop1lc() == cop0:
        e.frames_wait(5)
    tk["cop1lc"] = f"${cop1lc():06X}"
    tk["wall_s"] = round(time.monotonic() - t2, 1)
    if cop1lc() == cop0:
        return [f"takeover: COP1LC stayed ${cop0:06X} ({tk})"]
    # the converter starts a conversion at most once per DISP_N = 24 guest
    # frames (CONTRACTS "Display refresh"), so the new picture may come
    # that much later (vice2 run 2: a fixed 10-frame wait saw the old one)
    f0, h1 = m.frames(), h0
    while m.frames() - f0 < 120 and h1 == h0:
        e.frames_wait(5)
        h1 = hash_now()
    tk["picture_frames"] = m.frames() - f0
    va = look(app_state)
    e.frames_wait(20)
    vb = look(app_state)
    tk.update(hash=h1[:12], app=[va, vb], diag=display_diag(m, disp))
    if m.vice:                          # diagnostic: a forced conversion's picture
        try:                            # (a blank display converts nothing)
            WB.force_conv(m, timeout=30.0)
            tk["hash_forced"] = hash_now()[:12]
        except RuntimeError as ex:
            tk["hash_forced"] = f"none: {ex}"
        tk["diag_after_force"] = display_diag(m, disp)
    log(f"  takeover after {tk['wall_s']} s: COP1LC {tk['cop1lc']}, picture {h1[:12]} "
        f"(forced {tk.get('hash_forced')}), {tk['diag']}, app {va} -> {vb}")
    if h1 == h0:
        fails.append("takeover: the C64 picture did not change")
    if va.get("phase") != "running" or vb.get("phase") != "running":
        fails.append(f"takeover: the state block does not say running ({va}, {vb})")
    elif not vb["frames"] > va["frames"]:
        fails.append(f"takeover: the player's frame counter did not advance ({va}, {vb})")

    # -- 6. the music: W16-X / W16-P state (pending) ------------------------------------------
    rep["music"] = "pending: W16-X state block and W16-P aud state (not checked yet)"

    # -- 7. exit ---------------------------------------------------------------------------------
    ex = rep.setdefault("exit", {"mode": exit_mode})
    if exit_mode == "click" and mutant != "noclick":
        e.mouse_btn(WB.J_FIRE)
        e.frames_wait(3)
        e.mouse_btn(0)
    t3 = time.monotonic()
    limit = min(timeout, 300 if exit_mode == "click" else 1200)
    while time.monotonic() - t3 < limit and cop1lc() != cop0 and \
            cop1lc() != (v0["copinit"] or -1):
        e.frames_wait(5)
    ex["wall_s"] = round(time.monotonic() - t3, 1)
    ex["cop1lc"] = f"${cop1lc():06X}"

    # -- 8. return ---------------------------------------------------------------------------------
    rt = rep.setdefault("return", {})
    e.frames_wait(30)
    st2 = disp()
    v2 = look(lambda rd: guest_view(rd, st2, None))
    rt.update(cop1lc=f"${cop1lc():06X}", copinit=v2["copinit"] and f"${v2['copinit']:06X}",
              app=v2["app"], windows=[w["title"] for w in v2["windows"]],
              layers=v2["screen"]["layers"], tasks=v2["tasks"],
              bplpt=[f"${p:06X}" for p in st2["bplpt"]], bplcon0=f"${st2['bplcon0']:04X}")
    log(f"  return: {rt}")
    if cop1lc() not in (cop0, v2["copinit"]):
        fails.append(f"return: COP1LC ${cop1lc():06X} is neither the Workbench's ${cop0:06X} "
                     f"nor gb_copinit {rt['copinit']}")
    if v2["app"]:                       # 'MWBA' cleared first on exit: gone
        fails.append(f"return: the app anchor is still published ({v2['app']})")
    if F.MODWB_TOOL in v2["tasks"]:
        fails.append(f"return: a task named {F.MODWB_TOOL} still exists ({v2['tasks']})")
    if (st2["bplpt"], st2["bplcon0"]) != (st0["bplpt"], st0["bplcon0"]):
        fails.append(f"return: the display is not the Workbench's (bplpt/bplcon0 "
                     f"{rt['bplpt']} {rt['bplcon0']}, before {[hex(p) for p in st0['bplpt']]} "
                     f"${st0['bplcon0']:04X})")
    wb0 = [w["title"] for w in v0["windows"]]
    if rt["windows"] != wb0 or rt["layers"] != v0["screen"]["layers"]:
        fails.append(f"return: windows {rt['windows']} / layers {rt['layers']}, before "
                     f"{wb0} / {v0['screen']['layers']}")
    return fails


def display_diag(m, disp) -> dict:
    """The guest's display registers as the C64 sees them (chip_shd:
    BPLCON0 $100, COLOR00 $180, COP1LC $080), the converter's state
    (disp_st BPLCON0) and its conversion count."""
    shd = m.r(m.lab("chip_shd"), 0x184)
    st = disp()
    return {"shd_bplcon0": f"${shd[0x100] << 8 | shd[0x101]:04X}",
            "shd_color00": f"${shd[0x180] << 8 | shd[0x181]:04X}",
            "shd_cop1lc": f"${int.from_bytes(shd[0x80:0x84], 'big'):06X}",
            "disp_bplcon0": f"${st['bplcon0']:04X}",
            "nconv": struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]}


def prefs_target(v0: dict, dw: dict, disk: ModDisk) -> dict:
    """mutant wrongicon: the Prefs drawer icon's centre from its stored
    position, with the measured offset of ours (window -> icon)."""
    a = O.Adf(disk.image)
    do = W.parse(a.read_file(a.lookup("Prefs.info")))
    ic = v0["icon"] or {"x0": dw["x"] + disk.icon.x, "y0": dw["y"] + disk.icon.y}
    ox, oy = ic["x0"] - disk.icon.x, ic["y0"] - disk.icon.y
    return {"cx": ox + do.x + do.image.width // 2, "cy": oy + do.y + do.image.height // 2}
