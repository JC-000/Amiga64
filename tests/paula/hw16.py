#!/usr/bin/env python3
"""W16-P hardware probe (one U64E hold, supervisor-approved 2026-10-12).

    U64_HOST=10.43.23.81 U64_BASELINE_ON_ENTRY=0 python3 -m tests.paula.hw16 \
        --prg build/runner.prg --out build/evidence/w16-p/hw16

Legs, in one DeviceLock hold (tests/bench/u64_session.u64_hold; config
IDENTICAL afterwards or exit 3):
  df21     the sampler proof made differential (M0 ruling (3)): the 6510
           copies $DF20-$DFFF (never $DF1C-$DF1F, the UCI) with "Map
           Ultimate Audio $DF20-DFFF" Disabled, then Enabled; both dumps
           are recorded, and the leg looks for a byte pattern that tells
           them apart.
  rbsil    does the WB launcher's session end (set_speed(1) + reboot, as
           tests/aros_wb.session's finally) silence a voice that is still
           playing?  A hardware-looped tone is left running (the guest halts
           with DMA on); the UDP audio stream is captured across the reboot.
           If the tone survives, the fallback is tried as well: reload the
           runner and stop every voice from the 6510 (B := $FFFFFF, CTRL := 0,
           the CONTRACTS write order), then capture again.  Needs the UDP
           audio stream (the U64E; the C64U is on WiFi: no stream).
  rbwin    the instrumental form (W16-P fix round 1, M3: the C64U): the
           window dumped idle (after init), while the looped tone plays (the
           guest halted with DMA on), and after the session end's
           set_speed(1) + reboot (the runner reloaded, the item still mapped).
           Silence is shown if the playing dump differs from the idle one and
           the post-reboot dump equals the idle one again.
  cia      tools/s3probe/cia_rate inside the same hold (after the other legs:
           it replaces the runner): CIA2 ticks per video frame at 1 MHz and at
           --mhz (phi2 = ticks/frame x the frame rate).
Every voice is stopped by the 6510 before the hold restores, whatever happened.

    U64_HOST=10.43.23.158 ... python3 -m tests.paula.hw16 --mhz 64 --legs df21,rbwin,cia ...
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tests import run_paula as RP           # noqa: E402

STUB = 0xC100                   # buf_case: free before the driver is installed / used
DUMP = 0xC400                   # the window copy (224 bytes); run_paula's driver is at $C200
WIN_LO, WIN_N = 0xDF20, 0xE0
MAP_ITEM = RP.MAP_ITEM
LOG: list[str] = []


def hlog(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    LOG.append(line)
    print(line, flush=True)


def copy_stub() -> bytes:
    """ldx #0 / lda $DF20,x / sta DUMP,x / inx / cpx #$E0 / bne / rts"""
    return bytes([0xA2, 0x00,
                  0xBD, WIN_LO & 0xFF, WIN_LO >> 8,
                  0x9D, DUMP & 0xFF, DUMP >> 8,
                  0xE8, 0xE0, WIN_N, 0xD0, 0xF5, 0x60])


def stop_stub() -> bytes:
    """Every voice v (0-6): REPEAT B (+$15..+$17) := $FF, then CTRL (+0) := 0
    (CONTRACTS "Write order": repeat never off with B == LENGTH)."""
    code = bytearray([0xA9, 0xFF])                          # lda #$FF
    for v in range(7):
        for o in (0x15, 0x16, 0x17):
            a = WIN_LO + 32 * v + o
            code += bytes([0x8D, a & 0xFF, a >> 8])
    code += bytes([0xA9, 0x00])                             # lda #0
    for v in range(7):
        a = WIN_LO + 32 * v
        code += bytes([0x8D, a & 0xFF, a >> 8])
    return bytes(code + b"\x60")


def read_window(rig) -> bytes:
    rig.w(STUB, copy_stub())
    rig.call(STUB, 30.0)
    return bytes(rig.r(DUMP, WIN_N))


def read_window_t(tg) -> bytes:
    """read_window on a bare prepared target (no rig): the runner's call."""
    from c64_test_harness import read_bytes, write_bytes
    from tests.harness_util import call_runner
    write_bytes(tg.transport, STUB, copy_stub())
    call_runner(tg, STUB, 30.0)
    return bytes(read_bytes(tg.transport, DUMP, WIN_N))


