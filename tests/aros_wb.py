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
def build_spr_mod(labels_path: str, out_dir: str) -> bytes:
    """W13-D's pointer module (supervisor, at the merge): tests/run_spr.build_spr."""
    from tests import run_spr as SP
    return SP.build_spr(os.path.join(out_dir, "spr"), labels_path)[0]


def build_inpm(labels_path: str, out_dir: str) -> bytes:   # W14-M
    """W14-M M2: the input module (tests/aros/inpm.s, $C980-$C9FE)."""   # W14-M
    return build_module(os.path.join(ROOT, "tests", "aros", "inpm.s"), 0xC980,   # W14-M
                        0xC9FF, 3, labels_path, out_dir)                    # W14-M


MODULES: list = [("td", build_td, 0xC200, 0xC600),
                 ("spr", build_spr_mod, 0xC600, 0xC900),
                 ("inpm", build_inpm, 0xC980, 0xC9FF)]               # W14-M


def arm_inpm(m, mod: bytes | None = None) -> None:      # W14-M
    """Arm the staged input module: its queue state idle, then inp_mon :=
    its ID byte 0 (inp.s inp_mod compares it with $C983 on every call).
    After staging AND after every resume: a resume comes up disarmed
    (snap.s zeroes inp_mon, as spr_vec), the launcher re-arms."""   # W14-M
    if mod is None:                                     # W14-M: the staged one
        mod = dict((n, b) for n, _o, b in m.mods)["inpm"]   # W14-M
    if mod[3] == 0:                                     # W14-M
        raise RuntimeError("inpm: ID byte 0 is 0 (cannot arm): change the source")
    m.w(m.lab("inp_mon"), b"\0")                       # W14-M: disarmed while set up
    m.w(m.lab("inp_mlast"), bytes(4))                   # W14-M: mlast, mq (2), md
    m.w(m.lab("inp_mon"), mod[3:4])                     # W14-M


def arm_spr(m) -> None:
    """Arm the staged pointer module in the CONTRACTS order (spr_vec hi 0,
    spr_id, vec lo, vec hi).  After staging, and again after every resume
    (snap.s comes up disarmed)."""
    from tests import run_spr as SP
    SP.arm(m.w, m.lab)
STUCK: list = [0]               # --stuck (demo legs)
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
    arm_spr(m)                          # supervisor (W13-D merge): the pointer module
    arm_inpm(m)                         # W14-M: the input module
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
                fails += leg_demo(m, rom, r, a.vol, a.hold_s, s)   # W14-M: s
            elif leg == "e2e":
                fails += leg_e2e(m, rom, disk, s, r, a.vol, a.timeout, t_run, a.e2e_mutant)
            elif leg == "demo-boot":
                fails += leg_demo_boot(m, rom, disk, s, r, a.vol, a.timeout, t_run, a.hold_s)
            elif leg == "cost":
                fails += (leg_cost(m, r) if a.legs[0] == "cost" else
                          ["cost: run it first in the session (--legs cost,...): see leg_cost"])
            elif leg == "nohle":
                fails += leg_nohle(m, rom, disk, s, r, a.vol, a.timeout)
            else:
                fails.append(f"unknown leg {leg}")
        return fails

    fails = session(a.prg, a.backend, a.out, a.mhz, body)
    return fails, rep


RESUME_MUTANT = [None]                 # --resume-mutant: aros_resume.MUTANTS name for resume (b)


EPOCH = 0xB800 + 105                   # rc ram.inc disp_st + the pixel epoch (I2)


def force_conv(m: WbMachine, timeout: float = 120.0) -> None:
    """A forced conversion: the pixel epoch += 1, then wait until the next
    conversion has finished (CPU-drawn pixels get no epoch of their own)."""
    nc = lambda: m.r(m.lab("disp_nconv"), 2)             # noqa: E731
    n0 = nc()
    m.w(EPOCH, bytes([(m.r(EPOCH, 1)[0] + 1) & 0xFF]))
    t0 = time.monotonic()
    while nc() == n0:
        if time.monotonic() - t0 > timeout:
            raise RuntimeError("no conversion after the epoch bump")
        time.sleep(0.05)


def forced_hash(m: WbMachine, timeout: float = 120.0) -> str:
    """The C64 picture right after a forced conversion."""
    from tests import aros_resume as RS
    force_conv(m, timeout)
    return RS.fast_hash(m)


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
        # (b) at the Workbench.  The reference is the FROZEN C64 picture, taken
        # right after a forced conversion (pixel epoch bumped: CPU-drawn pixels
        # get no epoch of their own, so a static screen can lag the guest's
        # planes) and the suspend.  The fit view's crop hides the title bar
        # (the model's mask rows for it are empty), so the compare is exact.
        # leg_resume calls _resume directly: the pointer module is not
        # re-armed here (no sprite reaches the hash).
        b = rep.setdefault("wb", {})
        time.sleep(10.0 if m.vice else 5.0)
        b["hash_forced"] = forced_hash(m)[:12]
        _suspend(m, sn, path, b)
        h0 = RS.fast_hash(m)                # frozen: nothing converts any more
        b["hash_before"] = h0[:12]
        prg_r = RS.build_mutant(m.prg, RESUME_MUTANT[0]) if RESUME_MUTANT[0] else m.prg
        ep0 = m.r(EPOCH, 1)[0] if m.vice else None     # frozen: the image's epoch
        b["resume_prg"] = RESUME_MUTANT[0] or "base"
        sn, dt = _resume(m, prg_r, rid, path, b)
        t1 = b.pop("t_launch")
        if ep0 is not None:             # rev-wb E2E L2 (as aros_resume m3): the resume
            ep1 = m.r(EPOCH, 1)[0]      # bumps the epoch; read before the guest's next
            b["epoch"] = {"frozen": ep0, "after_resume": ep1}       # blit can
            if ep1 == ep0:
                fails.append(f"resume: the pixel epoch was not bumped (${ep0:02X})")
        fr0 = m.frames()
        while time.monotonic() - t1 < max(RESUME_BUDGET_S * 3, 60.0):
            if m.frames() > fr0 + 2 and RS.fast_hash(m) == h0:
                break
            time.sleep(0.25)
        el = time.monotonic() - t1
        b["back_s"] = round(el, 2)
        b["frames_after"] = m.frames() - fr0
        b["hash_after"] = RS.fast_hash(m)[:12]
        b["td"] = m.td()
        log(f"  wb: resumed (call {dt:.2f} s), frozen picture {h0[:12]}, after {b['hash_after']} "
            f"({el:.2f} s after the launch)")
        if m.frames() <= fr0 + 2 or RS.fast_hash(m) != h0:
            fails.append(f"resume: the Workbench did not come back ({b})")
        elif not m.vice and el > RESUME_BUDGET_S:
            fails.append(f"resume: {el:.1f} s > {RESUME_BUDGET_S:.0f} s")
        if RESUME_MUTANT[0]:
            return fails                    # teeth: the refusal step needs the base PRG
        fails += refuse_other_module(m, rid, path, rep.setdefault("refuse", {}))
    finally:
        if path and not keep:
            from c64_test_harness import storage_delete_file
            rep["deleted"] = storage_delete_file(os.environ["U64_HOST"], path, missing_ok=True)
            log(f"  {path} deleted: {rep['deleted']}")
        elif path:
            rep["kept"] = path
    return fails


