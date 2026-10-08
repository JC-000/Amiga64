#!/usr/bin/env python3
"""S3.7: the AROS machine profile and its boot milestones (W11-A).

docs/WAVE11_BRIEF.md C2-C4, docs/S3_PLAN.md §3.2-§3.5 and §4 "S3.7";
contract docs/CONTRACTS.md "Machine profile and AROS boot (S3.7)".

Boot legs (need the AROS ROM cache, tools/aros/fetch.sh; without it they
print a SKIP line and the run exits 0, like tests/aros_oracle.py).  ONE
boot per mach_s value: the REU is zeroed (tests/aros/mboot.s aros_fill),
the two ROM banks and the overlay longs are staged (host/machine.py
stage_plan; VICE: the runner's test_load_chunk, U64E: the harness's
SocketDMA REU write), read back by checksum on the C64 (aros_sum), and
aros_boot runs mach_run while the host watches the serial ring
(chip_ser/chip_sern) and the guest frame counter:
  m1   serial "ROMInfo: 1MiB ROM detected"
  m2   serial "romtaginit done"
  m3   the no-media ("insert disk") screen: needs the blitter (W11-B);
       until it lands this leg is expected RED (the leg reports what it
       saw: display mode, conversions, and the model frames it compared)
  ablate  (red evidence) the same boot with exception mode off
       (st_exc = 0): the 68000 CPU probe's MOVEC halts HALT_ILLEGAL long
       before ROMInfo, so m1 must fail

Machine-loop legs (no ROM; VICE): C4 and C3 of the brief.
  wkhook  C4.2: a STOPped CPU whose only possible waker is the copper
          (mach_fhook: a copper list SETs INTREQ COPER every frame; no
          client slot, no slot-0 hook) must wait and take the interrupt,
          not return HALT_STOP
  s0      C4.1: guest progress (main-loop instructions per real second
          and per guest frame) at s = 0..6 under the full machine profile
          (CIA, copper frame hook, display hook, a VERTB handler; copper
          off / 4 / 28 MOVEs per frame): every s reaches HALT (no
          livelock), and at s >= 4 the loop keeps >= 75% of the s = 6
          rate.  s <= 1 starves this idle guest on a 1 MHz host by
          arithmetic (docs/PERF.md (l)); AROS's own low-s stalls are a
          different matter (CONTRACTS S3.7 "C4")
  pix     C3: a change of the pixel epoch disp_st+105 alone (fields and
          gen unchanged) converts the display again

    python3 -m tests.aros_boot --prg build/X/runner.prg [--legs m1,m2]
           [--s 4] [--backend vice|u64] [--timeout 1800] [--out DIR]
    python3 -m tests.aros_boot --prg ... --mutants [--only a,b]

Exit 0 = every selected leg passed (or SKIP); --mutants: every mutant
failed at least one of its legs.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import machine  # noqa: E402

MB_ORG = 0xC000                 # tests/aros/mboot.s: buf_case
MB_END = 0xC800
P = 0xC900                      # buf_result + $100
R = 0xC940                      # buf_result + $140
TRAMP = R + 0x30                # VICE: jsr aros_boot / jmp * (goto target)
ZP_FRAMES = 0x96                # rc zp mach_frames (u32 LE)
DRV_SYMS = ("emu_reset", "mem_read32", "mem_rec_dirty", "mach_init",
            "mach_run", "mach_s", "chip_reset", "cia_reset", "cop_reset",
            "disp_init", "snap_arm")    # snap_arm: W12-T (mboot.s, supervisor-approved)
MILESTONES = {"m1": b"ROMInfo: 1MiB ROM detected",
              "m2": b"romtaginit done"}
# boot model (tools/s3probe/aros_boot_model.py, this ROM): the instruction
# count and guest frame at which each milestone's serial line completes
MODEL_AT = {"m1": (1_256_189, 95), "m2": (2_640_815, 212),
            "m3": (3_429_641, 343)}
BOOT_LEGS = ("m1", "m2", "m3", "ablate")
POLL_S = 0.02                   # U64E: the ring holds CHIP_SERSZ (1024) bytes, AROS sends ~400 in ~1 s
POLL_VICE_S = 0.25              # VICE: every monitor read halts the 6510 (warp is slower)
EVID = os.path.join(ROOT, "build", "evidence", "w11-aros")
VERBOSE = False
DRV_OVERRIDE: list = [None]
INFRA = "INFRA: "                # a failure of the rig, not of the code under test     # a mutant driver source (--mutants)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------

DRV_SRC = os.path.join(ROOT, "tests", "aros", "mboot.s")


def build_driver(labels_path: str, out_dir: str,
                 src: str | None = None) -> tuple[bytes, dict]:
    from c64_test_harness import Labels
    from tests.harness_util import lookup_label
    labels = Labels.from_file(labels_path)
    defs = []
    for n in DRV_SYMS:
        defs += ["-D", f"{n}=${lookup_label(labels, n):04X}"]
    os.makedirs(out_dir, exist_ok=True)
    obj, out = os.path.join(out_dir, "mboot.o"), os.path.join(out_dir, "mboot.bin")
    cfg = os.path.join(out_dir, "mboot.cfg")
    lbl = os.path.join(out_dir, "mboot.lbl")
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $%04X; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n"
                % (MB_ORG, MB_END - MB_ORG))
    for cmd in (["ca65", "-g", *defs, "--include-dir",
                 os.path.join(ROOT, "tests", "aros"), "-o", obj, src or DRV_SRC],
                ["ld65", "-C", cfg, "-o", out, "-Ln", lbl, obj]):
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        if r.returncode:
            raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")
    lab = {}
    for line in open(lbl):
        _, a, n = line.split()
        lab[n.lstrip(".")] = int(a, 16)
    return open(out, "rb").read(), lab


class CaptureError(RuntimeError):
    """The host could not read the serial ring consistently."""


class Machine:
    """One prepared target (runner booted, dispatcher on the U64E)."""

    def __init__(self, target, labels, prg: str):
        from tests.harness_util import lookup_label
        self.tg, self.t = target, target.transport
        self.vice = target.backend == "vice"
        self.labels = labels
        self.lab = lambda n: lookup_label(labels, n)
        d = os.path.join(os.path.dirname(os.path.abspath(prg)), "aros-drv")
        self.retries = 0                # serial snapshots read again
        self.sersz = ser_size(self.lab)  # chip_ser ring bytes (CHIP_SERSZ)
        # round 1, R3: a ring segment is ONE readmem GET on the U64E/C64U
        # (the transport's read_memory, no 128-byte split; verified up to
        # 1024 B on both firmwares, docs/HARDWARE.md); VICE keeps read_bytes
        self.one_get = not self.vice
        self.drv, self.dl = build_driver(os.path.splitext(prg)[0] + ".lbl", d,
                                         DRV_OVERRIDE[0])

    def w(self, addr: int, data: bytes) -> None:
        from c64_test_harness import write_bytes
        write_bytes(self.t, addr, data)

    def r(self, addr: int, n: int) -> bytes:
        from c64_test_harness import read_bytes
        b = read_bytes(self.t, addr, n)
        if self.vice:
            self.t.resume()             # a monitor read halts the 6510
        return b

    def load_driver(self) -> None:
        self.w(MB_ORG, self.drv)
        if self.r(MB_ORG, len(self.drv)) != self.drv:
            raise RuntimeError("driver readback mismatch")

    def call(self, name: str, timeout: float = 600.0) -> bytes:
        from tests.harness_util import call_runner
        self.w(R, b"\0")
        call_runner(self.tg, self.dl[name], timeout)
        res = self.r(R, 0x30)
        if res[0] != 0xFF:
            raise RuntimeError(f"{name} did not complete")
        return res

    def stage(self, rom: machine.Rom) -> dict:
        """Zero the REU, stage the ROM plan, verify it by checksum."""
        t0 = time.monotonic()
        self.load_driver()
        self.w(P, bytes([0, 0]))                # banks 0.., 256 of them
        self.call("aros_fill")
        t_fill = time.monotonic() - t0
        plan = machine.stage_plan(rom)
        if self.vice:
            from tests.run_program import Chunk, Session
            sess = Session(target=self.tg, labels=self.labels, timeout=300.0)
            sess.stage([Chunk(a, d) for a, d in plan])   # test_load_chunk
            self.load_driver()                  # staging used buf_case
        else:
            try:                        # the harness's SocketDMA REUWRITE
                for a, d in plan:
                    self.t.socket_dma_reu_write(a, d)
                self.staged_by = "socket_dma_reu_write"
            except Exception as e:      # no DMA service: through the runner
                log(f"  SocketDMA REU write unavailable ({e}); staging through "
                    "test_load_chunk (slow)")
                from tests.run_program import Chunk, Session
                sess = Session(target=self.tg, labels=self.labels, timeout=300.0)
                sess.stage([Chunk(a, d) for a, d in plan])
                self.load_driver()
                self.staged_by = "test_load_chunk"
        t_stage = time.monotonic() - t0 - t_fill
        sums = {}
        img = {a: d for a, d in plan}
        page0 = rom.rom[:8].ljust(256, b"\0")
        for name, a, want in (("ext", machine.AROS_EXT, img[machine.AROS_EXT]),
                              ("rom", machine.AROS_ROM, img[machine.AROS_ROM]),
                              ("page0", 0, page0),
                              ("hole", 0xF00000, bytes(256))):
            self.w(P + 4, struct.pack("<II", a, len(want)))
            got = struct.unpack_from("<I", self.call("aros_sum"), 4)[0]
            exp = machine.fletcher(want)
            sums[name] = (got, exp)
            if got != exp:
                raise RuntimeError(f"REU readback {name} at ${a:06X}: "
                                   f"${got:08X} != ${exp:08X}")
        return {"fill_s": round(t_fill, 1), "stage_s": round(t_stage, 1),
                "verify_s": round(time.monotonic() - t0 - t_fill - t_stage, 1),
                "sums_ok": sorted(sums)}

    def start(self, s: int, exc: bool = True) -> None:
        # rev-aros round 2: the serial ring and its count are zeroed from the
        # host before every boot (aros_boot's chip_reset zeroes them again),
        # so a stale read of a slot surfaces as a NUL, never as a byte of a
        # previous boot; the read-back proves the zeroes landed.
        ser, sern, z = self.lab("chip_ser"), self.lab("chip_sern"), bytes(self.sersz)
        self.w(ser, z)
        self.w(sern, bytes(2))
        if self.r(ser, self.sersz) != z or self.r(sern, 2) != bytes(2):
            raise CaptureError("the serial ring did not read back as zero")
        self.w(P + 12, bytes([s, 1 if exc else 0]))
        self.w(R, b"\0")
        a = self.dl["aros_boot"]
        if self.vice:
            from c64_test_harness import goto
            self.w(TRAMP, bytes([0x20, a & 0xFF, a >> 8,
                                 0x4C, TRAMP + 3 & 0xFF, (TRAMP + 3) >> 8]))
            goto(self.t, TRAMP)
        else:
            from c64_test_harness import write_bytes
            disp = self.tg._u64_dispatch
            write_bytes(self.t, disp["jsr_operand"], bytes([a & 0xFF, a >> 8]))
            write_bytes(self.t, disp["mbox"], bytes([0x01, 0x00]))

    def cpu_state(self) -> str:
        return _cpu_state(self)

    def pcs(self, n: int = 16, gap: float = 0.05) -> list[int]:
        out = []
        for _ in range(n):
            out.append(struct.unpack("<I", self.r(0x50, 4))[0])
            time.sleep(gap)
        return out

    def count(self) -> int:
        return struct.unpack("<H", self.r(self.lab("chip_sern"), 2))[0]

    def count_untorn(self) -> int:
        """chip_sern, re-read while it may be torn.  chip.s bumps the low
        byte, then (on its carry) the high byte, so a 2-byte read racing the
        carry is off by +-256 and shows a low byte of $FF (+256: low read
        before the carry, high after) or $00 (-256).  With the 256-byte ring
        a +-256 error always broke the bracket in serial(); with 1024 slots
        it can fit, so a count whose low byte is $FF/$00 is accepted only
        when a second read returns the same value (two reads ~4-112 ms
        apart cannot both land inside the guest's few-cycle carry window)."""
        for _ in range(64):
            n = self.count()
            if n & 0xFF not in (0x00, 0xFF) or self.count() == n:
                return n
            self.retries += 1
        raise CaptureError(f"chip_sern never settled at a carry ({n})")

    def seg(self, addr: int, n: int) -> bytes:
        """One contiguous ring segment.  U64E/C64U: ONE REST readmem GET
        through the transport (the harness's read_bytes would split a read
        above 256 B into 128-byte GETs, ~112 ms each on the C64U, and the
        guest keeps writing between them); VICE: read_bytes (the monitor
        read halts the 6510, so the split costs no consistency)."""
        if self.one_get:
            b = self.t.read_memory(addr, n)
            if len(b) != n:
                raise CaptureError(f"readmem ${addr:04X}: {len(b)} of {n} bytes")
            return b
        return self.r(addr, n)

    def ring_slots(self, lo: int, hi: int) -> bytearray:
        """A ring image (self.sersz bytes) holding the slots of the byte
        indices lo..hi-1 (mod 65536, at most self.sersz of them); the other
        slots are 0.  At most two contiguous segments (the run may wrap
        past the ring's end), each one seg() read."""
        sz, ser = self.sersz, self.lab("chip_ser")
        ring, k = bytearray(sz), (hi - lo) & 0xFFFF
        assert k <= sz
        a = lo % sz
        first = min(k, sz - a)
        if first:
            ring[a:a + first] = self.seg(ser + a, first)
        if k - first:
            ring[0:k - first] = self.seg(ser, k - first)
        return ring

    def serial(self, seen: int) -> tuple[int, bytes]:
        """(n, ring): ring slots of byte indices seen..n-1 are final (slot =
        index mod self.sersz; chip.s CHIP_SERSZ = 1024).  On the U64E/C64U
        the guest runs during the reads (chip.s stores the byte, then bumps
        the count), so the ring read is bracketed by two count reads
        n1 <= n2: a slot below n1 was written before the ring read began,
        and none of seen..n1-1 was overwritten during it as long as
        n2 - seen <= self.sersz.  Only those slots are read: on the U64E/C64U one
        poll is 1 GET when nothing is new, else count + 1 or 2 ring GETs
        (seg: one GET per contiguous segment) + count, plus one more count
        GET when a count's low byte is $FF/$00; one GET is ~112 ms on the
        C64U.  A count that may be torn
        across the low byte's carry is read again (count_untorn); any
        bracket that breaks these bounds is read again, and one that never
        settles raises CaptureError (fix round 1, HIGH-2: a capture defect
        fails, never passes silently).  Two count reads are rarely EQUAL
        during the burst, so equality is not required."""
        sz = self.sersz
        for _ in range(64):
            n1 = self.count_untorn()
            a = (n1 - seen) & 0xFFFF
            if a == 0:
                return n1, bytes(sz)
            if a <= sz:
                ring = self.ring_slots(seen, n1)
                n2 = self.count_untorn()
                b = (n2 - seen) & 0xFFFF
                if a <= b <= sz:
                    return n1, bytes(ring)
            self.retries += 1
        raise CaptureError("no consistent serial snapshot in 64 tries "
                           f"(last count {n1}, seen {seen}, ring {sz})")

    def serial_final(self, seen: int) -> tuple[int, bytes]:
        """After mach_run returned (the guest writes no more): the count is
        exact (nothing races it), so read it and the slots seen..n-1 once.
        When more than a ring's worth arrived since *seen*, the last
        self.sersz bytes are read (take() counts the rest as lost)."""
        n = self.count()
        lo = seen if (n - seen) & 0xFFFF <= self.sersz else (n - self.sersz) & 0xFFFF
        return n, bytes(self.ring_slots(lo, n))

    def frames(self) -> int:
        return struct.unpack("<I", self.r(ZP_FRAMES, 4))[0]


def _cpu_state(m: "Machine") -> str:
    zp = m.r(0x50, 6)
    pc = struct.unpack_from("<I", zp, 0)[0]
    shd = m.r(m.lab("chip_shd") + 0x1C, 4)
    nirq = struct.unpack("<I", m.r(m.lab("mach_nirq"), 4))[0]
    stp = m.r(m.lab("mach_stp"), 1)[0]
    return (f"PC ${pc:06X} SR ${zp[5]:02X}{zp[4]:02X} INTENAR ${shd[0]:02X}{shd[1]:02X} "
            f"INTREQR ${shd[2]:02X}{shd[3]:02X} irqs {nirq} stopped {stp}")


def ser_size(lab) -> int:
    """The chip_ser ring size from the runner's labels: the exported
    CHIP_SERSZ, cross-checked against the label distance chip_ser ->
    chip_logn (chip.s BSS order; chip.s asserts the same at link time)."""
    sz = lab("CHIP_SERSZ")
    if lab("chip_logn") - lab("chip_ser") != sz or not sz or 0x10000 % sz:
        raise RuntimeError(f"chip_ser ring size {sz}: labels disagree "
                           f"(chip_logn - chip_ser = "
                           f"{lab('chip_logn') - lab('chip_ser')})")
    return sz


def take(text: bytearray, ring: bytes, seen: int, n: int,
         sz: int) -> tuple[int, int]:
    """Append the bytes seen..n-1 (slot = index mod sz) to *text*; returns
    (the new seen, bytes lost because the ring wrapped between reads)."""
    new, lost = (n - seen) & 0xFFFF, 0
    if new > sz:
        lost = new - sz
        seen = (n - sz) & 0xFFFF
        new = sz
    for i in range(new):
        text.append(ring[(seen + i) % sz])
    return n, lost


def watch(m: Machine, want: list[str], timeout: float, out: dict) -> None:
    """Poll the serial ring until every milestone in *want* is seen, the
    boot returns, or *timeout*.  Fills out["serial"], out["at"].  When the
    boot has returned, one final read takes what the guest printed after
    the last poll (bytes printed just before a halt are not dropped)."""
    text, seen, lost = bytearray(), 0, 0
    sz = m.sersz
    at: dict[str, dict] = {}
    t0 = time.monotonic()
    out.update(at=at)
    last_print = last_slow = -1e9
    fr, rh = 0, b"\0" * 16
    while True:
        try:
            n, ring = m.serial(seen)
        except CaptureError as e:
            out.update(capture_error=str(e), serial=text.decode("latin-1"))
            log(f"  CAPTURE ERROR: {e}")
            return
        el = time.monotonic() - t0
        slow = el - last_slow >= 1.0
        if slow:                            # the rest once a second
            last_slow = el
            fr, rh = m.frames(), m.r(R, 16)
        if VERBOSE and slow:                # where the guest spends its time
            zp = m.r(0x50, 6)
            k = f"{'S' if zp[5] & 0x20 else 'U'}{zp[5] & 7}"
            hist = out.setdefault("sr_hist", {})
            hist[k] = hist.get(k, 0) + 1
        new = (n - seen) & 0xFFFF
        if new > 0x8000:                    # the count went backwards
            out.update(capture_error=f"chip_sern went back from {seen} to {n}",
                       serial=text.decode("latin-1"))
            log(f"  CAPTURE ERROR: {out['capture_error']}")
            return
        seen, nlost = take(text, ring, seen, n, sz)
        lost += nlost
        for k in want:
            if k in MILESTONES and k not in at and MILESTONES[k] in text:
                fr = m.frames()
                at[k] = {"wall_s": round(el, 2), "frames": fr,
                         "model_icount": MODEL_AT[k][0],
                         "ips_est": round(MODEL_AT[k][0] / max(el, 1e-6))}
                log(f"  {k} reached: {el:.1f} s, guest frame {fr} "
                    f"(model: frame {MODEL_AT[k][1]})")
        out.update(serial=text.decode("latin-1"), lost=lost, frames=fr,
                   capture_retries=m.retries,
                   wall_s=round(el, 1))
        if rh[0] == 0xFF:
            # mach_run returned: the guest writes no more, so this count is
            # exact; take what it printed since the poll above (review: the
            # ablate leg could pass on bytes dropped here)
            n, ring = m.serial_final(seen)
            if (n - seen) & 0xFFFF > 0x8000:
                out.update(capture_error=f"chip_sern went back from {seen} to {n}",
                           serial=text.decode("latin-1"))
                log(f"  CAPTURE ERROR: {out['capture_error']}")
                return
            seen, nlost = take(text, ring, seen, n, sz)
            lost += nlost
            for ms in want:
                if ms in MILESTONES and ms not in at and MILESTONES[ms] in text:
                    at[ms] = {"wall_s": round(el, 2), "frames": m.frames(),
                              "model_icount": MODEL_AT[ms][0], "final_read": True,
                              "ips_est": round(MODEL_AT[ms][0] / max(el, 1e-6))}
                    log(f"  {ms} reached (in the final read after the return)")
            out.update(serial=text.decode("latin-1"), lost=lost, final_count=n)
            out["returned"] = {"st_halt": rh[1], "st_reason": rh[2],
                               "pc": struct.unpack_from("<I", rh, 8)[0],
                               "frames": struct.unpack_from("<I", rh, 12)[0]}
            log(f"  mach_run returned: {out['returned']}")
            return
        if all(k in at for k in want if k in MILESTONES):
            if VERBOSE:
                log(f"  SR samples (S/U + IPL): {out.get('sr_hist')}")
            return
        if el > timeout:
            log(f"  timeout after {el:.0f} s (frame {fr}, {len(text)} serial bytes)")
            return
        if el - last_print > 30:
            last_print = el
            log(f"  ... {el:.0f} s, guest frame {fr}, serial {len(text)} B; "
                f"{m.cpu_state()}")
            if VERBOSE:
                log("      PCs " + " ".join(f"{x:06X}" for x in sorted(set(m.pcs()))))
        time.sleep(POLL_S if not m.vice else POLL_VICE_S)


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def vice_session(prg: str, fn):
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    cfg = ViceConfig(prg_path=prg, warp=True, sound=False, ntsc=False,
                     extra_args=list(REU_VICE_ARGS))
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        log(f"VICE pid {getattr(tg, 'pid', '?')}")
        try:
            prepare_target(tg, prg)
            return fn(Machine(tg, labels, prg))
        finally:
            mgr.release(tg)


def u64_session(prg: str, out_dir: str, mhz: int, fn):
    """One DeviceLock hold (tests/bench/u64_session.u64_hold: config
    snapshot before, exact restore and C64 reset after, even on failure).
    The CPU speed is set to *mhz* FIRST and the machine rebooted
    (`reboot(client)`: the harness README requires it after a turbo switch
    between REU-heavy workloads, stale DMA state), then the runner boots
    and the boot runs at *mhz* (W11 fix round 2)."""
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import reboot
    from tests.bench.u64_session import u64_hold
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    os.makedirs(out_dir, exist_ok=True)
    with u64_hold(out_dir) as hold:
        tg = hold["target"]
        tg.transport.set_speed(mhz)
        reboot(tg.transport.client)
        log(f"U64E: CPU speed {mhz} MHz, rebooted (reboot(client))")
        prepare_target(tg, prg)
        log(f"U64E: runner booted at {mhz} MHz")
        try:
            return fn(Machine(tg, labels, prg))
        finally:
            tg.transport.set_speed(1)
            reboot(tg.transport.client)


# --------------------------------------------------------------------------
# Boot legs
# --------------------------------------------------------------------------

def boot_legs(m: Machine, rom: machine.Rom, legs: list[str], s: int,
              timeout: float, rep: dict) -> list[str]:
    fails: list[str] = []
    want = [k for k in ("m1", "m2", "m3") if k in legs]
    if want:
        log(f"boot at mach_s = {s} ({m.tg.backend}), milestones {want}")
        r = rep.setdefault(f"boot_s{s}", {})
        r["stage"] = m.stage(rom)
        log(f"  staged: {r['stage']}")
        m.start(s)
        watch(m, want, timeout, r)
        if r.get("capture_error"):
            fails.append(f"{INFRA}serial capture: {r['capture_error']}")
        if r.get("lost"):
            fails.append(f"serial: {r['lost']} byte(s) lost (the {m.sersz}-byte ring "
                         f"wrapped between two polls)")
        mf, _ = model_frames()
        ref = (mf or {}).get("serial", "")
        if ref:
            # every byte captured must be the model's (the boot's serial text
            # is deterministic): a capture defect -- a NUL, a dropped or a
            # stale byte -- fails here, wherever the run stopped
            got = r["serial"]
            n = min(len(got), len(ref))
            bad = next((i for i in range(n) if got[i] != ref[i]), None)
            r["serial_matches_model"] = bad is None
            r["serial_compared"] = n
            if bad is not None:
                fails.append(f"serial: byte {bad} is {got[bad]!r}, the boot model's "
                             f"is {ref[bad]!r} ({n} bytes compared)")
            else:
                log(f"  serial: {n} bytes compared, equal to the boot model's")
        for k in want:
            if k == "m3":
                fails += leg_m3(m, r, timeout)
            elif k not in r["at"]:
                fails.append(f"{k}: {MILESTONES[k].decode()!r} not on the serial "
                             f"line (frame {r['frames']}, {len(r['serial'])} "
                             f"bytes, returned {r.get('returned')})")
    if "ablate" in legs:
        log("ablate: the same boot with exception mode off (st_exc = 0)")
        r = rep.setdefault("ablate_noexc", {})
        r["stage"] = m.stage(rom)
        m.start(s, exc=False)
        watch(m, ["m1"], min(timeout, 300.0), r)
        af = ablate_verdict(r)
        fails += af
        if not af:
            log(f"  red as expected: {r['returned']}")
    return fails


def ablate_verdict(r: dict) -> list[str]:
    """The ablate leg passes only on a clean capture that shows no m1 and a
    returned boot.  A capture error or lost bytes make the "m1 absent"
    verdict untrustworthy (the line may be in what was lost): INFRA, never
    a pass (round 1, R4)."""
    if r.get("capture_error"):
        return [f"{INFRA}ablate: serial capture: {r['capture_error']}"]
    if r.get("lost"):
        return [f"{INFRA}ablate: {r['lost']} serial byte(s) lost; m1 may be "
                "among them"]
    if "m1" in r.get("at", {}):
        return ["ablate: m1 reached with exception mode off"]
    if "returned" not in r:
        return ["ablate: the boot neither halted nor reached m1"]
    return []


MODEL_JSON = os.path.join(ROOT, "build", "aros", "model_frames.json")
VENV_PY = os.path.expanduser("~/.cache/amiga64/s3probe-venv/bin/python")


def model_frames() -> tuple[dict | None, str]:
    """The model's conversion hashes (tests/aros/model_frames.py), built
    once into build/aros/ (git-ignored) with the s3probe venv."""
    if not os.path.exists(MODEL_JSON):
        if not os.path.exists(VENV_PY):
            return None, f"no s3probe venv at {VENV_PY} (tools/s3probe/setup_venv.sh)"
        os.makedirs(os.path.dirname(MODEL_JSON), exist_ok=True)
        r = subprocess.run([VENV_PY, os.path.join(ROOT, "tests", "aros", "model_frames.py"),
                            MODEL_JSON], capture_output=True, text=True, cwd=ROOT)
        if r.returncode or not os.path.exists(MODEL_JSON):
            return None, f"model_frames.py failed: {r.stdout}{r.stderr}"
    with open(MODEL_JSON) as f:
        return json.load(f), ""


def c64_hash(m: "Machine") -> tuple[str, dict]:
    """The current VIC picture, hashed as model_frames.conv_hash does.
    The guest runs inside mach_run with $01 = $35 (KERNAL out, I/O in): the
    bitmap at $E000 is RAM, colour RAM and the VIC registers are I/O, and
    the screen matrix is RAM UNDER the I/O window.  VICE: the 6510 is halted
    for the whole snapshot, so $01 is switched to $34 for the matrix and put
    back to the value READ, not to a constant (fix: the first m3 version
    restored $37, mapping the KERNAL in under the running guest).  U64E: the
    6510 keeps running and $01 is never touched: the matrix is left out
    (the model side hashes the same way, "nomatrix")."""
    from c64_test_harness import read_bytes, write_bytes
    sys.path.insert(0, os.path.join(ROOT, "tests", "aros"))
    import model_frames as MF
    bm, sc = m.lab("c2v_bitmap"), m.lab("c2v_screen")
    t = m.t
    if m.vice:
        port = read_bytes(t, 0x0001, 1)
        bitmap = read_bytes(t, bm, 8000)
        t.write_memory(0x0001, bytes([0x34]), override="aros_boot: read RAM under I/O")
        screen = read_bytes(t, sc, 1000)
        t.write_memory(0x0001, port, override="aros_boot: restore $01")
    else:
        bitmap, screen = read_bytes(t, bm, 8000), None
    colram = read_bytes(t, 0xD800, 1000)
    vic = {a: read_bytes(t, a, 1)[0] for a in MF.VIC_MASK}
    if m.vice:
        t.resume()
    return MF.conv_hash(bitmap, screen, colram, vic), vic


def leg_m3(m: Machine, r: dict, timeout: float) -> list[str]:
    """Milestone 3: a conversion on the C64 equal to one of the model's
    insert-disk conversions (first seen at or after INSERT_DISK_FRAME).
    Needs the blitter (W11-B): without it the screen stays without pixels
    (S3_PLAN §3.4: the boot logic completes, no picture)."""
    mf, why = model_frames()
    if mf is None:
        return [f"m3: no model reference: {why}"]
    key = (lambda h, v: h) if m.vice else (lambda h, v: v["nomatrix"])
    every = {key(h, v): v for h, v in mf["hashes"].items()}
    ok = {key(h, v): v for h, v in mf["hashes"].items()
          if v["frame"] >= mf["insert_disk_frame"]}
    t0 = time.monotonic()
    seen: dict[str, int] = {}
    out = r.setdefault("m3", {"model_ok": {h[:12]: v["frame"] for h, v in ok.items()}})
    last = -1
    while time.monotonic() - t0 < timeout:
        nconv = struct.unpack("<H", m.r(m.lab("disp_nconv"), 2))[0]
        mode = m.r(m.lab("disp_mode"), 1)[0]
        if nconv != last or not m.vice:     # U64E: every poll (a torn read
            last = nconv                    # of a static screen heals)
            h, vic = c64_hash(m)
            fr = struct.unpack("<I", m.r(ZP_FRAMES, 4))[0]
            if h not in seen:
                seen[h] = fr
                hit = every.get(h)
                log(f"  m3: conversion {nconv} (mode {mode}) at guest frame {fr}: "
                    f"{h[:12]} {'= model frame ' + str(hit['frame']) if hit else '(not a model frame)'}")
            if h in ok:
                el = time.monotonic() - t0 + r.get("wall_s", 0)
                out.update(reached={"hash": h[:12], "model_frame": ok[h]["frame"],
                                    "guest_frame": fr, "wall_s": round(el, 1),
                                    "ips_est": round(MODEL_AT["m3"][0] / el)})
                r["at"]["m3"] = out["reached"]
                log(f"  m3 reached: {el:.1f} s, guest frame {fr}")
                return []
        time.sleep(2.0)
    out.update(seen={h[:12]: f for h, f in seen.items()}, nconv=last,
               mode=m.r(m.lab("disp_mode"), 1)[0], why=m.r(m.lab("disp_why"), 1)[0])
    return [f"m3: no conversion equal to a model insert-disk frame within "
            f"{timeout:.0f} s ({last} conversions, {len(seen)} distinct, display "
            f"mode {out['mode']} why {out['why']}); needs the blitter (W11-B)"]


# --------------------------------------------------------------------------
# Machine-loop legs (no ROM): C4.2 wkhook, C4.1 s0, C3 pix
# --------------------------------------------------------------------------

COPER = 0x0010                  # INTREQ/INTENA bit 4 (level 3)
COP1LC, COPJMP1, DMACON = 0xDFF080, 0xDFF088, 0xDFF096


def prog_wkhook(dma: bool = True) -> tuple:
    """Exception mode, IPL 0 after the STOP's SR.  The copper list SETs
    INTREQ COPER; the CPU DMACON write that turns copper DMA on runs it once
    synchronously, the guest clears that request, enables COPER (INTEN) and
    STOPs.  Only the copper's frame hook can wake it now: no client slot is
    armed and no slot-0 hook is installed.  The level-3 handler counts and
    HALTs (HALT_OK)."""
    from tests import run_irq as I
    from tests.run_mach import G, HALT, mv_imm_abs_w
    g = G()
    I.vec(g, 27, "h3")
    I.clr_l(g, I.CNT)
    mv_imm_abs_w(g, 0x7FFF, I.INTENA)
    mv_imm_abs_w(g, 0x7FFF, I.INTREQ)
    g.label("lst_at")
    I.mv_l_imm_abs(g, 0, COP1LC)                # patched: the list address
    mv_imm_abs_w(g, 0, COPJMP1)                 # load the copper PC
    if dma:
        mv_imm_abs_w(g, 0x8280, DMACON)         # DMAEN | COPEN: one sync run
    mv_imm_abs_w(g, COPER, I.INTREQ)            # clear that run's request
    mv_imm_abs_w(g, 0xC000 | COPER, I.INTENA)
    g(0x4E72, 0x2000)                           # stop #$2000
    g.label("after")
    g(HALT)                                     # (not reached on the fix)
    g.label("h3")
    I.addq_l_abs(g, I.CNT)
    g(HALT)
    return g


def leg_wkhook(prg: str) -> list[str]:
    """C4.2: with copper DMA on, the frame hook wakes a STOP (one COPER
    interrupt, then HALT_OK).  Fix round 1, L6: with copper DMA off the hook
    cannot post, so the same STOP returns HALT_STOP at once (reason 4)."""
    from tests import run_irq as I
    from tests.run_mach import BASE

    def body(rig) -> list[str]:
        f: list[str] = []
        if not rig.seam:
            return ["wkhook: the PRG has no event seam"]
        od = rig.dcall
        rig.dcall = lambda name, timeout=WK_TO: od(name, timeout)
        for tag, dma in (("wkhook", True), ("wkoff", False)):
            rig.call(rig.lab("cop_reset"))      # mach_fhook := cop_frame, DMA off
            g = prog_wkhook(dma)
            n = len(I.link(g, BASE))
            lst = BASE + 2 * n
            w = I.link(g, BASE)
            at = g.labels["lst_at"] + 1         # the move.l's immediate
            w[at], w[at + 1] = lst >> 16, lst & 0xFFFF
            lw = [0x09C, 0x8000 | COPER, 0xFFFF, 0xFFFE]
            pc, l2 = rig.stage(w, lw)
            assert l2 == lst
            hook = struct.unpack("<H", rig.r(rig.lab("mach_fhook"), 2))[0]
            if hook != rig.lab("cop_frame"):
                f.append(f"{tag}: mach_fhook ${hook:04X} is not cop_frame")
            cbv0 = rig.r(rig.lab("mach_cbv"), 2)
            try:
                res = rig.run(pc, s=2, flags=I.F_INIT | I.F_SUPER, sr=0x27)
            except Exception as e:
                f.append(f"{tag}: no return within {WK_TO:.0f} s ({type(e).__name__}): "
                         f"the STOP waits for a wake-up that cannot come")
                log(f"  {tag}: NO RETURN")
                return f                        # the machine state is unknown
            cnt = struct.unpack(">I", rig.guest_read(I.CNT, 4))[0]
            log(f"  {tag}: halt ${res['halt']:02X} reason {res['reason']} handler "
                f"runs {cnt} frames {res['frames'] - res['frames0']} "
                f"deliveries {res['nirq']} slot-0 hook {cbv0.hex()}")
            if dma and (res["halt"] != 1 or res["reason"] != 0 or cnt != 1):
                f.append(f"{tag}: halt ${res['halt']:02X} reason {res['reason']}, "
                         f"{cnt} handler runs: want HALT_OK after ONE COPER "
                         f"interrupt (the copper's frame hook can wake a STOP; "
                         f"HALT_STOP = reason 4 is the backlog bug)")
            if not dma and (res["halt"] != 1 or res["reason"] != 4 or cnt
                            or res["frames"] - res["frames0"] > 2):
                f.append(f"{tag}: halt ${res['halt']:02X} reason {res['reason']}, "
                         f"{cnt} handler runs after {res['frames'] - res['frames0']} "
                         f"frames: want HALT_STOP at once (copper DMA off)")
        return f
    return I.session(prg, body)


WK_TO = 60.0                    # s of VICE warp: a STOP that waits forever


S0_N = 3000                     # main-loop iterations (3 instructions each)
S0_TO = 240.0                   # s of VICE wall time: more = the guest starves
S0_WORK = {"idle": 0, "cop4": 4, "cop28": 28}  # copper MOVEs per frame
S0_S = (6, 4, 3, 2, 1, 0)
S0_KEEP = 0.75                  # s >= 4: the loop keeps >= 75% of the s = 6 rate
PAL_PHI2 = 985248


def prog_s0(lst: int) -> tuple:
    """Exception mode under the full machine profile: a level-3 VERTB
    handler (count, acknowledge, RTE: the cheapest one possible), copper
    DMA on with the list at *lst* (0: copper off), then S0_N iterations of
    addq/cmp/blt and HALT.  d0 = the iterations, CNT = handler runs."""
    from tests import run_irq as I
    from tests.run_mach import G, HALT, mv_imm_abs_w
    g = G()
    I.vec(g, 27, "h3")
    I.clr_l(g, I.CNT)
    g(0x7000)                                   # moveq #0,d0
    mv_imm_abs_w(g, 0x7FFF, I.INTENA)
    mv_imm_abs_w(g, 0x7FFF, I.INTREQ)
    if lst:
        I.mv_l_imm_abs(g, lst, COP1LC)
        mv_imm_abs_w(g, 0, COPJMP1)
        mv_imm_abs_w(g, 0x8280, DMACON)
    mv_imm_abs_w(g, 0xC020, I.INTENA)
    I.move_sr(g, 0x2000)
    g.label("spin")
    g(0x5280)                                   # addq.l #1,d0
    g(0x0C80, S0_N >> 16, S0_N & 0xFFFF)        # cmpi.l #N,d0
    g.bcc(0x6D00, "spin")                       # blt.w
    g(0x2239, *g.abs(I.CNT))                    # move.l CNT,d1
    g(HALT)
    g.label("h3")
    I.addq_l_abs(g, I.CNT)
    mv_imm_abs_w(g, 0x0020, I.INTREQ)
    g(0x4E73)                                   # rte
    return g


def leg_s0(prg: str, rep: dict) -> list[str]:
    """C4.1: guest progress per real second and per guest frame at each s
    under the machine profile (CIA installed by chip_reset, cop_reset's
    frame hook, disp_init's display hook with the display off).  PAL VICE:
    real time = frames x 14188 x 2^s phi2 (exact: the frame boundaries are
    phi2 deadlines).  A run that does not HALT within S0_TO is a starved
    guest."""
    from tests import run_irq as I
    from tests import run_mach as M
    from tests.run_mach import BASE, F_INIT, F_SUPER
    out = rep.setdefault("s0", {})
    fails: list[str] = []

    def body(rig) -> list[str]:
        f: list[str] = []
        orig = rig.call
        rig.call = lambda addr, timeout=S0_TO: orig(addr, timeout)
        for work, k in S0_WORK.items():
            n0 = len(I.link(prog_s0(1), BASE))
            lst = BASE + 2 * n0
            words = I.link(prog_s0(lst if k else 0), BASE)
            lw = []
            for i in range(k):
                lw += [0x180 + 2 * (i % 16), i]
            lw += [0xFFFF, 0xFFFE]
            for sv in S0_S:
                tag = f"s0 {work} s={sv}"
                rig.call(rig.lab("chip_reset"))         # CIAs, callbacks
                rig.call(rig.lab("cop_reset"))          # mach_fhook
                rig.call(rig.lab("disp_init"))          # cop_dhook
                pc, l2 = rig.stage(words, lw)
                assert not k or l2 == lst
                t0 = time.monotonic()
                try:
                    res = rig.run(pc, s=sv, flags=F_INIT | F_SUPER)
                except Exception as e:
                    out[tag] = {"starved": True, "wall_s": round(time.monotonic() - t0)}
                    f.append(f"{tag}: no HALT within {S0_TO:.0f} s ({type(e).__name__}):"
                             " the guest starves")
                    log(f"  {tag}: STARVED (no HALT in {S0_TO:.0f} s)")
                    return f                            # machine state unknown
                fr = max(res["frames"], 1)
                real = fr * 14188 * (1 << sv) / PAL_PHI2
                d0, d1 = res["d"][0], res["d"][1]
                ips = 3 * d0 / real
                out[tag] = {"frames": res["frames"], "real_s": round(real, 3),
                            "loop_ips": round(ips), "loop_per_frame": round(3 * d0 / fr, 1),
                            "handler_runs": d1, "halt": res["halt"],
                            "reason": res["reason"], "wall_s": round(time.monotonic() - t0, 1)}
                log(f"  {tag}: frames {res['frames']} real {real:.2f} s, main-loop "
                    f"{ips:.0f} instr/s, {3 * d0 / fr:.1f} per frame, handler {d1}")
                if res["halt"] != 1 or res["reason"] != 0 or d0 != S0_N:
                    f.append(f"{tag}: halt ${res['halt']:02X} reason {res['reason']} d0 {d0}")
        return f
    fails += M.session(prg, body)
    # Bounds (the finding is in docs/PERF.md "AROS boot"): every s HALTs (a
    # slow s is no livelock), and from s = 4 up the machine loop leaves
    # the guest >= S0_KEEP of its s = 6 rate.  s <= 1 is NOT bounded: on a
    # 1 MHz host the fixed per-frame work (pass, VERTB delivery, handler,
    # copper) fills most of a 14188-phi2 frame (AROS's own low-s stalls are
    # a different matter: CONTRACTS S3.7 "C4").
    for work in S0_WORK:
        ref = out.get(f"s0 {work} s=6", {}).get("loop_ips")
        for sv in S0_S:
            r = out.get(f"s0 {work} s={sv}")
            if sv >= 4 and ref and r and not r.get("starved") and \
                    r["loop_ips"] < S0_KEEP * ref:
                fails.append(f"s0 {work} s={sv}: {r['loop_ips']} main-loop instr/s "
                             f"< {S0_KEEP:.0%} of the s=6 rate {ref}")
    return fails


POLL_S_VAL = 6                  # s for leg poll (VICE: a frame ~1.4k instructions)
POLL_H = 1000                   # handler dbra iterations: ~2/3 of a frame at s = 6
POLL_N = 3000                   # main-loop iterations
POLL_KEEP = 0.75                # progress with the request pending / without
POLL_TO = 120.0                 # s of VICE warp


def prog_poll(pend: bool = True) -> object:
    """Fix round 1, HIGH-1: a level-3 VERTB handler that works ~POLL_H
    loop iterations.  pend: with requests pending under its IPL 3 all the
    while (VERTB acknowledged last, PORTS raised by the handler itself, as
    AROS's VBlank work runs with CIA-A pending); else with nothing pending
    (VERTB acknowledged first).  The level-2 handler acknowledges PORTS.  The main program
    counts to POLL_N and HALTs; d1 = VERTB handler runs, d2 = PORTS runs."""
    from tests import run_irq as I
    from tests.run_mach import G, HALT, mv_imm_abs_w
    g = G()
    I.vec(g, 27, "h3")
    I.vec(g, 26, "h2")
    I.clr_l(g, I.CNT)
    I.clr_l(g, I.CNT + 4)
    g(0x7000)                                   # moveq #0,d0
    mv_imm_abs_w(g, 0x7FFF, I.INTENA)
    mv_imm_abs_w(g, 0x7FFF, I.INTREQ)
    mv_imm_abs_w(g, 0xC028, I.INTENA)           # INTEN | VERTB | PORTS
    I.move_sr(g, 0x2000)
    g.label("spin")
    g(0x5280)                                   # addq.l #1,d0
    g(0x0C80, POLL_N >> 16, POLL_N & 0xFFFF)    # cmpi.l #N,d0
    g.bcc(0x6D00, "spin")                       # blt.w
    g(0x2239, *g.abs(I.CNT))                    # move.l CNT,d1
    g(0x2439, *g.abs(I.CNT + 4))                # move.l CNT+4,d2
    g(HALT)
    g.label("h3")
    I.addq_l_abs(g, I.CNT)
    if not pend:
        mv_imm_abs_w(g, 0x0020, I.INTREQ)       # acknowledge VERTB first:
        mv_imm_abs_w(g, 0x0008, I.INTREQ)       # nothing pending meanwhile
    else:
        mv_imm_abs_w(g, 0x8008, I.INTREQ)       # PORTS pending, masked by IPL 3
        mv_imm_abs_w(g, 0x0000, I.INTREQ)       # (same cost)
    g(0x3F07)                                   # move.w d7,-(sp)
    g(0x3E3C, POLL_H)                           # move.w #H,d7
    g.label("work")
    g.dbra(7, "work")                           # dbra d7,work
    g(0x3E1F)                                   # move.w (sp)+,d7
    mv_imm_abs_w(g, 0x0020, I.INTREQ)           # acknowledge VERTB (again)
    g(0x4E73)                                   # rte
    g.label("h2")
    I.addq_l_abs(g, I.CNT + 4)
    mv_imm_abs_w(g, 0x0008, I.INTREQ)           # acknowledge PORTS
    g(0x4E73)
    return g


def leg_poll(prg: str, rep: dict) -> list[str]:
    """Fix round 1, HIGH-1 (VICE): while a request waits under the IPL, the
    service passes poll every MACH_POLL phi2.  Their cost must not let a
    level-3 handler that fits in a frame at full speed outlast the frame
    (then the guest never leaves IPL 3: AROS's dispatcher livelock at VICE
    s <= 6).  s = POLL_S_VAL, VICE PAL: the main loop must reach HALT."""
    from tests import run_irq as I
    from tests.run_mach import BASE, F_INIT, F_SUPER
    from tests import run_mach as M
    out = rep.setdefault("poll", {})

    def body(rig) -> list[str]:
        orig = rig.call
        rig.call = lambda addr, timeout=POLL_TO: orig(addr, timeout)
        f, rate = [], {}
        for tag, pend in (("quiet", False), ("pending", True)):
            (pc,) = rig.stage(I.link(prog_poll(pend), BASE))
            try:
                res = rig.run(pc, s=POLL_S_VAL, flags=F_INIT | F_SUPER)
            except Exception as e:
                out[tag] = {"starved": True}
                log(f"  poll {tag}: no HALT within {POLL_TO:.0f} s")
                return f + [f"poll {tag}: no HALT within {POLL_TO:.0f} s "
                            f"({type(e).__name__}): the level-3 handler outlasts "
                            f"the frame"]
            d0, d1, d2 = res["d"][:3]
            fr = max(res["frames"], 1)
            rate[tag] = 3 * d0 / fr
            out[tag] = {"frames": res["frames"], "vertb": d1, "ports": d2,
                        "loop_per_frame": round(rate[tag], 1)}
            log(f"  poll {tag}: HALT after {res['frames']} frames, VERTB handler "
                f"runs {d1}, PORTS runs {d2}, main loop {rate[tag]:.1f} instr/frame")
            if res["halt"] != 1 or res["reason"] != 0 or d0 != POLL_N:
                f.append(f"poll {tag}: halt ${res['halt']:02X} reason {res['reason']} d0 {d0}")
        keep = rate["pending"] / max(rate["quiet"], 1e-9)
        out["keep"] = round(keep, 3)
        if keep < POLL_KEEP:
            f.append(f"poll: a request pending under the handler's IPL leaves the "
                     f"guest {keep:.0%} of its progress (bound {POLL_KEEP:.0%}): "
                     f"the MACH_POLL passes eat the frame")
        return f
    return M.session(prg, body)


LAT_K = 8                       # measurements per run
LAT_SLACK = {4: 384, 9: 64}     # E: the host work around the poll, fixed in
                                # phi2 (two trapped beam reads, the pass, the
                                # exception frame: ~4.8k phi2 at 1 MHz = ~300 E
                                # at s = 4, < 10 E at s = 9; VICE measured
                                # 544-560 E at s = 4, 126-128 E at s = 9)
LAT_S = (4, 9)
LAT_TABLE = 0x018000            # guest: K (t0, t1) VPOSR:VHPOSR longs


def prog_lat() -> object:
    """Fix round 2 (rev-aros): RTE / MOVE-to-SR recognition latency.  K
    times: at SR $2300 wait until VERTB is pending (masked: the same level),
    i.e. until a pass has posted it and armed the poll; then t0 := the beam
    (move.l VPOSR) and MOVE #$2000,SR; the level-3 handler takes t1 := the
    beam, acknowledges, flags.  Host: E ticks from t0 to t1."""
    from tests import run_irq as I
    from tests.run_mach import G, HALT, mv_imm_abs_w
    g = G()
    I.vec(g, 27, "h3")
    I.clr_l(g, I.CNT)
    mv_imm_abs_w(g, 0x7FFF, I.INTENA)
    mv_imm_abs_w(g, 0x7FFF, I.INTREQ)
    g(0x41F9, *g.abs(LAT_TABLE))                # lea TABLE,a0
    g(0x7E00 | (LAT_K - 1))                     # moveq #K-1,d7
    mv_imm_abs_w(g, 0xC020, I.INTENA)           # INTEN | VERTB
    g.label("next")
    I.move_sr(g, 0x2300)                        # IPL 3: VERTB masked
    mv_imm_abs_w(g, 0x0020, I.INTREQ)           # clear a stale one
    g.label("wv")
    g(0x0839, 5, *g.abs(0xDFF01F))              # btst #5,INTREQR+1
    g.bcc(0x6700, "wv")                         # beq.w: until a pass posts it
    I.clr_l(g, I.CNT + 4)                       # flag := 0
    g(0x20F9, *g.abs(0xDFF004))                 # move.l VPOSR,(a0)+   t0
    I.move_sr(g, 0x2000)                        # the IPL drop
    g.label("spin")
    g(0x4AB9, *g.abs(I.CNT + 4))                # tst.l flag
    g.bcc(0x6700, "spin")                       # beq.w
    g.dbra(7, "next")
    g(HALT)
    g.label("h3")
    g(0x20F9, *g.abs(0xDFF004))                 # move.l VPOSR,(a0)+   t1
    mv_imm_abs_w(g, 0x0020, I.INTREQ)
    I.mv_l_imm_abs(g, 1, I.CNT + 4)
    g(0x4E73)                                   # rte
    return g


def beam_cc(v: int) -> int:
    """VPOSR:VHPOSR long -> colour clocks into the frame."""
    line = ((v >> 16) & 1) << 8 | (v >> 8) & 0xFF
    return line * 227 + (v & 0xFF)


def leg_lat(prg: str, rep: dict) -> list[str]:
    """Fix round 2 (rev-aros must-fix 1): the documented bound "an IPL drop
    by RTE / MOVE to SR is recognised within one poll interval (<= 256 E
    for s >= 2) while a request is pending and a pass has seen it".  Every
    measured drop -> delivery <= 256 E + LAT_SLACK[s]."""
    from tests import run_irq as I
    from tests import run_mach as M
    from tests.run_mach import BASE, F_INIT, F_SUPER
    out = rep.setdefault("lat", {})

    def body(rig) -> list[str]:
        f: list[str] = []
        for sv in LAT_S:
            (pc,) = rig.stage(I.link(prog_lat(), BASE))
            res = rig.run(pc, s=sv, flags=F_INIT | F_SUPER)
            if res["halt"] != 1 or res["reason"] != 0:
                f.append(f"lat s={sv}: halt ${res['halt']:02X} reason {res['reason']}")
                continue
            raw = rig.guest_read(LAT_TABLE, 8 * LAT_K)
            ds = []
            for k in range(LAT_K):
                t0, t1 = struct.unpack_from(">II", raw, 8 * k)
                d = (beam_cc(t1) - beam_cc(t0)) % 70940
                ds.append(d / 5)
            bound = 256 + LAT_SLACK[sv]
            out[f"s{sv}"] = {"latency_E": [round(x, 1) for x in ds], "bound_E": bound}
            log(f"  lat s={sv}: drop -> delivery {min(ds):.0f}..{max(ds):.0f} E "
                f"(bound {bound} E: 256 + slack {LAT_SLACK[sv]})")
            if max(ds) > bound:
                f.append(f"lat s={sv}: an IPL drop was recognised {max(ds):.0f} E "
                         f"late (bound {bound} E)")
        return f
    return M.session(prg, body)


PIX_OFF = 105                   # disp_st + 105: the pixel epoch (C3)
PLANES = (0x100000, 0x4000)     # the two mc2 planes' region (base, length)


def pix_steps() -> list[dict]:
    """(name, restage seed or None, epoch delta, budget pre, conversion
    expected).  The fields and gen never change after the first step: only
    the pixels and the epoch do."""
    return [
        {"name": "first", "seed": 30, "dpix": 0, "conv": True},
        {"name": "pix-only", "seed": 31, "dpix": 1, "conv": True},
        {"name": "pix-same", "seed": 32, "dpix": 0, "conv": False},
        {"name": "pix-defer", "seed": 33, "dpix": 1, "conv": False,
         "ebud": "now"},
        {"name": "pix-due", "seed": None, "dpix": 0, "conv": True,
         "ebud": "due"},
        {"name": "pix-wrap", "seed": 34, "dpix": 255, "conv": True},
    ]


def leg_pix(prg: str) -> list[str]:
    """C3 on the run_pf shim build (tests/pf/shim.s: shim_kick = a fake
    cop_frame end calling cop_dhook): disp_init snapshots the epoch; a
    changed epoch with equal fields and gen converts again (the guest
    pixels changed); an unchanged one does not; inside mach_run the change
    waits for the DISP_N budget and is taken once it is due."""
    from c64_test_harness import Labels, UnifiedManager, ViceConfig
    from host import c2v_ref as Rf
    from tests import run_c2v as C
    from tests import run_pf as PF
    from tests.harness_util import REU_VICE_ARGS
    from tests.run_vectors import prepare_target
    sprg = prg if PF.has_label(prg, "shim_kick") else \
        PF.build(prg, "aros-pix", [], shim=True)
    labels = Labels.from_file(os.path.splitext(sprg)[0] + ".lbl")
    lut = Rf.build_lut()
    cfg = ViceConfig(prg_path=sprg, warp=True, sound=False,
                     extra_args=list(REU_VICE_ARGS))
    fails: list[str] = []
    st = dict(PF.STD, bplpt=[PLANES[0], PLANES[0] + 0x2000],
              color=PF.pal(40), gen=0x21)
    full = {**{k: 0 for k in ("bplcon1", "bplcon2", "bpl1mod", "bpl2mod")}, **st}
    full["bplpt"] = list(st["bplpt"]) + [0] * 4
    full["color"] = list(st["color"])
    with UnifiedManager(backend="vice", vice_config=cfg) as mgr:
        tg = mgr.acquire()
        try:
            prepare_target(tg, sprg)
            s = PF.Pf(tg, labels)
            dst = s.lab["disp_st"]
            mem = PF.Mem()
            data = PF.rnd(29, PLANES[1])
            s.stage(PLANES[0], data)
            mem.put(PLANES[0], data)
            s.wr(dst + PIX_OFF, b"\x5A")         # an epoch before disp_init
            C.call_checked(s, "disp_init", fails)
            s.wr(s.lab["mach_s"], b"\x10")       # E still across round trips
            s.call(s.lab["mach_init"])
            pix = 0x5A
            for step in pix_steps():
                f: list[str] = []
                if step["seed"] is not None:
                    data = PF.rnd(step["seed"], PLANES[1])
                    s.stage(PLANES[0], data)
                    mem.put(PLANES[0], data)
                if "ebud" in step:
                    s.wr(s.lab["mach_in"], b"\1")
                    s.call(s.lab["mach_etime"])
                    now = struct.unpack("<I", s.rd(s.lab["mach_et"], 4))[0]
                    ref = now - (s.lab["DISP_N"] * PF.FRAME_E
                                 if step["ebud"] == "due" else 0)
                    s.wr(s.lab["disp_laste"], struct.pack("<I", ref & 0xFFFFFFFF))
                    s.wr(s.lab["disp_bud"], b"\1")
                else:
                    s.wr(s.lab["mach_in"], b"\0")
                pix = (pix + step["dpix"]) & 0xFF
                s.write_ram(dst, Rf.pack_disp_st(st))     # gen/fields as before
                s.wr(dst + PIX_OFF, bytes([pix]))
                n0 = PF.state(s)["nconv"]
                C.call_checked(s, "shim_kick", f)
                f = [x for x in f if "m_rok" not in x]  # REC use varies by step
                n1 = PF.state(s)["nconv"]
                if (n1 - n0) != (1 if step["conv"] else 0):
                    f.append(f"conversions +{n1 - n0}, want "
                             f"+{1 if step['conv'] else 0}")
                if step["conv"]:
                    want = Rf.disp_convert(full, mem.read, lut)
                    f += C.diff("bitmap", s.read_ram(s.bitmap, 8000),
                                want["bitmap"], s.bitmap)
                    f += C.diff("matrix", s.read_ram(s.screen, 1000),
                                want["screen"], s.screen)
                log(f"  pix {step['name']:10s} epoch ${pix:02X} conversions "
                    f"+{n1 - n0} {'OK' if not f else 'FAIL'}")
                for x in f[:6]:
                    print(f"      {x}")
                fails += [f"pix {step['name']}: {x}" for x in f]
            s.wr(s.lab["mach_in"], b"\0")
        finally:
            mgr.release(tg)
    return fails


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(prg: str, legs: list[str], s, backend: str, timeout: float,
        out: str, mhz: int) -> tuple[list[str], dict]:
    """*s*: one mach_s or a list; each boot value gets its own session."""
    svals = list(s) if isinstance(s, (list, tuple)) else [s]
    rep: dict = {"prg": os.path.abspath(prg), "backend": backend, "s": svals,
                 "legs": legs}
    if backend != "vice":
        rep["mhz"] = mhz
    fails: list[str] = []
    bl = [x for x in legs if x in BOOT_LEGS]
    if bl:
        rom, why = machine.find_rom()
        if rom is None:
            print(f"SKIP aros_boot {','.join(bl)}: {why}")
            rep["skip"] = why
            bl = []
        else:
            rep["rom"] = {"cache": rom.cache, "ssp": f"${rom.ssp:08X}",
                          "pc": f"${rom.pc:08X}"}

    def body_for(sv: int, which: list[str]):
        def body(m: Machine):
            return boot_legs(m, rom, which, sv, timeout, rep)
        return body
    # one boot per session: mach_run never returns, so the ablation (a
    # second boot) gets a fresh machine of its own
    runs = [(sv, [x for x in bl if x != "ablate"]) for sv in svals]
    runs = [(sv, w) for sv, w in runs if w]
    if "ablate" in bl:
        runs.append((svals[0], ["ablate"]))
    for name, fn in (("wkhook", leg_wkhook), ("pix", leg_pix),
                     ("s0", lambda p: leg_s0(p, rep)),
                     ("poll", lambda p: leg_poll(p, rep)),
                     ("lat", lambda p: leg_lat(p, rep))):
        if name in legs:
            log(f"[{name}]")
            f = fn(prg)
            log(f"[{name}] {'PASS' if not f else 'FAIL'}")
            fails += f
    for sv, which in (runs if bl else []):
        t0 = time.monotonic()
        try:
            if backend == "vice":
                f = vice_session(prg, body_for(sv, which))
            else:
                f = u64_session(prg, os.path.join(out, f"u64-s{sv}-{which[0]}"),
                                mhz, body_for(sv, which))
        except Exception as e:                  # a dead session fails its s only
            f = [f"{INFRA}boot s={sv}: {type(e).__name__}: {e}"]
            log(f"  session failed: {f[0]}")
        fails += [f"[s={sv}] {x}" for x in f] if len(svals) > 1 else f
        rep.setdefault("session_wall_s", {})[sv] = round(time.monotonic() - t0, 1)
    return fails, rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--legs", default="m1,m2")
    ap.add_argument("--s", default=None,
                    help="mach_s for the boot (default: the profile's, "
                         "see CONTRACTS)")
    ap.add_argument("--backend", default="vice", choices=("vice", "u64"))
    ap.add_argument("--mhz", type=int, default=48)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default=os.path.join(EVID, "boot"))
    ap.add_argument("--json")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only")
    a = ap.parse_args(argv)
    global VERBOSE
    VERBOSE = a.verbose
    s = ([int(x) for x in a.s.split(",")] if a.s is not None else
         [BOOT_S_VICE if a.backend == "vice" else BOOT_S_U64])
    if a.mutants:
        return teeth(a.prg, a.only.split(",") if a.only else None, s, a.out)
    legs = [x for x in a.legs.split(",") if x]
    fails, rep = run(a.prg, legs, s, a.backend, a.timeout, a.out, a.mhz)
    rep["fails"] = fails
    if a.json:
        os.makedirs(os.path.dirname(os.path.abspath(a.json)), exist_ok=True)
        with open(a.json, "w") as f:
            json.dump(rep, f, indent=1)
    for x in fails:
        print(f"FAIL {x}")
    print(f"aros_boot: {'PASS' if not fails else f'FAIL ({len(fails)})'}"
          + (" (SKIP)" if rep.get("skip") else ""))
    return 1 if fails else 0


