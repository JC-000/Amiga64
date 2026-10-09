"""W13-W: runtime/ops/td.s at host level (the DF0 drive and the HLE trackdisk).

td.s is assembled standalone with a shim (the symbols it imports: the
memory API, mem_rec_dirty, code_invpg, mach_fhook, cia_st and the CIA_*
layout) and RUN in the 6502 emulator of test_hle_dos_uci.py, on the REU
controller model of test_snap_file.py.  The memory API entries are served
in Python (guest memory = the 16 MB REU, big-endian, m_val little-endian as
in core.s) and every call is recorded.

Reference for the drive lines: the boot model's Floppy
(tools/s3probe/wb_disk.py), which boots the real ROM from a disk.
Structure offsets: exec/io.h, exec/nodes.h, exec/execbase.h (public NDK).
"""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from host.tests import test_hle_dos_uci as U
from host.tests import test_snap_file as SF

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "s3probe"))
from wb_disk import Floppy  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("cl65") is None,
                                reason="cl65 (cc65) not installed")

TD_ADF = 0x200000
ADF_SIZE = 901120
CIA_SZ, CIA_PIN, CIA_DDRB = 45, 32, 3
TD_BUF = 0xB600
SYSBASE = 0xC00568
DEVBASE = 0xC0E5A4
ORIG = 0xFB65FE
IO = 0xC23F00
RET = 0xF81234
SP0 = 0xC08000
TDERR_NS, TDERR_WP = 20, 28
ZP = dict(r_a1=0x34, r_a7=0x4C, r_pc=0x50, m_addr=0x58, m_val=0x5C, st_ipc=0x8D,
          t_ea=0x60)

MAIN_CFG = """
MEMORY {
    ZP:   start = $0080, size = $0070, type = rw;
    MAIN: start = $1000, size = $1000, file = %O;
    BSSM: start = $6000, size = $0800, type = rw;
}
SEGMENTS {
    ZEROPAGE: load = ZP, type = zp, optional = yes;
    CODE:     load = MAIN, type = ro;
    RODATA:   load = MAIN, type = ro, optional = yes;
    DATA:     load = MAIN, type = rw, optional = yes;
    BSS:      load = BSSM, type = bss, optional = yes;
}
"""
MOD_CFG = """
MEMORY { M: start = $C200, size = $0400, file = %O; }
SEGMENTS { CODE: load = M, type = rw; RODATA: load = M, type = ro, optional = yes; }
"""
TD_ORG = 0xC200
HOOK_BASE = 0x4000
HOOKS = ("mem_read8", "mem_read16", "mem_read32", "mem_write8", "mem_write16",
         "mem_write32", "mem_rec_dirty", "code_invpg", "chained")
SHIM_AT = {"mach_fhook": 0x6F00, "cia_st": 0x6E00}


def _labels(lbl: Path) -> dict:
    lab = {}
    for line in lbl.read_text().splitlines():
        p = line.split()
        if len(p) == 3 and p[0] == "al":
            lab[p[2].lstrip(".")] = int(p[1], 16)
    return lab


def _run(cmd, cwd=ROOT):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    assert r.returncode == 0, r.stdout + r.stderr


