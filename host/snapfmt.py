"""Suspend/resume snapshot format (W12-T, I3) and the PRG build-ID stamp.

The C64 side is runtime/ops/snap.s; docs/CONTRACTS.md "Suspend/resume"
is the contract.  This module mirrors the format constants (snap.s asserts
nothing about Python, so host/tests/test_snap_fmt.py checks the mirror
against the assembled labels) and provides:

  * ``stamp(prg, lbl)``: the build ID.  ``snap_bid`` (8 bytes in the PRG)
    := the first 8 bytes of SHA-256 over the PRG file with those 8 bytes
    zeroed.  Deterministic (a byte-identical link gives the same ID) and
    idempotent.  The Makefile runs it after every runner link; a PRG
    linked any other way keeps an all-zero ID, and snap.s refuses to
    resume on an all-zero ID (SNAP_E_NOID).
  * ``parse_header(b)`` / ``build_header(...)``: the 256-byte header.
  * ``fletcher``: the checksum (host/machine.fletcher, the same as snap.s).

Snapshot area in the REU (rc: runtime/rc/guest.inc / machine.inc ``snap``
region, the REU under the trapped custom-chip bank, never guest-visible):

  +$0000  header, 256 bytes (layout below)
  +$0100  zero page $00-$FF as it was (resume restores $10-$7F, $88-$99)
  +$0200  disp_st page ($B800-$B8FF, RAM under BASIC)
  +$2FE0  the declared ROM id (32 bytes; in no file span: the resuming
          host declares its own)
  +$0300  MAIN BSS image, __BSS_RUN__ .. snap_keep (KEEP bytes; stashed,
          summed and filed as KEEPR = KEEP rounded up to whole pages,
          restored as exactly KEEP)

The file (UCI SAVE_REU, one open file): the spans of the header in order:
the header, the state (+$0100, $200 + KEEPR), then ``RANGES``.

Header (all little-endian):
  0   magic "A64S"
  4   format version (VERSION)
  5   number of spans (6)
  6   KEEP (u16): BSS image bytes
  8   __BSS_RUN__ (u16): its C64 address
  10  spans: per span REU address (u24) + length (u32); bytes 0-51 are a
      constant of the build (snap.s snap_tpl)
  52  flags: RQ_FILE when the suspend also wrote the file
  56  build ID (8 bytes, the suspending PRG's snap_bid)
  64  ROM id (32 bytes: the SHA-256 the host declared at SNAP_BASE+OFF_RID)
  96  fletcher of the state span (u32)
  100 zeros
  252 fletcher of bytes 0..251 (u32)
Resume compares 0..251 against the header it would write itself, except
the flags and the state sum (refusal codes by the first differing offset:
0-4 magic/version, 5-55 layout, 56-63 build ID, 64-95 ROM id).
"""

from __future__ import annotations

import argparse
import hashlib
import struct
import sys

VERSION = 1
MAGIC = b"A64S"
SNAP_BASE = 0xDF1000            # rc snap region ($DF1000-$DF3FFF)
SNAP_END = 0xDF3FFF
OFF_ZP, OFF_DST, OFF_BSS = 0x100, 0x200, 0x300
OFF_RID = 0x2FE0                # the ROM id the host/stager declares (32 bytes)
KEEP_MAX = SNAP_END + 1 - SNAP_BASE - OFF_BSS
H_NSP, H_FLAGS, H_BID, H_RID, H_SSUM, HDR_SUM = 5, 52, 56, 64, 96, 252
# The guest REU ranges a file carries (runtime/rc/machine.inc): chip RAM,
# slow RAM together with the unmapped $DC0000-$DEFFFF above it (AROS writes
# there: not zero after a boot to m3, measured on both Ultimates,
# tests/aros_resume.zero_check), the extension and Kickstart ROM banks.  The
# rest of the machine profile's unmapped space stays zero (checked by the
# same leg); the LUT at disp_lut is rebuilt by disp_init on resume.
RANGES = ((0x000000, 0x200000), (0xC00000, 0x1F0000),
          (0xE00000, 0x080000), (0xF80000, 0x080000))
BID_LEN = 8

# snap_req bits (host -> C64) and snap_st codes (C64 -> host), = snap.s
RQ_SUS, RQ_FILE, RQ_CONT = 0x01, 0x40, 0x80
ST_SUS, ST_SUSF, ST_RES = 0x10, 0x11, 0x20
ST_ERR = {0xE1: "magic/version", 0xE2: "header checksum", 0xE3: "build ID",
          0xE4: "ROM id", 0xE5: "state checksum", 0xE6: "file open",
          0xE7: "file transfer", 0xE8: "unstamped build (build ID 0)",
          0xE9: "layout (KEEP / BSS address / ranges)"}


