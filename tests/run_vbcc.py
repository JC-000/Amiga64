"""M2 exit bar: compile C programs with vbcc, run them in the interpreter.

Usage:
    python3 -m tests.run_vbcc --prg build/ws-b1/runner.prg
        [--only NAME[,NAME]] [--no-units] [--budget N] [--timeout SECS]
        [--no-poison] [--backend auto|vice|u64]

Two legs in one VICE session: first the HLE unit programs
(``fixtures/c/hle_unit/*.s``, assembled with vasm at test time — one
register-level test per HLE call, expectations in :func:`units`), then the
vbcc-compiled C programs (:func:`programs`).

Pipeline (docs/HLE.md "vbcc programs"): each ``fixtures/c/<name>.c`` is
compiled at test time with vbcc (``vc +aos68k``, toolchain installed by
``tools/vbcc/install.sh`` outside the repo; the compiled executables embed
vbcc's startup code + vc.lib and are therefore never committed — they land
in ``build/vbcc/``) -> ``host/hunk.py`` -> ``host/image.py`` ->
``host/hle_env.env_chunks()`` (fake ExecBase/DOSBase/Process/CLI, console
FileHandles, HLE state) -> staged through ``test_load_chunk`` ->
``test_run_program`` enters at the env's CLI launch stub (A0 = argument
string, D0 = its length — the AmigaOS process entry ABI) -> the vbcc
startup runs main() and returns its exit code in D0 to the $AFFF exit stub.

Checks per program: halted HALT_OK (not a trap / not a timeout), D0 = the
expected exit code (the program's return value, docs/HLE.md "Program
exit"), and the C64 screen rows spell exactly the expected output.

Adversarial leg (default on, ``--no-poison`` disables): before each run the
first POISON_LEN bytes of the HLE heap are filled with $A5 so MEMF_CLEAR
and "fresh memory is zero" assumptions cannot pass by accident, whatever
the REU happened to contain (its power-up contents are not specified, and
earlier runs in the same session leave data behind).

When the PRG exports ``hle_dos_init`` (WS-B2's file-handle pool), it is
called before every program (``--no-dos-init`` disables, to demonstrate
the leak test's teeth).  Programs marked ``needs_dos`` are skipped while
``hle_dos.s`` is the supervisor stub.

RAMDISK (m2-post): a program or unit with ``files`` gets a freshly
formatted RAMDISK holding them (``hle_dos_env.ramdisk_chunks``) staged with
it; ``expect_files`` reads the RAMDISK back afterwards
(``hle_dos_env.parse_directory`` over ``tests.run_dos.Guest``) and compares
contents (``None`` = the file must not exist).

A HALT_ALINE inside the env's library vector tables is decoded to the
library/LVO that the program called but the HLE does not implement.

Exit code 0 = all checks passed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass

from host import hle_dos_env, hle_env, hunk, image
from host.image import Chunk
from tests.run_program import HALT_OK, Session

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(PROJECT_ROOT, "fixtures", "c")
DEFAULT_VBCC = os.path.expanduser("~/.local/opt/vbcc")

SCREEN = 0x0400
SCREEN_COLS = 40
SCREEN_ROWS = 25
SPACE = 0x20

HALT_REASONS = {0x00: "HALT_OK", 0x01: "HALT_ALINE", 0x02: "HALT_FLINE",
                0x03: "HALT_ILLEGAL"}

HEAP_BASE = getattr(hle_env, "HEAP_BASE", 0x100000)
POISON_LEN = 0x2000
POISON_BYTE = 0xA5

# Names for decoding an unimplemented-LVO halt (diagnostics only).
LVO_NAMES = {
    "exec.library": {
        -30: "Supervisor", -108: "Alert", -120: "Disable", -126: "Enable",
        -132: "Forbid", -138: "Permit", -186: "Allocate", -192: "Deallocate",
        -198: "AllocMem", -204: "AllocAbs", -210: "FreeMem", -216: "AvailMem",
        -240: "AddHead", -246: "AddTail", -252: "Remove", -258: "RemHead",
        -276: "FindName", -294: "FindTask", -306: "SetSignal", -318: "Wait",
        -366: "PutMsg", -372: "GetMsg", -378: "ReplyMsg", -384: "WaitPort",
        -408: "OldOpenLibrary", -414: "CloseLibrary", -552: "OpenLibrary",
        -684: "AllocVec", -690: "FreeVec", -696: "CreatePool",
        -702: "DeletePool", -708: "AllocPooled", -714: "FreePooled",
        -732: "StackSwap",
    },
    "dos.library": {
        -30: "Open", -36: "Close", -42: "Read", -48: "Write", -54: "Input",
        -60: "Output", -66: "Seek", -72: "DeleteFile", -84: "Lock",
        -90: "UnLock", -126: "CurrentDir", -132: "IoErr", -216: "IsInteractive",
        -192: "DateStamp", -450: "ChangeMode", -462: "SetIoErr",
        -606: "SystemTagList", -810: "ReadItem", -906: "GetVar",
        -948: "PutStr",
    },
}


# --- expected results --------------------------------------------------------

def _alloc_sum() -> int:
    """Mirror of fixtures/c/alloc.c's checksum over all verified bytes
    (same step() walk: full for blocks <= 4096, stride 61 + 256-byte ends
    for the 70 000-byte block)."""
    sizes = (16, 100, 1000, 4096, 70000)
    total = 0
    for b, n in enumerate(sizes):
        i = 0
        while i < n:
            total += (b * 37 + i * 7 + (i >> 8)) & 0xFF
            if n <= 4096 or i < 256 or i >= n - 257:
                i += 1
            else:
                i += 61
                i = i if i < n - 256 else n - 256
    return total


@dataclass
class Program:
    name: str
    exit_code: int | None     # D0 at HALT_OK (None: halts elsewhere)
    lines: list[str]          # expected screen rows from row 0 (rest blank)
    reason: int = HALT_OK
    where: str | None = None  # decoded unimplemented-LVO slot (HALT_ALINE)
    needs_dos: bool = False   # needs WS-B2's real hle_dos.s (skip on stub)
    # RAMDISK: files staged before the run (a freshly formatted RAMDISK;
    # None = leave the RAMDISK as the previous program left it) and files
    # read back after it (value None = the file must NOT exist).
    files: dict[str, bytes] | list | None = None   # list: hle_dos_env.RdFile
    expect_files: dict[str, bytes | None] | None = None
    budget: int | None = None  # per-program instruction cap (overrides --budget)
    expect_addr: dict[str, int] | None = None   # RAMDISK extent address
    pools: bool = False       # needs the W5-H hle.s pool wiring (skip without)


def _scroll_line(n: int) -> str:
    return f"line {n:02d}" + "*" * ((n * 3) % 7)


def programs() -> dict[str, Program]:
    progs = [
        Program("hello", 0, ["Hello from vbcc!", "2 + 40 = 42"]),
        Program("exit", 7, ["exit(7) via stdio"]),
        # 30 lines of varying length through 25 rows: rows show 6..30.
        Program("scroll", 0, [_scroll_line(n) for n in range(6, 31)]),
        Program("alloc", 0, [f"alloc ok {_alloc_sum()}"]),
        Program("sysbase", 0, [
            f"ThisTask={hle_env.ENV_PROCESS:x}", "AttnFlags=0",
            f"MaxLocMem={0x200000:x}", "exec ver=40", "dos ver=40",
            "dos v40 ok", "dos v41 NULL", "dos v50 NULL", "dos.fake NULL",
            "utility NULL", "LibList empty"]),
        Program("getenv", 0, ["HOME=NULL", "after getenv"]),
        Program("time", 0, ["time=252460800", "same=1"]),
        Program("system", 0, ["system(NULL)=1", "system(cmd)=-1"]),
        Program("readitem", None, [], reason=0x01,
                where="dos.library/ReadItem (LVO -810)"),
        Program("putstr", None, ["before"], reason=0x01,
                where="dos.library/PutStr (LVO -948)"),
        Program("bigstack", 0, ["stack swapped"]),
        # handle-state leak across programs (b1-review F2): run in order
        Program("leak_open", 0, ["opened 8"], needs_dos=True),
        Program("leak_check", 0, ["open ok"], needs_dos=True),
        # --- stdio file I/O end to end on the RAMDISK (m2-post) ---------------
        Program("fio_rw", 0, ["fwrite=11", "fgetc=h fread=10", "eof=1",
                              "missing=NULL", "ram=via RAM:"],
                needs_dos=True, files={},
                expect_files={"t.txt": b"hello file\n", "ram.txt": b"via RAM:",
                              "RAM:ram.txt": None, "nosuch.txt": None}),
        Program("fio_seek", 0, ["end=20", "at5=5 tell=6", "cur+4=A",
                                "last=J tell=20", "first=0"],
                needs_dos=True, files={"in.txt": SEEK_DATA},
                expect_files={"in.txt": SEEK_DATA}),
        Program("fio_exit", 0, ["exit without fclose"], needs_dos=True,
                files={}, expect_files={"unclosed.txt": b"left open 42\n"}),
        Program("fio_fmt", 0, ["n=4 a=-123 w=word", "l=70000 b=7"],
                needs_dos=True, files={},
                expect_files={"fmt.txt": b"-123 word 70000\n7\n"}),
        # buffer across guest $180000 AND big.bin's extent across REU
        # $E80000 (staged pad file): the RAMDISK-side DMA must be split
        Program("fio_big", 0, ["straddle=1 odd=1", "read 3000 bad=0 eof=1"],
                needs_dos=True, files=BIG_FILES,
                expect_files={"big.bin": _big_data()},
                expect_addr={"big.bin": BIG_EXTENT}),
        Program("fio_append", 0, [
            "log.txt=one|two|", "new.txt=first|", "rp.txt=abXYefgh",
            "r+ missing=NULL", "r+ while shared=NULL", "a while shared=NULL",
            "r while append=NULL", "r after close=open"],
            needs_dos=True, files={"log.txt": b"one\n", "rp.txt": b"abcdefgh"},
            expect_files={"log.txt": b"one\ntwo\n", "new.txt": b"first\n",
                          "rp.txt": b"abXYefgh", "nosuch": None}),
        Program("fio_remove", 0, [
            "rm=0", "rm again=1 ioerr=205", "reopen=NULL",
            "rm open=1 ioerr=202", "rm RAM:=0"],
            needs_dos=True, files={"gone.txt": b"bye", "keep.txt": b"keep"},
            expect_files={"gone.txt": None, "keep.txt": b"keep",
                          "tmp.txt": None}),
        Program("fio_reuse", 0, ["created=32", "rm=0", "again=open",
                                 "more=NULL"],
                needs_dos=True, files={},
                expect_files={"f05": None, "again": b"reused", "f04": b"",
                              "f31": b"", "more": None}),
        Program("tspin", 0, ["spins=48 delta=1", "clock=-1"],
                budget=3_000_000),
        # --- W5-H heap reuse (docs/HLE.md "Heap") ------------------------------
        Program("heap_churn", 0, ["churn ok", "total>2x heap=1",
                                  "live<2MB=1 frees>50=1"]),
        Program("heap_coalesce", 0, ["coalesce", "fragmented 2MB=NULL",
                                     "merged 3MB=fit", "all freed 7MB=fit"]),
        Program("heap_pool", 0, ["pools ok 40 rounds"], pools=True),
        Program("heap_realloc", 0, ["realloc ok 30 rounds"]),
    ]
    return {p.name: p for p in progs}


SEEK_DATA = b"0123456789ABCDEFGHIJ"
# fio_big: big.bin's extent starts $200 below the 512 KB REU line $E80000,
# inside the first 1 KB stdio flush, so a flush from a buffer that is not
# page aligned ("odd=1") has one chunk whose REU range crosses the line.
BIG_EXTENT = 0xE80000 - 0x200
BIG_FILES = [hle_dos_env.RdFile("pad", b"",
                                capacity=BIG_EXTENT - hle_dos_env.RD_DATA),
             hle_dos_env.RdFile("big.bin", b"")]


def _big_data() -> bytes:
    """Mirror of fixtures/c/fio_big.c pat() over 3000 bytes."""
    return bytes((i * 7 + (i >> 8) + 1) & 0xFF for i in range(3000))


# --- HLE unit programs (fixtures/c/hle_unit/*.s, vasm) -------------------------

UNIT_DIR = os.path.join(FIXTURE_DIR, "hle_unit")
H = HEAP_BASE
# W5-H allocator: HEAP_CTL (8 bytes) opens the heap and every block has an
# 8-byte header, so the first payload is H + $10 and a request of n bytes
# takes round8(n) + 8.  The pre-W5-H units below keep their programs; their
# expected addresses moved with the layout (and u_alloc's "leaked" free is
# now reused, u_alloc2's whole-heap request no longer fits).
P = H + 0x10
POISON32 = int.from_bytes(bytes([POISON_BYTE]) * 4, "big")
NEG1 = 0xFFFF_FFFF
FILE_FH = (0x00F000 - 44) >> 2          # BPTR of the FileHandle u_file builds
de_bp = hle_dos_env.fh_bptr             # BPTR of WS-B2 pool slot n


@dataclass
class Unit:
    name: str
    regs: dict[str, int]
    lines: list[str]
    reason: int = HALT_OK
    pc: int | None = None               # expected PC when reason != HALT_OK
    heap_next: int | None = None        # ENV_STATE heap_next after the run
    seam: bool = False                  # C2 hand-off test, see run_unit()
    files: dict[str, bytes] | None = None   # stage a fresh RAMDISK (Program)
    src: str | None = None              # fixture file (default: name)
    lazy: bool = False                  # zap hle_dos_magic, no host DOS init
    pools: bool = False                 # needs the W5-H hle.s pool wiring


def units() -> dict[str, Unit]:
    dos = hle_env.DOS_BASE
    us = [
        # FreeMem(A) reuses A: A2 = A (split), A4 = the split's remainder
        Unit("u_alloc", {"D2": P, "D3": 0, "D4": 0, "D5": P + 112,
                         "A3": POISON32, "D6": P + 112, "A2": P,
                         "A4": P + 24}, [], heap_next=H + 0x88),
        # $700000 no longer fits (control block + header): D4 = 0, so the
        # next AllocMem(1) succeeds; exact exhaustion is u_heap5's
        Unit("u_alloc2", {"D2": 0, "D3": 0, "D4": 0, "D5": P, "D6": P + 16,
                          "D7": 0}, [], heap_next=H + 0x28),
        Unit("u_pool", {"A2": P, "D3": P + 16, "D4": 0, "A3": P + 80,
                        "D5": P + 96, "D6": POISON32, "A4": P + 96, "D7": 0},
             []),
        Unit("u_dos", {"A2": hle_env.FH_CONOUT >> 2,
                       "A3": hle_env.FH_CONIN >> 2, "D4": NEG1, "D5": 0,
                       "D6": 5, "D7": 13, "A4": 0, "A5": NEG1},
             ["unit ok", "line2"]),
        Unit("u_dos2", {"D4": 15, "D5": NEG1, "D6": 3, "D7": NEG1},
             ["no output call", "in!"]),
        Unit("u_file", {"D1": FILE_FH, "D2": 0x12345, "D3": 0x777,
                        "A7": (FILE_FH << 2) - 4}, [], reason=0x01,
             pc=dos - 48 + 2, seam=True),
        Unit("u_file2", {"D1": FILE_FH, "D2": 0x23456, "D3": 9,
                         "A7": (FILE_FH << 2) - 4}, [], reason=0x01,
             pc=dos - 42 + 2, seam=True),
        Unit("u_file3", {"D1": FILE_FH, "D3": 3,
                         "A7": (FILE_FH << 2) - 4}, [], reason=0x01,
             pc=dos - 48 + 2, seam=True),
        Unit("u_dos3", {"D4": 0, "D5": 0, "D6": NEG1}, []),
        Unit("u_openlib", {"D2": dos, "D3": 0, "D4": 0, "D5": 0, "D6": 0,
                           "D7": dos, "A2": 40, "A3": 40}, []),
        Unit("u_vec", {"D2": P + 4, "D3": 0, "D4": 104, "D5": P + 116,
                       "D6": P + 116, "D7": P + 140}, [], heap_next=H + 0xA0),
        Unit("u_swap", {"D2": P + 252, "D3": 0xCAFEBABE, "D4": 0x00F000,
                        "D5": P + 20, "D6": P + 260,
                        "A4": hle_env.ENV_STACK_LOWER, "D7": 0x00F000,
                        "A5": 0}, []),
        # DateStamp: the first call of a run returns tick 1 (m2-post task 4)
        Unit("u_dos4", {"D5": NEG1, "D6": P, "D2": 0, "D3": 0, "D4": 1,
                        "A4": POISON32, "D7": NEG1}, []),
        # m2-post: GetVar leaves its buffer alone + IoErr 205; Close on the
        # console/NIL: handles is DOSTRUE and leaves IoErr alone
        Unit("u_getvar", {"D5": NEG1, "D2": POISON32, "D3": POISON32,
                          "D6": 205, "D7": NEG1, "A3": NEG1, "A4": NEG1,
                          "A5": 205}, ["ok"], files={}),
        Unit("u_cmode", {"A2": de_bp(0), "D2": NEG1, "D3": 0, "D4": 202,
                         "D5": 0, "D6": 209, "A3": de_bp(1), "D7": 0,
                         "A4": 202, "A5": NEG1}, [], files={}),
        Unit("u_cmode2", {"A2": de_bp(0), "D4": 0, "D5": 209, "D6": 0,
                          "D7": 211, "A3": 0, "A4": 209, "D2": 0, "A5": 211,
                          "D3": NEG1}, [], files={}),
        Unit("u_clock", {"D2": 6, "D3": 0, "D4": 0, "D5": 6, "D6": 0, "D7": 1,
                         "A2": 60, "A3": 0, "A4": 6}, []),
        # fix1 N1: out-of-range tick / minute are normalised (>= carries)
        Unit("u_clock2", {"D2": 0, "D3": 6, "D4": 0, "D5": 1, "D6": 0,
                          "D7": 11}, []),
        # fix1 F2: GetVar as the FIRST dos call after boot (hle_dos_magic
        # zapped, no host init): its IoErr 205 must survive the lazy init
        Unit("u_getvar_lazy", {"D5": NEG1, "D2": POISON32, "D3": POISON32,
                               "D6": 205, "D7": NEG1, "A3": NEG1, "A4": NEG1,
                               "A5": 205}, ["ok"], src="u_getvar", lazy=True),
        Unit("u_task", {"D2": hle_env.ENV_PROCESS, "D3": 0, "D4": 0, "D5": 0,
                        "D6": hle_env.EXEC_VERSION,
                        "D7": hle_env.ENV_CLI >> 2}, []),
        Unit("u_unimpl", {}, [], reason=0x01,
             pc=hle_env.EXEC_BASE - 384 + 2),
        # --- W5-H allocator (docs/HLE.md "Heap"); HEAP_CTL+4 = rejected frees
        Unit("u_heap1", {"D2": H + 0x10, "D3": H + 0x58, "D4": 10,
                         "D5": H + 0xA0, "D6": 11, "D7": H + 0x10,
                         "A2": H + 0xE8, "A3": 0}, [], heap_next=H + 0x128),
        Unit("u_heap2", {"D2": H + 0x10, "D3": H + 0x58, "D4": H + 0xA0,
                         "A2": H + 0xE8, "D5": 3, "D6": H + 0x10,
                         "D7": H + 0x10, "A3": 0, "A4": 3}, [],
             heap_next=H + 0x20),
        Unit("u_heap3", {"A2": H + 0x10, "D2": H + 0x80, "A3": H + 0x10,
                         "D3": 0, "A4": H + 0x10, "D4": 0, "D5": 0,
                         "D6": H + 0xB0, "D7": H + 0x10BC}, [],
             heap_next=H + 0x10C8),
        Unit("u_heap4", {"A2": H + 0x10, "D2": H + 0x20, "D3": H + 0x90,
                         "D4": H + 0xD8, "D5": H + 0x10, "D6": H + 0xD8,
                         "D7": 0, "A3": 1, "A4": 0xCAFEF00D}, [],
             heap_next=H + 0xE0, pools=True),
        Unit("u_heap5", {"D2": P, "D3": 0, "D4": 0, "D5": P,
                         "D6": 0x7FFFF8, "A2": 0x7FFFF8, "D7": 0, "A3": 0,
                         "A4": 0}, [], heap_next=0x800000),
    ]
    return {u.name: u for u in us}


def assemble(name: str, out_dir: str) -> bytes:
    home = vbcc_home()
    src = os.path.join(UNIT_DIR, f"{name}.s")
    exe = os.path.join(out_dir, name)
    defs = [f"-D{n}=${getattr(hle_env, n):X}"
            for n in ("FH_NIL", "FH_CONOUT", "FH_CONIN", "ENV_CLOCK")]
    cmd = [os.path.join(home, "bin", "vasmm68k_mot"), "-quiet", "-Fhunkexe",
           "-nosym", *defs, "-o", exe, src]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit(f"error: vasm failed for {name}:\n{res.stdout}{res.stderr}")
    with open(exe, "rb") as f:
        return f.read()


# --- screen helpers ------------------------------------------------------------

def ascii_to_screen(c: int) -> int:
    """Host mirror of hle_putc's printable ASCII -> screen code mapping."""
    if 0x20 <= c < 0x40:
        return c
    if 0x40 <= c < 0x60:
        return c - 0x40
    if 0x60 <= c < 0x80:
        return c - 0x60
    return 0x3F


