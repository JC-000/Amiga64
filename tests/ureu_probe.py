#!/usr/bin/env python3
"""W12-T: READ-ONLY probe of the UCI REU load on a real Ultimate.

Supervisor ruling (fix round 1): read-only access to device storage is
allowed through the harness DeviceLock -- a UCI DOS directory listing, OPEN
FA_READ, LOAD_REU, FILE_INFO, SEEK, READ_DATA, CLOSE.  Nothing is ever
written: no SAVE_REU, no create, no delete; never /Flash, never /Temp.

One u64_hold (config snapshot, exact restore): Command Interface Enabled
(the devices keep it Disabled; the UCI does not answer without it), CPU
speed, reboot, the runner
(prepare_target: REU enabled, 16 MB, inside the hold), then:
  1. list the storage root (/SD on the C64U, /USB1 on the U64E) and, if it
     holds no file, its first sub-directories, one entry per reply block
     (dos.cc:804-822: [attrib][name], last_part on the last one);
  2. FILE_INFO the regular files and pick the largest <= MAX_BYTES;
  3. time ureu_ld (OPEN FA_READ, LOAD_REU chunks, CLOSE) into REU $800000
     against an open/close-only ureu_ld, and keep the reply text (H1:
     small_printf's "$%6x" = 6 upper-case zero-filled digits);
  4. compare REU bytes with READ_DATA of the same file at sampled offsets
     (an independent path: dos.cc READ_DATA through the same target);
  5. ask for one byte more than the file holds: the firmware answers "00"
     with the short count, ureu_ld must refuse (UREU_E_SHORT).

    U64_HOST=10.53.21.158 python3 -m tests.ureu_probe --prg build/X/runner.prg
        --mhz 64 [--root /SD] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import random
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import machine  # noqa: E402

ORG, TX, OUT, BLK, STAT, TXN, R = 0xC000, 0xC180, 0xC200, 0xC600, 0xC680, 0xC6A0, 0xC6B0
STUB, SBUF = 0xC700, 0xCB00
REU_AT = 0x800000
MAX_BYTES = 4 << 20
SYMS = ("uci_begin", "uci_put", "dosio_uci_init")

DRV = """
.ifndef PF_INNER
PF_INNER = 217                  ; the turbo fence (dosio_uci.s, c64-https uci_regs.inc)
.endif
UCI_CTL = $DF1C
UCI_RSP = $DF1E
UCI_STA = $DF1F
TX   = $C180                    ; command bytes (<= 128)
OUT  = $C200                    ; reply data, $C200-$C5FF
BLK  = $C600                    ; block end offsets (u16, <= 64)
STAT = $C680                    ; status bytes (<= 32)
TXN  = $C6A0
R    = $C6B0                    ; +0 done, +1/2 bytes, +3 blocks, +4 status bytes, +5 err
OUTHI = $04                     ; OUT holds $0400 bytes
.segment "CODE"
p_tx:   lda #0
        ldx #5
:       sta R,x
        dex
        bpl :-
        lda TX
        jsr uci_begin
        bcc :+
        jmp err
:       ldx #1
s:      cpx TXN
        beq go
        lda TX,x
        jsr uci_put
        inx
        bne s
go:     lda #$01                ; PUSH
        sta UCI_CTL
        jsr fence
wait:   lda #0
        sta tmo
        sta tmo+1
wl:     lda UCI_CTL
        jsr fence
        and #$28                ; reply valid / error
        bne rdy
        inc tmo
        bne wl
        inc tmo+1
        bne wl
        jmp err
rdy:    and #$08
        beq dat
        jmp err
dat:    lda UCI_CTL
        jsr fence
        and #$80
        beq be
        lda UCI_RSP
        jsr fence
        ldy R+2
        cpy #OUTHI
        bcs dat
        jsr putout
        jmp dat
be:     ldx R+3                 ; block end: its end offset
        cpx #64
        bcs nb
        txa
        asl
        tax
        lda R+1
        sta BLK,x
        lda R+2
        sta BLK+1,x
        inc R+3
nb:     lda UCI_CTL
        jsr fence
        and #$30
        cmp #$30
        bne st
        lda #$02                ; more blocks: DATA_ACC
        sta UCI_CTL
        jsr fence
        jmp wait
st:     lda UCI_CTL
        jsr fence
        and #$40
        beq ack
        lda UCI_STA
        jsr fence
        ldx R+4
        cpx #32
        bcs st
        sta STAT,x
        inc R+4
        jmp st