def build_td(d: Path, src: str | None = None, main_src: str | None = None) -> tuple[dict, dict]:
    """runtime/ops/td.s (MAIN half) linked standalone, then the module
    tests/aros/td.s (or *src*) at TD_ORG with the MAIN labels and the shim
    addresses as -D defines, as tests/aros_wb.py builds it against
    runner.lbl.  -> ({addr: bytes}, labels)."""
    (d / "main.cfg").write_text(MAIN_CFG)
    (d / "mod.cfg").write_text(MOD_CFG)
    main = d / "td_main.s"
    main.write_text(main_src if main_src is not None else (ROOT / "runtime/ops/td.s").read_text())
    _run(["cl65", "-g", "-t", "none", "-C", str(d / "main.cfg"), "-o", str(d / "main.bin"),
          "-Ln", str(d / "main.lbl"), "--asm-include-dir", str(ROOT / "runtime/ops"), str(main)])
    lab = _labels(d / "main.lbl")
    defs = {n: HOOK_BASE + i for i, n in enumerate(HOOKS)}
    defs.update(SHIM_AT)
    defs.update(CIA_PIN=CIA_PIN, CIA_DDRB=CIA_DDRB, CIA_B=CIA_SZ, TD_ORG=TD_ORG)
    for n, v in lab.items():
        if n.startswith("td_"):
            defs[n] = v
    mod = d / "td_mod.s"
    mod.write_text(src if src is not None else (ROOT / "tests/aros/td.s").read_text())
    args = [x for n, v in defs.items() for x in ("-D", f"{n}=${v:04X}")]
    _run(["ca65", "-g", *args, "-I", str(ROOT / "tests/aros"), "-o", str(d / "mod.o"), str(mod)])
    _run(["ld65", "-C", str(d / "mod.cfg"), "-o", str(d / "mod.bin"), "-Ln", str(d / "mod.lbl"),
          str(d / "mod.o")])
    mlab = _labels(d / "mod.lbl")
    mod_bin = bytearray((d / "mod.bin").read_bytes())
    mod_bin[3:7] = hashlib.sha1(bytes(mod_bin)).digest()[:4]     # aros_wb's stamp
    for n, v in mlab.items():
        lab.setdefault(n, v)
    lab.update({n: HOOK_BASE + i for i, n in enumerate(HOOKS)})
    lab.update(SHIM_AT)
    lab["td_arm_entry"] = TD_ORG
    return {0x1000: (d / "main.bin").read_bytes(), TD_ORG: bytes(mod_bin)}, lab


@pytest.fixture(scope="module")
def build(tmp_path_factory):
    return build_td(tmp_path_factory.mktemp("td"))


def test_module_layout(build):
    images, lab = build
    assert len(images[TD_ORG]) <= 0x400                    # $C200-$C5FF
    assert images[TD_ORG][0] == 0x4C and lab["td_arm"] == images[TD_ORG][1] | images[TD_ORG][2] << 8


class Dev:
    def __init__(self):
        self.reu = bytearray(1 << 24)


class Rig:
    RET6502 = 0xFFF0

    def __init__(self, build):
        self.images, self.lab = build
        self.dev = Dev()
        self.bus = SF.RecBus(self.dev)
        for at, img in self.images.items():
            self.bus.ram[at:at + len(img)] = img
        self.cpu = U.CPU(self.bus)
        self.calls: list[tuple] = []
        self.hooks = {self.lab[n]: n for n in HOOKS}

    # --- memory
    def g_w(self, a, b):
        self.dev.reu[a:a + len(b)] = b

    def g_r(self, a, n):
        return bytes(self.dev.reu[a:a + n])

    def g_l(self, a, v):
        self.g_w(a, struct.pack(">I", v & 0xFFFFFFFF))

    def g_rl(self, a):
        return struct.unpack(">I", self.g_r(a, 4))[0]

    def zp(self, name, v=None, n=4):
        a = ZP[name]
        if v is None:
            return int.from_bytes(self.bus.ram[a:a + n], "little")
        self.bus.ram[a:a + n] = (v & ((1 << 8 * n) - 1)).to_bytes(n, "little")

    def poke(self, name, data: bytes):
        a = self.lab[name]
        self.bus.ram[a:a + len(data)] = data

    def peek(self, name, n):
        a = self.lab[name]
        return bytes(self.bus.ram[a:a + n])

    def b(self, name):
        return self.peek(name, 1)[0]

    # --- the memory API, as core.s serves it (guest big-endian, m_val LE)
    def _hook(self, name):
        ram = self.bus.ram
        addr = int.from_bytes(ram[0x58:0x5C], "little")
        size = 1 if name.endswith("8") else 2 if name.endswith("16") else 4
        if name.startswith("mem_read"):
            assert addr < 1 << 24, f"{name} m_addr ${addr:08X}"
            v = self.g_r(addr, size)
            ram[0x5C:0x5C + size] = v[::-1]
            self.calls.append((name, addr, int.from_bytes(v, "big")))
        elif name.startswith("mem_write"):
            assert addr < 1 << 24, f"{name} m_addr ${addr:08X}"
            v = bytes(ram[0x5C:0x5C + size])[::-1]
            self.g_w(addr, v)
            self.calls.append((name, addr, int.from_bytes(v, "big")))
        elif name == "code_invpg":
            self.calls.append((name, self.cpu.x << 16 | self.cpu.a << 8))
            self.cpu.a = 0x5A                       # clobbers A, X; keeps Y
            self.cpu.x = 0xA5
        else:
            self.calls.append((name,))

    def call(self, name, a=None, x=None, limit=2_000_000):
        c = self.cpu
        c.sp = 0xFF
        r = self.RET6502 - 1
        c.push(r >> 8)
        c.push(r & 0xFF)
        c.pc = self.lab[name]
        if a is not None:
            c.a = a
        if x is not None:
            c.x = x
        steps = 0
        while c.pc != self.RET6502:
            h = self.hooks.get(c.pc)
            if h:
                self._hook(h)
                lo = c.pull()
                c.pc = ((c.pull() << 8) | lo) + 1 & 0xFFFF
                continue
            c.step()
            steps += 1
            assert steps < limit, f"{name}: no return (PC ${c.pc:04X})"
        return c

    def n(self, kind):
        return sum(1 for k in self.calls if k[0] == kind)