def screen_to_text(row: bytes) -> str:
    return "".join(chr(0x40 + c) if 1 <= c <= 0x1A else
                   ("@" if c == 0 else chr(c) if 0x20 <= c <= 0x3F else ".")
                   for c in row).rstrip()


def expected_screen(lines: list[str]) -> bytes:
    cells = bytearray([SPACE] * (SCREEN_COLS * SCREEN_ROWS))
    for r, text in enumerate(lines):
        for c, ch in enumerate(text.encode("ascii")[:SCREEN_COLS]):
            cells[r * SCREEN_COLS + c] = ascii_to_screen(ch)
    return bytes(cells)


# --- toolchain -------------------------------------------------------------------

def vbcc_home() -> str:
    home = os.environ.get("VBCC", DEFAULT_VBCC)
    if not os.path.exists(os.path.join(home, "bin", "vc")):
        raise SystemExit(f"error: vbcc not found at {home} "
                         f"(run tools/vbcc/install.sh or set VBCC)")
    return home


def compile_c(name: str, out_dir: str) -> bytes:
    home = vbcc_home()
    src = os.path.join(FIXTURE_DIR, f"{name}.c")
    exe = os.path.join(out_dir, name)
    env = dict(os.environ, VBCC=home,
               PATH=os.path.join(home, "bin") + os.pathsep + os.environ["PATH"])
    cmd = ["vc", "+aos68k", "-O2", "-o", exe, src]   # vbcc's stock config
    res = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise SystemExit(f"error: vc failed for {name}:\n{res.stdout}{res.stderr}")
    with open(exe, "rb") as f:
        return f.read()


