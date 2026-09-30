"""M1 golden-vector pipeline: SingleStepTests 680x0 -> binary batches.

Fetches 68000 single-instruction test files (JSON, gzipped) from the
SingleStepTests/680x0 repository pinned at ``UPSTREAM_COMMIT``, filters them to
the architectural user-mode subset defined in docs/CONTRACTS.md ("Test vector
pipeline"), samples N cases per file deterministically, vendors the sampled
subset into ``fixtures/vectors/`` for offline use, and compiles each vendored
file into the binary batch format consumed by the C64-side runner.

CLI::

    python3 -m host.vectors --out build/vectors [--offline] [--n 50]
                            [--only STEM,STEM] [--refresh]

Upstream JSON semantics (68000/v1/README.md at the pinned commit):

* ``initial.pc`` is the *formal* PC: the address of the instruction under
  test.  ``final.pc`` is likewise the address of the next instruction.  No
  pre-adjustment for the hardware prefetch offset is stored, so a naive
  fetch-from-memory interpreter can use these values directly.
* ``initial.prefetch`` holds the two words already in the prefetch queue:
  the word at ``pc`` (the opcode) and the word at ``pc+2``.  The ``ram``
  list normally starts at ``pc+4``; the opcode and first extension word
  exist *only* in the prefetch queue.  We therefore materialize
  ``prefetch[0]`` at ``pc`` and ``prefetch[1]`` at ``pc+2`` (big-endian)
  as initial memory.  Cases whose ``ram`` list disagrees with those four
  bytes are dropped ("prefetch-conflict") because a memory-fetching
  interpreter cannot reproduce them.

SR normalization (documented per contract):

* Retained initial and expected SR are both reduced to ``sr & 0x001F``:
  supervisor bit, trace bit and interrupt mask are cleared, the CCR
  (X/N/Z/V/C) is preserved.  Bits 5-7 of the CCR byte are always 0 on a
  68000, so masking with 0x1F loses nothing.
* The guest register file has a single A7.  A7 is the stack pointer that
  was *active* during the test: ``ssp`` when the case started in
  supervisor mode (99% of upstream cases), else ``usp``.  The inactive
  pointer is discarded; cases where the inactive pointer changes are
  dropped (nothing in the M1 subset can legally touch it).

Binary batch format (little-endian, per docs/CONTRACTS.md):

* header: ``u16 case_count``
* per case: ``u16 case_len`` (byte length of the whole case record,
  including this field, so ``next = offset + case_len``), 16 x ``u32``
  D0-D7/A0-A7, ``u32`` PC, ``u16`` SR, ``u8 n_init_mem``, n x {``u24``
  addr, ``u8`` val}, ``u8 n_exp_mem``, m x {``u24`` addr, ``u8`` val},
  16 x ``u32`` expected regs, ``u32`` expected PC, ``u16`` expected SR.
"""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Sequence

# --- upstream pin -----------------------------------------------------------

UPSTREAM_REPO = "SingleStepTests/680x0"
UPSTREAM_COMMIT = "e0d5ece9670205cc84a0101081837deb446f86a3"
RAW_BASE = (
    f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}"
)
SUITE_PATH = "68000/v1"

SAMPLE_SEED = 0xA5000  # fixed; sampling must be deterministic
DEFAULT_SAMPLE_N = 50
MAX_CASE_BYTES = 2048  # buf_case size; larger cases are dropped

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "vectors"
MANIFEST_NAME = "MANIFEST.json"
LICENSE_NAME = "UPSTREAM_LICENSE"

# M1 subset (upstream file stems).  Upstream buckets opcodes by *operation*:
# ADDI/ADDQ live in ADD.*, SUBI/SUBQ in SUB.*, CMPI/CMPM in CMP.*, ANDI in
# AND.*, ORI in OR.*, EORI in EOR.*, BRA in Bcc, MOVEQ is MOVE.q.
M1_FILES: tuple[str, ...] = (
    "MOVE.b", "MOVE.w", "MOVE.l", "MOVE.q", "MOVEA.w", "MOVEA.l",
    "LEA", "PEA",
    "ADD.b", "ADD.w", "ADD.l", "ADDA.w", "ADDA.l",
    "ADDX.b", "ADDX.w", "ADDX.l",
    "SUB.b", "SUB.w", "SUB.l", "SUBA.w", "SUBA.l",
    "SUBX.b", "SUBX.w", "SUBX.l",
    "CMP.b", "CMP.w", "CMP.l", "CMPA.w", "CMPA.l",
    "NEG.b", "NEG.w", "NEG.l", "NEGX.b", "NEGX.w", "NEGX.l",
    "AND.b", "AND.w", "AND.l", "OR.b", "OR.w", "OR.l",
    "EOR.b", "EOR.w", "EOR.l", "NOT.b", "NOT.w", "NOT.l",
    "TST.b", "TST.w", "TST.l", "CLR.b", "CLR.w", "CLR.l",
    "BTST", "BSET", "BCLR", "BCHG",
    "Scc",
    "ASL.b", "ASL.w", "ASL.l", "ASR.b", "ASR.w", "ASR.l",
    "LSL.b", "LSL.w", "LSL.l", "LSR.b", "LSR.w", "LSR.l",
    "ROL.b", "ROL.w", "ROL.l", "ROR.b", "ROR.w", "ROR.l",
    "ROXL.b", "ROXL.w", "ROXL.l", "ROXR.b", "ROXR.w", "ROXR.l",
    "Bcc", "BSR", "JMP", "JSR", "RTS", "RTR", "DBcc",
    "SWAP", "EXT.w", "EXT.l", "EXG",
    "MULU", "MULS", "DIVU", "DIVS",
    "MOVEM.w", "MOVEM.l",
    "NOP",
    "ABCD", "SBCD", "NBCD",
)

# Supervisor-mode / exception set (W6-S, docs/CONTRACTS.md "Supervisor mode &
# exceptions").  Compiled with ``--set super`` into a SEPARATE batch
# directory (build/vectors-super) in the extended super format below and
# run by tests/run_super_vectors.py with the exception flag set; the golden
# M1 set above is untouched.  ``<stem>.exc`` vendors only the exception
# cases of an upstream file whose non-exception cases already live in the
# M1 set (DIVU: the single divide-by-zero case at the pinned commit).
SUPER_FILES: tuple[str, ...] = (
    "ANDItoSR", "ORItoSR", "EORItoSR", "MOVEtoSR", "MOVEfromSR",
    "MOVEtoUSP", "MOVEfromUSP", "RTE", "RESET",
    "TRAP", "TRAPV", "CHK", "DIVU.exc",
)
EXC_SUFFIX = ".exc"

# SR bits
SR_TRACE = 0x8000
SR_SUPER = 0x2000
SR_SYSTEM_MASK = 0xA700  # trace | supervisor | interrupt mask
CCR_MASK = 0x001F

