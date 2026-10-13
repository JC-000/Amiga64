"""W17-B: the prebuilt binaries (prebuilt/, made by `make prebuilt`).

A user without cc65 or vasm runs the shipped binaries: the runner and every
module a Workbench session assembles at run time.  The tests/ builders
(aros_boot.build_driver, aros_wb.wb_driver/build_td/build_inpm2,
run_spr.build_spr, run_paula.build_aud, aros_modwb.build_standin) call
``use(...)`` first and load from here when ``AMIGA64_PREBUILT=1`` or the
tool is missing; ``AMIGA64_PREBUILT=0`` always builds from source.

The 6502 modules are assembled against runner.lbl (-D of its labels), so a
prebuilt module is valid only with the prebuilt runner: ``module`` refuses
a labels file whose SHA-256 is not prebuilt/runner.lbl's.  Every file is
checked against prebuilt/MANIFEST on load, and ``verify`` re-derives the
runner's build ID (host/snapfmt) and every module ID (SHA-1[:4] of the
module with its ID fields zero, the builders' rule) as well.

MANIFEST lines: ``<sha256>  <file>`` (sha256sum format), ``#: build_id
<hex>``, ``#: id <module> <hex>``, ``#: tool <name> <version line>``;
other ``#`` lines are comments.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_dir() -> str:
    """The one place prebuilt/ is looked up: $AMIGA64_PREBUILT_DIR, else the
    prebuilt/ beside host/ (a checkout: <repo>/prebuilt; the wheel ships the
    tree as amiga64/_tree/, so there: amiga64/_tree/prebuilt)."""
    return os.environ.get("AMIGA64_PREBUILT_DIR") or os.path.join(ROOT, "prebuilt")


DIR = find_dir()       # read-only in prebuilt mode: nothing is ever written here
VBCC_BIN = os.path.expanduser("~/.local/opt/vbcc/bin")
VASM = os.path.join(VBCC_BIN, "vasmm68k_mot")
VLINK = os.path.join(VBCC_BIN, "vlink")

# name -> its files; the first is the binary.  Order = staging order.
ARTIFACTS: dict[str, tuple[str, ...]] = {
    "runner": ("runner.prg", "runner.lbl", "runner.map"),
    "mboot": ("mboot.bin", "mboot.lbl"),          # tests/aros/mboot.s ($C000)
    "wbboot": ("wbboot.bin", "wbboot.lbl"),       # mboot + jsr TD_ORG (aros_wb.wb_driver)
    "td": ("td.bin",),                            # tests/aros/td.s ($C200)
    "td_var": ("td_var.bin",),                    # td.s TD_VARIANT=$5A (leg resume's refusal)
    "spr": ("spr.bin", "spr.lbl"),                # tests/aros/spr.s ($C600)
    "inpm": ("inpm.a.bin", "inpm.b.bin"),         # tests/aros/inpm.s ($C980, $CF00)
    "aud": ("aud.a.bin", "aud.b.bin", "aud.lbl"),  # tests/paula/aud.s ($CA00, $0400)
    "modwb": ("modwb", "modwb.map"),              # fixtures/modwb: the ModPlayer
    "standin": ("modwb_standin",),                # tests/modwb/standin.s
    "standin_noprof": ("modwb_standin_noprof",),  # standin.s STANDIN_NOPROF
}
NEEDS_RUNNER = ("mboot", "wbboot", "td", "td_var", "spr", "inpm", "aud")   # need runner.lbl
# module -> the (file, offset) of its 4-byte ID fields; the ID is SHA-1[:4]
# of the files concatenated with every field zero
IDS: dict[str, tuple[tuple[str, int], ...]] = {
    "td": (("td.bin", 3),),
    "td_var": (("td_var.bin", 3),),
    "inpm": (("inpm.a.bin", 3), ("inpm.b.bin", 3)),
    "aud": (("aud.a.bin", 3), ("aud.b.bin", 0)),
}
# tool -> the command whose first *n* output lines are its version
TOOLS: dict[str, tuple[list[str], int]] = {
    "ca65": (["ca65", "--version"], 1),
    "ld65": (["ld65", "--version"], 1),
    "cl65": (["cl65", "--version"], 1),
    "vasmm68k_mot": ([VASM, "-v"], 3),           # vasm, its m68k cpu and mot syntax modules
    "vlink": ([VLINK, "-v"], 1),
}


class PrebuiltError(RuntimeError):
    """A prebuilt file is missing, altered or does not fit the runner (one
    line, naming the file: the CLI prints it as is)."""


FIX = "reinstall amiga64 (in a checkout: make prebuilt)"


def have(*tools: str) -> bool:
    """Every tool is runnable (a name on PATH or an executable path)."""
    return all(os.access(t, os.X_OK) if os.sep in t else shutil.which(t) for t in tools)


def wanted(*tools: str, lbl: str | None = None) -> bool:
    """Load from prebuilt/: AMIGA64_PREBUILT=1; or (unset) a tool is missing,
    or *lbl* (the runner's labels) is prebuilt/runner.lbl itself (a session
    on the prebuilt runner never builds into prebuilt/)."""
    v = os.environ.get("AMIGA64_PREBUILT", "")
    if v in ("0", "1"):
        return v == "1"
    if lbl is not None and os.path.realpath(lbl) == os.path.realpath(os.path.join(DIR, "runner.lbl")):
        return True
    return not have(*tools)


def use(tools: tuple[str, ...], name: str | None, why: str = "",
        lbl: str | None = None) -> bool:
    """A builder's switch: True = load *name* from prebuilt/; False = build.
    *name* None (no prebuilt file) or *why* (a variant: its defines, mutant
    or source) always builds; a missing tool then raises a plain error."""
    if name is not None and not why and wanted(*tools, lbl=lbl):
        return True
    if not have(*tools):
        what = why or name or "this build"
        raise PrebuiltError(f"{what}: no prebuilt binary for it here, and it needs "
                            f"{', '.join(os.path.basename(t) for t in tools)} (not found)")
    return False


def _sha(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def manifest(d: str | None = None) -> dict:
    """{"files": {file: sha256}, "ids": {module: hex}, "build_id": hex,
    "tools": {tool: version line}}."""
    path = os.path.join(d or DIR, "MANIFEST")
    try:
        lines = open(path).read().splitlines()
    except OSError as e:
        raise PrebuiltError(f"{path}: missing or unreadable ({e.strerror}); {FIX}") from e
    m: dict = {"files": {}, "ids": {}, "build_id": None, "tools": {}}
    for ln in lines:
        if ln.startswith("#: "):
            p = ln[3:].split(" ", 2)
            if p[0] == "build_id":
                m["build_id"] = p[1]
            elif p[0] == "id":
                m["ids"][p[1]] = p[2]
            elif p[0] == "tool":
                m["tools"][p[1]] = p[2]
        elif ln and not ln.startswith("#"):
            sha, f = ln.split("  ", 1)
            m["files"][f] = sha
    return m


def path(f: str) -> str:
    """The checked path of one prebuilt file."""
    p = os.path.join(DIR, f)
    want = manifest()["files"].get(f)
    if want is None:
        raise PrebuiltError(f"{p}: not listed in {os.path.join(DIR, 'MANIFEST')}; {FIX}")
    if not os.path.isfile(p):
        raise PrebuiltError(f"{p}: missing; {FIX}")
    if _sha(p) != want:
        raise PrebuiltError(f"{p}: SHA-256 differs from MANIFEST (altered); {FIX}")
    return p


def runner() -> tuple[str, str]:
    """(runner.prg, runner.lbl), checked."""
    return path("runner.prg"), path("runner.lbl")


def read_lbl(p: str) -> dict[str, int]:
    """An ld65 -Ln file as the builders return it (name -> address)."""
    lab = {}
    for line in open(p):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return lab


def labels(name: str) -> dict[str, int]:
    """A module's own labels (mboot, wbboot, spr, aud)."""
    lbl = [f for f in ARTIFACTS[name] if f.endswith(".lbl")]
    if not lbl:
        raise PrebuiltError(f"prebuilt module {name} has no labels file")
    return read_lbl(path(lbl[0]))