# --- diagnostics -----------------------------------------------------------------

def decode_lvo_halt(pc: int) -> str | None:
    """PC after an A-line halt in a vector slot -> 'library/Function (LVO)'."""
    slot = pc - 2
    depth = getattr(hle_env, "LVO_DEPTH", {})
    best = None
    for lib, (base, _funcs) in hle_env.LIBRARIES.items():
        lvo = slot - base
        lo = -depth.get(lib, 1024)
        if lo <= lvo < 0 and lvo % 6 == 0:
            if best is None or abs(lvo) < abs(best[1]):
                best = (lib, lvo)
    if best is None:
        return None
    lib, lvo = best
    name = LVO_NAMES.get(lib, {}).get(lvo, "?")
    return f"{lib}/{name} (LVO {lvo})"


# --- run ---------------------------------------------------------------------------

def launch(manifest: dict) -> tuple[list[Chunk], int]:
    """Env chunks + the PC to start at (CLI launch stub when available)."""
    st = manifest["stack_top"]
    if hasattr(hle_env, "check_program_fits"):
        hle_env.check_program_fits(manifest)
        return (hle_env.env_chunks(stack_top=st, entry=manifest["entry"]),
                hle_env.ENV_LAUNCH)
    if hasattr(hle_env, "launch_chunks"):           # 58f9755 env (red runs)
        stub, pc = hle_env.launch_chunks(manifest["entry"])
        return hle_env.env_chunks(stack_top=st) + stub, pc
    return hle_env.env_chunks(stack_top=st), manifest["entry"]


