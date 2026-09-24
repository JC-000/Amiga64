"""Guest-side HLE environment for M2 (host side).

Builds the image chunks that make a guest program's AmigaOS library calls
land in the interpreter's HLE dispatcher (``runtime/ops/hle.s``) and give
compiler startup code (vbcc's startup.o + vc.lib) the process context it
expects:

- **AbsExecBase**: guest ``$000004`` holds a 32-bit BE pointer to a fake
  full-size ExecBase (632 bytes, V40: ThisTask, AttnFlags, MaxLocMem, …).
- **Library tables** (region ``$0F0000-$0FFFFF``, C4 amendment): ExecBase
  and DOSBase with their FULL vector tables.  AmigaOS calls are
  ``jsr -LVO(A6)``; every 6-byte slot at ``base - LVO`` down to the
  deepest known LVO (:data:`LVO_DEPTH`) is staged: implemented LVOs start
  with the A-line word ``$A1xx`` (xx = HLE function index), all others
  hold :data:`OP_LVO_UNIMPL`, so a call to an unimplemented function halts
  ``HALT_ALINE`` with PC = slot + 2 in its own library's table.
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

WS-B1 owns env ``$000400-$000BFF`` (staged in full, gaps zeroed) and the
library region; ``$000C00-$000FFF`` is WS-B2's (docs/WAVE4_BRIEF.md C4).  All chunks are :class:`host.image.Chunk` objects
using the same ``{u32 LE offset, u32 LE len, data}`` framing as
``image.bin``.  The constants mirror ``runtime/rc/guest.inc`` and
``runtime/ops/hle.s``; the ABI is documented in ``docs/HLE.md``.
"""

from __future__ import annotations

import struct

from .fixturegen import OP_HALT
from .hle_dos_env import DOS_FILE_FUNCS
from . import image as _image
from .image import STACK_TOP, Chunk

# --- Env memory map (guest addresses; mirrors runtime/rc/guest.inc) --------
ABSEXECBASE_ADDR = 0x000004  # AmigaOS AbsExecBase: pointer to ExecBase
ENV_BASE = 0x000400          # reserved env region (inclusive)
ENV_END = 0x001000           # reserved env region (exclusive)
# WS-B1 owns $000400-$000BFF; $000C00-$000FFF is WS-B2's (docs/WAVE4_BRIEF.md C4)
ENV_B1_END = 0x000C00        # exclusive
ENV_STATE = 0x000400         # HLE state block (8 bytes, see below)
ENV_CLOCK = 0x000408         # HLE clock: DateStamp state (8 bytes, see below)
ENV_LAUNCH = 0x000410        # CLI launch stub (12 bytes)
ENV_ARGS = 0x000420          # CLI argument string (32 bytes, NUL-terminated)
EXIT_ADDR = 0x000440         # $AFFF HALT word — final-RTS landing site
ENV_CLI_NAME = 0x000444      # BSTR command name buffer (60 bytes)
ENV_CLI = 0x000480           # struct CommandLineInterface (64 bytes)
ENV_PROCESS = 0x000A40       # struct Process (228 bytes, $A40-$B23)
FH_CONOUT = 0x000B30         # struct FileHandle, fh_Args = FH_KIND_CONOUT
FH_CONIN = 0x000B60          # struct FileHandle, fh_Args = FH_KIND_CONIN
FH_NIL = 0x000B90            # struct FileHandle, fh_Args = FH_KIND_NIL

# Library region (C4 amendment 2026-09-24): bases + full vector tables
ENV_LIBS = 0x0F0000          # first byte of the staged library area
LIBS_REGION_END = 0x0FFFFF   # last byte of the region (inclusive)
EXEC_BASE = 0x0F0400         # ExecBase; slots LVO -6..-828 below it
DOS_BASE = 0x0F0C00          # DOSBase; slots LVO -6..-1158 below it
PROG_END = _image.PROG_END   # last byte of the program load region (one source)

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
# HLE clock at ENV_CLOCK (big-endian, staged fresh for every run): the
# DateStamp the NEXT call starts from.  Every DateStamp call first advances
# it by one tick (carrying into minutes and days), then returns it.
#   +0 u32 ds_Days   +4 u16 ds_Minute (0-1439)   +6 u16 ds_Tick (0-2999)
CLOCK_SIZE = 8
TICKS_PER_SECOND = 50        # dos/dos.h TICKS_PER_SECOND

# --- AmigaOS structure offsets used by the HLE / vbcc startup ----------------
EXEC_VERSION = 40            # lib_Version (Kickstart 3.1): vc.lib >= 39 uses
                             # exec memory pools (CreatePool/AllocPooled...)
