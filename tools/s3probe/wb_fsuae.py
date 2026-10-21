#!/usr/bin/env python3
"""Boot a Workbench floppy on the pinned AROS ROM in FS-UAE (ground truth).

WB-measure (docs/WB_BOOT.md, task 1).  The ADF is the user's own
proprietary disk: it is opened read-only where it lies (FS-UAE with
``writable_floppy_images = 0``; the per-run base dir that would hold a write
overlay is deleted), never copied, and no test needs one.

Two modes:

* ``floppy`` -- a plain DF0: boot.  A host directory is mounted as a
  non-bootable DH1: (boot priority -128) whose ``Disk.info`` is a FIFO.
  Workbench reads every volume's Disk.info when it opens its root window,
  so the first reader of the FIFO marks "Workbench is up" without touching
  the floppy boot.  The watcher opens the FIFO for writing (non-blocking)
  and closes it at once: the reader sees an empty file.
* ``chain`` -- DH0: (a host directory, boot priority 10) boots first, starts
  ``wbsnap`` (tools/s3probe/wbsnap.c, built with vbcc) in the background,
  assigns SYS:, C:, S:, L:, LIBS:, DEVS:, FONTS: to DF0: and executes
  DF0:S/Startup-Sequence.  wbsnap dumps the front screen and every screen
  and window title every few seconds; these become PNGs here.  This is the
  only way to get screenshots on this host (no screen-recording permission,
  and FS-UAE has no headless screenshot or console debugger).

Both capture the serial port (the AROS nightly prints its boot log and any
alert there) and the FS-UAE log.  Evidence goes to --out (under the
git-ignored build/).

Usage: python3 tools/s3probe/wb_fsuae.py --adf DISK.adf [--mode floppy|chain]
           [--mem oracle|plain] [--seconds 90] [--out DIR]
"""
from __future__ import annotations

import argparse
import errno
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FS_UAE = os.environ.get("FS_UAE", "/Applications/FS-UAE.app/Contents/MacOS/fs-uae")
MEM = {"oracle": (1024, 512, 8192), "plain": (512, 512, 0)}

CFG = """[fs-uae]
amiga_model = A500
kickstart_file = {rom}
kickstart_ext_file = {ext}
chip_memory = {chip}
slow_memory = {slow}
fast_memory = {fast}
floppy_drive_0 = {adf}
writable_floppy_images = 0
hard_drive_0 = {hd}
hard_drive_0_label = {label}
hard_drive_0_priority = {pri}
headless = 1
sound = 0
floppy_drive_speed = {fspeed}
warp_mode = {warp}
automatic_input_grab = 0
initial_input_grab = 0
serial_port = {serial}
base_dir = {base}
"""

CHAIN_SCRIPT = """FailAt 21
Run >NIL: DH0:wbsnap {interval} {count} DH0:snaps/s
{assigns}CD DF0:
Execute DF0:S/Startup-Sequence
"""


ASSIGNS = (("SYS", ""), ("C", "C"), ("S", "S"), ("L", "L"), ("LIBS", "Libs"),
           ("DEVS", "Devs"), ("FONTS", "Fonts"), ("ENVARC", "Prefs/Env-Archive"))


def chain_assigns(adf: str) -> str:
    """Assign lines for the directories this disk has (names only are read)."""
    sys.path.insert(0, HERE)
    import adf as adfmod
    img = adfmod.AdfImage(adf)
    return "".join(f"DH0:C/Assign {name}: DF0:{sub}\n" for name, sub in ASSIGNS
                   if sub == "" or img.lookup(sub) is not None)


def rom_dir() -> str | None:
    c = glob.glob(os.path.join(REPO, "third_party", "aros", "aros-rom.bin"))
    return os.path.dirname(c[0]) if c else None


def build_wbsnap(out: str) -> str:
    home = os.environ.get("VBCC", os.path.expanduser("~/.local/opt/vbcc"))
    env = dict(os.environ, VBCC=home,
               PATH=os.path.join(home, "bin") + os.pathsep + os.environ["PATH"])
    exe = os.path.join(out, "wbsnap")
    subprocess.run(["vc", "+aos68k", "-O2", "-o", exe, os.path.join(HERE, "wbsnap.c")],
                   check=True, env=env, capture_output=True)
    return exe


def write_png(path, w, h, rows):
    raw = b"".join(b"\0" + r for r in rows)

    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def raw_to_png(raw: bytes, path: str) -> dict | None:
    """wbsnap's .raw (see wbsnap.c) -> PNG.  Returns the header fields."""
    if raw[:4] != b"WBS1" or len(raw) < 76:
        return None
    w, h, d, rb = struct.unpack_from(">HHHH", raw, 4)
    pal = struct.unpack_from(">32H", raw, 12)
    body = raw[76:]
    if w == 0 or h == 0 or len(body) < d * h * rb:
        return None
    rgb = [((c >> 8 & 15) * 17, (c >> 4 & 15) * 17, (c & 15) * 17) for c in pal]
    rows = []
    for y in range(h):
        planes = [body[(p * h + y) * rb:(p * h + y + 1) * rb] for p in range(d)]
        row = bytearray()
        for x in range(min(w, rb * 8)):
            idx = 0
            for p in range(d):
                idx |= ((planes[p][x >> 3] >> (7 - (x & 7))) & 1) << p
            row += bytes(rgb[idx & 31])
        rows.append(bytes(row))
    write_png(path, min(w, rb * 8), h, rows)
    return {"width": w, "height": h, "depth": d}