DOS_INIT = True       # main() clears it for --no-dos-init


def has_label(sess: Session, name: str) -> bool:
    from tests.harness_util import lookup_label
    try:
        lookup_label(sess.labels, name)
        return True
    except KeyError:
        return False


def stage_and_run(sess: Session, tag: str, blob: bytes, budget: int,
                  poison: bool, files: dict[str, bytes] | list | None = None,
                  lazy: bool = False):
    from c64_test_harness import write_bytes
    exe = hunk.parse(blob)
    chunks, manifest = image.build_image(exe)
    env, pc = launch(manifest)
    print(f"[{tag}] {len(blob)}-byte hunk file, {len(manifest['segments'])} "
          f"segment(s), entry=${manifest['entry']:06X} start=${pc:06X}")
    write_bytes(sess.target.transport, SCREEN, bytes([SPACE]) * 1000)
    staged = chunks + env
    if poison:
        staged.append(Chunk(HEAP_BASE, bytes([POISON_BYTE]) * POISON_LEN))
    if files is not None:            # a fresh, formatted RAMDISK
        staged += hle_dos_env.ramdisk_chunks(files)
    sess.stage(staged)
    if lazy:
        # "first dos call after boot": BSS magic gone, no host init, so the
        # program's first HLE DOS entry runs hle_dos_init lazily
        from c64_test_harness import write_bytes as _wb
        from tests.harness_util import lookup_label
        _wb(sess.target.transport, lookup_label(sess.labels, "hle_dos_magic"),
            b"\0\0\0\0")
    elif DOS_INIT and has_label(sess, "hle_dos_init"):
        sess._call("hle_dos_init")    # fresh WS-B2 handle pool per program
    t0 = time.monotonic()
    res = sess.run_program(pc, manifest["stack_top"], budget)
    dt = time.monotonic() - t0
    res.print_state()
    print(f"  wall {dt:.1f}s ({res.icount / max(dt, 1e-9):,.0f} instr/s, VICE warp)")
    why = HALT_REASONS.get(res.reason, f"${res.reason:02X}")
    if res.halt and res.reason == 0x01:
        where = decode_lvo_halt(res.pc)
        if where:
            why += f" in the {where} slot"
    if res.timeout:
        why = "TIMEOUT (budget exhausted)"
    elif not res.halt:
        why = "not halted"
    return res, manifest, why


