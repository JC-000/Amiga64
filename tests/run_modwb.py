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
XP_LEN, XS_LEN = 0x100, 0x8A          # W16-E: XS_ACRB0/1 at 136/137
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
            ("exec", -156): "UserState", ("exec", -198): "AllocMem", ("exec", -204): "AllocAbs",
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
ERR_BUSY, ERR_TRACE = 8, 9
FOREIGN = 0x0A0000                    # another instance's state struct (stub/host)
REF_TRACE, REF_FOREIGN = 0x40, 0x80   # XP_REFUSE: AllocAbs refused; a foreign anchor late

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
    "nogate": ["MW_MUT_NOGATE"],            # a held button exits at once
    "noreply": ["MW_MUT_NOREPLY"],          # the WBStartup never replied
    "tbcrb": ["MW_MUT_TBCRB"],              # the restore stops AROS's CIA-B timer B
    "anchorclobber": ["MW_MUT_ANCHORCLOBBER"],  # a second instance overwrites the anchor
    "anchorendany": ["MW_MUT_ANCHORENDANY"],    # the withdraw clears another's anchor
    "latchrace": ["MOD_MUT_R_LATCHRACE"],   # fix-1's old latch write (count, then write)
    "flagcount": ["MOD_MUT_FLAGCOUNT"],     # the replay counts TA underflows by ICR flag
}
# rev16-x's review mutants (its mut/, re-targeted as switches), and the
# package's earlier ones moved out of the 10 (supervisor, fix round 1)
REVIEW_MUTANTS = {
    "R1_nolmbwait": ["MW_MUT_NOLMBWAIT"],   # the restore does not wait for the release
    "R3_noreread": ["MOD_MUT_R_NOREREAD"],  # sw_now without the frame-turn re-read
    "R4_nolatchcount": ["MOD_MUT_R_NOLATCHCOUNT"],  # sw_latch writes without a count
    "R7_stageeignored": ["MOD_MUT_R_STAGEE"],   # stage_arm ignores P_STAGEE
    "R8_owednever": ["MOD_MUT_R_OWEDNEVER"],    # sw_owed never says "owed"
    "anchorany": ["MW_MUT_ANCHORANY"],      # the anchor without a valid profile block
    "anchorfirst": ["MW_MUT_ANCHORFIRST"],  # 'MWBA' written before the pointer
    "noint": ["MW_MUT_NOINT"],              # INTENA not restored
    "E3_atbcrb": ["MW_MUT_ATBCRB"],         # W16-E3: the restore stops CIA-A TB (the old code)
    "E3_atbclaim": ["MW_MUT_ATBCLAIM"],     # W16-E3: ciaa TB claimed (AddICRVector) again
    "E4_nowait": ["MOD_MUT_NOWAIT"],        # W16-E4: stage_wait does not wait (rev16-e 1)
    "E5_stagee4": ["MOD_MUT_STAGEE4"],      # W16-E5: 4x STAGE_E, no profile (rev16-e2 1)
}
# killed by the model only: the write order (VICE sees the end state and
# polls), or a model case (an injected stall, a model E rate)
MODEL_ONLY_MUTANTS = {"anchorfirst", "latchrace", "R3_noreread", "R4_nolatchcount",
                      "R7_stageeignored", "R8_owednever", "E4_nowait", "E5_stagee4"}

# bounds of the model's timing checks (fix round 1)
JITTER_E = 600            # a tick's lateness above its floor, no stall injected
STAGE_SLACK_E = 150       # stage delay: P_STAGEE + the level-2 entry
NO_PLAY = {"busy", "claim_refused", "not_mk", "no_file", "init_err"}   # error paths: no stage
GRID_LATE_E = 128         # the software grid's constant lateness (replay.s SW_GUARD)
SILENCE_MAX_INSTR = 130   # retrigger silence: a DMACON clear to its set (product), the
                          # busy-wait excluded (W16-E4: green <= 114, OWEDFULL >= 143)

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

# another instance's state struct (published by its anchor), and anchors
FOREIGN_ST = b"MWBS" + struct.pack(">HBB", 1, 1, 0) + bytes(ST_SIZE - 8)
ANCHOR_BUSY = b"MWBA" + struct.pack(">III", FOREIGN, 0x00010100, 0)
ANCHOR_STALE = b"MWBA" + struct.pack(">III", FOREIGN + 0x100, 0x00010200, 0)


def pre_anchor_busy(an: bytes) -> bool:
    """The launcher's rule (modwb.s anchor_busy), for the staged anchors."""
    return an[:4] == b"MWBA" and struct.unpack_from(">I", an, 4)[0] == FOREIGN