DOS_VERSION = 40             # OpenLibrary returns NULL for version > this
LVO_DEPTH = {                # deepest known LVO (bytes below the base)
    "exec.library": 828,     # -$33C
    "dos.library": 1158,     # -$486
}
NT_LIBRARY = 9
LN_NAME_OFF = 10
LIB_NEGSIZE_OFF = 16
LIB_POSSIZE_OFF = 18
LIB_VERSION_OFF = 20         # struct Library lib_Version (UWORD)
LIB_SIZE = 34                # sizeof(struct Library)
EXECBASE_SIZE = 632          # sizeof(struct ExecBase), V40
DOSBASE_SIZE = 70            # sizeof(struct DosLibrary)
EB_MAXLOCMEM_OFF = 62        # ExecBase MaxLocMem (end of chip memory)
EB_THISTASK_OFF = 276        # ExecBase ThisTask
EB_ATTNFLAGS_OFF = 296       # ExecBase AttnFlags (0 = plain 68000, no FPU)
MAX_LOC_MEM = 0x200000       # nominal 2 MB "chip" top (all guest RAM is alike)
NT_PROCESS = 13
LN_TYPE_OFF = 8
TC_SPLOWER_OFF = 0x3A
TC_SPUPPER_OFF = 0x3E
PR_STACKSIZE_OFF = 0x84
PR_CIS_OFF = 0x9C
PR_COS_OFF = 0xA0
PR_CLI_OFF = 0xAC
PR_CES_OFF = 0xE0
PROCESS_SIZE = 0xE4
CLI_COMMANDNAME_OFF = 0x10
CLI_DEFAULTSTACK_OFF = 0x34  # in longwords
CLI_SIZE = 64
CLI_NAME_BUF = 60            # BSTR length byte + name + NUL (startup writes it)
ENV_STACK_LOWER = 0x001000   # tc_SPLower: bottom of the guest stack region
# tc_SPUpper: the longword at [STACK_TOP] (the exit address the runner's
# A7 points at) is the top-most stack slot, so the stack ends past it.
ENV_STACK_UPPER = STACK_TOP + 4
STACK_SIZE = ENV_STACK_UPPER - ENV_STACK_LOWER   # pr_StackSize (bytes)

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
    "AllocVec": (-684, 0x0F),
    "FreeVec": (-690, 0x10),
    "StackSwap": (-732, 0x11),
}
DOS_FUNCS = {
    "Output": (-60, 0x02),
    "Write": (-48, 0x03),
    "Input": (-54, 0x04),
    "Read": (-42, 0x05),
    "IsInteractive": (-216, 0x06),
    "GetVar": (-906, 0x12),
    "DateStamp": (-192, 0x13),
    "SystemTagList": (-606, 0x14),
}
HLE_FN_COUNT = 0x15          # indices $00-$14 (mirrors hle.s HLE_FN_COUNT)
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
LIB_NAMES_ADDR = DOS_BASE + 72           # "exec.library\0", "dos.library\0"
LIBS_STAGED_END = LIB_NAMES_ADDR + 32    # exclusive end of the staged area


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


class _Area:
    """A contiguous guest byte range built in host memory, staged as ONE
    chunk (so every gap is staged as zeros); refuses overlapping writes."""

    def __init__(self, lo: int, hi: int) -> None:
        self.lo, self.buf = lo, bytearray(hi - lo)
        self.used = bytearray(hi - lo)

    def put(self, addr: int, data: bytes, what: str) -> None:
        off = addr - self.lo
        if off < 0 or off + len(data) > len(self.buf):
            raise ValueError(f"{what} at ${addr:06X} outside "
                             f"${self.lo:06X}-${self.lo + len(self.buf) - 1:06X}")
        if any(self.used[off:off + len(data)]):
            raise ValueError(f"{what} at ${addr:06X} overlaps another structure")
        self.buf[off:off + len(data)] = data
        self.used[off:off + len(data)] = b"\1" * len(data)

    def chunk(self) -> Chunk:
        return Chunk(self.lo, bytes(self.buf))


def _library(size: int, version: int, negsize: int, name_addr: int) -> bytes:
    lib = bytearray(size)
    lib[LN_TYPE_OFF] = NT_LIBRARY
    lib[LN_NAME_OFF:LN_NAME_OFF + 4] = _be32(name_addr)
    struct.pack_into(">HHH", lib, LIB_NEGSIZE_OFF, negsize, size, version)
    return lib


# struct ExecBase (V40) List headers: offset -> lh_Type.  MemList,
# ResourceList, DeviceList, IntrList, LibList, PortList, TaskReady,
# TaskWait, SemaphoreList, and the five SoftInts lists (16-byte
# SoftIntList, List first).  ex_MemHandlers (616) is a MinList.
EB_LISTS = {322: 10, 336: 8, 350: 3, 364: 2, 378: 9, 392: 4, 406: 1, 420: 1,
            532: 15, **{434 + 16 * i: 2 for i in range(5)}}