def check_screen(sess: Session, tag: str, lines: list[str]) -> None:
    from c64_test_harness import read_bytes
    got = read_bytes(sess.target.transport, SCREEN, SCREEN_COLS * SCREEN_ROWS)
    want = expected_screen(lines)
    rows_bad = [r for r in range(SCREEN_ROWS)
                if got[r * SCREEN_COLS:(r + 1) * SCREEN_COLS]
                != want[r * SCREEN_COLS:(r + 1) * SCREEN_COLS]]
    for r in range(SCREEN_ROWS):
        text = screen_to_text(got[r * SCREEN_COLS:(r + 1) * SCREEN_COLS])
        if text or r in rows_bad:
            mark = "!!" if r in rows_bad else "  "
            print(f"  {mark} row {r:2d}: |{text}|")
    what = f"{len(lines)} line(s)" if lines else "blank"
    sess.check(f"{tag}: screen {what}", not rows_bad, f"rows differ: {rows_bad}")


def run_one(sess: Session, prog: Program, blob: bytes, budget: int,
            poison: bool) -> None:
    tag = prog.name
    if pool_skip(tag, prog.pools):
        return
    if prog.needs_dos and hle_dos_is_stub(sess):
        print(f"[{tag}] SKIP: needs WS-B2's hle_dos.s (stub linked)")
        return
    if prog.budget is not None:
        budget = prog.budget
    res, manifest, why = stage_and_run(sess, tag, blob, budget, poison,
                                       files=prog.files)
    halted = res.halt != 0 and res.timeout == 0
    want = HALT_REASONS[prog.reason]
    sess.check(f"{tag}: halted {want}", halted and res.reason == prog.reason,
               why)
    if prog.where is not None:
        sess.check(f"{tag}: in the {prog.where} slot", prog.where in why, why)
    if prog.exit_code is not None:
        sess.check(f"{tag}: exit code {prog.exit_code}",
                   res.regs["D0"] == prog.exit_code, f"D0={res.regs['D0']:08X}")
    if prog.reason == HALT_OK:
        sess.check(f"{tag}: A7 back at stack_top+4",
                   res.regs["A7"] == manifest["stack_top"] + 4,
                   f"A7={res.regs['A7']:08X}")
    check_screen(sess, tag, prog.lines)
    if prog.expect_files is not None or prog.expect_addr:
        check_files(sess, tag, prog.expect_files or {}, prog.expect_addr or {})


