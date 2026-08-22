"""M0 end-to-end demo driver: build, stage, and run a Hunk executable.

Usage:
    python3 -m tests.run_program --prg build/runner.prg
        [--fixture minimal|reloc|all | --hunk FILE] [--budget N]
        [--labels PATH] [--timeout SECS] [--backend auto|vice|u64]

Pipeline (docs/CONTRACTS.md "M0 loader"): host/fixturegen.py builds a Hunk
executable -> host/hunk.py parses it -> host/image.py lays out and relocates
segments into sparse image chunks -> the chunks are staged into guest (REU)
memory through the C64-side `test_load_chunk` entry (tests/runner/
run_program.s), one buf_case-sized record per call, each record framed
exactly like image.bin -> `test_run_program` sets PC = manifest entry and
A7 = stack_top and loops `emu_step` until the guest HALTs (pseudo-op $AFFF)
or the instruction budget runs out -> final registers land in buf_result.

For the canned fixtures the driver also verifies every staged byte via
`mem_read8` and asserts the architectural end state computed from the
fixture definition. Exit code 0 = all checks passed.

Host-only helpers (record framing, expectations) have no c64-test-harness
dependency so host/tests/test_m0_pipeline.py can import this module.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass, field

from host import fixturegen, hunk, image
from host.image import Chunk

# Contract addresses (docs/CONTRACTS.md "C64 memory map")
BUF_CASE = 0xC000
BUF_RESULT = 0xC800
ZP_M_ADDR = 0x58
ZP_M_VAL = 0x5C

# buf_result layout for test_run_program (tests/runner/run_program.s)
RES_HALT = 1
RES_REASON = 2
RES_TIMEOUT = 3
RES_ICOUNT = 4
RES_REGS = 0x10
RESULT_LEN = RES_REGS + 70

HALT_OK = 0x00  # st_reason for the $AFFF HALT pseudo-op

# buf_case is 2 KB ($C000-$C7FF); 8 bytes go to the record header.
MAX_RECORD_PAYLOAD = 0x800 - 8

REG_NAMES = tuple(f"D{i}" for i in range(8)) + tuple(f"A{i}" for i in range(8))


def chunk_records(chunks: list[Chunk],
                  max_payload: int = MAX_RECORD_PAYLOAD) -> list[bytes]:
    """Split image chunks into buf_case-sized staging records.

    Each record uses the image.bin framing (u32 LE offset, u32 LE len,
    payload) that `test_load_chunk` consumes.
    """
    if max_payload < 1:
        raise ValueError("max_payload must be positive")
    records: list[bytes] = []
    for c in chunks:
        for pos in range(0, len(c.data), max_payload):
            part = c.data[pos:pos + max_payload]
            records.append(
                struct.pack("<II", c.guest_offset + pos, len(part)) + part
            )
    return records


@dataclass
class Expectation:
    """Architectural end state computed from a canned fixture definition."""

    regs: dict[str, int]           # named regs; unnamed ones must be 0
    pc: int
    icount: int                    # includes the halting instruction
    sr_ccr_mask: int = 0x00        # CCR bits that must match sr_ccr
    sr_ccr: int = 0x00


def fixture_minimal() -> tuple[bytes, "Expectation"]:
    """MOVEQ #1,D0; HALT — D0=1, PC=entry+4, 2 instructions."""
    blob = fixturegen.fixture_minimal_halt()
    exp = Expectation(regs={"D0": 1, "A7": image.STACK_TOP},
                      pc=image.BASE_ADDR + 4, icount=2)
    return blob, exp


def fixture_reloc() -> tuple[bytes, "Expectation"]:
    """MOVE.L (abs.L),D0; HALT — the address field is RELOC32-patched to the
    data hunk, so D0 proves relocation + staging + interpretation at once."""
    blob = fixturegen.fixture_code_data_reloc()
    d0 = int.from_bytes(
        fixturegen.RELOC_FIXTURE_DATA[
            fixturegen.RELOC_FIXTURE_FIELD_PRESET:
            fixturegen.RELOC_FIXTURE_FIELD_PRESET + 4
        ],
        "big",
    )
    exp = Expectation(regs={"D0": d0, "A7": image.STACK_TOP},
                      pc=image.BASE_ADDR + 8, icount=2)
    return blob, exp