def is_sampler(win: bytes) -> bool:
    """The mapped sampler's signature (hw16 df21, U64E fw 3.15): every
    even byte of $DF20-$DFFF reads $00, every odd byte $10.  Unmapped the
    window reads changing bus data ($DF21 $00 there); on VICE it is the
    REC's mirror (never read there by the product path)."""
    return len(win) == WIN_N and win == bytes([0x00, 0x10]) * (WIN_N // 2)


def stop_voices(rig) -> None:
    rig.w(STUB, stop_stub())
    rig.call(STUB, 30.0)


def blocks(win: bytes) -> list[str]:
    return [win[i:i + 32].hex() for i in range(0, len(win), 32)]


def signature(off: bytes, on: bytes) -> dict:
    """What tells the two dumps apart: the differing offsets, and whether
    the Disabled dump repeats with a period of 32 (a mirror) or 16."""
    diff = [i for i in range(WIN_N) if off[i] != on[i]]
    rep = {p: all(off[i] == off[i % p] for i in range(WIN_N)) for p in (16, 32)}
    rep_on = {p: all(on[i] == on[i % p] for i in range(WIN_N)) for p in (16, 32)}
    return {"n_diff": len(diff), "diff_offsets": [f"+${i:02X}" for i in diff[:64]],
            "off_periodic": rep, "on_periodic": rep_on,
            "df21_off": f"${off[1]:02X}", "df21_on": f"${on[1]:02X}"}


def stream(client):
    """Start the UDP audio stream into a capture; -> (cap, t0)."""
    import socket
    from c64_test_harness.backends.u64_audio_capture import (
        AudioCapture, U64_NTSC_AUDIO_RATE_HZ)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect((os.environ.get("U64_HOST", "10.43.23.81"), 80))
    ip = s.getsockname()[0]
    s.close()
    cap = AudioCapture(port=0, sample_rate=U64_NTSC_AUDIO_RATE_HZ)
    cap.start()
    client.stream_audio_start(f"{ip}:{cap.port}")
    return cap, time.monotonic()


def stream_stop(client, cap, wav: str) -> dict:
    import wave
    import numpy as np
    try:
        client.stream_audio_stop()
    except Exception as e:                       # noqa: BLE001
        hlog(f"stream_audio_stop failed: {e!r}")
    info = cap.stop(wav_path=wav)
    with wave.open(wav, "rb") as w:
        fr = w.getframerate()
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").reshape(
            -1, w.getnchannels()).astype(np.float64)
    return {"mono": raw.mean(axis=1), "rate": float(fr),
            "packets": info.packets_received, "dropped": info.packets_dropped,
            # W16-E4: where the harness zero-filled lost packets (frames), and
            # whether the sample index is still a clock
            "filled": list(getattr(info, "filled_frame_ranges", ())),
            "intact": bool(getattr(info, "time_base_intact", not info.packets_dropped))}


def win_stats(au: dict, lo: float, hi: float) -> dict:
    """rms (mean removed) and the peak frequency in [lo, hi) s of the capture."""
    x = au["mono"][int(lo * au["rate"]):int(hi * au["rate"])]
    return {k: round(v, 1) for k, v in RP.peak(x, au["rate"]).items()} | {"n": len(x)}


def tone_guest():
    """A 4096-sample square (period 32) at PER 428, VOL 64, looped in hardware
    (the latches = the block), then 30 frames, HALT with DMA still on."""
    from tests.run_mach import G, HALT, BASE, mv_imm_abs_w
    from tests.run_irq import link
    data = RP.square(32, 4096)

    def build(daddr: int) -> G:
        g = G()
        mv_imm_abs_w(g, 0x7FFF, RP.INTREQ)
        RP.emit(g, RP.chw(0, daddr, 2048, 428, 64) + [(RP.DMACON, 0x8201)])
        RP.wait_frames(g, 30, "w0")
        g(HALT)
        return g
    n = len(link(build(0), BASE))
    daddr = BASE + 2 * n
    daddr += -daddr % 16
    words = link(build(daddr), BASE)
    pad = (daddr - BASE) // 2 - len(words)
    dw = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2)]
    return words + [0] * pad + dw


