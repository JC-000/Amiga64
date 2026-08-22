"""Binary test-case format per docs/CONTRACTS.md, section "Test vector pipeline".

Batch file layout (all little-endian):
    u16 case_count
    case_count x case record

Case record layout (offsets relative to the start of the record):
    +0    u16 case_len      total record length INCLUDING this field
    +2    16 x u32          D0-D7, A0-A7 (initial)
    +66   u32               PC (initial)
    +70   u16               SR (initial; low byte = CCR)
    +72   u8  n_init
    +73   n x {u24 addr, u8 val}   initial guest memory bytes
    ...   u8  n_exp
    ...   m x {u24 addr, u8 val}   expected guest memory bytes
    ...   16 x u32          expected D0-D7, A0-A7
    ...   u32               expected PC
    ...   u16               expected SR

Total record size: 144 + 4*n_init + 4*n_exp bytes; must fit buf_case (2048).

INTEGRATOR NOTE: host/vectors.py (agent B-vectors) is the canonical encoder of
this format. This module intentionally duplicates the format definition so the
tests/ tree is self-contained (it also needs the encoder for hand-written stub
cases). If host/vectors.py grows a public decode API, tests/run_vectors.py can
switch to it; until then keep the two in sync with CONTRACTS.md.

The decoder parses records structurally (field-by-field) and only validates
case_len, accepting both the "includes the 2-byte length field" (canonical)
and "excludes it" interpretations; `Case.case_len_excludes_header` records
which one a decoded record used.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAX_CASE_BYTES = 2048  # buf_case ($C000-$C7FF) holds one record at a time

REG_NAMES = tuple(f"D{i}" for i in range(8)) + tuple(f"A{i}" for i in range(8))


@dataclass
class Case:
    regs: list[int]                    # 16 x u32, D0-D7 then A0-A7
    pc: int
    sr: int
    init_mem: list[tuple[int, int]]    # (24-bit guest addr, byte)
    exp_mem: list[tuple[int, int]]
    exp_regs: list[int]
    exp_pc: int
    exp_sr: int
    raw: bytes = b""                   # encoded record, as written to buf_case
    case_len_excludes_header: bool = field(default=False, repr=False)

    def opcode(self) -> int | None:
        """Best-effort opcode word: big-endian bytes at PC, PC+1 in init_mem."""
        mem = dict(self.init_mem)
        hi = mem.get(self.pc & 0xFFFFFF)
        lo = mem.get((self.pc + 1) & 0xFFFFFF)
        if hi is None or lo is None:
            return None
        return (hi << 8) | lo


def encode_case(c: Case) -> bytes:
    body = bytearray()
    body += struct.pack("<16I", *c.regs)
    body += struct.pack("<IH", c.pc, c.sr)
    for pairs in (c.init_mem, c.exp_mem):
        if len(pairs) > 255:
            raise ValueError(f"too many mem pairs ({len(pairs)} > 255)")
        body.append(len(pairs))
        for addr, val in pairs:
            body += (addr & 0xFFFFFF).to_bytes(3, "little")
            body.append(val & 0xFF)
    body += struct.pack("<16I", *c.exp_regs)
    body += struct.pack("<IH", c.exp_pc, c.exp_sr)
    rec = struct.pack("<H", len(body) + 2) + bytes(body)
    if len(rec) > MAX_CASE_BYTES:
        raise ValueError(f"case record {len(rec)} bytes exceeds buf_case ({MAX_CASE_BYTES})")
    return rec


def encode_batch(cases: list[Case]) -> bytes:
    return struct.pack("<H", len(cases)) + b"".join(encode_case(c) for c in cases)


def _parse_case(data: bytes, off: int) -> tuple[Case, int]:
    start = off
    (case_len,) = struct.unpack_from("<H", data, off)
    off += 2
    regs = list(struct.unpack_from("<16I", data, off))
    off += 64
    pc, sr = struct.unpack_from("<IH", data, off)
    off += 6
    pairs: list[list[tuple[int, int]]] = []
    for _ in range(2):
        n = data[off]
        off += 1
        lst = []
        for _ in range(n):
            addr = int.from_bytes(data[off:off + 3], "little")
            lst.append((addr, data[off + 3]))
            off += 4
        pairs.append(lst)
    exp_regs = list(struct.unpack_from("<16I", data, off))
    off += 64
    exp_pc, exp_sr = struct.unpack_from("<IH", data, off)
    off += 6
    size = off - start
    if case_len == size:
        excludes = False
    elif case_len == size - 2:
        excludes = True
    else:
        raise ValueError(
            f"case_len {case_len} at offset {start} inconsistent with structural size {size}"
        )
    return Case(regs, pc, sr, pairs[0], pairs[1], exp_regs, exp_pc, exp_sr,
                raw=bytes(data[start:off]), case_len_excludes_header=excludes), off


def decode_batch(data: bytes) -> list[Case]:
    (count,) = struct.unpack_from("<H", data, 0)
    off = 2
    cases = []
    for i in range(count):
        try:
            case, off = _parse_case(data, off)
        except (struct.error, IndexError) as e:
            raise ValueError(f"truncated batch while parsing case {i}: {e}") from e
        cases.append(case)
    if off != len(data):
        raise ValueError(f"{len(data) - off} trailing bytes after {count} cases")
    return cases