ADDR_MASK = 0xFFFFFF  # 24-bit guest bus

# Guest banks that are chip registers, not RAM (W7-T I/O trap,
# docs/CONTRACTS.md "I/O trap ($BF/$DF)"): $BF CIAs, $DF custom chips.
IO_BANKS = (0xBF, 0xDF)
IO_BANK_REASON = "io-bank"


# --- download ---------------------------------------------------------------

def _http_get(url: str, retries: int = 3) -> bytes:
    last: Exception | None = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Amiga64-vector-pipeline"}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
    raise RuntimeError(f"failed to fetch {url}: {last}")


def upstream_stem(stem: str) -> str:
    """Upstream file a fixture stem is cut from (``DIVU.exc`` -> ``DIVU``)."""
    return stem[:-len(EXC_SUFFIX)] if stem.endswith(EXC_SUFFIX) else stem


def fetch_upstream_cases(stem: str) -> list[dict[str, Any]]:
    """Download and parse one upstream JSON test file (e.g. ``"ADD.b"``)."""
    raw = _http_get(f"{RAW_BASE}/{SUITE_PATH}/{upstream_stem(stem)}.json.gz")
    return json.loads(gzip.decompress(raw))


# --- filtering --------------------------------------------------------------

def classify(case: dict[str, Any]) -> str | None:
    """Return a drop-reason string, or None if the case is retained.

    Exception detection: a 68000 exception always (a) pushes a 3-word
    (group 1/2) or 7-word (group 0 / address error) stack frame just below
    the initial SSP with supervisor function codes, and (b) reads the
    two-word exception vector from the vector table ($008-$0BF for vectors
    2-47).  We require both signatures.  For the rare user-mode-start
    cases, any bus cycle with a supervisor function code is decisive on
    its own.  Vendored fixtures omit ``transactions``; they were filtered
    before vendoring, so the remaining checks still apply.
    """
    init = case["initial"]
    fin = case["final"]
    sr0: int = init["sr"]
    sr1: int = fin["sr"]

    if sr0 & SR_TRACE:
        return "trace"
    if (sr0 ^ sr1) & SR_SYSTEM_MASK:
        # supervisor / trace / interrupt-mask changed: exception (or RTE-like
        # behavior) that the M1 user-mode subset cannot represent.
        return "sr-system-change"
    if init["pc"] & 1:
        return "odd-pc"

    supervisor = bool(sr0 & SR_SUPER)
    if supervisor:
        if fin["usp"] != init["usp"]:
            return "inactive-sp-change"
    else:
        if fin["ssp"] != init["ssp"]:
            return "inactive-sp-change"

    transactions = case.get("transactions")
    if transactions:
        ssp: int = init["ssp"]
        frame_writes = 0
        low_word_reads: set[int] = set()
        for t in transactions:
            if t[0] == "n":
                continue
            kind, _cycles, fc, addr, width = t[0], t[1], t[2], t[3], t[4]
            if not supervisor and (fc & 4):
                return "exception"  # user-mode start: any supervisor cycle
            if kind == "w" and width == ".w" and 2 <= ssp - addr <= 14:
                frame_writes += 1
            if kind == "r" and width == ".w" and 8 <= addr < 192:
                low_word_reads.add(addr)
        vector_pair = any(
            a % 4 == 0 and a + 2 in low_word_reads
            for a in low_word_reads
        )
        if frame_writes >= 3 and vector_pair:
            return "exception"

    if _prefetch_conflict(case):
        return "prefetch-conflict"
    return None


def _opcode_bytes(init: dict[str, Any]) -> dict[int, int]:
    """The 4 instruction-stream bytes defined by the prefetch queue."""
    pc = init["pc"] & ADDR_MASK
    w0, w1 = init["prefetch"]
    return {
        pc: (w0 >> 8) & 0xFF,
        (pc + 1) & ADDR_MASK: w0 & 0xFF,
        (pc + 2) & ADDR_MASK: (w1 >> 8) & 0xFF,
        (pc + 3) & ADDR_MASK: w1 & 0xFF,
    }


def _prefetch_conflict(case: dict[str, Any]) -> bool:
    init = case["initial"]
    op = _opcode_bytes(init)
    for addr, val in init["ram"]:
        a = addr & ADDR_MASK
        if a in op and val != op[a]:
            return True
    return False


# --- normalization ----------------------------------------------------------

REG_NAMES = ("d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7",
             "a0", "a1", "a2", "a3", "a4", "a5", "a6")


def _regs(state: dict[str, Any], supervisor: bool) -> list[int]:
    regs = [state[n] & 0xFFFFFFFF for n in REG_NAMES]
    regs.append((state["ssp"] if supervisor else state["usp"]) & 0xFFFFFFFF)
    return regs


def normalize(case: dict[str, Any]) -> dict[str, Any]:
    """Reduce a retained upstream case to architectural user-mode state.

    Returns ``{name, regs, pc, sr, init_mem, exp_regs, exp_pc, exp_sr,
    exp_mem}`` where regs are ``[D0..D7, A0..A6, A7]`` (A7 = the active
    stack pointer), SRs are CCR-only (``& 0x1F``) and mem lists are sorted
    ``(addr, value)`` pairs.  ``init_mem`` is the upstream ram list with
    the prefetch queue materialized at ``pc..pc+3``; ``exp_mem`` is
    ``init_mem`` overlaid with the upstream final ram list, i.e. it also
    asserts that unwritten initial bytes (opcode stream included) are
    unchanged.
    """
    init = case["initial"]
    fin = case["final"]
    supervisor = bool(init["sr"] & SR_SUPER)

    init_mem: dict[int, int] = {}
    for addr, val in init["ram"]:
        init_mem[addr & ADDR_MASK] = val & 0xFF
    init_mem.update(_opcode_bytes(init))

    exp_mem = dict(init_mem)
    for addr, val in fin["ram"]:
        exp_mem[addr & ADDR_MASK] = val & 0xFF

    return {
        "name": case["name"],
        "regs": _regs(init, supervisor),
        "pc": init["pc"] & ADDR_MASK,
        "sr": init["sr"] & CCR_MASK,
        "init_mem": sorted(init_mem.items()),
        "exp_regs": _regs(fin, supervisor),
        "exp_pc": fin["pc"] & ADDR_MASK,
        "exp_sr": fin["sr"] & CCR_MASK,
        "exp_mem": sorted(exp_mem.items()),
    }


def case_bin_size(norm: dict[str, Any]) -> int:
    return 144 + 4 * (len(norm["init_mem"]) + len(norm["exp_mem"]))


# --- sampling ---------------------------------------------------------------