def leg_rbsil(rig, client, tg, prg, out: str, rep: dict) -> list[str]:
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.run_vectors import prepare_target
    f: list[str] = []
    r = rep.setdefault("rbsil", {})
    pc = rig.stage(tone_guest())
    rig.cfg()
    rig.call(RP.AUD_A + 10)                       # init: voices silent
    cap, t0 = stream(client)
    wav = os.path.join(out, "audio_rbsil.wav")
    try:
        time.sleep(0.5)
        t_run = time.monotonic() - t0
        res = rig.run(pc, s=RP.HW_S)
        t_ret = time.monotonic() - t0
        time.sleep(1.5)                           # the tone loops on in hardware
        t_rb = time.monotonic() - t0
        tg.transport.set_speed(1)                 # = aros_wb.session's finally
        reboot(client)
        t_rb_done = time.monotonic() - t0
        time.sleep(4.0)
        t_end = time.monotonic() - t0
    finally:
        au = stream_stop(client, cap, wav)
    r["times"] = {k: round(v, 2) for k, v in dict(run=t_run, ret=t_ret, reboot=t_rb,
                                                  reboot_done=t_rb_done, end=t_end).items()}
    r["halt"] = res.get("halt"), res.get("reason")
    r["before_run"] = win_stats(au, 0.05, max(0.1, t_run - 0.05))
    r["before_reboot"] = win_stats(au, t_ret + 0.2, t_rb - 0.1)
    r["after_reboot"] = win_stats(au, t_rb_done + 1.0, min(t_end, len(au["mono"]) / au["rate"]))
    r["packets"], r["dropped"] = au["packets"], au["dropped"]
    hlog(f"rbsil: before run {r['before_run']}, playing {r['before_reboot']}, "
         f"after reboot {r['after_reboot']}; packets {au['packets']} dropped {au['dropped']}")
    playing = r["before_reboot"]["rms"] > 1000 and abs(r["before_reboot"]["f"] - 259.1) < 3
    if not playing:
        f.append(f"rbsil: no tone before the reboot ({r['before_reboot']}): the probe is void")
    r["silenced_by_reboot"] = r["after_reboot"]["rms"] < 200
    hlog(f"rbsil: the reboot {'SILENCES' if r['silenced_by_reboot'] else 'does NOT silence'} "
         "a playing voice")
    try:
        r["map_item_after_reboot"] = str(client.get_config_item(rep["map_cat"][0], MAP_ITEM))
    except Exception as e:                        # noqa: BLE001
        r["map_item_after_reboot"] = repr(e)
    if not r["silenced_by_reboot"]:               # the fallback path
        tg.transport.set_speed(MHZ[0])
        reboot(client)
        time.sleep(1.0)
        prepare_target(tg, prg)
        cap, t0 = stream(client)
        wav2 = os.path.join(out, "audio_rbsil_stop.wav")
        try:
            time.sleep(1.0)
            t_s = time.monotonic() - t0
            stop_voices(rig)
            t_s2 = time.monotonic() - t0
            time.sleep(2.0)
            t_e = time.monotonic() - t0
        finally:
            au2 = stream_stop(client, cap, wav2)
        r["fallback_before"] = win_stats(au2, 0.2, t_s - 0.05)
        r["fallback_after"] = win_stats(au2, t_s2 + 0.3, t_e)
        r["silenced_by_stub"] = r["fallback_after"]["rms"] < 200
        hlog(f"rbsil fallback: before {r['fallback_before']}, after the stop stub "
             f"{r['fallback_after']}: {'silent' if r['silenced_by_stub'] else 'STILL PLAYING'}")
        if not r["silenced_by_stub"]:
            f.append("rbsil: the 6510 stop stub did not silence the voice")
    return f


MHZ = [48]                      # --mhz: the device's turbo (U64E 48, C64U 64)


def leg_rbwin(rig, client, tg, prg, rep: dict) -> list[str]:
    """rbwin (docstring above): no stream needed."""
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.run_vectors import prepare_target
    f: list[str] = []
    r = rep.setdefault("rbwin", {})
    pc = rig.stage(tone_guest())
    rig.cfg()
    rig.call(RP.AUD_A + 10)                       # init: voices silent
    idle = read_window(rig)
    res = rig.run(pc, s=RP.HW_S)                  # returns with the voice looping
    time.sleep(0.2)
    play = read_window(rig)
    time.sleep(0.3)
    play2 = read_window(rig)
    tg.transport.set_speed(1)                     # = aros_wb.session's finally
    reboot(client)
    time.sleep(1.0)
    prepare_target(tg, prg)
    after = read_window(rig)
    r.update(halt=[res.get("halt"), res.get("reason")], idle=blocks(idle), playing=blocks(play),
             playing_again=blocks(play2), after_reboot=blocks(after))
    r["playing_differs"] = play != idle
    r["after_equals_idle"] = after == idle
    hlog(f"rbwin: idle v0 {idle[:32].hex()}")
    hlog(f"rbwin: play v0 {play[:32].hex()} (again {play2[:32].hex()})")
    hlog(f"rbwin: after v0 {after[:32].hex()}")
    if not r["playing_differs"]:
        r["verdict"] = "undetermined: the window does not show a playing voice"
    else:
        r["verdict"] = "silenced" if r["after_equals_idle"] else "NOT silenced"
        if not r["after_equals_idle"]:
            f.append("rbwin: the window still differs from idle after the reboot")
    hlog(f"rbwin: {r['verdict']}")
    return f


