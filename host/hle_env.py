"""Guest-side HLE environment for M2 (host side).

Builds the image chunks that make a guest program's AmigaOS library calls
land in the interpreter's HLE dispatcher (``runtime/ops/hle.s``) and give
compiler startup code (vbcc's startup.o + vc.lib) the process context it
expects:

- **AbsExecBase**: guest ``$000004`` holds a 32-bit BE pointer to a fake
  ExecBase (a ``struct Library`` with ``lib_Version`` = 40).
- **Library vector slots**: AmigaOS calls are ``jsr -LVO(A6)``.  For every
  implemented LVO the 6-byte slot at ``base - LVO`` starts with the A-line
  opcode word ``$A1xx`` (xx = HLE function index); every other slot in the
  table holds :data:`OP_LVO_UNIMPL` so a call to an unimplemented function
  halts ``HALT_ALINE`` with PC = slot + 2 instead of running garbage.
- **Process context**: ``FindTask(NULL)`` returns :data:`ENV_PROCESS`, a
  ``struct Process`` with ``pr_CLI`` (-> a CLI struct with a command name,
  so startup code takes the CLI path, not the Workbench-message path),
  ``pr_CIS``/``pr_COS`` (console FileHandle BPTRs, returned by
  Input()/Output()) and ``tc_SPLower``/``tc_SPUpper`` (the guest stack).
- **FileHandles** (contract C2): 44-byte ``struct FileHandle`` records whose
  ``fh_Args`` holds the handle kind (console out / console in / NIL:).
- **HLE state** (:data:`ENV_STATE`): heap bump pointer and console cursor.
  Staging the env re-initialises them for the next run.
- **Program exit**: a ``$AFFF`` HALT word sits at :data:`EXIT_ADDR`, and the
  env stages its address at ``[stack_top]``, so the program's final RTS
  halts ``HALT_OK`` (D0 = the program's return code).
- **CLI launch stub** (:func:`launch_chunks`): sets A0/D0 to the argument
  string per the AmigaOS process entry ABI and jumps to the program.

WS-B1 owns env ``$000400-$000BFF``; ``$000C00-$000FFF`` is WS-B2's
(docs/WAVE4_BRIEF.md C4).  All chunks are :class:`host.image.Chunk` objects
using the same ``{u32 LE offset, u32 LE len, data}`` framing as
``image.bin``.  The constants mirror ``runtime/rc/guest.inc`` and
``runtime/ops/hle.s``; the ABI is documented in ``docs/HLE.md``.
"""

from __future__ import annotations

import struct

from .fixturegen import OP_HALT
from .hle_dos_env import DOS_FILE_FUNCS
from .image import STACK_TOP, Chunk

# --- Env memory map (guest addresses; mirrors runtime/rc/guest.inc) --------
ABSEXECBASE_ADDR = 0x000004  # AmigaOS AbsExecBase: pointer to ExecBase
ENV_BASE = 0x000400          # reserved env region (inclusive)
ENV_END = 0x001000           # reserved env region (exclusive)
# WS-B1 owns $000400-$000BFF; $000C00-$000FFF is WS-B2's (docs/WAVE4_BRIEF.md C4)
ENV_B1_END = 0x000C00        # exclusive
ENV_STATE = 0x000400         # HLE state block (8 bytes, see below)
ENV_LAUNCH = 0x000410        # CLI launch stub (12 bytes)
ENV_ARGS = 0x000420          # CLI argument string (32 bytes, NUL-terminated)
EXIT_ADDR = 0x000440         # $AFFF HALT word — final-RTS landing site
ENV_CLI_NAME = 0x000444      # BSTR command name buffer (60 bytes)
ENV_CLI = 0x000480           # struct CommandLineInterface (64 bytes)
EXEC_BASE = 0x000800         # fake ExecBase (struct Library, 34 bytes used)
DOS_BASE = 0x000A00          # fake DOSBase structure (not read)
ENV_PROCESS = 0x000A40       # struct Process (228 bytes, $A40-$B23)
FH_CONOUT = 0x000B30         # struct FileHandle, fh_Args = FH_KIND_CONOUT
FH_CONIN = 0x000B60          # struct FileHandle, fh_Args = FH_KIND_CONIN
FH_NIL = 0x000B90            # struct FileHandle, fh_Args = FH_KIND_NIL

# HLE heap (exec AllocMem/FreeMem + pools), contract C4
HEAP_BASE = 0x100000         # first byte (8-byte aligned)
HEAP_END = 0x7FFFFF          # last byte (inclusive)

# HLE state block at ENV_STATE (all big-endian, staged fresh for every run —
# this is how the HLE is re-initialised per run without a runner hook):
#   +0 u32 heap_next   next free heap byte (bump pointer), starts HEAP_BASE
#   +4 u16 cursor      console cursor, cell offset 0-1000 from SCREEN
#   +6 u8  column      console cursor column 0-39
#   +7 u8  reserved
STATE_SIZE = 8