def rig(build, on=True) -> Rig:
    r = Rig(build)
    r.poke("td_on", bytes([1 if on else 0]))
    r.poke("cia_st", bytes(90))
    r.bus.ram[r.lab["cia_st"] + CIA_PIN] = 0xEB             # cia_reset's CIA-A pins
    return r


def pins(r: Rig) -> int:
    return r.bus.ram[r.lab["cia_st"] + CIA_PIN]


def prb(r: Rig, v: int, ddrb: int = 0xFF):
    r.bus.ram[r.lab["cia_st"] + CIA_SZ + CIA_DDRB] = ddrb
    r.zp("st_ipc", 0x123456)
    c = r.call("td_call", a=v, x=CIA_SZ)
    assert c.x == CIA_SZ and c.c == 0
    return pins(r)


# =============================================================================
# The drive lines
# =============================================================================

def test_off_changes_nothing(build):
    r = rig(build, on=False)
    c = r.call("td_call", a=0x77, x=CIA_SZ)
    assert c.c == 0 and c.x == CIA_SZ and pins(r) == 0xEB
    r.poke("mach_fhook", b"\x34\x12")
    r.call("td_arm")
    assert r.peek("mach_fhook", 2) == b"\x34\x12" and pins(r) == 0xEB


def test_arm_deselected_pins_and_chain(build):
    r = rig(build)
    r.poke("mach_fhook", struct.pack("<H", r.lab["chained"]))
    r.call("td_arm")
    assert pins(r) == 0xFF                                 # $EB | $3C
    assert r.peek("mach_fhook", 2) == struct.pack("<H", r.lab["td_fcall"])
    assert r.peek("td_vec", 2) == struct.pack("<H", r.lab["td_prb"])
    assert r.peek("td_fvec", 2) == struct.pack("<H", r.lab["td_fhook"])
    assert r.peek("td_mid", 4) == r.images[TD_ORG][3:7] != bytes(4)
    assert r.peek("td_chain", 2) == struct.pack("<H", r.lab["chained"])
    assert r.peek("td_slot", 3)[2] == 0xFF


def test_drive_id_probe_sequence(build):
    """The ROM's probe (PROGRESS.md §1): motor latched on at the /SEL0 fall,
    then 32 /SEL0 toggles with /MTR high read /RDY high."""
    r = rig(build)
    r.call("td_arm")
    assert prb(r, 0x7F) == 0xFF                            # not selected
    p = prb(r, 0x77)                                       # select, /MTR low: on
    assert p & 0x3C == 0x00                                # RDY low, TK0 low, WPRO low, CHNG low
    prb(r, 0x7F)
    prb(r, 0xFF)
    for _ in range(32):
        assert prb(r, 0xF7) & 0x3C == 0x20                 # motor off: /RDY high
        assert prb(r, 0xFF) & 0x3C == 0x3C


