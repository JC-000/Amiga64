"""W15-M: a generator of small 4-channel M.K. MODs (synthetic test music).

docs/WAVE15_BRIEF.md "W15-M" M1.  The only MODs that go in the repository
or in evidence are written by this module: synthetic waveforms, patterns
written in a tracker-like text form, and the named test songs of
:func:`songs`.  The layout follows the public MOD format description
(build/evidence/w15-mod/PROGRESS.md "Clean-room sources"):

  +0     20  song name (NUL-padded)
  +20    31 x 30 instrument records: name 22, length (words, BE), finetune
         (low nibble, signed), volume (0-64), repeat start (words), repeat
         length (words; 1 = no loop)
  +950   song length (positions), +951 restart position
  +952   128-byte position table, +1080 'M.K.'
  +1084  patterns: 64 rows x 4 channels x 4 bytes; then the sample data
         (8-bit signed) in instrument order

A note cell is ``"C-2 01 A0F"``: note (``---`` none), instrument in hex
(``..`` or ``00`` none), effect and parameter in hex (``...`` none).
Everything may be shortened: ``"C-2 01"``, ``"--- .. C20"``, ``""``.

    python3 -m host.modgen --out DIR [--song NAME]
"""

from __future__ import annotations

import argparse
import math
import os
import struct
from dataclasses import dataclass, field

# The documented finetune-0 periods, C-1 .. B-3 (36 notes).
PERIODS = [856, 808, 762, 720, 678, 640, 604, 570, 538, 508, 480, 453,
           428, 404, 381, 360, 339, 320, 302, 285, 269, 254, 240, 226,
           214, 202, 190, 180, 170, 160, 151, 143, 135, 127, 120, 113]
NOTE_NAMES = ["C-", "C#", "D-", "D#", "E-", "F-", "F#", "G-", "G#", "A-", "A#", "B-"]
ROWS, CHANNELS = 64, 4
HEADER_LEN = 1084


def note_index(name: str) -> int:
    """'C-1' -> 0 .. 'B-3' -> 35."""
    n = NOTE_NAMES.index(name[:2].upper().replace("-", "-"))
    octave = int(name[2])
    if not 1 <= octave <= 3:
        raise ValueError(f"note {name!r}: octave 1-3 only")
    return (octave - 1) * 12 + n


def note_name(idx: int) -> str:
    return f"{NOTE_NAMES[idx % 12]}{idx // 12 + 1}"


# --- samples ---------------------------------------------------------------------

@dataclass
class Sample:
    name: str = ""
    data: bytes = b""            # 8-bit signed PCM, even length
    finetune: int = 0            # -8 .. 7
    volume: int = 64
    loop_start: int = 0          # bytes (even)
    loop_len: int = 0            # bytes (even); 0 or 2 = no loop

    def record(self) -> bytes:
        if len(self.data) % 2 or self.loop_start % 2 or self.loop_len % 2:
            raise ValueError(f"sample {self.name!r}: odd length or loop")
        rl = self.loop_len // 2 if self.loop_len > 2 else 1
        rs = self.loop_start // 2 if self.loop_len > 2 else 0
        return (self.name.encode("latin-1")[:22].ljust(22, b"\0")
                + struct.pack(">HBBHH", len(self.data) // 2, self.finetune & 0x0F,
                              self.volume, rs, rl))


def _pcm(vals) -> bytes:
    return bytes(max(-128, min(127, int(v))) & 0xFF for v in vals)


def sine(n: int = 32, amp: int = 100) -> bytes:
    return _pcm(round(amp * math.sin(2 * math.pi * i / n)) for i in range(n))