FIXTURES = {"minimal": fixture_minimal, "reloc": fixture_reloc}


@dataclass
class RunResult:
    halt: int
    reason: int
    timeout: int
    icount: int
    regs: dict[str, int]
    pc: int
    sr: int

    @classmethod
    def from_bytes(cls, hdr: bytes) -> "RunResult":
        regs = {
            name: int.from_bytes(hdr[RES_REGS + 4 * i:RES_REGS + 4 * i + 4],
                                 "little")
            for i, name in enumerate(REG_NAMES)
        }
        return cls(
            halt=hdr[RES_HALT],
            reason=hdr[RES_REASON],
            timeout=hdr[RES_TIMEOUT],
            icount=int.from_bytes(hdr[RES_ICOUNT:RES_ICOUNT + 4], "little"),
            regs=regs,
            pc=int.from_bytes(hdr[RES_REGS + 64:RES_REGS + 68], "little"),
            sr=int.from_bytes(hdr[RES_REGS + 68:RES_REGS + 70], "little"),
        )

    def print_state(self) -> None:
        for base in ("D", "A"):
            row = "  ".join(f"{base}{i}={self.regs[f'{base}{i}']:08X}"
                            for i in range(8))
            print(f"  {row}")
        status = "TIMEOUT" if self.timeout else (
            f"halt reason={self.reason:02X}" if self.halt else "running")
        print(f"  PC={self.pc:08X}  SR={self.sr:04X}  "
              f"instructions={self.icount}  [{status}]")


