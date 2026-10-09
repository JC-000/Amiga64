"""W13-I host tests: host/inp_ref (the joystick -> mouse rule and the CIA1
scan model) and its parity with runtime/ops/inp.s's equates."""

from __future__ import annotations

import os
import re

import pytest

from host import inp_ref
from host.inp_ref import CBM, CAP, DOWN, F1, FIRE, LEFT, NMAX, RIGHT, UP, Inp, Matrix, scan

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def equates(rel: str) -> dict[str, str]:
    out = {}
    for line in open(os.path.join(ROOT, rel)):
        m = re.match(r"([A-Z][A-Z0-9_]*)\s*=\s*([^;]+?)\s*;", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def num(v: str) -> int:
    v = v.strip()
    return int(v[1:], 16) if v.startswith("$") else int(v)


# --- parity with the runtime -------------------------------------------------

def test_equates_match_runtime():
    e = equates("runtime/ops/inp.s")
    assert num(e["INP_CAP"]) == CAP
    assert e["INP_NMAX"].replace(" ", "") == "2*INP_CAP-2" and NMAX == 2 * CAP - 2
    assert num(e["INP_F1"]) == F1 and num(e["INP_CBM"]) == CBM
    assert num(e["INP_JOY0"]) == 0x0A                     # JOY0DAT ($DFF00A)
    assert num(e["INP_PIN"]) == num(equates("runtime/ops/cia.s")["CIA_PIN"])


def test_zp_scratch_claimed():
    zp = open(os.path.join(ROOT, "runtime/rc/zp.inc")).read()
    assert re.search(r"@region inp \$9A-\$9B", zp)
    assert re.search(r"inp_t\s*=\s*\$9A", zp) and re.search(r"inp_u\s*=\s*\$9B", zp)


def test_single_call_site():
    mach = open(os.path.join(ROOT, "runtime/ops/mach.s")).read()
    assert len(re.findall(r"^\s+jsr inp_frame\b.*W13-I", mach, re.M)) == 1
    for name in os.listdir(os.path.join(ROOT, "runtime/ops")):
        if name.endswith(".s") and name not in ("inp.s", "mach.s"):
            assert "inp_frame" not in open(os.path.join(ROOT, "runtime/ops", name)).read()


# --- the frame rule ------------------------------------------------------------

def test_step_sequence_and_cap():
    assert [inp_ref.step(n) for n in range(6)] == [1, 1, 2, 2, 3, 3]
    assert inp_ref.step(NMAX) == CAP and inp_ref.step(NMAX + 100) == CAP
    m = Inp()
    xs = []
    for _ in range(60):
        m.frame(RIGHT, 0)
        xs.append(m.x)
    deltas = [(b - a) & 0xFF for a, b in zip([0] + xs, xs)]
    assert deltas[:6] == [1, 1, 2, 2, 3, 3]
    assert max(deltas) == CAP and deltas[-1] == CAP and m.n == NMAX


def test_counter_wrap_both_ways():
    m = Inp()
    for _ in range(3):
        m.frame(UP | LEFT, 0)
    assert (m.y, m.x) == (0xFC, 0xFC)                     # 1 + 1 + 2 below 0
    m = Inp(y=0xFE, x=0xFF)
    m.frame(DOWN | RIGHT, 0)
    assert (m.y, m.x) == (0xFF, 0x00)


def test_centre_resets_direction_change_does_not():
    m = Inp()
    for _ in range(10):
        m.frame(RIGHT, 0)
    assert m.n == 10
    m.frame(RIGHT | UP, 0)                                # diagonal: keeps speed
    assert m.n == 11
    m.frame(0, 0)
    assert m.n == 1                                       # 0 -> 1 like inp.s
    y0 = m.y
    m.frame(UP, 0)
    assert (y0 - m.y) & 0xFF == 1


def test_opposite_bits_cancel():
    m = Inp(y=5, x=5)
    m.frame(UP | DOWN | LEFT | RIGHT, 0)
    assert (m.y, m.x) == (5, 5)


def test_buttons():
    m = Inp()
    o = m.frame(FIRE | RIGHT, CBM)                        # both + move (menus)
    assert o["pra"] == 0xAB and o["potinp"] == 0xFB00 and m.x == 1
    o = m.frame(0, 0)
    assert o["pra"] == 0xEB and o["potinp"] == 0xFF00
    m = Inp(pin=0x2A)                                     # pins 0-5 kept, /FIR1 high
    assert m.frame(0, 0)["pra"] == 0xEA
    assert m.frame(FIRE, 0)["pra"] == 0xAA


def test_f1_is_an_edge():
    m = Inp()
    m.frame(0, F1)
    assert m.view == 1
    m.view = 0
    m.frame(0, F1)                                        # still held: no new edge
    assert m.view == 0
    m.frame(0, 0)
    m.frame(0, F1 | CBM)
    assert m.view == 1
    m.view = 0
    m.frame(0, CBM)                                       # C= alone: no view edge
    assert m.view == 0


# --- the scan model: port 2 and the keyboard apart --------------------------

def test_scan_stick_alone():
    for v in range(32):
        assert scan(Matrix(joy2=v), 0) == (v, 0)


def test_scan_keys_alone():
    assert scan(Matrix(keys={(0, 4)}), 0) == (0, F1)
    assert scan(Matrix(keys={(7, 5)}), 0) == (0, CBM)
    assert scan(Matrix(keys={(0, 4), (7, 5)}), 0) == (0, F1 | CBM)
    assert scan(Matrix(keys={(1, 4), (7, 6), (0, 0)}), 0) == (0, 0)   # Z, Q, DEL


@pytest.mark.parametrize("joy2", [UP, DOWN, LEFT, RIGHT, FIRE, UP | RIGHT | FIRE, 31])
def test_scan_key_held_while_stick_moves(joy2):
    """A key never reaches the stick read; C= reads correctly with any stick."""
    joy, key = scan(Matrix(keys={(7, 5)}, joy2=joy2), 0)
    assert joy == joy2 and key == CBM
    # C= released while moving: read as up (row 5 idle unless S/F/H/K/F3)
    assert scan(Matrix(joy2=joy2), CBM) == (joy2, 0)


def test_scan_f1_ghost_with_stick_up_keeps_state():
    """Stick up pulls PA0 = column 0: F1's row 4 reads low at idle, so F1
    is unknown and keeps its last state (no phantom, no lost release)."""
    assert scan(Matrix(keys={(0, 4)}, joy2=UP), 0) == (UP, 0)
    assert scan(Matrix(keys={(0, 4)}, joy2=UP), F1) == (UP, F1)
    assert scan(Matrix(joy2=UP), F1) == (UP, 0)           # row 4 readable: up


def test_scan_ghost_letter_with_stick_is_not_a_key():
    """Z (column 1, row 4) held while the stick pulls column 1 (DOWN): the
    col-0 read sees row 4 low through the stick, but idle does too: F1
    stays as it was, never a phantom press."""
    assert scan(Matrix(keys={(1, 4)}, joy2=DOWN), 0) == (DOWN, 0)
    assert scan(Matrix(keys={(3, 5)}, joy2=RIGHT), 0) == (RIGHT, 0)   # H + col 3


def test_scan_port1_rows_never_reach_stick_or_keys():
    for v in range(32):
        joy, key = scan(Matrix(joy1=v), 0)
        assert joy == 0 and key == 0
    assert scan(Matrix(joy1=FIRE), F1)[1] == F1           # row 4 unknown: kept