def square(n: int = 32, amp: int = 80) -> bytes:
    return _pcm(amp if i < n // 2 else -amp for i in range(n))


def saw(n: int = 64, amp: int = 100) -> bytes:
    return _pcm(round(-amp + 2 * amp * i / n) for i in range(n))


def noise(n: int = 2048, amp: int = 90, seed: int = 0xACE1) -> bytes:
    """One-shot: a 16-bit LFSR, fading out; the first word is 0 (the MOD
    convention for the one-word silent repeat block)."""
    out, s = [0, 0], seed
    for i in range(n - 2):
        s = (s >> 1) ^ (-(s & 1) & 0xB400)
        out.append(((s & 0xFF) - 128) * amp * (n - i) // (128 * n))
    return _pcm(out)


def pluck(n: int = 1024, period: int = 32, amp: int = 110) -> bytes:
    out = [0, 0]
    for i in range(n - 2):
        out.append(amp * math.sin(2 * math.pi * i / period) * (1 - i / n) ** 2)
    return _pcm(out)


def std_samples() -> list[Sample]:
    """The instrument set every named song uses (1-based numbers)."""
    return [
        Sample("sine", sine(), volume=64, loop_start=0, loop_len=32),       # 01
        Sample("square", square(), volume=48, loop_start=0, loop_len=32),   # 02
        Sample("saw", saw(), volume=56, finetune=3, loop_start=0, loop_len=64),  # 03
        Sample("noise", noise(), volume=40),                                # 04 one-shot
        Sample("pluck", pluck(), volume=60, finetune=-2),                   # 05 one-shot
        Sample("pad", sine(64, 60) + sine(64, 60), volume=32,
               loop_start=64, loop_len=64),                                 # 06 loop after start
    ]


# --- patterns --------------------------------------------------------------------

def parse_cell(text: str) -> tuple[int, int, int, int]:
    """'C-2 01 A0F' -> (period, instrument, effect, param)."""
    parts = text.split()
    period = inst = eff = par = 0
    if parts and parts[0] not in ("---", "..."):
        period = PERIODS[note_index(parts[0])]
    if len(parts) > 1 and parts[1] not in ("..", "--"):
        inst = int(parts[1], 16)
    if len(parts) > 2 and parts[2] not in ("...", "---"):
        v = int(parts[2], 16)
        eff, par = v >> 8, v & 0xFF
    if not (0 <= inst <= 31 and 0 <= eff <= 15):
        raise ValueError(f"cell {text!r}")
    return period, inst, eff, par


def pack_cell(period: int, inst: int, eff: int, par: int) -> bytes:
    return bytes([(inst & 0xF0) | (period >> 8), period & 0xFF,
                  ((inst & 0x0F) << 4) | eff, par])


def pattern(rows: dict[int, list[str]] | list[list[str]]) -> bytes:
    """Rows as {row: [ch0, ch1, ch2, ch3]} (missing rows/cells empty) or a
    list of up to 64 rows."""
    if isinstance(rows, list):
        rows = dict(enumerate(rows))
    out = bytearray()
    for r in range(ROWS):
        cells = rows.get(r, [])
        for c in range(CHANNELS):
            out += pack_cell(*parse_cell(cells[c] if c < len(cells) else ""))
    return bytes(out)


@dataclass
class Song:
    name: str
    patterns: list[bytes]
    order: list[int]
    samples: list[Sample] = field(default_factory=std_samples)
    restart: int = 0
    title: str | None = None

    def build(self) -> bytes:
        if not 1 <= len(self.order) <= 128:
            raise ValueError("song length 1-128")
        if len(self.samples) > 31:
            raise ValueError("31 instruments at most")
        npat = max(self.order) + 1
        if npat > len(self.patterns):
            raise ValueError(f"order uses pattern {npat - 1}, only {len(self.patterns)} given")
        title = (self.title if self.title is not None else self.name)
        hdr = bytearray(title.encode("latin-1")[:20].ljust(20, b"\0"))
        smp = self.samples + [Sample()] * (31 - len(self.samples))
        for s in smp:
            hdr += s.record()
        hdr += bytes([len(self.order), self.restart])
        hdr += bytes(self.order).ljust(128, b"\0")
        hdr += b"M.K."
        assert len(hdr) == HEADER_LEN
        body = b"".join(self.patterns[:npat])
        return bytes(hdr) + body + b"".join(s.data for s in smp)


# --- the named test songs -----------------------------------------------------------

def _rows(*pairs) -> dict[int, list[str]]:
    return {r: cells for r, cells in pairs}


def song_basic() -> Song:
    p = _rows((0, ["C-2 01", "E-2 02", "G-2 03", "C-1 04"]),
              (4, ["D-2 ..", "", "A-2 03 C20", ""]),
              (8, ["--- 02", "F-2 01 C40", "", "C-3 05"]),
              (16, ["C-3 06", "--- .. C00", "B-3 01", "C#1 02"]),
              (24, ["", "G#2 03", "", "--- .. C10"]),
              (63, ["C-1 01", "", "", ""]))
    return Song("basic", [pattern(p)], [0])


def song_arp() -> Song:
    p = _rows((0, ["C-2 01 037", "E-2 02 047", "A-3 03 0C0", "C-1 01 000"]),
              (8, ["--- .. 037", "--- .. 0FF", "", "G-2 06 012"]),
              (16, ["", "", "", ""]))
    return Song("arp", [pattern(p)], [0])


def song_slides() -> Song:
    p = _rows((0, ["C-1 01 110", "B-3 02 220", "C-2 03 E13", "C-2 01 E25"]),
              (2, ["--- .. 1FF", "--- .. 2FF", "--- .. E1F", "--- .. E2F"]),
              (6, ["C-3 01 104", "C-1 02 208", "", ""]),
              (10, ["--- .. 100", "--- .. 200", "", ""]))
    return Song("slides", [pattern(p)], [0])


def song_toneporta() -> Song:
    p = _rows((0, ["C-1 01", "C-3 02", "G-2 03", "C-2 01"]),
              (2, ["C-3 .. 308", "C-1 .. 310", "--- .. 304", "C-1 01 340"]),
              (6, ["--- .. 300", "--- .. 300", "E-2 .. 300", ""]),
              (10, ["C-1 .. 502", "--- .. 50F", "--- .. 520", "--- .. 300"]),
              (14, ["--- .. 500", "", "", ""]))
    return Song("toneporta", [pattern(p)], [0])


def song_vibrato() -> Song:
    p = _rows((0, ["C-2 01 448", "E-2 03 4A4", "G-2 02 E41", "C-2 06 E42"]),
              (1, ["", "", "--- .. 4F2", "--- .. 434"]),
              (4, ["--- .. 400", "--- .. 602", "--- .. 400", "--- .. 620"]),
              (8, ["D-2 .. 400", "--- .. 610", "--- .. E44", ""]),
              (9, ["", "", "A-2 .. 488", ""]),
              (12, ["", "", "B-2 .. 400", ""]))
    return Song("vibrato", [pattern(p)], [0])


def song_tremolo() -> Song:
    p = _rows((0, ["C-2 01 748", "E-2 03 7F4", "G-2 02 E71", "C-2 06 E72"]),
              (1, ["", "", "--- .. 7F8", "--- .. 744"]),
              (4, ["--- .. 700", "", "--- .. 700", "--- .. 700"]),
              (8, ["C-3 .. 700", "--- .. C40", "--- .. E74", ""]),
              (9, ["", "", "A-2 .. 788", ""]))
    return Song("tremolo", [pattern(p)], [0])


def song_offset() -> Song:
    p = _rows((0, ["C-2 05 902", "C-2 04 904", "C-2 06 901", "C-2 05 9FF"]),
              (4, ["C-2 05 900", "C-2 04 900", "C-2 06 9FF", "C-2 03 908"]),
              (8, ["--- .. 904", "", "", ""]))
    return Song("offset", [pattern(p)], [0])


def song_volume() -> Song:
    p = _rows((0, ["C-2 01 A0F", "C-2 02 AF0", "C-2 03 EA4", "C-2 06 EB8"]),
              (2, ["--- .. A0F", "--- .. A20", "--- .. EAF", "--- .. EBF"]),
              (4, ["--- .. C50", "--- .. CFF", "--- .. C00", "--- .. A00"]),
              (6, ["--- .. A33", "--- .. A01", "--- 02", "--- .. A00"]),
              (8, ["", "", "", ""]))
    return Song("volume", [pattern(p)], [0])


def song_flow() -> Song:
    """B, D (decimal), B+D on one row, E6 loops (nested per channel), EE."""
    p0 = _rows((0, ["C-2 01", "", "", ""]),
               (1, ["--- .. E60", "E-2 02", "", ""]),
               (3, ["--- .. E62", "", "", ""]),
               (4, ["G-2 03 EE2", "", "", ""]),
               (5, ["", "", "", "--- .. D12"]))          # break to row 12 (decimal)
    p1 = _rows((0, ["C-3 01", "", "", ""]),
               (12, ["D-2 02", "", "", ""]),
               (13, ["", "--- .. B03", "", "--- .. D07"]))  # B+D: position 3, row 7
    p2 = _rows((0, ["E-2 03", "", "", ""]),
               (2, ["", "", "--- .. D99", ""]))            # Dxx > 63 -> row 0
    p3 = _rows((7, ["A-2 06", "", "", ""]),
               (8, ["", "", "", "--- .. B02"]))            # backwards jump: a loop
    return Song("flow", [pattern(p0), pattern(p1), pattern(p2), pattern(p3)],
                [0, 1, 2, 3])


def song_retrig() -> Song:
    p = _rows((0, ["C-2 05 E92", "C-2 04 EC3", "C-2 01 ED2", "--- .. ED1"]),
              (2, ["C-3 05 E93", "C-2 02 EC0", "E-2 03 ED9", "C-2 01 E90"]),
              (4, ["--- .. E91", "", "", ""]))
    return Song("retrig", [pattern(p)], [0])


def song_tempo() -> Song:
    """F speed then F tempo: the CIA timer from row 4 on."""
    p = _rows((0, ["C-2 01 F03", "E-2 02", "", ""]),
              (2, ["--- .. F01", "", "", ""]),
              (3, ["--- .. F04", "", "", ""]),
              (4, ["G-2 03 F96", "", "", ""]),          # 150 BPM
              (8, ["C-3 01 F20", "", "", ""]),          # 32 BPM (slowest CIA)
              (9, ["--- .. FFF", "", "", ""]),          # 255 BPM
              (10, ["--- .. F00", "", "", ""]))         # F00: ignored
    return Song("tempo", [pattern(p)], [0])


def song_edge() -> Song:
    """The rev15-mod rows (fix round 1): tremolo below 0, tone portamento
    down onto a target, 9xx with no note then a note with 900, E6 on two
    channels at once, D + EE on one row (D on the earlier channel), and an
    E6 in a new position whose mark must have been reset (no E60 there)."""
    p0 = _rows((0, ["C-2 01 C04", "C-1 01", "", ""]),
               (1, ["--- .. 7F8", "C-2 .. 3FF", "", ""]),       # 4 - 28 -> 0; 856 -> 601 -> 428
               (2, ["--- .. 7F8", "--- .. 300", "--- .. E60", "--- 05 902"]),   # 9xx: memory only
               (3, ["", "", "", "C-2 05 900"]),                  # offset $200 from memory
               (4, ["--- .. E60", "--- .. E60", "", ""]),
               (6, ["--- .. E61", "--- .. E62", "", ""]),        # two loops on one row
               (7, ["", "", "--- .. D05", "--- .. EE1"]))        # D first, then EE: delayed
    p1 = _rows((5, ["E-2 03", "", "", ""]),
               (6, ["", "", "--- .. E62", ""]),                  # mark reset: back to row 0
               (8, ["G-2 02 D00", "", "", ""]))
    return Song("edge", [pattern(p0), pattern(p1)], [0, 1])


def song_restart() -> Song:
    """Restart position 1 (played without the stop flag): positions 0, 1, 2,
    then 1, 2, 1, ...; each position is two rows (D00)."""
    pats = [pattern(_rows((0, [f"{n} {i:02X}", "", "", ""]), (1, ["--- .. D00", "", "", ""])))
            for n, i in (("C-2", 1), ("E-2", 2), ("G-2", 3))]
    return Song("restart", pats, [0, 1, 2], restart=1)


def song_demo() -> Song:
    """A short tune using the effects together (the listening build)."""
    bass = ["C-1 06", "", "C-1 06 A02", "", "G-1 06", "", "A#1 06 104", "",
            "C-1 06", "", "C-2 06 302", "", "D#1 06", "", "F-1 06 E91", ""]
    lead = ["C-3 03 448", "", "D#3 03 446", "", "G-2 03 0C7", "", "F-2 03", "",
            "C-3 03 E52", "", "A#2 03 7F4", "", "G-2 03 EC4", "", "F-2 03 ED2", ""]
    perc = ["C-3 04", "", "C-2 05", "", "C-3 04 C20", "", "C-2 05", "",
            "C-3 04", "", "C-2 05 E92", "", "C-3 04 C30", "C-3 04 C18", "C-2 05", ""]
    pad = ["C-2 01 C10", "", "", "", "--- .. A01", "", "", "",
           "D#2 02 C18", "", "", "", "--- .. A10", "", "", ""]
    rows = {}
    for r in range(ROWS):
        rows[r] = [bass[r % 16], lead[r % 16], perc[r % 16], pad[r % 16]]
    rows[0][3] = "C-2 01 F7D"        # 125 BPM through the CIA path
    p0 = pattern(rows)
    rows2 = {r: list(v) for r, v in rows.items()}
    rows2[32][1] = "C-3 03 037"
    rows2[63][0] = "--- .. E60"
    p1 = pattern(rows2)
    return Song("demo", [p0, p1], [0, 1, 0, 1], title="amiga64 demo")


SONGS = {f.__name__[5:]: f for f in (song_basic, song_arp, song_slides, song_toneporta,
                                    song_vibrato, song_tremolo, song_offset, song_volume,
                                    song_flow, song_retrig, song_tempo, song_demo,
                                    song_edge, song_restart)}


def songs() -> dict[str, bytes]:
    return {n: f().build() for n, f in SONGS.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--song", action="append")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    for n in a.song or SONGS:
        data = SONGS[n]().build()
        with open(os.path.join(a.out, f"{n}.mod"), "wb") as f:
            f.write(data)
        print(f"{n}.mod {len(data)} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
