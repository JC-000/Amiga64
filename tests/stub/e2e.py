"""Stub end-to-end: build runner.s + stub_core.s, generate mini vectors,
run them through the full tests/run_vectors.py driver in VICE.

Proves the whole M1 test-execution path (case encode -> buf_case ->
test_run_case -> emu_step -> buf_result -> compare) works before
runtime/core.s exists. The real thing is the same driver pointed at
build/runner.prg:  make runtime && make vectors && make vice-test

Usage:  python3 -m tests.stub.e2e
"""

from __future__ import annotations

import os
import subprocess
import sys

from tests.harness_util import BUILD_STUB_DIR, PROJECT_ROOT, cl65_build

STUB_PRG = os.path.join(BUILD_STUB_DIR, "stub_runner.prg")
STUB_LBL = os.path.join(BUILD_STUB_DIR, "stub_runner.lbl")
VECTORS_DIR = os.path.join(BUILD_STUB_DIR, "vectors")


def build_stub_runner() -> None:
    cl65_build(
        [os.path.join("tests", "runner", "runner.s"),
         os.path.join("tests", "stub", "stub_core.s")],
        STUB_PRG,
        os.path.join("tests", "stub", "stub.cfg"),
        STUB_LBL,
    )
    print(f"built {os.path.relpath(STUB_PRG, PROJECT_ROOT)}")


def main() -> int:
    build_stub_runner()
    gen = subprocess.run(
        [sys.executable, "-m", "tests.stub.make_mini_vectors", "--out", VECTORS_DIR],
        cwd=PROJECT_ROOT)
    if gen.returncode != 0:
        return gen.returncode
    # Exercise the real CLI exactly as `make vice-test` will.
    run = subprocess.run(
        [sys.executable, "-m", "tests.run_vectors",
         "--prg", STUB_PRG, "--labels", STUB_LBL, "--vectors", VECTORS_DIR],
        cwd=PROJECT_ROOT)
    return run.returncode


if __name__ == "__main__":
    sys.exit(main())