def test_step_tk0_and_change(build):
    r = rig(build)
    r.call("td_arm")
    prb(r, 0x77)                                           # select, motor on
    p = prb(r, 0x77 & ~0x01)                               # /STEP low
    assert p & 0x14 == 0                                   # no step yet: CHNG low, TK0 low
    p = prb(r, 0x77)                                       # rise, DIR=1 (out) at cyl 0
    assert p & 0x04 and not p & 0x10                       # disk seen; still cylinder 0
    prb(r, 0x75 & ~0x01)                                   # DIR 0 (in), /STEP low
    p = prb(r, 0x75)                                       # rise: cylinder 1
    assert p & 0x10 and r.b("td_cyl") == 1
    assert p & 0x08 == 0                                   # write-protected always


@pytest.mark.parametrize("seed", range(6))
def test_lines_equal_the_boot_models_floppy(build, seed):
    rnd = random.Random(seed)
    r = rig(build)
    r.call("td_arm")
    fl = Floppy(adf=object())
    ddrb = 0xFF
    for i in range(600):
        if rnd.random() < 0.05:
            ddrb = rnd.choice([0xFF, 0xFF, 0x00, rnd.randrange(256)])
        v = rnd.randrange(256)
        if rnd.random() < 0.6:                             # mostly DF0 traffic
            v = (v & ~0x78) | 0x70 | rnd.choice([0, 8])
        got = prb(r, v, ddrb)
        fl.write_prb((v & ddrb) | (~ddrb & 0xFF))
        want = 0xC3 & 0xEB | fl.pra_bits()
        assert got == want, f"step {i}: PRB ${v:02X} DDRB ${ddrb:02X}: ${got:02X} != ${want:02X}"
        assert r.b("td_cyl") == fl.cyl


def test_pins_keep_bits_0_1_6_7(build):
    r = rig(build)
    r.call("td_arm")
    r.bus.ram[r.lab["cia_st"] + CIA_PIN] = 0x00 | 0xC3 & 0x41
    prb(r, 0x77)
    assert pins(r) & 0xC3 == 0x41


# =============================================================================
# Locating the device and patching BeginIO
# =============================================================================

def make_exec(r: Rig, names=("timer.device", "input.device", "trackdisk.device",
                             "console.device"), base=DEVBASE, jmp=0x4EF9):
    r.g_l(4, SYSBASE)
    lh = SYSBASE + 350
    nodes = []
    for i, nm in enumerate(names):
        nb = base if nm == "trackdisk.device" else 0xC10000 + 0x200 * i
        nodes.append(nb)
        r.g_w(0xC20000 + 0x40 * i, nm.encode() + b"\0")
        r.g_l(nb + 10, 0xC20000 + 0x40 * i)
    for i, nb in enumerate(nodes):
        r.g_l(nb, nodes[i + 1] if i + 1 < len(nodes) else lh + 4)
    r.g_l(lh, nodes[0] if nodes else lh + 4)
    r.g_l(lh + 4, 0)
    r.g_w(base - 30, struct.pack(">HI", jmp, ORIG))
    r.g_w(base - 24, struct.pack(">HI", 0x4EF9, 0xFB7000))  # AbortIO


def armed(build, names=None) -> Rig:
    r = rig(build)
    r.poke("mach_fhook", struct.pack("<H", r.lab["chained"]))
    r.call("td_arm")
    make_exec(r, **({} if names is None else {"names": names}))
    r.call("td_fcall")
    return r


def test_fhook_patches_beginio(build):
    r = armed(build)
    assert r.n("chained") == 1
    assert r.g_r(DEVBASE - 30, 6) == bytes.fromhex("13C000BFD100")
    assert r.g_r(DEVBASE - 24, 6) == bytes.fromhex("4EF900FB7000")   # AbortIO untouched
    assert int.from_bytes(r.peek("td_slot", 3), "little") == DEVBASE - 30
    assert int.from_bytes(r.peek("td_orig", 3), "little") == ORIG
    writes = [c for c in r.calls if c[0].startswith("mem_write")]
    assert [c[0] for c in writes] == ["mem_write32", "mem_write16"]   # W12-C write check
    r.calls.clear()
    r.call("td_fcall")                                     # patched: only the chain
    assert r.calls == [("chained",)]