def resume_workbench(m: WbMachine, rom, path: str | None, rep: dict,
                     inp: bytes | None = None) -> float:
    """I3: resume a Workbench snapshot (the Ultimate: from the file *path*,
    e.g. <vol>/amiga64/wb-snap.bin; VICE: REU-resident) on a fresh runner
    load, the modules re-staged; -> seconds from the snap_resume launch
    until the guest runs again (frames advance)."""
    from tests import aros_resume as RS
    _resume(m, m.prg, RS.rom_id(rom), path, rep)
    t1 = rep.pop("t_launch")
    try:                                # W13-I (when merged): the snapshot carries
        a = m.lab("inp_src")            # inp_src; after a resume the launcher picks
    except KeyError:                    # the real joystick (0) for the demo
        a = None
    if a is not None and inp is None:   # the demo: the user's stick
        m.w(a, b"\0")
        rep["inp_src"] = 0
    elif a is not None:                 # scripted (e2e frozen reads): the image already
        m.w(a, inp)                     # carries inp_src/joy/key as suspended; this write
        rep["inp_src"] = inp[0]         # only confirms them (rev-wb E2E U2b)
    arm_spr(m)                          # supervisor: a resume comes up disarmed
    arm_inpm(m)                         # W14-M: the input module too (snap.s zeroes inp_mon)
    fr0 = m.frames()
    while time.monotonic() - t1 < 60 and m.frames() <= fr0 + 2:
        time.sleep(0.25)
    if m.frames() <= fr0 + 2:
        raise RuntimeError(f"resume: the guest does not run ({rep})")
    return time.monotonic() - t1


# --------------------------------------------------------------------------
# E2E (wave 13, on the merged tree): scripted input -> pointer -> Workbench
# --------------------------------------------------------------------------

PEEK_BUF, PEEK_PARK = 0xCD00, 0xCCF0   # buf_result, unused in machine mode (park outside the 256-byte buffer)
J_UP, J_DOWN, J_LEFT, J_RIGHT, J_FIRE = 1, 2, 4, 8, 16     # inp_joy (CONTRACTS "Input")
K_F1, K_CBM = 0x10, 0x20                                   # inp_key


