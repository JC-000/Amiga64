#!/usr/bin/env python3
"""W13-D: the readable 1:1 view on AROS's insert-disk screen (VICE), and
the pointer sprite legs (docs/WAVE13_BRIEF.md W13-D; docs/C2V.md "1:1 view
(W13-D)"; docs/CONTRACTS.md "Display refresh (S3.5)").

Leg ``aros1`` (needs the AROS ROM cache; SKIP without it, like
tests/aros_boot.py): one AROS boot in VICE (tests/aros_boot.py's machine:
staging, mach_s, the serial watch) to milestone m3, the insert-disk
screen, then, with the guest running:

  * the fit view as m3 left it (hash, PNG);
  * F1: the host sets inp_view bit 0 (W13-I's I1 byte; until W13-I lands
    disp.s's stand-in); the next frame hook must consume it, convert once
    in the 1:1 view (disp_one = 1, the window centred: disp_px = 20,
    disp_crop = the fit crop) and leave disp_mck equal to the matrix's
    sums; PNG of the C64's bitmap + matrix (both nibbles: the 1:1 view's
    cells have their own background);
  * a test-set offset (disp_px, disp_py, then the pixel epoch bumped so
    the next frame converts): the window moves (disp_crop = min(py, ymax));
  * F1 again: the fit view comes back byte-identical (hash) to the first.

Usage: python3 -m tests.run_spr --prg build/X/runner.prg [--legs aros1]
       [--s 9] [--timeout 900] [--out DIR]
VICE only (the C64U pan timing is the supervisor's combined gate).
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

from host import c2v_ref as R  # noqa: E402
from host import machine  # noqa: E402
from tests import aros_boot as A  # noqa: E402

EVID = os.path.join(ROOT, "build", "evidence", "w13-vis")
MODEL_VIEW = os.path.join(ROOT, "build", "aros", "model_view.json")
OFFSET = (40, 77)               # the aros1 leg's test-set window: the text, right half
VIEWS = ((20, R.ONE_PY_DEFAULT), OFFSET)   # the 1:1 windows the aros1 leg shows: F1's (W14-D: the top), the text


def view_hash(bitmap: bytes, screen: bytes, vic: dict) -> str:
    """model_frames.conv_hash without colour RAM (the 1:1 view leaves it
    as the fit view did; the VIC does not read it in hires)."""
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import model_frames as MF
    return MF.conv_hash(bitmap, screen, bytes(1000), vic)


def model_main(out: str) -> int:
    """(Run with the s3probe venv.)  The boot model's insert-disk frames
    converted in the 1:1 view at VIEWS (c2v_ref.disp_convert_view), hashed
    as tests/aros/model_frames.conv_hash: the aros1 leg's exact reference."""
    import glob
    sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe"))
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import aros_boot_model as M
    import model_frames as MF
    c = glob.glob(os.path.expanduser("~/.cache/amiga64/aros/*/aros-rom.bin"))
    if not c:
        print("SKIP: no AROS ROM")
        return 0
    scratch = os.path.join(os.path.dirname(os.path.abspath(out)), "model-run-view")
    os.makedirs(scratch, exist_ok=True)
    am = M.Amiga(os.path.dirname(c[0]), scratch)
    lut = R.build_lut()
    seen: dict[str, dict] = {}
    orig = am.copper

    def copper():
        n = orig()
        f = am.frames
        if MF.INSERT_DISK_FRAME <= f <= 450:
            st = MF.snapshot(am)
            for px, py in VIEWS:
                conv = R.disp_convert_view(st, am.mem.r_block, lut, 1, px, py)
                if "bitmap" not in conv:
                    continue
                h = view_hash(conv["bitmap"], conv["screen"], conv["vic"])
                if h not in seen:
                    seen[h] = {"frame": f, "view": [px, py], "one": conv["decide"]["one"],
                               "cells": len(set(conv.get("cells", []))),
                               "vic_pens": conv.get("vic_pens")}
        return n
    am.copper = copper
    am.run(452 / 50.0, 1000)
    with open(out, "w") as fp:
        json.dump({"views": VIEWS, "hashes": seen}, fp, indent=1)
    print(f"{len(seen)} distinct 1:1 conversions: "
          + ", ".join(f"{v['frame']} {v['view']}" for v in seen.values()))
    return 0
PIX_OFF = 105                   # disp_st+105: the pixel epoch (W11-A C3)


def png_cells(path: str, raw: dict, spr: dict | None = None) -> None:
    """A hires bitmap with per-cell colours: set = matrix high nibble,
    clear = matrix low nibble (the 1:1 view; in the fit view the low nibble
    is colour 0 everywhere, so this renders both).  *spr*: spr_read()'s VIC
    sprites 0-2 drawn on top (sprite 0 in front)."""
    from PIL import Image
    im = Image.new("RGB", (320, 200))
    sc = raw["screen"]
    for c in range(1000):
        r, cx = divmod(c, 40)
        for y in range(8):
            b = raw["bitmap"][r * 320 + cx * 8 + y]
            for k in range(8):
                col = sc[c] >> 4 if b >> (7 - k) & 1 else sc[c] & 15
                im.putpixel((cx * 8 + k, r * 8 + y), R.VIC_RGB[col])
    if spr:
        rg = spr["regs"]
        for n in (2, 1, 0):
            if not rg[0x15] >> n & 1:
                continue
            x0 = rg[2 * n] | (rg[0x10] >> n & 1) << 8
            y0 = rg[2 * n + 1]
            xe, ye = 1 + (rg[0x1D] >> n & 1), 1 + (rg[0x17] >> n & 1)
            blk = spr["blocks"][64 * n:64 * n + 63]
            for ln in range(21):
                for px in range(24):
                    if blk[3 * ln + px // 8] >> (7 - px % 8) & 1:
                        for dy in range(ye):
                            for dx in range(xe):
                                X, Y = x0 - 24 + px * xe + dx, y0 - 50 + ln * ye + dy
                                if 0 <= X < 320 and 0 <= Y < 200:
                                    im.putpixel((X, Y), R.VIC_RGB[rg[0x27 + n] & 15])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.resize((960, 600), Image.NEAREST).save(path)


def wait_conv(m, n0: int, timeout: float) -> int:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        n = struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]
        if n != n0:
            time.sleep(1.0)             # a conversion may follow at once
            return struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]
        time.sleep(0.5)
    return n0