ack:    lda #$02
        sta UCI_CTL
        jsr fence
        lda #$FF
        sta R
        rts
err:    lda #1
        sta R+5
        jsr dosio_uci_init
        lda #$FF
        sta R
        rts
putout: pha
        clc
        lda #<OUT
        adc R+1
        sta pa+1
        lda #>OUT
        adc R+2
        sta pa+2
        pla
pa:     sta $FFFF
        inc R+1
        bne :+
        inc R+2
:       rts
fence:  pha
        txa
        pha
        ldx #5
fo:     lda #PF_INNER
fi:     sec
        sbc #1
        bne fi
        dex
        bne fo
        pla
        tax
        pla
        rts
tmo:    .res 2
"""


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def build_driver(lab, out_dir: str, extra=()) -> tuple[bytes, int]:
    os.makedirs(out_dir, exist_ok=True)
    src, obj, out = (os.path.join(out_dir, n) for n in ("probe.s", "probe.o", "probe.bin"))
    cfg = os.path.join(out_dir, "probe.cfg")
    with open(src, "w") as f:
        f.write(DRV)
    with open(cfg, "w") as f:
        f.write("MEMORY { M: file = %%O, start = $%04X, size = $0180; }\n"
                "SEGMENTS { CODE: load = M, type = rw; }\n" % ORG)
    defs = [x for n in SYMS for x in ("-D", f"{n}=${lab(n):04X}")]
    for cmd in (["ca65", *defs, *extra, "-o", obj, src], ["ld65", "-C", cfg, "-o", out, obj]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stdout + r.stderr)
    return open(out, "rb").read(), ORG


class Probe:
    def __init__(self, tg, labels, drv: bytes):
        from tests.harness_util import lookup_label
        self.tg, self.t = tg, tg.transport
        self.lab = lambda n: lookup_label(labels, n)
        self.drv = drv
        self.w(ORG, drv)
        if self.r(ORG, len(drv)) != drv:
            raise RuntimeError("probe driver readback mismatch")

    def w(self, a, b):
        from c64_test_harness import write_bytes
        write_bytes(self.t, a, b)

    def r(self, a, n):
        out = bytearray()
        while len(out) < n:
            k = min(1024, n - len(out))
            out += self.t.read_memory(a + len(out), k)
        return bytes(out)

    def call(self, addr: int, timeout: float = 120.0) -> None:
        from tests.harness_util import call_runner
        call_runner(self.tg, addr, timeout)

    def tx(self, cmd: bytes) -> dict:
        """One UCI transaction on DOS target cmd[0]: reply blocks + status."""
        assert len(cmd) <= 128
        self.w(TX, cmd)
        self.w(TXN, bytes([len(cmd)]))
        self.w(R, b"\0")
        self.call(ORG)
        res = self.r(R, 6)
        if res[0] != 0xFF:
            raise RuntimeError("probe transaction did not complete")
        n, nb, ns = res[1] | res[2] << 8, res[3], res[4]
        data = self.r(OUT, n) if n else b""
        ends = struct.unpack(f"<{nb}H", self.r(BLK, 2 * nb)) if nb else ()
        blocks, o = [], 0
        for e in ends:
            blocks.append(data[o:e])
            o = e
        return {"err": res[5], "blocks": blocks, "data": data,
                "status": self.r(STAT, ns).decode("latin-1") if ns else ""}

    # --- DOS
    def cd(self, path: str) -> str:
        return self.tx(b"\x01\x11" + path.encode())["status"]

    def ls(self) -> list[tuple[int, str]]:
        st = self.tx(b"\x01\x13")["status"]
        if not st.startswith("00"):
            return []
        r = self.tx(b"\x01\x14")
        return [(b[0], b[1:].decode("latin-1")) for b in r["blocks"] if b]

    def size(self, name: str) -> int | None:
        if not self.tx(b"\x01\x02\x01" + name.encode())["status"].startswith("00"):
            return None
        r = self.tx(b"\x01\x07")
        self.tx(b"\x01\x03")
        return struct.unpack_from("<I", r["data"])[0] if len(r["data"]) >= 4 else None

    def read_at(self, name: str, pos: int, n: int) -> bytes:
        assert self.tx(b"\x01\x02\x01" + name.encode())["status"].startswith("00")
        assert self.tx(b"\x01\x06" + struct.pack("<I", pos))["status"].startswith("00")
        d = self.tx(b"\x01\x04" + struct.pack("<H", n))["data"]
        self.tx(b"\x01\x03")
        return d

    # --- the runner's ureu_ld and the REU
    def ureu_ld(self, name: str, ra: int, rl: int) -> tuple[int, float, str]:
        self.w(self.lab("dosio_name"), name.encode())
        self.w(self.lab("dosio_nlen"), bytes([len(name)]))
        self.w(self.lab("ureu_ra"), ra.to_bytes(3, "little"))
        self.w(self.lab("ureu_rl"), struct.pack("<I", rl))
        self.w(self.lab("ureu_err"), b"\xAA")
        t0 = time.monotonic()
        self.call(self.lab("ureu_ld"), timeout=900.0)
        dt = time.monotonic() - t0
        reply = self.r(self.lab("dosio_buf"), 40).split(b"\0")[0].decode("latin-1")
        return self.r(self.lab("ureu_err"), 1)[0], dt, reply

    def reu_read(self, a: int, n: int) -> bytes:
        rd = self.lab("mem_rec_dirty")
        code = bytes([
            0xA9, SBUF & 0xFF, 0x8D, 0x02, 0xDF, 0xA9, SBUF >> 8, 0x8D, 0x03, 0xDF,
            0xA9, a & 0xFF, 0x8D, 0x04, 0xDF, 0xA9, (a >> 8) & 0xFF, 0x8D, 0x05, 0xDF,
            0xA9, a >> 16, 0x8D, 0x06, 0xDF, 0xA9, n & 0xFF, 0x8D, 0x07, 0xDF,
            0xA9, n >> 8, 0x8D, 0x08, 0xDF, 0xA9, 0x00, 0x8D, 0x0A, 0xDF,
            0xA9, 0x91, 0x8D, 0x01, 0xDF, 0x20, rd & 0xFF, rd >> 8, 0x60])
        self.w(STUB, code)
        self.call(STUB)
        return self.r(SBUF, n)


FORBIDDEN = ("flash", "temp")    # never touched, not even listed or entered


def find_file(p: Probe, root: str, rep: dict) -> tuple[str, str, int] | None:
    """(dir, name, size): the largest regular file <= MAX_BYTES among up to
    80 FILE_INFO'd files in root and its first sub-directories.  Nothing
    outside *root* is listed; a directory that does not open is not listed
    (the firmware would list the old path); Flash/Temp never entered."""
    if any(x in root.lower().split("/") for x in FORBIDDEN):
        raise RuntimeError(f"refusing root {root}")
    cands: list[tuple[int, str, str]] = []
    todo, listing = [root], rep.setdefault("listing", {})
    while todo and len(listing) < 6:
        d = todo.pop(0)
        st = p.cd(d)
        if not st.startswith("00"):
            listing[d] = {"cd": st}
            log(f"  {d}: {st} (not listed)")
            continue
        ents = p.ls()
        listing[d] = {"cd": st, "n": len(ents), "entries": [(a, n) for a, n in ents[:40]]}
        log(f"  {d}: {st}; {len(ents)} entries")
        if d == root:
            todo += [f"{d}/{n}" for a, n in ents
                     if a & 0x10 and n.lower() not in FORBIDDEN][:5]
        for a, n in ents:
            if a & 0x10 or not n or len(n) > 60 or len(cands) >= 80:
                continue
            sz = p.size(n)
            if sz is not None:
                cands.append((sz, d, n))
    rep["sizes"] = {f"{d}/{n}": sz for sz, d, n in cands}
    ok = [c for c in cands if 1024 <= c[0] <= MAX_BYTES]
    if not ok:
        return None
    sz, d, n = max(ok)
    assert p.cd(d).startswith("00")
    return d, n, sz


def run(p: Probe, root: str, rep: dict) -> list[str]:
    fails = []
    got = find_file(p, root, rep)
    if got is None:
        return [f"no readable regular file (1 KB .. {MAX_BYTES} B) under {root}"]
    d, name, size = got
    rep["file"] = {"name": name, "size": size, "dir": d,
                   "fw_path": p.tx(b"\x01\x12")["data"].decode("latin-1")}
    log(f"  file {d}/{name}: {size} bytes")
    e0, t0, _ = p.ureu_ld(name, REU_AT, 0)                 # open + close only
    e1, t1, reply = p.ureu_ld(name, REU_AT, size)
    rep["load"] = {"err_empty": e0, "t_empty_s": round(t0, 3), "err": e1,
                   "t_s": round(t1, 3), "last_reply": reply,
                   "chunks": -(-size // 0x40000),
                   "throughput_kib_s": round(size / 1024 / max(t1 - t0, 1e-3), 1)}
    log(f"  LOAD_REU {size} B: err {e1}, {t1:.2f} s (open+close {t0:.2f} s) -> "
        f"{rep['load']['throughput_kib_s']} KiB/s; last reply {reply!r}")
    if e0 or e1:
        fails.append(f"ureu_ld refused: empty err {e0}, load err {e1}, reply {reply!r}")
    import re
    if not re.match(r"^\$[0-9A-F]{6} BYTES LOADED TO REU \$[0-9A-F]{6}$", reply):
        fails.append(f"reply text not small_printf's: {reply!r}")
    rnd = random.Random(size)
    offs = sorted({0, max(0, size - 256)} | {rnd.randrange(0, max(1, size - 256)) for _ in range(14)})
    bad = []
    for o in offs:
        n = min(256, size - o)
        want = p.read_at(name, o, n)
        have = p.reu_read(REU_AT + o, n)
        if want != have:
            bad.append(o)
    rep["samples"] = {"offsets": len(offs), "bytes": sum(min(256, size - o) for o in offs),
                      "mismatch": bad}
    log(f"  READ_DATA vs REU: {len(offs)} samples, {len(bad)} mismatch")
    if bad:
        fails.append(f"REU differs from READ_DATA at offsets {bad[:5]}")
    e2, _, reply2 = p.ureu_ld(name, REU_AT, size + 1)
    rep["short"] = {"err": e2, "reply": reply2}
    log(f"  one byte past the end: err {e2} (want 5), reply {reply2!r}")
    if e2 != 5:
        fails.append(f"a request past the end gave ureu_err {e2}, want 5 (E_SHORT)")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--prg", required=True)
    ap.add_argument("--mhz", type=int, default=64)
    ap.add_argument("--root", default=None, help="/SD (C64U) or /USB1 (U64E)")
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "evidence", "w12-reu", "probe"))
    a = ap.parse_args(argv)
    from c64_test_harness import Labels
    from c64_test_harness.backends.ultimate64_helpers import get_reu_config, reboot
    from tests.bench.u64_session import u64_hold
    from tests.harness_util import lookup_label
    from tests.run_vectors import prepare_target
    prg = os.path.abspath(a.prg)
    labels = Labels.from_file(os.path.splitext(prg)[0] + ".lbl")
    host = os.environ.get("U64_HOST", "?")
    tag = f"{host}-{a.mhz}mhz"
    out = os.path.join(a.out, tag)
    os.makedirs(out, exist_ok=True)
    drv, _ = build_driver(lambda n: lookup_label(labels, n), out)
    rep: dict = {"host": host, "mhz": a.mhz, "prg": prg}
    fails: list[str] = []
    with u64_hold(out) as hold:
        tg = hold["target"]
        tg.transport.set_speed(a.mhz)
        reboot(tg.transport.client)
        cl = tg.transport.client
        cat, item = "C64 and Cartridge Settings", "Command Interface"
        rep["ci_before"] = cl.get_config_value(cat, item)
        rep["ci_choices"] = cl.get_config_choices(cat, item)
        want = next((c for c in rep["ci_choices"] if c.lower().startswith("enabled")), "Enabled")
        cl.set_config_item(cat, item, want)     # inside the hold: u64_hold restores it
        rep["ci_set"] = cl.get_config_value(cat, item)
        log(f"Command Interface: {rep['ci_before']} -> {rep['ci_set']} (choices {rep['ci_choices']})")
        prepare_target(tg, prg)                 # REU on, 16 MB, inside the hold
        rep["reu_config"] = list(get_reu_config(cl))
        info = tg.transport.client.get_info() if hasattr(tg.transport.client, "get_info") else {}
        root = a.root or ("/SD" if "C64 Ultimate" in str(info) or host.endswith(".158") else "/USB1")
        rep["root"] = root
        log(f"probe {host} at {a.mhz} MHz, root {root}, REU {rep['reu_config']}")
        try:
            p = Probe(tg, labels, drv)
            p.call(lookup_label(labels, "dosio_uci_init"))
            fails = run(p, root, rep)
        except Exception as e:
            fails = [f"INFRA: {type(e).__name__}: {e}"]
        finally:
            tg.transport.set_speed(1)
            reboot(tg.transport.client)
    rep["fails"] = fails
    with open(os.path.join(out, "probe.json"), "w") as f:
        json.dump(rep, f, indent=1)
    for x in fails:
        print(f"FAIL {x}")
    print(f"ureu_probe: {'PASS' if not fails else f'FAIL ({len(fails)})'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