# --- AmigaOS structure offsets used by the HLE / vbcc startup ----------------
EXEC_VERSION = 40            # lib_Version (Kickstart 3.1): vc.lib >= 39 uses
                             # exec memory pools (CreatePool/AllocPooled...)
LIB_VERSION_OFF = 20         # struct Library lib_Version (UWORD)
LIB_SIZE = 34                # sizeof(struct Library)
NT_PROCESS = 13
LN_TYPE_OFF = 8
TC_SPLOWER_OFF = 0x3A
TC_SPUPPER_OFF = 0x3E
PR_CIS_OFF = 0x9C
PR_COS_OFF = 0xA0
PR_CLI_OFF = 0xAC
PR_CES_OFF = 0xE0
PROCESS_SIZE = 0xE4
CLI_COMMANDNAME_OFF = 0x10
CLI_SIZE = 64
CLI_NAME_BUF = 60            # BSTR length byte + name + NUL (startup writes it)
ENV_STACK_LOWER = 0x001000   # tc_SPLower: bottom of the guest stack region

# FileHandle model (contract C2): BPTR (addr >> 2) to a longword-aligned
# 44-byte struct FileHandle; HLE reads only fh_Args (offset 36) = handle kind.
FH_SIZE = 44
FH_ARGS_OFF = 36
FH_KIND_CONOUT = 0
FH_KIND_CONIN = 1
FH_KIND_NIL = 2
FH_KIND_FILE0 = 0x10         # WS-B2 file slot n = kind $10+n

OP_HLE_BASE = 0xA100         # HLE call opcode word = OP_HLE_BASE | index
OP_LVO_UNIMPL = 0xA0FF       # planted in every unimplemented vector slot:
                             # any $Axxx other than $A1xx/$AFFF halts
                             # HALT_ALINE with PC = slot + 2 (decodable)
SLOT_SIZE = 6                # AmigaOS library vector slots are 6 bytes
FIRST_LVO = -30              # first user LVO (-6..-24 are the std vectors)

# --- function set: name -> (LVO, HLE index); indices $00-$2F are WS-B1's -----
EXEC_FUNCS = {
    "OpenLibrary": (-552, 0x00),
    "CloseLibrary": (-414, 0x01),
    "FindTask": (-294, 0x07),
    "AllocMem": (-198, 0x08),
    "FreeMem": (-210, 0x09),
    "SetSignal": (-306, 0x0A),
    "CreatePool": (-696, 0x0B),
    "DeletePool": (-702, 0x0C),
    "AllocPooled": (-708, 0x0D),
    "FreePooled": (-714, 0x0E),
}
DOS_FUNCS = {
    "Output": (-60, 0x02),
    "Write": (-48, 0x03),
    "Input": (-54, 0x04),
    "Read": (-42, 0x05),
    "IsInteractive": (-216, 0x06),
}
HLE_FN_COUNT = 0x0F          # indices $00-$0E (mirrors hle.s HLE_FN_COUNT)
# File-DOS functions (HLE indices $30-$4F) are owned by WS-B2 in
# host/hle_dos_env.py and merged here (docs/WAVE4_BRIEF.md C3).
_clash = set(DOS_FUNCS) & set(DOS_FILE_FUNCS)
if _clash:
    raise RuntimeError(f"dos.library functions claimed twice: {_clash}")
DOS_FUNCS.update(DOS_FILE_FUNCS)
LIBRARIES = {
    "exec.library": (EXEC_BASE, EXEC_FUNCS),
    "dos.library": (DOS_BASE, DOS_FUNCS),
}
# Unimplemented-slot sentinel fill: deepest LVO per library that still
# leaves the slot table clear of the structures staged below it.
SENTINEL_DEPTH = {
    "exec.library": EXEC_BASE - (ENV_CLI + CLI_SIZE),    # -832: all of V40
    "dos.library": DOS_BASE - (EXEC_BASE + 0x40),         # -448
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


def bptr(addr: int) -> int:
    if addr % 4:
        raise ValueError(f"BPTR target ${addr:06X} not longword aligned")
    return addr >> 2


def _be32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFF_FFFF)


def _library(version: int) -> bytes:
    lib = bytearray(LIB_SIZE)
    lib[LIB_VERSION_OFF:LIB_VERSION_OFF + 2] = struct.pack(">H", version)
    return bytes(lib)


def filehandle(kind: int) -> bytes:
    """44-byte struct FileHandle, all zero except fh_Args = *kind* (C2)."""
    fh = bytearray(FH_SIZE)
    fh[FH_ARGS_OFF:FH_ARGS_OFF + 4] = _be32(kind)
    return bytes(fh)