class FifoProbe:
    """Detect the first open of a FIFO by a reader (Workbench's Disk.info)."""

    def __init__(self, path: str):
        self.path, self.hits, self._stop = path, [], False
        os.mkfifo(path)
        self.t0 = time.monotonic()
        self.th = threading.Thread(target=self._run, daemon=True)
        self.th.start()

    def _run(self):
        while not self._stop:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as e:
                if e.errno not in (errno.ENXIO, errno.ENOENT):
                    return
                time.sleep(0.02)
                continue
            self.hits.append(round(time.monotonic() - self.t0, 2))
            os.close(fd)
            time.sleep(0.2)

    def stop(self):
        self._stop = True


def run(adf: str, mode: str, mem: str, seconds: float, out: str,
        warp: bool = True, interval: int = 150, fs_uae: str = FS_UAE,
        script: str | None = None) -> dict:
    rd = rom_dir()
    if rd is None or not os.path.exists(fs_uae):
        return {"skip": "no AROS ROM cache or no FS-UAE"}
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    work = tempfile.mkdtemp(prefix="wbfsuae-")
    try:
        hd = os.path.join(work, "hd")
        os.makedirs(hd)
        probe = None
        if mode == "floppy":
            probe = FifoProbe(os.path.join(hd, "Disk.info"))
            label, pri = "Probe", -128
        else:
            label, pri = "DH0", 10
            os.makedirs(os.path.join(hd, "S"))
            os.makedirs(os.path.join(hd, "C"))
            os.makedirs(os.path.join(hd, "snaps"))
            shutil.copy(build_wbsnap(work), os.path.join(hd, "wbsnap"))
            shutil.copy(os.path.join(rd, "C", "Assign"), os.path.join(hd, "C", "Assign"))
            with open(os.path.join(hd, "S", "Startup-Sequence"), "w") as f:
                tmpl = open(script).read() if script else CHAIN_SCRIPT
                f.write(tmpl.format(interval=interval, assigns=chain_assigns(adf),
                                            count=int(seconds * 50 / interval) + 2))
        serial = os.path.join(work, "serial.txt")
        open(serial, "w").close()
        chip, slow, fast = MEM[mem]
        cfg = os.path.join(work, "run.fs-uae")
        with open(cfg, "w") as f:
            f.write(CFG.format(rom=os.path.join(rd, "aros-rom.bin"),
                               ext=os.path.join(rd, "aros-ext.bin"),
                               chip=chip, slow=slow, fast=fast, adf=adf, hd=hd,
                               label=label, pri=pri, serial=serial,
                               base=os.path.join(work, "base"),
                               warp=1 if warp else 0, fspeed=0 if warp else 100))
        t0 = time.monotonic()
        with open(os.path.join(work, "stdout.txt"), "w") as log:
            proc = subprocess.Popen([fs_uae, cfg], cwd=work, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            try:
                while time.monotonic() - t0 < seconds and proc.poll() is None:
                    time.sleep(0.2)
            finally:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        if probe:
            probe.stop()
        res = {"adf": os.path.basename(adf), "mode": mode, "mem": mem,
               "chip_slow_fast_kb": [chip, slow, fast], "seconds": seconds,
               "warp": warp, "wb_diskinfo_read_s": probe.hits if probe else None}
        ser = open(serial, "rb").read().decode("latin-1").replace("\r", "")
        with open(os.path.join(out, "serial.txt"), "w") as f:
            f.write(ser)
        lines = ser.splitlines()
        res["serial_lines"] = len(lines)
        res["serial_resets"] = ser.count("ROMInfo: 1MiB ROM detected")
        res["serial_alerts"] = [l for l in lines if "lert" in l or "GURU" in l.upper()][:20]
        logs = glob.glob(os.path.join(work, "base", "**", "fs-uae.log.txt"), recursive=True)
        if logs:
            shutil.copy(logs[0], os.path.join(out, "fs-uae.log.txt"))
        snaps = []
        sd = os.path.join(hd, "snaps")
        if os.path.isdir(sd):
            for n in sorted(os.listdir(sd)):
                p = os.path.join(sd, n)
                if n.endswith(".txt"):
                    shutil.copy(p, os.path.join(out, n))
                    snaps.append({"file": n, "titles": open(p, encoding="latin-1").read().splitlines()})
                elif n.endswith(".raw"):
                    info = raw_to_png(open(p, "rb").read(), os.path.join(out, n[:-4] + ".png"))
                    snaps.append({"file": n[:-4] + ".png", **(info or {"bad": True})})
        res["snaps"] = snaps
        with open(os.path.join(out, "result.json"), "w") as f:
            json.dump(res, f, indent=1)
        return res
    finally:
        shutil.rmtree(work, ignore_errors=True)   # includes any floppy write overlay


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adf", required=True)
    ap.add_argument("--mode", choices=("floppy", "chain"), default="floppy")
    ap.add_argument("--mem", choices=tuple(MEM), default="oracle")
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--interval", type=int, default=150, help="wbsnap period in ticks")
    ap.add_argument("--no-warp", action="store_true")
    ap.add_argument("--script", help="chain mode: a Startup-Sequence template "
                    "({interval}, {count}, {assigns}) instead of the built-in one")
    ap.add_argument("--out", default=os.path.join(REPO, "build", "evidence", "wb-measure", "fsuae"))
    a = ap.parse_args(argv)
    res = run(a.adf, a.mode, a.mem, a.seconds, a.out, not a.no_warp, a.interval,
              script=a.script)
    print(json.dumps({k: v for k, v in res.items() if k != "snaps"}))
    for s in res.get("snaps", []):
        if "titles" in s:
            print(s["file"], "|", " / ".join(t.strip() for t in s["titles"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
