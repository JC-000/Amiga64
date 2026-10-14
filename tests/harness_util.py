"""Shared helpers for the Amiga64 test harness (tests/smoke.py, tests/run_vectors.py).

VICE is driven through the c64-test-harness package (see
https://github.com/JC-000/c64-test-harness). Backend selection is kept switchable: everything
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


# --------------------------------------------------------------------------
# Ultimate 64 call mechanism
#
# The harness's stock run_subroutine() triggers its trampoline by injecting
# "SYS nnnn" into the keyboard buffer, which requires BASIC READY. Our
# runner's `main` parks in a `jmp @idle` busy loop after setting the ready
# flag, so keyboard injection can never execute. Instead the host installs
# a mailbox dispatcher in the unused $CE00 page of buf_result and retargets
# the idle JMP into it with a single-byte write (atomic — the operand's low
# byte is unchanged, so the spinning CPU sees either the old or the new
# target, never a torn address):
#
#   @idle:  jmp @idle          ; 4C lo hi at main+5; hi byte at main+7
#     -->   jmp $CExx          ; xx = lo(main+5), unchanged
#
#   dispatcher:  lda MBOX / beq dispatcher   ; spin
#                lda #0  / sta MBOX
#                jsr mem_rec_dirty           ; C1 (when the label exists)
#                jsr $xxxx                   ; operand patched per call
#                lda #$FF / sta DONE
#                jmp dispatcher
#
# The mem_rec_dirty call is contract C1 (docs/WAVE4_BRIEF.md): between two
# mailbox calls the host's REST DMA and the Ultimate firmware may leave the
# REC in any state, so every host-dispatched call -- including direct
# memory-API calls such as run_program's peek_guest -> mem_read8 --
# invalidates the memory API's REC shadow first.  On VICE, jsr() enters
# the target directly; nothing on the VICE host side touches the REC, and
# the runner entries invalidate on their own.
#
# Per call the host writes the JSR operand, then MBOX=1/DONE=0 in one DMA
# write, and polls DONE for $FF. IRQs stay enabled until the first
# test_run_case SEIs (and never CLIs), which is harmless either way.
# --------------------------------------------------------------------------

U64_DISPATCH_PAGE = 0xCE00           # unused span of buf_result ($C800-$CFFF)
U64_MBOX = U64_DISPATCH_PAGE | 0xF8
U64_DONE = U64_DISPATCH_PAGE | 0xF9
U64_SELFTEST_RTS = U64_DISPATCH_PAGE | 0xFA
IDLE_JMP_OFFSET = 5                  # runner.s: main = lda #$A5 / sta abs / @idle


def u64_install_dispatcher(target, main_addr: int,
                           rec_dirty_addr: int | None = None) -> dict:
    """Install the mailbox dispatcher and retarget the runner's idle loop.

    *rec_dirty_addr*: address of the core's ``mem_rec_dirty`` (C1); when
    given, the dispatcher calls it before every dispatched call.

    Returns the dispatch map {mbox, done, jsr_operand} and stashes it on
    the target for call_runner(). Verifies the idle-loop bytes before
    patching and self-tests the mechanism with a bare RTS call.
    """
    from c64_test_harness import read_bytes, write_bytes
    transport = target.transport

    idle = main_addr + IDLE_JMP_OFFSET
    got = read_bytes(transport, idle, 3)
    want = bytes([0x4C, idle & 0xFF, (idle >> 8) & 0xFF])
    if got not in (want, bytes([0x4C, idle & 0xFF, (U64_DISPATCH_PAGE >> 8)])):
        raise RuntimeError(
            f"runner idle loop not found at ${idle:04X}: "
            f"expected {want.hex()} got {got.hex()} — runner.s layout changed?"
        )

    entry = U64_DISPATCH_PAGE | (idle & 0xFF)
    body = entry + 3
    size = 21 + (3 if rec_dirty_addr is not None else 0)
    if (body + size) > (U64_MBOX & 0xFFFF):
        # entry sits high in the page; put the body at the page start
        body = U64_DISPATCH_PAGE
        if body + size >= entry:
            raise RuntimeError(f"no room for dispatcher body around ${entry:04X}")
    blo, bhi = body & 0xFF, body >> 8
    mlo, mhi = U64_MBOX & 0xFF, U64_MBOX >> 8
    dlo, dhi = U64_DONE & 0xFF, U64_DONE >> 8
    dirty = b""
    if rec_dirty_addr is not None:
        dirty = bytes([0x20, rec_dirty_addr & 0xFF,
                       (rec_dirty_addr >> 8) & 0xFF])   # jsr mem_rec_dirty
    code = bytes([
        0xAD, mlo, mhi,          # body:  lda MBOX
        0xF0, 0xFB,              #        beq body
        0xA9, 0x00,              #        lda #0
        0x8D, mlo, mhi,          #        sta MBOX
    ]) + dirty + bytes([
        0x20, 0x00, 0x00,        #        jsr $0000   (operand patched)
        0xA9, 0xFF,              #        lda #$FF
        0x8D, dlo, dhi,          #        sta DONE
        0x4C, blo, bhi,          #        jmp body
    ])
    assert len(code) == size
    jsr_operand = body + 11 + len(dirty)

    write_bytes(transport, body, code)
    write_bytes(transport, U64_MBOX, bytes([0x00, 0x00, 0x60]))  # flags + RTS
    if entry != body:
        write_bytes(transport, entry, bytes([0x4C, blo, bhi]))
    if read_bytes(transport, body, len(code)) != code:
        raise RuntimeError("dispatcher body readback mismatch")

    # Atomic single-byte retarget of the idle JMP's operand high byte.
    write_bytes(transport, main_addr + IDLE_JMP_OFFSET + 2,
                bytes([U64_DISPATCH_PAGE >> 8]))

    disp = {"mbox": U64_MBOX, "done": U64_DONE, "jsr_operand": jsr_operand}
    target._u64_dispatch = disp

    # Self-test: call a bare RTS through the mailbox.
    _u64_call(transport, disp, U64_SELFTEST_RTS, timeout=10.0)
    return disp


def _u64_call(transport, disp: dict, addr: int, timeout: float) -> None:
    from c64_test_harness import read_bytes, write_bytes
    write_bytes(transport, disp["jsr_operand"],
                bytes([addr & 0xFF, (addr >> 8) & 0xFF]))
    # MBOX=1 and DONE=0 in one DMA write (adjacent bytes)
    write_bytes(transport, disp["mbox"], bytes([0x01, 0x00]))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_bytes(transport, disp["done"], 1)[0] == 0xFF:
            return
        time.sleep(0.002)
    mbox = read_bytes(transport, disp["mbox"], 1)[0]
    raise TimeoutError(
        f"u64 dispatcher: call to ${addr:04X} did not complete in {timeout:.1f}s "
        f"(MBOX={mbox:02X}: {'never picked up' if mbox else 'started, no return'})"
    )


def call_runner(target, addr: int, timeout: float) -> None:
    """Invoke a runner entry point on any backend.

    VICE: jsr() (event-based breakpoint). Ultimate 64: mailbox dispatcher
    installed by u64_install_dispatcher() (see above — the harness's stock
    run_subroutine needs BASIC READY, which the runner never returns to).
    """
    if target.backend == "vice":
        from c64_test_harness import jsr
        jsr(target.transport, addr, timeout=timeout)
    else:
        disp = getattr(target, "_u64_dispatch", None)
        if disp is None:
            raise RuntimeError(
                "U64 dispatcher not installed — prepare_target() must run "
                "u64_install_dispatcher() before call_runner()"
            )
        _u64_call(target.transport, disp, addr, timeout)


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