def touches_io_bank(case: dict[str, Any]) -> bool:
    """True when an upstream case has initial or final RAM (which lists
    every byte the instruction reads or writes, prefetch included) in an
    I/O bank ($BF/$DF), where the interpreter has chip registers instead
    of the flat RAM the vectors assume."""
    addrs = [a for a, _ in case["initial"]["ram"]]
    addrs += [a for a, _ in case["final"]["ram"]]
    pc = case["initial"]["pc"]
    addrs += [pc + i for i in range(4)]
    return any(((a & ADDR_MASK) >> 16) in IO_BANKS for a in addrs)


def exclude_io_bank(cases: Sequence[dict[str, Any]]
                    ) -> tuple[list[dict[str, Any]], int]:
    """POST-sample exclusion (supervisor ruling, Wave 7): drop whole cases
    that touch an I/O bank, never re-sample, so every other sampled case
    stays exactly the one validated before the trap existed."""
    kept = [c for c in cases if not touches_io_bank(c)]
    return kept, len(cases) - len(kept)


def stride_sample(items: Sequence[Any], n: int, seed: int = SAMPLE_SEED
                  ) -> list[Any]:
    """Deterministic fixed-stride sample of ``n`` items in original order."""
    if n <= 0 or len(items) <= n:
        return list(items)
    stride = len(items) // n
    start = seed % stride
    return [items[start + i * stride] for i in range(n)]