# --------------------------------------------------------------------------
# Teeth: source mutants (<= 10, docs/WAVE11_BRIEF.md operating rules)
# --------------------------------------------------------------------------

MACH, DISP, DRV = "runtime/ops/mach.s", "runtime/ops/disp.s", "tests/aros/mboot.s"
MUTANTS = {
    # C4.2: the frame hook is not a wake source again (the backlog bug)
    "wkfhook": (MACH, [("        lda mach_fhook+1        ; W11-A: the frame hook (the copper) can post\n"
                        "        beq no                  ; W11-A: a request every frame (C4.2), but\n",
                        "        jmp no                  ; MUTANT\n")]),
    # L6: the frame hook wakes a STOP even with copper DMA off
    "wkdma": (MACH, [("        lda disp_st+2           ; W11-A: only with copper DMA on (fix 1, L6):\n"
                      "        and #$02                ; W11-A: DMACON DMAEN (bit 9) and COPEN (bit 7),\n"
                      "        beq no                  ; W11-A: cop.s's state in disp_st+1 (LE; BASIC\n"
                      "        lda disp_st+1           ; W11-A: is out: mach_wk runs inside mach_run)\n"
                      "        bmi yes                 ; W11-A\n",
                      "        jmp yes                 ; MUTANT\n")]),
    # HIGH-1: the fixed 1024-phi2 poll again (the s <= 6 dispatcher livelock)
    "pollfix": (MACH, [
        ("        cmp mach_plo,x          ; W11-A (supervisor grant, HIGH-1): max(MACH_POLL,\n",
         "        cmp #<MACH_POLL         ; MUTANT\n"),
        ("        sbc mach_phi,x          ; W11-A (supervisor grant, HIGH-1): phi2, at most\n",
         "        sbc #>MACH_POLL         ; MUTANT\n"),
        ("        lda mach_plo,x          ; W11-A (supervisor grant, HIGH-1)\n",
         "        lda #<MACH_POLL         ; MUTANT\n"),
        ("        lda mach_phi,x          ; W11-A (supervisor grant, HIGH-1)\n",
         "        lda #>MACH_POLL         ; MUTANT\n")]),
    # rev-aros round 2: the s = 4 poll interval 8x too long (2048 E)
    "poll8": (MACH, [("mach_phi:  .byte >MACH_POLL, >MACH_POLL, >MACH_POLL, $08, $10, $20, $40, $80\n",
                      "mach_phi:  .byte >MACH_POLL, >MACH_POLL, >MACH_POLL, $08, $80, $20, $40, $80\n")]),
    # C3: disp_frame ignores an epoch change (gen and dirty only)
    "pixread": (DISP, [("        lda disp_st + DISP_ST_PIX ; W11-A: C3, guest pixels rewritten (a blit)\n"
                        "        cmp disp_pix            ; W11-A\n"
                        "        bne @chg                ; W11-A\n", "")]),
    # C3: a new epoch with equal fields is dropped by the snapshot compare
    "pixforce": (DISP, [("        php                     ; W11-A\n"
                         "        jsr disp_snap           ; (takes the epoch too)\n"
                         "        plp                     ; W11-A\n"
                         "        bne disp_take           ; W11-A\n",
                         "        jsr disp_snap\n")]),
    # C3: the epoch is never taken (every later frame converts again)
    "pixtake": (DISP, [("        lda disp_st + DISP_ST_PIX ; W11-A: C3, the epoch is taken\n"
                        "        sta disp_pix            ; W11-A\n", "")]),
    # C3: an epoch change bypasses the DISP_N budget inside mach_run
    "pixbudget": (DISP, [("        cmp disp_pix            ; W11-A\n"
                          "        bne @chg                ; W11-A\n",
                          "        cmp disp_pix            ; W11-A\n"
                          "        bne @go                 ; MUTANT\n")]),
    # boot entry: SSP and PC swapped (the reset longs read the wrong way)
    "vecswap": (DRV, [("        lda #<AROS_CHIP         ; the reset longs: SSP at 0, PC at 4 (BE)\n",
                       "        lda #<(AROS_CHIP + 4)   ; MUTANT\n"),
                      ("        lda #<(AROS_CHIP + 4)\n        sta m_addr\n",
                       "        lda #<AROS_CHIP         ; MUTANT\n        sta m_addr\n")]),
    # boot entry: exception mode off (st_exc = 0)
    "noexc": (DRV, [("        and #1\n        sta st_exc              ; latched by emu_reset\n",
                     "        and #0                  ; MUTANT\n"
                     "        sta st_exc              ; latched by emu_reset\n")]),
}
MUTANT_LEGS = {"pollfix": ["poll"], "poll8": ["lat"], "wkfhook": ["wkhook"], "wkdma": ["wkhook"], "pixread": ["pix"], "pixforce": ["pix"],
               "pixtake": ["pix"], "pixbudget": ["pix"], "vecswap": ["m1"],
               "noexc": ["m1"]}