EB_MINLISTS = (616,)


def _empty_list(buf: bytearray, off: int, addr: int, ntype: int | None) -> None:
    """Empty (Min)List at buf[off] (guest address addr): lh_Head = &lh_Tail,
    lh_Tail = 0, lh_TailPred = &lh_Head (+ lh_Type for a full List)."""
    struct.pack_into(">III", buf, off, addr + 4, 0, addr)
    if ntype is not None:
        buf[off + 12] = ntype


def _execbase() -> bytes:
    eb = _library(EXECBASE_SIZE, EXEC_VERSION, LVO_DEPTH["exec.library"],
                  LIB_NAMES_ADDR)
    for off, ntype in EB_LISTS.items():
        _empty_list(eb, off, EXEC_BASE + off, ntype)
    for off in EB_MINLISTS:
        _empty_list(eb, off, EXEC_BASE + off, None)
    eb[EB_MAXLOCMEM_OFF:EB_MAXLOCMEM_OFF + 4] = _be32(MAX_LOC_MEM)
    eb[EB_THISTASK_OFF:EB_THISTASK_OFF + 4] = _be32(ENV_PROCESS)
    struct.pack_into(">H", eb, EB_ATTNFLAGS_OFF, 0)
    return bytes(eb)


def filehandle(kind: int) -> bytes:
    """44-byte struct FileHandle, all zero except fh_Args = *kind* (C2)."""
    fh = bytearray(FH_SIZE)
    fh[FH_ARGS_OFF:FH_ARGS_OFF + 4] = _be32(kind)
    return bytes(fh)


def _process() -> bytes:
    p = bytearray(PROCESS_SIZE)
    p[LN_TYPE_OFF] = NT_PROCESS
    p[TC_SPLOWER_OFF:TC_SPLOWER_OFF + 4] = _be32(ENV_STACK_LOWER)
    p[TC_SPUPPER_OFF:TC_SPUPPER_OFF + 4] = _be32(ENV_STACK_UPPER)
    p[PR_STACKSIZE_OFF:PR_STACKSIZE_OFF + 4] = _be32(STACK_SIZE)
    p[PR_CIS_OFF:PR_CIS_OFF + 4] = _be32(bptr(FH_CONIN))
    p[PR_COS_OFF:PR_COS_OFF + 4] = _be32(bptr(FH_CONOUT))
    p[PR_CLI_OFF:PR_CLI_OFF + 4] = _be32(bptr(ENV_CLI))
    # pr_CES stays 0: vbcc's startup then uses Output() for stderr
    return bytes(p)