def fletcher(data: bytes, a: int = 0, b: int = 0) -> int:
    """(b << 16) | a with a = sum of bytes, b = sum of the running a's, both
    mod 2^16: host/machine.fletcher, also what snap.s computes."""
    for x in data:
        a = (a + x) & 0xFFFF
        b = (b + a) & 0xFFFF
    return (b << 16) | a


def keepr(keep: int) -> int:
    return (keep + 0xFF) & ~0xFF


def spans(keep: int) -> list[tuple[int, int]]:
    """(REU address, length) of every SAVE_REU/LOAD_REU span, file order."""
    return [(SNAP_BASE, 0x100), (SNAP_BASE + OFF_ZP, 0x200 + keepr(keep))] \
        + list(RANGES)


def build_header(bid: bytes, rid: bytes, keep: int, bss_run: int,
                 state_sum: int, flags: int = 0) -> bytes:
    sp = spans(keep)
    h = bytearray(256)
    h[0:4] = MAGIC
    h[4], h[5] = VERSION, len(sp)
    struct.pack_into("<HH", h, 6, keep, bss_run)
    o = 10
    for a, n in sp:
        h[o:o + 3] = a.to_bytes(3, "little")
        struct.pack_into("<I", h, o + 3, n)
        o += 7
    assert o == H_FLAGS
    h[H_FLAGS] = flags
    h[H_BID:H_BID + 8] = bid
    h[H_RID:H_RID + 32] = rid
    struct.pack_into("<I", h, H_SSUM, state_sum)
    struct.pack_into("<I", h, HDR_SUM, fletcher(h[:HDR_SUM]))
    return bytes(h)


def parse_header(h: bytes) -> dict:
    keep, bss_run = struct.unpack_from("<HH", h, 6)
    sp = [(int.from_bytes(h[10 + 7 * i:13 + 7 * i], "little"),
           struct.unpack_from("<I", h, 13 + 7 * i)[0]) for i in range(h[5])]
    hs = struct.unpack_from("<I", h, HDR_SUM)[0]
    return {"magic": bytes(h[0:4]), "version": h[4], "spans": sp,
            "keep": keep, "bss_run": bss_run, "flags": h[H_FLAGS],
            "bid": bytes(h[H_BID:H_BID + 8]), "rid": bytes(h[H_RID:H_RID + 32]),
            "state_sum": struct.unpack_from("<I", h, H_SSUM)[0],
            "sum": hs, "sum_ok": hs == fletcher(h[:HDR_SUM])}


def labels(lbl_path: str) -> dict[str, int]:
    out = {}
    for line in open(lbl_path):
        p = line.split()
        if len(p) == 3 and p[0] == "al":
            out[p[2].lstrip(".")] = int(p[1], 16)
    return out


def prg_offset(prg: bytes, addr: int) -> int:
    load = prg[0] | prg[1] << 8
    off = addr - load + 2
    if not 2 <= off <= len(prg) - BID_LEN:
        raise ValueError(f"${addr:04X} is outside the PRG (load ${load:04X})")
    return off


def build_id(prg: bytes, bid_addr: int) -> bytes:
    o = prg_offset(prg, bid_addr)
    z = prg[:o] + bytes(BID_LEN) + prg[o + BID_LEN:]
    return hashlib.sha256(z).digest()[:BID_LEN]


def stamp(prg_path: str, lbl_path: str, as_id: bytes | None = None) -> bytes:
    """Write the build ID into *prg_path* (or *as_id*: test builds that must
    accept another build's snapshot, tests/aros_resume.py mutants)."""
    lab = labels(lbl_path)
    if "snap_bid" not in lab:
        raise KeyError(f"{lbl_path}: no snap_bid label")
    prg = bytearray(open(prg_path, "rb").read())
    bid = as_id if as_id is not None else build_id(bytes(prg), lab["snap_bid"])
    o = prg_offset(bytes(prg), lab["snap_bid"])
    prg[o:o + BID_LEN] = bid
    with open(prg_path, "wb") as f:
        f.write(prg)
    return bid


def read_bid(prg_path: str, lbl_path: str) -> bytes:
    prg = open(prg_path, "rb").read()
    o = prg_offset(prg, labels(lbl_path)["snap_bid"])
    return prg[o:o + BID_LEN]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stamp", nargs=2, metavar=("PRG", "LBL"), required=True)
    ap.add_argument("--as-id", help="hex build ID to write instead (tests)")
    a = ap.parse_args(argv)
    bid = stamp(*a.stamp, bytes.fromhex(a.as_id) if a.as_id else None)
    print(f"snap build ID {bid.hex()} -> {a.stamp[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
