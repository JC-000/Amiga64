#!/usr/bin/env python3
"""W10-H: probe of the U64E built-in sampler ("Ultimate Audio", $DF20-$DFFF).

Builds sampler.s (a script interpreter, see its header), uploads it ONCE, and
drives it from the host: every access to the expansion port is made by the
6510.  Measures

  (1) the register window as the C64 sees it (disabled vs enabled, write-only
      behaviour, status/version reads),
  (2) playback timing from a sample staged in REU memory (start $1000000+),
  (3) the end-of-sample IRQ on the C64 IRQ line and how it is acknowledged,
  (4) coexistence with REU DMA,
  (5) the same at 8 and 48 MHz CPU speed,
  (6) optionally the audio output (UDP stream capture): which REU address the
      voice really plays from, and the loop.

All verdicts come from evaluate(meas, expect): a pure function over the saved
measurements, so --analyze re-judges a saved run offline and the red leg can
use a deliberately wrong expectation (it must exit 1).

  python3 sampler.py --backend u64 --out DIR            # hardware session
  python3 sampler.py --backend vice --out DIR           # interpreter check on
                                                        # VICE (no sampler: the
                                                        # sampler checks FAIL)
  python3 sampler.py --analyze DIR/meas_u64.json [--expect-version 0x11]

Hardware sessions run under tests.bench.u64_session.u64_hold (one DeviceLock
hold, full config snapshot, restore + diff on every exit path).
Exit 0 when every check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)

SCRIPT, RES, DATA = 0x1000, 0x1400, 0x2000
BASE = 0xDF20                       # voice 0 control
NVIS = 7                            # voices that fit in $DF20-$DFFF
REU_BASE = 0x1000000                # sampler memory address of REU byte 0
NTSC_HZ, PAL_HZ = 1022727.27, 985248.0
MAP_ITEM = "Map Ultimate Audio $DF20-DFFF"

OP_END, OP_WR, OP_RD, OP_TIM, OP_POLL, OP_TSTART, OP_DELAY, OP_IRQ, OP_MARK = range(9)

# voice registers (offsets within a 32-byte voice block), from sampler_pkg.vhd
R_CTRL, R_VOL, R_PAN = 0x00, 0x01, 0x02
R_START = 0x04                      # 4 bytes, big-endian, 26 bits used
R_LEN = 0x09                        # 3 bytes, big-endian
R_RATE_H, R_RATE_L = 0x0E, 0x0F
R_REPA, R_REPB = 0x11, 0x15         # 3 bytes each, big-endian
R_CLEAR = 0x1F                      # write: bit0 clears this voice; $FF all
C_EN, C_REP, C_IRQ, C_16 = 0x01, 0x02, 0x04, 0x10


def va(v: int, off: int) -> int:
    return BASE + 32 * v + off


# ----------------------------------------------------------------- scripts
class Script:
    """Builds a script and remembers how to decode its result bytes."""

    def __init__(self):
        self.b = bytearray()
        self.kinds = []

    def _e(self, op, addr=0, val=0):
        self.b += bytes([op, addr & 255, (addr >> 8) & 255, val & 255])

    def wr(self, addr, val):
        self._e(OP_WR, addr, val)

    def rd(self, addr):
        self._e(OP_RD, addr)
        self.kinds.append("rd")

    def tim(self):
        self._e(OP_TIM)
        self.kinds.append("tim")

    def poll(self, addr, mask):
        self._e(OP_POLL, addr, mask)
        self.kinds.append("poll")

    def tstart(self):
        self._e(OP_TSTART)

    def delay(self, n):
        self._e(OP_DELAY, 0, n)

    def irq(self, on):
        self._e(OP_IRQ, 0, 1 if on else 0)

    def mark(self, v):
        self._e(OP_MARK, 0, v)
        self.kinds.append("rd")

    def wr3(self, addr, v):
        for i in range(3):
            self.wr(addr + i, (v >> (16 - 8 * i)) & 255)

    def voice(self, v, start, length, rate, ctrl, vol=0x3F, pan=0x08,
              rep=None):
        """Program voice *v* (control last)."""
        self.wr(va(v, R_CTRL), 0)
        self.wr(va(v, R_VOL), vol)
        self.wr(va(v, R_PAN), pan)
        for i in range(4):
            self.wr(va(v, R_START + i), (start >> (24 - 8 * i)) & 255)
        self.wr3(va(v, R_LEN), length)
        self.wr(va(v, R_RATE_H), rate >> 8)
        self.wr(va(v, R_RATE_L), rate)
        if rep is not None:
            self.wr3(va(v, R_REPA), rep[0])
            self.wr3(va(v, R_REPB), rep[1])
        self.wr(va(v, R_CTRL), ctrl)

    def reu(self, c64, reu, n):
        for i, (a, v) in enumerate(((0xDF02, c64 & 255), (0xDF03, c64 >> 8),
                                    (0xDF04, reu & 255), (0xDF05, (reu >> 8) & 255),
                                    (0xDF06, reu >> 16), (0xDF07, n & 255),
                                    (0xDF08, n >> 8), (0xDF09, 0), (0xDF0A, 0))):
            self.wr(a, v)

    def end(self):
        self._e(OP_END)
        return bytes(self.b)


def decode(kinds, res: bytes, hz: float) -> list:
    out, i = [], 0
    for k in kinds:
        if k == "rd":
            out.append(res[i])
            i += 1
        elif k == "tim":
            raw = int.from_bytes(res[i:i + 4], "big")
            out.append((0xFFFFFFFF - raw) / hz * 1e6)       # us since TSTART
            i += 4
        else:
            raw = int.from_bytes(res[i + 2:i + 6], "big")
            out.append({"timeout": res[i], "raw": res[i + 1],
                        "us": (0xFFFFFFFF - raw) / hz * 1e6})
            i += 6
    assert i == len(res), (i, len(res), kinds)
    return out


# ------------------------------------------------------------- the target
class Rig:
    """The uploaded probe on one target; runs scripts and reads mailboxes."""

    def __init__(self, target, labels, log):
        from c64_test_harness import read_bytes, wait_for_memory, write_bytes
        self.rb, self.wfm, self.wb = read_bytes, wait_for_memory, write_bytes
        self.tr, self.L, self.log = target.transport, labels, log
        self.pal = False
        self.hz = NTSC_HZ

    def ready(self):
        if self.wfm(self.tr, self.L["ready"], 0xC3, timeout=30.0) is None:
            raise TimeoutError("probe never became ready")
        self.pal = self.rb(self.tr, 0x02A6, 1)[0] != 0
        self.hz = PAL_HZ if self.pal else NTSC_HZ

    def run(self, s: Script, timeout=30.0) -> list:
        blob = s.end()
        assert len(blob) < RES - SCRIPT, len(blob)
        self.wb(self.tr, SCRIPT, blob)
        self.wb(self.tr, self.L["done"], bytes([0]))
        self.wb(self.tr, self.L["go"], bytes([1]))
        if self.wfm(self.tr, self.L["done"], 0xA5, timeout=timeout) is None:
            raise TimeoutError("script never finished")
        end = int.from_bytes(self.rb(self.tr, self.L["res_end"], 2), "little")
        res = self.rb(self.tr, RES, end - RES) if end > RES else b""
        return decode(s.kinds, res, self.hz)

    def mail(self, name, n=1) -> bytes:
        return self.rb(self.tr, self.L[name], n)

    def poke(self, addr, data: bytes):
        self.wb(self.tr, addr, data)

    def peek(self, addr, n) -> bytes:
        return self.rb(self.tr, addr, n)


def read_block(rig, base=BASE, n=0x100 - 0x20) -> bytes:
    s = Script()
    for a in range(base, base + n):
        s.rd(a)
    return bytes(rig.run(s))


def play_once(rig, rate, length, start=REU_BASE, extra=None, v=0):
    """Play voice *v* once (IRQ on) and time it.  -> dict."""
    s = Script()
    s.wr(va(v, R_CLEAR), 0xFF)
    s.tstart()
    s.voice(v, start, length, rate, C_EN | C_IRQ)
    s.tim()                                   # when the control write was done
    s.poll(BASE, 1 << v)
    s.rd(BASE)
    s.wr(va(v, R_CLEAR), 1 << 0 if v == 0 else 0xFF)
    s.rd(BASE)
    s.wr(va(v, R_CTRL), 0)
    s.delay(2)
    t0, p, st_end, st_clr = rig.run(s)
    return {"rate": rate, "length": length, "t_arm_us": t0, "end_us": p["us"],
            "timeout": p["timeout"], "raw": p["raw"], "status_end": st_end,
            "status_after_clear": st_clr}


# ------------------------------------------------------------ measurements
def stage_reu(rig, log):
    """Stash two 4 KB square waves into REU 0x000000 / 0x001000 and read
    them back (REU sanity).  Pattern A: period 64, B: period 32 (+-64)."""
    def sq(period):
        return bytes([0x40 if (i % period) < period // 2 else 0xC0
                      for i in range(4096)])
    a, b = sq(64), sq(32)
    rig.poke(DATA, a)
    rig.poke(DATA + 0x1000, b)
    s = Script()
    s.reu(DATA, 0x000000, 4096)
    s.wr(0xDF01, 0x90)                         # stash
    s.reu(DATA + 0x1000, 0x001000, 4096)
    s.wr(0xDF01, 0x90)
    s.rd(0xDF00)
    rig.run(s)
    rig.poke(DATA, bytes(8192))
    s = Script()
    s.reu(DATA + 0x2000, 0x000000, 4096)
    s.wr(0xDF01, 0x91)                         # fetch
    s.reu(DATA + 0x3000, 0x001000, 4096)
    s.wr(0xDF01, 0x91)
    rig.run(s)
    ok = (rig.peek(DATA + 0x2000, 4096) == a and
          rig.peek(DATA + 0x3000, 4096) == b)
    log(f"REU stage + readback of two 4 KB patterns: {'OK' if ok else 'MISMATCH'}")
    return ok


def m_registers(rig, log) -> dict:
    out = {}
    blk = read_block(rig)
    out["block_idle"] = blk.hex()
    log(f"idle block: even={sorted(set(blk[0::2]))} odd={sorted(set(blk[1::2]))}")
    # write-only: scribble over voices 0 and 3 (never the control byte, so
    # nothing starts) and read the whole window again
    s = Script()
    for v in (0, 3):
        for off in range(1, 0x1F):
            s.wr(va(v, off), 0x55)
    rig.run(s)
    out["block_scribbled"] = read_block(rig).hex()
    s = Script()
    for v in (0, 3):
        s.voice(v, REU_BASE, 16, 1, 0)         # leave the voices harmless
    rig.run(s)
    return out


def m_timing(rig, log, rates, lengths) -> list:
    rows = []
    for rate in rates:
        for length in lengths:
            r = play_once(rig, rate, length)
            rows.append(r)
            log(f"play rate={rate:4d} len={length:5d}: end {r['end_us']:10.1f} us "
                f"(arm {r['t_arm_us']:.1f}) timeout={r['timeout']} "
                f"status end/after-clear {r['status_end']:02x}/{r['status_after_clear']:02x}")
    return rows


def irq_case(rig, name, ctrl_irq=True, ack_en=0, ack_val=1, ack_addr=None,
             limit=50, voice=0, rate=28, length=256, wait=40) -> dict:
    """One IRQ experiment on *voice*.  The handler counts entries."""
    ack_addr = va(voice, R_CLEAR) if ack_addr is None else ack_addr
    s = Script()
    L = rig.L
    s.wr(va(voice, R_CLEAR), 0xFF)
    s.wr(L["ack_en"], ack_en)
    s.wr(L["ack_val"], ack_val)
    s.wr(0xF6, ack_addr & 255)
    s.wr(0xF7, ack_addr >> 8)
    s.wr(L["irq_limit"], limit)
    s.tstart()
    s.voice(voice, REU_BASE, length, rate, C_EN | (C_IRQ if ctrl_irq else 0))
    s.irq(True)
    s.delay(wait)                              # the whole playback + IRQ(s)
    s.irq(False)
    s.rd(BASE)                                 # status, I set, line state
    s.irq(True)
    s.delay(4)                                 # does a masked level IRQ return?
    s.irq(False)
    s.wr(va(voice, R_CLEAR), 0xFF)
    s.rd(BASE)
    s.wr(va(voice, R_CTRL), 0)
    s.delay(2)
    st, st_clr = rig.run(s)
    cnt = int.from_bytes(rig.mail("irq_count", 2), "little")
    r = {"name": name, "count": cnt, "spur": rig.mail("irq_spur")[0],
         "st_first": rig.mail("irq_stfirst")[0], "st_last": rig.mail("irq_stlast")[0],
         "status_after": st, "status_after_final_clear": st_clr, "limit": limit}
    rig.log(f"irq {name}: count={cnt} spur={r['spur']} first status={r['st_first']:02x} "
        f"status after={st:02x} after clear={st_clr:02x}")
    return r


def m_irq(rig, log) -> dict:
    out = {}
    out["noack"] = irq_case(rig, "no ack (level)", ack_en=0)
    out["ack01"] = irq_case(rig, "ack: write 1 to own clear reg", ack_en=1, ack_val=1)
    out["ackff"] = irq_case(rig, "ack: write $FF", ack_en=1, ack_val=0xFF)
    out["ack00"] = irq_case(rig, "ack: write 0 (bit 0 clear)", ack_en=1, ack_val=0)
    out["ackother"] = irq_case(rig, "ack: voice 1's clear reg with 1", ack_en=1,
                               ack_val=1, ack_addr=va(1, R_CLEAR))
    out["ackstatrd"] = irq_case(rig, "ack: only reads status (in handler)", ack_en=0)
    out["irqoff"] = irq_case(rig, "ctrl IRQ bit off", ctrl_irq=False, ack_en=0)
    out["v3"] = irq_case(rig, "voice 3, ack own", ack_en=1, ack_val=1, voice=3)
    out["v6"] = irq_case(rig, "voice 6 (last visible), ack own", ack_en=1,
                         ack_val=1, voice=6)
    return out


def m_repeat(rig, log) -> dict:
    """Does the repeat loop fire the end IRQ?  (a) repeat B inside the
    sample: loops for ever, no status; (b) repeat B beyond the length: the
    sample ends normally."""
    out = {}
    for tag, rb in (("loop", 511), ("b_beyond_length", 0xFFFFF0)):
        s = Script()
        s.wr(va(0, R_CLEAR), 0xFF)
        s.voice(0, REU_BASE, 1024, 28, C_EN | C_REP | C_IRQ, rep=(0, rb))
        s.delay(60)                                 # ~78 ms = 17 sample times
        s.rd(BASE)
        s.wr(va(0, R_CTRL), 0)                      # stop (enable off)
        s.delay(2)
        s.rd(BASE)
        s.wr(va(0, R_CLEAR), 0xFF)
        s.rd(BASE)
        st, st_stop, st_clr = rig.run(s)
        out[tag] = {"status_running": st, "status_after_stop": st_stop,
                    "status_after_clear": st_clr}
        log(f"repeat {tag}: status while running {st:02x}, after stop {st_stop:02x}, "
            f"after clear {st_clr:02x}")
    return out


def m_latency(rig, log, n=10) -> list:
    """Status-poll vs IRQ-handler time of the same kind of end event, each
    taken from its own arm stamp (the control write, ~+-40 us stamp jitter)."""
    rows = []
    L = rig.L
    for i in range(n):
        # (a) polled: interrupts off, poll the status bit
        s = Script()
        s.wr(va(0, R_CLEAR), 0xFF)
        s.wr(L["ack_en"], 0)
        s.tstart()
        s.voice(0, REU_BASE, 1024, 28, C_EN | C_IRQ)
        s.tim()
        s.poll(BASE, 1)
        s.wr(va(0, R_CLEAR), 0xFF)
        s.wr(va(0, R_CTRL), 0)
        s.delay(2)
        arm_p, p = rig.run(s)
        # (b) IRQ: handler stamps the timer on its first entry, acks
        s = Script()
        s.wr(va(0, R_CLEAR), 0xFF)
        s.wr(L["ack_en"], 1)
        s.wr(L["ack_val"], 1)
        s.wr(0xF6, va(0, R_CLEAR) & 255)
        s.wr(0xF7, va(0, R_CLEAR) >> 8)
        s.wr(L["irq_limit"], 20)
        s.tstart()
        s.voice(0, REU_BASE, 1024, 28, C_EN | C_IRQ)
        s.tim()
        s.irq(True)
        s.poll(L["irq_count"], 0xFF)
        s.irq(False)
        s.wr(va(0, R_CTRL), 0)
        s.delay(2)
        arm_q, q = rig.run(s)
        raw = int.from_bytes(rig.mail("irq_first", 4), "big")
        t_irq = (0xFFFFFFFF - raw) / rig.hz * 1e6
        rows.append({"poll_us": p["us"] - arm_p, "irq_us": t_irq - arm_q,
                     "poll_timeout": p["timeout"], "irq_timeout": q["timeout"],
                     "irq_count": int.from_bytes(rig.mail("irq_count", 2), "little")})
        log(f"latency {i}: arm->status-poll hit {rows[-1]['poll_us']:.1f} us, arm->IRQ "
            f"handler stamp {rows[-1]['irq_us']:.1f} us "
            f"(delta {rows[-1]['irq_us'] - rows[-1]['poll_us']:+.1f} us)")
    return rows


def m_reu(rig, log) -> dict:
    """REU DMA while a voice plays (and IRQ delivery across DMA)."""
    out = {}
    rate, length = 126, 4096                   # ~83 ms
    base = play_once(rig, rate, length)
    out["play_alone"] = base
    # DMA alone: 8 fetches of 1 KB
    s = Script()
    s.reu(DATA + 0x2000, 0x002000, 1024)
    for _ in range(8):
        s.tim()
        s.wr(0xDF01, 0xB1)
        s.tim()
    t = rig.run(s)
    dma_alone = [t[2 * i + 1] - t[2 * i] for i in range(8)]
    out["dma_alone_us"] = dma_alone
    # stash a known block, then DMA during playback, then verify the block
    blk = bytes((i * 7 + 3) & 255 for i in range(1024))
    rig.poke(DATA + 0x2000, blk)
    s = Script()
    s.reu(DATA + 0x2000, 0x002000, 1024)
    s.wr(0xDF01, 0xB0)                         # stash (autoload keeps regs)
    rig.run(s)
    rig.poke(DATA + 0x2400, bytes(1024))
    s = Script()
    s.wr(va(0, R_CLEAR), 0xFF)
    s.reu(DATA + 0x2400, 0x002000, 1024)       # fetch into a clean buffer
    s.tstart()
    s.voice(0, REU_BASE, length, rate, C_EN | C_IRQ)
    for _ in range(40):
        s.tim()
        s.wr(0xDF01, 0xB1)                     # fetch 1 KB
        s.tim()
    s.poll(BASE, 1)
    s.rd(BASE)
    s.wr(va(0, R_CLEAR), 1)
    s.wr(va(0, R_CTRL), 0)
    s.delay(2)
    r = rig.run(s)
    tims, p = r[:80], r[80]
    dma_busy = [tims[2 * i + 1] - tims[2 * i] for i in range(40)]
    ok = rig.peek(DATA + 0x2400, 1024) == blk
    out.update({"dma_during_us": dma_busy, "play_end_us": p["us"],
                "play_timeout": p["timeout"], "fetched_ok": ok,
                "dma_ended_before_play": tims[-1] < p["us"] and tims[-1] < base["end_us"]})
    log(f"REU: DMA alone mean {sum(dma_alone)/8:.1f} us; during play mean "
        f"{sum(dma_busy)/40:.1f} us (max {max(dma_busy):.1f}); play alone "
        f"{base['end_us']:.0f} us, with DMA {p['us']:.0f} us; data ok={ok}; "
        f"last DMA ended at {tims[-1]:.0f} us")
    # IRQ delivered across a DMA burst (handler acks)
    L = rig.L
    s = Script()
    s.wr(va(0, R_CLEAR), 0xFF)
    s.wr(L["ack_en"], 1)
    s.wr(L["ack_val"], 1)
    s.wr(0xF6, va(0, R_CLEAR) & 255)
    s.wr(0xF7, va(0, R_CLEAR) >> 8)
    s.wr(L["irq_limit"], 20)
    s.reu(DATA + 0x2400, 0x002000, 1024)
    s.voice(0, REU_BASE, 512, 28, C_EN | C_IRQ)
    s.irq(True)
    for _ in range(30):
        s.wr(0xDF01, 0xB1)
    s.delay(10)
    s.irq(False)
    s.rd(BASE)
    s.wr(va(0, R_CTRL), 0)
    s.delay(2)
    (st,) = rig.run(s)
    out["irq_across_dma"] = {"count": int.from_bytes(rig.mail("irq_count", 2), "little"),
                             "status_after": st}
    log(f"IRQ across DMA burst: count={out['irq_across_dma']['count']} status after {st:02x}")
    return out


def m_speed(rig, log, mhz, set_speed) -> dict:
    set_speed(mhz)
    blk = read_block(rig)
    ok_pat = blk[0::2] == bytes(len(blk[0::2])) and blk[1::2] == bytes([0x10]) * len(blk[1::2])
    nbad = sum(1 for i, x in enumerate(blk) if x != (0 if i % 2 == 0 else 0x10))
    pl = [play_once(rig, 126, 4096) for _ in range(3)]
    r = {"mhz": mhz, "block": blk.hex(), "block_clean": ok_pat, "bad_bytes": nbad,
         "play": pl}
    log(f"{mhz} MHz: block clean={ok_pat} (bad bytes {nbad}); play rate 126 len 4096 -> "
        + ", ".join(f"{p['end_us'] - p['t_arm_us']:.0f}us" for p in pl))
    return r


def local_ip(host) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((host, 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


def audio_peak(wav_samples, rate_hz):
    import numpy as np
    x = np.asarray(wav_samples, dtype=np.float64)
    x = x - x.mean()
    rms = float(np.sqrt((x ** 2).mean())) if len(x) else 0.0
    if rms == 0.0:
        return {"rms": 0.0, "f_hz": 0.0}
    w = np.hanning(len(x))
    sp = np.abs(np.fft.rfft(x * w))
    k = int(sp.argmax())
    # parabolic refine
    if 0 < k < len(sp) - 1:
        a, b, c = np.log(sp[k - 1] + 1e-9), np.log(sp[k] + 1e-9), np.log(sp[k + 1] + 1e-9)
        k = k + 0.5 * (a - c) / (a - 2 * b + c)
    return {"rms": rms, "f_hz": float(k * rate_hz / len(x))}


def m_audio(rig, log, client, host, out_dir, rate=283) -> dict:
    """Loop a REU pattern forever and capture the UDP audio stream: shows
    that the voice plays what is at its REU start address (pattern A, period
    64 samples, at REU 0; pattern B, period 32, at REU 0x1000) and that the
    repeat loop runs.  Frequency = 1 / (period * sample time)."""
    from c64_test_harness.backends.u64_audio_capture import (
        AudioCapture, U64_NTSC_AUDIO_RATE_HZ)
    ip = local_ip(host)
    out = {"ip": ip, "rate": rate, "captures": {}}

    def one(label, script_start, script_stop):
        cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
        cap.start()
        try:
            client.stream_audio_start(f"{ip}:{cap.port}")
            time.sleep(0.5)
            rig.run(script_start)
            time.sleep(1.2)
            rig.run(script_stop)
            time.sleep(0.4)
        finally:
            try:
                client.stream_audio_stop()
            except Exception as e:                     # noqa: BLE001
                log(f"stream_audio_stop failed: {e!r}")
            wav = os.path.join(out_dir, f"audio_{label}.wav")
            res = cap.stop(wav_path=wav)
        import wave
        import numpy as np
        with wave.open(wav, "rb") as w:
            fr = w.getframerate()
            n = w.getnframes()
            raw = np.frombuffer(w.readframes(n), dtype="<i2").reshape(-1, w.getnchannels())
        mono = raw.astype(np.float64).mean(axis=1)
        mid = raw[int(len(raw) * 0.35):int(len(raw) * 0.65)].astype(np.float64)
        ch = [mid[:, k].std() for k in range(raw.shape[1])]
        mean = [mid[:, k].mean() for k in range(raw.shape[1])]
        # analyse the middle of the capture (voice running)
        a, b = int(len(mono) * 0.35), int(len(mono) * 0.65)
        pk = audio_peak(mono[a:b], float(U64_NTSC_AUDIO_RATE_HZ))
        pk.update({"mean_l": float(mean[0]), "mean_r": float(mean[1]), "rms_l": float(ch[0]), "rms_r": float(ch[1]), "packets": res.packets_received, "dropped": res.packets_dropped,
                   "time_base_intact": bool(res.time_base_intact),
                   "frames": int(n), "wav": os.path.basename(wav),
                   "min": int(raw.min()), "max": int(raw.max())})
        out["captures"][label] = pk
        log(f"audio {label}: rms {pk['rms']:.1f} (L {pk['rms_l']:.0f} R {pk['rms_r']:.0f}; mean L {pk['mean_l']:.0f} R {pk['mean_r']:.0f}) f {pk['f_hz']:.1f} Hz "
            f"(min {pk['min']} max {pk['max']}, packets {pk['packets']} dropped {pk['dropped']})")

    def loop_script(start_reu, vol=0x3F, pan=0x08):
        s = Script()
        # repeat A/B are positions relative to the start address
        s.voice(0, REU_BASE + start_reu, 0xFFFFFF, rate, C_EN | C_REP, vol=vol,
                pan=pan, rep=(0, 4095))
        return s

    def stop_script():
        s = Script()
        s.wr(va(0, R_CTRL), 0)
        s.delay(2)
        return s

    # REU 0x10000: 256 zero bytes, 0x10100: 64 bytes of $20 (a DC level)
    rig.poke(DATA, bytes(256) + bytes([0x20]) * 64)
    s = Script()
    s.reu(DATA, 0x010000, 320)
    s.wr(0xDF01, 0xB0)
    rig.run(s)

    def quiet():
        """Play 16 zero samples on every visible voice: a voice that has
        finished holds its last sample as a DC level until it is replayed."""
        s = Script()
        for v in range(NVIS):
            s.voice(v, REU_BASE + 0x10000, 16, 3, C_EN)
        s.delay(3)
        for v in range(NVIS):
            s.wr(va(v, R_CTRL), 0)
        s.delay(2)
        return s

    def dc():
        s = quiet()
        s.voice(0, REU_BASE + 0x10100, 16, 3, C_EN)
        s.delay(3)
        s.wr(va(0, R_CTRL), 0)
        s.delay(2)
        return s

    one("silence", quiet(), Script())
    one("dc_hold", dc(), Script())
    one("dc_zeroed", quiet(), Script())
    rig.run(quiet())
    one("patA", loop_script(0), stop_script())
    one("patB", loop_script(0x1000), stop_script())
    one("patA_vol8", loop_script(0, vol=8), stop_script())
    one("patA_pan0", loop_script(0, pan=0), stop_script())
    one("patA_pan15", loop_script(0, pan=15), stop_script())
    return out


# ------------------------------------------------------------- evaluation
def fit_tick(rows):
    """Per rate: least-squares line of play time (end - arm, us) vs length.
    Then: slope vs (rate + 1).  -> (ns per rate tick, ns constant per
    sample, {rate: (ns per sample, intercept us)}, worst residual us)."""
    by = {}
    for r in rows:
        by.setdefault(r["rate"], []).append((r["length"], r["end_us"] - r["t_arm_us"]))
    per, pts, worst = {}, [], 0.0
    for rate, v in by.items():
        n = len(v)
        mx = sum(x for x, _ in v) / n
        my = sum(y for _, y in v) / n
        sl = sum((x - mx) * (y - my) for x, y in v) / sum((x - mx) ** 2 for x, _ in v)
        ic = my - sl * mx
        worst = max([worst] + [abs(y - (ic + sl * x)) for x, y in v])
        per[rate] = (sl * 1000.0, ic)
        pts.append((rate + 1, sl * 1000.0))
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    a = sum((p[0] - mx) * (p[1] - my) for p in pts) / sum((p[0] - mx) ** 2 for p in pts)
    return a, my - a * mx, per, worst


def evaluate(m: dict, expect: dict) -> list:
    """-> [(name, ok, detail)]"""
    ck = []

    def add(name, ok, detail=""):
        ck.append((name, bool(ok), detail))

    ev = expect["version"]
    dis = bytes.fromhex(m["registers"]["disabled_block"])
    idle = bytes.fromhex(m["registers"]["block_idle"])
    scr = bytes.fromhex(m["registers"]["block_scribbled"])
    add("disabled: window does not read as the sampler",
        dis[1] != ev, f"$DF21 reads ${dis[1]:02x} while the map item is Disabled "
        f"(distinct bytes in $DF20-$DFFF: {sorted(set(dis))})")
    add(f"enabled: every odd address in $DF20-$DFFF reads ${ev:02x}",
        set(idle[1::2]) == {ev}, f"odd bytes {sorted(set(idle[1::2]))}")
    add("enabled: every even address reads the IRQ status = 0 when idle",
        set(idle[0::2]) == {0}, f"even bytes {sorted(set(idle[0::2]))}")
    add("registers are write-only (scribbling voices 0 and 3 changes no read)",
        scr == idle, "")
    if "timing" not in m:
        return ck
    tm = m["timing"]
    add("play: every run reached its end status (no timeout)",
        all(not r["timeout"] for r in tm), "")
    add("play: status bit 0 set at the end and cleared by the clear register",
        all(r["status_end"] & 1 and not r["status_after_clear"] & 1 for r in tm), "")
    a, b, per, worst = fit_tick(tm)
    want = expect["tick_ns"]
    add(f"play: slope per rate tick = {want:.0f} ns +-{expect['tick_tol']*100:.0f}%",
        abs(a - want) <= expect["tick_tol"] * want,
        f"fit {a:.1f} ns per (rate+1), constant {b:.0f} ns per sample")
    cw = expect["const_ns"]
    add(f"play: constant per sample = {cw:.0f} ns +-{expect['const_tol']:.0f} ns "
        "(independent of the fit: one 8-clock voice slot at 66.67 MHz, sampler2.vhd fetch1 state)",
        abs(b - cw) <= expect["const_tol"], f"fit constant {b:.1f} ns")
    add("play: time is linear in length at each rate (worst residual < 60 us; "
        "the stamps carry ~+-40 us of interpreter/badline jitter)", worst < 60.0,
        f"worst residual {worst:.1f} us")
    add("play: per-sample time = (rate + 1) * tick + constant, every rate within 1% + 10 ns",
        all(abs(per[r][0] - (a * (r + 1) + b)) <= 0.01 * per[r][0] + 10 for r in per),
        ", ".join(f"rate {r}: {per[r][0]:.0f} ns" for r in sorted(per)))
    # IRQ
    q = m["irq"]
    lim = q["noack"]["limit"]
    n_lvl = lim + 1                    # the limit, plus one re-entry after SEI/CLI
    add("irq: end-of-sample IRQ reaches the C64 and is a LEVEL (no ack: re-enters "
        f"up to the limit {lim}, and once more after a SEI/CLI)", q["noack"]["count"] == n_lvl,
        f"count {q['noack']['count']}; status stays ${q['noack']['status_after']:02x}")
    add("irq: reading the status does not acknowledge it",
        q["ackstatrd"]["count"] == n_lvl and q["ackstatrd"]["status_after"] & 1, "")
    add("irq: writing 1 to the voice's clear register acknowledges (count 1)",
        q["ack01"]["count"] == 1 and not q["ack01"]["status_after"] & 1,
        f"count {q['ack01']['count']}")
    add("irq: writing $FF to a clear register acknowledges", q["ackff"]["count"] == 1,
        f"count {q['ackff']['count']}")
    add("irq: writing 0 (bit 0 clear) does not acknowledge",
        q["ack00"]["count"] == n_lvl, f"count {q['ack00']['count']}")
    add("irq: another voice's clear register does not acknowledge",
        q["ackother"]["count"] == n_lvl, f"count {q['ackother']['count']}")
    add("irq: ctrl bit 2 off -> no status bit, no IRQ",
        q["irqoff"]["count"] == 0 and q["irqoff"]["status_after"] == 0, "")
    add("irq: voice 3 -> status bit 3, acked", q["v3"]["count"] == 1 and q["v3"]["st_first"] == 8,
        f"first status ${q['v3']['st_first']:02x}")
    add("irq: voice 6 (last visible) -> status bit 6, acked",
        q["v6"]["count"] == 1 and q["v6"]["st_first"] == 0x40,
        f"first status ${q['v6']['st_first']:02x}")
    add("irq: no spurious entry (handler always saw a status bit)",
        all(v["spur"] == 0 for v in q.values()), "")
    rp = m["repeat"]
    add("repeat: with repeat B inside the sample the voice loops for ever "
        "(no end status, no IRQ in 17 sample times)",
        rp["loop"]["status_running"] == 0 and rp["loop"]["status_after_stop"] == 0, str(rp["loop"]))
    add("repeat: with repeat B beyond the length the sample ends and raises its status",
        rp["b_beyond_length"]["status_running"] & 1 and not rp["b_beyond_length"]["status_after_clear"] & 1,
        str(rp["b_beyond_length"]))
    lat = m["latency"]
    d = [r["irq_us"] - r["poll_us"] for r in lat]
    md = sum(d) / len(d)
    add("irq: every run delivered exactly one IRQ and the mean IRQ-handler stamp is within "
        "100 us of the status-poll stamp (n=%d)" % len(d),
        all(not r["poll_timeout"] and not r["irq_timeout"] and r["irq_count"] == 1 for r in lat)
        and abs(md) < 100,
        f"IRQ-poll delta mean {md:+.1f} us, min {min(d):+.1f}, max {max(d):+.1f}")
    # REU
    r = m["reu"]
    add("reu: DMA fetch/stash data intact while a voice plays", r["fetched_ok"], "")
    add("reu: DMA ran inside the playback window",
        r["dma_ended_before_play"], "")
    alone = sum(r["dma_alone_us"]) / len(r["dma_alone_us"])
    busy = sum(r["dma_during_us"]) / len(r["dma_during_us"])
    spread = (max(r["dma_alone_us"]) - min(r["dma_alone_us"])) / alone
    add(f"reu: DMA time with a voice playing within {expect['dma_tol']*100:.0f}% of DMA alone "
        "(the tolerance sits just above the alone-run spread)",
        abs(busy - alone) <= expect["dma_tol"] * alone and spread <= expect["dma_tol"],
        f"alone {alone:.1f} us (n=8 spread {spread*100:.1f}%), with sampler {busy:.1f} us "
        f"(delta {(busy - alone) / alone * 100:+.1f}%, inside the noise)")
    # durations = end stamp minus the arm stamp (both are stamps from TSTART)
    pa = r["play_alone"]["end_us"] - r["play_alone"]["t_arm_us"]
    pw = r["play_end_us"] - r["play_alone"]["t_arm_us"]   # same arm stamp class (~1.2 ms)
    add(f"reu: playback time with DMA within {expect['play_tol']*100:.1f}% of alone "
        "(noise: arm-stamp jitter ~+-40 us = 0.05%)",
        not r["play_timeout"] and abs(pw - pa) <= expect["play_tol"] * pa,
        f"alone {pa:.0f} us, with DMA {pw:.0f} us (delta {pw - pa:+.0f} us)")
    add("reu: IRQ delivered across a DMA burst (count 1)",
        r["irq_across_dma"]["count"] == 1, f"count {r['irq_across_dma']['count']}")
    # speed
    for k, sp in m.get("speed", {}).items():
        add(f"{k} MHz: window reads clean ($10/0 pattern)", sp["block_clean"],
            f"{sp['bad_bytes']} bad bytes of 224")
        ref = sum(p["end_us"] - p["t_arm_us"] for p in m["speed"]["1"]["play"]) / 3
        got = sum(p["end_us"] - p["t_arm_us"] for p in sp["play"]) / 3
        add(f"{k} MHz: playback time within 1% of 1 MHz (register writes land)",
            abs(got - ref) <= 0.01 * ref, f"{got:.0f} us vs {ref:.0f} us")
    # audio
    au = m.get("audio")
    if au:
        c = au["captures"]
        sil, pa, pb, pv = c["silence"], c["patA"], c["patB"], c["patA_vol8"]
        tsamp = (a * (au["rate"] + 1) + b) * 1e-9
        fa, fb = 1 / (64 * tsamp), 1 / (32 * tsamp)
        # the stream carries a constant offset with every voice idle (the mixer's
        # other sources); levels are judged relative to the silence capture
        add("audio: silent (no AC) once every voice has played zeros",
            sil["rms"] < 50, f"rms {sil['rms']:.1f}, idle offset mean L/R "
            f"{sil['mean_l']:.0f}/{sil['mean_r']:.0f}")
        dh, dz = c["dc_hold"], c["dc_zeroed"]
        step = dh["mean_l"] - sil["mean_l"]
        add("audio: a stopped voice holds its last sample as a DC level (until replayed)",
            dh["rms"] < 50 and abs(step) > 500 and abs(dz["mean_l"] - sil["mean_l"]) < 300,
            f"held $20: mean L {dh['mean_l']:.0f} vs idle {sil['mean_l']:.0f} (step {step:+.0f} "
            f"= {step / 32:.1f} per sample LSB); after zeros {dz['mean_l']:.0f}")
        add("audio: pattern A (REU 0) heard at the predicted frequency",
            abs(pa["f_hz"] - fa) <= 0.03 * fa and pa["rms"] > 500,
            f"{pa['f_hz']:.1f} Hz vs {fa:.1f} Hz, rms {pa['rms']:.0f}")
        add("audio: pattern B (REU 0x1000) heard at 2x: the start address selects the data",
            abs(pb["f_hz"] - fb) <= 0.03 * fb and pb["rms"] > 500,
            f"{pb['f_hz']:.1f} Hz vs {fb:.1f} Hz, rms {pb['rms']:.0f}")
        add("audio: volume 8/63 scales the level to ~8/63 (0.08..0.2 of volume 63)",
            0.08 * pa["rms"] < pv["rms"] < 0.2 * pa["rms"],
            f"rms {pv['rms']:.0f} vs {pa['rms']:.0f} (ratio {pv['rms'] / max(pa['rms'], 1):.3f})")
        p0, p15 = c["patA_pan0"], c["patA_pan15"]
        add("audio: pan 0 favours left, pan 15 favours right (channel rms; not hard-panned)",
            p0["rms_l"] > p0["rms_r"] > 0 and p15["rms_r"] > p15["rms_l"] > 0,
            f"pan0 L/R {p0['rms_l']:.0f}/{p0['rms_r']:.0f}; pan15 L/R {p15['rms_l']:.0f}/{p15['rms_r']:.0f}")
    return ck


def judge(m, expect, log) -> bool:
    ck = evaluate(m, expect)
    ok = True
    for name, good, detail in ck:
        ok &= good
        log(f"  [{'PASS' if good else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))
    log(f"{sum(g for _, g, _ in ck)}/{len(ck)} checks passed")
    return ok


# ------------------------------------------------------------------ driver
def build(out: str):
    os.makedirs(out, exist_ok=True)
    prg, lbl = os.path.join(out, "sampler.prg"), os.path.join(out, "sampler.lbl")
    subprocess.run(["cl65", "-t", "none", "-C", "sampler.cfg", "-o", prg, "-Ln", lbl,
                    "sampler.s"], check=True, cwd=HERE)
    labels = {}
    for line in open(lbl):
        _, a, n = line.split()
        labels[n.lstrip(".")] = int(a, 16)
    return prg, labels


_MEAS = {}


def u64_run(a, log, prg, labels):
    from c64_test_harness import run_prg_via_sys
    import tests.bench.u64_session as us
    us.log = log
    meas = _MEAS
    with us.u64_hold(a.out) as hold:
        t = hold["target"]
        client = t.transport.client
        info = hold["info"]
        meas["info"] = info
        log(f"GET /v1/info: {json.dumps(info, sort_keys=True)}")
        cats = {c: client.get_config_category(c).get(c, {}) for c in client.list_configs()}
        where = [c for c, items in cats.items() if MAP_ITEM in items]
        log(f"config item {MAP_ITEM!r} found in categories {where}: "
            f"{[cats[c][MAP_ITEM] for c in where]}")
        meas["map_item_categories"] = where
        cat = where[0]

        def upload():
            t.transport.set_speed(1)
            log("uploading probe via run_prg_via_sys at 1 MHz")
            run_prg_via_sys(t, open(prg, "rb").read())

        def set_speed(m):
            t.transport.set_speed(m)
            time.sleep(0.5)
            log(f"CPU speed set to {m} MHz")

        reu = [(i["RAM Expansion Unit"], i.get("REU Size")) for i in cats.values()
               if "RAM Expansion Unit" in i][0]
        log(f"REU found: {reu}")
        meas["reu_config"] = reu
        if reu[0] != "Enabled":
            from c64_test_harness.backends.ultimate64_helpers import set_reu
            set_reu(client, True, 512 * 1024)
            log("REU enabled (restored with the rest of the config)")
        rig = Rig(t, labels, log)
        client.set_config_item(cat, MAP_ITEM, "Disabled")
        client.reset()
        time.sleep(3.0)
        upload()
        rig.ready()
        log(f"video standard: {'PAL' if rig.pal else 'NTSC'}")
        reg = {}
        reg["disabled_block"] = read_block(rig).hex()
        log(f"disabled block: distinct {sorted(set(bytes.fromhex(reg['disabled_block'])))}")
        log(f"setting {MAP_ITEM} = Enabled")
        client.set_config_item(cat, MAP_ITEM, "Enabled")
        time.sleep(1.0)
        probe = read_block(rig, 0xDF20, 2)
        log(f"$DF20/$DF21 after enabling (no reset): {probe.hex()}")
        meas["enable_needs_reset"] = probe[1] != 0x10
        if probe[1] != 0x10:
            log("not mapped yet: C64 reset + re-upload (once)")
            client.reset()
            time.sleep(3.0)
            upload()
            rig.ready()
            probe = read_block(rig, 0xDF20, 2)
            log(f"$DF20/$DF21 after reset: {probe.hex()}")
        m = {"enable_needs_reset": meas["enable_needs_reset"], "pal": rig.pal}
        meas.update(m)
        meas["reu_stage_ok"] = stage_reu(rig, log)
        reg.update(m_registers(rig, log))
        meas["registers"] = reg
        meas["timing"] = m_timing(rig, log, [0, 1, 3, 28, 63, 126, 283], [1024, 4096, 16384])
        meas["irq"] = m_irq(rig, log)
        meas["repeat"] = m_repeat(rig, log)
        meas["latency"] = m_latency(rig, log)
        meas["reu"] = m_reu(rig, log)
        meas["speed"] = {}
        meas["speed"]["1"] = m_speed(rig, log, 1, set_speed)
        for mhz in (8, 48):
            meas["speed"][str(mhz)] = m_speed(rig, log, mhz, set_speed)
        set_speed(1)
        if not a.no_audio:
            try:
                meas["audio"] = m_audio(rig, log, client, a.host, a.out)
            except Exception as e:                       # noqa: BLE001
                log(f"AUDIO PHASE FAILED: {e!r}")
                meas["audio_error"] = repr(e)
        # leave the sampler quiet
        s = Script()
        for v in range(NVIS):
            s.wr(va(v, R_CTRL), 0)
        s.wr(va(0, R_CLEAR), 0xFF)
        rig.run(s)
    log(f"config restored={hold.get('restored')} reset_ok={hold.get('reset_ok')}")
    meas["restored"] = hold.get("restored")
    meas["reset_ok"] = hold.get("reset_ok")
    return meas


def vice_run(a, log, prg, labels):
    """Interpreter self-test on plain RAM and the CIA timer, plus the window
    as VICE has it (no sampler there: the sampler checks must FAIL)."""
    from c64_test_harness import UnifiedManager, ViceConfig
    cfg = ViceConfig(prg_path=prg, warp=False, sound=False)
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        t = mgr.acquire()
        try:
            rig = Rig(t, labels, log)
            rig.ready()
            s = Script()
            s.wr(0x2800, 0x5A)
            s.rd(0x2800)
            s.mark(0x77)
            s.tstart()
            s.delay(100)                       # 100 x 255 x 5 cycles = ~127 ms
            s.tim()
            s.poll(0x2800, 0x0F)               # 0x5A & 0x0F = 0x0A: hits at once
            r = rig.run(s)
            st = {"rd": r[0], "mark": r[1], "delay_us": r[2], "poll": r[3]}
            log(f"VICE self-test: {st}")
            good = (r[0] == 0x5A and r[1] == 0x77 and 120e3 < r[2] < 140e3
                    and r[3]["timeout"] == 0 and r[3]["raw"] == 0x5A)
            log(f"VICE interpreter self-test {'OK' if good else 'FAILED'}")
            reg = {"disabled_block": read_block(rig).hex()}
            reg["block_idle"] = reg["block_scribbled"] = reg["disabled_block"]
            log(f"VICE window distinct bytes {sorted(set(bytes.fromhex(reg['disabled_block'])))}")
            return {"registers": reg, "selftest": st, "selftest_ok": good}
        finally:
            mgr.release(t)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backend", choices=["u64", "vice"], default="u64")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "evidence", "w10-hw"))
    ap.add_argument("--host", default=os.environ.get("U64_HOST"), help="device address (or U64_HOST)")
    ap.add_argument("--analyze", help="re-judge a saved meas json, no device")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--expect-version", type=lambda x: int(x, 0), default=0x10)
    ap.add_argument("--expect-tick-ns", type=float, default=160.0)
    ap.add_argument("--tick-tol", type=float, default=0.03)
    ap.add_argument("--dma-tol", type=float, default=0.10,
                    help="DMA-time tolerance; just above the measured alone-run spread (9.4%%)")
    ap.add_argument("--play-tol", type=float, default=0.005)
    ap.add_argument("--expect-const-ns", type=float, default=120.0)
    ap.add_argument("--const-tol-ns", type=float, default=15.0)
    a = ap.parse_args(argv)
    if a.backend == "u64" and not a.analyze and not a.host:
        ap.error("give --host or set U64_HOST to your device's address")
    a.out = os.path.abspath(a.out)
    os.makedirs(a.out, exist_ok=True)
    logf = open(os.path.join(a.out, f"driver_{a.backend}.log"), "a")

    def log(msg):
        line = f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()
        sess = os.path.join(a.out, "SESSION.log")
        with open(sess, "a") as f:
            f.write(line + "\n")

    expect = {"version": a.expect_version, "tick_ns": a.expect_tick_ns,
              "tick_tol": a.tick_tol, "dma_tol": a.dma_tol, "play_tol": a.play_tol,
              "const_ns": a.expect_const_ns, "const_tol": a.const_tol_ns}
    if a.analyze:
        m = json.load(open(a.analyze))
        return 0 if judge(m, expect, log) else 1
    prg, labels = build(a.out)
    try:
        meas = (u64_run if a.backend == "u64" else vice_run)(a, log, prg, labels)
    finally:                                # keep what was measured on a crash
        json.dump(_MEAS, open(os.path.join(a.out, f"partial_{a.backend}.json"), "w"), indent=1)
    json.dump(meas, open(os.path.join(a.out, f"meas_{a.backend}.json"), "w"), indent=1)
    if a.backend == "vice" and not meas.get("selftest_ok"):
        log("VICE interpreter self-test FAILED")
        return 2
    if a.backend == "vice":
        log("VICE has no sampler: the sampler checks below are expected to FAIL (exit 1)")
    return 0 if judge(meas, expect, log) else 1


if __name__ == "__main__":
    sys.exit(main())
