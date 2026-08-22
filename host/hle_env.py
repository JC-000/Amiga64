"""Guest-side HLE environment for M2 (host side).

Builds the extra image chunks that make a guest program's AmigaOS library
calls land in the interpreter's HLE dispatcher (``runtime/ops/hle.s``):

- **AbsExecBase**: guest address ``$000004`` holds a 32-bit BE pointer to a
  fake ExecBase placed in the reserved env region.
- **Library vector slots**: AmigaOS calls are ``jsr -LVO(A6)``.  For every
  supported LVO the 6-byte slot at ``base - LVO`` starts with the A-line
  opcode word ``$A1xx`` (xx = HLE function index), so the ``jsr`` executes
  our HLE call with the return address already on the guest stack.
- **Program exit**: a ``$AFFF`` HALT word sits at :data:`EXIT_ADDR`, and the
  env stages the 32-bit BE address of that word at ``[stack_top]``.
  ``test_run_program`` sets ``A7 = stack_top`` and pushes nothing, staging
  happens before the run, and ``emu_reset`` never touches guest memory —
  so the program's final RTS pops :data:`EXIT_ADDR` and halts ``HALT_OK``.

Env region: ``$000400-$000FFF`` guest — above the 68k vector page, below
the stack (``$00F000``) and the first program segment (``$010000``).

All chunks are :class:`host.image.Chunk` objects using the same
``{u32 LE offset, u32 LE len, data}`` framing as ``image.bin``, so they
compose with (and stage exactly like) ``host/image.py`` output.

The constants here mirror ``runtime/ops/hle.s`` — the two files are the
one ABI, documented in ``docs/HLE.md``.
"""

from __future__ import annotations

import struct

from .fixturegen import OP_HALT
from .image import STACK_TOP, Chunk

# --- Env memory map (guest addresses) --------------------------------------
ABSEXECBASE_ADDR = 0x000004  # AmigaOS AbsExecBase: pointer to ExecBase
ENV_BASE = 0x000400          # reserved env region (inclusive)
ENV_END = 0x001000           # reserved env region (exclusive)
EXIT_ADDR = 0x000440         # $AFFF HALT word — final-RTS landing site
EXEC_BASE = 0x000800         # fake ExecBase structure
DOS_BASE = 0x000A00          # fake DOSBase structure

# Magic file handle returned by dos.library/Output (mirrors hle.s FH_MAGIC).
FH_MAGIC = 0x0000C0DE

OP_HLE_BASE = 0xA100         # HLE call opcode word = OP_HLE_BASE | index
SLOT_SIZE = 6                # AmigaOS library vector slots are 6 bytes

# --- v0 function set: name -> (LVO, HLE index) ------------------------------
EXEC_FUNCS = {
    "OpenLibrary": (-552, 0x00),
    "CloseLibrary": (-414, 0x01),
}
DOS_FUNCS = {
    "Output": (-60, 0x02),
    "Write": (-48, 0x03),
}
LIBRARIES = {
    "exec.library": (EXEC_BASE, EXEC_FUNCS),
    "dos.library": (DOS_BASE, DOS_FUNCS),
}


def slot_addr(base: int, lvo: int) -> int:
    """Guest address of the vector slot for a (negative) LVO."""
    if lvo >= 0:
        raise ValueError(f"LVO must be negative, got {lvo}")
    return base + lvo


def slot_word(index: int) -> int:
    """A-line opcode word planted at a slot for HLE function *index*."""
    if not 0 <= index <= 0xFF:
        raise ValueError(f"HLE index {index} does not fit a byte")
    return OP_HLE_BASE | index


def env_chunks(stack_top: int = STACK_TOP) -> list[Chunk]:
    """Image chunks for the complete v0 HLE environment.

    Stage these alongside the program's own chunks (order irrelevant —
    nothing overlaps).  *stack_top* must match the A7 the driver passes to
    ``test_run_program``.
    """
    chunks = [
        Chunk(ABSEXECBASE_ADDR, struct.pack(">I", EXEC_BASE)),
        Chunk(EXIT_ADDR, struct.pack(">H", OP_HALT)),
    ]
    for base, funcs in LIBRARIES.values():
        for lvo, index in funcs.values():
            addr = slot_addr(base, lvo)
            if not ENV_BASE <= addr <= ENV_END - SLOT_SIZE:
                raise ValueError(
                    f"vector slot ${addr:06X} outside env region "
                    f"${ENV_BASE:06X}-${ENV_END - 1:06X}"
                )
            data = struct.pack(">H", slot_word(index)) + b"\0" * (SLOT_SIZE - 2)
            chunks.append(Chunk(addr, data))
    chunks.append(Chunk(stack_top, struct.pack(">I", EXIT_ADDR)))
    return chunks