def check_labels(labels_path: str) -> None:
    """The runner the caller builds against must be the prebuilt one."""
    if _sha(labels_path) != manifest()["files"]["runner.lbl"]:
        raise PrebuiltError(
            f"{labels_path} is not {os.path.join(DIR, 'runner.lbl')}: the prebuilt modules fit only the "
            f"prebuilt runner (use --prg {path('runner.prg')}, or install cc65 to build)")


def module(name: str, labels_path: str | None = None):
    """The binary of *name* (bytes), or its segments (inpm, aud: a tuple).
    A 6502 module needs *labels_path* = the runner it is staged with."""
    if name in NEEDS_RUNNER:
        if labels_path is None:
            raise PrebuiltError(f"prebuilt module {name}: the runner's labels are required")
        check_labels(labels_path)
    bins = [f for f in ARTIFACTS[name] if not f.endswith((".lbl", ".map"))]
    out = []
    for f in bins:
        with open(path(f), "rb") as fh:
            out.append(fh.read())
    return out[0] if len(out) == 1 else tuple(out)


def module_id(blobs: dict[str, bytes], fields) -> list[str]:
    """[the derived ID, then each stamped field] (hex)."""
    z = []
    for f, off in fields:
        b = bytearray(blobs[f])
        b[off:off + 4] = bytes(4)
        z.append(bytes(b))
    got = [blobs[f][off:off + 4].hex() for f, off in fields]
    return [hashlib.sha1(b"".join(z)).digest()[:4].hex()] + got