def _process() -> bytes:
    p = bytearray(PROCESS_SIZE)
    p[LN_TYPE_OFF] = NT_PROCESS
    p[TC_SPLOWER_OFF:TC_SPLOWER_OFF + 4] = _be32(ENV_STACK_LOWER)
    p[TC_SPUPPER_OFF:TC_SPUPPER_OFF + 4] = _be32(STACK_TOP)
    p[PR_CIS_OFF:PR_CIS_OFF + 4] = _be32(bptr(FH_CONIN))
    p[PR_COS_OFF:PR_COS_OFF + 4] = _be32(bptr(FH_CONOUT))
    p[PR_CLI_OFF:PR_CLI_OFF + 4] = _be32(bptr(ENV_CLI))
    # pr_CES stays 0: vbcc's startup then uses Output() for stderr
    return bytes(p)


def _cli() -> bytes:
    c = bytearray(CLI_SIZE)
    c[CLI_COMMANDNAME_OFF:CLI_COMMANDNAME_OFF + 4] = _be32(bptr(ENV_CLI_NAME))
    return bytes(c)


def _bstr_name(name: bytes) -> bytes:
    if len(name) > CLI_NAME_BUF - 2:
        raise ValueError(f"command name longer than {CLI_NAME_BUF - 2} bytes")
    buf = bytes([len(name)]) + name
    return buf + b"\0" * (CLI_NAME_BUF - len(buf))


def state_block(heap_next: int = HEAP_BASE, cursor: int = 0,
                column: int = 0) -> bytes:
    return struct.pack(">IHBB", heap_next, cursor, column, 0)


def env_chunks(stack_top: int = STACK_TOP,
               command_name: bytes = b"prog") -> list[Chunk]:
    """Image chunks for the complete HLE environment.

    Stage these alongside the program's own chunks (order irrelevant —
    nothing overlaps).  *stack_top* must match the A7 the driver passes to
    ``test_run_program``.  Staging them re-initialises all HLE state
    (heap pointer, console cursor) for the next run.
    """
    chunks = [
        Chunk(ABSEXECBASE_ADDR, struct.pack(">I", EXEC_BASE)),
        Chunk(ENV_STATE, state_block()),
        Chunk(EXIT_ADDR, struct.pack(">H", OP_HALT)),
        Chunk(ENV_CLI_NAME, _bstr_name(command_name)),
        Chunk(ENV_CLI, _cli()),
        Chunk(EXEC_BASE, _library(EXEC_VERSION)),
        Chunk(DOS_BASE, _library(0)),
        Chunk(ENV_PROCESS, _process()),
        Chunk(FH_CONOUT, filehandle(FH_KIND_CONOUT)),
        Chunk(FH_CONIN, filehandle(FH_KIND_CONIN)),
        Chunk(FH_NIL, filehandle(FH_KIND_NIL)),
    ]
    for lib, (base, funcs) in LIBRARIES.items():
        claimed = {}
        for name, (lvo, index) in funcs.items():
            addr = slot_addr(base, lvo)
            if not ENV_BASE <= addr <= ENV_END - SLOT_SIZE:
                raise ValueError(
                    f"vector slot ${addr:06X} outside env region "
                    f"${ENV_BASE:06X}-${ENV_END - 1:06X}"
                )
            if -lvo > SENTINEL_DEPTH[lib]:
                raise ValueError(f"{lib}/{name} LVO {lvo} collides with the "
                                 f"structures below the slot table")
            claimed[lvo] = slot_word(index)
        for lvo in range(FIRST_LVO, -SENTINEL_DEPTH[lib] - 1, -SLOT_SIZE):
            word = claimed.get(lvo, OP_LVO_UNIMPL)
            data = struct.pack(">H", word) + b"\0" * (SLOT_SIZE - 2)
            chunks.append(Chunk(slot_addr(base, lvo), data))
    chunks.append(Chunk(stack_top, struct.pack(">I", EXIT_ADDR)))
    return chunks


LAUNCH_SIZE = 12
ARGS_MAX = 31                # ENV_ARGS buffer is 32 bytes incl. the NUL


def launch_chunks(entry: int, args: bytes = b"\n") -> tuple[list[Chunk], int]:
    """CLI launch stub: the AmigaOS process entry ABI for a CLI command is
    A0 = argument string (ending in a newline), D0 = its length.
    ``test_run_program`` zeroes every register, so the driver enters at
    this stub instead of the program entry:

        lea    ENV_ARGS.w,a0
        moveq  #len,d0
        jmp    entry.l

    Returns (chunks, pc_to_start_at).  The stub's JMP leaves the stack as
    the runner set it, so the program's final RTS still pops EXIT_ADDR.
    """
    if len(args) > ARGS_MAX:
        raise ValueError(f"args longer than {ARGS_MAX} bytes")
    code = struct.pack(">HHHHI", 0x41F8, ENV_ARGS, 0x7000 | len(args),
                       0x4EF9, entry)
    assert len(code) == LAUNCH_SIZE
    return [Chunk(ENV_LAUNCH, code),
            Chunk(ENV_ARGS, args + b"\0" * (ARGS_MAX + 1 - len(args)))], ENV_LAUNCH
