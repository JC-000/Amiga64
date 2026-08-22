"""Minimal proof the VICE harness path works, with no interpreter core involved.

Assembles tests/stub/smoke.s (a ~20-line PRG) into build/stub/smoke.prg, boots
it in VICE via c64-test-harness, jsr()s the `smoke_add` label discovered from
the ld65 label file, and asserts the memory results.

Usage:  python3 -m tests.smoke
"""

from __future__ import annotations

import os
import sys

from c64_test_harness import (
    Labels, ViceConfig, ViceInstanceManager, read_bytes, write_bytes, jsr,
)

from tests.harness_util import (
    PROJECT_ROOT, BUILD_STUB_DIR, cl65_build, lookup_label, wait_ready,
)

SMOKE_PRG = os.path.join(BUILD_STUB_DIR, "smoke.prg")
SMOKE_LBL = os.path.join(BUILD_STUB_DIR, "smoke.lbl")

CASES = [(1, 2), (0x7F, 0x01), (0xFF, 0xFF), (0x00, 0x00)]


def main() -> int:
    cl65_build(
        [os.path.join("tests", "stub", "smoke.s")],
        SMOKE_PRG,
        os.path.join("tests", "stub", "stub.cfg"),
        SMOKE_LBL,
    )
    print(f"built {os.path.relpath(SMOKE_PRG, PROJECT_ROOT)}")

    labels = Labels.from_file(SMOKE_LBL)
    smoke_add = lookup_label(labels, "smoke_add")
    print(f"smoke_add @ ${smoke_add:04X}")

    config = ViceConfig(prg_path=SMOKE_PRG, warp=True, sound=False)
    failed = 0
    with ViceInstanceManager(config=config) as mgr:
        inst = mgr.acquire()
        print(f"VICE PID={inst.pid}, port={inst.port}")
        transport = inst.transport
        wait_ready(transport)
        for a, b in CASES:
            write_bytes(transport, 0xC000, bytes([a, b]))
            jsr(transport, smoke_add, timeout=10.0)
            got = read_bytes(transport, 0xC002, 2)
            want = a + b
            actual = got[0] | (got[1] << 8)
            status = "ok" if actual == want else "FAIL"
            if actual != want:
                failed += 1
            print(f"  smoke_add({a:#04x}, {b:#04x}) = {actual:#06x} "
                  f"(want {want:#06x}) {status}")
        mgr.release(inst)

    if failed:
        print(f"SMOKE FAILED: {failed}/{len(CASES)} cases")
        return 1
    print(f"SMOKE PASSED: {len(CASES)}/{len(CASES)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
