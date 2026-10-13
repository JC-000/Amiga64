"""W16-P: the Workbench profile block and the app anchor (WAVE16 M0 ruling (4)).

A guest program can't measure real time itself, so the host tells it the
profile's facts in a block of guest memory: rc region ``prof``
$2DC000-$2DC0FF (runtime/rc/machine.inc), right after the staged ADF.
Plain REU memory: outside chip and slow RAM, so AROS never allocates it.
W13-W's boot-model watch saw no guest access to banks $20-$2D before the
Workbench.

The profile block, $2DC000-$2DC03F, is written by the host only, and only
while the guest is not running: at the boot, after ``Machine.stage`` and
before ``start``; at a resume, after ``aros_fill`` and before the
``snap_resume`` launch (the file spans end at $2DBFFF). It is big-endian,
the 68000's own order:

  +0  u32 magic 'A64P'
  +4  u16 version (1)
  +6  u16 size (64)
  +8  u32 e_hz_q8   guest E ticks per real second x 256
  +12 u32 tick_e    round(E Hz / 50): the 125-BPM tick in E (CIA period =
                    tick_e x 125 / BPM)
  +16 u32 frame_e   E ticks per guest frame (the VERTB period) = 14188
  +20 u32 phi2_hz   the host's phi2 (NTSC 1022727, PAL 985248)
  +24 u8  s         the E-clock shift in force (mach_sl)
  +25 u8  host      HOST_VICE / HOST_U64E / HOST_C64U
  +26 u8  flags     F_AUD (aud armed: Paula writes reach the module),
                    F_LIVE (the sampler is live: audible), F_RT (real time
                    is meaningful: an Ultimate, not VICE warp)
  +27 u8  0
  +28 u16 cfg_r16   the aud module's r16 in force
  +30 u16 CPU MHz   (informative)
  +32..+59 0 (reserved)
  +60 u32 check     ~(the 32-bit sum of the longs at +0..+56)

A guest takes the block only when the magic, the version and the check
all match. Otherwise (a real Amiga, or a session without the block, which
reads zero) it falls back to Amiga timing.

The app anchor, $2DC040-$2DC04F, is written by the guest program and only
read by the host:

  +0  u32 magic 'MWBA'
  +4  u32 pointer to the app's state struct (mod_view etc.)
  +8  u32 the guest's sequence/flags long
  +12 u32 reserved

The anchor rules (rev16-p M4; docs/CONTRACTS.md):
  1. The program writes the anchor only when the profile block is valid
     (otherwise $2DC040 may be anything, e.g. a real Amiga's memory).
  2. Publish: +4, +8, +12 first, 'MWBA' last.  Withdraw (exit, before the
     struct is freed): clear 'MWBA' first.  The host may read between any
     two guest writes, so the magic gates the rest.
  3. The pointer is never 0, even, and the struct lies inside chip RAM
     ($000000-$1FFFFF) or slow RAM ($C00000-$DBFFFF).
  4. A snapshot's file spans end at $2DBFFF: a file resume loses the anchor
     (and the block, which the launcher rewrites); the app must publish it
     again after a resume if it relies on it.
"""

from __future__ import annotations

import struct

from host import paula_ref as PR

PROF_REGION = (0x2DC000, 0x2DC0FF)      # rc machine.inc `prof` (inclusive)
PROF_ADDR, PROF_SIZE = 0x2DC000, 64
ANCHOR_ADDR, ANCHOR_SIZE = 0x2DC040, 16

MAGIC = b"A64P"
VERSION = 1
ANCHOR_MAGIC = b"MWBA"
FRAME_E = 14188                         # MACH_FRAME_E

HOST_VICE, HOST_U64E, HOST_C64U = 0, 1, 2
F_AUD, F_LIVE, F_RT = 0x01, 0x02, 0x04

# guest RAM an anchor's pointer may name (machine profile): chip, slow
RAM_SPANS = ((0x000000, 0x200000), (0xC00000, 0xDC0000))

_HDR = struct.Struct(">4sHHIIIIBBBBHH")     # +0 .. +31


def e_hz(phi2_hz: float, s: int) -> float:
    """Guest E ticks per real second: phi2 >> s (CIA2 counts phi2 at any
    turbo, HARDWARE "CIA clock at turbo")."""
    return phi2_hz / (1 << s)


def tick_e(phi2_hz: float, s: int) -> int:
    """E ticks per 1/50 s: the 125-BPM replay tick."""
    return round(e_hz(phi2_hz, s) / 50)


def r16(phi2_hz: float, s: int) -> int:
    """The aud module's cfg_r16 for this profile (E ticks per PAL colour
    clock x 65536)."""
    return PR.r16_for(e_hz(phi2_hz, s))


def check(body: bytes) -> int:
    """~(the 32-bit sum of the 15 longs at +0..+56)."""
    return ~sum(struct.unpack(">15I", body[:60])) & 0xFFFFFFFF


def build(s: int, host: int, flags: int, mhz: int,
          phi2_hz: int = PR.NTSC_PHI2) -> bytes:
    """The 64-byte profile block for shift *s* on *host*."""
    if not 0 <= s <= 16:
        raise ValueError(f"s {s} out of range")
    if host not in (HOST_VICE, HOST_U64E, HOST_C64U):
        raise ValueError(f"host {host}")
    if flags & ~(F_AUD | F_LIVE | F_RT):
        raise ValueError(f"flags ${flags:02X}")
    if flags & F_LIVE and not flags & F_AUD:
        raise ValueError("F_LIVE needs F_AUD (the sampler is driven by the module)")
    eq8 = round(e_hz(phi2_hz, s) * 256)
    body = _HDR.pack(MAGIC, VERSION, PROF_SIZE, eq8, tick_e(phi2_hz, s), FRAME_E,
                     phi2_hz, s, host, flags, 0, r16(phi2_hz, s), mhz)
    body += bytes(60 - len(body))
    return body + struct.pack(">I", check(body))


def parse(raw: bytes) -> dict | None:
    """The guest's rule: the fields, or None when the magic, the version
    or the check do not match (the guest then uses Amiga timing)."""
    if len(raw) < PROF_SIZE:
        return None
    raw = bytes(raw[:PROF_SIZE])
    f = _HDR.unpack_from(raw)
    if f[0] != MAGIC or f[1] != VERSION or f[2] != PROF_SIZE:
        return None
    if struct.unpack_from(">I", raw, 60)[0] != check(raw):
        return None
    return {"e_hz_q8": f[3], "tick_e": f[4], "frame_e": f[5], "phi2_hz": f[6],
            "s": f[7], "host": f[8], "flags": f[9], "r16": f[11], "mhz": f[12]}


def parse_anchor(raw: bytes, size: int = 1) -> dict | None:
    """The app anchor as the host reads it: None while the program has not
    published one (no magic); otherwise its fields, with ``ptr_ok`` False
    when the pointer is 0, odd, or the *size*-byte struct it names does not
    lie wholly inside one guest RAM span (the host must not follow it then)."""
    if len(raw) < ANCHOR_SIZE or bytes(raw[:4]) != ANCHOR_MAGIC:
        return None
    ptr, seq, res = struct.unpack_from(">III", raw, 4)
    ok = (ptr != 0 and not ptr & 1 and size > 0
          and any(lo <= ptr and ptr + size <= hi for lo, hi in RAM_SPANS))
    return {"ptr": ptr, "seq": seq, "reserved": res, "ptr_ok": ok}
