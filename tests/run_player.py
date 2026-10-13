#!/usr/bin/env python3
"""W15-M M3: the integrated MOD player (replay + W15-U's UI + W15-P's Paula
module) on an Ultimate, instrumental; a VICE dry run of the same flow.

docs/WAVE15_BRIEF.md M3 and the M3 GO (supervisor).  One session:

  * the product runner PRG boots (prepare_target); the system's video
    standard is read by the 6510 (the VIC's last raster line: PAL 311,
    NTSC 262), so phi2 = 985248 (PAL) or 1022727 Hz (NTSC);
  * the player (fixtures/mod, UI_SRC=ui/ui.s, MOD_TRACE) is built with
    PF_REALTIME, s = 0, tick_e = round(phi2 / 50) >> s (PAL 19705, NTSC
    20455); W15-P's module (tests/paula/aud.s, run_paula.build_aud) with
    cfg_r16 = paula_ref.r16_for(phi2 >> s) and cfg_hw = 1 (the sampler;
    0 on VICE: no sampler there);
  * pb_fill zeroes guest chip RAM; the image, parameters and MOD are
    staged (REU DMA on the Ultimates, test_load_chunk on VICE); then
    tests/mod/pboot.s, the module's segments and its config; pb_boot boots
    the program (exception mode), arms the module, runs mach_run;
  * while it plays: the UDP audio stream is captured (Ultimates) and the
    C64-side counters are polled (disp_nconv, mach_frames, mach_nirq);
    guest memory is read only after the halt (a runner call during the
    run perturbs it);
  * the program halts after max_ticks (the song's own length for the
    probe; N seconds for the demo); then status, mod_view and the trace.

Checks (asserted; the instrumental M3 gate):
  * no fault, state done; the trace and every tick's view equal mod_ref
    (run_mod.check_case: the same byte-exact comparison as the model and
    VICE legs, now with the real UI and the real Paula module);
  * tempo: ticks/s from the guest's own tick timestamps (CIA-A TOD frames
    + beam; at s = 0 guest E = phi2, real time) within 1% of the song's
    real-time rate (phi2 / 50 x 125 / BPM per tick); a wall-clock
    cross-check (coarse: host polling);
  * pitch (Ultimates): the "probe" song plays one channel, a sine of 32
    samples, one note per 4 rows; every note's frequency in the captured
    audio against the sampler law for its Paula period (run_paula.f_expect,
    PITCH_TOL; the sample time constant of the device's /v1/info product,
    paula_ref.sample_c) and against Paula's (0.5%), aligned to the audio by
    the first sustained tone (align); the stream rate implied by the last
    note's onset within RATE_CHECK_TOL of the nominal (else the alignment,
    not the pitch, is reported);
  * the UI updated (disp_nconv grew while it played);
  * headroom, reported: the deepest catch-up (S_MAXCU), stage flushes,
    level-6 entries, deliveries, the module's counters.

    python3 -m tests.run_player --prg build/X/runner.prg --backend vice     # dry run
    U64_HOST=<ip> python3 -m tests.run_player --prg ... --backend u64 --mhz 64 \\
        [--seconds 20]                                   # instrumental (one hold)
    U64_HOST=<ip> python3 -m tests.run_player --prg ... --backend u64 --mhz 64 \\
        --attended [--song demo] [--seconds 60]          # the listening window
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import mod_ref, modgen  # noqa: E402
from tests import run_mod as RM  # noqa: E402

PB_ORG, PB_END = 0xC000, 0xC200
P, R = 0xC900, 0xC940
TRAMP = R + 0x30
PAL_PHI2, NTSC_PHI2 = 985248, 1022727
PLAY_S = 0
EVID = os.path.join(ROOT, "build", "evidence", "w15-m3")
PB_SYMS = ("emu_reset", "mem_read32", "mem_rec_dirty", "mach_init", "mach_run",
           "mach_s", "chip_reset", "cia_reset", "cop_reset", "disp_init",
           "snap_arm", "aud_mon", "mach_cbv")
LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


# --------------------------------------------------------------------------
# Builds
# --------------------------------------------------------------------------

def build_pboot(labels_path: str, out_dir: str) -> tuple[bytes, dict]:
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    labels = Labels.from_file(labels_path)
    defs = []
    for n in PB_SYMS:
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    os.makedirs(out_dir, exist_ok=True)
    obj, out = os.path.join(out_dir, "pboot.o"), os.path.join(out_dir, "pboot.bin")
    cfg, lbl = os.path.join(out_dir, "pboot.cfg"), os.path.join(out_dir, "pboot.lbl")
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n" % (PB_ORG, PB_END - PB_ORG))
    for cmd in (["ca65", "-g", *defs, "-o", obj, os.path.join(ROOT, "tests", "mod", "pboot.s")],
                ["ld65", "-C", cfg, "-o", out, "-Ln", lbl, obj]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    lab = {}
    for line in open(lbl):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return open(out, "rb").read(), lab


# --------------------------------------------------------------------------
# The rig (VICE or an Ultimate; one prepared target)
# --------------------------------------------------------------------------

class RigIntegrityError(RuntimeError):
    """pboot or the module's segments do not match the build (rig rule (b))."""