def leg_cia(tg, out: str, rep: dict) -> list[str]:
    """cia_rate's probe at 1 MHz and MHZ[0] (2 runs each), NTSC expected."""
    sys.path.insert(0, os.path.join(ROOT, "tools", "s3probe", "cia_rate"))
    import cia_rate as CR
    from c64_test_harness import run_prg_via_sys
    d = os.path.join(out, "cia_rate")
    prg, labels = CR.build(d)
    tg.transport.set_speed(1)
    run_prg_via_sys(tg, open(prg, "rb").read())

    def set_speed(m):
        tg.transport.set_speed(m)
        time.sleep(0.5)
        hlog(f"cia: CPU speed {m} MHz")
    runs = CR.measure(tg, labels, [1, MHZ[0]], 2, set_speed, hlog)
    ok = CR.report(runs, None, hlog)
    rep["cia"] = [{"mhz": x["mhz"], "run": x["run"], "pal": x["pal"],
                   "ticks_per_frame": x["decoded"]["mean_ticks_per_frame"]} for x in runs]
    return [] if ok else ["cia: a run is off the phi2 expectation (CR.report)"]


def main(argv=None) -> int:
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench import u64_session as us
    from tests.run_vectors import prepare_target
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--legs", default="df21,rbsil")
    ap.add_argument("--mhz", type=int, default=48, help="the device's turbo (U64E 48, C64U 64)")
    a = ap.parse_args(argv)
    legs = a.legs.split(",")
    MHZ[0] = a.mhz
    signal.signal(signal.SIGINT, signal.default_int_handler)

    def term(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGALRM, term)
    os.makedirs(a.out, exist_ok=True)
    us.log = hlog
    prg = os.path.abspath(a.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    rep: dict = {"legs": legs}
    fails: list[str] = []
    with us.u64_hold(a.out) as hold:
        tg = hold["target"]
        client = tg.transport.client
        rig = None
        try:
            cats = {c: client.get_config_category(c).get(c, {}) for c in client.list_configs()}
            where = [c for c, items in cats.items() if MAP_ITEM in items]
            rep.update(map_cat=where, info=hold["info"],
                       map_found=[cats[c].get(MAP_ITEM) for c in where])
            hlog(f"found {MAP_ITEM!r} in {where} = {rep['map_found']}")
            if not where:
                raise RuntimeError(f"INFRA: no {MAP_ITEM!r}")
            tg.transport.set_speed(MHZ[0])
            reboot(client)
            time.sleep(1.0)
            prepare_target(tg, prg)
            rig = RP.HwRig(tg, labels, prg)
            if "df21" in legs:
                off = read_window(rig)
                off2 = read_window(rig)
            client.set_config_item(where[0], MAP_ITEM, "Enabled")
            time.sleep(1.0)
            prepare_target(tg, prg)
            hlog("sampler mapped, runner reloaded")
            if "df21" in legs:
                on = read_window(rig)
                on2 = read_window(rig)
                sig = signature(off, on)
                rep["df21"] = {"off": blocks(off), "off_again_equal": off == off2,
                               "on": blocks(on), "on_again_equal": on == on2, **sig}
                hlog(f"df21: Disabled $DF21 = {sig['df21_off']}, Enabled {sig['df21_on']}; "
                     f"{sig['n_diff']} differing bytes; periodic off {sig['off_periodic']} "
                     f"on {sig['on_periodic']}")
                for i, blk in enumerate(blocks(off)):
                    hlog(f"  off +${32 * i:02X}: {blk}")
                for i, blk in enumerate(blocks(on)):
                    hlog(f"  on  +${32 * i:02X}: {blk}")
                if on[1] != 0x10:
                    fails.append(f"df21: Enabled $DF21 = ${on[1]:02X}, not $10")
            if "rbsil" in legs:
                rig.install()
                fails += leg_rbsil(rig, client, tg, prg, a.out, rep)
            if "rbwin" in legs:
                rig.install()
                fails += leg_rbwin(rig, client, tg, prg, rep)
            if "cia" in legs:
                fails += leg_cia(tg, a.out, rep)
        except Exception as e:                    # noqa: BLE001
            fails.append(f"{type(e).__name__}: {e}")
            hlog(f"error: {e!r}")
        finally:                                  # every voice stopped before the restore
            try:
                tg.transport.set_speed(MHZ[0])
                prepare_target(tg, prg)
                if rig is None:
                    rig = RP.HwRig(tg, labels, prg)
                stop_voices(rig)
                hlog("voices stopped by the 6510 before the restore")
            except Exception as e:                # noqa: BLE001
                hlog(f"stopping the voices before the restore failed: {e!r}")
    rep["restored"] = hold.get("restored")
    rep["fails"] = fails
    with open(os.path.join(a.out, "report.json"), "w") as fh:
        json.dump(rep, fh, indent=1, default=str)
    with open(os.path.join(a.out, "SESSION.log"), "a") as fh:
        fh.write("\n".join(LOG) + "\n")
    hlog(f"hw16: {'PASS' if not fails else f'FAIL ({len(fails)})'}; "
         f"config restored: {hold.get('restored')}")
    if not hold.get("restored"):
        return 3
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