def state(m) -> dict:
    return {k: m.r(m.lab(k), 1)[0] for k in ("disp_view", "disp_one", "disp_px",
                                              "disp_py", "disp_crop", "disp_ymax",
                                              "disp_mode", "inp_view")}


def leg_aros1(prg: str, s: int, timeout: float, out: str, rep: dict) -> list[str]:
    rom, why = machine.find_rom()
    if rom is None:
        print(f"SKIP run_spr aros1: {why}")
        rep["skip"] = why
        return []

    def body(m) -> list[str]:
        fails: list[str] = []
        r = rep.setdefault("boot", {})
        r["stage"] = m.stage(rom)
        sbin, _ = build_spr(os.path.join(out, "spr-mod"), m.labels)   # D2: the pointer
        m.w(SPR_ORG, sbin)
        arm(m.w, m.lab)
        m.start(s)
        A.watch(m, ["m2"], timeout, r)
        f = A.leg_m3(m, r, timeout)
        if f:
            return f
        h_fit, raw_fit = A.c64_hash(m)
        sp = spr_read_live(m)
        rep["pointer_fit"] = {"regs": sp["regs"][:0x30].hex(), "enabled": sp["regs"][0x15] & 7}
        png_cells(os.path.join(out, "insert_fit.png"), raw_fit, sp)
        st0 = state(m)
        rep["fit"] = {"hash": h_fit[:12], "state": st0}
        A.log(f"  fit: {h_fit[:12]} {st0}")
        if not sp["regs"][0x15] & 7:
            fails.append("pointer: not shown on the insert-disk screen (fit)")
        # fix round 1: the module stays armed; the insert-disk pointer's image
        # is empty, so it never moves the window (no pan to an invisible pointer)
        # F1 -> 1:1
        n0 = struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]
        t0 = time.monotonic()
        m.w(m.lab("inp_view"), b"\x01")
        n1 = wait_conv(m, n0, 120.0)
        el = time.monotonic() - t0
        _, raw1 = A.c64_hash(m)
        h1 = view_hash(raw1["bitmap"], raw1["screen"], raw1["vic"])
        st1 = state(m)
        A.log(f"  1:1: {h1[:12]} after {el:.1f} s, conversions {n0} -> {n1}, {st1}")
        rep["one"] = {"hash": h1[:12], "state": st1, "wall_s": round(el, 1),
                      "nconv": [n0, n1]}
        if n1 == n0:
            fails.append("F1: no conversion within 120 s")
        if st1["inp_view"] & 1:
            fails.append("F1: inp_view bit 0 not consumed")
        if (st1["disp_view"], st1["disp_one"], st1["disp_px"]) != (1, 1, R.PX_CENTRE):
            fails.append(f"F1: view state {st1}, want view 1, one 1, px {R.PX_CENTRE}")
        if st1["disp_crop"] != R.ONE_PY_DEFAULT:
            fails.append(f"F1: the window's line {st1['disp_crop']}, want "
                         f"{R.ONE_PY_DEFAULT} (W14-D: the top)")
        mv = model_view()
        if mv is not None:
            hit = mv.get(h1)
            rep["one"]["model"] = hit
            if not hit or hit["view"] != [20, R.ONE_PY_DEFAULT]:
                fails.append(f"1:1: {h1[:12]} is not a model 1:1 frame of the centred "
                             f"window ({len(mv)} model hashes)")
            else:
                A.log(f"  1:1 = model frame {hit['frame']} at {hit['view']}")
        if R.screen_sum(raw1["screen"]) != raw1["mck"]:
            fails.append("1:1: disp_mck != the matrix's sums")
        if raw1["vic"][0xD016] & 0x10:
            fails.append("1:1: not hires")
        sp = spr_read_live(m)
        rep["pointer_1to1"] = {"regs": sp["regs"][:0x30].hex(), "enabled": sp["regs"][0x15] & 7}
        png_cells(os.path.join(out, "insert_1to1.png"), raw1, sp)
        # a test-set offset: the left edge, the top
        n0 = n1
        m.w(m.lab("disp_px"), bytes([OFFSET[0]]))
        m.w(m.lab("disp_py"), bytes([OFFSET[1]]))
        pix = m.r(A_DISP_ST + PIX_OFF, 1)[0]
        m.w(A_DISP_ST + PIX_OFF, bytes([(pix + 1) & 0xFF]))
        n1 = wait_conv(m, n0, 120.0)
        _, raw2 = A.c64_hash(m)
        h2 = view_hash(raw2["bitmap"], raw2["screen"], raw2["vic"])
        st2 = state(m)
        rep["offset"] = {"hash": h2[:12], "state": st2}
        A.log(f"  offset {OFFSET}: {h2[:12]} {st2}")
        if n1 == n0 or (st2["disp_px"], st2["disp_crop"]) != OFFSET:
            fails.append(f"offset: conversions {n0} -> {n1}, state {st2}")
        if mv is not None:
            hit = mv.get(h2)
            rep["offset"]["model"] = hit
            if not hit or hit["view"] != list(OFFSET):
                fails.append(f"offset: {h2[:12]} is not a model 1:1 frame of the "
                             f"window {OFFSET}")
            else:
                A.log(f"  offset = model frame {hit['frame']} at {hit['view']}")
        png_cells(os.path.join(out, "insert_1to1_text.png"), raw2)
        # the pointer at the test offset: it follows only a visible pointer
        cur = m.r(m.lab("disp_cur"), 71)
        ST = m.lab("c2v_args") + 180
        time.sleep(2.0)
        sn = m.r(ST, 7)
        pos, ctl = (sn[3] << 8) | sn[4], (sn[5] << 8) | sn[6]
        u16 = lambda o: cur[o] | cur[o + 1] << 8          # noqa: E731
        c = {"ddfstrt": u16(34), "diwstrt": u16(30), "bplcon0": u16(2)}
        st4 = state(m)
        sp4 = spr_read_live(m)
        nonempty = any(sp4["blocks"])
        want, wpx, wpy, pans = R.spr_settle(c, pos, ctl, True, True, OFFSET[0], OFFSET[1],
                                            st4["disp_ymax"], nonempty=nonempty)
        rep["pointer_image_nonempty"] = nonempty
        rep["follow"] = {"pos": f"${pos:04X}", "ctl": f"${ctl:04X}", "state": st4,
                         "want": [wpx, wpy, pans], "vic": sp4["regs"][:0x30].hex()}
        A.log(f"  pointer POS ${pos:04X} CTL ${ctl:04X}: window ({st4['disp_px']}, "
              f"{st4['disp_py']}), model ({wpx}, {wpy}) after {pans} pan(s); {want}")
        if (st4["disp_px"], st4["disp_py"]) != (wpx, wpy):
            fails.append(f"follow: window ({st4['disp_px']}, {st4['disp_py']}), "
                         f"want ({wpx}, {wpy})")
        rg = sp4["regs"]
        if "hidden" not in want and (rg[0] | (rg[0x10] & 1) << 8, rg[1]) != \
                (want["x"] | (want["msb"] & 1) << 8, want["y"]):
            fails.append(f"follow: VIC sprite 0 at ({rg[0] | (rg[0x10] & 1) << 8}, "
                         f"{rg[1]}), want ({want['x'] | (want['msb'] & 1) << 8}, {want['y']})")
        _, raw4 = A.c64_hash(m)
        png_cells(os.path.join(out, "insert_1to1_pointer.png"), raw4, sp4)
        n1 = struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]
        # F1 again: the fit view, byte-identical
        n0 = n1
        m.w(m.lab("inp_view"), b"\x01")
        n1 = wait_conv(m, n0, 120.0)
        h3, raw3 = A.c64_hash(m)
        st3 = state(m)
        rep["back"] = {"hash": h3[:12], "state": st3}
        A.log(f"  fit again: {h3[:12]} {st3}")
        mf, _ = A.model_frames()
        okf = {h: v for h, v in (mf or {}).get("hashes", {}).items()
               if v["frame"] >= (mf or {}).get("insert_disk_frame", 0)}
        if h3 not in okf:           # the screen animates (model frames 345/371/397)
            fails.append(f"F1 back: fit hash {h3[:12]} is not a model insert-disk frame")
        else:
            A.log(f"  fit again = model frame {okf[h3]['frame']}")
        return fails

    return A.vice_session(prg, body)