def _cli() -> bytes:
    c = bytearray(CLI_SIZE)
    c[CLI_COMMANDNAME_OFF:CLI_COMMANDNAME_OFF + 4] = _be32(bptr(ENV_CLI_NAME))
    c[CLI_DEFAULTSTACK_OFF:CLI_DEFAULTSTACK_OFF + 4] = _be32(STACK_SIZE // 4)
    return bytes(c)


def _bstr_name(name: bytes) -> bytes:
    if len(name) > CLI_NAME_BUF - 2:
        raise ValueError(f"command name longer than {CLI_NAME_BUF - 2} bytes")
    buf = bytes([len(name)]) + name
    return buf + b"\0" * (CLI_NAME_BUF - len(buf))


def state_block(heap_next: int = HEAP_BASE, cursor: int = 0,
                column: int = 0) -> bytes:
    return struct.pack(">IHBB", heap_next, cursor, column, 0)


def _library_tables() -> Chunk:
    """ExecBase + DOSBase with every vector slot down to LVO_DEPTH, the
    library name strings and all padding, as one chunk starting at
    ENV_LIBS."""
    area = _Area(ENV_LIBS, LIBS_STAGED_END)
    names = b"exec.library\0".ljust(16, b"\0") + b"dos.library\0".ljust(16, b"\0")
    area.put(LIB_NAMES_ADDR, names, "library names")
    area.put(EXEC_BASE, _execbase(), "ExecBase")
    area.put(DOS_BASE, _library(DOSBASE_SIZE, DOS_VERSION,
                                LVO_DEPTH["dos.library"], LIB_NAMES_ADDR + 16),
             "DOSBase")
    for lib, (base, funcs) in LIBRARIES.items():
        depth = LVO_DEPTH[lib]
        claimed = {}
        for name, (lvo, index) in funcs.items():
            if lvo >= 0 or -lvo > depth or lvo % SLOT_SIZE:
                raise ValueError(f"{lib}/{name}: LVO {lvo} not a slot of the "
                                 f"{depth}-byte table")
            claimed[lvo] = slot_word(index)
        table = bytearray()
        for lvo in range(-depth, 0, SLOT_SIZE):          # low address first
            table += struct.pack(">H", claimed.get(lvo, OP_LVO_UNIMPL))
            table += b"\0" * (SLOT_SIZE - 2)
        area.put(base - depth, bytes(table), f"{lib} vector table")
    return area.chunk()


def check_program_fits(manifest: dict) -> None:
    """Raise ValueError if a laid-out program reaches past the load region
    ($010000-PROG_END) into the library tables (host/image.py does not
    bound the region; drivers call this before staging)."""
    for seg in manifest["segments"]:
        end = seg["guest_addr"] + seg["size"] - 1
        if end > PROG_END:
            raise ValueError(
                f"{seg['kind']} segment ${seg['guest_addr']:06X}-${end:06X} "
                f"exceeds the program load region (ends ${PROG_END:06X})")


def clock_block(days: int = 0, minute: int = 0, tick: int = 0) -> bytes:
    if not (0 <= minute < 1440 and 0 <= tick < 60 * TICKS_PER_SECOND):
        raise ValueError(f"bad DateStamp minute={minute} tick={tick}")
    return struct.pack(">IHH", days, minute, tick)


def env_chunks(stack_top: int = STACK_TOP,
               command_name: bytes = b"prog", entry: int | None = None,
               args: bytes = b"\n") -> list[Chunk]:
    """Image chunks for the complete HLE environment.

    The returned chunks do not overlap each other, nor a program image
    that fits the load region (check it with :func:`check_program_fits`),
    so their order relative to the program's chunks is irrelevant.  They
    DO overlap :func:`launch_chunks` output and anything else staged into
    the env/library areas (e.g. a test's own records): stage such chunks
    AFTER these, or pass *entry* here.  *stack_top* must match the A7 the driver passes to
    ``test_run_program``.  Staging them re-initialises all HLE state
    (heap pointer, console cursor) for the next run.  The WS-B1 env part
    ($000400-$000BFF) and the library area are each one gap-free chunk.
    With *entry*, the CLI launch stub + *args* (:func:`launch_code`) are
    built into the env chunk; start the program at ``ENV_LAUNCH``.
    """
    env = _Area(ENV_BASE, ENV_B1_END)
    env.put(ENV_STATE, state_block(), "HLE state")
    env.put(ENV_CLOCK, clock_block(), "HLE clock")
    env.put(EXIT_ADDR, struct.pack(">H", OP_HALT), "exit stub")
    env.put(ENV_CLI_NAME, _bstr_name(command_name), "CLI name")
    env.put(ENV_CLI, _cli(), "CLI")
    env.put(ENV_PROCESS, _process(), "Process")
    env.put(FH_CONOUT, filehandle(FH_KIND_CONOUT), "FH_CONOUT")
    env.put(FH_CONIN, filehandle(FH_KIND_CONIN), "FH_CONIN")
    env.put(FH_NIL, filehandle(FH_KIND_NIL), "FH_NIL")
    if entry is not None:
        code, argbuf = launch_code(entry, args)
        env.put(ENV_LAUNCH, code, "launch stub")
        env.put(ENV_ARGS, argbuf, "CLI args")
    return [
        Chunk(ABSEXECBASE_ADDR, struct.pack(">I", EXEC_BASE)),
        env.chunk(),
        _library_tables(),
        Chunk(stack_top, struct.pack(">I", EXIT_ADDR)),
    ]


LAUNCH_SIZE = 12
ARGS_MAX = 31                # ENV_ARGS buffer is 32 bytes incl. the NUL


def launch_code(entry: int, args: bytes = b"\n") -> tuple[bytes, bytes]:
    """(stub code, argument buffer) for the CLI launch stub, see
    :func:`launch_chunks`."""
    if len(args) > ARGS_MAX:
        raise ValueError(f"args longer than {ARGS_MAX} bytes")
    code = struct.pack(">HHHHI", 0x41F8, ENV_ARGS, 0x7000 | len(args),
                       0x4EF9, entry)
    assert len(code) == LAUNCH_SIZE
    return code, args + b"\0" * (ARGS_MAX + 1 - len(args))


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
    These chunks lie inside the env chunk's range; prefer
    ``env_chunks(entry=...)``, or stage these AFTER :func:`env_chunks`.
    """
    code, argbuf = launch_code(entry, args)
    return [Chunk(ENV_LAUNCH, code), Chunk(ENV_ARGS, argbuf)], ENV_LAUNCH