def filter_and_sample(cases: Iterable[dict[str, Any]], n: int,
                      seed: int = SAMPLE_SEED, stem: str | None = None
                      ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter, then stride-sample the retained pool, then drop the sampled
    cases that touch an I/O bank (``exclude_io_bank``: post-sample, no
    re-sample); returns (cases, stats).

    ``stem`` selects the filter: a SUPER_FILES stem uses classify_super
    (and the smaller super size limit), anything else the M1 classify.
    """
    sup = stem in SUPER_FILES
    retained: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    total = 0
    for case in cases:
        total += 1
        if sup:
            reason = classify_super(case, exc_only=stem.endswith(EXC_SUFFIX))
            if reason is None and (super_case_bin_size(normalize_super(case))
                                   > MAX_SUPER_CASE_BYTES):
                reason = "too-big"
        else:
            reason = classify(case)
            if reason is None and (case_bin_size(normalize(case))
                                   > MAX_CASE_BYTES):
                reason = "too-big"
        if reason is None:
            retained.append(case)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    sampled, n_io = exclude_io_bank(stride_sample(retained, n, seed))
    stats = {
        "total": total,
        "retained_pool": len(retained),
        "sampled": len(sampled),
        "dropped": dict(sorted(dropped.items())),
    }
    if n_io:
        stats["dropped_after_sampling"] = {IO_BANK_REASON: n_io}
    return sampled, stats


# --- vendoring --------------------------------------------------------------

def _strip(case: dict[str, Any]) -> dict[str, Any]:
    """Keep only name/initial/final (drop cycle counts and bus detail)."""
    return {
        "name": case["name"],
        "initial": case["initial"],
        "final": case["final"],
    }


def _deterministic_gz(payload: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()


def encode_fixture(stem: str, cases: Sequence[dict[str, Any]],
                   sample_n: int) -> bytes:
    doc = {
        "commit": UPSTREAM_COMMIT,
        "source": f"{SUITE_PATH}/{upstream_stem(stem)}.json.gz",
        "sample_seed": SAMPLE_SEED,
        "sample_n": sample_n,
        "cases": [_strip(c) for c in cases],
    }
    payload = json.dumps(doc, sort_keys=True,
                         separators=(",", ":")).encode("ascii")
    return _deterministic_gz(payload)


def fixture_path(stem: str, fixtures_dir: Path = FIXTURES_DIR) -> Path:
    return fixtures_dir / f"{stem}.json.gz"


def load_fixture(stem: str, fixtures_dir: Path = FIXTURES_DIR
                 ) -> list[dict[str, Any]]:
    with gzip.open(fixture_path(stem, fixtures_dir)) as f:
        doc = json.load(f)
    return doc["cases"]


def vendor_file(stem: str, n: int = DEFAULT_SAMPLE_N,
                fixtures_dir: Path = FIXTURES_DIR) -> dict[str, int]:
    """Download one upstream file, filter+sample, write the fixture."""
    cases = fetch_upstream_cases(stem)
    sampled, stats = filter_and_sample(cases, n, stem=stem)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixture_path(stem, fixtures_dir).write_bytes(
        encode_fixture(stem, sampled, n))
    return stats


def write_license(fixtures_dir: Path = FIXTURES_DIR) -> None:
    root_readme = _http_get(f"{RAW_BASE}/README.md").decode("utf-8")
    suite_readme = _http_get(
        f"{RAW_BASE}/{SUITE_PATH}/README.md").decode("utf-8")
    text = (
        "Upstream attribution for fixtures/vectors/*.json.gz\n"
        "===================================================\n\n"
        f"Source repository: https://github.com/{UPSTREAM_REPO}\n"
        f"Pinned commit:     {UPSTREAM_COMMIT}\n\n"
        "The repository contains no LICENSE file at the pinned commit; the\n"
        "test sets are published \"as an aid to reimplementation\" (see the\n"
        "README below).  The files vendored here are filtered, subsampled\n"
        "derivatives of the upstream JSON test sets, with bus/cycle detail\n"
        "removed.\n\n"
        "----- upstream README.md -----\n\n"
        + root_readme +
        "\n----- upstream 68000/v1/README.md -----\n\n"
        + suite_readme
    )
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    (fixtures_dir / LICENSE_NAME).write_text(text, encoding="utf-8")


# --- binary batch compiler / decoder ---------------------------------------

def _pack_u24(addr: int) -> bytes:
    return struct.pack("<I", addr & ADDR_MASK)[:3]


def pack_case(norm: dict[str, Any]) -> bytes:
    init_mem = norm["init_mem"]
    exp_mem = norm["exp_mem"]
    if len(init_mem) > 255 or len(exp_mem) > 255:
        raise ValueError(f"too many mem pairs in {norm['name']!r}")
    case_len = case_bin_size(norm)
    out = bytearray()
    out += struct.pack("<H", case_len)
    out += struct.pack("<16I", *norm["regs"])
    out += struct.pack("<IH", norm["pc"], norm["sr"])
    out += struct.pack("<B", len(init_mem))
    for addr, val in init_mem:
        out += _pack_u24(addr) + struct.pack("<B", val)
    out += struct.pack("<B", len(exp_mem))
    for addr, val in exp_mem:
        out += _pack_u24(addr) + struct.pack("<B", val)
    out += struct.pack("<16I", *norm["exp_regs"])
    out += struct.pack("<IH", norm["exp_pc"], norm["exp_sr"])
    assert len(out) == case_len
    return bytes(out)


def compile_batch(cases: Sequence[dict[str, Any]]) -> bytes:
    """Compile vendored (raw upstream-shaped) cases into one binary batch."""
    blobs = []
    for case in cases:
        reason = classify(case)
        if reason is not None:
            raise ValueError(
                f"unfiltered case {case['name']!r} in fixture: {reason}")
        blobs.append(pack_case(normalize(case)))
    return struct.pack("<H", len(blobs)) + b"".join(blobs)


def decode_batch(data: bytes) -> list[dict[str, Any]]:
    """Parse a binary batch back into normalized-case dicts (sans names)."""
    (count,) = struct.unpack_from("<H", data, 0)
    off = 2
    cases: list[dict[str, Any]] = []
    for _ in range(count):
        (case_len,) = struct.unpack_from("<H", data, off)
        p = off + 2
        regs = list(struct.unpack_from("<16I", data, p)); p += 64
        pc, sr = struct.unpack_from("<IH", data, p); p += 6
        (n_init,) = struct.unpack_from("<B", data, p); p += 1
        init_mem = []
        for _i in range(n_init):
            addr = int.from_bytes(data[p:p + 3], "little")
            init_mem.append((addr, data[p + 3])); p += 4
        (n_exp,) = struct.unpack_from("<B", data, p); p += 1
        exp_mem = []
        for _i in range(n_exp):
            addr = int.from_bytes(data[p:p + 3], "little")
            exp_mem.append((addr, data[p + 3])); p += 4
        exp_regs = list(struct.unpack_from("<16I", data, p)); p += 64
        exp_pc, exp_sr = struct.unpack_from("<IH", data, p); p += 6
        if p - off != case_len:
            raise ValueError(
                f"case_len mismatch: header {case_len}, parsed {p - off}")
        cases.append({
            "regs": regs, "pc": pc, "sr": sr, "init_mem": init_mem,
            "exp_regs": exp_regs, "exp_pc": exp_pc, "exp_sr": exp_sr,
            "exp_mem": exp_mem,
        })
        off += case_len
    if off != len(data):
        raise ValueError(f"trailing bytes in batch: {len(data) - off}")
    return cases


# --- supervisor / exception set (W6-S) --------------------------------------
#
# Super batch format (tests/run_super_vectors.py): ``u16 case_count``, then
# per case the standard case record (pack_case: SR is the FULL 16-bit SR,
# A7 the stack pointer active in that SR) immediately followed by a fixed
# SUPER_EXT_LEN-byte extension:
#
#   u32 osp          inactive stack pointer before the step (USP when S=1,
#                    else SSP) -> ZP r_osp
#   u32 exp_osp      expected inactive stack pointer after the step (w.r.t.
#                    the FINAL S bit)
#   u8  flags        bit 0: run with the exception flag st_exc set
#   u8  exp_halt     expected st_halt (0 = retires normally)
#   u8  exp_reason   expected st_reason (checked only when exp_halt != 0)
#   u8  pad
#
# The driver writes the standard record to buf_case and the extension to
# SUPER_EXT_ADDR (the last 16 bytes of buf_case), so a super record may use
# at most MAX_SUPER_CASE_BYTES.

SR_IMPL_MASK = 0xA71F        # bits a 68000 SR can hold: T, S, I2-I0, XNZVC
SUPER_EXT_LEN = 12
SUPER_EXT_AREA = 16
MAX_SUPER_CASE_BYTES = MAX_CASE_BYTES - SUPER_EXT_AREA
FLAG_EXC = 0x01
SUPER_HAND_STEM = "SUPERHAND"

# Halt reasons (runtime/macros.inc + runtime/core.s; mirrored for the
# hand-authored expectations).
HALT_OK = 0x00
HALT_ALINE = 0x01
HALT_FLINE = 0x02
HALT_ILLEGAL = 0x03
HALT_STOP = 0x04
HALT_ADDRESS = 0x05

ADDRESS_ERROR_VECTORS = (2, 3)  # bus / address error: reason code only


def exception_vector(case: dict[str, Any]) -> int | None:
    """Vector number of the exception a case takes, from its bus trace.

    The signature is the two-word vector fetch from the table at 0 whose
    value is the final PC, immediately followed by the first handler
    prefetch at that PC.  Independent of the stack pointer (so it also
    catches frames below a post-incremented SSP, e.g. ``CHK (A7)+``).
    Returns None for cases without transactions (vendored fixtures).
    """
    fpc = case["final"]["pc"] & ADDR_MASK
    tr = [t for t in case.get("transactions") or () if t[0] != "n"]
    for j in range(len(tr) - 2):
        a, b, c = tr[j], tr[j + 1], tr[j + 2]
        if (a[0] == "r" and b[0] == "r" and a[4] == ".w" and b[4] == ".w"
                and a[3] < 0x400 and a[3] % 4 == 0 and b[3] == a[3] + 2
                and ((a[5] << 16) | b[5]) & ADDR_MASK == fpc
                and c[0] == "r" and c[3] & ADDR_MASK == fpc):
            return a[3] // 4
    return None


def classify_super(case: dict[str, Any], exc_only: bool = False
                   ) -> str | None:
    """Drop reason for a super-set case, or None if retained.

    Everything architectural is kept (supervisor state, both stack
    pointers, exceptions); dropped are address/bus-error cases (outside
    C4), a set trace bit at the start (tracing is not modelled), odd PCs
    and prefetch conflicts.  ``exc_only`` (``<stem>.exc``) additionally
    drops cases that take no exception.  Transaction-based checks apply
    at vendoring time only.
    """
    init = case["initial"]
    if init["sr"] & SR_TRACE:
        return "trace"
    if init["pc"] & 1:
        return "odd-pc"
    if case.get("transactions"):
        vec = exception_vector(case)
        if vec in ADDRESS_ERROR_VECTORS:
            return "address-error"
        if exc_only and vec is None:
            return "no-exception"
    if _prefetch_conflict(case):
        return "prefetch-conflict"
    return None


def normalize_super(case: dict[str, Any]) -> dict[str, Any]:
    """normalize() with the full SR and both stack pointers kept."""
    norm = normalize(case)
    init = case["initial"]
    fin = case["final"]
    s0 = bool(init["sr"] & SR_SUPER)
    s1 = bool(fin["sr"] & SR_SUPER)
    norm["regs"] = _regs(init, s0)
    norm["exp_regs"] = _regs(fin, s1)
    norm["sr"] = init["sr"] & 0xFFFF
    norm["exp_sr"] = fin["sr"] & 0xFFFF
    norm["osp"] = (init["ssp"] if not s0 else init["usp"]) & 0xFFFFFFFF
    norm["exp_osp"] = (fin["ssp"] if not s1 else fin["usp"]) & 0xFFFFFFFF
    norm["flags"] = FLAG_EXC
    norm["exp_halt"] = 0
    norm["exp_reason"] = 0
    return norm


def super_case_bin_size(norm: dict[str, Any]) -> int:
    return case_bin_size(norm) + SUPER_EXT_LEN


def pack_super_case(norm: dict[str, Any]) -> bytes:
    if super_case_bin_size(norm) - SUPER_EXT_LEN > MAX_SUPER_CASE_BYTES:
        raise ValueError(f"super case {norm.get('name')!r} too big")
    return pack_case(norm) + struct.pack(
        "<IIBBBB", norm["osp"], norm["exp_osp"], norm["flags"],
        norm["exp_halt"], norm["exp_reason"], 0)


def compile_super_batch(cases: Sequence[dict[str, Any]],
                        exc_only: bool = False) -> bytes:
    blobs = []
    for case in cases:
        reason = classify_super(case, exc_only)
        if reason is not None:
            raise ValueError(
                f"unfiltered case {case['name']!r} in fixture: {reason}")
        blobs.append(pack_super_case(normalize_super(case)))
    return struct.pack("<H", len(blobs)) + b"".join(blobs)


def pack_super_norms(norms: Sequence[dict[str, Any]]) -> bytes:
    return struct.pack("<H", len(norms)) + b"".join(
        pack_super_case(n) for n in norms)


def decode_super_batch(data: bytes) -> list[dict[str, Any]]:
    """Parse a super batch into normalized dicts (+ ``raw`` record bytes)."""
    (count,) = struct.unpack_from("<H", data, 0)
    off = 2
    out: list[dict[str, Any]] = []
    for _ in range(count):
        (case_len,) = struct.unpack_from("<H", data, off)
        rec = data[off:off + case_len]
        (norm,) = decode_batch(struct.pack("<H", 1) + rec)
        ext = data[off + case_len:off + case_len + SUPER_EXT_LEN]
        if len(ext) != SUPER_EXT_LEN:
            raise ValueError("truncated super extension")
        (norm["osp"], norm["exp_osp"], norm["flags"], norm["exp_halt"],
         norm["exp_reason"], _pad) = struct.unpack("<IIBBBB", ext)
        norm["raw"] = bytes(rec)
        norm["ext"] = bytes(ext)
        out.append(norm)
        off += case_len + SUPER_EXT_LEN
    if off != len(data):
        raise ValueError(f"trailing bytes in super batch: {len(data) - off}")
    return out


# --- hand-authored super cases ----------------------------------------------
#
# Upstream has no privilege-violation, illegal, A-line, F-line or STOP
# cases, no exception taken from user mode, and exactly one divide-by-zero
# case.  These are authored here from the 68000 PRM, with every point the
# upstream data does pin taken from it (see docs/CONTRACTS.md "Supervisor
# mode & exceptions"): group-2 traps (TRAP/TRAPV/CHK) stack the address of
# the NEXT instruction; group-1 exceptions (privilege, illegal, A/F-line)
# and divide-by-zero stack the address of the faulting instruction; the
# stacked SR is the SR before the exception (after CHK/DIV flag updates);
# the new SR has S set and T cleared, the CCR and mask unchanged.
#
# Sources (Motorola M68000 Family User's Manual M68000UM/AD rev 8, Section 6
# "Exception Processing"; Programmer's Reference Manual M68000PM/AD rev 1,
# Sections 4 and 6.  Section numbers are cited from those editions; check
# them against your copy):
#   frame layout (SR word at SSP, PC long at SSP+2: the 3-word group 1/2
#       frame) and the S:=1 / T:=0 / PC:=(vector*4) sequence  UM 6.2.4, 6.2.5
#   TRAP #n -> vector 32+n, stacked PC = next instruction  UM 6.3.5; PRM "TRAP"
#   TRAPV / CHK -> vectors 7 / 6, stacked PC = next        UM 6.3.5; PRM
#                                                          "TRAPV", "CHK"
#   illegal ($4AFC, unassigned encodings), A-line, F-line -> vectors
#       4 / 10 / 11, stacked PC = the offending instruction      UM 6.3.6
#   privilege violation -> vector 8, stacked PC = the instruction UM 6.3.7
#   MOVE to SR, ANDI/ORI/EORI to SR, MOVE USP, RTE, STOP, RESET are
#       privileged; STOP loads SR then stops; RTE pops SR, then PC  PRM 6
#   divide by zero -> vector 5: UM 6.3.5 says the NEXT instruction is
#       stacked, but the pinned upstream vectors stack the DIVU itself
#       (DIVU.exc); the vectors are the authority, so these cases follow them.
# Mutants that swap the stacked PC/SR order or the PC-advance rule
# (build/evidence/w6-super/mut: frame-pc-sr-swapped, group1-stacks-next,
# group2-stacks-insn, div0-stacks-next) each turn SUPERHAND red on its own.

HAND_PC = 0x001000
HAND_SSP = 0x00F000
HAND_USP = 0x00C000
HAND_REGS = [0x00000000, 0x11111111, 0x22222222, 0x33333333,
             0x44444444, 0x55555555, 0x66666666, 0x77777777,
             0x00002000, 0x00002100, 0x00002200, 0x00002300,
             0x00002400, 0x00002500, 0x00003000]  # D0-D7, A0-A6
# Vector table: vector n -> handler $4000 + n*$10 (even, distinct).
HAND_VECTORS = (4, 5, 6, 7, 8, 10, 11, 32, 33, 47)


def _handler(vec: int) -> int:
    return 0x4000 + vec * 0x10


def _be(addr: int, val: int, n: int) -> dict[int, int]:
    return {(addr + i) & ADDR_MASK: (val >> (8 * (n - 1 - i))) & 0xFF
            for i in range(n)}


def _hand(name: str, words: Sequence[int], *, sr: int,
          d: dict[int, int] | None = None, a: dict[int, int] | None = None,
          mem: dict[int, int] | None = None, flags: int = FLAG_EXC,
          exc: tuple[int, int] | None = None, ccr_after: int | None = None,
          exp_sr: int | None = None, exp_pc: int | None = None,
          exp_d: dict[int, int] | None = None,
          exp_a: dict[int, int] | None = None,
          swap: bool = False, osp_after: int | None = None,
          halt: int = 0, reason: int = 0,
          wmem: dict[int, int] | None = None, pc: int = HAND_PC,
          handlers: dict[int, int] | None = None) -> dict[str, Any]:
    """One normalized super case.

    ``exc=(vector, stacked_pc)`` describes an exception: the 6-byte frame
    (SR word, PC long) goes below the supervisor stack pointer, the PC
    becomes the handler, S is set and T cleared.  ``ccr_after`` is the
    CCR the instruction leaves before the exception (stacked).  ``swap``
    = the SR change clears S (A7 becomes the USP).  ``osp_after``
    overrides the expected inactive SP.  ``pc`` places the instruction
    (default $1000); ``handlers`` overrides vector -> handler entries of
    the table (e.g. handlers at or above $10000).
    """
    sup = bool(sr & SR_SUPER)
    table = {v: _handler(v) for v in HAND_VECTORS}
    table.update(handlers or {})
    regs = list(HAND_REGS)
    for i, v in (d or {}).items():
        regs[i] = v
    for i, v in (a or {}).items():
        regs[8 + i] = v
    a7 = HAND_SSP if sup else HAND_USP
    osp = HAND_USP if sup else HAND_SSP
    init_mem: dict[int, int] = {}
    for vec, h in table.items():
        init_mem.update(_be(vec * 4, h, 4))
    init_mem.update(mem or {})
    for i, w in enumerate(words):
        init_mem.update(_be(pc + 2 * i, w, 2))
    exp_mem = dict(init_mem)
    exp_mem.update(wmem or {})
    exp = regs[:15] + [a7]
    for i, v in (exp_d or {}).items():
        exp[i] = v
    for i, v in (exp_a or {}).items():
        exp[8 + i] = v
    e_osp = osp
    e_sr = sr if exp_sr is None else exp_sr
    e_pc = pc + 2 * len(words)
    if exc is not None:
        vec, spc = exc
        stacked = (sr & ~0x1F) | (sr & 0x1F if ccr_after is None
                                  else ccr_after)
        frame = HAND_SSP - 6            # SSP is HAND_SSP in either mode
        exp_mem.update(_be(frame, stacked, 2))
        exp_mem.update(_be(frame + 2, spc, 4))
        exp[15] = frame
        e_osp = HAND_USP                # USP: inactive after, unchanged
        e_sr = (stacked | SR_SUPER) & ~SR_TRACE & 0xFFFF
        e_pc = table[vec]
    elif swap:                          # S cleared: A7 <- USP, SSP inactive
        exp[15] = osp
        e_osp = a7
    if exp_pc is not None:
        e_pc = exp_pc
    if osp_after is not None:
        e_osp = osp_after
    return {
        "name": name, "regs": regs[:15] + [a7], "pc": pc, "sr": sr,
        "init_mem": sorted(init_mem.items()),
        "exp_regs": exp, "exp_pc": e_pc, "exp_sr": e_sr,
        "exp_mem": sorted(exp_mem.items()),
        "osp": osp, "exp_osp": e_osp, "flags": flags,
        "exp_halt": halt, "exp_reason": reason,
    }


def super_hand_cases() -> list[dict[str, Any]]:
    """The SUPERHAND batch (see the section comment above)."""
    P, U, S = HAND_PC, 0x0015, 0x2715   # user / supervisor SR with CCR XZC
    c: list[dict[str, Any]] = []
    h = c.append
    # privilege violations from user mode: vector 8, stacked PC = opcode
    for name, words in (("MOVE D0,SR", (0x46C0,)),
                        ("ANDI #0,SR", (0x027C, 0x0000)),
                        ("ORI #$700,SR", (0x007C, 0x0700)),
                        ("EORI #$2000,SR", (0x0A7C, 0x2000)),
                        ("MOVE A0,USP", (0x4E60,)),
                        ("MOVE USP,A1", (0x4E69,)),
                        ("RTE", (0x4E73,)), ("RESET", (0x4E70,)),
                        ("STOP #$2700", (0x4E72, 0x2700))):
        h(_hand(f"priv {name} (U)", words, sr=U, exc=(8, P)))
    # traps from user mode (stack switch), stacked PC = next instruction
    h(_hand("TRAP #0 (U)", (0x4E40,), sr=U, exc=(32, P + 2)))
    h(_hand("TRAP #1 (U)", (0x4E41,), sr=U, exc=(33, P + 2)))
    h(_hand("TRAP #15 (S)", (0x4E4F,), sr=S, exc=(47, P + 2)))
    h(_hand("TRAPV V=1 (U)", (0x4E76,), sr=0x0002, exc=(7, P + 2)))
    h(_hand("TRAPV V=0 (U)", (0x4E76,), sr=U))
    h(_hand("CHK D1,D0 Dn<0 (U)", (0x4181,), sr=U,
            d={0: 0x0000FFFE, 1: 0x00000010}, exc=(6, P + 2),
            ccr_after=0x18))
    h(_hand("CHK D1,D0 Dn>src (U)", (0x4181,), sr=0x001F,
            d={0: 0x00000011, 1: 0x00000010}, exc=(6, P + 2),
            ccr_after=0x10))
    h(_hand("CHK D1,D0 in range (U)", (0x4181,), sr=0x000F,
            d={0: 0x00000010, 1: 0x00000010}, exp_sr=0x0008))
    # illegal-instruction family: stacked PC = opcode address
    h(_hand("ILLEGAL (S)", (0x4AFC,), sr=S, exc=(4, P)))
    h(_hand("ILLEGAL (U)", (0x4AFC,), sr=U, exc=(4, P)))
    # T set: the instruction never executes, so no trace follows; the
    # frame keeps T, the handler runs with T cleared
    h(_hand("ILLEGAL T=1 (S)", (0x4AFC,), sr=0xA715, exc=(4, P)))
    h(_hand("MOVEC 68010+ (S)", (0x4E7A, 0x0801), sr=S, exc=(4, P)))
    h(_hand("RTD 68010+ (S)", (0x4E74, 0x0010), sr=S, exc=(4, P)))
    h(_hand("$4E00 undefined (S)", (0x4E00,), sr=S, exc=(4, P)))
    h(_hand("TST.w bad EA 7/5 (S)", (0x4A7D,), sr=S, exc=(4, P)))
    h(_hand("MOVE A0,SR bad EA (S)", (0x46C8,), sr=S, exc=(4, P)))
    # review F1: privilege is checked BEFORE the EA side effect (A0 stays
    # $2000), and T=1 does not make user mode privileged (vector 8; the
    # instruction never executes, so no trace follows)
    h(_hand("priv MOVE (A0)+,SR (U) A0 unchanged", (0x46D8,), sr=U,
            a={0: 0x00002000}, exc=(8, P)))
    h(_hand("priv RESET (U, T=1)", (0x4E70,), sr=0x8015, exc=(8, P)))
    # review F1: group-1 stacked PC and handler addresses above $FFFF
    # (programs load at $010000): all 24 bits of st_ipc and of the vector
    h(_hand("ILLEGAL at $012340 (S) -> $034560", (0x4AFC,), sr=S,
            pc=0x012340, handlers={4: 0x034560}, exc=(4, 0x012340)))
    h(_hand("priv MOVE D0,SR at $0ABCDE (U) -> $045670", (0x46C0,), sr=U,
            pc=0x0ABCDE, handlers={8: 0x045670}, exc=(8, 0x0ABCDE)))
    h(_hand("TRAP #1 at $010000 (S) -> $056780", (0x4E41,), sr=S,
            pc=0x010000, handlers={33: 0x056780}, exc=(33, 0x010002)))
    h(_hand("CHK A1,D0 bad EA (U)", (0x4189,), sr=U, exc=(4, P)))
    h(_hand("A-line $A000 (U)", (0xA000,), sr=U, exc=(10, P)))
    h(_hand("A-line $A1FF (S)", (0xA1FF,), sr=S, exc=(10, P)))
    h(_hand("F-line $F000 (S)", (0xF000,), sr=S, exc=(11, P)))
    h(_hand("F-line $FFFF (U)", (0xFFFF,), sr=U, exc=(11, P)))
    # divide by zero: NZVC cleared, stacked PC = opcode (upstream DIVU.exc)
    h(_hand("DIVU D1,D0 /0 (S)", (0x80C1,), sr=0x271F,
            d={1: 0xFFFF0000}, exc=(5, P), ccr_after=0x10))
    h(_hand("DIVS #0,D0 /0 (U)", (0x81FC, 0x0000), sr=0x000F,
            exc=(5, P), ccr_after=0x00))
    # STOP / RESET in supervisor mode
    h(_hand("STOP #$2700 (S)", (0x4E72, 0x2700), sr=S, exp_sr=0x2700,
            halt=1, reason=HALT_STOP))
    h(_hand("STOP #$0015 -> U", (0x4E72, 0x0015), sr=S, exp_sr=0x0015,
            swap=True, halt=1, reason=HALT_STOP))
    h(_hand("RESET (S)", (0x4E70,), sr=S))
    # SR writes: T is stored (tracing not modelled), bits 12/11/7-5 read 0
    h(_hand("ORI #$FFFF,SR (S)", (0x007C, 0xFFFF), sr=S, exp_sr=0xA71F))
    h(_hand("MOVE D2,SR clears S", (0x46C2,), sr=S, d={2: 0x0000D8E3},
            exp_sr=0x8003, swap=True))
    h(_hand("MOVE A7,USP (S)", (0x4E67,), sr=S, osp_after=HAND_SSP))
    h(_hand("MOVE USP,A7 (S)", (0x4E6F,), sr=S, exp_a={7: HAND_USP}))
    h(_hand("RTE -> U", (0x4E73,), sr=S,
            mem={**_be(HAND_SSP, 0x0008, 2), **_be(HAND_SSP + 2, 0x5000, 4)},
            exp_sr=0x0008, exp_pc=0x5000, exp_a={7: HAND_USP},
            osp_after=HAND_SSP + 6))
    # odd vector / odd RTE target: halt with the address-error reason
    h(_hand("TRAP #1 odd vector (S)", (0x4E41,), sr=S,
            mem=_be(33 * 4, 0x00004211, 4), exc=(33, P + 2),
            exp_pc=0x4211, halt=1, reason=HALT_ADDRESS))
    h(_hand("RTE odd PC (S)", (0x4E73,), sr=S,
            mem={**_be(HAND_SSP, 0x2700, 2), **_be(HAND_SSP + 2, 0x5001, 4)},
            exp_sr=0x2700, exp_pc=0x5001, exp_a={7: HAND_SSP + 6},
            halt=1, reason=HALT_ADDRESS))
    # legal 68000 ops the interpreter does not implement keep halting
    # ILLEGAL in exception mode (never a wrong illegal-instruction trap)
    h(_hand("MOVEP.W (d16,A0),D0 unimpl (S)", (0x0108, 0x0010), sr=S,
            exp_pc=P + 2, halt=1, reason=HALT_ILLEGAL))
    h(_hand("TAS D0 unimpl (S)", (0x4AC0,), sr=S, halt=1,
            reason=HALT_ILLEGAL))
    # $AFFF keeps its pseudo-op meaning with the flag set
    h(_hand("$AFFF halt (S, flag set)", (0xAFFF,), sr=S,
            halt=1, reason=HALT_OK))
    # flag CLEAR: today's halts, bit for bit (PC past the opcode word,
    # registers, SR, inactive SP and memory unchanged)
    for name, words, rsn, pc_adv in (
            ("TRAP #0", (0x4E40,), HALT_ILLEGAL, 2),
            ("TRAPV V=1", (0x4E76,), HALT_ILLEGAL, 2),
            ("CHK D1,D0", (0x4181,), HALT_ILLEGAL, 2),
            ("A-line $A000", (0xA000,), HALT_ALINE, 2),
            ("F-line $F000", (0xF000,), HALT_FLINE, 2),
            ("ILLEGAL", (0x4AFC,), HALT_ILLEGAL, 2),
            ("MOVE D0,SR", (0x46C0,), HALT_ILLEGAL, 2),
            ("ANDI #0,SR", (0x027C, 0x0000), HALT_ILLEGAL, 2),
            ("MOVE A0,USP", (0x4E60,), HALT_ILLEGAL, 2),
            ("RTE", (0x4E73,), HALT_ILLEGAL, 2),
            ("STOP #$2700", (0x4E72, 0x2700), HALT_ILLEGAL, 2),
            ("RESET", (0x4E70,), HALT_ILLEGAL, 2),
            ("DIVU D1,D0 /0", (0x80C1,), HALT_ILLEGAL, 2),
            ("$4E00 undefined (op_illegal)", (0x4E00,), HALT_ILLEGAL, 2)):
        # (A bad EA halting from inside ea_calc is NOT listed: today the
        # 6502 rts after that nested halt resumes the handler, which then
        # updates flags from garbage -- pinned only with the flag set.)
        for sr in (0x0002, 0x2702):
            h(_hand(f"flag clear: {name} ({'S' if sr & SR_SUPER else 'U'})",
                    words, sr=sr, flags=0, d={1: 0}, exp_pc=P + pc_adv,
                    halt=1, reason=rsn))
    return c


# --- multi-instruction exception-mode programs (review F1) -----------------
#
# Run by tests/run_super_vectors.py twice: through test_run_program (the
# emu_step path) and through emu_reset + emu_run (the product / AROS path,
# where only the latch-patched run_jv and nx_br lead into the exception
# loop).  Each starts in USER mode at >= $010000 with st_exc set, takes
# exceptions on its first AND later instructions, and ends at $AFFF inside
# a handler (HALT_OK).  Expected state authored by hand (PRM/UM as cited
# above); host/tests/test_super_model.py re-derives it by stepping the
# reference model.

RUN_USP = 0x00F000
RUN_SSP = 0x00E000


def super_run_programs() -> list[dict[str, Any]]:
    progs = []
    # A: TRAP (1st instruction) -> RTE back to user -> MOVEQ -> ILLEGAL
    #    (3rd instruction, group 1: stacked PC must be ITS address).
    mem: dict[int, int] = {}
    mem.update(_be(32 * 4, 0x020000, 4))          # TRAP #0 handler
    mem.update(_be(4 * 4, 0x020010, 4))           # illegal handler
    for a, w in ((0x010000, 0x4E40), (0x010002, 0x70FF), (0x010004, 0x4AFC),
                 (0x020000, 0x4E73),
                 (0x020010, 0x2C2F), (0x020012, 0x0002),     # MOVE.L 2(A7),D6
                 (0x020014, 0x3E17),                         # MOVE.W (A7),D7
                 (0x020016, 0xAFFF)):
        mem.update(_be(a, w, 2))
    frame = RUN_SSP - 6
    progs.append({
        "name": "TRAP->RTE->MOVEQ->ILLEGAL (U, code/handlers >= $10000)",
        "mem": mem, "pc": 0x010000,
        "exp": {"d0": 0xFFFFFFFF, "d6": 0x00010004, "d7": 0x00000008,
                "a7": frame},
        "exp_pc": 0x020018, "exp_sr": 0x2000, "exp_osp": RUN_USP,
        "exp_mem": {**_be(frame, 0x0008, 2), **_be(frame + 2, 0x010004, 4)},
        "icount": 7,
    })
    # B: MOVEQ -> DIVU #0 (2nd instruction; stacks the DIVU address).
    mem = {}
    mem.update(_be(5 * 4, 0x040000, 4))
    for a, w in ((0x030000, 0x7005), (0x030002, 0x80FC), (0x030004, 0x0000),
                 (0x040000, 0x2C2F), (0x040002, 0x0002), (0x040004, 0xAFFF)):
        mem.update(_be(a, w, 2))
    progs.append({
        "name": "MOVEQ->DIVU #0 (U, 2nd instruction)",
        "mem": mem, "pc": 0x030000,
        "exp": {"d0": 0x00000005, "d6": 0x00030002, "a7": frame},
        "exp_pc": 0x040006, "exp_sr": 0x2000, "exp_osp": RUN_USP,
        "exp_mem": {**_be(frame, 0x0000, 2), **_be(frame + 2, 0x030002, 4)},
        "icount": 4,
    })
    # C: ILLEGAL as the FIRST instruction (group 1: its stacked PC comes
    #    from the snapshot of THAT instruction, so emu_run must enter the
    #    exception loop at once -- the latch-patched run_jv, review F1).
    mem = {}
    mem.update(_be(4 * 4, 0x060000, 4))
    for a, w in ((0x050000, 0x4AFC),
                 (0x060000, 0x2C2F), (0x060002, 0x0002), (0x060004, 0xAFFF)):
        mem.update(_be(a, w, 2))
    progs.append({
        "name": "ILLEGAL first (U, group 1 on instruction 1)",
        "mem": mem, "pc": 0x050000,
        "exp": {"d6": 0x00050000, "a7": frame},
        "exp_pc": 0x060006, "exp_sr": 0x2000, "exp_osp": RUN_USP,
        "exp_mem": {**_be(frame, 0x0000, 2), **_be(frame + 2, 0x050000, 4)},
        "icount": 3,
    })
    return progs


# --- manifest ---------------------------------------------------------------

def write_manifest(stats: dict[str, dict[str, int]],
                   fixtures_dir: Path = FIXTURES_DIR) -> None:
    doc = {
        "commit": UPSTREAM_COMMIT,
        "repo": UPSTREAM_REPO,
        "suite": SUITE_PATH,
        "sample_seed": SAMPLE_SEED,
        "files": dict(sorted(stats.items())),
    }
    (fixtures_dir / MANIFEST_NAME).write_text(
        json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(fixtures_dir: Path = FIXTURES_DIR) -> dict[str, Any]:
    return json.loads(
        (fixtures_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


# --- CLI --------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m host.vectors",
        description="Vendor SingleStepTests 68000 vectors and compile "
                    "binary batches.")
    ap.add_argument("--out", type=Path, required=True,
                    help="output directory for .bin batches")
    ap.add_argument("--offline", action="store_true",
                    help="use only vendored fixtures; no network")
    ap.add_argument("--n", type=int, default=DEFAULT_SAMPLE_N,
                    help="cases to sample per file when vendoring")
    ap.add_argument("--only", type=str, default=None,
                    help="comma-separated subset of file stems")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download even if a fixture already exists")
    ap.add_argument("--set", choices=("m1", "super"), default="m1",
                    help="m1: golden user-mode batches (default); super: "
                         "supervisor/exception batches in the extended "
                         "format + SUPERHAND (tests/run_super_vectors.py)")
    ap.add_argument("--fixtures", type=Path, default=FIXTURES_DIR,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    universe = SUPER_FILES if args.set == "super" else M1_FILES
    stems = list(universe)
    if args.only:
        stems = [s for s in args.only.split(",") if s]
        unknown = sorted(set(stems) - set(universe))
        if unknown:
            ap.error(f"not in {args.set} set: {', '.join(unknown)}")

    fixtures_dir: Path = args.fixtures
    if not args.offline:
        try:
            manifest_stats = load_manifest(fixtures_dir).get("files", {})
        except FileNotFoundError:
            manifest_stats = {}
        todo = [s for s in stems
                if args.refresh or not fixture_path(s, fixtures_dir).exists()]
        if todo or not (fixtures_dir / LICENSE_NAME).exists():
            write_license(fixtures_dir)
        if todo:
            print(f"vendoring {len(todo)} file(s) from {UPSTREAM_REPO}"
                  f"@{UPSTREAM_COMMIT[:12]} ...", flush=True)
            with ThreadPoolExecutor(max_workers=8) as pool:
                for stem, stats in zip(
                        todo, pool.map(
                            lambda s: vendor_file(s, args.n, fixtures_dir),
                            todo)):
                    manifest_stats[stem] = stats
                    print(f"  {stem}: total {stats['total']}, pool "
                          f"{stats['retained_pool']}, sampled "
                          f"{stats['sampled']}, dropped {stats['dropped']}",
                          flush=True)
            write_manifest(manifest_stats, fixtures_dir)

    missing = [s for s in stems if not fixture_path(s, fixtures_dir).exists()]
    if missing:
        print(f"error: missing vendored fixtures for: {', '.join(missing)}"
              f" (run without --offline first)", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    total_cases = 0
    for stem in stems:
        cases, _ = exclude_io_bank(load_fixture(stem, fixtures_dir))
        if args.set == "super":
            blob = compile_super_batch(cases, stem.endswith(EXC_SUFFIX))
        else:
            blob = compile_batch(cases)
        (args.out / f"{stem}.bin").write_bytes(blob)
        total_cases += len(cases)
    if args.set == "super" and not args.only:
        hand = super_hand_cases()
        (args.out / f"{SUPER_HAND_STEM}.bin").write_bytes(
            pack_super_norms(hand))
        stems.append(SUPER_HAND_STEM)
        total_cases += len(hand)
    print(f"wrote {len(stems)} batch(es), {total_cases} case(s) "
          f"to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
