"""BSS is cleared at boot — stale C64 memory must never leak into a run.

On the U64E, C64 RAM survives a PRG (re)load: BSS comes up holding whatever
the previous program left. Two HLE flags live in BSS and trust it:
``hle_dos_magic`` ("DOS!" = lazy init already done) and ``dosio_want_uci``
("UCI!" = opt into the ULTDOS backend, which writes device storage). A
leftover "UCI!" from an earlier opt-in run must never switch a later program
onto ULTDOS (w5-tools finding, 2026-09-24).

This test models a reload with dirty RAM on VICE: boot, plant stale bytes
across BSS (including the two flags), clear READY, restart the PRG at its
SYS entry ($080D), and require every BSS byte to read zero once READY is
back (``main`` writes no BSS).

    python3 -m tests.bss_boot --prg build/runner.prg
"""

from __future__ import annotations

import argparse
import os
import sys
import time

READY_FLAG = 0xCFFF
SYS_ENTRY = 0x080D


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prg", required=True)
    args = ap.parse_args(argv)
    prg = os.path.abspath(args.prg)

    from c64_test_harness import (Labels, UnifiedManager, ViceConfig,
                                  read_bytes, write_bytes)
    from c64_test_harness.execute import goto
    from tests.harness_util import REU_VICE_ARGS, lookup_label
    from tests.run_vectors import prepare_target

    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    bss = lookup_label(labels, "__BSS_RUN__")
    size = lookup_label(labels, "__BSS_SIZE__")
    magic = lookup_label(labels, "hle_dos_magic")
    want = lookup_label(labels, "dosio_want_uci")

    cfg = ViceConfig(prg_path=prg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    fails = []
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        target = mgr.acquire()
        try:
            prepare_target(target, prg)
            t = target.transport
            # dirty RAM: a nonzero pattern over all of BSS, then the flags
            write_bytes(t, bss, bytes((0xA5 ^ i) & 0xFF or 1
                                      for i in range(size)))
            write_bytes(t, magic, b"DOS!")
            write_bytes(t, want, b"UCI!")
            write_bytes(t, READY_FLAG, b"\x00")
            goto(t, SYS_ENTRY)                     # "reload": re-enter the PRG
            # On VICE every monitor command (each read_bytes here) halts the
            # CPU until resume(): a bare read poll after goto() freezes the
            # boot clear at its first read and never sees READY.  Resume
            # after every poll read; no settle sleep (it only hid this for
            # short clears).  c64-test-harness#514.
            deadline = time.time() + 10
            while True:
                ready = read_bytes(t, READY_FLAG, 1)[0] == 0xA5
                if ready:
                    break
                t.resume()
                if time.time() > deadline:
                    fails.append("PRG did not come back READY after restart")
                    break
                time.sleep(0.05)
            if ready:
                got = read_bytes(t, bss, size)
                dirty = [i for i, b in enumerate(got) if b]
                if dirty:
                    fails.append(f"{len(dirty)} BSS byte(s) not cleared at "
                                 f"boot, first at ${bss + dirty[0]:04X}")
                if read_bytes(t, magic, 4) != b"\0\0\0\0":
                    fails.append("hle_dos_magic survived the restart")
                if read_bytes(t, want, 4) != b"\0\0\0\0":
                    fails.append("dosio_want_uci ('UCI!') survived the restart")
        finally:
            mgr.release(target)

    for f in fails:
        print(f"FAIL  {f}")
    print(f"BSS ${bss:04X}+${size:X}: "
          + ("cleared at boot" if not fails else f"{len(fails)} failure(s)"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
