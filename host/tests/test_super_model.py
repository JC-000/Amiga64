"""Independent Python reference model of the W6-S system ops.

Validated (build/evidence/w6-super/model/) against EVERY non-address-error
case of the pinned upstream files for ANDI/ORI/EORI to SR, MOVE to/from SR,
MOVE to/from USP, RTE, TRAP, TRAPV, CHK, RESET and DIVU/0 (83,435 cases).
Here it re-checks the vendored super fixtures and -- the part the upstream
data cannot pin -- the hand-authored SUPERHAND expectations, so a slip in
either the host-side builder or the fixtures fails without VICE.
"""

from __future__ import annotations

import pytest

from host import vectors as V

M = 0xFFFFFF
L = 0xFFFFFFFF


class AddrErr(Exception):
    pass


class CPU:
    def __init__(self, n):
        self.d = list(n["regs"][:8])
        self.a = list(n["regs"][8:])
        self.sr = n["sr"]
        self.osp = n["osp"]
        self.pc = self.ipc = n["pc"]
        self.mem = dict(n["init_mem"])

    def usp(self):
        return self.osp if self.sr & 0x2000 else self.a[7]

    def r8(self, a): return self.mem.get(a & M, 0)
    def r16(self, a):
        if a & 1:
            raise AddrErr
        return self.r8(a) << 8 | self.r8(a + 1)
    def r32(self, a): return self.r16(a) << 16 | self.r16(a + 2)
    def w8(self, a, v): self.mem[a & M] = v & 255
    def w16(self, a, v): self.w8(a, v >> 8); self.w8(a + 1, v)
    def w32(self, a, v): self.w16(a, v >> 16); self.w16(a + 2, v)

    def fetch(self):
        v = self.r16(self.pc)
        self.pc = (self.pc + 2) & L
        return v

    def swap(self):
        self.a[7], self.osp = self.osp, self.a[7]

    def set_sr(self, v):
        v &= 0xA71F
        s_off = (self.sr & 0x2000) and not (v & 0x2000)
        self.sr = v
        if s_off:
            self.swap()

    def exc(self, vec, pc):
        old = self.sr
        if not old & 0x2000:
            self.swap()
        self.sr = (old | 0x2000) & 0x7FFF
        self.a[7] = (self.a[7] - 6) & L
        self.w16(self.a[7], old)
        self.w32(self.a[7] + 2, pc)
        self.pc = self.r32(vec * 4)
        if self.pc & 1:
            raise AddrErr

    def ea(self, f, n):
        mode, reg = f >> 3, f & 7
        s16 = lambda x: x - 0x10000 if x & 0x8000 else x
        if mode == 0: return ("d", reg)
        if mode == 1: return ("a", reg)
        if mode == 2: return ("m", self.a[reg])
        if mode == 3:
            a = self.a[reg]
            self.a[reg] = (a + (2 if reg == 7 and n == 1 else n)) & L
            return ("m", a)
        if mode == 4:
            self.a[reg] = (self.a[reg] - (2 if reg == 7 and n == 1 else n)) & L
            return ("m", self.a[reg])
        if mode == 5: return ("m", (self.a[reg] + s16(self.fetch())) & L)
        if mode == 6: return ("m", self.brief(self.a[reg]))
        if reg == 0: return ("m", s16(self.fetch()) & L)
        if reg == 1: return ("m", self.fetch() << 16 | self.fetch())
        if reg == 2:
            b = self.pc
            return ("m", (b + s16(self.fetch())) & L)
        if reg == 3: return ("m", self.brief(self.pc))
        if reg == 4:
            a = self.pc
            self.pc += 4 if n == 4 else 2
            return ("m", a + (1 if n == 1 else 0))
        raise ValueError("bad EA")

    def brief(self, base):
        w = self.fetch()
        d8 = (w & 255) - (256 if w & 128 else 0)
        r = w >> 12
        x = self.d[r] if r < 8 else self.a[r - 8]
        if not w & 0x800:
            x &= 0xFFFF
            x -= 0x10000 if x & 0x8000 else 0
        return (base + d8 + x) & L

    def load(self, e, n):
        k, v = e
        if k == "d": return self.d[v] & ((1 << 8 * n) - 1)
        if k == "a": return self.a[v] & ((1 << 8 * n) - 1)
        return {1: self.r8, 2: self.r16, 4: self.r32}[n](v)

    def step(self):
        """Execute one instruction; returns 'stop', or raises AddrErr."""
        op = self.fetch()
        S = self.sr & 0x2000
        priv = lambda: self.exc(8, self.ipc)
        if op == 0xAFFF:
            return "halt-ok"
        if op & 0xF138 == 0x0108 or (op & 0xFFC0 == 0x4AC0 and op != 0x4AFC):
            return "unimplemented"              # MOVEP / TAS: halt ILLEGAL
        if op >> 12 == 0xA:
            return self.exc(10, self.ipc)
        if op >> 12 == 0xF:
            return self.exc(11, self.ipc)
        if op in (0x4AFC, 0x4E74, 0x4E00) or op & 0xFFF8 == 0x4E78 \
                or op == 0x4A7D or op & 0xFFF8 == 0x46C8 \
                or op & 0xF1F8 == 0x4188:
            return self.exc(4, self.ipc)
        if op in (0x007C, 0x027C, 0x0A7C):
            if not S: return priv()
            imm = self.fetch()
            self.set_sr({0x007C: self.sr | imm, 0x027C: self.sr & imm,
                         0x0A7C: self.sr ^ imm}[op])
            return None
        if op & 0xFFC0 == 0x46C0:
            if not S: return priv()
            return self.set_sr(self.load(self.ea(op & 63, 2), 2))
        if op & 0xFFC0 == 0x40C0:
            e = self.ea(op & 63, 2)
            if e[0] == "d":
                self.d[e[1]] = (self.d[e[1]] & 0xFFFF0000) | self.sr
            else:
                self.r16(e[1])
                self.w16(e[1], self.sr)
            return None
        if op & 0xFFF0 == 0x4E60:
            if not S: return priv()
            if op & 8: self.a[op & 7] = self.osp
            else: self.osp = self.a[op & 7]
            return None
        if op == 0x4E73:
            if not S: return priv()
            sp = self.a[7]
            sr, pc = self.r16(sp), self.r32(sp + 2)
            self.a[7] = (sp + 6) & L
            self.set_sr(sr)
            self.pc = pc & M
            if pc & 1:
                raise AddrErr
            return None
        if op & 0xFFF0 == 0x4E40:
            return self.exc(32 + (op & 15), self.pc)
        if op == 0x4E76:
            return self.exc(7, self.pc) if self.sr & 2 else None
        if op == 0x4E70:
            return priv() if not S else None
        if op == 0x4E72:
            if not S: return priv()
            self.set_sr(self.fetch())
            return "stop"
        if op & 0xF1C0 == 0x4180:
            bound = self.load(self.ea(op & 63, 2), 2)
            s = lambda x: x - 0x10000 if x & 0x8000 else x
            dn = s(self.d[op >> 9 & 7] & 0xFFFF)
            if dn < 0:
                self.sr = (self.sr & ~0x0F) | 0x08
                return self.exc(6, self.pc)
            if dn > s(bound):
                self.sr &= ~0x0F
                return self.exc(6, self.pc)
            self.sr &= ~0x07
            return None
        if op & 0xF100 == 0x7000:                       # MOVEQ (program filler)
            v = op & 0xFF
            v = (v - 0x100 if v & 0x80 else v) & L
            self.d[op >> 9 & 7] = v
            self.sr = (self.sr & ~0x0F) | (0x08 if v & 0x80000000 else 0) | \
                (0x04 if v == 0 else 0)
            return None
        if op & 0xF1C0 in (0x2000, 0x3000):             # MOVE.L/.W <ea>,Dn
            n = 4 if op & 0xF000 == 0x2000 else 2
            v = self.load(self.ea(op & 63, n), n)
            r = op >> 9 & 7
            self.d[r] = v if n == 4 else (self.d[r] & 0xFFFF0000) | v
            top = 1 << (8 * n - 1)
            self.sr = (self.sr & ~0x0F) | (0x08 if v & top else 0) | \
                (0x04 if v == 0 else 0)
            return None
        if op & 0xF0C0 == 0x80C0:
            if self.load(self.ea(op & 63, 2), 2) == 0:
                self.sr &= ~0x0F
                return self.exc(5, self.ipc)
        raise NotImplementedError(f"{op:04X}")

    def check(self, n):
        diffs = []
        exp = n["exp_regs"]
        if self.d + self.a != exp: diffs.append("regs")
        if self.pc & M != n["exp_pc"]: diffs.append("pc")
        if self.sr != n["exp_sr"]: diffs.append("sr")
        if self.osp != n["exp_osp"]: diffs.append("osp")
        for a, v in n["exp_mem"]:
            if self.r8(a) != v: diffs.append(f"mem{a:06X}")
        return diffs