MUTANT_TIMEOUT = 240.0          # m1 comes at ~50 s of VICE warp


def mutate(name: str, out_dir: str) -> str:
    rel, subs = MUTANTS[name]
    text = open(os.path.join(ROOT, rel)).read()
    for a, b in subs:
        if text.count(a) != 1:
            raise RuntimeError(f"mutant {name}: anchor found {text.count(a)}x")
        text = text.replace(a, b)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"mut_{name}_{os.path.basename(rel)}")
    with open(path, "w") as f:
        f.write(text)
    return path


def build_mutant_prg(name: str, out_dir: str, shim: bool) -> str:
    """The runner (+ tests/pf/shim.s for the pix leg) with one source swapped."""
    from tests.rec_build import make_sources
    from tests import run_pf as PF
    rel = MUTANTS[name][0]
    msrc = mutate(name, out_dir)
    gen = os.path.join(out_dir, "gen", "dispatch.s")
    os.makedirs(os.path.dirname(gen), exist_ok=True)
    subprocess.run([sys.executable, "host/gen_dispatch.py", "-o", gen],
                   cwd=ROOT, check=True)
    src = os.path.join(ROOT, rel)
    srcs = [msrc if x == src else x for x in make_sources()] + ([PF.SHIM] if shim else [])
    prg = os.path.join(out_dir, "runner.prg")
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg", "-o", prg,
           "-Ln", os.path.join(out_dir, "runner.lbl"),
           "-m", os.path.join(out_dir, "runner.map"),
           "--asm-include-dir", os.path.dirname(rel)]
    cmd += [os.path.relpath(x, ROOT) for x in srcs] + [gen]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"mutant build failed: {r.stdout}\n{r.stderr}")
    return prg


