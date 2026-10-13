#!/usr/bin/env python3
"""W16-X: the AmigaOS launcher of the MOD player (fixtures/modwb/), leg S.

docs/WAVE16_BRIEF.md (W16-X) and its addendum: the gate is leg S, the
launcher under our own exec stub (fixtures/modwb/stub/xstub.s), on the
model (Musashi + host/chipset_ref, as tests/run_mod.py) and on VICE (the
runner, tests/aros/mboot.s aros_boot in exception mode).  Per case:

  * the launcher's hunk executable is relocated as LoadSeg would (host/
    hunk.py), at one of two load maps (A, B): its chip BSS hunk (the UI's
    copper list and planes) in chip RAM, the tails of its code/data hunks
    filled with $A5 (only BSS hunks are cleared), the BSS hunks zero;
  * the stub builds the OS, sets the "OS" hardware state and calls the
    launcher in user mode (a Workbench start with its WBStartup, a project
    start, or a shell start with an argument line);
  * the W15 fixed blocks $000400-$0004FF and W15-U's $070000-$07FFFF hold a
    canary that must survive (the relocation, I-1); on the model nothing
    but the UI code may write $040000-$07FFFF at all;
  * after the return: the stub's interrupts work again (VERTBs and the "OS"
    CIA-A timer A counted for XP_WAIT frames in user mode), INTENAR,
    DMACONR, the autovectors, COP1LC (= GfxBase->copinit), the CIA ICR
    enable masks (hardware and resource) equal the state before; every
    claim is released (blitter, audio.device, ICR vectors, memory, files,
    libraries); Forbid/Disable are balanced; the WBStartup is replied
    once, last, under Forbid; the call order is the M0 sequence;
  * the app anchor ($2DC040, addendum) leads to the state struct, whose
    state, exit reason and error are the case's;
  * song-end cases (MOD_TRACE builds) replay exactly as host/mod_ref.py
    says (tests/run_mod.check_case: every Paula write per tick, every
    view, the timing), with the MOD where the stub's AllocMem put it.

    ~/.cache/amiga64/s3probe-venv/bin/python -m tests.run_modwb --leg model
    python3 -m tests.run_modwb --leg vice --prg build/X/runner.prg [--s 6]
    ... --mutants [--only a,b]   (each mutant must fail the leg)
    ... --red                    (the replay at the W15 fixed addresses: must fail)
"""

from __future__ import annotations

import argparse
import os
import signal
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import hunk, mod_ref, modgen  # noqa: E402
from tests import run_mod as RM  # noqa: E402

FIX = os.path.join(ROOT, "fixtures", "modwb")
STUB_SRC = os.path.join(FIX, "stub", "xstub.s")
VASM = os.path.expanduser("~/.local/opt/vbcc/bin/vasmm68k_mot")
EVID = os.path.join(ROOT, "build", "evidence", "w16-x")

# fixtures/modwb/stub/xstub.s
STUB_AT = 0x010000
STUB_VARS = int(os.environ.get("MODWB_VARS", "0"), 16)
SSP0 = 0x003000
XP, XS, XLOG = 0x000500, 0x000600, 0x005000
XP_LEN, XS_LEN = 0x100, 0x88
HEAP = 0x100000
COPLIST = 0x027800
OS_VIEW = 0x00EE0000
LOG_ENT = 24
XFILE = 0x2E0000
LIB = {1: "exec", 2: "dos", 3: "gfx", 4: "intuition", 5: "cia"}
LIB_BASES = {0x021000: "exec", 0x022000: "dos", 0x023000: "gfx", 0x024000: "intuition",
             0x025000: "ciaa", 0x025800: "ciab"}
LVO_NAME = {("exec", -120): "Disable", ("exec", -126): "Enable", ("exec", -132): "Forbid",
            ("exec", -138): "Permit", ("exec", -150): "SuperState",
            ("exec", -156): "UserState", ("exec", -198): "AllocMem",
            ("exec", -210): "FreeMem", ("exec", -294): "FindTask", ("exec", -372): "GetMsg",
            ("exec", -378): "ReplyMsg", ("exec", -384): "WaitPort",
            ("exec", -414): "CloseLibrary", ("exec", -444): "OpenDevice",
            ("exec", -450): "CloseDevice", ("exec", -498): "OpenResource",
            ("exec", -552): "OpenLibrary", ("exec", -666): "CreateMsgPort",
            ("exec", -672): "DeleteMsgPort", ("dos", -30): "Open", ("dos", -36): "Close",
            ("dos", -42): "Read", ("dos", -66): "Seek", ("dos", -126): "CurrentDir",
            ("gfx", -222): "LoadView", ("gfx", -228): "WaitBlit", ("gfx", -270): "WaitTOF",
            ("gfx", -456): "OwnBlitter", ("gfx", -462): "DisownBlitter",
            ("intuition", -96): "DisplayBeep", ("intuition", -390): "RethinkDisplay",
            ("intuition", -588): "EasyRequestArgs", ("cia", -6): "AddICRVector",
            ("cia", -12): "RemICRVector", ("cia", -18): "AbleICR", ("cia", -24): "SetICR"}

# fixtures/modwb/modwb.s
ANCHOR, PROF = 0x2DC040, 0x2DC000
ST = {"magic": 0, "ver": 4, "state": 6, "why": 7, "err": 8, "minit": 9, "claim": 10,
      "audio": 14, "prof": 15, "view": 16, "status": 20, "param": 24, "uicop": 28,
      "mod": 32, "modlen": 36, "sdma": 40, "sint": 42, "sadk": 44, "maska": 46,
      "maskb": 47, "oldview": 48, "copinit": 52, "frames": 56, "ticke": 60,
      "stagee": 62, "svec": 64}
ST_SIZE = 92
ST_DONE, ST_FAILED = 3, 4
WHY = {1: "lmb", 2: "end", 3: "loop", 4: "init"}

# the two load maps (code, data, bss, chip bss): A low, B high
MAPS = {"A": (0x030000, 0x038000, 0x03A000, 0x090000),
        "B": (0x0C0000, 0x0C8000, 0x0CA000, 0x0D0000)}
