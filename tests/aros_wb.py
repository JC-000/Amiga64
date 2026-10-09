#!/usr/bin/env python3
"""W13-W: boot the user's Workbench 3.1 disk on AROS (HLE trackdisk).

docs/WAVE13_BRIEF.md W1-W4; contract docs/CONTRACTS.md "HLE trackdisk and
DF0"; design and observations build/evidence/w13-wb/PROGRESS.md.

Needs the AROS ROM cache (tools/aros/fetch.sh) and the user's ADF
(~/Documents/Amiga Disk Images/amiga-os-310-workbench.adf, read-only, never
copied into the repository or the evidence); without either every leg
prints a SKIP line and the run exits 0.

One emulator session per run (the brief's suite shape).  The boot:
  1. tests/aros_boot.Machine.stage: the whole REU zeroed, the ROM staged and
     checksummed (unchanged);
  2. the disk at TD_ADF (rc machine.inc): VICE stages the image with the
     overlay applied (test_load_chunk); an Ultimate LOAD_REUs the ADF from
     <vol>/amiga64/ with the runner's ureu_ld (the byte count is checked by
     ureu.s) and writes the overlay's blocks with the harness's REU write;
     both: aros_sum of the 901,120 bytes = the host's image;
  3. td_on := 1, and a test-time variant of tests/aros/mboot.s that calls
     td_arm after snap_arm (the frozen driver is not edited) boots.
Milestones, from td.s's block watch (td_wa/td_whit: a chunk read from the
watched block; the host moves the watch after each hit):
  w1   block 0 read (the boot block)
  w2   the Startup-Sequence's first data block read (host/adf_overlay.py)
  wbi  Disk.info's first data block read: Workbench opened its window
  w3   the screen: a C64 conversion equal to one of the boot model's
       Workbench frames (tests/aros/model_frames.py --adf), >= 3 colours
The serial text must equal the boot model's (same disk, same script).

Legs: w (w1-w3), nohle (red: the same boot, BeginIO never patched),
resume (W4: suspend mid-boot and at the Workbench to <vol>/amiga64/
wb-snap-test.bin (deleted; --keep: wb-snap.bin, the demo snapshot), reset,
the REU zeroed, resume from the file: the disk is read after it, the
Workbench is back <= 20 s after the launch; a different td module is
refused), demo (resume wb-snap.bin and keep it running --hold-s seconds).

    python3 -m tests.aros_wb --prg build/X/runner.prg [--legs w]
        [--backend vice|u64] [--mhz 64] [--vol /SD] [--script trim]
I3 (W13-D/W13-I): boot_workbench(m, rom, Disk(raw, script), s, rep, vol)
boots to the Workbench in an open session (session(...) gives the
WbMachine); resume_workbench(m, rom, path, rep) resumes a snapshot file.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import adf_overlay as O  # noqa: E402
from host import machine  # noqa: E402
from tests import aros_boot as AB  # noqa: E402

TD_ADF = 0x200000                    # rc machine.inc (checked against the labels' build below)
ADF_LOCAL = os.path.expanduser("~/Documents/Amiga Disk Images/amiga-os-310-workbench.adf")
ADF_NAME = "amiga-os-310-workbench.adf"
EVID = os.path.join(ROOT, "build", "evidence", "w13-wb")
MB_LIMIT = 0xC200                    # mboot $C000-$C1FF (machine-mode area layout)
# --watch (a counter build, scratch mkwatch.sh: every runtime REC bank store
# jsr's $CDC0 with A = the bank): this stub counts banks $20-$2D (TD_ADF..
# TD_ADF_END) at $CDF0/1, i.e. any guest-originated REU access to the disk
# (CPU data, code refill, blitter, display fetch); td's own copy is not counted.
WATCH_AT, WATCH_N = 0xCDC0, 0xCDF0
WATCH_STUB = bytes.fromhex("08EEF2CDD003EEF3CDC920900CC92EB008EEF0CDD003EEF1CD2860")  # + all banks at $CDF2/3 (mod 64K: the positive control)
WATCH = [False]
IDLE_WAIT_S = 30.0                   # leg w: after Disk.info, before the frozen guest check
WB_BUDGET_S = 240.0                  # brief W3: trim, C64U 64 MHz, from the runner start
log = AB.log


# --------------------------------------------------------------------------
# the driver: mboot.s + td_arm (test-time variant; mboot.s is not edited)
# --------------------------------------------------------------------------

def wb_driver(labels_path: str, out_dir: str) -> tuple[bytes, dict]:
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    src = open(AB.DRV_SRC).read()
    anchor = "        jsr snap_arm ; W12-T\n"
    if src.count(anchor) != 1:
        raise RuntimeError("tests/aros/mboot.s: the snap_arm line moved")
    src = src.replace(anchor, anchor + "        jsr TD_ORG ; W13-W: the td module's td_arm (tests/aros_wb.py)\n")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "wbboot.s")
    with open(path, "w") as f:
        f.write(src)
    labels = Labels.from_file(labels_path)
    defs = []
    for n in AB.DRV_SYMS + ("TD_ORG",):
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    import subprocess
    obj, out, cfg, lbl = (os.path.join(out_dir, n) for n in
                          ("wbboot.o", "wbboot.bin", "wbboot.cfg", "wbboot.lbl"))
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n" % (AB.MB_ORG, MB_LIMIT - AB.MB_ORG))
    for cmd in (["ca65", "-g", *defs, "--include-dir", os.path.join(ROOT, "tests", "aros"),
                 "-o", obj, path], ["ld65", "-C", cfg, "-o", out, "-Ln", lbl, obj]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    lab = {}
    for line in open(lbl):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return open(out, "rb").read(), lab


# --------------------------------------------------------------------------
# machine-mode modules (buf_case: mboot $C000-$C1FF, td $C200-$C5FF, W13-D's
# spr $C600-$C8FF): built at test time against runner.lbl, ID-stamped,
# staged with the driver and re-staged before every resume
# --------------------------------------------------------------------------

import hashlib  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402


def module_syms(src: str) -> list[str]:
    """The -D symbols a module asks for (its `.ifndef X / .error "-D` lines)."""
    return re.findall(r'^\.ifndef (\w+)\n\.error "-D', src, re.M)


def build_module(src_path: str, org: int, end: int, id_off: int, labels_path: str,
                 out_dir: str, defines: tuple = ()) -> bytes:
    """Assemble *src_path* at *org* (must end <= *end*) with every symbol it
    asks for from runner.lbl, and stamp its ID: SHA-1[:4] of the binary
    with the 4 bytes at *id_off* zero, written there."""
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    labels = Labels.from_file(labels_path)
    src = open(src_path).read()
    defs = []
    for n in module_syms(src):
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    for d in defines:
        defs += ["-D", d]
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, os.path.splitext(os.path.basename(src_path))[0])
    with open(base + ".cfg", "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; RODATA: load = M, type = ro, "
                "optional = yes; }\n" % (org, end - org))
    for cmd in (["ca65", "-g", *defs, "-I", os.path.join(ROOT, "tests", "aros"),
                 "-o", base + ".o", src_path],
                ["ld65", "-C", base + ".cfg", "-o", base + ".bin", "-Ln", base + ".lbl",
                 base + ".o"]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    b = bytearray(open(base + ".bin", "rb").read())
    if org + len(b) > end:
        raise RuntimeError(f"{src_path}: ${org:04X}+{len(b)} > ${end:04X}")
    if b[id_off:id_off + 4] != bytes(4):
        raise RuntimeError(f"{src_path}: no zero ID field at +{id_off}")
    b[id_off:id_off + 4] = hashlib.sha1(bytes(b)).digest()[:4]
    return bytes(b)


def build_td(labels_path: str, out_dir: str, defines: tuple = ()) -> bytes:
    src = os.path.join(ROOT, "tests", "aros", "td.s")
    if TD_MUTANT[0]:                    # --td-mutant: host/tests/test_td.MUTANTS
        from host.tests.test_td import MUTANTS
        old, new = MUTANTS[TD_MUTANT[0]]
        text = open(src).read()
        assert text.count(old) == 1
        os.makedirs(out_dir, exist_ok=True)
        src = os.path.join(out_dir, f"td_{TD_MUTANT[0]}.s")
        with open(src, "w") as f:
            f.write(text.replace(old, new))
    return build_module(src, 0xC200, 0xC600, 3,
                        labels_path, out_dir, tuple(TD_DEFINES) + tuple(defines))


TD_MUTANT = [None]


# (name, builder(labels_path, out_dir) -> bytes, org, end).  W13-D adds
# ("spr", build_spr, 0xC600, 0xC900) at the merge.  Each module carries its
# own ID check against the MAIN state its arm routine set (td: td_mid).
MODULES: list = [("td", build_td, 0xC200, 0xC600)]
TD_DEFINES: list = []           # --td-define (evidence builds: TD_ZPJUNK=$A5)


class WbMachine(AB.Machine):
    def __init__(self, target, labels, prg: str):
        super().__init__(target, labels, prg)
        self.prg = os.path.abspath(prg)
        self.drv, self.dl = wb_driver(os.path.splitext(prg)[0] + ".lbl",
                                      os.path.join(os.path.dirname(self.prg), "aros-wb-drv"))
        lp = os.path.splitext(self.prg)[0] + ".lbl"
        mdir = os.path.join(os.path.dirname(self.prg), "aros-wb-mod")
        self.mods = [(n, org, fn(lp, mdir)) for n, fn, org, _end in MODULES]

    def load_driver(self) -> None:
        """mboot + td_arm, then every machine-mode module (also the path a
        resume's fresh load takes: tests/aros_resume.fresh_load)."""
        super().load_driver()
        if WATCH[0]:                    # before any staging calls the counted runtime
            self.w(WATCH_AT, WATCH_STUB)
        for n, org, b in self.mods:
            self.w(org, b)
            if self.r(org, len(b)) != b:
                raise RuntimeError(f"module {n} readback mismatch at ${org:04X}")

    def module_ids(self) -> dict:
        return {n: b[3:7].hex() for n, _org, b in self.mods}

    def b(self, name: str, n: int = 1) -> bytes:
        return self.r(self.lab(name), n)

    def td(self) -> dict:
        return {"nrd": struct.unpack("<H", self.b("td_nrd", 2))[0],
                "npas": self.b("td_npas")[0], "nqr": self.b("td_nqr")[0],
                "whit": self.b("td_whit")[0],
                "slot": int.from_bytes(self.b("td_slot", 3), "little"),
                "orig": int.from_bytes(self.b("td_orig", 3), "little"),
                "cyl": self.b("td_cyl")[0], "chg": self.b("td_chg")[0]}

    def watch_block(self, blk: int) -> None:
        a = TD_ADF + blk * O.BLOCK
        self.w(self.lab("td_wa"), bytes([(a >> 8) & 0xFE, a >> 16]))
        self.w(self.lab("td_whit"), b"\0")


# --------------------------------------------------------------------------
# the disk
# --------------------------------------------------------------------------

class Disk:
    def __init__(self, raw: bytes, script: str):
        self.raw, self.script = raw, script
        self.ov = O.make_overlay(raw, script)
        self.image = O.apply(raw, self.ov)
        a = O.Adf(self.image)
        di = a.lookup("Disk.info")
        self.blocks = {"w1": 0, "w2": self.ov.first_data,
                       "wbi": a.file_blocks(di)[0] if di else None}


def stage_disk(m: WbMachine, disk: Disk, vol: str | None) -> dict:
    """The image at TD_ADF, checked by aros_sum on the C64; then td_on."""
    from tests.harness_util import call_runner
    t0 = time.monotonic()
    out: dict = {"script": disk.script, "overlay_blocks": [k for k, _ in disk.ov.blocks]}
    if m.vice:
        from tests.run_program import Chunk, Session
        sess = Session(target=m.tg, labels=m.labels, timeout=600.0)
        sess.stage([Chunk(TD_ADF, disk.image)])
        m.load_driver()
        out["by"] = "test_load_chunk"
    else:
        name = f"{vol}/amiga64/{ADF_NAME}".encode()
        call_runner(m.tg, m.lab("dosio_uci_init"), 30.0)
        m.w(m.lab("dosio_name"), name)
        m.w(m.lab("dosio_nlen"), bytes([len(name)]))
        m.w(m.lab("ureu_ra"), TD_ADF.to_bytes(3, "little"))
        m.w(m.lab("ureu_rl"), struct.pack("<I", O.ADF_SIZE))
        m.w(m.lab("ureu_err"), b"\xAA")
        t1 = time.monotonic()
        call_runner(m.tg, m.lab("ureu_ld"), 300.0)
        out["load_reu_s"] = round(time.monotonic() - t1, 2)
        err = m.r(m.lab("ureu_err"), 1)[0]
        out["reply"] = m.r(m.lab("dosio_buf"), 40).split(b"\0")[0].decode("latin-1")
        if err:
            raise RuntimeError(f"LOAD_REU {name.decode()}: ureu_err {err} ({out['reply']!r})")
        for a, b in O.reu_writes(disk.ov, TD_ADF):
            m.t.socket_dma_reu_write(a, b)
        out["by"] = "ureu_ld + socket_dma_reu_write"
    m.w(AB.P + 4, struct.pack("<II", TD_ADF, O.ADF_SIZE))
    got = struct.unpack_from("<I", m.call("aros_sum"), 4)[0]
    want = machine.fletcher(disk.image)
    out.update(sum_c64=f"${got:08X}", sum_host=f"${want:08X}",
               stage_s=round(time.monotonic() - t0, 1))
    if got != want:
        raise RuntimeError(f"staged disk: aros_sum ${got:08X} != ${want:08X}")
    m.w(m.lab("td_on"), b"\1")
    return out


def enable_ci(m: WbMachine, rep: dict) -> None:
    """The UCI answers only with the Command Interface on (both devices
    keep it Disabled; set inside the hold, which restores it)."""
    if m.vice:
        return
    cl = m.t.client
    cat, item = "C64 and Cartridge Settings", "Command Interface"
    before = cl.get_config_value(cat, item)
    cl.set_config_item(cat, item, "Enabled")
    rep["ci"] = [before, cl.get_config_value(cat, item)]


# --------------------------------------------------------------------------
# the model's Workbench frames
# --------------------------------------------------------------------------

def model_wb(script: str) -> tuple[dict | None, str]:
    path = os.path.join(ROOT, "build", "aros", f"model_wb_{script}.json")
    if not os.path.exists(path):
        if not os.path.exists(AB.VENV_PY):
            return None, f"no s3probe venv at {AB.VENV_PY}"
        import subprocess
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = subprocess.run([AB.VENV_PY, os.path.join(ROOT, "tests", "aros", "model_frames.py"),
                            path, "--adf", ADF_LOCAL, "--startup", script],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode or not os.path.exists(path):
            return None, f"model_frames.py --adf failed: {r.stdout}{r.stderr}"
    with open(path) as f:
        return json.load(f), ""


# --------------------------------------------------------------------------
# the boot (I3)
# --------------------------------------------------------------------------

def boot_workbench(m: WbMachine, rom, disk: Disk, s: int, rep: dict,
                   vol: str | None = None, timeout: float = 1800.0,
                   want_screen: bool = True, t_start: float | None = None,
                   stop_after: str | None = None, after_stage=None) -> list[str]:
    """Stage, boot and watch to the Workbench.  rep gets the milestones
    (wall seconds from *t_start*, default: the staging start)."""
    t_start = time.monotonic() if t_start is None else t_start
    rep["stage"] = m.stage(rom)
    rep["disk"] = stage_disk(m, disk, vol)
    log(f"  staged: ROM {rep['stage']}, disk {rep['disk']}")
    if after_stage:
        after_stage()
    order = [k for k in ("w1", "w2", "wbi") if disk.blocks.get(k) is not None]
    if stop_after:
        order = order[:order.index(stop_after) + 1]
    m.watch_block(disk.blocks[order[0]])
    if WATCH[0]:
        m.w(WATCH_AT, WATCH_STUB)
        m.w(WATCH_N, bytes(4))
    m.start(s)
    t_boot = time.monotonic()
    rep["boot_start_s"] = round(t_boot - t_start, 1)
    mf, why = model_wb(disk.script)
    ref = (mf or {}).get("serial", "")
    at: dict = {}
    rep["at"] = at
    text, seen, k = bytearray(), 0, 0
    last_log = 0.0
    while k < len(order):
        el = time.monotonic() - t_start
        try:
            n, ring = m.serial(seen)
            seen, lost = AB.take(text, ring, seen, n, m.sersz)
        except AB.CaptureError as e:
            return [f"{AB.INFRA}serial capture: {e}"]
        if lost:
            rep["serial_lost"] = rep.get("serial_lost", 0) + lost
        got = bytes(text).decode("latin-1")
        if ref:
            bad = next((i for i in range(min(len(got), len(ref))) if got[i] != ref[i]), None)
            if bad is not None:
                rep["serial"] = got
                return [f"serial: byte {bad} is {got[bad]!r}, the model's is {ref[bad]!r}"]
        if m.b("td_whit")[0]:
            key = order[k]
            fr = m.frames()
            at[key] = {"wall_s": round(el, 1), "frame": fr, "block": disk.blocks[key],
                       "td": m.td()}
            log(f"  {key} (block {disk.blocks[key]}) at {el:.1f} s, guest frame {fr}")
            k += 1
            if k < len(order):
                m.watch_block(disk.blocks[order[k]])
            continue
        if m.r(AB.R, 1)[0] == 0xFF:
            res = m.r(AB.R, 16)
            rep["returned"] = {"st_halt": res[1], "st_reason": res[2],
                               "pc": struct.unpack_from("<I", res, 8)[0]}
            break
        if el > timeout:
            break
        if el - last_log > 30:
            last_log = el
            log(f"  ... {el:.0f} s, frame {m.frames()}, td {m.td()}, serial {len(text)} B")
        time.sleep(AB.POLL_VICE_S if m.vice else 0.25)
    rep["serial_bytes"] = len(text)
    rep["serial_equals_model_prefix"] = bool(ref) and ref.startswith(bytes(text).decode("latin-1"))
    rep["td_end"] = m.td()
    fails = [f"{key}: block {disk.blocks[key]} never read (frame {m.frames()}, "
             f"td {m.td()}, returned {rep.get('returned')})" for key in order if key not in at]
    rep["serial_text"] = bytes(text)
    rep["serial_seen"] = seen
    if fails or not want_screen or stop_after:
        return fails
    return fails + leg_screen(m, mf, why, rep, t_start, timeout)


def guest_check(m: WbMachine, sn, rep: dict, slot: int) -> list[str]:
    """On a FROZEN machine (REU-resident suspend; the Ultimate: after a reset
    and fresh load, the REU kept), *slot* = td_slot read before: read guest memory from
    the REU (public NDK offsets, as the boot model's WbTracker) and check
    (1) intuition's first screen is titled "Workbook" and has a window,
    (2) the patched BeginIO slot still holds move.b d0,$BFD100 (no
    SumLibrary/SetFunction undid it), (3) no MemHeader of SysBase->MemList
    covers TD_ADF..TD_ADF_END (nothing can be allocated over the disk)."""
    def rd(a, n):
        out = b""
        while len(out) < n:
            k = min(256, n - len(out))
            out += sn.reu_read(a + len(out), k)
        return out

    def l(a):
        return struct.unpack(">I", rd(a, 4))[0]

    def cstr(a):
        return rd(a, 40).split(b"\0")[0].decode("latin-1") if 0 < a < 1 << 24 else ""

    def walk(lh, limit=64):
        n = l(lh)
        while n and n < 1 << 24 and l(n) and limit:
            yield n
            n, limit = l(n), limit - 1

    out: dict = {}
    fails: list[str] = []
    sb = l(4)
    out["sysbase"] = f"${sb:06X}"
    if l(sb + 38) != (~sb) & 0xFFFFFFFF:
        return [f"guest: SysBase ${sb:06X} fails ChkBase"]
    out["slot"] = {"at": f"${slot:06X}", "bytes": rd(slot, 6).hex()}
    if rd(slot, 6) != bytes.fromhex("13C000BFD100"):
        fails.append(f"guest: the BeginIO slot at ${slot:06X} no longer holds the patch")
    ib = next((n for n in walk(sb + 378) if cstr(l(n + 10)) == "intuition.library"), None)
    scr = l(ib + 60) if ib else 0
    title = cstr(l(scr + 22)) if scr else ""
    win = l(scr + 4) if scr else 0
    out["screen"] = {"title": title, "first_window": f"${win:06X}"}
    if not title.startswith("Workbook") or not win:
        fails.append(f"guest: no Workbench screen with a window ({out['screen']})")
    mh = []
    for n in walk(sb + 322):
        lo, hi = l(n + 20), l(n + 24)
        mh.append((cstr(l(n + 10)), f"${lo:06X}-${hi:06X}"))
        if lo <= machine.TD_ADF_END and hi > machine.TD_ADF:
            fails.append(f"guest: MemHeader {mh[-1]} overlaps the disk")
    out["memheaders"] = mh
    rep["guest"] = out
    log(f"  guest at the Workbench: {out}")
    return fails


def leg_screen(m: WbMachine, mf, why: str, rep: dict, t_start: float,
               timeout: float) -> list[str]:
    """w3's hash part: a C64 conversion equal to a model Workbench frame
    with >= 3 visible colours, title-bar rows masked (model_frames
    title_mask_rows: the rows showing Screen.BarHeight + 1 lines, measured
    on the model).  PENDING (not a failure) while the model's frames show
    no picture: the fit view gains hires BPU 1-3 with W13-D; the supervisor
    regenerates the frames at the merge and this switches on by itself.
    The guest-side check (guest_check) is w3 meanwhile."""
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import model_frames as MF
    if mf is None:
        return [f"w3: no model reference: {why}"]
    vis = {h: v for h, v in mf["hashes"].items() if v.get("visible", 0) >= MF.MIN_VISIBLE}
    if not vis:
        rep["w3_hash"] = {"pending": "the model's Workbench frames show no picture "
                          f"(c2v decide why {sorted({v['why'] for v in mf['hashes'].values()})})"}
        log(f"  w3 hash: PENDING ({rep['w3_hash']['pending']})")
        return []
    variant = "vice" if m.vice else "u64"
    ok = {(v["masked"][variant] if "masked" in v else (h if m.vice else v["u64"])): v
          for h, v in vis.items()}
    seen: dict = {}
    while time.monotonic() - t_start < timeout:
        h, raw = AB.c64_hash(m)
        v0 = next(iter(vis.values()))
        rows = v0.get("mask_rows", [])
        if rows or "masked" in v0:
            bm, sc, cr = MF.masked(raw["bitmap"], raw["screen"], raw["colram"], rows)
            h = (MF.conv_hash(bm, sc, cr, raw["vic"]) if m.vice else
                 MF.conv_hash(bm, None, cr, raw["vic"], None if rows else raw["mck"]))
        seen.setdefault(h, m.frames())
        if h in ok:
            el = time.monotonic() - t_start
            v = ok[h]
            rep["w3_hash"] = {"wall_s": round(el, 1), "model_frame": v["frame"],
                              "visible": v["visible"], "frame": m.frames(), "mask_rows": rows}
            png = os.path.join(EVID, f"w3_{variant}.png")
            err = AB.m3_png(png, raw, v.get("vic_pens"))
            rep["w3_hash"]["png"] = err or png
            log(f"  w3 hash at {el:.1f} s: model frame {v['frame']}, {v['visible']} colours")
            return []
        time.sleep(2.0)
    rep["w3_hash"] = {"seen": {h[:12]: f for h, f in seen.items()}}
    return [f"w3: no conversion equal to a model Workbench frame ({len(seen)} distinct seen)"]


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------

def session(prg: str, backend: str, out: str, mhz: int, fn):
    if backend == "vice":
        from c64_test_harness import Labels, UnifiedManager, ViceConfig
        from tests.harness_util import REU_VICE_ARGS
        from tests.run_vectors import prepare_target
        prg = os.path.abspath(prg)
        labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
        cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=False,
                         extra_args=list(REU_VICE_ARGS))
        with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
            tg = mgr.acquire()
            log(f"VICE pid {getattr(tg, 'pid', '?')}")
            try:
                prepare_target(tg, prg)
                return fn(WbMachine(tg, labels, prg))
            finally:
                mgr.release(tg)
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench.u64_session import u64_hold
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    os.makedirs(out, exist_ok=True)
    with u64_hold(out) as hold:
        tg = hold["target"]
        tg.transport.set_speed(mhz)
        reboot(tg.transport.client)
        log(f"Ultimate {os.environ.get('U64_HOST')}: {mhz} MHz, rebooted")
        prepare_target(tg, prg)
        try:
            return fn(WbMachine(tg, labels, prg))
        finally:
            tg.transport.set_speed(1)
            reboot(tg.transport.client)


def run(a, rom, raw: bytes) -> tuple[list[str], dict]:
    rep: dict = {"prg": os.path.abspath(a.prg), "backend": a.backend, "script": a.script,
                 "host": os.environ.get("U64_HOST") if a.backend == "u64" else None,
                 "mhz": a.mhz if a.backend == "u64" else None}
    disk = Disk(raw, a.script)
    rep["blocks"] = disk.blocks
    s = a.s if a.s is not None else (AB.BOOT_S_VICE if a.backend == "vice" else AB.BOOT_S_U64)
    rep["mach_s"] = s
    t0 = time.monotonic()

    def body(m: WbMachine) -> list[str]:
        rep["session_s"] = round(time.monotonic() - t0, 1)   # lock wait + reboot + runner load
        t_run = time.monotonic()                             # milestones: from the runner start
        enable_ci(m, rep)
        fails: list[str] = []
        for leg in a.legs:
            r = rep.setdefault(leg, {})
            log(f"leg {leg}")
            if leg == "w":
                from tests import aros_resume as RS
                sn = RS.Snap(m, m.prg, RS.rom_id(rom))
                fails += boot_workbench(m, rom, disk, s, r, a.vol, a.timeout, t_start=t_run,
                                        after_stage=sn.declare_rid)
                if "wbi" in r.get("at", {}):
                    rep["wb_wall_s"] = r["at"]["wbi"]["wall_s"]
                    time.sleep(IDLE_WAIT_S)     # Workbench settles (model: idle ~0.9 M instr later)
                    r["td_idle"] = m.td()
                    if r["td_idle"]["nqr"]:
                        fails.append(f"td: {r['td_idle']['nqr']} non-quick read(s) passed "
                                     "to the native path (no disk DMA)")
                    _suspend(m, sn, None, r)    # frozen, REU-resident
                    slot = int.from_bytes(m.b("td_slot", 3), "little")
                    if not m.vice:              # a frozen Ultimate polls no mailbox: read the
                        RS.fresh_load(m, m.prg)  # REU after a reset (it survives: aros_resume)
                    fails += guest_check(m, sn, r, slot)
                    if WATCH[0]:
                        n, tot = struct.unpack("<HH", m.r(WATCH_N, 4))
                        r["watch_disk_bank_stores"] = n
                        r["watch_all_bank_stores_mod64k"] = tot
                        log(f"  watch: {n} guest REU bank store(s) into $200000-$2DBFFF "
                            f"({tot} of all banks, mod 65536: the counter runs)")
                        if not tot:
                            fails.append("watch: the counter never ran (not a counter build?)")
                        if n:
                            fails.append(f"watch: the guest touched the disk region ({n})")
                    r["serial_final_count"] = m.count()   # frozen: exact (no alert text)
            elif leg == "resume":
                fails += leg_resume(m, rom, disk, s, r, a.vol, a.timeout, a.keep)
            elif leg == "demo":
                fails += leg_demo(m, rom, r, a.vol, a.hold_s)
            elif leg == "nohle":
                fails += leg_nohle(m, rom, disk, s, r, a.vol, a.timeout)
            else:
                fails.append(f"unknown leg {leg}")
        return fails

    fails = session(a.prg, a.backend, a.out, a.mhz, body)
    return fails, rep


SNAP_TEST = "wb-snap-test.bin"         # created and deleted by leg resume
SNAP_DEMO = "wb-snap.bin"              # --keep: left on the device for the user's demo
RESUME_BUDGET_S = 20.0                 # brief W4 (C64U)


def _suspend(m: WbMachine, sn, path: str | None, rep: dict) -> int:
    """Suspend (Ultimate: + the file *path*); -> chip_sern when frozen."""
    from host import snapfmt as F
    if path:
        name = path.encode()
        m.w(m.lab("dosio_name"), name)
        m.w(m.lab("dosio_nlen"), bytes([len(name)]))
    m.w(sn.a_st, b"\0")                     # a previous suspend's status must not satisfy the wait
    t0 = time.monotonic()
    rq = F.RQ_SUS | (F.RQ_FILE if path else 0)
    sn.request(rq)
    want = F.ST_SUSF if path else F.ST_SUS
    v = sn.wait_st(lambda v: v == want or v in F.ST_ERR, 300.0)
    rep["suspend_s"], rep["suspend_st"] = round(time.monotonic() - t0, 2), f"${v:02X}"
    if v != want:
        raise RuntimeError(f"suspend failed: snap_st ${v:02X} ({F.ST_ERR.get(v)})")
    rep["frozen_frame"] = m.frames()
    return m.count()


def _resume(m: WbMachine, prg: str, rid: bytes, path: str | None, rep: dict):
    """Reset + fresh runner load (+ the whole REU zeroed when a file holds
    the snapshot), resume; -> (Snap, seconds of the resume call)."""
    from host import snapfmt as F
    from tests import aros_resume as RS
    RS.fresh_load(m, prg)
    if path:
        m.w(AB.P, bytes([0, 0]))
        m.call("aros_fill")
        name = path.encode()
        m.w(m.lab("dosio_name"), name)
        m.w(m.lab("dosio_nlen"), bytes([len(name)]))
    sn = RS.Snap(m, prg, rid)
    sn.declare_rid()
    rep["t_launch"] = time.monotonic()        # = aros_resume m3file's t2: snap_resume launched
    v, dt = sn.resume(F.RQ_FILE if path else 0, timeout=300.0)
    rep["resume_st"], rep["resume_s"] = f"${v:02X}", round(dt, 2)
    if v != F.ST_RES:
        raise RuntimeError(f"resume refused: ${v:02X} ({F.ST_ERR.get(v)})")
    return sn, dt


def leg_resume(m: WbMachine, rom, disk: Disk, s: int, rep: dict, vol: str | None,
               timeout: float, keep: bool) -> list[str]:
    """W4.  (a) Suspend mid-boot (right after w2), reset, resume: the
    resumed guest must still read the disk -- Workbench's Disk.info read
    is served (the staged disk travelled in the snapshot: the chip span
    covers TD_ADF) and the serial text continues the model's.  (b) Suspend
    at the Workbench, reset, resume: the guest runs again and the picture
    (the C64's conversion hash taken before the suspend) is back within
    RESUME_BUDGET_S of the launch.  Ultimate: through a file under
    <vol>/amiga64/ with the REU zeroed in between; VICE: REU-resident."""
    from tests import aros_resume as RS
    rid = RS.rom_id(rom)
    path = None if m.vice else f"{vol}/amiga64/{SNAP_DEMO if keep else SNAP_TEST}"
    fails: list[str] = []
    a = rep.setdefault("mid", {})
    sn = RS.Snap(m, m.prg, rid)
    f = boot_workbench(m, rom, disk, s, a, vol, timeout, stop_after="w2",
                       after_stage=sn.declare_rid)
    if f:
        return f
    n = _suspend(m, sn, path, a)
    a.pop("serial_text", None)
    a.pop("serial_seen", None)
    log(f"  mid: suspended after w2 in {a['suspend_s']} s at frame {a['frozen_frame']}")
    try:
        sn, dt = _resume(m, m.prg, rid, path, a)
        a.pop("t_launch", None)
        log(f"  mid: resumed in {dt:.2f} s")
        c = m.count()
        a["serial_count"] = {"frozen": n, "after_resume": c}
        if (c - n) & 0xFFFF > m.sersz:
            fails.append(f"resume: the serial count was not restored ({n} -> {c})")
        if m.vice:
            fails += RS.cwin_check(m, a)
        m.watch_block(disk.blocks["wbi"])
        t0 = time.monotonic()
        nrd0 = m.td()["nrd"]
        while time.monotonic() - t0 < timeout and not m.b("td_whit")[0]:
            time.sleep(AB.POLL_VICE_S if m.vice else 0.25)
        a["after"] = {"td": m.td(), "frames": m.frames(), "s": round(time.monotonic() - t0, 1)}
        if not m.b("td_whit")[0]:
            return fails + [f"resume: Disk.info never read after the resume ({a['after']})"]
        if a["after"]["td"]["nrd"] <= nrd0:
            fails.append("resume: no read served after the resume")
        log(f"  mid: Workbench read Disk.info {a['after']['s']} s after the resume, "
            f"{a['after']['td']['nrd'] - nrd0} reads served since")
        # (b) at the Workbench
        b = rep.setdefault("wb", {})
        time.sleep(10.0 if m.vice else 5.0)
        h0 = RS.fast_hash(m)
        b["hash_before"] = h0[:12]
        _suspend(m, sn, path, b)
        sn, dt = _resume(m, m.prg, rid, path, b)
        t1 = b.pop("t_launch")
        fr0 = m.frames()
        while time.monotonic() - t1 < max(RESUME_BUDGET_S * 3, 60.0):
            if m.frames() > fr0 + 2 and RS.fast_hash(m) == h0:
                break
            time.sleep(0.25)
        el = time.monotonic() - t1
        b["back_s"] = round(el, 2)
        b["frames_after"] = m.frames() - fr0
        b["td"] = m.td()
        log(f"  wb: resumed (call {dt:.2f} s), running with the picture back {el:.2f} s "
            f"after the launch")
        if m.frames() <= fr0 + 2 or RS.fast_hash(m) != h0:
            fails.append(f"resume: the Workbench did not come back ({b})")
        elif not m.vice and el > RESUME_BUDGET_S:
            fails.append(f"resume: {el:.1f} s > {RESUME_BUDGET_S:.0f} s")
        fails += refuse_other_module(m, rid, path, rep.setdefault("refuse", {}))
    finally:
        if path and not keep:
            from c64_test_harness import storage_delete_file
            rep["deleted"] = storage_delete_file(os.environ["U64_HOST"], path, missing_ok=True)
            log(f"  {path} deleted: {rep['deleted']}")
        elif path:
            rep["kept"] = path
    return fails


def resume_workbench(m: WbMachine, rom, path: str | None, rep: dict) -> float:
    """I3: resume a Workbench snapshot (the Ultimate: from the file *path*,
    e.g. <vol>/amiga64/wb-snap.bin; VICE: REU-resident) on a fresh runner
    load, the modules re-staged; -> seconds from the snap_resume launch
    until the guest runs again (frames advance)."""
    from tests import aros_resume as RS
    _resume(m, m.prg, RS.rom_id(rom), path, rep)
    t1 = rep.pop("t_launch")
    fr0 = m.frames()
    while time.monotonic() - t1 < 60 and m.frames() <= fr0 + 2:
        time.sleep(0.25)
    if m.frames() <= fr0 + 2:
        raise RuntimeError(f"resume: the guest does not run ({rep})")
    return time.monotonic() - t1


def leg_demo(m: WbMachine, rom, rep: dict, vol: str, hold_s: float) -> list[str]:
    """The user's demo: resume <vol>/amiga64/wb-snap.bin (left by
    `--legs resume --keep` on the final tree) and keep the machine running
    *hold_s* seconds before the hold restores the device."""
    path = f"{vol}/amiga64/{SNAP_DEMO}"
    el = resume_workbench(m, rom, path, rep)
    rep["running_s"] = round(el, 2)
    rep["td"] = m.td()
    log(f"  demo: {path} resumed, guest running {el:.2f} s after the launch; holding {hold_s:.0f} s")
    time.sleep(hold_s)
    return []


def refuse_other_module(m: WbMachine, rid: bytes, path: str | None, rep: dict) -> list[str]:
    """Suspend at the Workbench, then resume with a DIFFERENT td module
    staged (TD_VARIANT build): runtime/ops/td.s must refuse it -- the
    machine halts with st_halt 1, st_reason TD_HALT_MOD -- and serve no
    request through it."""
    from tests import aros_resume as RS
    sn = RS.Snap(m, m.prg, rid)
    _suspend(m, sn, path, rep)
    good = m.mods
    lp = os.path.splitext(m.prg)[0] + ".lbl"
    var = build_td(lp, os.path.join(os.path.dirname(m.prg), "aros-wb-mod-var"), ("TD_VARIANT=$5A",))
    m.mods = [("td", 0xC200, var)]
    try:
        rep["ids"] = {"snapshot": good[0][2][3:7].hex(), "staged": var[3:7].hex()}
        _resume(m, m.prg, rid, path, rep)
        rep.pop("t_launch", None)
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60 and m.r(0x78, 1)[0] != 1:
            time.sleep(0.25)
        rep["st_halt"], rep["st_reason"] = m.r(0x78, 1)[0], m.r(0x79, 1)[0]
        want = m.lab("TD_HALT_MOD")
        log(f"  other module: st_halt {rep['st_halt']}, st_reason ${rep['st_reason']:02X} "
            f"(want 1, ${want:02X}); ids {rep['ids']}")
        if (rep["st_halt"], rep["st_reason"]) != (1, want):
            return [f"refuse: a resume onto another td module was not refused ({rep})"]
        return []
    finally:
        m.mods = good


def leg_nohle(m: WbMachine, rom, disk: Disk, s: int, rep: dict, vol, timeout) -> list[str]:
    """Red evidence on the real code: the HLE never patches BeginIO (td_slot
    pre-set to a bank-$00 address no instruction reaches) -- the drive lines
    still answer, AROS's own trackdisk runs, there is no disk DMA: no block
    is ever served and no Workbench."""
    rep["stage"] = m.stage(rom)
    rep["disk"] = stage_disk(m, disk, vol)
    m.watch_block(0)
    m.start(s)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:          # td_arm has run once td_slot reads $FFFFFF
        if m.b("td_slot", 3) == b"\xff\xff\xff":
            break
        time.sleep(0.2)
    m.w(m.lab("td_slot"), b"\x02\x00\x00")     # "patched" at $000002: never matches
    lim = min(timeout, 600.0)
    while time.monotonic() - t0 < lim:
        if m.td()["nrd"]:
            break
        time.sleep(1.0)
    rep["td"] = m.td()
    rep["frames"] = m.frames()
    log(f"  nohle: {rep['td']} after {time.monotonic() - t0:.0f} s, frame {rep['frames']}")
    if rep["td"]["nrd"]:
        return ["nohle: a block was served with the HLE off"]
    return []


def session_log(a, rep: dict, fails: list[str]) -> None:
    """One line per device hold in EVID/SESSION.log (brief: every hold),
    with u64_hold's restore verdict from <out>/config_after.json."""
    try:
        with open(os.path.join(a.out, "config_after.json")) as f:
            ca = json.load(f)
        verdict = "IDENTICAL" if ca.get("diff_vs_before") == {} else f"DIFF {ca.get('diff_vs_before')}"
    except (OSError, ValueError) as e:
        verdict = f"config_after.json unreadable: {e}"
    os.makedirs(EVID, exist_ok=True)
    with open(os.path.join(EVID, "SESSION.log"), "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} aros_wb {os.environ.get('U64_HOST')} "
                f"{a.mhz} MHz legs={','.join(a.legs)} prg={os.path.basename(os.path.dirname(rep['prg']))} "
                f"wall={rep['wall_s']}s fails={len(fails)} config={verdict}\n")


def _term(signum, frame):
    raise KeyboardInterrupt(f"signal {signum}")   # unwinds through u64_hold's restore


def main(argv=None) -> int:
    import signal
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _term)
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default="w")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--vol", default="/SD", help="Ultimate storage volume (C64U /SD, U64E /USB1)")
    ap.add_argument("--script", default="trim", choices=sorted(O.SCRIPTS))
    ap.add_argument("--s", type=int, default=None)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=EVID)
    ap.add_argument("--keep", action="store_true",
                    help="leg resume: leave <vol>/amiga64/wb-snap.bin (the demo snapshot)")
    ap.add_argument("--td-define", action="append", default=[],
                    help="assemble the td module with -D X (evidence builds, e.g. TD_ZPJUNK=$A5)")
    ap.add_argument("--hold-s", type=float, default=300.0,
                    help="leg demo: seconds the resumed Workbench keeps running")
    ap.add_argument("--td-mutant", default=None,
                    help="red: build the td module as host/tests/test_td.MUTANTS[NAME]")
    ap.add_argument("--watch", action="store_true",
                    help="a counter build (scratch mkwatch.sh): fail if the guest touches TD_ADF")
    ap.add_argument("--tag", default=None, help="report name (default: backend-script)")
    a = ap.parse_args(argv)
    a.legs = [x for x in a.legs.split(",") if x]
    WATCH[0] = a.watch
    TD_DEFINES[:] = a.td_define
    TD_MUTANT[0] = a.td_mutant
    if a.vol.lower().strip("/") in ("flash", "temp") or not a.vol.startswith("/"):
        raise SystemExit(f"--vol {a.vol}: never Flash or Temp")
    rom, why = machine.find_rom()
    if rom is None:
        print(f"SKIP aros_wb: {why}")
        return 0
    if not os.path.exists(ADF_LOCAL):
        print(f"SKIP aros_wb: no ADF at {ADF_LOCAL}")
        return 0
    with open(ADF_LOCAL, "rb") as f:
        raw = f.read()
    os.makedirs(a.out, exist_ok=True)
    t0 = time.monotonic()
    fails, rep = run(a, rom, raw)
    rep["wall_s"] = round(time.monotonic() - t0, 1)
    rep["fails"] = fails
    if a.backend == "u64":
        session_log(a, rep, fails)
    tag = a.tag or f"{a.backend}-{a.script}"
    with open(os.path.join(a.out, f"aros_wb-{tag}.json"), "w") as f:
        json.dump(rep, f, indent=1, default=str)
    for x in fails:
        print(f"FAIL {x}")
    print(f"aros_wb {','.join(a.legs)}: {'PASS' if not fails else f'FAIL ({len(fails)})'} "
          f"({rep['wall_s']} s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