def run_model(n):
    c = CPU(n)
    try:
        r = c.step()
        halt = {"stop": (1, V.HALT_STOP),
                "halt-ok": (1, V.HALT_OK),
                "unimplemented": (1, V.HALT_ILLEGAL)}.get(r, (0, 0))
    except AddrErr:
        halt = (1, V.HALT_ADDRESS)
    return c, halt


FIXTURE_NORMS = [(stem, i, V.normalize_super(case))
                 for stem in V.SUPER_FILES
                 for i, case in enumerate(V.load_fixture(stem))]


@pytest.mark.parametrize("stem", V.SUPER_FILES)
def test_model_matches_vendored_fixture(stem: str) -> None:
    bad = []
    for s, i, n in FIXTURE_NORMS:
        if s != stem:
            continue
        c, halt = run_model(n)
        d = c.check(n) + (["halt"] if halt != (0, 0) else [])
        if d:
            bad.append((i, d))
    assert not bad


@pytest.mark.parametrize("n", [c for c in V.super_hand_cases()
                               if c["flags"] & V.FLAG_EXC],
                         ids=lambda n: n["name"])
def test_model_matches_hand_case(n) -> None:
    c, halt = run_model(n)
    assert halt == (n["exp_halt"], n["exp_reason"])
    assert c.check(n) == []


@pytest.mark.parametrize("prog", V.super_run_programs(), ids=lambda p: p["name"])
def test_model_matches_run_program(prog) -> None:
    # multi-instruction programs (review F1): step the model from user mode
    # until the $AFFF halt; registers start at 0 like test_run_program's
    n = {"regs": [0] * 15 + [V.RUN_USP], "sr": 0x0000, "osp": V.RUN_SSP,
         "pc": prog["pc"], "init_mem": list(prog["mem"].items())}
    c = CPU(n)
    for count in range(1, 100):
        c.ipc = c.pc
        if c.step() == "halt-ok":
            break
    else:
        pytest.fail("no $AFFF halt within 100 instructions")
    assert count == prog["icount"]
    exp = [prog["exp"].get(f"d{i}", 0) for i in range(8)] + \
          [prog["exp"].get(f"a{i}", 0) for i in range(7)] + [prog["exp"]["a7"]]
    assert c.d + c.a == exp
    assert (c.pc & M, c.sr, c.osp) == (prog["exp_pc"], prog["exp_sr"],
                                       prog["exp_osp"])
    for a, v in prog["exp_mem"].items():
        assert c.r8(a) == v
