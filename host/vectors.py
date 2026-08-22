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

# SR bits
SR_TRACE = 0x8000
SR_SUPER = 0x2000
SR_SYSTEM_MASK = 0xA700  # trace | supervisor | interrupt mask
CCR_MASK = 0x001F

ADDR_MASK = 0xFFFFFF  # 24-bit guest bus


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


def fetch_upstream_cases(stem: str) -> list[dict[str, Any]]:
    """Download and parse one upstream JSON test file (e.g. ``"ADD.b"``)."""
    raw = _http_get(f"{RAW_BASE}/{SUITE_PATH}/{stem}.json.gz")
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

def stride_sample(items: Sequence[Any], n: int, seed: int = SAMPLE_SEED
                  ) -> list[Any]:
    """Deterministic fixed-stride sample of ``n`` items in original order."""
    if n <= 0 or len(items) <= n:
        return list(items)
    stride = len(items) // n
    start = seed % stride
    return [items[start + i * stride] for i in range(n)]


def filter_and_sample(cases: Iterable[dict[str, Any]], n: int,
                      seed: int = SAMPLE_SEED
                      ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Filter, then stride-sample the retained pool; returns (cases, stats)."""
    retained: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    total = 0
    for case in cases:
        total += 1
        reason = classify(case)
        if reason is None and case_bin_size(normalize(case)) > MAX_CASE_BYTES:
            reason = "too-big"
        if reason is None:
            retained.append(case)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    sampled = stride_sample(retained, n, seed)
    stats = {
        "total": total,
        "retained_pool": len(retained),
        "sampled": len(sampled),
        "dropped": dict(sorted(dropped.items())),
    }
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
        "source": f"{SUITE_PATH}/{stem}.json.gz",
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
    sampled, stats = filter_and_sample(cases, n)
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
    ap.add_argument("--fixtures", type=Path, default=FIXTURES_DIR,
                    help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    stems = list(M1_FILES)
    if args.only:
        stems = [s for s in args.only.split(",") if s]
        unknown = sorted(set(stems) - set(M1_FILES))
        if unknown:
            ap.error(f"not in M1 subset: {', '.join(unknown)}")

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
        cases = load_fixture(stem, fixtures_dir)
        blob = compile_batch(cases)
        (args.out / f"{stem}.bin").write_bytes(blob)
        total_cases += len(cases)
    print(f"wrote {len(stems)} batch(es), {total_cases} case(s) "
          f"to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