def teeth(prg: str, only: list[str] | None, s: int, out: str) -> int:
    bdir = os.path.join(os.path.dirname(os.path.abspath(prg)), "aros-mut")
    names = only or list(MUTANTS)
    bad, rows = [], {}
    for name in names:
        legs = MUTANT_LEGS[name]
        rel = MUTANTS[name][0]
        d = os.path.join(bdir, name)
        print(f"=== mutant {name} ({rel}; legs {','.join(legs)})", flush=True)
        verdict, fails = None, []
        try:                            # anchor drift / build failure: ERROR
            if rel == DRV:
                DRV_OVERRIDE[0] = mutate(name, d)
                mprg = prg
            else:
                mprg = build_mutant_prg(name, d, shim="pix" in legs)
        except Exception as e:
            verdict, fails = "ERROR", [f"build: {type(e).__name__}: {e}"]
        if verdict is None:
            try:
                fails, _ = run(mprg, legs, s, "vice", MUTANT_TIMEOUT, out, 48)
            except Exception as e:      # a leg that died: the rig, not a kill
                verdict, fails = "ERROR", [f"{INFRA}{type(e).__name__}: {e}"]
        DRV_OVERRIDE[0] = None
        if verdict is None:
            infra = [x for x in fails if x.startswith(INFRA)]
            verdict = "ERROR" if infra else "KILLED" if fails else "SURVIVED"
        rows[name] = {"verdict": verdict, "by": fails[:3]}
        print(f"=== mutant {name}: {verdict}" + (f" ({fails[0][:120]})" if fails else ""),
              flush=True)
        if verdict != "KILLED":
            bad.append(f"{name}:{verdict}")
    killed = sum(1 for r in rows.values() if r["verdict"] == "KILLED")
    print(f"teeth: {killed} killed, "
          f"{sum(1 for r in rows.values() if r['verdict'] == 'SURVIVED')} survived, "
          f"{sum(1 for r in rows.values() if r['verdict'] == 'ERROR')} error {bad}")
    return 1 if bad else 0


# mach_s for the boot profile, per host (measured: docs/CONTRACTS.md
# "Machine profile and AROS boot (S3.7)", PERF "AROS boot")
BOOT_S_VICE = 9                 # speed: m2 in ~120 s of warp (~9k instr/frame after m1)
BOOT_S_U64 = 3                  # speed to m3 (L8): 87-98 s at 48 MHz (s=4: 101 s, s=2: 106, s=5: 114)

if __name__ == "__main__":
    sys.exit(main())