CANARIES = ((0x000400, 0x100), (0x070000, 0x10000))
CANARY_BYTE = 0xC3
PRE_INTENAR, PRE_DMACONR = 0x6028, 0x02C0
PRE_MASKA, PRE_MASKB = 0x01, 0x02     # the "OS" timers: ciaa TA, ciab TB (as AROS)

MUTANTS = {
    "novec": ["MW_MUT_NOVEC"],              # the autovectors not restored
    "noicr": ["MW_MUT_NOICR"],              # the CIA ICR enable masks not restored
    "nocop": ["MW_MUT_NOCOP"],              # COP1LC left on the UI's list
    "noint": ["MW_MUT_NOINT"],              # INTENA not restored
    "nogate": ["MW_MUT_NOGATE"],            # a held button exits at once
    "noreply": ["MW_MUT_NOREPLY"],          # the WBStartup never replied
    "tbcrb": ["MW_MUT_TBCRB"],              # the restore stops AROS's CIA-B timer B
    "anchorany": ["MW_MUT_ANCHORANY"],      # the anchor without a valid profile block
    "anchorfirst": ["MW_MUT_ANCHORFIRST"],  # 'MWBA' written before the pointer
    "flagcount": ["MOD_MUT_FLAGCOUNT"],     # the replay counts TA underflows by ICR flag
}
# killed by the model's write-order log only (VICE sees the end state and
# polls; a few instructions' window is not observable there)
MODEL_ONLY_MUTANTS = {"anchorfirst"}

log = RM.log


# --------------------------------------------------------------------------
# Build and load
# --------------------------------------------------------------------------

def build(defs: list[str], out: str, red: bool = False) -> bytes:
    os.makedirs(out, exist_ok=True)
    cmd = ["make", "-s", "-C", FIX, f"OUT={out}", f"DEFS={' '.join(defs)}"]
    if red:
        cmd.append("RED=1")
    subprocess.run(cmd + ["clean"], capture_output=True, text=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"build {defs} failed:\n{r.stdout}{r.stderr}")
    with open(os.path.join(out, "modwb"), "rb") as f:
        return f.read()


def launcher_code_len(out: str) -> int:
    """modwb.s's part of the CODE hunk (linked first), from vlink's map."""
    with open(os.path.join(out, "modwb.map")) as fh:
        for line in fh:
            p = line.split()
            if len(p) == 4 and p[3] == "modwb.s(CODE)" and p[0] == "00000000":
                return int(p[2], 16)
    raise RuntimeError("modwb.s(CODE) not in the map")


def build_stub(out: str) -> bytes:
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, "xstub.bin")
    r = subprocess.run([VASM, "-quiet", "-m68000", "-Fbin", "-o", path, STUB_SRC],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"stub: {r.stdout}{r.stderr}")
    with open(path, "rb") as f:
        return f.read()


def loadseg(exe: bytes, where: tuple[int, int, int, int]) -> tuple[list[tuple[int, bytes]], list[int]]:
    """The hunks placed and relocated as dos.library's LoadSeg would: the
    first code hunk is the entry; a CHIP hunk goes to chip RAM; code/data
    tails beyond the stored bytes hold $A5 (not promised zero), BSS is
    zero.  Returns (chunks, the hunk addresses)."""
    ex = hunk.parse(exe)
    nxt = {"code": where[0], "data": where[1], "bss": where[2], "chip": where[3]}
    addrs = []
    for s in ex.segments:
        region = "chip" if s.memory_flags == 1 else s.kind
        a = nxt[region]
        nxt[region] = (a + s.alloc_size + 7) & ~7
        addrs.append(a)
    for s, a in zip(ex.segments, addrs):
        if s.memory_flags == 1 and a + s.alloc_size > 0x200000:
            raise RuntimeError("a CHIP hunk outside chip RAM")
    chunks = []
    for s, a in zip(ex.segments, addrs):
        if s.kind == "bss":
            buf = bytearray(s.alloc_size)
        else:
            buf = bytearray(s.data) + b"\xA5" * (s.alloc_size - len(s.data))
        for tgt, offs in s.relocs.items():
            for o in offs:
                v = struct.unpack_from(">I", buf, o)[0]
                struct.pack_into(">I", buf, o, (v + addrs[tgt]) & 0xFFFFFFFF)
        chunks.append((a, bytes(buf)))
    return chunks, addrs


def profile_block(tick_e: int, flags: int = 0x07, s: int = 0, bad_check: bool = False) -> bytes:
    """W16-P's block (addendum): 'A64P', 1, 64, e_hz_q8, tick_e, frame_e,
    phi2_hz, s, host, flags, 0, r16, MHz, ..., ~sum at +60."""
    b = bytearray(64)
    struct.pack_into(">4sHHIIII", b, 0, b"A64P", 1, 64, tick_e * 50 * 256, tick_e,
                     14188, 1022727)
    b[24], b[25], b[26] = s, 1, flags
    s_ = sum(struct.unpack(">15I", bytes(b[:60]))) & 0xFFFFFFFF
    struct.pack_into(">I", b, 60, (~s_ ^ (1 if bad_check else 0)) & 0xFFFFFFFF)
    return bytes(b)


def profile_ok(b: bytes) -> bool:
    """The guest's rule (W16-P host/profile_block.parse): magic, version 1,
    size 64 and the check."""
    if b[:4] != b"A64P" or struct.unpack_from(">HH", b, 4) != (1, 64):
        return False
    return struct.unpack_from(">I", b, 60)[0] == ~sum(struct.unpack(">15I", b[:60])) & 0xFFFFFFFF


# a valid block without real time (VICE-like): the anchor, the Amiga's timing
PROF_NORT = profile_block(40, flags=0x01, s=9)


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