def check_files(sess: Session, tag: str, want: dict[str, bytes | None],
                addrs: dict[str, int] | None = None) -> None:
    """Read the RAMDISK back through the REU (tests.run_dos.Guest, which
    splits reads at 512 KB lines) and compare file contents."""
    from tests.run_dos import Guest
    g = Guest(sess, sess.labels)
    try:
        files = hle_dos_env.parse_directory(g.read)
    except ValueError as e:
        sess.check(f"{tag}: RAMDISK readable", False, str(e))
        return
    for name, data in want.items():
        ent = files.get(name.lower())
        if data is None:
            sess.check(f"{tag}: file {name} absent", ent is None,
                       f"present, {ent.size if ent else 0} bytes")
            continue
        sess.check(f"{tag}: file {name} = {len(data)} bytes",
                   ent is not None and ent.data == data,
                   "missing" if ent is None else
                   f"size {ent.size}: {ent.data[:40]!r}")
    for name, addr in (addrs or {}).items():
        ent = files.get(name.lower())
        sess.check(f"{tag}: file {name} extent at ${addr:06X}",
                   ent is not None and ent.addr == addr,
                   "missing" if ent is None else f"at ${ent.addr:06X}")


def read_heap_next(sess: Session) -> int:
    return int.from_bytes(bytes(sess.peek_guest(hle_env.ENV_STATE + i)
                                for i in range(4)), "big")