def test_fhook_without_device_retries(build):
    r = armed(build, names=("timer.device", "input.device"))
    assert r.peek("td_slot", 3)[2] == 0xFF
    make_exec(r)
    r.call("td_fcall")
    assert int.from_bytes(r.peek("td_slot", 3), "little") == DEVBASE - 30


def test_fhook_prefix_name_is_not_trackdisk(build):
    r = armed(build, names=("trackdisk.devic", "trackdisk.device2"))
    assert r.peek("td_slot", 3)[2] == 0xFF


def test_fhook_refuses_a_non_jmp_slot(build):
    r = rig(build)
    r.call("td_arm")
    make_exec(r, jmp=0x4E75)
    r.call("td_fcall")
    assert r.peek("td_slot", 3)[2] == 0xFF and r.n("mem_write16") == 0


def test_fhook_bounded_walk_on_a_cycle(build):
    r = rig(build)
    r.call("td_arm")
    r.g_l(4, SYSBASE)
    r.g_l(SYSBASE + 350, 0xC10000)
    r.g_l(0xC10000, 0xC10000)                              # a node that points at itself
    r.g_l(0xC1000A, 0xC20000)
    r.g_w(0xC20000, b"x\0")
    r.call("td_fcall", limit=500_000)
    assert r.n("mem_read8") <= 32


# =============================================================================
# BeginIO
# =============================================================================

def adf_pattern():
    return bytes((i * 7 + (i >> 9) * 13) & 0xFF for i in range(ADF_SIZE))


def request(r: Rig, cmd=2, flags=1, off=0, length=512, data=0x3000):
    r.g_w(IO, bytes(48))
    r.g_w(IO + 28, struct.pack(">HBB", cmd, flags, 0x55))
    r.g_l(IO + 32, 0xDEADBEEF)
    r.g_l(IO + 36, length)
    r.g_l(IO + 40, data)
    r.g_l(IO + 44, off)
    r.zp("r_a1", IO)
    r.zp("r_a7", SP0)
    r.g_l(SP0, RET)
    r.zp("r_pc", DEVBASE - 24)                             # past the 6-byte slot


def trap(r: Rig):
    r.zp("st_ipc", DEVBASE - 30)
    r.calls.clear()
    c = r.call("td_call", a=0x5A, x=CIA_SZ)
    assert c.c == 1 and c.x == CIA_SZ
    return c


@pytest.fixture
def served(build):
    r = armed(build)
    r.g_w(TD_ADF, adf_pattern())
    return r


def returned(r: Rig):
    assert r.zp("r_pc") == RET and r.zp("r_a7") == SP0 + 4


@pytest.mark.parametrize("cmd", [2, 0x8002])
@pytest.mark.parametrize("off,length,data", [
    (0, 512, 0x3000), (512 * 879, 1024, 0xC1A0A2), (512 * 1759, 512, 0x1FFE00),
    (0, 0x10000, 0x40000), (1024, 1536, 0x3ab8), (0, 1, 0x5FF), (512, 257, 0xC23FFF),
    (ADF_SIZE - 512, 512, 0x100)])
def test_quick_read_served(served, cmd, off, length, data):
    r = served
    adf = adf_pattern()
    request(r, cmd=cmd, off=off, length=length, data=data)
    trap(r)
    returned(r)
    assert r.g_r(data, length) == adf[off:off + length]
    assert r.g_r(data - 1, 1) == b"\0" and r.g_r(data + length, 1) == b"\0"
    assert r.g_r(IO + 31, 1) == b"\0" and r.g_rl(IO + 32) == length
    assert r.g_r(IO + 28, 3) == struct.pack(">HB", cmd, 1)   # command, flags kept
    pages = {(data + i) >> 8 for i in range(0, length)} | {(data + length - 1) >> 8}
    assert {c[1] >> 8 for c in r.calls if c[0] == "code_invpg"} == pages
    assert r.n("mem_rec_dirty") == 1
    assert struct.unpack("<H", r.peek("td_nrd", 2))[0] == 1
    assert r.bus.rec[0x0A] == 0
    for kind, c64, reu, n in r.bus.dmas:
        assert c64 == TD_BUF and 1 <= n <= 256
        assert (reu >> 16) not in (0xBF, 0xDF)


