"""The user's Workbench ADF: the checks, `adf add` and `adf ls`.

The user's file is only ever read.  Files added with `adf add` go onto the
REU copy of each session (host/adf_files.DiskEdit on an in-memory image;
only the changed blocks travel, host/adf_files.overlay_of), or into a NEW
local ADF with --out.  The limits are reported, never worked around:
the free blocks, the AmigaDOS name rules, no overwrite, and no deletion
(the only file the session removes is Classes/DataTypes/amigaguide.datatype,
from the REU copy, to make room for the ModPlayer: the Wave 16 ruling).

The tree is on sys.path (amiga64/__init__.py).
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field

from amiga64 import config as C
from host import adf_files as F
from host import adf_overlay as O

MODWB_DROP, MODWB_TOOL, MODWB_INFO, MODWB_MOD = F.MODWB_DROP, F.MODWB_TOOL, F.MODWB_INFO, F.MODWB_MOD
SCRIPT = "trim"                          # the session's Startup-Sequence overlay (WB_BOOT 4.3)
NAME_MAX = 30
# SHA-256 of the images tested end to end (Waves 13-16, both devices):
TESTED = {
    "24dbf267a7f7b01757025fa682a173cef4092a915eff36a5844b264c13fc729c":
        "Workbench 3.1 (amiga-os-310-workbench.adf), tested on the U64E and the C64U",
}


class AdfProblem(C.ConfigError):
    """The ADF cannot be used (plain message)."""


class AddError(C.ConfigError):
    """`adf add` refused (plain message, one per limit)."""


@dataclass
class AdfInfo:
    path: str
    sha256: str
    dostype: int
    ffs: bool
    free: int
    tested: bool
    note: str

    @property
    def verdict(self) -> str:
        return (f"tested: {self.note}" if self.tested else
                "untested (it passes the checks and may work; the tested image is "
                "Workbench 3.1, amiga-os-310-workbench.adf)")


def read(path: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        raise AdfProblem(f"{path}: no file there") from None
    except IsADirectoryError:
        raise AdfProblem(f"{path} is a directory, not an ADF") from None
    except OSError as e:
        raise AdfProblem(f"{path}: cannot read it ({e.strerror})") from None


def check(raw: bytes, path: str = "the ADF") -> AdfInfo:
    if len(raw) != O.ADF_SIZE:
        hd = " (an HD disk?)" if len(raw) == 2 * O.ADF_SIZE else ""
        raise AdfProblem(f"{path} is {len(raw):,} bytes{hd}; only DD disk images of "
                         f"{O.ADF_SIZE:,} bytes are supported (no HD disks, no "
                         f"compressed .adz or .dms files)")
    if raw[:3] != b"DOS":
        raise AdfProblem(f"{path} has no DOS boot block: not an AmigaDOS disk")
    if raw[3] > 3:
        what = " (a directory cache disk)" if raw[3] in (4, 5) else ""
        raise AdfProblem(f"{path} is DOS\\{raw[3]}{what}: only OFS and FFS disks (DOS\\0 to "
                         f"DOS\\3, no directory cache) are supported; copy the files to a "
                         f"DOS\\1 disk")
    try:
        O.Adf(raw)
        F.fsck(raw)
        free = F.DiskEdit(raw).free_count()
    except O.AdfError as e:
        raise AdfProblem(f"{path}: the file system is inconsistent ({e}); "
                         f"repair it on an Amiga or an emulator first") from None
    try:
        O.make_overlay(raw, SCRIPT)
    except O.AdfError as e:
        raise AdfProblem(f"{path} is not a bootable Workbench disk: S/Startup-Sequence "
                         f"({e})") from None
    sha = hashlib.sha256(raw).hexdigest()
    return AdfInfo(path, sha, raw[3], bool(raw[3] & 1), free, sha in TESTED, TESTED.get(sha, ""))


def inspect(path: str) -> AdfInfo:
    return check(read(path), path)


# --------------------------------------------------------------------------
# adf add
# --------------------------------------------------------------------------

@dataclass
class Request:
    """One `adf add` argument: a host file or directory, the drawer on the
    disk (`--to`, "" = the root) and the name (`--as`, default its own)."""
    host: str
    to: str = ""
    as_name: str | None = None

    def todict(self) -> dict:
        return {"host": self.host, "to": self.to, "as": self.as_name}


@dataclass
class Report:
    added: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    blocks: int = 0


def blocks_needed(size: int, ffs: bool) -> int:
    """Header + extension + data blocks for a file of *size* bytes (as
    DiskEdit.add_file allocates them)."""
    per = O.BLOCK if ffs else O.BLOCK - 24
    nd = -(-size // per)
    ne = max(0, -(-(nd - O.HT_SIZE) // O.HT_SIZE))
    return 1 + ne + nd


def check_name(name: str, where: str) -> None:
    if not 1 <= len(name) <= NAME_MAX:
        raise AddError(f"{where}: the name {name!r} is {len(name)} characters; AmigaDOS "
                       f"names are 1 to at most {NAME_MAX} characters. Use --as NAME")
    for c in ":/":
        if c in name:
            raise AddError(f"{where}: the name {name!r} contains {c!r}, which AmigaDOS "
                           f"names cannot. Use --as NAME")
    try:
        name.encode("latin-1")
    except UnicodeEncodeError:
        raise AddError(f"{where}: the name {name!r} is not Latin-1 (AmigaDOS names are "
                       f"Latin-1). Use --as NAME") from None


def _drawer(e: F.DiskEdit, to: str) -> str:
    to = to.strip("/")
    if not to:
        return ""
    k = e.adf.lookup(to)
    if k is None:
        raise AddError(f"--to {to}: no such drawer on the disk")
    if O._sl(e.adf.header(k), O.BLOCK - 4) not in (O.ST_ROOT, O.ST_USERDIR):
        raise AddError(f"--to {to}: not a drawer")
    return to


def _add_one(e: F.DiskEdit, host: str, dpath: str, rep: Report) -> None:
    try:
        st = os.lstat(host)
    except FileNotFoundError:
        raise AddError(f"{host}: no such file or directory") from None
    if stat.S_ISLNK(st.st_mode):
        raise AddError(f"{host} is a symbolic link: give the file it points to")
    if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        raise AddError(f"{host}: only plain files and directories can be added")
    check_name(dpath.rpartition("/")[2], host)
    if e.adf.lookup(dpath) is not None:
        raise AddError(f"{dpath} already exists on the disk; amiga64 never overwrites a "
                       f"file there. Use --as NAME")
    if stat.S_ISDIR(st.st_mode):
        need = 1
    else:
        with open(host, "rb") as f:
            data = f.read()
        need = blocks_needed(len(data), e.adf.ffs)
    free = e.free_count()
    if need > free:
        raise AddError(f"Not enough room: {dpath} needs {need} blocks, the disk has "
                       f"{free} free ({free * (O.BLOCK if e.adf.ffs else O.BLOCK - 24):,} "
                       f"bytes). amiga64 never deletes your files to make room")
    if stat.S_ISDIR(st.st_mode):
        e.mkdir(dpath)
        rep.added.append(dpath)
        for n in sorted(os.listdir(host)):
            if n.startswith("."):
                rep.skipped.append(os.path.join(host, n))
                continue
            _add_one(e, os.path.join(host, n), f"{dpath}/{n}", rep)
    else:
        e.add_file(dpath, data)
        rep.added.append(dpath)
    rep.blocks += need


def add(e: F.DiskEdit, reqs: list[Request]) -> Report:
    rep = Report()
    for r in reqs:
        to = _drawer(e, r.to)
        name = r.as_name or os.path.basename(os.path.normpath(r.host))
        _add_one(e, r.host, f"{to}/{name}" if to else name, rep)
    return rep


def session_image(raw: bytes, player: bytes, mod: bytes,
                  reqs: list[Request]) -> tuple[bytes, Report]:
    """The session's disk: *raw* with the trim Startup-Sequence, the
    ModPlayer files (aros_modwb.ModDisk's image) and the requested files."""
    image = O.apply(raw, O.make_overlay(raw, SCRIPT))
    image = F.modwb_image(image, player, mod)
    e = F.DiskEdit(image)
    rep = add(e, reqs)
    out = e.data()
    F.fsck(out)
    return out, rep


def write_out(src: str, reqs: list[Request], out: str) -> Report:
    """A new local ADF: the original *src* plus the files (no script
    overlay, no ModPlayer).  *out* must not exist."""
    if os.path.lexists(out):
        raise AddError(f"{out} already exists; --out writes a new file and never replaces one")
    raw = read(src)
    check(raw, src)
    e = F.DiskEdit(raw)
    rep = add(e, reqs)
    img = e.data()
    F.fsck(img)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(img)
    return rep


class Staged:
    """The files staged for every session: [[staged]] in the config file
    (supervisor ruling 2026-10-13)."""

    def __init__(self, path: str | None = None):
        self.path = path                # None: config.config_path()

    def requests(self) -> list[Request]:
        rows = C.load(self.path).get("staged") or []
        if not isinstance(rows, list):
            raise C.ConfigError("config: [[staged]] must be a list of tables")
        return [Request(r["host"], r.get("to") or "", r.get("as")) for r in rows]

    def _write(self, reqs: list[Request]) -> None:
        cfg = C.load(self.path)
        cfg["staged"] = [r.todict() for r in reqs]
        C.save(cfg, self.path)

    def add(self, raw: bytes, player: bytes, mod: bytes, new: list[Request]) -> Report:
        """Stage *new* after checking the whole session disk with it (a
        refused add stages nothing)."""
        new = [Request(os.path.abspath(r.host), r.to, r.as_name) for r in new]
        old = self.requests()
        _img, _ = session_image(raw, player, mod, old)
        e = F.DiskEdit(_img)
        rep = add(e, new)
        self._write(old + new)
        return rep

    @staticmethod
    def disk_path(r: Request) -> str:
        name = r.as_name or os.path.basename(os.path.normpath(r.host))
        return f"{r.to.strip('/')}/{name}" if r.to.strip("/") else name

    def remove(self, dpath: str) -> int:
        reqs = self.requests()
        keep = [r for r in reqs if self.disk_path(r).lower() != dpath.strip("/").lower()]
        self._write(keep)
        return len(reqs) - len(keep)

    def clear(self) -> None:
        self._write([])


# --------------------------------------------------------------------------
# adf ls
# --------------------------------------------------------------------------

def listing(image: bytes, drawer: str = "", staged: set[str] | None = None) -> list[str]:
    """`adf ls`: the drawer's entries (name, size or <drawer>), then the free
    blocks.  *staged*: lower-case disk paths to mark."""
    a = O.Adf(image)
    drawer = drawer.strip("/")
    dk = a.lookup(drawer) if drawer else O.ROOT
    if dk is None:
        raise AddError(f"{drawer}: no such drawer on the disk")
    h = a.header(dk)
    if O._sl(h, O.BLOCK - 4) not in (O.ST_ROOT, O.ST_USERDIR):
        raise AddError(f"{drawer}: not a drawer")
    rows = []
    for slot in range(O.HT_SIZE):
        k = O._l(h, 24 + 4 * slot)
        while k:
            e = a.header(k)
            n = a.bname(e)
            st = O._sl(e, O.BLOCK - 4)
            p = f"{drawer}/{n}" if drawer else n
            size = "<drawer>" if st == O.ST_USERDIR else str(a.file_size(k))
            mark = "  (staged)" if staged and p.lower() in staged else ""
            rows.append((n.lower(), f"  {n + ('/' if st == O.ST_USERDIR else ''):<32} {size:>8}{mark}"))
            k = O._l(e, O.BLOCK - 16)
    free = F.DiskEdit(image).free_count()
    per = O.BLOCK if a.ffs else O.BLOCK - 24
    return [r for _, r in sorted(rows)] + [f"{free} blocks free ({free * per:,} bytes)"]