def hle_dos_is_stub(sess: Session) -> bool:
    """True while runtime/ops/hle_dos.s is the supervisor stub: all three
    entry labels then share one address (a single halt)."""
    from tests.harness_util import lookup_label
    addrs = {lookup_label(sess.labels, n) for n in
             ("hle_dos_dispatch", "hle_dos_read", "hle_dos_write")}
    return len(addrs) == 1


POOL_WIRED: bool | None = None    # main(): pool_wiring() of the PRG


def pool_wiring(prg: str, labels) -> bool | None:
    """W5-H pool entries: None = the PRG has no hle_pool_delete (a pre-W5-H
    allocator: pool tests run and must fail), True = hle.s calls both
    hle_pool_alloc and hle_pool_delete (a JSR/JMP to each is in the code),
    False = the entries exist but hle.s is not wired yet (pool tests SKIP;
    the wiring is a supervisor merge step, docs/HLE.md "Heap")."""
    from tests.harness_util import lookup_label
    try:
        adrs = [lookup_label(labels, n)
                for n in ("hle_pool_alloc", "hle_pool_delete")]
    except KeyError:
        return None
    with open(prg, "rb") as f:
        code = f.read()[2:]
    return all(any(bytes([op, a & 0xFF, a >> 8]) in code for op in (0x20, 0x4C))
               for a in adrs)


def pool_skip(tag: str, needs: bool) -> bool:
    if needs and POOL_WIRED is False:
        print(f"[{tag}] SKIP: needs the W5-H hle.s pool wiring "
              f"(hle_pool_alloc/hle_pool_delete not called)")
        return True
    return False


