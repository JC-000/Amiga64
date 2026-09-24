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

from host import hle_env, hunk, image
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
        -450: "ChangeMode", -462: "SetIoErr", -810: "ReadItem", -948: "PutStr",
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
            "utility NULL"]),
        Program("readitem", None, [], reason=0x01,
                where="dos.library/ReadItem (LVO -810)"),
        Program("putstr", None, ["before"], reason=0x01,
                where="dos.library/PutStr (LVO -948)"),
        Program("bigstack", 0, ["stack swapped"]),
        # handle-state leak across programs (b1-review F2): run in order
        Program("leak_open", 0, ["opened 8"], needs_dos=True),
        Program("leak_check", 0, ["open ok"], needs_dos=True),
    ]
    return {p.name: p for p in progs}


# --- HLE unit programs (fixtures/c/hle_unit/*.s, vasm) -------------------------

UNIT_DIR = os.path.join(FIXTURE_DIR, "hle_unit")
H = HEAP_BASE
POISON32 = int.from_bytes(bytes([POISON_BYTE]) * 4, "big")
NEG1 = 0xFFFF_FFFF
FILE_FH = (0x00F000 - 44) >> 2          # BPTR of the FileHandle u_file builds


@dataclass
class Unit:
    name: str
    regs: dict[str, int]
    lines: list[str]
    reason: int = HALT_OK
    pc: int | None = None               # expected PC when reason != HALT_OK
    heap_next: int | None = None        # ENV_STATE heap_next after the run
    seam: bool = False                  # C2 hand-off test, see run_unit()


def units() -> dict[str, Unit]:
    dos = hle_env.DOS_BASE
    us = [
        Unit("u_alloc", {"D2": H, "D3": 0, "D4": 0, "D5": H + 104,
                         "A3": POISON32, "D6": H + 104, "A2": H + 112,
                         "A4": H + 128}, [], heap_next=H + 136),
        Unit("u_alloc2", {"D2": 0, "D3": 0, "D4": H, "D5": 0, "D6": H,
                          "D7": 0}, [], heap_next=H + 8),
        Unit("u_pool", {"A2": H, "D3": H + 8, "D4": 0, "A3": H + 64,
                        "D5": H + 72, "D6": POISON32, "A4": H + 72, "D7": 0},
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
        Unit("u_vec", {"D2": H + 4, "D3": 0, "D4": 104, "D5": H + 108,
                       "D6": H + 108, "D7": H + 124}, [], heap_next=H + 128),
        Unit("u_swap", {"D2": H + 252, "D3": 0xCAFEBABE, "D4": 0x00F000,
                        "D5": H + 20, "D6": H + 260,
                        "A4": hle_env.ENV_STACK_LOWER, "D7": 0x00F000}, []),
        Unit("u_task", {"D2": hle_env.ENV_PROCESS, "D3": 0, "D4": 0, "D5": 0,
                        "D6": hle_env.EXEC_VERSION,
                        "D7": hle_env.ENV_CLI >> 2}, []),
        Unit("u_unimpl", {}, [], reason=0x01,
             pc=hle_env.EXEC_BASE - 384 + 2),
    ]
    return {u.name: u for u in us}


def assemble(name: str, out_dir: str) -> bytes:
    home = vbcc_home()
    src = os.path.join(UNIT_DIR, f"{name}.s")
    exe = os.path.join(out_dir, name)
    defs = [f"-D{n}=${getattr(hle_env, n):X}"
            for n in ("FH_NIL", "FH_CONOUT", "FH_CONIN")]
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
                  poison: bool):
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
    sess.stage(staged)
    if DOS_INIT and has_label(sess, "hle_dos_init"):
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
    if prog.needs_dos and hle_dos_is_stub(sess):
        print(f"[{tag}] SKIP: needs WS-B2's hle_dos.s (stub linked)")
        return
    res, manifest, why = stage_and_run(sess, tag, blob, budget, poison)
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


def run_unit(sess: Session, u: Unit, blob: bytes, budget: int) -> None:
    tag = u.name
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
    res, manifest, why = stage_and_run(sess, tag, blob, budget, poison=True)
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
    blobs = {n: (assemble(n, args.out) if n in utable
                 else compile_c(n, args.out)) for n in names}
    print(f"built {len(blobs)} program(s) (vasm units / vbcc C) into {args.out}")

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(labels_path)
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