A_DISP_ST = 0xB800              # rc ram.inc disp_st (W10-P)


def leg_hw(prg: str, s: int, mhz: int, timeout: float, out: str, rep: dict) -> list[str]:
    """Leg "hw" (C64U / U64E, one DeviceLock hold, config restored): AROS to
    m3 with the pointer module armed; then the wall time from F1 to the
    finished 1:1 picture, and from the pointer module's pan (the
    insert-disk pointer sits at the screen's top-left, so the centred
    window re-centres on it at once) to its finished picture; F1 back;
    the same toggle with the module disarmed.  Times: host polls of
    disp_nconv (one REST read each, ~0.1 s on the C64U)."""
    rom, why = machine.find_rom()
    if rom is None:
        print(f"SKIP run_spr hw: {why}")
        return []

    def nconv(m):
        return struct.unpack("<H", bytes(m.t.read_memory(m.lab("disp_nconv"), 2)))[0]

    def until(m, n0, k, tmax=60.0):
        t0 = time.monotonic()
        stamps = []
        n = n0
        while time.monotonic() - t0 < tmax:
            n2 = nconv(m)
            if n2 != n:
                stamps += [time.monotonic() - t0] * ((n2 - n) & 0xFFFF)
                n = n2
                if len(stamps) >= k:
                    break
        return stamps

    def body(m) -> list[str]:
        fails: list[str] = []
        r = rep.setdefault("hw_boot", {})
        r["stage"] = m.stage(rom)
        sbin, _ = build_spr(os.path.join(out, "spr-mod"), m.labels)
        m.w(SPR_ORG, sbin)
        arm(m.w, m.lab)
        m.start(s)
        A.watch(m, ["m2"], timeout, r)
        f = A.leg_m3(m, r, timeout)
        if f:
            return f
        A.log(f"  m3 at {r['at']['m3']['wall_s']} s")
        rd = lambda n, k=1: bytes(m.t.read_memory(m.lab(n), k))   # noqa: E731
        reg = lambda a, k=1: bytes(m.t.read_memory(a, k))          # noqa: E731
        res = {"pointer_fit": reg(0xD000, 0x30).hex()}
        for tag, armed in (("armed", True), ("disarmed", False)):
            if not armed:
                m.w(m.lab("spr_vec") + 1, b"\0")
            n0 = nconv(m)
            m.w(m.lab("inp_view"), b"\x01")
            st = until(m, n0, 1, 30.0)
            res[f"f1_{tag}_s"] = [round(x, 2) for x in st]
            res[f"win_{tag}"] = [rd("disp_view")[0], rd("disp_px")[0], rd("disp_py")[0]]
            res[f"pointer_1to1_{tag}"] = reg(0xD000, 0x30).hex()
            A.log(f"  F1 ({tag}): conversions at {res[f'f1_{tag}_s']} s, window "
                  f"{res[f'win_{tag}']}")
            if not st:
                fails.append(f"F1 ({tag}): no conversion within 30 s")
            if res[f"win_{tag}"][1] != R.PX_CENTRE:
                fails.append(f"F1 ({tag}): window {res[f'win_{tag}']}, want centred "
                             "(the insert-disk pointer's image is empty: no pan)")
            if armed:                     # a window move (as a pan makes one): time it
                n0 = nconv(m)
                m.w(m.lab("disp_px"), bytes([40]))
                pix = bytes(m.t.read_memory(A_DISP_ST + PIX_OFF, 1))[0]
                m.w(A_DISP_ST + PIX_OFF, bytes([(pix + 1) & 0xFF]))
                mv = until(m, n0, 1, 30.0)
                res["move_s"] = [round(x, 2) for x in mv]
                A.log(f"  window move (px 40 + the pixel epoch): {res['move_s']} s")
            n0 = nconv(m)
            m.w(m.lab("inp_view"), b"\x01")
            back = until(m, n0, 1, 30.0)
            res[f"back_{tag}_s"] = [round(x, 2) for x in back]
            if rd("disp_view")[0] != 0:
                fails.append(f"F1 back ({tag}): disp_view not 0")
        rep["hw"] = res
        return fails

    os.makedirs(out, exist_ok=True)
    f = A.u64_session(prg, os.path.join(out, "hold"), mhz, body)
    try:                                  # SESSION.log (brief): one line per hold
        ca = json.load(open(os.path.join(out, "hold", "config_after.json")))
        cb = json.load(open(os.path.join(out, "hold", "config_before.json")))
        with open(os.path.join(EVID, "SESSION.log"), "a") as lf:
            lf.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} run_spr hw {os.environ.get('U64_HOST')} "
                     f"{mhz} MHz s={s} lock_wait {cb.get('lock_wait_s', 0):.0f}s config "
                     f"{'IDENTICAL' if ca.get('diff_vs_before') == {} else 'DIFFERS'} "
                     f"failures {len(f)}\n")
    except OSError:
        pass
    return f