def run_unit(sess: Session, u: Unit, blob: bytes, budget: int) -> None:
    tag = u.name
    if pool_skip(tag, u.pools):
        return
    if u.seam and not hle_dos_is_stub(sess):
        # Real WS-B2 linked: the handle built on the stack is not an open
        # pool slot, so hle_dos_* must fail it cleanly — HALT_OK, D0 = -1,
        # stack restored, nothing on the console (b1-review F5).
        res, manifest, why = stage_and_run(sess, tag, blob, budget, poison=True)
        sess.check(f"{tag}: halted HALT_OK (serviced by hle_dos)",
                   res.halt != 0 and res.timeout == 0 and res.reason == HALT_OK,
                   why)
        sess.check(f"{tag}: D0=-1 (slot not open)", res.regs["D0"] == NEG1,
                   f"D0={res.regs['D0']:08X}")
        sess.check(f"{tag}: A7 back at stack_top+4",
                   res.regs["A7"] == manifest["stack_top"] + 4,
                   f"A7={res.regs['A7']:08X}")
        check_screen(sess, tag, [])
        return
    res, manifest, why = stage_and_run(sess, tag, blob, budget, poison=True,
                                       files=u.files, lazy=u.lazy)
    halted = res.halt != 0 and res.timeout == 0
    want = HALT_REASONS[u.reason]
    sess.check(f"{tag}: halted {want}", halted and res.reason == u.reason, why)
    if u.reason != HALT_OK:
        sess.check(f"{tag}: PC=${u.pc:06X}", res.pc == u.pc,
                   f"PC={res.pc:08X}")
    for reg, val in u.regs.items():
        sess.check(f"{tag}: {reg}={val:08X}", res.regs[reg] == val,
                   f"got {res.regs[reg]:08X}")
    if u.reason == HALT_OK:
        sess.check(f"{tag}: A7 back at stack_top+4",
                   res.regs["A7"] == manifest["stack_top"] + 4,
                   f"A7={res.regs['A7']:08X}")
    if u.heap_next is not None:
        got = read_heap_next(sess)
        sess.check(f"{tag}: heap_next=${u.heap_next:06X}", got == u.heap_next,
                   f"got ${got:08X}")
    check_screen(sess, tag, u.lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_vbcc",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True, help="runner PRG")
    ap.add_argument("--only", help="comma-separated program names "
                    "(default: every unit in fixtures/c/hle_unit, then "
                    f"{', '.join(programs())})")
    ap.add_argument("--budget", type=int, default=0, metavar="N",
                    help="max guest instructions per program, 0 = unlimited")
    ap.add_argument("--labels", help="ld65 -Ln label file (default: PRG .lbl)")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="per-call harness timeout in seconds (default 1800)")
    ap.add_argument("--no-poison", action="store_true",
                    help="do not pre-fill the heap with $A5 (C programs; "
                    "units always poison)")
    ap.add_argument("--no-dos-init", action="store_true",
                    help="do NOT call hle_dos_init before each program "
                    "(demonstrates the leak test's teeth)")
    ap.add_argument("--no-units", action="store_true",
                    help="skip the HLE unit programs (fixtures/c/hle_unit)")
    ap.add_argument("--backend", default="auto", choices=["auto", "vice", "u64"])
    ap.add_argument("--out", default=os.path.join(PROJECT_ROOT, "build", "vbcc"),
                    help="build dir for compiled programs (default build/vbcc)")
    args = ap.parse_args(argv)

    global DOS_INIT
    DOS_INIT = not args.no_dos_init
    prg = os.path.abspath(args.prg)
    labels_path = (os.path.abspath(args.labels) if args.labels
                   else os.path.splitext(prg)[0] + ".lbl")
    for p in (prg, labels_path):
        if not os.path.exists(p):
            print(f"error: not found: {p}", file=sys.stderr)
            return 2
    table = programs()
    utable = units()
    if args.only:
        names = args.only.split(",")
    else:
        names = ([] if args.no_units else list(utable)) + list(table)
    for n in names:
        if n not in table and n not in utable:
            print(f"error: unknown program {n!r}", file=sys.stderr)
            return 2

    os.makedirs(args.out, exist_ok=True)
    blobs = {n: (assemble(utable[n].src or n, args.out) if n in utable
                 else compile_c(n, args.out)) for n in names}
    print(f"built {len(blobs)} program(s) (vasm units / vbcc C) into {args.out}")

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(labels_path)
    global POOL_WIRED
    POOL_WIRED = pool_wiring(prg, labels)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=args.timeout)
            for n in names:
                if n in utable:
                    run_unit(sess, utable[n], blobs[n], args.budget)
                else:
                    run_one(sess, table[n], blobs[n], args.budget,
                            poison=not args.no_poison)
                print()
        finally:
            mgr.release(target)

    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    for f in sess.failures:
        print(f"FAIL  {f}")
    return 1 if n_bad else 0


if __name__ == "__main__":
    sys.exit(main())
