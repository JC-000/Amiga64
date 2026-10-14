"""The Ultimate, through c64-test-harness only: detection and the storage files.

Every device call runs inside one DeviceLock hold (tests/bench/u64_session
.u64_hold: the full config is recorded on entry and restored, compared, on
exit, and the C64 is reset).  Nothing here uses raw REST, curl or FTP, and
nothing touches the Ultimate menu.

Detection (docs/WAVE17_BRIEF.md rulings): the product name gives the kind,
the kind gives the default speed and volume; --kind, --mhz and --vol (or
the config) override them.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field

from amiga64 import config as C

KINDS = {                               # product name -> (kind, MHz, preferred volume)
    "Ultimate 64 Elite": ("U64E", 48, "USB1"),
    "C64 Ultimate": ("C64U", 64, "SD"),
}
TESTED_FW = {"U64E": ("3.15",), "C64U": ("1.1.0",)}
DIR = "amiga64"                          # <vol>/amiga64/ holds the ADF and the snapshot
SNAP = "wb-snap.bin"
TESTED_ADF_NAME = "amiga-os-310-workbench.adf"   # the name the tests use (tests/aros_wb.py)
REU_ITEM, CI_ITEM = "REU Size", "Command Interface"
DMA_ITEM, FTP_ITEM = "Ultimate DMA Service", "FTP File Service"
TEMP_MANAGED = re.compile(r"^temp[0-9a-fA-F]+$")      # firmware attachment files in /Temp
TEMP_KEEP = 1         # the harness's sweep keeps the newest (temp_gc DEFAULT_KEEP / $U64_TEMP_GC_KEEP)
SAMPLER_ITEM = "Map Ultimate Audio $DF20-DFFF"   # tests/paula/hw16.MAP_ITEM
LOCK_TIMEOUT_S = 300.0


class DeviceError(C.ConfigError):
    """A plain message about the device."""


@dataclass
class Detected:
    host: str
    product: str = ""
    kind: str = ""
    firmware: str = ""
    mhz: int = 0
    vol: str = ""
    volumes: list[str] = field(default_factory=list)
    reu16: bool | None = None
    ci: bool | None = None
    sampler: bool | None = None
    dma: bool | None = None                          # Network Settings: Ultimate DMA Service
    ftp: bool | None = None                          # Network Settings: FTP File Service
    files: dict = field(default_factory=dict)       # name -> size in <vol>/amiga64/
    adf_use: str | None = None                       # the device file with our ADF's bytes
    adf_differs: list[str] = field(default_factory=list)   # candidate names with other bytes
    temp_managed: list[str] = field(default_factory=list)  # /Temp: firmware-managed leftovers
    restored: bool | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def fw_tested(self) -> bool:
        return self.firmware in TESTED_FW.get(self.kind, ())


def adf_name(sha256: str) -> str:
    """The ADF's file name on the device (supervisor ruling 2026-10-13): it
    carries the image's hash, so two images never collide and nothing is
    ever overwritten."""
    return f"wb-{sha256[:12]}.adf"


def adf_candidates(sha256: str) -> list[str]:
    """The names a session may use, in order: our own, then the tests' name
    (reused only when its bytes are the same: the ruling's fallback, so the
    copy already on a device is not uploaded again)."""
    return [adf_name(sha256), TESTED_ADF_NAME]


def kind_of(product: str) -> tuple[str, int, str] | None:
    return KINDS.get(product)


def choose_volume(writable: list[str], preferred: str, override: str | None) -> str:
    names = {v.lower(): v for v in writable}
    if override:
        v = override.strip("/")
        if v.lower() in ("flash", "temp"):
            raise DeviceError(f"--vol /{v}: amiga64 never writes to /Flash or /Temp")
        if v.lower() not in names:
            raise DeviceError(f"--vol /{v}: not a writable volume on the device "
                              f"(it has: {', '.join('/' + x for x in writable) or 'none'})")
        return "/" + names[v.lower()]
    if preferred.lower() in names:
        return "/" + names[preferred.lower()]
    if writable:
        return "/" + writable[0]
    raise DeviceError("the device has no writable storage volume: insert a USB stick (U64E) "
                      "or a microSD card (C64U) and try again")


def set_env(host: str, password: str | None) -> None:
    """The harness reads the device from U64_HOST/U64_PASSWORD."""
    os.environ["U64_HOST"] = host
    if password:
        os.environ["U64_PASSWORD"] = password


def find_item(full: dict, item: str) -> str | None:
    return next((c for c, items in full.items() if item in (items or {})), None)


def enabled(full: dict, item: str) -> bool | None:
    cat = find_item(full, item)
    return None if cat is None else str(full[cat][item]).strip() == "Enabled"


def inspect_hold(hold: dict, det: Detected, storage, kind: str | None, mhz: int | None,
                 vol: str | None, adf_sha: str | None = None) -> Detected:
    """Fill *det* from an open hold (target, info, before).  *storage*: the
    harness's storage functions (storage_writable_volumes, storage_list_dir,
    storage_get_file): FTP, no /Temp cost.  *adf_sha*: the user's ADF, to
    find on the device (adf_candidates).  Every call here is a GET, a
    bodyless request or FTP: none leaves a file in /Temp."""
    info = hold["info"] or {}
    det.product = str(info.get("product", ""))
    det.firmware = str(info.get("firmware_version", ""))
    k = kind_of(det.product)
    if k is None and not kind:
        raise DeviceError(f"{det.host} is a {det.product or 'device of unknown type'}: amiga64 "
                          f"supports the Ultimate 64 Elite and the C64 Ultimate "
                          f"(--kind U64E|C64U to try anyway)")
    if kind:
        k = next(v for v in KINDS.values() if v[0] == kind)
    det.kind, dmhz, pvol = k
    det.mhz = mhz or dmhz
    full = hold.get("before") or {}
    client = hold["target"].transport.client
    cat = find_item(full, REU_ITEM)
    if cat:
        try:
            det.reu16 = "16 MB" in client.get_config_choices(cat, REU_ITEM)
        except Exception as e:          # noqa: BLE001 - reported, not fatal here
            det.notes.append(f"REU size choices unreadable: {e}")
    else:
        det.reu16 = False
    det.ci = find_item(full, CI_ITEM) is not None
    det.sampler = find_item(full, SAMPLER_ITEM) is not None
    det.dma, det.ftp = enabled(full, DMA_ITEM), enabled(full, FTP_ITEM)
    if det.ftp is False:
        raise DeviceError(f"the Ultimate at {det.host} has its FTP File Service off; amiga64 "
                          f"needs it for the disk files: turn on Network Settings / "
                          f"{FTP_ITEM} (amiga64 changes no network setting)")
    try:
        det.temp_managed = sorted(e.name for e in storage.storage_list_dir(det.host, "/Temp")
                                  if TEMP_MANAGED.match(e.name))
    except FileNotFoundError:
        det.temp_managed = []
    det.volumes = list(storage.storage_writable_volumes(det.host))
    det.vol = choose_volume(det.volumes, pvol, vol)
    d = f"{det.vol}/{DIR}"
    try:
        det.files = {e.name: e.size for e in storage.storage_list_dir(det.host, d)
                     if e.kind == "file"}
    except FileNotFoundError:
        det.files = {}
    for name in adf_candidates(adf_sha) if adf_sha else ():
        if name not in det.files:
            continue
        dev = storage.storage_get_file(det.host, f"{d}/{name}")
        if hashlib.sha256(dev).hexdigest() == adf_sha:
            det.adf_use = name
            break
        det.adf_differs.append(name)
    return det


def probe(host: str, out: str, kind: str | None = None, mhz: int | None = None,
          vol: str | None = None, adf_sha: str | None = None,
          hold_fn=None, storage=None) -> Detected:
    """One short hold: detect the device and read <vol>/amiga64/."""
    if hold_fn is None:
        from tests.bench.u64_session import u64_hold as hold_fn
    if storage is None:
        import c64_test_harness as storage
    det = Detected(host)
    os.makedirs(out, exist_ok=True)
    try:
        with hold_fn(out, lock_timeout=LOCK_TIMEOUT_S) as hold:
            inspect_hold(hold, det, storage, kind, mhz, vol, adf_sha)
    except DeviceError:
        raise
    except Exception as e:              # noqa: BLE001 - the plain message is the product
        raise DeviceError(explain(host, e)) from e
    det.restored = hold.get("restored")
    return det


def explain(host: str, e: BaseException) -> str:
    n = type(e).__name__
    s = str(e)
    if "DeviceLockTimeout" in n:
        return (f"the Ultimate at {host} is busy (another amiga64 or a test holds it) and "
                f"did not become free in {LOCK_TIMEOUT_S:.0f} s; try again later")
    if "FTP" in s and ("off" in s or "File Service" in s):
        return (f"the Ultimate at {host} has its FTP file service off; turn on "
                f"'FTP File Service' in the network settings: {s}")
    if isinstance(e, (ConnectionError, TimeoutError, OSError)) or "Connection" in n \
            or "Timeout" in n or "unreachable" in s.lower():
        return (f"cannot reach the Ultimate at {host} ({n}: {s}); check the address, the "
                f"network cable or Wi-Fi, and that the device is switched on")
    return f"the Ultimate at {host}: {n}: {s}"


def ensure_adf(host: str, vol: str, name: str, raw: bytes, storage=None) -> str:
    """The ADF on the device (inside a hold): uploaded when absent (never
    overwritten), else verified by SHA-256.  -> "uploaded" or "present"."""
    if storage is None:
        import c64_test_harness as storage
    path = f"{vol}/{DIR}/{name}"
    try:
        storage.storage_put_file(host, path, bytes(raw), overwrite=False, verify=True)
        return "uploaded"
    except FileExistsError:
        dev = storage.storage_get_file(host, path)
        if hashlib.sha256(dev).digest() != hashlib.sha256(raw).digest():
            raise DeviceError(f"{path} on the device is a different file than your ADF; "
                              f"amiga64 never overwrites it. Move it away on the device "
                              f"(or use another --vol)") from None
        return "present"


def hold_dir(what: str) -> str:
    return os.path.join(C.state_dir(), "holds", f"{time.strftime('%Y%m%d-%H%M%S')}-{what}")