def test_zero_length_read(served):
    r = served
    request(r, length=0)
    trap(r)
    returned(r)
    assert r.g_rl(IO + 32) == 0 and r.g_r(IO + 31, 1) == b"\0" and r.bus.dmas == []


@pytest.mark.parametrize("off,length", [(ADF_SIZE - 512, 1024), (ADF_SIZE, 512),
                                        (0x1000000, 512), (0, 0x1000000),
                                        (0xFFFFFE00, 512), (0xE00000, 512)])
def test_read_past_the_image_refused(served, off, length):
    r = served
    request(r, off=off, length=length)
    trap(r)
    returned(r)
    assert r.g_r(IO + 31, 1) == bytes([TDERR_NS]) and r.g_rl(IO + 32) == 0
    assert r.bus.dmas == [] and r.g_r(0x3000, 16) == bytes(16)


@pytest.mark.parametrize("bank", [0xBF, 0xDF])
def test_read_into_the_io_banks_stops(served, bank):
    r = served
    dst = ((bank - 1) << 16) | 0xFF00
    request(r, length=1024, data=dst)
    trap(r)
    returned(r)
    assert r.g_r(IO + 31, 1) == bytes([TDERR_NS]) and r.g_rl(IO + 32) == 256
    assert r.g_r(dst, 256) == adf_pattern()[:256]
    assert r.g_r(bank << 16, 768) == bytes(768)
    assert r.n("mem_rec_dirty") == 1


@pytest.mark.parametrize("cmd", [3, 11, 17, 0x8003, 0x800B])
def test_quick_write_is_write_protected(served, cmd):
    r = served
    request(r, cmd=cmd, length=512)
    before = r.g_r(TD_ADF, 4096)
    trap(r)
    returned(r)
    assert r.g_r(IO + 31, 1) == bytes([TDERR_WP]) and r.g_rl(IO + 32) == 0
    assert r.g_r(TD_ADF, 4096) == before and r.bus.dmas == []


@pytest.mark.parametrize("cmd,flags", [(2, 0), (0x8002, 0), (3, 0), (9, 1), (14, 1), (15, 1),
                                       (20, 0), (22, 1), (16, 1), (0x4002, 1)])
def test_others_go_to_the_native_beginio(served, cmd, flags):
    r = served
    request(r, cmd=cmd, flags=flags)
    trap(r)
    assert r.zp("r_pc") == ORIG and r.zp("r_a7") == SP0
    assert r.g_r(IO + 28, 8) == struct.pack(">HBBI", cmd, flags, 0x55, 0xDEADBEEF)
    assert r.b("td_nqr") == (1 if cmd & 0x7FFF == 2 and not flags and cmd & 0x7F00 == 0 else 0)
    assert not [c for c in r.calls if c[0].startswith("mem_write")]
    assert r.b("td_npas") == 1


def test_other_pcs_are_drive_writes(served):
    r = served
    request(r)
    r.zp("st_ipc", DEVBASE - 28)
    c = r.call("td_call", a=0x77, x=CIA_SZ)
    assert c.c == 0 and r.zp("r_pc") == DEVBASE - 24 and r.g_rl(IO + 32) == 0xDEADBEEF


def test_watch_counts_reads_of_a_block(served):
    r = served
    blk = 408
    a = TD_ADF + blk * 512
    r.poke("td_wa", bytes([(a >> 8) & 0xFE, a >> 16]))
    request(r, off=0, length=1024)
    trap(r)
    assert r.b("td_whit") == 0
    request(r, off=407 * 512, length=1536, data=0x3080)
    trap(r)
    assert r.b("td_whit") >= 1
    request(r, off=409 * 512, length=512)
    n = r.b("td_whit")
    trap(r)
    assert r.b("td_whit") == n


def test_other_module_id_refuses(served):
    """A resume onto a different module (td_mid != the staged ID): the PRB
    hook calls nothing, the frame hook halts with TD_HALT_MOD."""
    r = served
    r.bus.ram[TD_ORG + 5] ^= 0x40
    request(r)
    r.zp("st_ipc", DEVBASE - 30)
    pins0 = pins(r)
    c = r.call("td_call", a=0x77, x=CIA_SZ)
    assert c.c == 0 and c.x == CIA_SZ and pins(r) == pins0
    assert r.g_rl(IO + 32) == 0xDEADBEEF and r.zp("r_pc") == DEVBASE - 24
    r.calls.clear()
    r.bus.ram[0x78:0x7A] = b"\0\0"
    r.call("td_fcall")
    assert r.bus.ram[0x78] == 1 and r.bus.ram[0x79] == r.lab["TD_HALT_MOD"]
    assert r.calls == []                                   # not even the chained hook


