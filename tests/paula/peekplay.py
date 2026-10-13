#!/usr/bin/env python3
"""W16-P: a PLAYING aud survives aros_wb.GuestPeek (WAVE16 M0 ruling (1)).

    python3 -m tests.paula.peekplay --prg build/runner.prg [--mutant peeknorest]

VICE, the player profile (tests/run_paula's rig, the module's log build):
four channels play looping blocks (latches = the block: hardware loops)
and the guest acknowledges every AUDx, for long enough that GuestPeek
parks the 6510 many times while blocks end.  drv_run is launched through
a trampoline (not the blocking jsr) so the host can peek while it runs.
PASS needs all of these:
  - every park left $CA00-$CDFF byte for byte as the 6510 had it (the
    peek's own audit: read right after the halt, and again just before
    the 6510 runs on);
  - at least MIN_PLAYING parks fell while the module was firing (st_nfire
    moved between two parks);
  - afterwards the run is clean and, as in run_paula's legs: the log
    mirror equals the voice images (M5 logcheck, no hazard), both segments
    are intact, every channel still plays its block in a hardware loop
    (one program, CTRL 3).
Not checked: leg blocks' exact grid.  A park stops the 6510 while the CIA
time base runs on (the peek's real cost to the guest, as in W16-D's e2e),
so an unacknowledged AUDx can make a channel park legitimately.
`--mutant peeknorest` (aros_wb.AUD_MUTANTS) must FAIL.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from host import paula_ref as PR            # noqa: E402
from tests import aros_wb as WB             # noqa: E402
from tests import run_paula as RP           # noqa: E402

TRAMP = 0xC900                  # jsr drv_run / jmp * (free in run_paula's layout)
PEEK_AT = 0x020000              # any guest page: what the peek reads does not matter
S = 4
PERS = (250, 333, 401, 517)
LENS = (80, 72, 56, 48)         # run_paula leg blocks' channels (a 1 MHz host keeps up)
BUSY = 12                       # x 64K busy iterations, acknowledging AUDx
MIN_PLAYING = 5
PEEK_GAP_S = 0.05               # between parks: the guest runs (and acknowledges)
PEEK_MAX = 150                  # rev16-p M5: then only wait (a loaded host cannot time out)


class AuditFail(Exception):
    """A park left aud's bytes changed: the test fails here, by the audit."""


def program():
    w = sum((RP.chw(x, RP.SAMPLE + 0x400 * x, LENS[x], PERS[x], 64) for x in range(4)), [])
    w += [(RP.DMACON, 0x820F)]
    g = RP.G()
    RP.emit(g, w)
    RP.busy(g, BUSY, "b", ack=True)
    g(RP.HALT)
    return g.words()


def run_peeking(rig, pc: int, peek, rep: dict) -> dict:
    """Rig.run with drv_run launched by goto; peek until R[0] = $FF."""
    from c64_test_harness import goto, read_bytes
    drv = rig.dlab["drv_run"]
    orig = rig.call

    def call(addr: int, timeout: float = 300.0) -> None:
        if addr != drv:
            return orig(addr, timeout)
        rig.w(TRAMP, bytes([0x20, drv & 0xFF, drv >> 8, 0x4C, (TRAMP + 3) & 0xFF,
                            (TRAMP + 3) >> 8]))
        goto(rig.t, TRAMP)
        nf, playing, n = None, 0, 0
        t0 = time.monotonic()
        while True:
            r0 = read_bytes(rig.t, RP.R, 1)[0]
            fire = struct.unpack("<H", bytes(read_bytes(rig.t, RP.AUD_A + 21, 2)))[0]
            rig.t.resume()
            if r0 == 0xFF:
                break
            if nf is not None and fire != nf and n < PEEK_MAX:
                playing += 1                    # the next park falls while blocks end
            nf = fire
            if n < PEEK_MAX:
                peek.fresh()
                peek.read(PEEK_AT, 256)
                n += 1
                if not peek.audit[-1]:          # rev16-p M5: stop at the first bad park
                    rep["peeks"], rep["peeks_while_firing"] = n, playing
                    raise AuditFail(f"park {n} left $CA00-$CDFF changed (the peek did not "
                                    "put aud's bytes back)")
            time.sleep(PEEK_GAP_S)
            if time.monotonic() - t0 > timeout:
                raise RP.Hang("drv_run did not return under the peeks")
        rep["peeks"], rep["peeks_while_firing"] = n, playing
    rig.call = call
    try:
        return rig.run(pc, s=S)
    finally:
        rig.call = orig


def loops(rig, f: list[str], tag: str, rep: dict) -> None:
    """Every channel on, one program, its voice looping in hardware."""
    m, st = rig.state()
    rep["blocks"], rep["parks_mod"] = [mx["nb"] for mx in m], st["npark"]
    for x in range(4):
        if m[x]["np"] != 1 or m[x]["ctl"] != 3 or m[x]["nb"] < 3:
            f.append(f"{tag} ch{x}: np {m[x]['np']} ctl {m[x]['ctl']} nb {m[x]['nb']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--mutant", default=None, choices=WB.AUD_MUTANTS)
    a = ap.parse_args(argv)
    WB.AUD_MUTANT[0] = a.mutant
    rep: dict = {"mutant": a.mutant}

    def body(rig) -> list[str]:
        f = RP.need_hook(rig, "peekplay")
        if f:
            return f
        pc = rig.stage(program())
        rig.cfg(PR.R16_AMIGA)
        peek = WB.GuestPeek(rig)
        peek.audit = []
        try:
            res = run_peeking(rig, pc, peek, rep)
        except AuditFail as e:
            return [f"peekplay: AUDIT: {e}"]
        rep["parks"], rep["parks_changed"] = len(peek.audit), peek.audit.count(False)
        if rep["parks_changed"]:
            f.append(f"peekplay: {rep['parks_changed']} of {rep['parks']} parks changed "
                     "$CA00-$CDFF (the peek did not put aud's bytes back)")
        if rep.get("peeks_while_firing", 0) < MIN_PLAYING:
            f.append(f"peekplay: only {rep.get('peeks_while_firing')} peeks while the module "
                     f"fired (want >= {MIN_PLAYING}): the test has no teeth")
        if not RP.clean(res, "peekplay", f):
            return f
        RP.logcheck(rig, f, "peekplay")
        rig.intact(f, "peekplay")
        loops(rig, f, "peekplay", rep)
        return f

    try:
        fails = RP.session(a.prg, body)
    except Exception as e:                       # noqa: BLE001
        fails = [f"{type(e).__name__}: {e}"]
    for x in fails:
        print(f"FAIL {x}")
    print(f"peekplay{' ' + a.mutant if a.mutant else ''}: {rep}")
    print(f"peekplay: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