class GuestPeek:
    """VICE: read guest memory (the REU) while the guest runs.  The 6510 is
    halted by the monitor, parked on JMP * at PEEK_PARK with I set, REC
    fetches REU -> PEEK_BUF run (one per 256-byte page, all pages of a
    prefetch in one park), and the REC address/length/control registers,
    the park bytes, PC/FL and $01 are put back exactly, so an interrupted
    REC sequence of the runtime continues with its own values (the method
    of c64_test_harness snapshot.extract_reu_contents, for <= 256 bytes)."""

    def __init__(self, m: WbMachine):
        self.m, self.t, self.cache = m, m.t, {}

    def fresh(self) -> None:
        self.cache.clear()

    def _fetch(self, pages: list[int]) -> dict:
        """One park for ALL of *pages* (256 bytes each): the guest does not
        run between them, so a multi-page read is one consistent picture
        (rev-wb E2E F3: page-by-page parks let the guest redraw between)."""
        t, ov = self.t, "aros_wb e2e: REU peek"
        for _ in range(200):            # x64sc runs a REC transfer from the CPU loop
            regs = t.read_registers()   # after the command write (harness #514): a
            if not t.read_memory(0xDF01, 1)[0] & 0x80:      # halt in between leaves
                break                   # the runtime's own transfer pending -- let it
            t.resume()                  # run first, or ours would replace it
        else:
            raise RuntimeError("peek: the runtime's REC transfer never completed")
        port = t.read_memory(0x0001, 1)
        io_in = port[0] & 4 and port[0] & 3
        if not io_in:
            t.write_memory(0x0001, b"\x35", override=ov)
        rec = t.read_memory(0xDF02, 9)
        park = t.read_memory(PEEK_PARK, 3)
        t.write_memory(PEEK_PARK, bytes([0x4C, PEEK_PARK & 0xFF, PEEK_PARK >> 8]), override=ov)
        t.set_registers({"PC": PEEK_PARK, "FL": regs["FL"] | 0x04})
        out = {}
        try:
            t.write_memory(0xDF0A, b"\0", override=ov)
            for addr in pages:
                t.write_memory(0xDF02, bytes([PEEK_BUF & 0xFF, PEEK_BUF >> 8, addr & 0xFF,
                                              (addr >> 8) & 0xFF, addr >> 16, 0, 1]), override=ov)
                t.write_memory(0xDF01, b"\x91", override=ov)
                t0 = time.monotonic()
                while True:             # the CPU only spins on the park meanwhile
                    t.resume()
                    if not t.read_memory(0xDF01, 1)[0] & 0x80:
                        break
                    if time.monotonic() - t0 > 5:
                        raise RuntimeError("peek: the REC fetch never ran")
                out[addr] = bytes(t.read_memory(PEEK_BUF, 256))
        finally:
            t.write_memory(0xDF02, rec[:7], override=ov)
            t.write_memory(0xDF0A, rec[8:9], override=ov)
            t.write_memory(PEEK_PARK, park, override=ov)
            t.set_registers({"PC": regs["PC"], "FL": regs["FL"]})
            if not io_in:
                t.write_memory(0x0001, port, override=ov)
            rc = getattr(t, "_resume_confirmed", None)      # a REC stall across a
            if callable(rc):                                # vsync leaves spare monitor
                rc(window=0.05)                             # traps (harness #516)
            else:
                t.resume()
        return out

    def prefetch(self, ranges: list[tuple[int, int]]) -> None:
        """Fetch every page of *ranges* ((addr, n)) not yet cached, all in
        ONE park."""
        pages = sorted({pg & 0xFFFFFF for a, n in ranges
                        for pg in range(a & ~0xFF, a + n, 0x100)} - set(self.cache))
        if pages:
            self.cache.update(self._fetch(pages))

    def read(self, addr: int, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            a = addr + len(out)
            pg = a & ~0xFF
            if pg not in self.cache:
                self.cache.update(self._fetch([pg]))
            k = min(n - len(out), pg + 256 - a)
            out += self.cache[pg][a - pg:a - pg + k]
        return bytes(out)


def l32(read, a: int) -> int:
    return struct.unpack(">I", read(a, 4))[0]


def wb_screen(read) -> dict:
    """Intuition's first screen, its windows and its layers (public NDK
    offsets: IntuitionBase +60 FirstScreen; Screen +4 FirstWindow, +22
    Title, +224 LayerInfo (whose first long is top_layer); Window +0
    NextWindow, +32 Title; Layer +4 back).  A menu Intuition shows lives in
    layers of its own on the screen: the boot model counts 3 at rest, 5
    with the Workbook menu down, 3 again after it closed."""
    def cstr(a):
        return read(a, 40).split(b"\0")[0].decode("latin-1") if 0 < a < 1 << 24 else ""
    sb = l32(read, 4)
    lh = sb + 378
    n, ib = l32(read, lh), None
    for _ in range(64):
        if not n or not l32(read, n):
            break
        if cstr(l32(read, n + 10)) == "intuition.library":
            ib = n
            break
        n = l32(read, n)
    if not ib:
        return {"title": "", "windows": [], "layers": 0}
    scr = l32(read, ib + 60)
    wins, w = [], l32(read, scr + 4)
    for _ in range(32):
        if not w or w >= 1 << 24:
            break
        wins.append(cstr(l32(read, w + 32)))
        w = l32(read, w)
    nl, ly = 0, l32(read, scr + 224)
    while ly and ly < 1 << 24 and nl < 64:
        nl += 1
        ly = l32(read, ly + 4)
    return {"title": cstr(l32(read, scr + 22)), "windows": wins, "layers": nl}


def find_icon(read, st: dict, title_lines: int) -> dict:
    """The Workbench disk icon from the guest's own screen planes: in the
    rows below the title bar and above the icons' labels (title_lines ..
    title_lines + 16), the columns with a non-background pixel form one
    cluster per icon; RAM Disk's is the first, the disk's the one with the
    largest centre (Workbook lays the volume icons out left to right).
    -> the box in hires screen pixels and its centre."""
    n = (st["bplcon0"] >> 12) & 7
    mod = st["bpl1mod"] - 0x10000 if st["bpl1mod"] & 0x8000 else st["bpl1mod"]
    rowb = 80 + (mod & ~1)              # hires fetch 80 bytes + the modulo
    rows = range(title_lines, title_lines + 17)
    cols = set()
    pix: dict[int, set] = {}
    for y in rows:
        line = [read((st["bplpt"][p] & 0xFFFFFE) + y * rowb, 80) for p in range(n)]
        for x in range(640):
            if any(pl[x >> 3] >> (7 - (x & 7)) & 1 for pl in line):
                cols.add(x)
                pix.setdefault(x, set()).add(y)
    clusters, cur = [], []
    for x in sorted(cols):
        if cur and x - cur[-1] > 8:
            clusters.append(cur)
            cur = []
        cur.append(x)
    if cur:
        clusters.append(cur)
    boxes = [{"x0": c[0], "x1": c[-1], "y0": min(min(pix[x]) for x in c),
              "y1": max(max(pix[x]) for x in c)} for c in clusters if c[-1] - c[0] >= 12]
    if len(boxes) < 2:
        raise RuntimeError(f"icons: {len(boxes)} cluster(s) below the title bar: {boxes}")
    b = dict(max(boxes, key=lambda b: b["x0"] + b["x1"]))
    b.update(cx=(b["x0"] + b["x1"]) // 2, cy=(b["y0"] + b["y1"]) // 2, all=boxes)
    return b


class E2E:
    """The scripted-input session helpers (inp_src = 1: inp_joy / inp_key)."""

    def __init__(self, m: WbMachine):
        self.m = m
        self.a_joy, self.a_key = m.lab("inp_joy"), m.lab("inp_key")
        self.st_args = m.lab("c2v_args") + 180

    def frames_wait(self, k: int, timeout: float = 120.0) -> None:
        f0, t0 = self.m.frames(), time.monotonic()
        while self.m.frames() < f0 + k:
            if time.monotonic() - t0 > timeout:
                raise RuntimeError(f"the guest did not run {k} frames in {timeout:.0f} s")
            time.sleep(0.02 if not self.m.vice else 0.05)

    def joy(self, v: int) -> None:
        self.m.w(self.a_joy, bytes([v]))

    def key(self, v: int) -> None:
        self.m.w(self.a_key, bytes([v]))

    def cur(self) -> dict:
        c = self.m.r(self.m.lab("disp_cur"), 71)
        u16 = lambda o: c[o] | c[o + 1] << 8        # noqa: E731
        return {"ddfstrt": u16(34), "diwstrt": u16(30), "bplcon0": u16(2)}

    def pos(self) -> tuple[int, int, int, int]:
        """(SX lores, SY line, POS, CTL) of the pointer as the module last
        read it from the guest (c2v_args + 180: POS/CTL, CONTRACTS D2)."""
        from host import c2v_ref as R
        sn = self.m.r(self.st_args, 7)
        pos, ctl = (sn[3] << 8) | sn[4], (sn[5] << 8) | sn[6]
        c = self.cur()
        hs, vs, _ = R.spr_decode(pos, ctl)
        return (hs + 1 - R.spr_first_pixel(c, True), vs - ((c["diwstrt"] >> 8) & 0xFF),
                pos, ctl)

    def vic(self) -> tuple[int, int, int]:
        r = self.m.r(0xD000, 0x20)
        return r[0] | (r[0x10] & 1) << 8, r[1], r[0x15]

    def goto(self, tx: int, ty: int, tol: int = 2, limit: int = 400) -> dict:
        """Closed loop on the pointer (lores SX, line SY): hold a direction
        while far, tap (one frame, then centre: the acceleration resets)
        when near."""
        steps = 0
        for steps in range(limit):
            sx, sy, _, _ = self.pos()
            dx, dy = tx - sx, ty - sy
            if abs(dx) <= tol and abs(dy) <= tol:
                break
            bits = 0
            if abs(dx) > tol:
                bits |= J_RIGHT if dx > 0 else J_LEFT
            if abs(dy) > tol:
                bits |= J_DOWN if dy > 0 else J_UP
            self.joy(bits)
            self.frames_wait(3 if max(abs(dx), abs(dy)) > 24 else 1)
            self.joy(0)
            self.frames_wait(2)
        sx, sy, _, _ = self.pos()
        return {"target": [tx, ty], "at": [sx, sy], "iterations": steps,
                "ok": abs(tx - sx) <= tol and abs(ty - sy) <= tol}

    # -- W14-M: the scripted 1351 (inp_mpot / inp_mb with the override) ------
    def mouse_on(self, s: int) -> None:                     # W14-M
        """Arm the 1351 sampler (mach slot 7, every 8192 phi2 = $2000 >> s E
        ticks) on the override pots, reference at (64, 64)."""  # W14-M
        import struct                                       # W14-M
        m = self.m                                          # W14-M
        a = m.lab("inp_msamp")                              # W14-M
        m.w(m.lab("mach_cbv") + 14, bytes([a & 0xFF, a >> 8]))   # W14-M: slot 7
        self.mpot = [0x40, 0x40]                            # W14-M: POTY, POTX
        m.w(m.lab("inp_mpot"), bytes(self.mpot))            # W14-M
        m.w(m.lab("inp_mok"), b"\0")                        # W14-M: next = reference
        # W14-M: 8192 phi2 (~120 Hz) is for a real 1351 on the Ultimate; on
        # VICE (s = 9: 16 E ticks, ~890 samples a frame) the passes would eat
        # the guest, so the scripted pots are sampled ~7 times a frame there
        per = 2048 if m.vice else max(1, 0x2000 >> s)       # W14-M
        m.w(m.lab("inp_mper"), struct.pack("<I", per))      # W14-M
        self.frames_wait(2)                                 # W14-M

    def mouse_move(self, dx: int, dy: int) -> None:         # W14-M
        """Move by (dx, dy) Amiga counts (|d| <= 31 per sample: the host
        waits a frame per step, many samples per frame)."""   # W14-M
        self.mpot[1] = (self.mpot[1] + 2 * dx) & 0x7E       # W14-M: X
        self.mpot[0] = (self.mpot[0] - 2 * dy) & 0x7E       # W14-M: Y counts up
        self.m.w(self.m.lab("inp_mpot"), bytes(self.mpot))  # W14-M
        self.frames_wait(1)                                 # W14-M

    def ramp_probe(self, frames: int = 34) -> dict:         # W14-M
        """The stick's ramp as AROS shows it: park at the left edge, then
        hold RIGHT for *frames* guest frames, the pointer read every frame;
        lores px per guest frame at the start and at the cap, and px/s at
        the C64U's s = 3 rate (72.08 / 8 frames/s, CONTRACTS "Machine
        loop": guest frames/s = phi2 / 14188 / 2^s)."""     # W14-M
        self.joy(J_LEFT)                                    # W14-M: park left
        self.frames_wait(40)                                # W14-M
        self.joy(0)                                         # W14-M
        self.frames_wait(4)                                 # W14-M
        f0 = self.m.frames()                                # W14-M
        xs = [(f0, self.pos()[0])]                          # W14-M
        self.joy(J_RIGHT)                                   # W14-M
        while self.m.frames() < f0 + frames:                # W14-M
            self.frames_wait(1)                             # W14-M
            xs.append((self.m.frames(), self.pos()[0]))     # W14-M
        self.joy(0)                                         # W14-M
        rate = lambda a, b: (b[1] - a[1]) / max(1, b[0] - a[0])   # noqa: E731  W14-M
        start = rate(xs[0], next((p for p in xs if p[0] >= f0 + 6), xs[-1]))   # W14-M
        cap = rate(next((p for p in xs if p[0] >= f0 + 32), xs[-2]), xs[-1]) \
            if xs[-1][0] >= f0 + 33 else None               # W14-M
        fps = 72.08 / 8                                     # W14-M: C64U, s = 3
        out = {"series": xs, "start_px_per_frame": round(start, 2),   # W14-M
               "cap_px_per_frame": cap and round(cap, 2),   # W14-M
               "start_px_s_c64u": round(start * fps, 1),    # W14-M
               "cap_px_s_c64u": cap and round(cap * fps, 1)}   # W14-M
        log(f"  ramp: {out['start_px_per_frame']} px/frame at the start, "
            f"{out['cap_px_per_frame']} at the cap (C64U s=3: {out['start_px_s_c64u']} / "
            f"{out['cap_px_s_c64u']} px/s); series {xs}")  # W14-M
        return out                                          # W14-M

    def mouse_btn(self, b: int) -> None:                    # W14-M: $10 left, $01 right
        self.m.w(self.m.lab("inp_ml"), bytes([b]))          # W14-M: M2 (module armed)
        self.m.w(self.m.lab("inp_mb"), bytes([b]))          # W14-M: M1 (no module)

    def mgoto(self, tx: int, ty: int, tol: int = 2, limit: int = 200) -> dict:   # W14-M
        """Closed loop on the pointer with the scripted 1351."""   # W14-M
        # W14-M: AROS moves the sprite from JOY0DAT a few frames after the
        # sampler integrated the move, so pos() lags: move at most half the
        # distance per step, wait 3 frames, and only stop on a position that
        # holds over another 4 frames (an overshoot of up to 23 lines when
        # the loop stopped on a stale position: DBG trace 2026-10-11)
        steps = 0                                           # W14-M
        for steps in range(limit):                          # W14-M
            sx, sy, _, _ = self.pos()                       # W14-M
            dx, dy = tx - sx, ty - sy                       # W14-M
            if abs(dx) <= tol and abs(dy) <= tol:           # W14-M
                self.frames_wait(4)                         # W14-M: still there?
                sx, sy, _, _ = self.pos()                   # W14-M
                if abs(tx - sx) <= tol and abs(ty - sy) <= tol:   # W14-M
                    break                                   # W14-M
                continue                                    # W14-M
            cl = lambda d: max(-24, min(24, d // 2 if abs(d) > 1 else d))   # noqa: E731  W14-M
            self.mouse_move(cl(dx), cl(dy))                 # W14-M
            self.frames_wait(2)                             # W14-M
        sx, sy, _, _ = self.pos()                           # W14-M
        return {"target": [tx, ty], "at": [sx, sy], "iterations": steps,   # W14-M
                "ok": abs(tx - sx) <= tol and abs(ty - sy) <= tol, "mouse": True}


def guest_read_frozen(m: WbMachine, rom, fn, rep: dict, key: int = 0):
    """The Ultimate (no live REU read): suspend REU-resident (frozen), reset
    + fresh load, run fn(reader) on the REU, resume, scripted input again."""
    from tests import aros_resume as RS
    sn = RS.Snap(m, m.prg, RS.rom_id(rom))
    _suspend(m, sn, None, rep)
    RS.fresh_load(m, m.prg)
    rd = lambda a, n: b"".join(sn.reu_read(a + o, min(256, n - o)) for o in range(0, n, 256))  # noqa: E731
    out = fn(rd)
    resume_workbench(m, rom, None, rep, bytes([1, 0, key]))    # scripted, *key* held throughout
    return out


MENU_DELAY = [0]                # --e2e-menu-delay frames before the menu step
E2E_FROZEN = [False]           # --e2e-frozen: guest reads by suspend/reset/resume on VICE too
E2E_MOUSE = [False]            # W14-M --e2e-mouse: the double-click by the scripted 1351
E2E_RAMP = [False]             # W14-M --e2e-ramp: measure the stick's ramp at the end
DEMO_MOUSE = [0]               # W14-M --mouse-port 1/2: the demo legs drive the user's 1351


def e2e_serial(m: WbMachine, b: dict) -> str:
    """The serial text the guest printed after the boot leg's capture
    (AROS reports an alert there)."""
    seen = b.get("serial_seen", 0)
    try:
        n, ring = m.serial(seen)
        text = bytearray()
        AB.take(text, ring, seen, n, m.sersz)
        return bytes(text).decode("latin-1")
    except Exception as ex:             # noqa: BLE001 - diagnostics only
        return f"<serial unreadable: {ex}>"


def leg_e2e(m: WbMachine, rom, disk: Disk, s: int, rep: dict, vol, timeout: float,
            t_start: float, mutant: str | None = None) -> list[str]:
    try:
        return _leg_e2e(m, rom, disk, s, rep, vol, timeout, t_start, mutant)
    except Exception as ex:             # noqa: BLE001 - report what the guest said
        rep["exception"] = repr(ex)
        rep["serial_after_boot"] = e2e_serial(m, rep.get("boot", {}))
        rep["frames"] = m.frames()
        log(f"  e2e: {ex!r}; serial after the boot: {rep['serial_after_boot'][-400:]!r}")
        return [f"e2e: {ex!r}"]


def _leg_e2e(m: WbMachine, rom, disk: Disk, s: int, rep: dict, vol, timeout: float,
             t_start: float, mutant: str | None = None) -> list[str]:
    """--legs e2e (the merged tree): boot (w3 hash), the pointer, a move, a
    double-click on the disk icon, the right button, F1 (see the module
    doc).  Scripted input throughout (inp_src = 1)."""
    from host import c2v_ref as R
    from tests import aros_resume as RS
    from tests import run_spr as SP
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import model_frames as MF
    sn = RS.Snap(m, m.prg, RS.rom_id(rom))

    def stage_hook():
        sn.declare_rid()
        m.w(m.lab("inp_src"), b"\x01\0\0")          # scripted, idle

    b = rep.setdefault("boot", {})
    fails = boot_workbench(m, rom, disk, s, b, vol, timeout, t_start=t_start,
                           after_stage=stage_hook)
    if fails:
        return fails
    if "w3_hash" not in b or "pending" in b["w3_hash"]:
        return ["e2e: w3 hash did not pass (no model frames with a picture?)"]
    e = E2E(m)
    g = GuestPeek(m) if m.vice and not E2E_FROZEN[0] else None
    mf, _ = model_wb(disk.script)
    title_lines = (mf or {}).get("title_lines", 15)

    def hash_now() -> str:
        return AB.c64_hash(m)[0] if m.vice else RS.fast_hash(m)

    def settle(limit: int = 20) -> None:
        """Until no conversion happens for 20 frames: a reference render of
        the guest's planes must see the planes the C64 converted."""
        nconv = lambda: m.r(m.lab("disp_nconv"), 2)        # noqa: E731
        for _ in range(limit):
            n0 = nconv()
            e.frames_wait(20)
            if nconv() == n0:
                return

    # -- 2. the pointer is visible, and where the guest puts it --------------
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120 and not e.vic()[2] & 7:
        time.sleep(0.5)
    p = rep.setdefault("pointer", {})
    vx, vy, en = e.vic()
    p.update(vic=[vx, vy], d015=en)
    if not en & 7:
        return fails + [f"pointer: $D015 = ${en:02X}, no sprite 0-2 enabled"]
    if m.vice:                          # the RAM under I/O: VICE's monitor only
        sp = SP.spr_read_live(m)
        p["blocks_nonempty"] = any(sp["blocks"])
        if not p["blocks_nonempty"]:
            fails.append("pointer: the sprite blocks at $D400 are empty")
    if g:                               # the guest's POS/CTL, live (VICE)
        shd = m.r(m.lab("chip_shd") + 0x120, 4)
        pt = ((shd[0] << 24 | shd[1] << 16 | shd[2] << 8 | shd[3]) & 0xFFFFFE)
        g.fresh()
        pw = g.read(pt, 4)
        pos, ctl = pw[0] << 8 | pw[1], pw[2] << 8 | pw[3]
        p["guest"] = {"spr0pt": f"${pt:06X}", "pos": f"${pos:04X}", "ctl": f"${ctl:04X}"}
    else:
        _, _, pos, ctl = e.pos()
        p["guest"] = {"pos": f"${pos:04X}", "ctl": f"${ctl:04X}", "from": "module cache"}
    crop = m.r(m.lab("disp_crop"), 1)[0]
    want = R.spr_place(e.cur(), pos, ctl, True, False, 0, crop, 0)
    p["want"] = want
    if "hidden" in want or (vx, vy) != (want["x"] | (want["msb"] & 1) << 8, want["y"]):
        fails.append(f"pointer: VIC sprite 0 at ({vx}, {vy}), the guest's POS/CTL map to {want}")
    log(f"  pointer: VIC ({vx}, {vy}) $D015 ${en:02X}, guest {p['guest']}, want {want}")

    # -- 3. move: right, then down ---------------------------------------------
    mv = rep.setdefault("move", {})
    N = 40
    for name, bit, axis in (("right", J_RIGHT, 0), ("down", J_DOWN, 1)):
        jd0 = m.r(m.lab("chip_shd") + 0x0A, 2)
        sx0, sy0, _, _ = e.pos()
        v0 = e.vic()
        f0, w0 = m.frames(), time.monotonic()
        e.joy(bit)
        e.frames_wait(N)
        e.joy(0)
        f1, w1 = m.frames(), time.monotonic()
        e.frames_wait(3)
        jd1 = m.r(m.lab("chip_shd") + 0x0A, 2)
        sx1, sy1, _, _ = e.pos()
        v1 = e.vic()
        d = (sx1 - sx0, sy1 - sy0)[axis]
        dv = (v1[0] - v0[0], v1[1] - v0[1])[axis]
        mv[name] = {"joy0dat": [jd0.hex(), jd1.hex()], "amiga_px": d, "vic_px": dv,
                    "frames": f1 - f0, "wall_s": round(w1 - w0, 2),
                    "px_per_s": round(d / max(w1 - w0, 1e-3), 1),
                    "px_per_frame": round(d / max(f1 - f0, 1), 2)}
        log(f"  move {name}: JOY0DAT {jd0.hex()} -> {jd1.hex()}, pointer {d:+d} lores px "
            f"(VIC {dv:+d}) in {f1 - f0} frames / {w1 - w0:.1f} s")
        if jd1 == jd0 or d <= 0 or dv <= 0:
            fails.append(f"move {name}: {mv[name]}")

    # -- 4. double-click the disk icon -----------------------------------------
    dc = rep.setdefault("doubleclick", {})
    st = R.unpack_disp_st(m.r(0xB800, 256))
    if g:
        g.fresh()
        icon = find_icon(g.read, st, title_lines)
        before = wb_screen(g.read)
    else:
        icon, before = guest_read_frozen(
            m, rom, lambda rd: (find_icon(rd, st, title_lines), wb_screen(rd)), dc)
    dc["icon"] = {k: icon[k] for k in ("x0", "x1", "y0", "y1", "cx", "cy")}
    dc["icons_found"] = icon["all"]
    dc["windows_before"] = before["windows"]
    target = icon
    if mutant == "wrongicon":           # red: double-click RAM Disk instead
        b0 = min(icon["all"], key=lambda b: b["x0"])
        target = {"cx": (b0["x0"] + b0["x1"]) // 2, "cy": (b0["y0"] + b0["y1"]) // 2}
    if E2E_MOUSE[0]:                    # W14-M: the scripted 1351 instead of the stick
        e.mouse_on(s)                   # W14-M
        nav = e.mgoto(target["cx"] // 2, target["cy"])   # W14-M
    else:                               # W14-M
        nav = e.goto(target["cx"] // 2, target["cy"])
    dc["goto"] = nav
    log(f"  icon {dc['icon']} (from the guest's planes); pointer {nav}; windows {before['windows']}")
    if not nav["ok"]:
        fails.append(f"doubleclick: the pointer did not reach the icon ({nav})")
    h0 = hash_now()
    nrd0 = m.td()["nrd"]
    if mutant != "nofire":
        for _ in range(2):
            if E2E_MOUSE[0]:            # W14-M: the 1351's left button
                e.mouse_btn(J_FIRE)     # W14-M
                e.frames_wait(2)        # W14-M
                e.mouse_btn(0)          # W14-M
                e.frames_wait(2)        # W14-M
                continue                # W14-M
            e.joy(J_FIRE)
            e.frames_wait(2)
            e.joy(0)
            e.frames_wait(2)
    t1, h1, after = time.monotonic(), h0, before
    while time.monotonic() - t1 < min(timeout, 300):
        e.frames_wait(10)
        h1 = hash_now()
        if g:
            g.fresh()
            after = wb_screen(g.read)
            if len(after["windows"]) > len(before["windows"]) and h1 != h0:
                break
        elif h1 != h0:
            break
    if not g:                           # rev-wb E2E U2a: the first change may be the
        for _ in range(6):              # icon's selection highlight; read until the
            after = guest_read_frozen(m, rom, wb_screen, dc)   # list changes (6 tries)
            if len(after["windows"]) > len(before["windows"]):
                break
            e.frames_wait(60)
        dc["frozen_reads"] = _ + 1
        h1 = hash_now()
    dc.update(windows_after=after["windows"], hash_changed=h1 != h0,
              reads_after=m.td()["nrd"] - nrd0, wall_s=round(time.monotonic() - t1, 1))
    log(f"  double-click: windows {after['windows']}, screen changed {h1 != h0}, "
        f"{dc['reads_after']} disk reads, {dc['wall_s']} s")
    want_title = O.Adf(disk.image).bname(O.Adf(disk.image).block(O.ROOT)) + ":"
    dc["want_title"] = want_title       # the volume name from the ADF's root block
    new = [w for w in after["windows"] if w not in before["windows"]]
    if len(after["windows"]) <= len(before["windows"]) or h1 == h0:
        fails.append(f"doubleclick: no new Workbook window ({dc})")
    elif want_title not in new:
        fails.append(f"doubleclick: the new window is {new}, not the disk's {want_title!r}")
    if any("Failure" in w or "Alert" in w for w in after["windows"]):
        dc["serial_after_boot"] = e2e_serial(m, b)
        return fails + [f"doubleclick: AROS raised an alert ({after['windows']}); serial: "
                        f"{dc['serial_after_boot'][-300:]!r}"]

    # -- 5./6. F1 (the title bar becomes visible: 1:1 at line 0), the right
    #    button there (Intuition's menu bar and a menu drop down), F1 back --
    f1r = rep.setdefault("f1", {})
    rb = rep.setdefault("menu", {})
    hf = hash_now()

    def ref_compare(view: int, px: int = 0, py: int = 0, tries: int = 3) -> tuple[dict, dict]:
        """VICE: force a conversion, then read every bitplane page in ONE
        park (the guest stopped for the whole read) and render it with
        c2v_ref; the C64 picture must equal it.  A guest draw between the
        conversion and the park can still differ: up to *tries* attempts
        (a real conversion defect fails every one)."""
        eq, raw = {}, {}
        for k in range(tries):
            settle()
            force_conv(m)
            h, raw = AB.c64_hash(m)
            st = R.unpack_disp_st(m.r(0xB800, 256))
            vs = (st["diwstrt"] >> 8) & 0xFF
            ve = ((st["diwstop"] >> 8) & 0xFF) | ((~st["diwstop"] >> 7) & 0x100)
            n = (st["bplcon0"] >> 12) & 7
            g.fresh()
            mods = [st["bpl1mod"], st["bpl2mod"]]
            mods = [md - 0x10000 if md & 0x8000 else md for md in mods]
            g.prefetch([((st["bplpt"][p] & 0xFFFFFE), (ve - vs + 1) * (80 + max(0, mods[p & 1])) + 80)
                        for p in range(n)])
            conv = R.disp_convert_view(st, g.read, R.build_lut(), view, px, py)
            eq = {"bitmap": conv["bitmap"] == raw["bitmap"], "screen": conv["screen"] == raw["screen"]}
            if view == 0:
                eq["colram"] = conv.get("colram") is None or \
                    bytes(c & 15 for c in conv["colram"]) == bytes(c & 15 for c in raw["colram"])
            eq["attempt"] = k + 1
            if all(v for kk, v in eq.items() if kk != "attempt"):
                break
        return eq, raw
    nc = lambda: struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]   # noqa: E731

    def f1(view: int) -> dict:
        n0 = nc()
        e.key(K_F1)
        e.frames_wait(2)
        e.key(0)
        t2 = time.monotonic()
        while time.monotonic() - t2 < 120 and (nc() == n0 or m.r(m.lab("disp_one"), 1)[0] != view):
            time.sleep(0.25)
        stt = SP.state(m)
        out = {"disp_one": stt["disp_one"], "state": stt, "s": round(time.monotonic() - t2, 1)}
        log(f"  F1 -> view {view}: {stt} after {out['s']} s")
        return out

    f1r["to_1"] = r1 = f1(1)
    if r1["disp_one"] != 1:
        return fails + [f"F1: disp_one {r1['disp_one']}, want 1"]
    nav = e.goto(20, 4)                 # the first menu header ("Workbench"/"Workbook")
    rb["goto"] = nav
    e.frames_wait(5)
    stt = SP.state(m)
    if not g:
        f1r["c2v_ref_equal"] = "skipped: no live REU read (frozen path)"
    if g:
        eq, raw = ref_compare(1, stt["disp_px"], stt["disp_py"])
        stt = SP.state(m)
        text = sum(1 for c in range(80) if (raw["screen"][c] >> 4) != (raw["screen"][c] & 15)
                   and any(raw["bitmap"][(c // 40) * 320 + (c % 40) * 8 + y] for y in range(8)))
        f1r.update(c2v_ref_equal=eq, two_pen_cells_rows01=text, window=[stt["disp_px"], stt["disp_crop"]])
        log(f"  1:1 at ({stt['disp_px']}, {stt['disp_crop']}): c2v_ref of the guest's planes equal "
            f"{eq}; {text} two-pen cells in the title rows")
        png = os.path.join(EVID, "e2e_1to1.png")
        SP.png_cells(png, raw)
        f1r["png"] = png
        if not (eq["bitmap"] and eq["screen"]):
            fails.append(f"F1: the 1:1 picture differs from c2v_ref's render of the guest's planes ({eq})")
        if stt["disp_crop"] == 0 and text < 10:
            fails.append(f"F1: the title rows show only {text} two-pen cells")
    if MENU_DELAY[0]:                   # --e2e-menu-delay: start the menu step later
        e.frames_wait(MENU_DELAY[0], timeout=max(120.0, 2.0 * MENU_DELAY[0]))   # (the resting title bar changes by itself)
    settle()
    force_conv(m)                       # rev-wb F2: the reference is a fresh picture
    hm0 = hash_now()

    def layers(key: int = 0) -> int:
        if g:
            g.fresh()
            return wb_screen(g.read)["layers"]
        return guest_read_frozen(m, rom, wb_screen, rb, key)["layers"]

    ly0 = layers()
    if mutant != "nocbm":
        e.key(K_CBM)
    e.frames_wait(6)
    e.joy(J_RIGHT)                      # Intuition opens a menu on a move over its header
    e.frames_wait(1)
    e.joy(0)
    f0 = m.frames()
    hm1 = hm0
    while m.frames() < f0 + 300:        # drawing the menu takes ~0.5 M instructions
        e.frames_wait(10)               # (boot model: 0.9 s)
        hm1 = hash_now()
        if hm1 != hm0:
            break
    e.frames_wait(20)                   # the drop-down finishes drawing
    hm1 = hash_now()
    ly1 = layers(0 if mutant == "nocbm" else K_CBM)     # ONCE (rev-wb E2E U2b: a frozen
    rb["frames_to_menu"] = m.frames() - f0                # read per poll costs a resume)
    if m.vice:                          # (VICE: the matrix under I/O is readable)
        _, rawm = AB.c64_hash(m)
        SP.png_cells(os.path.join(EVID, "e2e_menu.png"), rawm)
    e.key(0)
    e.frames_wait(30)
    settle()
    force_conv(m)                       # rev-wb E2E r2: the closed picture, converted
    hm2 = hash_now()
    ly2 = layers()
    rb.update(hash_before=hm0[:12], hash_held=hm1[:12], hash_after=hm2[:12],
              layers=[ly0, ly1, ly2], menu_shown=hm1 != hm0 and ly1 > ly0,
              closed=hm2 != hm1 and ly2 < ly1)
    log(f"  right button (1:1 view): screen {hm0[:12]} -> {hm1[:12]} (C= held) -> {hm2[:12]}; "
        f"screen layers {ly0} -> {ly1} -> {ly2}")
    if ly1 <= ly0:
        fails.append(f"menu: holding C= added no layer to the screen ({rb})")
    if hm1 == hm0 or hm1 == hm2:
        fails.append(f"menu: the held picture is not distinct from before and after ({rb})")
    if ly2 >= ly1:
        fails.append(f"menu: releasing C= did not close the menu's layers ({rb})")
    e.frames_wait(100)                  # the guest finishes closing the menu (its
    settle()                            # CPU-drawn pixels convert only with F1's forced
    f1r["to_0"] = r0 = f1(0)            # conversion: no pixel epoch for CPU writes)
    if r0["disp_one"] != 0:
        fails.append(f"F1 back: disp_one {r0['disp_one']}, want 0")
    else:
        # the menu changed the screen, so the fit picture is compared with
        # c2v_ref's fit render of the guest's planes now (VICE), not with
        # the picture before F1
        hb = hash_now()
        f1r["fit_hash"] = {"before_f1": hf[:12], "back": hb[:12]}
        if not g:
            f1r["fit_c2v_ref_equal"] = "skipped: no live REU read (frozen path)"
        if g:
            eq, _ = ref_compare(0)
            f1r["fit_c2v_ref_equal"] = eq
            log(f"  fit view again: {hb[:12]}; c2v_ref fit render of the guest's planes equal {eq}")
            if not all(v for k, v in eq.items() if k != "attempt"):
                fails.append(f"F1 back: the fit picture differs from c2v_ref's ({eq})")
    if mutant:
        rep["mutant"] = mutant
    if E2E_RAMP[0]:                     # W14-M: the stick's ramp in guest frames
        rep["ramp"] = e.ramp_probe()    # W14-M
    rep["serial_after_boot"] = e2e_serial(m, b)
    if "lert" in rep["serial_after_boot"] or "Failure" in rep["serial_after_boot"]:
        fails.append(f"e2e: alert text on the serial line: {rep['serial_after_boot'][-300:]!r}")
    return fails


def leg_demo_boot(m: WbMachine, rom, disk: Disk, s: int, rep: dict, vol, timeout: float,
                  t_start: float, hold_s: float) -> list[str]:
    """The user's boot demo: boot to the Workbench, then hand port 2 to the
    user (inp_src = 0, the pointer armed) and hold."""
    fails = boot_workbench(m, rom, disk, s, rep, vol, timeout, t_start=t_start,
                           want_screen=False)
    if fails:
        return fails
    if STUCK[0]:                        # supervisor: mask the stuck switch first
        m.w(m.lab("inp_stuck"), bytes([STUCK[0]]))
    m.w(m.lab("inp_src"), b"\0\0\0")
    arm_spr(m)
    demo_mouse(m, s)                    # W14-M
    log(f"  demo-boot: the Workbench is up; port 2 is the user's (inp_src 0); holding {hold_s:.0f} s")
    time.sleep(hold_s)
    return []


def demo_mouse(m, s: int) -> None:                      # W14-M
    """The user's 1351 (--mouse-port 1/2): mouse port, the slot-7 sampler
    (mach_cbv[7] := inp_msamp, inp_mper := 8192 phi2 = $2000 >> s E
    ticks), a fresh reference; the input module re-armed."""   # W14-M
    if not DEMO_MOUSE[0]:                               # W14-M
        return                                          # W14-M
    a = m.lab("inp_msamp")                              # W14-M
    m.w(m.lab("inp_mport"), bytes([DEMO_MOUSE[0] - 1]))   # W14-M
    m.w(m.lab("mach_cbv") + 14, bytes([a & 0xFF, a >> 8]))   # W14-M
    m.w(m.lab("inp_mok"), b"\0")                        # W14-M
    m.w(m.lab("inp_mper"), struct.pack("<I", max(1, 0x2000 >> s)))   # W14-M
    log(f"  demo: the 1351 on port {DEMO_MOUSE[0]} (sampled every {max(1, 0x2000 >> s)} "
        f"E ticks), the stick on port {3 - DEMO_MOUSE[0]}")   # W14-M


def leg_demo(m: WbMachine, rom, rep: dict, vol: str, hold_s: float,
             s: int = 3) -> list[str]:
    """The user's demo: resume <vol>/amiga64/wb-snap.bin (left by
    `--legs resume --keep` on the final tree) and keep the machine running
    *hold_s* seconds before the hold restores the device."""
    path = f"{vol}/amiga64/{SNAP_DEMO}"
    el = resume_workbench(m, rom, path, rep)
    if STUCK[0]:                        # supervisor: the user's stick has a stuck switch
        m.w(m.lab("inp_stuck"), bytes([STUCK[0]]))
        rep["inp_stuck"] = STUCK[0]
    demo_mouse(m, s)                    # W14-M
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


COST_STUB = 0xCC80                    # leg cost: the timing stub (buf_result, after aros_resume's $CC00)
COST_RES = 0xCCE0


def leg_cost(m: WbMachine, rep: dict) -> list[str]:
    """rev-wb L7: the 6502 cycles td_call takes for a quick CMD_READ (the
    whole BeginIO trap: decode, copy, io_Error/io_Actual, RTS), measured
    with CIA #2 timer A on a fresh runner (VICE: phi2 exact), minus an
    empty jsr; BASIC out ($01 = $35) as inside mach_run.  Run it FIRST in a
    session (or alone): after a boot or resume leg the machine loop's
    IRQ/NMI vectors and one-shot are still armed and its handlers run
    between the host's writes (gate3: a stale r_a7)."""
    from tests import aros_resume as RS
    from tests.harness_util import call_runner
    if not m.vice:
        return ["cost: VICE only (exact phi2)"]
    sn = RS.Snap(m, m.prg, b"")
    m.load_driver()                                   # mboot + the modules
    call_runner(m.tg, m.lab("emu_reset"), 30.0)       # the code-window map (cw_mok)
    m.w(m.lab("td_on"), b"\1")
    call_runner(m.tg, 0xC200, 30.0)                   # td_arm
    io, sp, slot, ret = 0xC23F00, 0xC08000, 0xC0E4C6, 0xF81234
    rts_at = COST_RES + 4
    m.w(rts_at, b"\x60")

    def stub(target: int) -> bytes:
        return bytes([0xA5, 0x01, 0x48, 0xA9, 0x35, 0x85, 0x01, 0x78,      # lda $01/pha/lda #$35/sta $01/sei
                      0xA9, 0x7F, 0x8D, 0x0D, 0xDD, 0xA9, 0x00, 0x8D, 0x0E, 0xDD,
                      0xA9, 0xFF, 0x8D, 0x04, 0xDD, 0x8D, 0x05, 0xDD,
                      0xA9, 0x11, 0x8D, 0x0E, 0xDD,                      # force load, start
                      0xA9, 0x5A, 0xA2, 0x2D, 0x20, target & 0xFF, target >> 8,
                      0xA9, 0x00, 0x8D, 0x0E, 0xDD,                      # stop
                      0xAD, 0x04, 0xDD, 0x8D, COST_RES & 0xFF, COST_RES >> 8,
                      0xAD, 0x05, 0xDD, 0x8D, (COST_RES + 1) & 0xFF, COST_RES >> 8,
                      0x68, 0x85, 0x01, 0x60])

    def run(target: int) -> int:
        m.w(COST_STUB, stub(target))
        call_runner(m.tg, COST_STUB, 30.0)
        lo, hi = m.r(COST_RES, 2)
        return 0xFFFF - (lo | hi << 8)

    base = run(rts_at)
    out = {}
    for name, length, data in (("512 aligned", 512, 0x3000), ("1024 aligned", 1024, 0x3000),
                               ("1024 unaligned", 1024, 0x3058), ("512 unaligned", 512, 0x3058)):
        req = bytearray(48)
        struct.pack_into(">HBB", req, 28, 2, 1, 0)
        struct.pack_into(">III", req, 36, length, data, 512 * 10)
        sn.reu_write(io, bytes(req))
        sn.reu_write(sp, struct.pack(">I", ret))
        m.w(m.lab("td_slot"), slot.to_bytes(3, "little"))
        m.w(0x8D, slot.to_bytes(4, "little"))        # st_ipc
        m.w(0x34, io.to_bytes(4, "little"))          # r_a1
        m.w(0x4C, sp.to_bytes(4, "little"))          # r_a7
        c = run(m.lab("td_call")) - base
        act = struct.unpack(">I", sn.reu_read(io + 32, 4))[0]
        pc = int.from_bytes(m.r(0x50, 4), "little")
        out[name] = {"cycles": c, "us_at_64mhz": round(c / 64, 1), "io_actual": act,
                     "pc_after": f"${pc:06X}"}
        log(f"  cost {name}: {c} cycles ({c / 64:.1f} us at 64 MHz), io_Actual {act}, PC ${pc:06X}")
        if act != length or pc != ret:
            return [f"cost: the measured read did not complete ({out[name]})"]
    rep["cost"], rep["baseline_cycles"] = out, base
    return []


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
    ap.add_argument("--stuck", type=lambda v: int(v, 0), default=0,
                    help="demo legs: inp_stuck mask for a stuck port-2 switch (e.g. 0x01 = UP)")
    ap.add_argument("--keep", action="store_true",
                    help="leg resume: leave <vol>/amiga64/wb-snap.bin (the demo snapshot)")
    ap.add_argument("--td-define", action="append", default=[],
                    help="assemble the td module with -D X (evidence builds, e.g. TD_ZPJUNK=$A5)")
    ap.add_argument("--hold-s", type=float, default=300.0,
                    help="leg demo: seconds the resumed Workbench keeps running")
    ap.add_argument("--resume-mutant", default=None,
                    help="teeth: resume (b) on an aros_resume mutant PRG (e.g. noepoch, nodst): must fail")
    ap.add_argument("--e2e-menu-delay", type=int, default=0,
                    help="leg e2e: guest frames to wait before the menu step (teeth from both "
                         "resting title-bar pictures)")
    ap.add_argument("--e2e-mouse", action="store_true",          # W14-M
                    help="W14-M leg e2e: reach and double-click the disk icon with the "
                         "scripted 1351 (inp_mpot / inp_mb) instead of the stick")
    ap.add_argument("--mouse-port", type=int, choices=(1, 2), default=0,   # W14-M
                    help="W14-M demo legs: the user's 1351 in this port (the stick in the other)")
    ap.add_argument("--e2e-ramp", action="store_true",           # W14-M
                    help="W14-M leg e2e: at the end, the stick's ramp in px/frame")
    ap.add_argument("--e2e-frozen", action="store_true",
                    help="leg e2e on VICE: guest reads by suspend/reset/resume (as on the Ultimate)")
    ap.add_argument("--e2e-mutant", default=None, choices=("nofire", "wrongicon", "nocbm"),
                    help="red: leg e2e without the fire presses / double-clicking RAM Disk / "
                         "without C= (each must fail)")
    ap.add_argument("--td-mutant", default=None,
                    help="red: build the td module as host/tests/test_td.MUTANTS[NAME]")
    ap.add_argument("--watch", action="store_true",
                    help="a counter build (scratch mkwatch.sh): fail if the guest touches TD_ADF")
    ap.add_argument("--tag", default=None, help="report name (default: backend-script)")
    a = ap.parse_args(argv)
    a.legs = [x for x in a.legs.split(",") if x]
    WATCH[0] = a.watch
    STUCK[0] = a.stuck
    if a.watch and "e2e" in a.legs:     # WATCH_AT/WATCH_N lie in GuestPeek's PEEK_BUF page
        raise SystemExit("--watch cannot run with leg e2e (both use $CD00-$CDFF)")
    TD_DEFINES[:] = a.td_define
    TD_MUTANT[0] = a.td_mutant
    E2E_FROZEN[0] = a.e2e_frozen
    E2E_MOUSE[0] = a.e2e_mouse          # W14-M
    E2E_RAMP[0] = a.e2e_ramp            # W14-M
    DEMO_MOUSE[0] = a.mouse_port        # W14-M
    MENU_DELAY[0] = a.e2e_menu_delay
    RESUME_MUTANT[0] = a.resume_mutant
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