@dataclass
class Session:
    """One prepared backend target plus the labels needed to drive it."""

    target: object
    labels: object
    timeout: float
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def check(self, what: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.checks.append(what)
        else:
            self.failures.append(f"{what}: {detail}" if detail else what)
            print(f"  CHECK FAILED  {what}  {detail}")

    def _call(self, label: str) -> None:
        from tests.harness_util import call_runner, lookup_label
        call_runner(self.target, lookup_label(self.labels, label),
                    self.timeout)

    def stage(self, chunks: list[Chunk]) -> None:
        from c64_test_harness import read_bytes, write_bytes
        transport = self.target.transport
        records = chunk_records(chunks)
        for rec in records:
            write_bytes(transport, BUF_CASE, rec)
            self._call("test_load_chunk")
            if read_bytes(transport, BUF_RESULT, 1)[0] != 0xFF:
                raise RuntimeError("test_load_chunk did not complete")
        total = sum(len(c.data) for c in chunks)
        print(f"  staged {total} byte(s) in {len(records)} record(s)")

    def peek_guest(self, addr: int) -> int:
        """Read one guest byte back through the interpreter's mem_read8."""
        from c64_test_harness import read_bytes, write_bytes
        write_bytes(self.target.transport, ZP_M_ADDR, struct.pack("<I", addr))
        self._call("mem_read8")
        return read_bytes(self.target.transport, ZP_M_VAL, 1)[0]

    def verify_staged(self, chunks: list[Chunk]) -> None:
        bad = 0
        for c in chunks:
            for i, expect in enumerate(c.data):
                got = self.peek_guest(c.guest_offset + i)
                if got != expect:
                    bad += 1
                    print(f"  staging mismatch at ${c.guest_offset + i:06X}: "
                          f"expected {expect:02X} got {got:02X}")
        nbytes = sum(len(c.data) for c in chunks)
        self.check(f"staged image readback ({nbytes} bytes)", bad == 0,
                   f"{bad} byte(s) wrong")

    def run_program(self, pc: int, a7: int, budget: int) -> RunResult:
        from c64_test_harness import read_bytes, write_bytes
        transport = self.target.transport
        write_bytes(transport, BUF_CASE, struct.pack("<III", pc, a7, budget))
        self._call("test_run_program")
        hdr = read_bytes(transport, BUF_RESULT, RESULT_LEN)
        if hdr[0] != 0xFF:
            raise RuntimeError("test_run_program did not complete")
        return RunResult.from_bytes(hdr)

    def verify_result(self, name: str, res: RunResult,
                      exp: Expectation) -> None:
        self.check(f"{name}: halted", res.halt != 0 and res.timeout == 0,
                   f"halt={res.halt:02X} timeout={res.timeout}")
        self.check(f"{name}: reason HALT_OK", res.reason == HALT_OK,
                   f"reason={res.reason:02X}")
        for reg in REG_NAMES:
            want = exp.regs.get(reg, 0)
            self.check(f"{name}: {reg}={want:08X}", res.regs[reg] == want,
                       f"got {res.regs[reg]:08X}")
        self.check(f"{name}: PC={exp.pc:08X}", res.pc == exp.pc,
                   f"got {res.pc:08X}")
        self.check(f"{name}: {exp.icount} instructions",
                   res.icount == exp.icount, f"got {res.icount}")
        if exp.sr_ccr_mask:
            self.check(f"{name}: CCR", (res.sr ^ exp.sr_ccr)
                       & exp.sr_ccr_mask == 0, f"SR={res.sr:04X}")


def run_fixture(sess: Session, name: str, blob: bytes,
                exp: Expectation | None, budget: int) -> None:
    exe = hunk.parse(blob)
    chunks, manifest = image.build_image(exe)
    print(f"[{name}] {len(blob)}-byte hunk file, "
          f"{len(manifest['segments'])} segment(s), "
          f"entry=${manifest['entry']:06X} stack_top=${manifest['stack_top']:06X}")
    sess.stage(chunks)
    if exp is not None:
        sess.verify_staged(chunks)
    res = sess.run_program(manifest["entry"], manifest["stack_top"], budget)
    res.print_state()
    if exp is not None:
        sess.verify_result(name, res, exp)


def run_budget_demo(sess: Session, name: str, exp: Expectation) -> None:
    """Re-run the (already staged) fixture with budget=1: the first
    instruction retires, then the budget stops the run before HALT."""
    res = sess.run_program(image.BASE_ADDR, image.STACK_TOP, budget=1)
    print(f"[{name}+budget=1]")
    res.print_state()
    sess.check(f"{name}+budget=1: timeout flagged",
               res.timeout == 1 and res.halt == 0,
               f"timeout={res.timeout} halt={res.halt:02X}")
    sess.check(f"{name}+budget=1: 1 instruction", res.icount == 1,
               f"got {res.icount}")
    first_reg, first_val = next(iter(exp.regs.items()))
    sess.check(f"{name}+budget=1: {first_reg} updated",
               res.regs[first_reg] == first_val
               or first_reg == "A7",  # A7 comes from init, skip
               f"got {res.regs[first_reg]:08X}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tests.run_program",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True, help="runner PRG (build/runner.prg)")
    ap.add_argument("--fixture", default="all",
                    choices=["minimal", "reloc", "all"],
                    help="canned fixturegen program(s) to run (default: all)")
    ap.add_argument("--hunk", help="run this Hunk executable file instead")
    ap.add_argument("--budget", type=int, default=10_000, metavar="N",
                    help="max guest instructions, 0 = unlimited (default 10000)")
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

    if args.hunk:
        with open(args.hunk, "rb") as f:
            jobs = [(os.path.basename(args.hunk), f.read(), None)]
    else:
        names = list(FIXTURES) if args.fixture == "all" else [args.fixture]
        jobs = []
        for n in names:
            blob, exp = FIXTURES[n]()
            jobs.append((n, blob, exp))

    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS

    labels = Labels.from_file(labels_path)
    vice_config = ViceConfig(prg_path=prg, warp=True, sound=False,
                             extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend=args.backend, vice_config=vice_config) as mgr:
        target = mgr.acquire()
        try:
            from tests.run_vectors import prepare_target
            prepare_target(target, prg)
            sess = Session(target=target, labels=labels, timeout=args.timeout)
            for name, blob, exp in jobs:
                run_fixture(sess, name, blob, exp, args.budget)
                if exp is not None and args.budget != 1:
                    run_budget_demo(sess, name, exp)
                print()
        finally:
            mgr.release(target)

    n_ok = len(sess.checks)
    n_bad = len(sess.failures)
    if jobs[0][2] is None:
        print("(no canned expectations for an external hunk file; "
              "state printed above)")
    print(f"{n_ok}/{n_ok + n_bad} checks passed")
    if n_bad:
        for f in sess.failures:
            print(f"FAIL  {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
