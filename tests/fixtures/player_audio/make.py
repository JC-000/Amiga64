#!/usr/bin/env python3
"""Cut the player-probe audio fixtures (W16-A) from the sup16 recordings.

    python3 tests/fixtures/player_audio/make.py   # reads build/evidence/sup16/*-player

For each device: the probe's left channel (channel 0 plays there) as int16
segments of the original timeline -- the pre-roll with the C64U's DC step
and the first note's onset, the pitch windows of PER 856, 339 (the sup16
C64U failure) and 113 (the largest sampler-law difference) and the last
note's onset range under both alignments (the first find_tone window and
a sustained tone) -- plus the guest tick records.  The gaps are filled with
the pre-roll median and the right channel with its pre-roll median by
host/tests/test_player_audio.py's loader (the probe's right side is
silent; mono = (L + R) / 2 as run_player builds it).  Each file < 200 KB.
"""
import os
import sys
import wave

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, ROOT)
from tests import run_mod as RM  # noqa: E402
from tests import run_player as P  # noqa: E402
from tests.run_paula import find_tone  # noqa: E402

RATE = 47940.34090909091
PHI2 = 1022727
HERE = os.path.dirname(os.path.abspath(__file__))
KEEP = (0, 3, 7)        # the probe notes kept: PER 856 (next to the step), 339, 113


def spans(recs: bytes, a_list: list[int]) -> list[tuple[int, int]]:
    c = P.cases(PHI2, 0, 20.0, False, "demo")[0]
    t0 = RM.rec_time(recs, 0)
    sec = lambda i: (RM.rec_time(recs, i) - t0) / PHI2
    notes = []
    for i, t in enumerate(c.ticks):
        p, ins = __import__("struct").unpack_from(">HB", t.view, 36)
        if notes and notes[-1][2] == p and notes[-1][3] == ins:
            notes[-1][1] = i
        else:
            notes.append([i, i, p, ins])
    sine = [n for n in notes if n[2] and n[3] == 1 and n[1] - n[0] >= 12]
    out = []
    for a in a_list:
        for i0, i1, _, _ in (sine[k] for k in KEEP):
            lo, hi = sec(i0), sec(i1)
            m0, m1 = lo + 0.2 * (hi - lo), hi - 0.2 * (hi - lo)
            out.append((a + int(m0 * RATE) - 64, a + int(m1 * RATE) + 64))
        il = sine[-1][0]
        out.append((a + int((sec(il) - 0.1) * RATE) - 64, a + int((sec(il) + 0.3) * RATE) + 64))
    return out


def main() -> None:
    for dev in ("c64u", "u64e"):
        d = os.path.join(ROOT, "build", "evidence", "sup16", f"{dev}-player")
        with wave.open(os.path.join(d, "audio_probe.wav")) as w:
            raw = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, 2)
        recs = open(os.path.join(d, "recs_probe.bin"), "rb").read()
        mono = raw.astype(np.float64).mean(axis=1)
        a_old, _ = find_tone(mono, RATE)
        a_new = int(0.7793 * RATE) if dev == "c64u" else a_old   # checked by the test
        pre0 = max(0, min(a_old, a_new) - int(0.25 * RATE))
        segs = [(pre0, a_new + int(0.15 * RATE))] + spans(recs, sorted({a_old, a_new}))
        segs.sort()
        merged = []
        for s, e in segs:                       # union of the spans
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        L = raw[:, 0]
        pre = raw[:a_old - int(0.05 * RATE)]
        np.savez_compressed(
            os.path.join(HERE, f"probe_{dev}.npz"), n=len(L),
            starts=np.array([s for s, _ in merged]),
            data=np.concatenate([L[s:e] for s, e in merged]),
            lens=np.array([e - s for s, e in merged]),
            fill_l=int(np.median(pre[:, 0])), fill_r=int(np.median(pre[:, 1])),
            recs=np.frombuffer(recs, np.uint8))
        sz = os.path.getsize(os.path.join(HERE, f"probe_{dev}.npz"))
        print(dev, "segments", len(merged), "samples", sum(e - s for s, e in merged), "bytes", sz)


if __name__ == "__main__":
    main()