def fxx_song() -> bytes:
    """rev16-x's probe/grid.py song: an Fxx on every row (8 tempos), speed 1,
    so CIA-B TA's latch is written on every tick: the latch-write race."""
    bpms = [0x7D, 0x50, 0xC8, 0x96, 0xFF, 0x40, 0xA0, 0x60]
    pats = []
    for p in range(4):
        rows = {r: ["C-2 01 F%02X" % bpms[(r + p) % len(bpms)]] for r in range(64)}
        rows[0] = rows[0] + ["--- .. F02"]
        pats.append(modgen.pattern(rows))
    return modgen.Song("fxx", pats, [0, 1, 2, 3]).build()


def trig_song() -> bytes:
    """255 BPM, speed 1, a note on every row in 2 channels: every tick
    retriggers, so an underflow often falls inside a trigger tick, and the
    tick it owes is run at once only through sw_owed (emit, run_loop);
    without that each such tick waits a period and, at this load, the
    backlog makes the tempo drift (rev16-x R8; measured: the model at
    tick_e 2557 keeps up with sw_owed, drifts 6292 E without)."""
    rows = {r: ["C-2 01 000", "E-2 01 000"] for r in range(64)}
    rows[0] = ["C-2 01 FFF", "E-2 01 F01"]
    return modgen.Song("trig", [modgen.pattern(rows)], [0, 0]).build()


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
                 expect_lost: bool = False, hold: int = 6, pre_anchor: bytes | None = None,
                 epc: tuple = (1, 10), grid: bool = False, inject: str = "",
                 builds: tuple = ("product", "trace"), jitter: bool = False,
                 atb: bool = False):
        self.name, self.song, self.mode, self.ld = name, song, mode, ld
        self.refuse, self.cli, self.arg1 = refuse, cli, arg1
        self.tool = "ModPlayer"
        self.fname = fname or (arg1 if mode == 2 else cli.split()[0].strip('"') if cli.split()
                               else "ModPlayer.mod")
        self.raw = raw if raw is not None else modgen.songs()[song]
        self.prof, self.lmb, self.expect, self.trace = prof, lmb, expect, trace
        self.tick_e, self.model_only = tick_e, model_only
        self.stall, self.expect_lost = stall, expect_lost    # (every N frames, E): model
        # the left button stays down `hold` host frames after the exit (the
        # restore must wait for its release: rev16-x HIGH-1)
        self.hold = hold
        # the anchor before the start: another instance's (its struct at
        # FOREIGN) or a stale one; None = zero (rev16-x MEDIUM-2)
        self.pre_anchor = pre_anchor
        # model only: the E rate (E per CPU cycle), the software grid against
        # CIA-B TA's real underflows, an injected stall ("old"/"new": around
        # the latch write; "turn": a frame turn inside sw_now), the jitter bound
        self.epc, self.grid, self.inject, self.jitter = epc, grid, inject, jitter
        self.builds = builds
        self.atb = atb                  # W16-E: CIA-A TB runs free before the call (AROS)
        self.anchored = (prof is not None and profile_ok(prof)   # the anchor rules
                         and not (pre_anchor and pre_anchor_busy(pre_anchor)))
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
        p[148] = int(self.atb)          # W16-E: XP_ATB
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
        out += [(ANCHOR, self.pre_anchor or bytes(16))]
        out += [(FOREIGN, FOREIGN_ST)]
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
             prof=profile_block(12960, s=3), jitter=True),
        Case("stall24", "tempo", trace=True, tick_e=2557, prof=profile_block(2557, s=3),
             stall=(24, 2170), model_only=True),
        Case("stall1", "tempo", ld="B", trace=True, tick_e=2557, prof=profile_block(2557, s=3),
             stall=(1, 2170), model_only=True),
        # a profile without real time (VICE): the Amiga's timing
        Case("prof_novrt", "restart", prof=profile_block(40, flags=0x01, s=9)),
        # a profile with a bad check: ignored
        Case("prof_bad", "restart", ld="B", prof=profile_block(2557, s=3, bad_check=True)),
        # LMB: pressed at launcher frame 20 (the button was up), released 6
        # frames after the exit: the OS must not see it down at Enable
        Case("lmb", "demo", prof=PROF_NORT, lmb=((0, False), (20, True)),
             expect=(ST_DONE, ("lmb",), 0)),
        # LMB held from the start (the launching click), released, pressed again
        Case("lmb_held", "demo", ld="B", prof=PROF_NORT,
             lmb=((0, True), (12, False), (30, True)),
             expect=(ST_DONE, ("lmb",), 0)),
        # a long press: still down 30 frames after the exit (under LMB_WAIT 50)
        Case("lmb_long", "demo", prof=PROF_NORT, lmb=((0, False), (16, True)), hold=30,
             expect=(ST_DONE, ("lmb",), 0)),
        # a second instance: another's anchor is published; back out, untouched
        Case("busy", "restart", prof=PROF_NORT, pre_anchor=ANCHOR_BUSY,
             expect=(ST_FAILED, (None,), ERR_BUSY)),
        # a stale anchor (no struct behind it): replaced, then withdrawn
        Case("stale", "restart", ld="B", prof=PROF_NORT, pre_anchor=ANCHOR_STALE),
        # another writer's anchor appears after our restore: not withdrawn by us
        Case("foreign_late", "restart", prof=PROF_NORT, refuse=REF_FOREIGN),
        # a quoted shell name with a space; the trace build: its RAM refused
        Case("cli_space", "restart", mode=1, cli='"My Tune.mod" x\n', fname="My Tune.mod",
             prof=PROF_NORT),
        Case("trace_busy", "restart", prof=PROF_NORT, refuse=REF_TRACE,
             expect=(ST_FAILED, (None,), ERR_TRACE), builds=("trace",)),
        # no audio.device: plays anyway
        Case("noaudio", "restart", refuse=0x10, prof=PROF_NORT),
        # W16-E: CIA-A TB runs free before the call (AROS, no resource owner): back as found
        Case("atb_free", "restart", ld="B", prof=PROF_NORT, atb=True),
        # backing out before any takeover
        Case("claim_refused", "restart", refuse=0x04, prof=PROF_NORT, expect=(ST_FAILED, (None,), 6)),
        Case("not_mk", "restart", raw=bytes(bad_mk), prof=PROF_NORT, expect=(ST_FAILED, (None,), 5)),
        Case("no_file", "restart", fname="Other.mod", expect=(ST_FAILED, (None,), 2)),
        # mod_init refuses it after the takeover: restored, then reported
        Case("init_err", "restart", raw=bytes(no_len), prof=PROF_NORT, expect=(ST_FAILED, ("init",), 7)),
        # the software grid against CIA-B TA across latch writes (rev16-x
        # MEDIUM-1: probe/grid.py's cases): an Fxx on every tick at s = 3's
        # tick_e, at the E rates of an Ultimate (2/15, 4/15 E per cycle), and
        # with a 2170 E stall every frame
        Case("fxx_215", "fxx", raw=fxx_song(), tick_e=2557, prof=profile_block(2557, s=3),
             epc=(2, 15), grid=True, model_only=True),
        Case("fxx_415", "fxx", raw=fxx_song(), ld="B", tick_e=2557,
             prof=profile_block(2557, s=3), epc=(4, 15), grid=True, model_only=True),
        Case("fxx_stall1", "fxx", raw=fxx_song(), tick_e=2557, prof=profile_block(2557, s=3),
             stall=(1, 2170), grid=True, model_only=True),
        # a stall forced across every latch write, the next underflow passing
        # before the write ("old") or after it ("new"): sw_amb decides
        Case("fxx_old", "fxx", raw=fxx_song(), tick_e=2557, prof=profile_block(2557, s=3),
             inject="old", grid=True, model_only=True),
        Case("fxx_new", "fxx", raw=fxx_song(), ld="B", tick_e=2557,
             prof=profile_block(2557, s=3), inject="new", grid=True, model_only=True),
        # a frame turn forced between sw_now's first TOD read and the beam
        # (the time must not go a frame back): ticks stay on time
        Case("turn", "tempo", trace=True, tick_e=2557, prof=profile_block(2557, s=3),
             inject="turn", jitter=True, model_only=True),
        # 255 BPM at s = 3's tick_e, no stall: ticks on time (a tick owed in
        # the retrigger gap is run at once: sw_owed)
        Case("rt_2557", "tempo", ld="B", trace=True, tick_e=2557,
             prof=profile_block(2557, s=3), jitter=True, model_only=True),
        Case("rt_trig", "trig", raw=trig_song(), trace=True, tick_e=2557,
             prof=profile_block(2557, s=3), jitter=True, model_only=True),
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
    taken = want_err not in (2, 5, 6, ERR_BUSY, ERR_TRACE)
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
    pre_a = PRE_MASKA | (0x02 if c.atb else 0)   # W16-E: TB enabled before the call
    if (maska, maskb) != (pre_a, PRE_MASKB):
        f.append(f"{c.name}: ICR resource masks ${maska:02X}/${maskb:02X}")
    if (hw["maska_hw"], hw["maskb_hw"]) != (pre_a, PRE_MASKB):
        f.append(f"{c.name}: CIA ICR enable masks ${hw['maska_hw']:02X}/"
                 f"${hw['maskb_hw']:02X}, want ${pre_a:02X}/${PRE_MASKB:02X}")
    if xs[137] != xs[136]:              # W16-E; W16-E3: every case (CIA-A TB never programmed)
        f.append(f"{c.name}: CIA-A CRB ${xs[137]:02X} after, ${xs[136]:02X} before "
                 "(CIA-A TB is AROS's, never programmed)")
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
    if xs[130]:
        f.append(f"{c.name}: the left button was down at Enable (the Workbench would "
                 "see a click)")
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
    if opened != c.fname and want_err not in (2, ERR_BUSY, ERR_TRACE):
        f.append(f"{c.name}: opened {opened!r}, want {c.fname!r}")
    if want_err in (2, 5):                  # the requester names the file asked for
        fmt = rd(struct.unpack_from(">I", xs, 60)[0], 80).split(b"\0")[0]
        arg = rd(struct.unpack_from(">I", xs, 132)[0], 32).split(b"\0")[0].decode("latin-1")
        if b"%s" not in fmt or arg != opened:
            f.append(f"{c.name}: the requester says {fmt!r} with {arg!r} (opened {opened!r})")
    # the call order
    ob = first(calls, "OwnBlitter")
    after = lambda name, k=ob: next((i for i in range(max(k, 0), len(calls))   # noqa: E731
                                     if calls[i][0] == name), -1)
    if taken:
        seq = [ob, first(calls, "LoadView", lambda x: x[4] == 0),
               after("Forbid"), after("Disable"), first(calls, "SuperState"),
               first(calls, "UserState"), after("Enable"), after("Permit"),
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
        if claim != b"\x01\xff\x00\x01":     # W16-E3: ciaa TB never asked ($FF)
            f.append(f"{c.name}: claims {claim.hex()} (ciaa TA, ciab TB are the OS's)")
    else:
        # (the anchor's Forbid/Permit pair, first; the WB reply's Forbid, last)
        k0 = (first(calls, "Permit") + 1) if c.prof and profile_ok(c.prof) else 0
        for n in ("Forbid", "Disable", "SuperState", "LoadView", "OwnBlitter"):
            if n in names[k0:-2]:
                f.append(f"{c.name}: backed out, but called {n}")
        if not struct.unpack_from(">I", xs, 60)[0]:
            f.append(f"{c.name}: backed out without a requester")
    if want_err == 6 and st[ST["claim"]:ST["claim"] + 4] != b"\x01\xff\x01\x01":   # W16-E3
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
        if c.jitter and hw.get("jitter", True) and worst > JITTER_E:
            f.append(f"{c.name}: a tick {worst} E late above its floor (bound {JITTER_E}: "
                     "no stall but the injected ones; a tick owed and not run?)")
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
    if c.pre_anchor and pre_anchor_busy(c.pre_anchor):
        # another instance's anchor: never touched (nor its struct)
        if an != c.pre_anchor:
            f.append(f"{n}: another instance's anchor changed: {an.hex()}")
        if wlog:
            f.append(f"{n}: {len(wlog)} writes to another instance's anchor")
        return f
    foreign = ANCHOR_BUSY[:8] + bytes(8)
    if c.refuse & REF_FOREIGN:
        # another writer published after our restore: ours was gone, theirs stays
        if an != foreign:
            f.append(f"{n}: the late foreign anchor is {an.hex()} after the return "
                     f"(want {foreign.hex()}: withdrawn by the wrong owner?)")
        if not wlog:
            return f
        # the order check up to their write, as if ours was withdrawn there
        wlog = wlog[:next((k for k, w in enumerate(wlog) if w[1] == foreign), len(wlog))]
        an = bytes(4) + wlog[-1][1][4:]
        wlog = wlog + [(wlog[-1][0], an)]
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
    prev = c.pre_anchor or bytes(16)
    pub = wd = None
    for k, (ncalls, b) in enumerate(wlog):
        mag, was = b[:4], prev[:4]
        if mag != was:
            if pub is None and wd is None and c.pre_anchor and mag == bytes(4):
                pass                        # the stale anchor withdrawn first
            elif mag == b"MWBA" and pub is None:
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


def lmb_down(c: Case, rd, clock: float) -> bool:
    """The left button by the case's schedule of launcher frames (A_FRAMES)
    until the launcher's exit (why = LMB); then still down for c.hold host
    frames (clock: the host's frame count) and up: never forced up by the
    restore (the launcher must wait for the release before Enable: the
    stub records the button at Enable).  The struct is read at its load
    address (the case's map), not through the anchor."""
    st = rd(c.st_at, ST_SIZE)
    if st[:4] != b"MWBS":
        return c.lmb[0][1]
    if WHY.get(st[ST["why"]]) == "lmb":
        if getattr(c, "exit_at", None) is None:
            c.exit_at = clock
        return clock - c.exit_at < c.hold
    fr = struct.unpack_from(">I", st, ST["frames"])[0]
    down = False
    for t, d in c.lmb:
        if fr >= t:
            down = d
    return down


# --------------------------------------------------------------------------
# Leg "model"
# --------------------------------------------------------------------------

def map_syms(out: str) -> dict[str, tuple[str, int]]:
    """vlink's map: symbol -> (section, offset), the launcher's link."""
    syms, sec = {}, None
    with open(os.path.join(out, "modwb.map")) as fh:
        for line in fh:
            if line.startswith("Symbols of "):
                sec = line.split()[2].rstrip(":")
                continue
            p = line.split()
            if sec and len(p) >= 2 and p[0].startswith("0x") and p[1].endswith(":"):
                syms.setdefault(p[1][:-1], (sec, int(p[0], 16)))
    return syms


def resolve(exe: bytes, ld: str, syms: dict, names) -> dict[str, int]:
    """Absolute addresses at the case's load map (CODE: the first code hunk;
    BSS: the non-chip BSS hunk)."""
    segs = hunk.parse(exe).segments
    addrs = loadseg(exe, MAPS[ld])[1]
    base = {"CODE": addrs[0],
            "BSS": next(a for s, a in zip(segs, addrs) if s.kind == "bss" and s.memory_flags != 1)}
    return {n: base[syms[n][0]] + syms[n][1] for n in names if n in syms}


SYM_NAMES = ("G", "sw_one", "sw_wlatch", "sw_now", "sw_amb", "do_tick", "stage_step",
             "finish", "stage_arm",      # W16-E3: stage_arm
             "ta_cnt", "stage_wait")     # W16-E4: the busy-wait, counted apart
TURN_E = 300              # "turn": a frame boundary this close is forced inside sw_now


class Model(RM.Model):
    """run_mod's model (Musashi, chipset_ref's CIAs and Paula, E = CPU/10 or
    the case's rate, VERTB per 14188 E) with the stub's chunks; the left
    button is CIA-A PRA pin 6, driven from the launcher's frame count.
    Fix round 1: CIA-B TA's real underflows against the software grid
    (c.grid), stalls injected at a PC (c.inject), the DMA stage delay (E
    from the stage one-shot's start to the DMACON set) and the retrigger
    silence (instructions from a tick's DMACON clear to its set)."""

    def __init__(self, c: Case, chunks, mw_code_len: int, sy: dict | None = None):
        class Src:
            e_per_cyc = c.epc

            def chunks(self, _img):
                return chunks
        sy = sy or {}
        super().__init__(b"", {k: sy[k] for k in ("do_tick", "stage_step", "finish")
                               if k in sy}, Src())
        self.case, self.sy = c, sy
        self.mw_lo, self.mw_hi = c.entry, c.entry + mw_code_len
        self.anchor_last, self.anchor_log = c.pre_anchor or bytes(16), []
        for a, d in chunks:                 # run_mod's trapped UI area holds its own copy
            for k in range(len(d)):
                if 0x040000 <= a + k < 0x080000:
                    self.uiarea[a + k - 0x040000] = d[k]
        self.hw_under: list[int] = []       # CIA-B TA's underflows (E), continuous mode
        self.sw_under: list[int] = []       # the software grid's (G_UTOD x FRAME_E + G_UE)
        self._one = None
        self.arms: list[int] = []           # E of each stage_arm entry (W16-E3)
        self.sets: list[int] = []           # E of each audio DMACON set
        self.silence: list[tuple[int, int, int]] = []  # a tick's clear -> its set: (all
        self.hi = 0                         # instructions, those at IPL >= 2, those of
        self.hiw = 0                        # them in stage_wait and its ta_cnts: W16-E4)
        self._wait = None                   # stage_wait's (return PC, SP) while inside
        self._sclr = None
        self.injected = 0
        self.irq_kicks = 0
        self.amb = 0                        # sw_amb entries
        if c.grid:
            cia, orig = self.cs.ciab, self.cs.ciab.eclock

            def ecl(n, cia=cia, orig=orig, m=self):
                if cia.cra & 1 and not cia.cra & 0x28 and n > cia.ta:
                    t = m.e + cia.ta + 1
                    while t <= m.e + n:
                        m.hw_under.append(t)
                        t += cia.ta_latch + 1
                orig(n)
            cia.eclock = ecl

    def on_instr(self, pc):
        p = pc & 0xFFFFFF
        if self.mw_lo <= p < self.mw_hi:    # the launcher's own code: the anchor's
            b = bytes(self.mem.r_block(ANCHOR, 16))     # changes, in write order
            if b != self.anchor_last:
                self.anchor_last = b
                ncalls = (self.mem.r32(XS + 44) - XLOG) // LOG_ENT
                self.anchor_log.append((ncalls, b))
        sy, inj = self.sy, self.case.inject
        if p == sy.get("stage_wait"):       # W16-E4: inside stage_wait, until it returns
            self._wait = (self.mem.r32(self.cpu.r_sp()), self.cpu.r_sp() + 4)
        elif self._wait and p == self._wait[0] and self.cpu.r_sp() == self._wait[1]:
            self._wait = None
        if (self.cpu.r_sr() >> 8) & 7 >= 2:
            self.hi += 1
            if self._wait and sy["ta_cnt"] <= p < sy["sw_now"]:
                self.hiw += 1
        if self.case.grid:
            if p == sy.get("sw_one"):
                self._one = (self.mem.r32(self.cpu.r_sp()), self.cpu.r_sp() + 4)
            elif self._one and p == self._one[0] and self.cpu.r_sp() == self._one[1]:
                g = sy["G"]
                self.sw_under.append(self.mem.r32(g + 112) * RM.FRAME_E + self.mem.r16(g + 110))
                self._one = None
            if p == sy.get("sw_amb"):
                self.amb += 1
        if p == sy.get("stage_arm"):         # W16-E3: no one-shot; the stages'
            self.arms.append(self.e)         # busy-wait starts here
        if inj and self.cs.ciab.cra & 1:
            at = {"old": sy.get("sw_wlatch"), "new": sy.get("sw_wlatch", -99) + 16}.get(inj)
            if p == at:                     # the next underflow passes in a stall
                self.advance(self.cs.ciab.ta + 1 + 40)      # before/after the write
                self.injected += 1
        if inj == "turn" and p == sy.get("sw_now", -99) + 4:   # the first beam read
            left = RM.FRAME_E - self.e % RM.FRAME_E
            if left <= TURN_E:
                self.advance(left + 2)
                self.injected += 1
        if not self.halted and self.mem.r16(p) == 0xAFFF:
            self.xs_snap = self.read(XS, XS_LEN)    # Musashi then takes the A-line
        super().on_instr(pc)                        # trap: the stub's fault stub

    def c_w16(self, a, v):
        r = a & 0x1FE
        if r == 0x096 and v & 0xF and not (self.ui_range[0] <= self.cur_pc < self.ui_range[1]):
            if v & 0x8000:
                self.sets.append(self.e)
                if self._sclr and v & self._sclr[1]:
                    self.silence.append((self.icount - self._sclr[0], self.hi - self._sclr[2],
                                         self.hiw - self._sclr[3]))
                    self._sclr = None
            elif self._dt is not None and self._sclr is None:
                self._sclr = (self.icount, v & 0xF, self.hi, self.hiw)
        if self.cur_pc == self.sy.get("finish"):
            self._sclr = None
        super().c_w16(a, v)

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
        down = lmb_down(self.case, self.read, self.frames)
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
            ic = self.icount
            res = self.mach.execute(self.SLICE)
            ran = res.cycles if res.cycles > 0 else self.SLICE
            if self.icount == ic and self.ipl > (self.cpu.r_sr() >> 8) & 7:
                # Musashi stopped (STOP) with a level above the mask asserted
                # and not taken (seen once, fxx_stall1 product): a 68000 takes
                # it, so the level is re-asserted (counted, logged)
                self.cpu.set_irq(0)
                self.cpu.set_irq(self.ipl)
                self.irq_kicks += 1
            n, rem = divmod(rem + ran * self.epc[0], self.epc[1])
            self.advance(n)
            if self.frames != fr:
                fr = self.frames
                self.frame_hook()
                if every and fr % every == 0:
                    self.advance(stall_e)       # a c2v conversion: the guest stalled,
                    fr = self.frames            # time (the CIAs, the frames) runs on

    def timing_checks(self, c: Case, product: bool) -> list[str]:
        """The grid; the stage delay (the product build: in a trace build the
        trace's cost makes ticks flush their stages)."""
        f = []
        if c.grid:
            sw, hw = self.sw_under, self.hw_under
            offs = [s - h for s, h in zip(sw, hw)]
            c.grid_res = (len(sw), len(hw), sorted(set(offs))[:6], self.amb, self.injected)
            if len(sw) < 100 or not 0 <= len(hw) - len(sw) <= 1:
                f.append(f"{c.name}: the software grid counted {len(sw)} underflows, CIA-B TA "
                         f"had {len(hw)}")
            if offs and (len(set(offs)) != 1 or not 0 <= offs[0] <= GRID_LATE_E):
                k = next((i for i in range(1, len(offs)) if offs[i] != offs[0]), 0)
                f.append(f"{c.name}: the software grid left CIA-B TA's at underflow {k}: "
                         f"offsets (sw - hw E) {offs[max(0, k - 1):k + 3]} (want one value "
                         f"in 0..{GRID_LATE_E})")
            if c.inject in ("old", "new") and (self.injected < 100 or self.amb < self.injected):
                f.append(f"{c.name}: {self.injected} stalls injected, sw_amb ran {self.amb}x")
        want_se = (c.tick_e * 300 + 20455 // 2) // 20455 if c.tick_e else 0
        d = []
        for k, a in enumerate(self.arms):
            nxt = self.arms[k + 1] if k + 1 < len(self.arms) else 1 << 62
            s = next((x for x in self.sets if a <= x < nxt), None)
            if s is not None:
                d.append(s - a)
        # W16-E4 (rev16-e 1): the floor, every product case.  The busy-wait
        # is the only thing that holds the stage back now: never under
        # P_STAGEE E (no profile: STAGE_E), at any E rate or stall
        floor = (want_se or 1) if c.tick_e else 300
        if product and d and min(d) < floor:
            f.append(f"{c.name}: the stage delay is {min(d)} E at least (of {len(d)}), want "
                     f">= P_STAGEE {floor}")
        # (the ceiling: the model's own E rate, no stall around the latch
        # writes: the level-2 entry's E then depend on the rate and the stall.
        # W16-E5 (rev16-e2 1): at every profile, no profile (STAGE_E) too;
        # no stage at all fails unless the case is one of the error paths that
        # never play (rev16-e2 R2-1: exempt by name, not by profile)
        if product and c.epc == (1, 10) and not c.inject and (d or c.name not in NO_PLAY):
            med = sorted(d)[len(d) // 2] if d else None
            c.stage_res = (len(d), med, min(d) if d else None)
            if med is None or med > floor + STAGE_SLACK_E:
                f.append(f"{c.name}: the stage delay is {med} E (median of {len(d)}), want "
                         f"P_STAGEE {floor} + at most {STAGE_SLACK_E}")
        elif product and d:
            c.stage_res = (len(d), "floor only", min(d))
        return f


def leg_model(defs: list[str], red: bool = False, only: list[str] | None = None) -> list[str]:
    """Every case on the product build and on the MOD_TRACE build (the
    replay checked against mod_ref and the tick timing: trace builds)."""
    fails: list[str] = []
    stub = None
    for kind in ("product", "trace"):
        out = os.path.join(ROOT, "build", "modwb-model-" + kind + ("-" + "-".join(defs) if defs
                                                                    else "") + ("-red" if red else ""))
        exe = build((["MOD_TRACE"] if kind == "trace" else []) + defs, out, red)
        stub = stub or build_stub(out)
        syms = map_syms(out)
        for c in cases():
            if (only and c.name not in only) or kind not in c.builds:
                continue
            if kind == "product":
                c.ref = None
            t0 = time.monotonic()
            ch = c.chunks(exe, stub)
            m = Model(c, ch, launcher_code_len(out), resolve(exe, c.ld, syms, SYM_NAMES))
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
                f += m.timing_checks(c, kind == "product")
            f = [f"[{kind}] {x}" for x in f]
            log(f"model[{kind}] {c.name}: {m.frames} frames, {m.icount} instructions, "
                f"lost/worst stall/period {getattr(c, 'lost', '-')}, grid "
                f"{getattr(c, 'grid_res', '-')}, stage {getattr(c, 'stage_res', '-')}, "
                f"{'irq re-asserted %dx, ' % m.irq_kicks if m.irq_kicks else ''}"
                f"{time.monotonic() - t0:.1f}s: {'ok' if not f else 'FAIL'}")
            for x in f[:8]:
                print("    " + x)
            fails += f
            if f and STOP_FIRST[0]:
                return fails
    return fails


def silence_gate(defs: list[str]) -> list[str]:
    """The retrigger silence (rev16-x LOW-MEDIUM-5), product build, model
    speed: from each DMACON clear a tick writes to the DMACON set of the
    same channel(s), the instructions run at IPL >= 2 (the handlers, and
    W15's IPL-raised publish/wait in the foreground: what holds the level-2
    stage back).  All instructions are logged too.  Red: MOD_MUT_OWEDFULL
    (a full count in the gap).
    W16-E4 (rev16-e 2): the stages busy-wait in stage_wait (no CIA-A TB),
    which is counted apart: the bound is on the gap's work without the
    wait, asserted at tick_e 20455, 2557 and with no profile (modwb's
    P_TICKE 0 path: VICE, an Ultimate without the block).  The wait's own
    IPL-2 instructions are logged per profile, not asserted here: its
    length is held by the stage delay's floor and ceiling in timing_checks,
    both asserted at every profile (W16-E5)."""
    from tests import run_player
    out = os.path.join(ROOT, "build", "modwb-silence" + ("-" + "-".join(defs) if defs else ""))
    exe = build(list(defs), out)
    stub = build_stub(out)
    syms = map_syms(out)
    if not syms["ta_cnt"][1] < syms["stage_wait"][1] < syms["sw_now"][1]:
        raise SystemExit("silence_gate: want ta_cnt, stage_wait, sw_now in that order")
    songs = {"probe": run_player.probe_song(), "basic": modgen.songs()["basic"],
             "tempo": modgen.songs()["tempo"], "retrig": modgen.songs()["retrig"]}
    f, res = [], {}
    for te in (20455, 2557, 0):
        for n, raw in songs.items():
            c = Case(f"sil_{n}_{te or 'noprof'}", n, raw=raw, tick_e=te,
                     prof=profile_block(te, s=3) if te else PROF_NORT)
            m = Model(c, c.chunks(exe, stub), launcher_code_len(out),
                      resolve(exe, c.ld, syms, SYM_NAMES))
            m.run(max_e=c.max_frames * RM.FRAME_E)
            work = max((x[1] - x[2] for x in m.silence), default=0)
            wait = max((x[2] for x in m.silence), default=0)
            res[c.name] = (len(m.silence), work, wait, max((x[0] for x in m.silence), default=0))
            if not m.silence:
                f.append(f"{c.name}: no retrigger seen")
            elif work > SILENCE_MAX_INSTR:
                f.append(f"{c.name}: retrigger silence {work} instructions at IPL >= 2 "
                         f"without the stage wait, over {len(m.silence)} retriggers (bound "
                         f"{SILENCE_MAX_INSTR})")
    log("retrigger silence (product; n, max IPL>=2 without the wait, max IPL>=2 in "
        f"stage_wait (bounded by timing_checks), max all): {res}")
    return f


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
    exes = {"product": build(list(defs), out + "-pr", red),
            "trace": build(["MOD_TRACE", *defs], out + "-tr", red)}
    stub = build_stub(out)
    fails: list[str] = []

    def run_case(m, c: Case, kind: str) -> list[str]:
        sess = Session(target=m.tg, labels=m.labels, timeout=300.0)
        g = Guest(SimpleNamespace(target=m.tg, timeout=600.0), m.labels)
        cia = m.lab("cia_st")
        t0 = time.monotonic()
        m.load_driver()
        m.w(AB.P, bytes([0, RM.CHIP_FILL_BANKS]))
        m.call("aros_fill")
        if kind == "product":
            c.ref = None
        ch = c.chunks(exes[kind], stub)
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
                down = lmb_down(c, peek.read, (time.monotonic() - t0) / VICE_FRAME_S)
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
              "timing": True,
              # (no jitter bound: the conversions stall the guest by more)
              "jitter": False}
        f += check(c, g.read, hw)
        if peek and c.anchored and not seen_pub:
            f.append(f"{c.name}: the anchor never seen published while playing")
        f = [f"[{kind}] {x}" for x in f]
        log(f"vice[{kind}] {c.name} (s = {s}): {time.monotonic() - t0:.0f}s, lost/worst stall/"
            f"period {getattr(c, 'lost', '-')}: "
            f"{'ok' if not f else 'FAIL'}")
        for x in f[:8]:
            print("    " + x)
        return f

    # one emulator session per case: a second run in the same session
    # misbehaves (finding F1, build/evidence/w16-x/PROGRESS.md)
    todo = []
    for kind in ("product", "trace"):
        allc = {x.name: x for x in cases()}      # (fresh: the LMB schedule's state)
        todo += [(c, kind) for c in ([allc[n] for n in only] if only else list(allc.values()))
                 if not c.model_only and kind in c.builds]
    if ONE_SESSION[0]:                  # F1's reproduction: every case in one session
        AB.vice_session(prg, lambda m: [fails.extend(run_case(m, c, k)) for c, k in todo])
        return fails
    for c, kind in todo:
        f = AB.vice_session(prg, lambda m, c=c, kind=kind: run_case(m, c, kind))
        fails.extend(f)
        if f and STOP_FIRST[0]:
            break
    return fails



VICE_FRAME_S = 0.06                     # a guest frame at s = 6 (measured)
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
    ap.add_argument("--review-mutants", action="store_true",
                    help="rev16-x's mutants and the package's retired ones")
    ap.add_argument("--only", default="", help="mutants (with --mutants) or cases")
    ap.add_argument("--red", action="store_true")
    ap.add_argument("--silence-red", action="store_true",
                    help="the retrigger silence gate on MOD_MUT_OWEDFULL (must fail)")
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

    if a.mutants or a.review_mutants:
        STOP_FIRST[0] = True
        bad = []
        for name, defs in (REVIEW_MUTANTS if a.review_mutants else MUTANTS).items():
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
    if a.silence_red:
        f = silence_gate(["MOD_MUT_OWEDFULL"])
        for x in f:
            print("    " + x)
        print(f"run_modwb silence red: {'FAIL as expected' if f else 'PASSED (no teeth!)'}")
        return 0 if f else 1
    if a.red:
        STOP_FIRST[0] = True
        f = leg([], red=True, cs=only or None)
        print(f"run_modwb {a.leg} red: {'FAIL as expected' if f else 'PASSED (no teeth!)'}")
        return 0 if f else 1
    f = leg([], cs=only or None)
    if a.leg == "model" and not only:
        f += silence_gate([])
    print(f"run_modwb {a.leg}: {'PASS' if not f else f'FAIL ({len(f)})'}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