class Rig:
    def __init__(self, target, labels, prg: str):
        from tests.harness_util import lookup_label
        from tests.run_paula import build_aud
        self.tg, self.t = target, target.transport
        self.vice = target.backend == "vice"
        self.labels = labels
        self.lab = lambda n: lookup_label(labels, n)
        d = os.path.join(os.path.dirname(os.path.abspath(prg)), "player")
        lbl = os.path.splitext(prg)[0] + ".lbl"
        self.pb, self.pl = build_pboot(lbl, d)
        self.a, self.b, self.alab = build_aud(lbl, os.path.join(d, "aud"))
        try:                                    # W13-I: idle scripted input
            self.w(self.lab("inp_src"), bytes([1, 0, 0]))
        except KeyError:
            pass

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        if addr < 0x0800 and addr + n > 0x0400:
            raise RuntimeError(f"read of ${addr:04X}+{n}: the module's segment B")
        return self._read(addr, n)

    def _read(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        b = read_bytes(self.t, addr, n)
        if self.vice:
            self.t.resume()
        return b

    def check_segments(self) -> None:
        """Rig rule (b): pboot, the module's segment A and segment B read
        back equal to the build before every play (the 6510 idles in the
        runner then); the only read of B."""
        for name, addr, want in (("pboot", PB_ORG, self.pb), ("segment A", 0xCA00, self.a),
                                 ("segment B", 0x0400, self.b)):
            got = self._read(addr, len(want))
            if got != want:
                bad = [i for i in range(len(want)) if got[i] != want[i]]
                raise RigIntegrityError(
                    f"{name} at ${addr:04X} differs from the build in {len(bad)} of "
                    f"{len(want)} bytes, first ${addr + bad[0]:04X}: "
                    f"${got[bad[0]]:02X} (want ${want[bad[0]]:02X})")

    def call(self, addr: int, timeout: float = 300.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    def pcall(self, name: str) -> bytes:
        self.w(R, b"\0")
        self.call(self.pl[name])
        res = self.r(R, 0x20)
        if res[0] != 0xFF:
            raise RuntimeError(f"{name} did not complete")
        return res

    def video(self) -> tuple[str, int, int]:
        """The 6510 reads the VIC's last raster line: PAL 311, NTSC 262."""
        code = bytes([0x2C, 0x11, 0xD0, 0x10, 0xFB,         # w: bit $D011 / bpl w
                      0xA9, 0x00, 0x8D, 0xF0, 0xC1,         # lda #0 / sta $C1F0
                      0xAD, 0x12, 0xD0, 0xCD, 0xF0, 0xC1,   # l: lda $D012 / cmp $C1F0
                      0x90, 0x03, 0x8D, 0xF0, 0xC1,         # bcc s / sta $C1F0
                      0x2C, 0x11, 0xD0, 0x30, 0xF0,         # s: bit $D011 / bmi l
                      0x60])
        self.w(0xC1C0, code)
        self.call(0xC1C0)
        last = 256 + self.r(0xC1F0, 1)[0]
        if last >= 300:
            return "PAL", last, PAL_PHI2
        return "NTSC", last, NTSC_PHI2

    def stage(self, case: RM.Case, img: bytes) -> None:
        self.w(PB_ORG, self.pb)
        self.w(P, bytes([0, 0x20]))             # guest chip RAM $000000-$1FFFFF
        self.pcall("pb_fill")
        chunks = case.chunks(img)
        if self.vice:
            from tests.run_program import Chunk, Session
            Session(target=self.tg, labels=self.labels, timeout=300.0).stage(
                [Chunk(a, d) for a, d in chunks])
        else:
            for a, d in chunks:
                self.t.socket_dma_reu_write(a, d)
        self.w(PB_ORG, self.pb)                 # staging may use buf_case
        self.w(0xCA00, self.a)                  # the module, segment A
        self.w(0x0400, self.b)                  # segment B (the text screen)

    def start(self, s: int, r16: int, hw: int) -> None:
        self.w(0xCA00 + 13, struct.pack("<HB", r16, hw) + (0xDF4000).to_bytes(3, "little"))
        self.w(P + 12, bytes([s, 1, self.a[3]]))
        self.w(R, b"\0")
        a = self.pl["pb_boot"]
        if self.vice:
            from c64_test_harness import goto
            self.w(TRAMP, bytes([0x20, a & 0xFF, a >> 8, 0x4C, (TRAMP + 3) & 0xFF,
                                 (TRAMP + 3) >> 8]))
            goto(self.t, TRAMP)
        else:
            disp = self.tg._u64_dispatch
            self.w(disp["jsr_operand"], bytes([a & 0xFF, a >> 8]))
            self.w(disp["mbox"], bytes([0x01, 0x00]))

    def counters(self) -> dict:
        nconv = struct.unpack("<H", self.r(self.lab("disp_nconv"), 2))[0]
        frames = struct.unpack("<I", self.r(0x96, 4))[0]
        nirq = struct.unpack("<I", self.r(self.lab("mach_nirq"), 4))[0]
        return {"t": time.monotonic(), "nconv": nconv, "frames": frames, "nirq": nirq}

    def aud_state(self) -> dict:
        h = self.r(0xCA00 + 13, 18)
        le = lambda b: int.from_bytes(b, "little")
        return dict(r16=le(h[0:2]), hw=h[2], nw=le(h[6:8]), nfire=le(h[8:10]),
                    ndat=h[10], nres=h[15], npark=le(h[16:18]))


# --------------------------------------------------------------------------
# One play
# --------------------------------------------------------------------------

def tick_rate(c: RM.Case, recs: bytes, phi2: int, s: int) -> dict:
    """Measured ticks/s from the guest's timestamps vs the song's rate."""
    n = len(recs) // RM.TR_REC
    ok = [i for i in range(n) if not RM.torn(recs, i)]
    if len(ok) < 10:
        return {"n": n, "measured": 0.0, "expected": 0.0, "err": 1.0}
    i0, i1 = ok[0], ok[-1]
    e = RM.rec_time(recs, i1) - RM.rec_time(recs, i0)       # guest E = phi2 >> s
    secs = (e << s) / phi2
    meas = (i1 - i0) / secs
    # the song's real-time rate over the same ticks: tick k lasts 1/50 s x 125 / B_k
    dur = 0.0
    for k in range(i0, i1):
        b = c.ticks[k].bpm if (k + 1 < len(c.ticks) and c.ticks[k + 1].cia) else 125
        dur += 125 / (50 * b)
    exp = (i1 - i0) / dur
    return {"n": n, "measured": meas, "expected": exp, "err": meas / exp - 1,
            "torn": n - len(ok)}


def play(rig: Rig, c: RM.Case, img: bytes, syms: dict, s: int, r16: int, hw: int,
         capture: bool, out: str, deadline_s: float,
         pal: bool = False) -> tuple[dict, dict | None]:
    rig.stage(c, img)
    rig.check_segments()
    log(f"{c.name}: pboot, segment A, segment B equal the build")
    before = rig.counters()
    cap = au = None
    if capture:
        cap = Capture(rig, out, c.name, pal)
        cap.begin()
    t0 = time.monotonic()
    rig.start(s, r16, hw)
    polls = [before]
    try:
        while True:
            time.sleep(1.0)
            r = rig.r(R, 0x10)
            polls.append(rig.counters())
            if len(polls) % 10 == 0:
                p = polls[-1]
                log(f"  {p['t'] - t0:5.0f} s: frames {p['frames'] - before['frames']}, "
                    f"deliveries {(p['nirq'] - before['nirq']) & 0xFFFFFFFF}, UI conversions "
                    f"{p['nconv'] - before['nconv']}")
            if r[0] == 0xFF:
                break
            if time.monotonic() - t0 > deadline_s:
                raise TimeoutError(f"{c.name}: no halt in {deadline_s:.0f} s")
    finally:
        if cap:
            au = cap.end()
    wall = time.monotonic() - t0
    aud = rig.aud_state()                   # before any guest peek (it uses $C800-$CBFF)
    g = Reader(rig)
    st = g.read(RM.STATUS, 64)
    twp, ttp = struct.unpack_from(">II", st, 32)
    stream = g.read(RM.TR_W, twp - RM.TR_W) if twp > RM.TR_W else b""
    recs = g.read(RM.TR_T, ttp - RM.TR_T) if ttp > RM.TR_T else b""
    view = g.read(RM.VIEW, RM.VIEW_LEN)
    with open(os.path.join(out, f"recs_{c.name}.bin"), "wb") as fh:   # offline analysis
        fh.write(recs)
    c.timing = False                    # real-time pacing: checked by tick_rate below
    f = RM.check_case(c, st, stream, recs, view, None)
    res = {"halt": r[1], "reason": r[2], "frames": struct.unpack_from("<I", r, 12)[0],
           "wall": wall, "fails": f, "status": st.hex(),
           "maxcu": st[28], "flush": struct.unpack_from(">H", st, 40)[0],
           "l6": struct.unpack_from(">H", st, 42)[0], "torn": getattr(c, "torn", 0),
           "nconv": polls[-1]["nconv"],     # since this boot (disp_init zeroes it)
           "nirq": (polls[-1]["nirq"] - before["nirq"]) & 0xFFFFFFFF,
           "aud": aud, "recs": recs}
    if res["halt"] != 1 or res["reason"]:
        f.append(f"{c.name}: mach_run returned st_halt {res['halt']} reason {res['reason']}")
    return res, au


class Reader:
    """Guest memory after the halt: one REU -> C64 DMA per KB, the code at
    $C600 and the bytes at $C200-$C5FF (the td/spr module areas: never
    armed in the player profile).  Not run_dos.Guest: its $C800-$CBFF
    buffer covers pboot's P/R and the audio module's segment A, and a
    second play in the session then hung the 6510 in the module (W15-M M3
    dry run, build/evidence/w15-m3)."""
    CODE, DST, MAX = 0xC600, 0xC200, 0x400

    def __init__(self, rig: "Rig"):
        self.rig = rig

    def read(self, addr: int, n: int) -> bytes:
        from tests.run_dos import REU_BLOCK
        out = bytearray()
        dirty = self.rig.lab("mem_rec_dirty")
        while n > 0:
            k = min(n, self.MAX, REU_BLOCK - (addr & (REU_BLOCK - 1)))
            code = bytes([
                0xA9, self.DST & 0xFF, 0x8D, 0x02, 0xDF, 0xA9, self.DST >> 8, 0x8D, 0x03, 0xDF,
                0xA9, addr & 0xFF, 0x8D, 0x04, 0xDF, 0xA9, (addr >> 8) & 0xFF, 0x8D, 0x05, 0xDF,
                0xA9, (addr >> 16) & 0xFF, 0x8D, 0x06, 0xDF,
                0xA9, k & 0xFF, 0x8D, 0x07, 0xDF, 0xA9, k >> 8, 0x8D, 0x08, 0xDF,
                0xA9, 0x00, 0x8D, 0x0A, 0xDF, 0xA9, 0x91, 0x8D, 0x01, 0xDF,
                0x20, dirty & 0xFF, dirty >> 8, 0x60])
            self.rig.w(self.CODE, code)
            self.rig.call(self.CODE)
            out += self.rig.r(self.DST, k)
            addr, n = addr + k, n - k
        return bytes(out)


class Capture:
    """The UDP audio stream around a play (run_paula.HwRig.capture's way)."""

    def __init__(self, rig: Rig, out: str, tag: str, pal: bool):
        self.rig, self.out, self.tag, self.pal = rig, out, tag, pal

    def rate(self):
        """The stream's exact rate: phi2 x 3 / 64 (the harness's
        PHI2_CYCLES_PER_AUDIO_SAMPLE); NTSC 2109375/44 (its constant), PAL
        phi2 = 17734475/18 Hz."""
        from fractions import Fraction
        from c64_test_harness.backends.u64_audio_capture import U64_NTSC_AUDIO_RATE_HZ
        return Fraction(17734475 * 3, 18 * 64) if self.pal else U64_NTSC_AUDIO_RATE_HZ

    def begin(self) -> None:
        import socket
        from c64_test_harness.backends.u64_audio_capture import AudioCapture
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((os.environ["U64_HOST"], 80))
        ip = s.getsockname()[0]
        s.close()
        self.cap = AudioCapture(port=0, sample_rate=self.rate())
        self.cap.start()
        self.wav = os.path.join(self.out, f"audio_{self.tag}.wav")
        self.rig.t.client.stream_audio_start(f"{ip}:{self.cap.port}")
        self.t0 = time.monotonic()
        time.sleep(0.3)

    def end(self) -> dict:
        import wave
        import numpy as np
        time.sleep(0.3)
        try:
            self.rig.t.client.stream_audio_stop()
        except Exception as e:                  # noqa: BLE001
            log(f"stream_audio_stop failed: {e!r}")
        t1 = time.monotonic()
        info = self.cap.stop(wav_path=self.wav)
        with wave.open(self.wav, "rb") as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(
                -1, w.getnchannels()).astype(np.float64)
        fill = getattr(info, "packets_filled", 0)
        intact = (not getattr(info, "sequence_resyncs", 0) and not getattr(
            info, "payloads_discarded", 0) and not getattr(info, "nonstandard_payloads", 0))
        return {"l": raw[:, 0], "r": raw[:, 1], "mono": raw.mean(axis=1),
                "rate": float(self.rate()), "packets": info.packets_received,
                "dropped": info.packets_dropped, "filled": fill,
                "resyncs": getattr(info, "sequence_resyncs", 0),
                "discarded": getattr(info, "payloads_discarded", 0),
                "intact": intact, "wav": self.wav,
                "rate_wall": len(raw) / (t1 - self.t0)}     # coarse: host clock


ALIGN_HOLD = 0.05      # s: the first tone must last this long (a DC step is 1-2 10 ms windows)
RATE_CHECK_TOL = 0.01  # the cross-check's bound: 1% of the 3.36 s span is 34 ms, under the
                       # 20% margin (~92 ms) each pitch window keeps inside its note, and
                       # 7x the spread of the measurement itself (+0.14% on both devices:
                       # 20 ms windows, 5 ms hop); the sup16 C64U misalignment read +6.7%


def align(au: dict, thr: float = 500.0) -> int:
    """The audio's first sample of the song: the first tone held ALIGN_HOLD
    seconds.  Not the first loud window: the C64U's stream steps from its
    idle DC 0.22 s before the first note (W16-A, build/evidence/sup16/c64u-pitch)."""
    from tests.run_paula import find_tone
    return find_tone(au["mono"], au["rate"], thr=thr, hold=ALIGN_HOLD)[0]


def pitch_check(c: RM.Case, res: dict, au: dict, phi2: int, s: int,
                law_c: float | None = None) -> tuple[list[str], list]:
    """Each probe note's frequency (middle 60% of its rows) against the
    sampler law (*law_c*: the device's constant, paula_ref.sample_c) and
    Paula; aligned by the first sustained tone; the stream-rate cross-check
    asserted (RATE_CHECK_TOL)."""
    from host import paula_ref as PR
    from tests.run_paula import PITCH_TOL, f_expect, peak
    law_c = PR.SAMPLE_C_LAW if law_c is None else law_c
    recs = res["recs"]
    f, rows = [], []
    a = align(au)
    t_first = RM.rec_time(recs, 0)
    sec = lambda i: ((RM.rec_time(recs, i) - t_first) << s) / phi2
    notes = []                                  # (first tick, last tick, period, instrument)
    for i, t in enumerate(c.ticks):
        p, ins = struct.unpack_from(">HB", t.view, 36)
        if notes and notes[-1][2] == p and notes[-1][3] == ins:
            notes[-1][1] = i
        else:
            notes.append([i, i, p, ins])
    for i0, i1, per, ins in notes:
        if not per or ins != 1 or i1 - i0 < 12:      # the sine notes only
            continue
        lo, hi = sec(i0), sec(i1)
        m0, m1 = lo + 0.2 * (hi - lo), hi - 0.2 * (hi - lo)
        x = au["l"][a + int(m0 * au["rate"]):a + int(m1 * au["rate"])]   # channel 0: left
        pk = peak(x, au["rate"])
        fs, fp = f_expect(per, 32, law_c)
        rows.append({"per": per, "f": pk["f"], "f_sampler": fs, "f_paula": fp,
                     "t": round(lo, 3), "rms": pk["rms"]})
        if abs(pk["f"] / fs - 1) > PITCH_TOL:
            f.append(f"pitch PER {per} at {lo:.2f} s: {pk['f']:.2f} Hz, sampler law {fs:.2f}")
        if abs(pk["f"] / fp - 1) > 0.005:
            f.append(f"pitch PER {per} at {lo:.2f} s: {pk['f']:.2f} Hz, Paula {fp:.2f} (> 0.5%)")
    if not rows:
        f.append("pitch: no note analysed")
    # a cross-check of the stream rate: the last note's onset in the audio
    # against the guest's own time between the first and the last note
    from tests.run_paula import onset
    if len(notes) >= 2 and rows:
        i_last = [n for n in notes if n[2] and n[3] == 1][-1][0]
        f_old = [rows[-2]["f_sampler"]] if len(rows) > 1 else []
        dt = onset(au["l"], au["rate"], a + int((sec(i_last) - 0.1) * au["rate"]),
                   a + int((sec(i_last) + 0.3) * au["rate"]), rows[-1]["f_sampler"], f_old)
        if dt is None:
            f.append("stream rate cross-check: the last note's onset is not in the audio "
                     "(the alignment, not the pitch)")
        else:
            t_audio = sec(i_last) - 0.1 + dt
            rc = au["rate"] * t_audio / sec(i_last)
            rows.append({"rate_check": round(rc, 1),
                         "note": "the stream rate implied by the last note's onset "
                                 "(+-5 ms windows: about +-0.2%)"})
            if abs(rc / au["rate"] - 1) > RATE_CHECK_TOL:
                f.append(f"stream rate cross-check: {rc:.1f} Hz vs {au['rate']:.1f} "
                         f"(> {RATE_CHECK_TOL:.0%}): the alignment, not the pitch")
    return f, rows


def note_ends(c: RM.Case, res: dict, au: dict, phi2: int, s: int, side: str = "l",
              chan: int = 0, law_c: float | None = None) -> dict:
    """What follows each note's end on *chan* (output *side*), against what
    Paula should play (paula_ref's sampler law, the trace's write timeline):

    * every trigger: a gap (|x| at the noise floor) and the largest
      sample-to-sample step within 3 ms of it (the DMA-off -> restart window);
    * every one-shot end (trigger + 2 LEN / rate): the 5 ms around it (the
      largest step) and the rest until the next trigger: DC (mean minus the
      pre-song floor), RMS (AC), the strongest spectral peak (a buzz), and
      the level relative to the note's own RMS;
    * the song end (DMACON off): the same, for 0.25 s.
    Timestamps in seconds from the first trigger in the audio (align);
    one-shot lengths by the device's sample time constant *law_c*."""
    import numpy as np
    from host import paula_ref as PR
    from tests.run_paula import peak
    law_c = PR.SAMPLE_C_LAW if law_c is None else law_c
    x = au[side]
    rate = au["rate"]
    recs = res["recs"]
    a = align(au, thr=200.0)
    floor = x[max(0, a - int(0.15 * rate)):max(1, a - int(0.05 * rate))]   # after a DC step
    base = float(floor.mean()) if len(floor) else 0.0
    noise = float(floor.std()) if len(floor) else 0.0
    t0 = RM.rec_time(recs, 0)
    sec = lambda i: ((RM.rec_time(recs, i) - t0) << s) / phi2
    idx = lambda t: a + int(t * rate)
    bit = 1 << chan
    trig = []                                   # (tick, LEN words, PER, sample bytes)
    for i, t in enumerate(c.ticks):
        w = t.writes
        if any(r == mod_ref.DMACON and not v & 0x8000 and v & bit for r, v in w):
            ln = next((v for r, v in w if r == mod_ref.AUD[chan] + 4), 0)
            per = next((v for r, v in w if r == mod_ref.AUD[chan] + 6), 0)
            rln = [v for r, v in w if r == mod_ref.AUD[chan] + 4][-1:]   # after the set: repeat
            trig.append((i, ln, per, rln[0] if rln else 0))
    out = {"floor_rms": round(noise, 1), "triggers": [], "oneshot_ends": [], "song_end": None}
    for k, (i, ln, per, rln) in enumerate(trig):
        ts = sec(i)
        w = x[idx(ts) - int(0.003 * rate):idx(ts) + int(0.003 * rate)]
        if len(w) < 4:
            continue
        d = np.abs(np.diff(w))
        quiet = np.abs(w - base) <= 3 * max(noise, 1.0)
        run = best = 0
        for q in quiet:
            run = run + 1 if q else 0
            best = max(best, run)
        out["triggers"].append({"t": round(ts, 4), "per": per, "max_step": float(d.max()),
                                "gap_ms": round(best / rate * 1000, 2)})
        if not ln or not per or rln > 1:        # a looped note: retriggered, no end
            continue
        dur = 2 * ln * PR.sample_ns(PR.sampler_rate(per), law_c) / 1e9
        nxt = sec(trig[k + 1][0]) if k + 1 < len(trig) else ts + dur + 0.25
        if ts + dur + 0.01 >= nxt:              # cut by the next trigger before its end
            continue
        note = x[idx(ts + 0.2 * dur):idx(ts + 0.8 * dur)]
        aft = x[idx(ts + dur + 0.005):idx(nxt - 0.005)]
        edge = x[idx(ts + dur - 0.0025):idx(ts + dur + 0.0025)]
        pk = peak(aft, rate) if len(aft) > 64 else {"f": 0.0, "rms": 0.0}
        nrms = float(note.std()) if len(note) else 0.0
        out["oneshot_ends"].append({
            "t_end": round(ts + dur, 4), "per": per, "after_s": round(nxt - ts - dur, 3),
            "note_rms": round(nrms, 1),
            "after_dc": round(float(aft.mean()) - base, 1) if len(aft) else None,
            "after_rms": round(float(aft.std()), 1) if len(aft) else None,
            "after_rel_db": round(20 * np.log10(max(float(aft.std()), 0.1) / max(nrms, 0.1)), 1)
            if len(aft) else None,
            "after_peak_hz": round(pk["f"], 1),
            "edge_max_step": float(np.abs(np.diff(edge)).max()) if len(edge) > 1 else None})
    if trig:                                    # the song end: the last tick (DMA off)
        te = sec(len(c.ticks) - 1) + 1 / 50
        aft = x[idx(te + 0.005):idx(te + 0.25)]
        if len(aft) > 64:
            out["song_end"] = {"t": round(te, 3), "dc": round(float(aft.mean()) - base, 1),
                               "rms": round(float(aft.std()), 1),
                               "peak_hz": round(peak(aft, rate)["f"], 1)}
    return out


def demo_clicks(c: RM.Case, res: dict, au: dict, phi2: int, s: int,
                law_c: float | None = None) -> dict:
    """The demo mixes channels, so per-note residuals are not separable:
    count sample-to-sample discontinuities (a step > 8x the local RMS of
    the steps) on each output side, and how many fall within 5 ms of a
    predicted one-shot end on the channels of that side, against what the
    same fraction of the time would catch by chance."""
    import numpy as np
    from host import paula_ref as PR
    law_c = PR.SAMPLE_C_LAW if law_c is None else law_c
    rate = au["rate"]
    recs = res["recs"]
    a = align(au, thr=200.0)
    t0 = RM.rec_time(recs, 0)
    sec = lambda i: ((RM.rec_time(recs, i) - t0) << s) / phi2
    out = {}
    for side, chans in (("l", (0, 3)), ("r", (1, 2))):
        x = au[side][a:a + int(sec(len(c.ticks) - 1) * rate)]
        d = np.abs(np.diff(x))
        w = int(0.01 * rate)
        loc = np.convolve(d, np.ones(w) / w, mode="same") + 1.0
        clicks = np.nonzero(d > 8 * loc)[0] / rate
        ends = []
        for ch in chans:
            for i, t in enumerate(c.ticks):
                if any(r == mod_ref.DMACON and not v & 0x8000 and v & (1 << ch)
                       for r, v in t.writes):
                    ln = next((v for r, v in t.writes if r == mod_ref.AUD[ch] + 4), 0)
                    per = next((v for r, v in t.writes if r == mod_ref.AUD[ch] + 6), 0)
                    rl = [v for r, v in t.writes if r == mod_ref.AUD[ch] + 4][-1:]
                    if ln and per and rl and rl[0] <= 1:          # one-shots only
                        ends.append(sec(i) + 2 * ln * PR.sample_ns(PR.sampler_rate(per), law_c) / 1e9)
        ends = np.array(sorted(ends))
        near = sum(1 for t in clicks if len(ends) and np.abs(ends - t).min() < 0.005)
        span = len(x) / rate
        chance = min(1.0, len(ends) * 0.01 / max(span, 1e-9))
        out[side] = {"clicks": int(len(clicks)), "near_oneshot_end": int(near),
                     "by_chance": round(chance * len(clicks), 1),
                     "first_clicks_s": [round(float(t), 3) for t in clicks[:12]]}
    return out


# --------------------------------------------------------------------------
# Songs and sessions
# --------------------------------------------------------------------------

BURST_LEN = 600


def burst(first: int) -> bytes:
    """A one-shot full-amplitude square burst (period 20 samples); its
    first word is *first* (0: the MOD convention's silent null loop)."""
    v = [first, first] + [100 if (i // 10) % 2 == 0 else -100 for i in range(BURST_LEN - 2)]
    return bytes(x & 0xFF for x in v)


PROBE_SINE = ["C-1", "G-1", "C-2", "E-2", "G-2", "C-3", "E-3", "B-3"]   # rows 0-28, inst 1
PROBE_BURST = ["C-2", "G-2", "C-3", "C-1"]                              # rows 32-44, inst 7
PROBE_NZ = ["C-2", "C-3"]                                               # rows 48-52, inst 8
NZ_FIRST = 60


def probe_song() -> bytes:
    """Channel 0 only (the left output; channel 3, also left, is silent).
    Rows 0-28: eight notes of the looped 32-sample sine (pitch; each
    change is a retrigger).  Rows 32-44: a one-shot square burst (600 B,
    first word 0): after its end Paula plays the 1-word null loop = silence.
    Rows 48-52: the same burst with a nonzero first word (60): after its end
    Paula holds that word (a DC level, by Paula's own semantics).  Row 63
    ends the song: DMA off."""
    rows = {4 * k: [f"{n} 01"] for k, n in enumerate(PROBE_SINE)}
    rows.update({32 + 4 * k: [f"{n} 07"] for k, n in enumerate(PROBE_BURST)})
    rows.update({48 + 4 * k: [f"{n} 08"] for k, n in enumerate(PROBE_NZ)})
    smp = modgen.std_samples() + [modgen.Sample("burst", burst(0), volume=64),
                                  modgen.Sample("burstnz", burst(NZ_FIRST), volume=64)]
    return modgen.Song("probe", [modgen.pattern(rows)], [0], samples=smp).build()


def cases(phi2: int, s: int, seconds: float, attended: bool, song: str) -> list[RM.Case]:
    tick_e = round(phi2 / 50) >> s
    if attended:
        raw = modgen.SONGS[song]().build()
        return [RM.Case(song, raw, RM.PF_REALTIME, tick_e=tick_e,
                        max_ticks=int(seconds * 50), timing=False, s=s)]
    return [RM.Case("probe", probe_song(), RM.PF_STOPEND | RM.PF_TRACE | RM.PF_REALTIME,
                    tick_e=tick_e, timing=False, s=s),
            RM.Case("demo", modgen.SONGS["demo"]().build(), RM.PF_TRACE | RM.PF_REALTIME,
                    tick_e=tick_e, max_ticks=int(seconds * 50), timing=False, s=s)]


def schedule(c: RM.Case, tick_s: float) -> list[str]:
    out, t, last = [], 0.0, None
    for k, tk in enumerate(c.ticks):
        pos, row = tk.view[29], tk.view[31]
        if (pos, row) != last and row % 16 == 0:
            out.append(f"  {t:6.1f} s  position {pos} pattern {tk.view[30]} row {row}")
        last = (pos, row)
        b = tk.bpm if (k + 1 < len(c.ticks) and c.ticks[k + 1].cia) else 125
        t += tick_s * 125 / b
    return out


def session(rig: Rig, out: str, a, fails: list[str], rep: dict) -> None:
    vstd, last, phi2 = rig.video()
    log(f"video: {vstd} (last raster line {last}), phi2 {phi2} Hz")
    rep["video"] = {"std": vstd, "last_line": last, "phi2": phi2}
    from host import paula_ref
    s = a.s if a.s is not None else (PLAY_S if not rig.vice else 6)
    if rig.vice and s:
        # the VICE dry run: Amiga-nominal guest time throughout (a tick = one
        # 14188-E frame, a colour clock = E / 5), s = 6 for speed
        phi_run, r16 = (14188 * 50) << s, paula_ref.R16_AMIGA
    else:
        # the player profile: s = 0, guest E = the real phi2
        phi_run, r16 = phi2, paula_ref.r16_for(phi2 >> s)
    hw = 0 if rig.vice else 1
    ui = "ui/ui.s"
    defs = [] if a.attended else ["MOD_TRACE"]
    img, syms = RM.build(defs, os.path.join(ROOT, "build", "player" + ("-tr" if defs else "")),
                         ui_src=ui)
    log(f"player built (UI_SRC={ui}, {' '.join(defs) or 'product'}), {len(img)} bytes; "
        f"s = {s}, tick_e = {round(phi_run / 50) >> s}, cfg_r16 = {r16}, cfg_hw = {hw}")
    rep.update(s=s, r16=r16, hw=hw)
    product = rep.get("info", {}).get("product")
    law_c = paula_ref.sample_c(product)
    if not rig.vice:
        rep["sampler_c_ns"] = law_c
        log(f"sampler law: (rate + 1) x 160 + {law_c:g} ns ({product!r}"
            f"{'' if product in paula_ref.SAMPLE_C_NS else ': unmeasured, the W10-H law'})")
    for c in cases(phi_run, s, a.seconds, a.attended, a.song):
        if a.only and c.name not in a.only.split(","):
            continue
        tick_s = 1 / 50
        if a.attended:
            print(f"LISTENING WINDOW: '{c.name}', {len(c.ticks) * tick_s:.0f} s, schedule:")
            for line in schedule(c, tick_s):
                print(line)
        log(f"{c.name}: {len(c.ticks)} ticks ({len(c.ticks) / 50:.1f} s at 50 ticks/s)")
        res, au = play(rig, c, img, syms, s, r16, hw, capture=not rig.vice and not a.no_audio,
                       out=out, deadline_s=60 + 3 * len(c.ticks) / 50 * (1 if s == 0 else 2 ** s),
                       pal=vstd == "PAL")
        f = list(res["fails"]) if not a.attended else []
        lr = {k: v for k, v in res.items() if k not in ("recs", "status", "fails")}
        if not a.attended:
            tr = tick_rate(c, res["recs"], phi_run, s)
            lr["tempo"] = tr
            log(f"{c.name}: tempo {tr['measured']:.3f} ticks/s vs {tr['expected']:.3f} "
                f"({tr['err'] * 100:+.3f}%), torn {tr.get('torn')}")
            if not rig.vice and abs(tr["err"]) > 0.01:
                f.append(f"{c.name}: tempo {tr['err'] * 100:+.2f}% (bound 1%)")
            if res["nconv"] <= 0:
                f.append(f"{c.name}: the UI never converted (disp_nconv unchanged)")
            if au is not None:
                lr["audio"] = {k: au[k] for k in ("rate", "rate_wall", "packets", "dropped",
                                                  "filled", "resyncs", "discarded", "intact", "wav")}
                log(f"{c.name}: audio {au['packets']} packets, dropped {au['dropped']}, filled "
                    f"{au['filled']}, resyncs {au['resyncs']}, discarded {au['discarded']}; rate "
                    f"{au['rate']:.2f} Hz (phi2 x 3/64), host-clock estimate {au['rate_wall']:.0f}")
                if not au["intact"]:
                    f.append(f"{c.name}: the audio time base is not intact (resync/discard)")
                if c.name == "probe":
                    ne = note_ends(c, res, au, phi_run, s, law_c=law_c)
                    lr["note_ends"] = ne
                    log(f"  note ends: floor rms {ne['floor_rms']}; song end {ne['song_end']}")
                    for e in ne["triggers"]:
                        log(f"    trigger t={e['t']} PER {e['per']}: gap {e['gap_ms']} ms, "
                            f"max step {e['max_step']:.0f}")
                    for e in ne["oneshot_ends"]:
                        log(f"    one-shot end t={e['t_end']} PER {e['per']}: after "
                            f"{e['after_s']} s: DC {e['after_dc']}, rms {e['after_rms']} "
                            f"({e['after_rel_db']} dB re the note's {e['note_rms']}), peak "
                            f"{e['after_peak_hz']} Hz, edge step {e['edge_max_step']}")
                if c.name == "demo":
                    dc = demo_clicks(c, res, au, phi_run, s, law_c=law_c)
                    lr["demo_clicks"] = dc
                    log(f"  demo discontinuities: {dc}")
                if c.name == "probe":
                    pf, rows = pitch_check(c, res, au, phi_run, s, law_c=law_c)
                    lr["pitch"] = rows
                    f += pf
                    for row in rows:
                        if "per" in row:
                            log(f"  pitch PER {row['per']}: {row['f']:.2f} Hz (sampler "
                                f"{row['f_sampler']:.2f}, Paula {row['f_paula']:.2f})")
                        else:
                            log(f"  stream rate cross-check: {row['rate_check']} Hz "
                                f"({row['note']})")
        log(f"{c.name}: wall {res['wall']:.1f} s, frames {res['frames']}, UI conversions "
            f"{res['nconv']}, deliveries {res['nirq']}, catch-up max {res['maxcu']}, stage "
            f"flushes {res['flush']}, level-6 entries {res['l6']}, torn {res['torn']}, "
            f"module {res['aud']}: {'ok' if not f else 'FAIL'}")
        for x in f[:8]:
            log("    " + x)
        lr["fails"] = f
        rep["plays"][c.name] = lr
        fails += f
    if a.product_probe and not a.attended:
        product_probe(rig, out, a, fails, rep, ui, phi_run, s, r16, hw, vstd == "PAL")


def product_probe(rig: Rig, out: str, a, fails: list[str], rep: dict, ui: str,
                  phi_run: int, s: int, r16: int, hw: int, pal: bool) -> None:
    """W15-RT: the probe from the PRODUCT build (no MOD_TRACE), the build
    that ships: no trace, so no content check; the halt, the UI and the
    audio stream only (the retrigger gaps: note_end_analysis.py offline,
    aligned on the nominal tick times)."""
    img, _ = RM.build([], os.path.join(ROOT, "build", "player"), ui_src=ui)
    tick_e = round(phi_run / 50) >> s
    c = RM.Case("probe_product", probe_song(), RM.PF_STOPEND | RM.PF_REALTIME,
                tick_e=tick_e, timing=False, s=s)
    log(f"probe_product: product build, {len(img)} bytes, {len(c.ticks)} ticks")
    res, au = play(rig, c, img, {}, s, r16, hw, capture=not rig.vice and not a.no_audio,
                   out=out, deadline_s=60 + 3 * len(c.ticks) / 50 * (1 if s == 0 else 2 ** s),
                   pal=pal)
    f = [x for x in res["fails"] if "mach_run returned" in x]   # no trace: content unchecked
    if res["nconv"] <= 0:
        f.append("probe_product: the UI never converted (disp_nconv unchanged)")
    lr = {k: v for k, v in res.items() if k not in ("recs", "status", "fails")}
    if au is not None:
        lr["audio"] = {k: au[k] for k in ("rate", "packets", "dropped", "filled", "resyncs",
                                          "discarded", "intact", "wav")}
        log(f"probe_product: audio {au['packets']} packets, dropped {au['dropped']}, "
            f"resyncs {au['resyncs']}, discarded {au['discarded']}")
        if not au["intact"]:
            f.append("probe_product: the audio time base is not intact (resync/discard)")
    log(f"probe_product: wall {res['wall']:.1f} s, frames {res['frames']}, UI conversions "
        f"{res['nconv']}, deliveries {res['nirq']}, module {res['aud']}: "
        f"{'ok' if not f else 'FAIL'}")
    lr["fails"] = f
    rep["plays"][c.name] = lr
    fails += f


def main(argv=None) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGALRM, term)     # an outer deadline (perl alarm) restores too
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--backend", choices=("vice", "u64"), default="vice")
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--s", type=int, help="mach_s (default: 0 on hardware, 6 on VICE)")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--attended", action="store_true")
    ap.add_argument("--song", default="demo")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--only", help="only these plays (probe, demo)")
    ap.add_argument("--product-probe", action="store_true",
                    help="also play the probe from the product build (W15-RT)")
    ap.add_argument("--out", default=os.path.join(EVID, "run"))
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    prg = os.path.abspath(a.prg)
    from c64_test_harness import Labels
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    rep: dict = {"plays": {}, "backend": a.backend}
    fails: list[str] = []
    if a.backend == "vice":
        from c64_test_harness import UnifiedManager, ViceConfig
        from tests.harness_util import REU_VICE_ARGS
        from tests.run_vectors import prepare_target
        cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=False,
                         extra_args=list(REU_VICE_ARGS))
        with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
            tg = mgr.acquire()
            try:
                prepare_target(tg, prg)
                session(Rig(tg, labels, prg), a.out, a, fails, rep)
            finally:
                mgr.release(tg)
        restored = True
    else:
        restored = hw_session(prg, labels, a, fails, rep)
    rep["fails"] = fails
    with open(os.path.join(a.out, "report.json"), "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    with open(os.path.join(a.out, "SESSION.log"), "a") as fh:
        fh.write("\n".join(LOG) + "\n")
    log(f"run_player {a.backend}{' attended' if a.attended else ''}: "
        f"{'PASS' if not fails else f'FAIL ({len(fails)})'}; config restored: {restored}")
    if not restored:
        return 3
    return 1 if fails else 0


def hw_session(prg: str, labels, a, fails: list[str], rep: dict) -> bool:
    """ONE DeviceLock hold (tests/bench/u64_session.u64_hold: config
    snapshot, exact restore): the CPU speed, a reboot, the sampler mapped
    (inside the hold), the REU checked, $DF21 read by the 6510; then the
    plays; every voice silenced before the restore."""
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench import u64_session as us
    from tests.run_paula import MAP_ITEM
    from tests.run_vectors import prepare_target
    us.log = log
    with us.u64_hold(a.out) as hold:
        tg = hold["target"]
        client = tg.transport.client
        rep["info"] = hold["info"]
        cats = {c: client.get_config_category(c).get(c, {}) for c in client.list_configs()}
        where = [c for c, items in cats.items() if MAP_ITEM in items]
        reu = [(i.get("RAM Expansion Unit"), i.get("REU Size")) for i in cats.values()
               if "RAM Expansion Unit" in i]
        log(f"found: {MAP_ITEM!r} in {where}, REU {reu} (prepare_target sets 16 MB)")
        if not where:
            fails.append(f"INFRA: no {MAP_ITEM!r} item on this device")
        else:
            tg.transport.set_speed(a.mhz)
            reboot(client)
            client.set_config_item(where[0], MAP_ITEM, "Enabled")
            time.sleep(1.0)
            prepare_target(tg, prg)             # sets the REU to 16 MB (set_reu)
            cats = {c: client.get_config_category(c).get(c, {}) for c in client.list_configs()}
            reu = [(i.get("RAM Expansion Unit"), i.get("REU Size")) for i in cats.values()
                   if "RAM Expansion Unit" in i]
            log(f"after prepare: REU {reu}")
            reu_ok = bool(reu) and reu[0][0] == "Enabled" and "16" in str(reu[0][1])
            ver = None
            if not reu_ok:
                fails.append(f"INFRA: the REU did not read back Enabled/16 MB: {reu}")
            else:
                rig = Rig(tg, labels, prg)
                rig.w(0xC100, bytes([0xAD, 0x21, 0xDF, 0x8D, 0xF0, 0xC1, 0x60]))
                rig.call(0xC100)
                ver = rig.r(0xC1F0, 1)[0]
                rep["df21"] = ver
                log(f"runner booted at {a.mhz} MHz, sampler item set; $DF21 = ${ver:02X}")
            if not reu_ok:
                pass
            elif ver != 0x10:
                fails.append(f"INFRA: $DF21 = ${ver:02X}: the sampler is not mapped")
            else:
                try:
                    session(rig, a.out, a, fails, rep)
                finally:
                    try:                        # every voice silent before the restore
                        rig.w(0xCA00, rig.a)
                        rig.w(0x0400, rig.b)
                        rig.call(0xCA00 + 10, timeout=30.0)
                        log("voices silenced (module init) before the restore")
                    except Exception as e:      # noqa: BLE001
                        log(f"silencing before the restore failed: {e!r}")
    rep["restored"] = hold.get("restored")
    return bool(hold.get("restored"))


if __name__ == "__main__":
    raise SystemExit(main())
