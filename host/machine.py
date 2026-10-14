"""Machine-mode guest profile and AROS ROM loader (S3.7, W11-A).

The host side of ``runtime/rc/machine.inc`` (docs/CONTRACTS.md "Machine
profile and AROS boot (S3.7)"): the guest map the AROS Kickstart
replacement boots on, and the plan that stages it into the REU.

The ROM banks are read from the bundle in the repository (W17-K:
``third_party/aros/``, the pinned binaries with their APL 1.1 LICENSE and
SOURCE.txt) or, failing that, from the verified W6-O cache
(``~/.cache/amiga64/aros/<sha>/``, tools/aros/fetch.sh); both are pinned by
tools/aros/manifest.txt.  Nothing here writes ROM bytes anywhere.
``find_rom`` fails closed (a missing or mismatching file is never used) and
reports why, so a caller can print a SKIP line and exit 0, as
tests/aros_oracle.py does.

The staging plan (``stage_plan``), in order:
  1. zero the whole 16 MB REU (``ZERO_SPANS``): every byte the ROM probes
     outside RAM and ROM reads 0, as in the boot model (16 MB of zeroed
     flat RAM); on the U64E the REU otherwise holds the previous user's data;
  2. aros-ext.bin at AROS_EXT, aros-rom.bin at AROS_ROM;
  3. the overlay: the ROM's first 8 bytes (reset SSP, reset PC) at
     AROS_CHIP, exactly what the boot model does (``mem.w_block(0,
     rom[:8])``).  The boot entry loads SSP/PC from those longs.  The
     overlay itself (ROM mirrored at 0 until CIA-A PRA bit 0 clears) is not
     modelled: AROS reads the vectors once, at reset, and turns OVL off
     before it touches low memory (S3_PLAN §3.2, model row 562,232).
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass

# --- Profile (mirrors runtime/rc/machine.inc; host/rescheck.py checks) -------
AROS_CHIP = 0x000000
AROS_CHIP_END = 0x1FFFFF
TD_ADF = 0x200000                    # W13-W: the staged DF0 disk (rc machine.inc)
TD_ADF_END = 0x2DBFFF
AROS_SLOW = 0xC00000
AROS_SLOW_END = 0xDBFFFF
AROS_EXT = 0xE00000
AROS_EXT_END = 0xE7FFFF
AROS_ROM = 0xF80000
AROS_ROM_END = 0xFFFFFF

REU_SIZE = 0x1000000
BANK_SIZE = 0x80000
ZERO_SPANS = ((0x000000, REU_SIZE),)        # (start, length): everything

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(ROOT, "tools", "aros", "manifest.txt")
CACHE_ROOT = os.environ.get("AMIGA64_AROS_CACHE",
                            os.path.expanduser("~/.cache/amiga64/aros"))
ROM_FILES = (("aros-ext.bin", AROS_EXT), ("aros-rom.bin", AROS_ROM))
BUNDLE = os.path.join(ROOT, "third_party", "aros")   # W17-K: the bundled ROM
CACHE_FIX = ("run tools/aros/fetch.sh", "delete the directory, re-run tools/aros/fetch.sh")
BUNDLE_FIX = ("restore it: git checkout -- third_party/aros",) * 2    # W17-K2: in a checkout


def is_checkout() -> bool:
    """W17-K2: a clone has tools/aros/fetch.sh; an installed amiga64 does not
    (so its messages never send the user to fetch.sh or into the bundle)."""
    return os.path.isfile(os.path.join(ROOT, "tools", "aros", "fetch.sh"))


@dataclass(frozen=True)
class Rom:
    """The verified ROM banks of one cache directory."""
    cache: str
    ext: bytes
    rom: bytes

    @property
    def ssp(self) -> int:
        return struct.unpack_from(">I", self.rom, 0)[0]

    @property
    def pc(self) -> int:
        return struct.unpack_from(">I", self.rom, 4)[0]


def _manifest(path: str) -> tuple[str, dict[str, str]]:
    """(archive sha256, {cache file: sha256}) from tools/aros/manifest.txt."""
    arch, files = None, {}
    with open(path) as f:
        for line in f:
            parts = line.strip().split("|")
            if parts[0] == "archive" and len(parts) == 3:
                arch = parts[2]
            elif parts[0] == "file" and len(parts) == 4:
                files[parts[1]] = parts[3]
    if arch is None or not files:
        raise ValueError(f"bad manifest {path}")
    return arch, files


def _verified(d: str, files: dict[str, str], manifest: str,
              fix: tuple[str, str] | None = CACHE_FIX) -> tuple[Rom | None, str]:
    """(Rom, "") when both banks in *d* verify against *files*, else (None, why).
    *fix*: the advice for a missing / a mismatching file (None: none)."""
    missing, bad = (f" ({x})" for x in fix) if fix else ("", "")
    data = {}
    for name, _addr in ROM_FILES:
        p = os.path.join(d, name)
        if name not in files:
            return None, f"{name} not pinned in {manifest}"
        if not os.path.isfile(p):
            return None, f"{name} missing in {d}{missing}"
        with open(p, "rb") as f:
            b = f.read()
        if hashlib.sha256(b).hexdigest() != files[name]:
            return None, f"{name} SHA256 mismatch in {d}: refusing to use it{bad}"
        if len(b) != BANK_SIZE:
            return None, f"{name} is {len(b)} bytes, expected {BANK_SIZE}"
        data[name] = b
    return Rom(d, data["aros-ext.bin"], data["aros-rom.bin"]), ""


def find_rom(manifest: str = MANIFEST, root: str = CACHE_ROOT, bundle: str = BUNDLE,
             checkout: bool | None = None) -> tuple[Rom | None, str]:
    """(Rom, "") when both banks verify, else (None, why).  W17-K: the
    bundle (*bundle*) first, then the cache under *root*; a bundle file that
    does not match the manifest is never used (the cache is tried).
    W17-K2: outside a checkout (*checkout*, default is_checkout()) a failure
    is a broken install, and the advice is to reinstall."""
    if checkout is None:
        checkout = is_checkout()
    try:
        arch, files = _manifest(manifest)
    except (OSError, ValueError) as e:
        return None, f"manifest unreadable: {e}"
    bwhy = f"no bundle at {bundle}"
    if os.path.isdir(bundle):                           # W17-K
        rom, bwhy = _verified(bundle, files, manifest,  # W17-K
                              BUNDLE_FIX if checkout else None)
        if rom is not None:
            return rom, ""
    d = os.path.join(root, arch)
    if os.path.isdir(d):
        rom, why = _verified(d, files, manifest)
        if rom is not None:
            return rom, ""
    else:
        why = f"no AROS cache at {d}" + (f" ({CACHE_FIX[0]})" if checkout else "")
    if not checkout:                                    # W17-K2
        return None, f"broken install: the bundled AROS ROM: {bwhy}; reinstall amiga64"
    return None, f"{why}; bundle: {bwhy}"


def stage_plan(rom: Rom) -> list[tuple[int, bytes]]:
    """The (guest address, bytes) writes after the zero fill, in order."""
    return [(AROS_EXT, rom.ext), (AROS_ROM, rom.rom),
            (AROS_CHIP, rom.rom[:8])]


def fletcher(data: bytes) -> int:
    """The checksum tests/aros/mboot.s ``aros_sum`` computes over a REU span
    (a readback check, not a cryptographic one): a = sum of the bytes, b =
    sum of the running a's, both mod 2^16; returns (b << 16) | a.  A byte
    error or a swap of two bytes changes b."""
    a = b = 0
    for x in data:
        a = (a + x) & 0xFFFF
        b = (b + a) & 0xFFFF
    return (b << 16) | a
