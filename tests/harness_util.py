"""Shared helpers for the Amiga64 test harness (tests/smoke.py, tests/run_vectors.py).

VICE is driven through the c64-test-harness package (see the c64-test skill /
~/Documents/c64-test-harness). Backend selection is kept switchable: everything
here talks to a c64_test_harness TestTarget or C64Transport, never to VICE
process internals.
"""

from __future__ import annotations

import os
import subprocess
import time

from c64_test_harness import read_bytes

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD_STUB_DIR = os.path.join(PROJECT_ROOT, "build", "stub")

# Contract addresses (docs/CONTRACTS.md "C64 memory map") + runner.s layout
BUF_CASE = 0xC000
BUF_RESULT = 0xC800
RES_DONE = BUF_RESULT + 0x00
RES_HALT = BUF_RESULT + 0x01
RES_REASON = BUF_RESULT + 0x02
RES_NMEM = BUF_RESULT + 0x03
RES_REGS = BUF_RESULT + 0x10   # 70 bytes: 16 x u32 regs, u32 PC, u16 SR (LE)
RES_MEM = BUF_RESULT + 0x80
READY_FLAG = BUF_RESULT + 0x7FF  # $CFFF
READY_MAGIC = 0xA5

# VICE REU flags per contract ("-reu -reusize 16384" = 16 MB)
REU_VICE_ARGS = ["-reu", "-reusize", "16384"]


def wait_ready(transport, timeout: float = 60.0) -> None:
    """Poll the runner's ready flag ($CFFF == $A5) until the PRG has booted.

    Every read pauses the CPU (binary monitor semantics), so resume between
    polls to let autostart + the BASIC stub run.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_bytes(transport, READY_FLAG, 1)[0] == READY_MAGIC:
            return
        transport.resume()
        time.sleep(0.25)
    raise TimeoutError(
        f"runner PRG never signalled ready (${READY_FLAG:04X} != ${READY_MAGIC:02X} "
        f"within {timeout:.0f}s)"
    )


def call_runner(target, addr: int, timeout: float) -> None:
    """Invoke a runner entry point on any backend.

    VICE: jsr() (event-based breakpoint). Other backends (Ultimate 64):
    run_subroutine(), which self-reports via a sentinel trampoline.
    """
    if target.backend == "vice":
        from c64_test_harness import jsr
        jsr(target.transport, addr, timeout=timeout)
    else:
        from c64_test_harness import run_subroutine
        run_subroutine(target, addr, timeout=timeout)


def cl65_build(sources: list[str], out_prg: str, cfg: str, labels_out: str) -> None:
    """Assemble + link ca65 sources into a PRG with a VICE label file."""
    os.makedirs(os.path.dirname(out_prg), exist_ok=True)
    cmd = ["cl65", "-t", "none", "-C", cfg, "-o", out_prg, "-Ln", labels_out] + sources
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise RuntimeError(
            f"cl65 build failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}"
        )


def lookup_label(labels, name: str) -> int:
    """Label lookup tolerant of the leading dot in VICE label files."""
    addr = labels.address(name)
    if addr is None:
        addr = labels.address("." + name)
    if addr is None:
        raise KeyError(f"label '{name}' not found in label file")
    return addr
