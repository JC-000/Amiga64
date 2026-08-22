"""M2 exit demo driver: run the HLE hello-world program, verify the screen.

Usage:
    python3 -m tests.run_hello --prg build/m2/runner.prg
        [--budget N] [--labels PATH] [--timeout SECS] [--backend auto|vice|u64]

Pipeline (docs/HLE.md): host/fixturegen.fixture_hello() builds a two-hunk
AmigaOS console program -> host/image.py lays it out ->
host/hle_env.env_chunks() adds the HLE environment (AbsExecBase, library
vector slots holding $A1xx words, the $AFFF exit stub, and the pre-staged
return address at [stack_top]) -> everything is staged through the existing
test_load_chunk path -> test_run_program runs it (A7 = stack_top, so the
program's final RTS pops the exit stub address and halts HALT_OK) -> the
driver reads C64 screen RAM at $0400 and asserts the screen codes spell the
message, plus the architectural end state.  Exit code 0 = all checks passed.

The screen region is cleared to spaces before the run (the boot screen has
text there), so the assertion sees only what dos.library/Write printed.
"""

from __future__ import annotations

import argparse
import os
import sys

from host import fixturegen, hle_env, hunk, image
from tests.run_program import HALT_OK, Session

SCREEN = 0x0400
SCREEN_COLS = 40
SCREEN_ROWS_CHECKED = 2   # message + one row proving the newline landed
SPACE = 0x20              # screen code for blank

HELLO_ICOUNT = 15         # instructions incl. 4 HLE calls and the final HALT


def ascii_to_screen(text: bytes) -> list[int]:
    """Host mirror of hle_putc's ASCII -> screen-code conversion (printables)."""
    out = []
    for c in text:
        if c < 0x20:
            continue          # control chars don't produce a cell
        elif c < 0x40:
            out.append(c)
        elif c < 0x60:
            out.append(c - 0x40)
        elif c < 0x80:
            out.append(c - 0x60)
        else:
            out.append(0x3F)  # '?'
    return out


def expected_screen_rows() -> list[list[int]]:
    """Screen-code contents of the first rows after the hello program ran."""
    rows = [[SPACE] * SCREEN_COLS for _ in range(SCREEN_ROWS_CHECKED)]
    row, col = 0, 0
    for c in fixturegen.HELLO_MESSAGE:
        if c == 0x0A:
            row, col = row + 1, 0
            continue
        code = ascii_to_screen(bytes([c]))
        if code and row < len(rows) and col < SCREEN_COLS:
            rows[row][col] = code[0]
            col += 1
    return rows


def expected_end_state(manifest: dict) -> dict[str, int]:
    """Architectural register state at HALT (docs/HLE.md calling convention)."""
    entry = manifest["entry"]
    msg_addr = manifest["segments"][1]["guest_addr"]
    return {
        "D0": len(fixturegen.HELLO_MESSAGE),        # Write's return value
        "D1": hle_env.FH_MAGIC,                     # Output's file handle
        "D2": msg_addr,                             # relocated message ptr
        "D3": len(fixturegen.HELLO_MESSAGE),
        "A1": entry + fixturegen.HELLO_DOSNAME_OFFSET,  # lea dosname(pc)
        "A6": hle_env.DOS_BASE,
        "A7": manifest["stack_top"] + 4,            # final RTS popped the exit
    }


def verify_screen(sess: Session, transport) -> None:
    from c64_test_harness import read_bytes
    got = read_bytes(transport, SCREEN,
                     SCREEN_ROWS_CHECKED * SCREEN_COLS)
    for r, want in enumerate(expected_screen_rows()):
        row = list(got[r * SCREEN_COLS:(r + 1) * SCREEN_COLS])
        sess.check(f"screen row {r} spells "
                   f"{'HELLO, AMIGA' if r == 0 else 'blank (newline)'}",
                   row == want,
                   f"got {bytes(row).hex()} want {bytes(want).hex()}")
    text = "".join(chr(0x40 + c) if 1 <= c <= 0x1A else
                   (chr(c) if 0x20 <= c <= 0x3F else ".")
                   for c in got[:SCREEN_COLS])
    print(f"  screen row 0: |{text}|")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_hello",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True, help="runner PRG (build/m2/runner.prg)")
    ap.add_argument("--budget", type=int, default=10_000, metavar="N",
                    help="max guest instructions (default 10000)")
    ap.add_argument("--labels", help="ld65 -Ln label file (default: PRG path with .lbl)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="per-call harness timeout in seconds")
    ap.add_argument("--backend", default="auto", choices=["auto", "vice", "u64"],
                    help="c64-test-harness backend (default: auto)")
    args = ap.parse_args(argv)

    prg = os.path.abspath(args.prg)
    if not os.path.exists(prg):
        print(f"error: PRG not found: {prg}", file=sys.stderr)
        return 2
    labels_path = (os.path.abspath(args.labels) if args.labels
                   else os.path.splitext(prg)[0] + ".lbl")
    if not os.path.exists(labels_path):
        print(f"error: label file not found: {labels_path}", file=sys.stderr)
        return 2

    blob = fixturegen.fixture_hello()
    exe = hunk.parse(blob)
    chunks, manifest = image.build_image(exe)
    env = hle_env.env_chunks(stack_top=manifest["stack_top"])
    print(f"[hello] {len(blob)}-byte hunk file, "
          f"{len(manifest['segments'])} segment(s), "
          f"entry=${manifest['entry']:06X} stack_top=${manifest['stack_top']:06X}, "
          f"{len(env)} env chunk(s)")

    from c64_test_harness import Labels, UnifiedManager, ViceConfig, write_bytes
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

            # Clear the screen region: the C64 boot screen has text at $0400.
            write_bytes(target.transport, SCREEN, bytes([SPACE]) * 1000)

            sess.stage(chunks + env)
            res = sess.run_program(manifest["entry"], manifest["stack_top"],
                                   args.budget)
            res.print_state()

            sess.check("halted (no timeout)",
                       res.halt != 0 and res.timeout == 0,
                       f"halt={res.halt:02X} timeout={res.timeout}")
            sess.check("reason HALT_OK", res.reason == HALT_OK,
                       f"reason={res.reason:02X}")
            want = expected_end_state(manifest)
            for reg, val in want.items():
                sess.check(f"{reg}={val:08X}", res.regs[reg] == val,
                           f"got {res.regs[reg]:08X}")
            sess.check(f"PC={hle_env.EXIT_ADDR + 2:08X} (past exit stub)",
                       res.pc == hle_env.EXIT_ADDR + 2, f"got {res.pc:08X}")
            sess.check(f"{HELLO_ICOUNT} instructions",
                       res.icount == HELLO_ICOUNT, f"got {res.icount}")
            verify_screen(sess, target.transport)
        finally:
            mgr.release(target)

    n_ok, n_bad = len(sess.checks), len(sess.failures)
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    if n_bad:
        for f in sess.failures:
            print(f"FAIL  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