class Case:
    """mode: 0 Workbench (the tool's name + .mod), 1 shell, 2 a project
    icon (WBArg 1).  expect: (state, why, err)."""

    def __init__(self, name: str, song: str, mode: int = 0, ld: str = "A",
                 refuse: int = 0, cli: str = "", arg1: str = "", fname: str | None = None,
                 raw: bytes | None = None, prof: bytes | None = None, lmb: tuple | None = None,
                 expect: tuple = (ST_DONE, ("end", "loop"), 0), trace: bool = False,
                 tick_e: int = 0, model_only: bool = False, stall: tuple | None = None,
                 expect_lost: bool = False):
        self.name, self.song, self.mode, self.ld = name, song, mode, ld
        self.refuse, self.cli, self.arg1 = refuse, cli, arg1
        self.tool = "ModPlayer"
        self.fname = fname or (arg1 if mode == 2 else cli.split()[0].strip('"') if cli.split()
                               else "ModPlayer.mod")
        self.raw = raw if raw is not None else modgen.songs()[song]
        self.prof, self.lmb, self.expect, self.trace = prof, lmb, expect, trace
        self.tick_e, self.model_only = tick_e, model_only
        self.stall, self.expect_lost = stall, expect_lost    # (every N frames, E): model
        self.anchored = prof is not None and profile_ok(prof)   # the anchor rules
        self.wait = 5
        self.ref = None
        if trace:
            self.ref = RM.Case(name, self.raw, RM.PF_STOPEND | RM.PF_TRACE | RM.PF_REALTIME,
                               tick_e=tick_e, modat=HEAP)
        self.max_frames = 4 * (len(self.ref.ticks) if self.ref else 400) + 400

    def xparam(self, entry: int) -> bytes:
        p = bytearray(XP_LEN)
        struct.pack_into(">4sIBBHII", p, 0, b"XSTP", entry, self.mode, self.refuse,
                         self.wait, XFILE, len(self.raw))
        for off, s in ((20, self.fname), (52, self.tool), (84, self.arg1), (116, self.cli)):
            p[off:off + len(s)] = s.encode()
        return bytes(p)

    def chunks(self, exe: bytes, stub: bytes) -> list[tuple[int, bytes]]:
        hk, addrs = loadseg(exe, MAPS[self.ld])
        self.entry = addrs[0]
        segs = hunk.parse(exe).segments     # the state struct: first in the chip DATA hunk
        self.st_at = next(a for sg, a in zip(segs, addrs)
                          if sg.kind == "data" and sg.memory_flags == 1)
        out = [(0, struct.pack(">II", SSP0, STUB_AT)), (XP, self.xparam(addrs[0])),
               (XS, bytes(XS_LEN)), (STUB_AT, stub), (XFILE, self.raw)]
        out += [(a, bytes([CANARY_BYTE]) * n) for a, n in CANARIES]
        out += [(PROF, self.prof or bytes(64))]
        out += [(ANCHOR, bytes(16))]
        return out + hk


def cases() -> list[Case]:
    s = modgen.songs()
    bad_mk = bytearray(s["restart"])
    bad_mk[1080:1084] = b"FLT4"
    no_len = bytearray(s["restart"])
    no_len[950] = 0                          # mod_init: song length 0 (err 3)
    return [
        # the Workbench start, song end, the whole replay checked (load map A)
        Case("wb_end", "basic", trace=True),
        # a shell start with an argument, a looping song, load map B
        Case("cli_loop", "restart", mode=1, ld="B", cli='"Tune.mod"\n'),
        # a project icon; the profile block's real-time tick (s = 0 numbers)
        Case("proj_rt", "tempo", mode=2, arg1="Song.mod", trace=True, tick_e=20455,
             prof=profile_block(20455)),
        # 255 BPM at the stall-to-tick ratio of s = 3 on an Ultimate (a c2v
        # conversion ~2170 E against tick_e 2557): on VICE s = 6 the real
        # UI's conversion stalls ~11000 E, so tick_e 12960; on the model the
        # stall is injected (time runs on, the guest does not)
        Case("rt_s3", "tempo", ld="B", trace=True, tick_e=12960,
             prof=profile_block(12960, s=3)),
        Case("stall24", "tempo", trace=True, tick_e=2557, prof=profile_block(2557, s=3),
             stall=(24, 2170), model_only=True),
        Case("stall1", "tempo", ld="B", trace=True, tick_e=2557, prof=profile_block(2557, s=3),
             stall=(1, 2170), model_only=True),
        # a profile without real time (VICE): the Amiga's timing
        Case("prof_novrt", "restart", prof=profile_block(40, flags=0x01, s=9)),
        # a profile with a bad check: ignored
        Case("prof_bad", "restart", ld="B", prof=profile_block(2557, s=3, bad_check=True)),
        # LMB: pressed at launcher frame 20 (the button was up), released 6 later
        Case("lmb", "demo", prof=PROF_NORT, lmb=((0, False), (20, True), (26, False)),
             expect=(ST_DONE, ("lmb",), 0)),
        # LMB held from the start (the launching click), released, pressed again
        Case("lmb_held", "demo", ld="B", prof=PROF_NORT,
             lmb=((0, True), (12, False), (30, True), (36, False)),
             expect=(ST_DONE, ("lmb",), 0)),
        # no audio.device: plays anyway
        Case("noaudio", "restart", refuse=0x10, prof=PROF_NORT),
        # backing out before any takeover
        Case("claim_refused", "restart", refuse=0x04, prof=PROF_NORT, expect=(ST_FAILED, (None,), 6)),
        Case("not_mk", "restart", raw=bytes(bad_mk), prof=PROF_NORT, expect=(ST_FAILED, (None,), 5)),
        Case("no_file", "restart", fname="Other.mod", expect=(ST_FAILED, (None,), 2)),
        # mod_init refuses it after the takeover: restored, then reported
        Case("init_err", "restart", raw=bytes(no_len), prof=PROF_NORT, expect=(ST_FAILED, ("init",), 7)),
    ]


# --------------------------------------------------------------------------
# The checks
# --------------------------------------------------------------------------

def parse_log(b: bytes) -> list[tuple[str, int, int, int, int]]:
    """(name, d0, d1, a0, a1) per call."""
    out = []
    for k in range(0, len(b) - LOG_ENT + 1, LOG_ENT):
        lib, lvo, d0, d1, a0, a1, a6 = struct.unpack_from(">hhIIIII", b, k)
        lname = LIB.get(lib, "?")
        out.append((LVO_NAME.get((lname, lvo), f"{lname}{lvo}"), d0, d1, a0, a1))
    return out