SPR_ORG, SPR_END = 0xC600, 0xC900   # the machine-mode area's spr slot (supervisor)
SPR_SRC = os.path.join(ROOT, "tests", "aros", "spr.s")
SPR_SYMS = ("mach_in", "disp_mode", "chip_shd", "mem_rec_dirty", "disp_cur",
            "disp_nconv", "disp_one", "disp_px", "disp_py", "disp_crop",
            "disp_ymax", "spr_vec", "spr_id")
SPR_ID = 0x31525053                 # "SPR1": tests/aros/spr.s SPR_ID
SPR_MUTANTS = ("SPR_MUT_MASK", "SPR_MUT_PFH", "SPR_MUT_NCONV", "SPR_MUT_PAN")


def build_spr(out_dir: str, labels, defines: tuple = ()) -> tuple[bytes, dict]:
    """W13-D D2: assemble tests/aros/spr.s at $C600 against *labels* (a
    c64_test_harness Labels or a runner.lbl path).  Returns the binary (to
    write at SPR_ORG) and its labels (spr_arm, spr_frame, ...).  *defines*:
    test-only mutants (SPR_MUT_*).  W13-W's module loader calls this."""
    import subprocess
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    if isinstance(labels, str):
        labels = Labels.from_file(labels)
    defs = []
    for n in SPR_SYMS:
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    for d in defines:
        defs += ["-D", d]
    os.makedirs(out_dir, exist_ok=True)
    obj, out = os.path.join(out_dir, "spr.o"), os.path.join(out_dir, "spr.bin")
    cfg, lbl = os.path.join(out_dir, "spr.cfg"), os.path.join(out_dir, "spr.lbl")
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n" % (SPR_ORG, SPR_END - SPR_ORG))
    for cmd in (["ca65", "-g", *defs, "-o", obj, SPR_SRC],
                ["ld65", "-C", cfg, "-o", out, "-Ln", lbl, obj]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    lab = {}
    for line in open(lbl):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return open(out, "rb").read(), lab


def model_view() -> dict | None:
    """build/aros/model_view.json (built once with the s3probe venv)."""
    import subprocess
    if not os.path.exists(MODEL_VIEW):
        if not os.path.exists(A.VENV_PY):
            return None
        os.makedirs(os.path.dirname(MODEL_VIEW), exist_ok=True)
        r = subprocess.run([A.VENV_PY, os.path.abspath(__file__), "--model", MODEL_VIEW,
                            "--prg", "-"], capture_output=True, text=True, cwd=ROOT)
        if r.returncode or not os.path.exists(MODEL_VIEW):
            print(r.stdout + r.stderr)
            return None
    with open(MODEL_VIEW) as f:
        d = json.load(f)
    if [list(v) for v in d.get("views", [])] != [list(v) for v in VIEWS]:
        os.remove(MODEL_VIEW)               # built for other windows: rebuild
        return model_view()
    return d["hashes"]


# --------------------------------------------------------------------------
# Leg "spr" (D2): guest programs under the machine loop (tests/aros/mboot.s
# aros_boot: emu_reset, mach_init, chip/cia/cop reset, disp_init, mach_run
# to the guest's HALT), the spr module armed, against c2v_ref.spr_*.
# --------------------------------------------------------------------------

PROG = 0x10000                  # the guest program (image.BASE_ADDR)
SPRD = 0x20000                  # sprite data (one list per case, 0x400 apart)
PLANES = 0x30000                # bitplanes: 4 x 0x5000 (640 x 256), zero / text
SPR_SCAN = 8                    # VERTBs each case waits before its HALT
CUST = 0xDFF000
WB_PAL = [0xAAA, 0x000, 0xFFF, 0x68B]
SPR_PAL = [0xF00, 0x0F0, 0x00F]  # COLOR17-19


def ptr_words(hstart: int, vstart: int, height: int) -> tuple[int, int]:
    """SPRxPOS / SPRxCTL for a sprite at (HSTART, VSTART), *height* lines."""
    vstop = vstart + height
    pos = ((vstart & 0xFF) << 8) | ((hstart >> 1) & 0xFF)
    ctl = ((vstop & 0xFF) << 8) | ((vstart >> 8) & 1) << 2 | ((vstop >> 8) & 1) << 1 \
        | (hstart & 1)
    return pos, ctl


def arrow(seed: int, height: int) -> list[tuple[int, int]]:
    import random
    if seed == 0:                               # an empty image (AROS's insert-disk pointer)
        return [(0, 0)] * height
    r = random.Random(seed)
    return [(r.randrange(0x10000), r.randrange(0x10000)) for _ in range(height)]


def spr_cases() -> list[dict]:
    """Each: the display (bplcon0, ddfstrt, diwstrt, lace), the view
    (view, px, py), the sprite (hstart, vstart, height, image seed), dma."""
    hi2 = {"bplcon0": 0xA200, "ddfstrt": 0x3C, "diwstrt": 0x2C81, "diwstop": 0x2CC1}
    return [
        dict(name="fit", disp=hi2, view=(0, 20, 0), spr=(0xC0, 0x60, 11, 1)),
        dict(name="fit-x>255", disp=hi2, view=(0, 20, 0), spr=(0x1C0, 0x90, 16, 2)),
        dict(name="fit-left-edge", disp=hi2, view=(0, 20, 0), spr=(0x70, 0x40, 7, 3)),
        dict(name="fit-hidden-nospren", disp=hi2, view=(0, 20, 0), spr=(0xC0, 0x60, 11, 1),
             dma=0x8300),
        dict(name="fit-image-change", disp=hi2, view=(0, 20, 0), spr=(0xC0, 0x60, 21, 9),
             spr2=(0xC8, 0x64, 13, 10)),     # SPR0PT moves to a second list mid-run
        dict(name="fit-tall-30", disp=hi2, view=(0, 20, 0), spr=(0xD0, 0x70, 30, 4)),
        dict(name="fit-ddf34", disp=dict(hi2, ddfstrt=0x34), view=(0, 20, 0),
             spr=(0xC0, 0x60, 11, 1)),
        dict(name="fit-lores-mc2", disp={"bplcon0": 0x2200, "ddfstrt": 0x38,
                                         "diwstrt": 0x2C81, "diwstop": 0x2CC1},
             view=(1, 20, 0), spr=(0xA0, 0x80, 11, 5)),
        dict(name="1:1-centre", disp=hi2, view=(1, 20, 28), spr=(0x80 + 0x81 - 1, 0x2C + 120, 11, 6)),
        dict(name="1:1-right-x>255", disp=hi2, view=(1, 40, 28), spr=(0x81 - 1 + 300, 0x2C + 110, 11, 6)),
        dict(name="1:1-pan-left", disp=hi2, view=(1, 20, 28), spr=(0x81 - 1 + 84, 0x2C + 120, 11, 6)),
        dict(name="1:1-pan-bottom", disp=hi2, view=(1, 20, 0), spr=(0x81 - 1 + 160, 0x2C + 190, 11, 6)),
        dict(name="1:1-hysteresis", disp=hi2, view=(1, 12, 28), spr=(0x81 - 1 + 100, 0x2C + 130, 11, 6)),
        dict(name="1:1-clamped-edge", disp=hi2, view=(1, 0, 28), spr=(0x81 - 1 + 4, 0x2C + 120, 11, 6)),
        dict(name="1:1-lace", disp=dict(hi2, bplcon0=0xA204, lace=True), view=(1, 20, 77),
             spr=(0x81 - 1 + 160, 0x2C + 120, 11, 7)),
        # fix round 1: an empty image never moves the window (the insert-disk
        # pointer at the top-left); a palette change with a conversion re-reads
        # the colours (nconv); the duty budget: 24 frames
        dict(name="1:1-empty-no-pan", disp=hi2, view=(1, 20, 28), spr=(0x81 - 1, 0x2C, 16, 0)),
        dict(name="fit-palette-change", disp=hi2, view=(0, 20, 0), spr=(0xC0, 0x60, 11, 1),
             pal2=[0x0FF, 0xF0F, 0xFF0], scan=40),
    ]


def spr_prog(case: dict, pt: int) -> list[int]:
    from tests import run_irq as I
    from tests.run_mach import HALT, G, mv_imm_abs_w, wait_vertb
    d = case["disp"]
    g = G()
    mv_imm_abs_w(g, 0x7FFF, CUST + 0x9A)
    mv_imm_abs_w(g, 0x7FFF, CUST + 0x9C)
    mv_imm_abs_w(g, d["bplcon0"], CUST + 0x100)
    bpu = (d["bplcon0"] >> 12) & 7
    for p in range(bpu):
        I.mv_l_imm_abs(g, PLANES + p * 0x5000, CUST + 0xE0 + 4 * p)
    mod = 80 if d.get("lace") else 0
    mv_imm_abs_w(g, mod, CUST + 0x108)
    mv_imm_abs_w(g, mod, CUST + 0x10A)
    for reg, v in ((0x8E, d["diwstrt"]), (0x90, d["diwstop"]), (0x92, d["ddfstrt"]),
                   (0x94, 0xD4 if d["bplcon0"] & 0x8000 else 0xD0)):
        mv_imm_abs_w(g, v, CUST + reg)
    for i, c in enumerate(WB_PAL):
        mv_imm_abs_w(g, c, CUST + 0x180 + 2 * i)
    for i, c in enumerate(SPR_PAL):
        mv_imm_abs_w(g, c, CUST + 0x1A2 + 2 * i)
    I.mv_l_imm_abs(g, pt, CUST + 0x120)
    mv_imm_abs_w(g, case.get("dma", 0x8320), CUST + 0x96)
    n = case.get("scan", SPR_SCAN)
    for k in range(n):
        wait_vertb(g, f"w{k}")
        if k == SPR_SCAN // 2 and "spr2" in case:
            I.mv_l_imm_abs(g, pt + 0x200, CUST + 0x120)
        if k == SPR_SCAN // 2 and "pal2" in case:
            for i, c in enumerate(case["pal2"]):
                mv_imm_abs_w(g, c, CUST + 0x1A2 + 2 * i)
            mv_imm_abs_w(g, 0x123, CUST + 0x182)   # COLOR01 too: a conversion (COLOR17-19
                                                    # alone are not display fields: a limit)
    g(HALT)
    return g.words()


def spr_expect(case: dict) -> dict:
    """The model: the settled placement, the window, the blocks, the colours."""
    from host import c2v_ref as R
    d = case["disp"]
    view, px, py = case["view"]
    hstart, vstart, height, seed = case.get("spr2", case["spr"])
    pos, ctl = ptr_words(hstart, vstart, height)
    hires = bool(d["bplcon0"] & 0x8000)
    lace = bool(d.get("lace"))
    L = 256
    one = bool(view) and hires
    ymax = max(0, L - (100 if lace else 200))
    cur = {"ddfstrt": d["ddfstrt"], "diwstrt": d["diwstrt"], "bplcon0": d["bplcon0"],
           "crop": 0}                  # W14-D: the fit view keeps the top
    lut = R.build_lut()
    if case.get("dma", 0x8320) & 0x20 == 0:
        return {"hidden": True, "px": px, "py": py, "pans": 0}
    crop0 = min(py, ymax) if one else cur["crop"]
    pans = 0
    for _ in range(4):
        nonempty = any(a | b for a, b in arrow(seed, min(height, 21)))
        r = R.spr_place(cur, pos, ctl, hires, one, px, crop0, ymax, nonempty)
        if "pan" not in r:
            break
        px, py = r["pan"]
        crop0 = min(py, ymax)
        pans += 1
    r.update(px=px, py=py, pans=pans,
             blocks=R.spr_blocks(arrow(seed, height), height),
             cols=[lut[c & 0xFFF] for c in case.get("pal2", SPR_PAL)])
    return r


def spr_read_live(m) -> dict:
    """spr_read while the guest runs (VICE: the monitor halts the 6510, so
    $01 is switched for the read and put back to the value read)."""
    return spr_read(m)


def spr_read(m) -> dict:
    """The VIC's sprite registers and the RAM under I/O ($01 = $34 around
    the read: the runner is idle at its jmp *)."""
    from c64_test_harness import read_bytes
    t = m.t
    regs = read_bytes(t, 0xD000, 0x30)
    port = read_bytes(t, 0x0001, 1)
    t.write_memory(0x0001, bytes([0x34]), override="run_spr: RAM under I/O")
    ptrs = read_bytes(t, 0xD3F8, 3)
    blocks = read_bytes(t, 0xD400, 192)
    t.write_memory(0x0001, port, override="run_spr: restore $01")
    if m.vice:
        t.resume()
    return {"regs": bytes(regs), "ptrs": bytes(ptrs), "blocks": bytes(blocks)}


def spr_run_case(m, case: dict, k: int, sbin: bytes, slab: dict, s: int) -> list[str]:
    from tests.run_program import Chunk, Session
    hstart, vstart, height, seed = case["spr"]
    pos, ctl = ptr_words(hstart, vstart, height)
    pt = SPRD + 0x400 * k
    words = [pos, ctl]
    for a, b in arrow(seed, height):
        words += [a, b]
    words += [0, 0]
    if "spr2" in case:                          # the second list at pt + $200
        h2, v2, n2, s2 = case["spr2"]
        words += [0] * (0x100 - len(words))
        words += list(ptr_words(h2, v2, n2))
        for a, b in arrow(s2, n2):
            words += [a, b]
        words += [0, 0]
    data = b"".join(w.to_bytes(2, "big") for w in words)
    prog = b"".join(w.to_bytes(2, "big") for w in spr_prog(case, pt))
    reset = (0x8000).to_bytes(4, "big") + PROG.to_bytes(4, "big")
    sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
    sess.stage([Chunk(0, reset), Chunk(PROG, prog), Chunk(pt, data)])
    m.load_driver()
    m.w(SPR_ORG, sbin)
    arm(m.w, m.lab)
    view, px, py = case["view"]
    m.w(m.lab("disp_view"), bytes([view]))
    m.w(m.lab("disp_px"), bytes([px]))
    m.w(m.lab("disp_py"), bytes([py]))
    m.w(0xD015, b"\0")                         # VIC sprites off (a fresh module: son 0)
    m.w(A.R, b"\0")
    m.start(s)
    t0 = time.monotonic()
    while m.r(A.R, 1)[0] != 0xFF:
        if time.monotonic() - t0 > 300:
            return [f"{case['name']}: the guest did not HALT in 300 s"]
        time.sleep(0.5)
    res = m.r(A.R, 3)
    f: list[str] = []
    if res[1] != 1 or res[2] != 0:
        f.append(f"halt ${res[1]:02X} reason {res[2]} (want HALT_OK)")
    got = spr_read(m)
    want = spr_expect(case)
    rg = got["regs"]
    gpx, gpy = m.r(m.lab("disp_px"), 1)[0], m.r(m.lab("disp_py"), 1)[0]
    if want.get("hidden"):
        if rg[0x15] & 7:
            f.append(f"$D015 = ${rg[0x15]:02X}: shown, want hidden")
    else:
        exp = {0x15: 7, 0x10: want["msb"], 0x1D: want["xexp"], 0x17: want["yexp"],
               0x00: want["x"], 0x02: want["x"], 0x04: want["x"],
               0x01: want["y"], 0x03: want["y"], 0x05: want["y"],
               0x1C: 0, 0x1B: 0}
        for i, c in enumerate(want["cols"]):
            exp[0x27 + i] = c
        for a, v in exp.items():
            mask = 0x0F if a >= 0x27 else (7 if a in (0x10, 0x15, 0x17, 0x1D, 0x1C, 0x1B) else 0xFF)
            if rg[a] & mask != v & mask:
                f.append(f"$D0{a:02X} = ${rg[a] & mask:02X}, want ${v & mask:02X}")
        if got["ptrs"] != bytes([0x50, 0x51, 0x52]):
            f.append(f"sprite pointers {got['ptrs'].hex()}, want 505152")
        if got["blocks"] != want["blocks"]:
            bad = sum(a != b for a, b in zip(got["blocks"], want["blocks"]))
            f.append(f"sprite blocks: {bad}/192 bytes differ")
        if (gpx, gpy) != (want["px"], want["py"]) and view:
            f.append(f"window (px {gpx}, py {gpy}), want ({want['px']}, {want['py']}) "
                     f"after {want['pans']} pan(s)")
    print(f"  {case['name']:20s} {'OK' if not f else 'FAIL'}  "
          f"{'hidden' if want.get('hidden') else 'x %d y %d msb %d' % (want['x'], want['y'], want['msb'])}"
          f"  window ({gpx}, {gpy}) pans {want.get('pans', 0)}")
    for x in f:
        print(f"      {x}")
    return [f"{case['name']}: {x}" for x in f]


CALL_T, CALL_OUT = 0xC300, 0xC3F0     # td's slot: unused by this leg


def spr_call(m, slab: dict) -> int:
    """phi2 cycles of one spr_frame call as disp_vw makes it: $01 = $35,
    mach_in = 1, IRQs masked (run_c2v's CIA2 TA->TB count, screen blanked)."""
    from tests import run_c2v as C
    mi = m.lab("mach_in")
    t = bytes([0x78, 0xA9, 0x01, 0x8D, mi & 0xFF, mi >> 8,        # sei / mach_in := 1
               0xA9, 0x35, 0x85, 0x01])                            # $01 := $35
    t += C.PERF_T_START
    t += bytes([0x20, SPR_ORG & 0xFF, SPR_ORG >> 8])               # jsr spr_frame
    t += bytes([0xA9, 0x00, 0x8D, 0x0E, 0xDD])                     # stop TA
    t += bytes([0xA9, 0x37, 0x85, 0x01, 0xA9, 0x00, 0x8D, mi & 0xFF, mi >> 8])
    for i, reg in enumerate((0xDD04, 0xDD05, 0xDD06, 0xDD07)):
        o = CALL_OUT + i
        t += bytes([0xAD, reg & 0xFF, reg >> 8, 0x49, 0xFF, 0x8D, o & 0xFF, o >> 8])
    t += bytes([0x68, 0x8D, 0x11, 0xD0, 0x58, 0x60])               # pla / sta $D011 / cli / rts
    m.w(CALL_T, t)
    from tests.harness_util import call_runner
    call_runner(m.tg, CALL_T, 60.0)
    return int.from_bytes(m.r(CALL_OUT, 4), "little")


def spr_calls(m, sbin: bytes, slab: dict, s: int) -> list[str]:
    """Per-frame cost (PERF (o)) and hidden -> hidden writes nothing: after
    the "fit" case's run, spr_frame called directly: no change (the cache),
    a position change (POS/CTL: the image is read again, as the brief
    asks), an image change (SPR0PT), shown -> hidden, hidden -> hidden."""
    from tests.run_program import Chunk, Session
    f: list[str] = []
    case = spr_cases()[0]
    f += spr_run_case(m, case, 0, sbin, slab, s)
    out = {}
    out["no change"] = spr_call(m, slab)
    out["no change (again)"] = spr_call(m, slab)
    hstart, vstart, height, seed = case["spr"]
    pos, ctl = ptr_words(hstart + 6, vstart + 3, height)
    sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
    sess.stage([Chunk(SPRD, pos.to_bytes(2, "big") + ctl.to_bytes(2, "big"))])
    out["position change"] = spr_call(m, slab)
    out["position, then no change"] = spr_call(m, slab)
    shd = m.lab("chip_shd") + 0x120
    m.w(shd + 2, ((SPRD + 0x400) >> 8 & 0xFF).to_bytes(1, "little"))   # case 1's list
    out["image change (SPR0PT)"] = spr_call(m, slab)
    st = A_DISP_ST + 1
    dmacon = m.r(st, 1)[0]
    m.w(st, bytes([dmacon & ~0x20 & 0xFF]))  # SPREN off
    out["shown -> hidden"] = spr_call(m, slab)
    if m.r(0xD015, 1)[0] & 7:
        f.append("sprcall: shown -> hidden left $D015 bits 0-2 set")
    poison = bytes((0xA5 + i) & 0xFF for i in range(0x2F))
    keep = m.r(0xD011, 2) + m.r(0xD016, 1) + m.r(0xD018, 1) + m.r(0xD019, 2)
    m.w(0xD000, poison[:0x11])               # sprite X/Y, $D010
    m.w(0xD015, poison[0x15:0x16])
    m.w(0xD017, poison[0x17:0x18])
    m.w(0xD01B, poison[0x1B:0x1E])
    m.w(0xD027, poison[0x27:0x2A])
    before = m.r(0xD000, 0x2F)
    out["hidden -> hidden"] = spr_call(m, slab)
    after = m.r(0xD000, 0x2F)
    regs = list(range(0, 0x11)) + [0x15, 0x17, 0x1B, 0x1C, 0x1D, 0x27, 0x28, 0x29]
    if any(before[a] != after[a] for a in regs):
        f.append("sprcall: hidden -> hidden wrote a VIC sprite register")
    m.w(st, bytes([dmacon]))
    for k, v in out.items():
        print(f"  perf spr_frame {k:28s} {v:7d} cycles")
    RESULTS["sprcall"] = out
    return [f"sprcall: {x}" for x in f]


RESULTS: dict = {}


def spr_guard(m, sbin: bytes, slab: dict, s: int) -> list[str]:
    """Fix round 1: spr_vec armed, then the module's bytes overwritten (a
    tool stub, a resume without re-staging): the next frame hook must
    disarm (spr_vec high byte 0) and the guest must run on to its HALT."""
    from tests.run_program import Chunk, Session
    case = spr_cases()[0]
    f = spr_run_case(m, case, 0, sbin, slab, s)          # armed and working
    hstart, vstart, height, seed = case["spr"]
    pos, ctl = ptr_words(hstart, vstart, height)
    prog = b"".join(w.to_bytes(2, "big") for w in spr_prog(case, SPRD))
    sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
    sess.stage([Chunk(PROG, prog)])
    m.load_driver()
    m.w(SPR_ORG, sbin)
    arm(m.w, m.lab)
    m.w(SPR_ORG, bytes([0x00] * 0x40))                     # the ID (and code) overwritten
    m.w(0xD015, b"\x07")                                  # sprites shown by the old arming
    m.w(A.R, b"\0")
    m.start(s)
    t0 = time.monotonic()
    while m.r(A.R, 1)[0] != 0xFF:
        if time.monotonic() - t0 > 120:
            return f + ["guard: the guest did not HALT (the overwritten module ran?)"]
        time.sleep(0.5)
    res = m.r(A.R, 3)
    hi = m.r(m.lab("spr_vec") + 1, 1)[0]
    en = m.r(0xD015, 1)[0] & 7
    ok = res[1] == 1 and res[2] == 0 and hi == 0 and en == 0
    print(f"  {'guard-overwritten':20s} {'OK' if ok else 'FAIL'}  halt ${res[1]:02X} "
          f"reason {res[2]} spr_vec hi ${hi:02X} $D015&7 {en}")
    if not ok:
        f.append(f"guard: halt ${res[1]:02X} reason {res[2]}, spr_vec hi ${hi:02X}, "
                 f"$D015&7 {en} (want HALT_OK, disarmed, sprites hidden)")
    return f


def arm(w, lab) -> None:
    """W13-D D2: arm the staged module: spr_vec := spr_frame (high byte last),
    spr_id := SPR_ID (a resume compares it; W13-W's loader does the same)."""
    entry = SPR_ORG                     # spr.s: jmp spr_frame at $C600
    w(lab("spr_vec") + 1, b"\0")       # (disarmed while it is written)
    w(lab("spr_id"), SPR_ID.to_bytes(4, "little"))       # fix round 2: the ID first,
    w(lab("spr_vec"), bytes([entry & 0xFF]))             # then the vector low,
    w(lab("spr_vec") + 1, bytes([entry >> 8]))           # then high (arms)
    w(0xD01B, b"\0")                    # VIC sprite priority / multicolour: 0
    w(0xD01C, b"\0")                    # (hires sprites in front; the module never writes them)


def leg_spr(prg: str, s: int, defines: tuple = ()) -> list[str]:
    from tests.harness_util import lookup_label
    out = os.path.join(os.path.dirname(os.path.abspath(prg)), "spr-mod")

    def body(m) -> list[str]:
        try:
            sbin, slab = build_spr(out, m.labels, defines)
        except KeyError as e:
            print(f"  spr: FAIL ({e}: no pointer hook in this PRG)")
            return [f"spr: {e}"]
        A.log(f"spr module: {len(sbin)} bytes at ${SPR_ORG:04X}-${SPR_ORG + len(sbin) - 1:04X}")
        fails: list[str] = []
        for k, case in enumerate(spr_cases()):
            fails += spr_run_case(m, case, k, sbin, slab, s)
        fails += spr_calls(m, sbin, slab, s)
        fails += spr_guard(m, sbin, slab, s)
        return fails
    return A.vice_session(prg, body)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default="aros1")
    ap.add_argument("--s", type=int, default=9)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", default=os.path.join(EVID, "aros1"))
    ap.add_argument("--model", help="(s3probe venv) write the 1:1 model hashes")
    ap.add_argument("--spr-s", type=int, default=2, help="leg spr: mach_s")
    ap.add_argument("--hw-s", type=int, default=4, help="leg hw: mach_s")
    ap.add_argument("--mhz", type=int, default=64, help="leg hw: CPU speed")
    ap.add_argument("--mutants", action="store_true",
                    help="leg spr with each SPR_MUT_* module mutant: each must fail")
    a = ap.parse_args(argv)
    if a.model:
        return model_main(a.model)
    if a.mutants:
        caught = {}
        for name in SPR_MUTANTS:
            print(f"mutant {name}:")
            caught[name] = len(leg_spr(a.prg, a.spr_s, (f"{name}=1",)))
        print("mutants: " + ", ".join(f"{k} {'CAUGHT' if v else 'MISSED'} ({v})"
                                      for k, v in caught.items()))
        return 0 if all(caught.values()) else 1
    rep: dict = {"prg": os.path.abspath(a.prg), "s": a.s}
    fails: list[str] = []
    t0 = time.monotonic()
    if "aros1" in a.legs.split(","):
        fails += leg_aros1(a.prg, a.s, a.timeout, a.out, rep)
    if "hw" in a.legs.split(","):
        fails += leg_hw(a.prg, a.hw_s, a.mhz, a.timeout, a.out, rep)
    if "spr" in a.legs.split(","):
        fails += leg_spr(a.prg, a.spr_s)
        rep["sprcall"] = RESULTS.get("sprcall")
    rep["wall_s"] = round(time.monotonic() - t0, 1)
    rep["failures"] = fails
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "report.json"), "w") as f:
        json.dump(rep, f, indent=1)
    for x in fails:
        print(f"  FAIL {x}")
    print(f"run_spr: {len(fails)} failure(s), {rep['wall_s']} s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