def tool_version(tool: str) -> str | None:
    cmd, n = TOOLS[tool]
    if not have(cmd[0]):
        return None
    r = subprocess.run(cmd, capture_output=True, text=True)
    lines = [x.strip() for x in (r.stdout + r.stderr).strip().splitlines()]
    return " / ".join(lines[:n]) or None


def versions() -> dict[str, str | None]:
    return {t: tool_version(t) for t in TOOLS}


def verify(d: str | None = None) -> list[str]:
    """Every problem with prebuilt/ (empty: all good)."""
    from host import snapfmt
    d = d or DIR
    try:
        m = manifest(d)
    except PrebuiltError as e:
        return [str(e)]
    probs, blobs = [], {}
    want = {f for files in ARTIFACTS.values() for f in files}
    for f in sorted(want - set(m["files"])):
        probs.append(f"{f}: not in MANIFEST")
    for f, sha in m["files"].items():
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            probs.append(f"{f}: missing")
            continue
        blobs[f] = open(p, "rb").read()
        if hashlib.sha256(blobs[f]).hexdigest() != sha:
            probs.append(f"{f}: SHA-256 differs from MANIFEST")
    for name, fields in IDS.items():
        if not all(f in blobs for f, _ in fields):
            continue
        derived, *got = module_id(blobs, fields)
        if any(g != derived for g in got):
            probs.append(f"{name}: stamped ID {got} != derived ID {derived}")
        if m["ids"].get(name) != derived:
            probs.append(f"{name}: MANIFEST ID {m['ids'].get(name)} != derived ID {derived}")
    if "runner.prg" in blobs and "runner.lbl" in blobs:
        lab = snapfmt.labels(os.path.join(d, "runner.lbl"))
        bid = snapfmt.build_id(blobs["runner.prg"], lab["snap_bid"]).hex()
        got = snapfmt.read_bid(os.path.join(d, "runner.prg"), os.path.join(d, "runner.lbl")).hex()
        if not got == bid == m["build_id"]:
            probs.append(f"runner: build ID stamped {got}, derived {bid}, MANIFEST {m['build_id']}")
    return probs