def first(calls, name, pred=lambda c: True) -> int:
    return next((i for i, c in enumerate(calls) if c[0] == name and pred(c)), -1)


def check(c: Case, rd, hw: dict) -> list[str]:
    """rd(addr, n) reads guest memory after the HALT; hw: cop1lc, maska_hw,
    maskb_hw (the CIAs' real enable masks), canary_ok (model trap info)."""
    f: list[str] = []
    xs = rd(XS, XS_LEN)
    if xs[0:4] != b"XSTS":
        return [f"{c.name}: the stub never ran"]
    phase, fault, vec = xs[4], xs[5], struct.unpack_from(">H", xs, 6)[0]
    unimp = struct.unpack_from(">I", xs, 8)[0]
    if unimp:
        slot = unimp - 6
        lib = next(((n, slot - b) for b, n in LIB_BASES.items() if b - 6 * 150 <= slot < b),
                   ("?", slot))
        return [f"{c.name}: unimplemented call {lib[0]} LVO {lib[1]}"]
    if fault:
        return [f"{c.name}: exception, vector {vec} (stub phase {phase})"]
    if phase != 4:
        return [f"{c.name}: stub phase {phase} (want 4: returned and checked)"]
    calls = parse_log(rd(XLOG, struct.unpack_from(">I", xs, 44)[0] - XLOG))
    names = [x[0] for x in calls]
    c.free_at = first(calls, "FreeMem") if "FreeMem" in names else None
    if DUMP[0]:
        print(f"      anchor {rd(ANCHOR, 16).hex()} st {rd(c.st_at, 64).hex()}")
        print(f"      stub vars {rd(STUB_AT + STUB_VARS, 0x140).hex()}")
        for x in calls:
            print(f"      {x[0]:16s} d0 ${x[1]:08X} d1 ${x[2]:08X} a0 ${x[3]:08X} a1 ${x[4]:08X}")
    ret = struct.unpack_from(">I", xs, 12)[0]
    want_state, want_why, want_err = c.expect
    # the state struct (first in the chip DATA hunk) and the app anchor
    st = rd(c.st_at, ST_SIZE)
    if st[0:4] != b"MWBS":
        return [f"{c.name}: no state struct at ${c.st_at:06X}: {st[:8].hex()}"]
    f += check_anchor(c, rd(ANCHOR, 16), st, hw.get("anchor_log"))
    gs = lambda k: st[ST[k]]                        # noqa: E731
    gl = lambda k: struct.unpack_from(">I", st, ST[k])[0]   # noqa: E731
    why = WHY.get(gs("why"))
    if (gs("state"), gs("err")) != (want_state, want_err) or why not in want_why:
        f.append(f"{c.name}: state {gs('state')} why {why} err {gs('err')} "
                 f"(want {want_state} {want_why} {want_err})")
    if ret != (20 if want_err else 0):
        f.append(f"{c.name}: the launcher returned {ret}")
    taken = want_err not in (2, 5, 6)
    # the hardware and the OS state after the return
    i2, d2 = struct.unpack_from(">HH", xs, 24)
    i1, d1 = struct.unpack_from(">HH", xs, 20)
    if (i1, d1 & 0x07FF) != (PRE_INTENAR, PRE_DMACONR):     # (BZERO/BBUSY: the blitter's)
        f.append(f"{c.name}: the stub's pre-state INTENAR ${i1:04X} DMACONR ${d1:04X}")
    if i2 != i1:
        f.append(f"{c.name}: INTENAR ${i2:04X} after, ${i1:04X} before")
    if d2 & 0x07FF != d1 & 0x07FF:
        f.append(f"{c.name}: DMACONR ${d2:04X} after, ${d1:04X} before")
    vec_now, vec_pre = rd(0x64, 28), xs[96:124]
    if vec_now != vec_pre:
        f.append(f"{c.name}: autovectors {vec_now.hex()} after, {vec_pre.hex()} before")
    if hw["cop1lc"] != COPLIST:
        f.append(f"{c.name}: COP1LC ${hw['cop1lc']:06X}, want copinit ${COPLIST:06X}")
    maska, maskb = xs[28], xs[29]
    if (maska, maskb) != (PRE_MASKA, PRE_MASKB):
        f.append(f"{c.name}: ICR resource masks ${maska:02X}/${maskb:02X}")
    if (hw["maska_hw"], hw["maskb_hw"]) != (PRE_MASKA, PRE_MASKB):
        f.append(f"{c.name}: CIA ICR enable masks ${hw['maska_hw']:02X}/"
                 f"${hw['maskb_hw']:02X}, want ${PRE_MASKA:02X}/${PRE_MASKB:02X}")
    vbl, ta, wto = struct.unpack_from(">H", xs, 30)[0], struct.unpack_from(">H", xs, 32)[0], xs[34]
    if wto or vbl != c.wait:
        f.append(f"{c.name}: after the return {vbl} VERTBs of {c.wait} (a wait timed out: {wto})")
    if not ta:
        f.append(f"{c.name}: no CIA-A timer A interrupt after the return (the OS's timer)")
    if not struct.unpack_from(">H", xs, 124)[0]:
        f.append(f"{c.name}: no CIA-B timer B interrupt after the return (the OS's timer)")
    dtod = (xs[129] - xs[128]) & 0xFF        # TODLO alone: a latch left held reads stale
    if not wto and dtod not in (vbl, vbl + 1):
        f.append(f"{c.name}: CIA-A TOD moved {dtod} over {vbl} VERTBs after the return "
                 "(a TOD latch left held)")
    if xs[126] != xs[127]:
        f.append(f"{c.name}: CIA-B CRB ${xs[127]:02X} after, ${xs[126]:02X} before (AROS's timer)")
    if struct.unpack_from(">H", xs, 16)[0] & 0x2000:
        f.append(f"{c.name}: returned in supervisor mode")
    if struct.unpack_from(">I", xs, 52)[0] != 0x005000:
        f.append(f"{c.name}: USP ${struct.unpack_from('>I', xs, 52)[0]:06X} at the return")
    wb = c.mode in (0, 2)                   # a Workbench start ends in Forbid (the
    bal = {"Disable nest": xs[18],          # task's end ends it), so its ReplyMsg
           "Forbid nest": xs[19] - (1 if wb else 0), "blitter owned": xs[35],   # is last
           "libraries open": xs[36], "allocations": xs[37], "bad FreeMem": xs[38],
           "audio.device open": xs[41], "files open": xs[42], "ICR vectors kept": xs[43]}
    for k, v in bal.items():
        if v:
            f.append(f"{c.name}: {k} {v} after the return")
    if c.mode in (0, 2):
        if xs[39] != 1 or xs[40] < 1:
            f.append(f"{c.name}: ReplyMsg {xs[39]}x (Forbid nest {xs[40]})")
        elif names[-1] != "ReplyMsg":
            f.append(f"{c.name}: the last call is {names[-1]}, not ReplyMsg")
    elif xs[39]:
        f.append(f"{c.name}: a shell start replied a message")
    cur = struct.unpack_from(">I", xs, 56)[0]
    if cur != 0x100:
        f.append(f"{c.name}: the current dir ${cur:X} after (want the start's $100)")
    opened = xs[64:96].split(b"\0")[0].decode("latin-1")
    if opened != c.fname and want_err != 2:
        f.append(f"{c.name}: opened {opened!r}, want {c.fname!r}")
    # the call order
    if taken:
        seq = [first(calls, "OwnBlitter"), first(calls, "LoadView", lambda x: x[4] == 0),
               first(calls, "Forbid"), first(calls, "Disable"), first(calls, "SuperState"),
               first(calls, "UserState"), first(calls, "Enable"), first(calls, "Permit"),
               first(calls, "LoadView", lambda x: x[4] == OS_VIEW),
               first(calls, "RethinkDisplay"), first(calls, "DisownBlitter")]
        if -1 in seq or seq != sorted(seq):
            f.append(f"{c.name}: call order {seq} (OwnBlitter, LoadView(0), Forbid, Disable, "
                     "SuperState, UserState, Enable, Permit, LoadView(old), "
                     "RethinkDisplay, DisownBlitter)")
        adds = [i for i, x in enumerate(calls) if x[0] == "AddICRVector"]
        rems = [i for i, x in enumerate(calls) if x[0] == "RemICRVector"]
        if not adds or max(adds) > seq[2] or [r for r in rems if seq[2] < r < seq[7]]:
            f.append(f"{c.name}: the ICR claims are not outside the takeover")
        claim = st[ST["claim"]:ST["claim"] + 4]
        if claim != b"\x01\x00\x00\x01":
            f.append(f"{c.name}: claims {claim.hex()} (ciaa TA, ciab TB are the OS's)")
    else:
        for n in ("Forbid", "Disable", "SuperState", "LoadView", "OwnBlitter"):
            if n in names[:-2]:
                f.append(f"{c.name}: backed out, but called {n}")
        if not struct.unpack_from(">I", xs, 60)[0]:
            f.append(f"{c.name}: backed out without a requester")
    if want_err == 6 and st[ST["claim"]:ST["claim"] + 4] != b"\x01\x00\x01\x01":
        f.append(f"{c.name}: claims {st[ST['claim']:ST['claim'] + 4].hex()} (ciab TA refused)")
    # the profile block
    te, se = struct.unpack_from(">HH", st, ST["ticke"])
    if c.tick_e:
        want_se = (c.tick_e * 300 + 20455 // 2) // 20455 or 1
        if (te, se, gs("prof")) != (c.tick_e, want_se, 1):
            f.append(f"{c.name}: P_TICKE {te} P_STAGEE {se} prof {gs('prof')}, want "
                     f"{c.tick_e} {want_se} 1")
    elif (te, se, gs("prof")) != (0, 0, 0):
        f.append(f"{c.name}: P_TICKE {te} P_STAGEE {se} prof {gs('prof')} (want the "
                 "Amiga's timing)")
    if c.refuse & 0x10 and gs("audio") != 1:
        f.append(f"{c.name}: audio {gs('audio')} with audio.device refused")
    elif not c.refuse & 0x10 and taken and gs("audio") != 0:
        f.append(f"{c.name}: audio {gs('audio')}")
    # the canaries (the W15 fixed blocks, W15-U's fixed UI area)
    for a, n in CANARIES:
        got = rd(a, n)
        if got != bytes([CANARY_BYTE]) * n:
            k = next(i for i in range(n) if got[i] != CANARY_BYTE)
            f.append(f"{c.name}: ${a + k:06X} changed (a W15 fixed address)")
    f += [f"{c.name}: {x}" for x in hw.get("extra", [])]
    # the replay itself
    if c.ref and not f:
        status = rd(gl("status"), 64)
        twp, ttp = struct.unpack_from(">II", status, 32)
        stream = rd(RM.TR_W, twp - RM.TR_W) if twp > RM.TR_W else b""
        recs = rd(RM.TR_T, ttp - RM.TR_T) if ttp > RM.TR_T else b""
        if gl("mod") != HEAP:
            f.append(f"{c.name}: the MOD at ${gl('mod'):06X}")
        f += RM.check_case(c.ref, status, stream, recs, rd(gl("view"), RM.VIEW_LEN), None,
                           timing=hw.get("timing", True))
        nrec = (ttp - RM.TR_T) // RM.TR_REC if ttp > RM.TR_T else 0
        lost, where, worst, u_min = lost_ticks(c.ref, recs, min(nrec, len(c.ref.ticks)))
        c.lost = (lost, worst, u_min)
        if lost:
            f.append(f"{c.name}: {lost} CIA-B TA underflow(s) lost (software count; "
                     f"tick, rise E, period E: {where[:6]}; worst stall {worst} E)")
        if DUMP[0]:
            want = c.ref.ticks
            a = 0 if c.ref.flags & RM.PF_REALTIME else 0
            pred = RM.predict_cia(c.ref, a)
            late = [RM.rec_time(recs, i) - pred[i] for i in range(len(pred))]
            jumps = [(i, late[i] - late[i - 1], want[i].bpm) for i in range(1, len(late))
                     if abs(late[i] - late[i - 1]) > 3000]
            print(f"      lateness jumps (tick, delta E, bpm): {jumps[:12]}")
            print(f"      S_L6 {struct.unpack_from('>H', status, 42)[0]}, ticks {len(want)}")
    if c.lmb and not f:
        fr = gl("frames")
        press = next(t for t, down in c.lmb if down and t > 0)
        if not press <= fr <= press + hw.get("lmb_slack", 3):     # (VICE: the host's poll)
            f.append(f"{c.name}: left at launcher frame {fr}, pressed at {press}")
    return f


def check_anchor(c: Case, an: bytes, st: bytes, wlog: list | None) -> list[str]:
    """The anchor rules (CONTRACTS, W16-P's profile block; supervisor
    2026-10-12).  After the return: with a valid profile block the anchor
    was published and withdrawn ('MWBA' 0), its pointer is the struct (non
    zero, even, the whole struct in chip $000000-$1FFFFF or slow
    $C00000-$DBFFFF), +8 holds seq << 16 | state << 8 with the last state
    published (2 restored, or 4 failed) and +12 is 0; without one the 16
    bytes were never written (staged 0).  wlog (the model leg): every change
    of the 16 bytes seen between two launcher instructions, in order: +4,
    +8, +12 before the magic; the magic last; while published the pointer
    never moves; the magic cleared before the stub's first FreeMem."""
    f: list[str] = []
    n = c.name
    if not c.anchored:
        if any(an):
            f.append(f"{n}: the anchor written without a valid profile block: {an.hex()}")
        if wlog:
            f.append(f"{n}: {len(wlog)} anchor writes without a valid profile block")
        return f
    ptr, seq, res = struct.unpack_from(">III", an, 4)
    if an[:4] != b"\0\0\0\0":
        f.append(f"{n}: the anchor not withdrawn after the return: {an.hex()}")
    if ptr != c.st_at:
        f.append(f"{n}: the anchor points to ${ptr:06X}, the struct is at ${c.st_at:06X}")
    if not (ptr and not ptr & 1 and any(lo <= ptr and ptr + ST_SIZE <= hi
                                         for lo, hi in ((0, 0x200000), (0xC00000, 0xDC0000)))):
        f.append(f"{n}: the anchor's pointer ${ptr:08X} breaks rule 3")
    last = 4 if c.expect[0] == ST_FAILED else 2
    if (seq & 0xFF, (seq >> 8) & 0xFF, res) != (0, last, 0) or not seq >> 16:
        f.append(f"{n}: anchor +8 ${seq:08X} +12 ${res:08X} (want state {last}, a seq)")
    if wlog is None:
        return f
    # the write order (model): [(index of the stub call log at the time, 16 bytes)]
    prev = bytes(16)
    pub = wd = None
    for k, (ncalls, b) in enumerate(wlog):
        mag, was = b[:4], prev[:4]
        if mag != was:
            if mag == b"MWBA" and pub is None:
                pub = k
                if b[4:8] != struct.pack(">I", c.st_at) or b[8:16] != bytes(8):
                    f.append(f"{n}: 'MWBA' published before +4/+8/+12: {b.hex()}")
            elif mag == bytes(4) and was == b"MWBA" and wd is None:
                wd = (k, ncalls)
            else:
                f.append(f"{n}: anchor magic {was.hex()} -> {mag.hex()} (write {k})")
        elif mag == b"MWBA" and b[4:8] != prev[4:8]:
            f.append(f"{n}: the anchor's pointer changed while published")
        prev = b
    if pub is None or wd is None:
        f.append(f"{n}: anchor published at write {pub}, withdrawn at {wd}")
    elif c.free_at is not None and wd[1] > c.free_at:
        f.append(f"{n}: the anchor withdrawn after the first FreeMem (call {c.free_at})")
    return f


def lost_ticks(ref, recs: bytes, n: int) -> tuple[int, list, int, int]:
    """The missed-tick detector (supervisor, the R1 condition).  The replay
    counts CIA-B TA underflows in software (MOD_HUNK: one ICR flag), so a
    guest stall longer than an underflow period (a c2v conversion: ~17 ms
    real ~ 2170 E at s = 3) can merge two underflows into one count and lose
    a tick for good.  The reference is free-running: each tick record's CIA-A
    TOD (+1 per frame) and beam (tests/run_mod.rec_time), against the
    underflow-exact grid (run_mod.predict_cia).  Lateness L = measured -
    predicted; a late tick is caught up (L falls back), a lost underflow
    shifts every later tick by its period for good, so the floor of L (the
    suffix minimum) rises by one underflow period per loss.  Returns (lost
    underflows, [(tick, rise E, period E)], the largest L - floor (the worst
    stall seen), the smallest underflow period)."""
    pred = RM.predict_cia(ref, 0)[:n]
    late = [RM.rec_time(recs, i) - pred[i] for i in range(len(pred))]
    if len(late) < 2:
        return 0, [], 0, 0
    per = [0] + [(pred[i] - pred[i - 1]) // mod_ref.cia_split(pred[i] - pred[i - 1])[0]
                 for i in range(1, len(pred))]
    per[0] = per[1]
    u_min = min(per)
    floor = late[:]
    for i in range(len(late) - 2, -1, -1):
        floor[i] = min(late[i], floor[i + 1])
    lost, where = 0, []
    for i in range(1, len(floor)):
        d = floor[i] - floor[i - 1]
        if d > per[i] // 2:
            k = (d + per[i] // 2) // per[i]
            lost += k
            where.append((i, d, per[i]))
    worst = max(x - f for x, f in zip(late, floor))
    return lost, where, worst, u_min


def lmb_down(c: Case, rd) -> bool:
    """The left button by the case's schedule of launcher frames (A_FRAMES);
    up once the launcher has restored (state >= 2).  The struct is read at
    its load address (the case's map), not through the anchor."""
    st = rd(c.st_at, ST_SIZE)
    if st[:4] != b"MWBS":
        return c.lmb[0][1]
    if st[ST["state"]] >= 2:
        return False
    fr = struct.unpack_from(">I", st, ST["frames"])[0]
    down = False
    for t, d in c.lmb:
        if fr >= t:
            down = d
    return down


# --------------------------------------------------------------------------
# Leg "model"
# --------------------------------------------------------------------------

class Model(RM.Model):
    """run_mod's model (Musashi, chipset_ref's CIAs and Paula, E = CPU/10,
    VERTB per 14188 E) with the stub's chunks; the left button is CIA-A
    PRA pin 6, driven from the launcher's frame count."""

    def __init__(self, c: Case, chunks, mw_code_len: int):
        class Src:
            e_per_cyc = (1, 10)

            def chunks(self, _img):
                return chunks
        super().__init__(b"", {}, Src())
        self.case = c
        self.mw_lo, self.mw_hi = c.entry, c.entry + mw_code_len
        self.anchor_last, self.anchor_log = bytes(16), []
        for a, d in chunks:                 # run_mod's trapped UI area holds its own copy
            for k in range(len(d)):
                if 0x040000 <= a + k < 0x080000:
                    self.uiarea[a + k - 0x040000] = d[k]

    def on_instr(self, pc):
        if self.mw_lo <= pc < self.mw_hi:  # the launcher's own code: the anchor's
            b = bytes(self.mem.r_block(ANCHOR, 16))     # changes, in write order
            if b != self.anchor_last:
                self.anchor_last = b
                ncalls = (self.mem.r32(XS + 44) - XLOG) // LOG_ENT
                self.anchor_log.append((ncalls, b))
        if not self.halted and self.mem.r16(pc & 0xFFFFFF) == 0xAFFF:
            self.xs_snap = self.read(XS, XS_LEN)    # Musashi then takes the A-line
        super().on_instr(pc)                        # trap: the stub's fault stub

    def read_halted(self, a: int, n: int) -> bytes:
        b = bytearray(self.read(a, n))
        snap = getattr(self, "xs_snap", None)
        if snap:
            for k in range(n):
                if XS <= a + k < XS + XS_LEN:
                    b[k] = snap[a + k - XS]
        return bytes(b)

    def frame_hook(self):
        if not self.case.lmb:
            return
        down = lmb_down(self.case, self.read)
        pins = self.cs.ciaa.pra_in
        self.cs.ciaa.pra_in = (pins & ~0x40) | (0 if down else 0x40)

    def advance(self, n: int) -> None:
        """n E ticks of the CIAs and the frame clock (VERTB, CIA-A TOD)."""
        while n:
            to_fr = RM.FRAME_E - self.e % RM.FRAME_E
            k = min(n, to_fr)
            self.cs.ciaa.eclock(k)
            self.cs.ciab.eclock(k)
            self.e += k
            n -= k
            if self.e % RM.FRAME_E == 0:
                self.frames += 1
                self.cs.ciaa.tod_tick(1)
                self.cs.request(5)

    def run(self, max_e: int) -> None:
        fr = self.frames
        rem = 0
        every, stall_e = self.case.stall or (0, 0)
        while not self.halted and self.e < max_e:
            res = self.mach.execute(self.SLICE)
            ran = res.cycles if res.cycles > 0 else self.SLICE
            n, rem = divmod(rem + ran * self.epc[0], self.epc[1])
            self.advance(n)
            if self.frames != fr:
                fr = self.frames
                self.frame_hook()
                if every and fr % every == 0:
                    self.advance(stall_e)       # a c2v conversion: the guest stalled,
                    fr = self.frames            # time (the CIAs, the frames) runs on


def leg_model(defs: list[str], red: bool = False, only: list[str] | None = None) -> list[str]:
    out = os.path.join(ROOT, "build", "modwb-model" + ("-" + "-".join(defs) if defs else "")
                       + ("-red" if red else ""))
    exe_tr = build(["MOD_TRACE", *defs], out + "-tr", red)
    stub = build_stub(out)
    fails: list[str] = []
    for c in cases():
        if only and c.name not in only:
            continue
        t0 = time.monotonic()
        ch = c.chunks(exe_tr, stub)
        m = Model(c, ch, launcher_code_len(out + "-tr"))
        m.run(max_e=c.max_frames * RM.FRAME_E)
        rd = m.read_halted
        regs = m.cs.regs
        hw = {"cop1lc": ((regs[0x80 >> 1] << 16) | regs[0x82 >> 1]) & 0xFFFFFE,
              "maska_hw": m.cs.ciaa.icr_mask, "maskb_hw": m.cs.ciab.icr_mask,
              "extra": [f"${a:06X} (W15-U's fixed area) written at PC ${pc:06X}"
                        for a, pc in m.uiarea_bad],
              "anchor_log": m.anchor_log}
        f = [] if m.halted else [f"{c.name}: no HALT within {c.max_frames} frames"]
        if m.halted:
            f += check(c, rd, hw)
        log(f"model {c.name}: {m.frames} frames, {m.icount} instructions, lost/worst stall/"
            f"period {getattr(c, 'lost', '-')}, "
            f"{time.monotonic() - t0:.1f}s: {'ok' if not f else 'FAIL'}")
        for x in f[:8]:
            print("    " + x)
        fails += f
        if f and STOP_FIRST[0]:
            break
    return fails


# --------------------------------------------------------------------------
# Leg "vice"
# --------------------------------------------------------------------------

def leg_vice(prg: str, defs: list[str], s: int, red: bool = False,
             only: list[str] | None = None) -> list[str]:
    from tests import aros_boot as AB
    from tests.run_dos import Guest
    from tests.run_program import Chunk, Session
    from types import SimpleNamespace
    from tests import aros_wb as WB

    def on_term(*_):
        raise KeyboardInterrupt("SIGTERM")
    signal.signal(signal.SIGTERM, on_term)
    out = os.path.join(ROOT, "build", "modwb-vice" + ("-" + "-".join(defs) if defs else "")
                       + ("-red" if red else ""))
    exe_tr = build(["MOD_TRACE", *defs], out + "-tr", red)
    stub = build_stub(out)
    fails: list[str] = []

    def run_case(m, c: Case) -> list[str]:
        sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
        g = Guest(SimpleNamespace(target=m.tg, timeout=600.0), m.labels)
        cia = m.lab("cia_st")
        t0 = time.monotonic()
        m.load_driver()
        m.w(AB.P, bytes([0, RM.CHIP_FILL_BANKS]))
        m.call("aros_fill")
        ch = c.chunks(exe_tr, stub)
        sess.stage([Chunk(a, d) for a, d in ch])
        m.load_driver()
        m.start(s)
        deadline = time.monotonic() + 90 + 0.15 * c.max_frames * 2 ** (s - 6)   # (measured ~0.06 s/frame at s = 6)
        # the left button through the input seam (runtime/ops/inp.s: inp_src
        # != 0 takes inp_joy; fire -> CIA-A PRA pin 6 low, once per frame)
        src0, joy0 = m.r(m.lab("inp_src"), 1), m.r(m.lab("inp_joy"), 1)
        if c.lmb:
            m.w(m.lab("inp_joy"), b"\x10" if c.lmb[0][1] else b"\x00")
            m.w(m.lab("inp_src"), b"\x01")
        peek = WB.GuestPeek(m) if c.lmb else None
        down_now = bool(c.lmb and c.lmb[0][1])
        seen_pub = False
        f: list[str] = []
        while True:
            r = m.r(AB.R, 0x10)
            if r[0] == 0xFF:
                break
            if peek:
                peek.fresh()
                an = peek.read(ANCHOR, 16)          # published while it plays
                if an[:4] == b"MWBA" and struct.unpack_from(">I", an, 4)[0] == c.st_at:
                    seen_pub = True
                down = lmb_down(c, peek.read)
                if down != down_now:
                    m.w(m.lab("inp_joy"), b"\x10" if down else b"\x00")
                    down_now = down
            if time.monotonic() > deadline:
                return [f"{c.name}: no halt by the deadline"]
            time.sleep(0.3 if c.lmb else 1.0)
        m.w(m.lab("inp_src"), src0)
        m.w(m.lab("inp_joy"), joy0)
        halt, reason = r[1], r[2]
        if halt != 1 or reason != 0:
            f.append(f"{c.name}: mach_run st_halt {halt} reason {reason}")
        cop = int.from_bytes(m.r(m.lab("chip_shd") + 0x80, 4), "big") & 0xFFFFFE
        hw = {"cop1lc": cop, "maska_hw": m.r(cia + 19, 1)[0],
              "maskb_hw": m.r(cia + 45 + 19, 1)[0], "lmb_slack": 40,
              # VICE s = 6 with W15-U's UI: every trail refresh's conversion
              # stalls the guest ~11000 E (caught up: run_mod's late bound is
              # 3 frames); the underflows are counted from time (MOD_HUNK)
              "timing": True}
        f += check(c, g.read, hw)
        if peek and c.anchored and not seen_pub:
            f.append(f"{c.name}: the anchor never seen published while playing")
        log(f"vice {c.name} (s = {s}): {time.monotonic() - t0:.0f}s, lost/worst stall/"
            f"period {getattr(c, 'lost', '-')}: "
            f"{'ok' if not f else 'FAIL'}")
        for x in f[:8]:
            print("    " + x)
        return f

    # one emulator session per case: a second run in the same session
    # misbehaves (finding F1, build/evidence/w16-x/PROGRESS.md)
    allc = {x.name: x for x in cases()}
    todo = [c for c in ([allc[n] for n in only] if only else list(allc.values()))
            if not c.model_only]
    if ONE_SESSION[0]:                  # F1's reproduction: every case in one session
        AB.vice_session(prg, lambda m: [fails.extend(run_case(m, c)) for c in todo])
        return fails
    for c in todo:
        f = AB.vice_session(prg, lambda m, c=c: run_case(m, c))
        fails.extend(f)
        if f and STOP_FIRST[0]:
            break
    return fails



STOP_FIRST = [False]
ONE_SESSION = [False]
DUMP = [os.environ.get("MODWB_DUMP") == "1"]


def main(argv=None) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--leg", choices=("model", "vice"), default="model")
    ap.add_argument("--prg")
    ap.add_argument("--s", type=int, default=6)
    ap.add_argument("--mutants", action="store_true")
    ap.add_argument("--only", default="", help="mutants (with --mutants) or cases")
    ap.add_argument("--red", action="store_true")
    ap.add_argument("--one-session", action="store_true",
                    help="VICE: every case in one emulator session (reproduces finding F1)")
    a = ap.parse_args(argv)
    ONE_SESSION[0] = a.one_session
    only = [x for x in a.only.split(",") if x]

    def leg(defs, red=False, cs=None):
        if a.leg == "model":
            return leg_model(defs, red, cs)
        if not a.prg:
            raise SystemExit("--prg is required for --leg vice")
        return leg_vice(a.prg, defs, a.s, red, cs)

    if a.mutants:
        STOP_FIRST[0] = True
        bad = []
        for name, defs in MUTANTS.items():
            if only and name not in only:
                continue
            if a.leg == "vice" and name in MODEL_ONLY_MUTANTS:
                log(f"mutant {name}: model leg only (skipped)")
                continue
            f = leg(defs)
            log(f"mutant {name}: {'KILLED' if f else 'SURVIVED'}")
            if not f:
                bad.append(name)
        print(f"run_modwb {a.leg} mutants: "
              f"{'PASS (all killed)' if not bad else 'FAIL: survived ' + ','.join(bad)}")
        return 1 if bad else 0
    if a.red:
        STOP_FIRST[0] = True
        f = leg([], red=True, cs=only or None)
        print(f"run_modwb {a.leg} red: {'FAIL as expected' if f else 'PASSED (no teeth!)'}")
        return 0 if f else 1
    f = leg([], cs=only or None)
    print(f"run_modwb {a.leg}: {'PASS' if not f else f'FAIL ({len(f)})'}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