def test_trap_before_patch_never_matches(build):
    r = rig(build)
    r.call("td_arm")
    r.zp("st_ipc", 0xFFFFFF)
    c = r.call("td_call", a=0x77, x=CIA_SZ)
    assert c.c == 0


# =============================================================================
# Teeth: each mutant (one build each) fails at least one check above
# =============================================================================

MUTANTS = {   # name: (original, replacement) in tests/aros/td.s (the module)
    "nohle":   ("        cmp #$F9\n", "        cmp #$00\n"),      # BeginIO never patched
    "offset":  ("        adc #^TD_ADF\n", "        adc #^TD_ADF\n        inc t_op1+1\n        inc t_op1+1\n"),
    "wpok":    ("wp:\n        lda #TDERR_WP\n", "wp:\n        lda #0\n"),
    "nochg":   ("        sty td_chg\n", "        bit td_chg\n"),
    "noinv":   ("        jsr code_invpg\n", "        bit code_invpg\n"),
    "noguard": ("        cmp #$BF\n        beq refuse\n        cmp #$DF\n        beq refuse\n", ""),
    "nopop":   ("        adc #4\n", "        adc #0\n"),
}


def battery(build) -> list[str]:
    """Every check of this file once (fixed parameters); the names of those
    that fail."""
    checks = [
        (test_arm_deselected_pins_and_chain, {}),
        (test_drive_id_probe_sequence, {}),
        (test_step_tk0_and_change, {}),
        (test_lines_equal_the_boot_models_floppy, {"seed": 0}),
        (test_fhook_patches_beginio, {}),
    ]
    served_checks = [
        (test_others_go_to_the_native_beginio, {"cmd": 9, "flags": 1}),
        (test_quick_read_served, {"cmd": 2, "off": 512, "length": 257, "data": 0xC23FFF}),
        (test_quick_read_served, {"cmd": 2, "off": 512 * 879, "length": 1024, "data": 0xC1A0A2}),
        (test_read_past_the_image_refused, {"off": ADF_SIZE - 512, "length": 1024}),
        (test_read_into_the_io_banks_stops, {"bank": 0xBF}),
        (test_quick_write_is_write_protected, {"cmd": 3}),
        (test_watch_counts_reads_of_a_block, {}),
    ]
    bad = []
    for fn, kw in checks + served_checks:
        try:
            if (fn, kw) in served_checks:
                r = armed(build)
                r.g_w(TD_ADF, adf_pattern())
                fn(r, **kw)
            else:
                fn(build, **kw)
        except (AssertionError, KeyError, IndexError) as e:
            bad.append(f"{fn.__name__}{kw}: {str(e)[:60]}")
    return bad


def test_battery_passes_on_the_real_code(build):
    assert battery(build) == []


def test_mutant_nomid_is_killed(tmp_path):
    """MAIN: td_call/td_fcall without the module-ID check."""
    src = (ROOT / "runtime/ops/td.s").read_text()
    old = "        cmp td_mid,y\n        bne :+\n"
    assert src.count(old) == 1
    b = build_td(tmp_path, main_src=src.replace(old, "        cmp td_mid,y\n        nop\n        nop\n"))
    r = armed(b)
    r.g_w(TD_ADF, adf_pattern())
    with pytest.raises(AssertionError):
        test_other_module_id_refuses(r)


@pytest.mark.parametrize("name", sorted(MUTANTS))
def test_mutant_is_killed(tmp_path, name):
    old, new = MUTANTS[name]
    src = (ROOT / "tests/aros/td.s").read_text()
    assert src.count(old) == 1, f"mutant {name}: anchor not unique"
    bad = battery(build_td(tmp_path, src.replace(old, new)))
    assert bad, f"mutant {name} survived"
