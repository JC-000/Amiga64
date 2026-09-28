"""W6-K keyboard check: fixtures/c/kbd_echo.c vs its host mirror, and the
pure parts of tests/kbd_echo.py (no emulator, no device).

kbd_echo.c decodes every delivered character back to a matrix position
with its own copy of the key map, and judges each step against its own
table of expected lines.  tests/kbd_echo.py mirrors both (it checks the
screen rows the program prints, and its VICE scripts must type exactly
those lines).  If any of the three drift apart, a hardware session would
report a false FAIL or a false PASS; this pins them without VICE.
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from tests import kbd_echo as ke
from tests import run_con

SRC = Path(__file__).resolve().parents[2] / "fixtures" / "c" / "kbd_echo.c"


def _c_bytes(text: str, name: str) -> list[int]:
    """Values of `static const char NAME[64] = { ... };` (ints and char
    literals)."""
    m = re.search(rf"\b{name}\[64\]\s*=\s*\{{(.*?)\}};", text, re.S)
    assert m, f"{name}[64] not found in {SRC.name}"
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    out = []
    for tok in re.findall(r"'(?:\\.|[^'])'|\d+", body):
        if tok.startswith("'"):
            lit = tok[1:-1]
            out.append(ord(lit[1]) if lit.startswith("\\") else ord(lit))
        else:
            out.append(int(tok))
    return out


def _c_wants(text: str) -> list[str]:
    m = re.search(r"\bwant\[NSTEP\]\s*=\s*\{(.*?)\};", text, re.S)
    assert m, "want[NSTEP] not found"
    return [s.encode().decode("unicode_escape")
            for s in re.findall(r'"((?:\\.|[^"])*)"', m.group(1))]


def _mirror(table: list[str | None]) -> list[int]:
    return [0 if ch is None else ord(ch) for ch in table]


def test_c_key_map_is_hle_con_map():
    text = SRC.read_text()
    assert _c_bytes(text, "kplain") == _mirror(run_con.KEYMAP_PLAIN)
    assert _c_bytes(text, "kshift") == _mirror(run_con.KEYMAP_SHIFT)


def test_c_step_table_is_host_step_table():
    text = SRC.read_text()
    assert _c_wants(text) == [s.want for s in ke.STEPS]
    assert re.search(rf"#define NSTEP {len(ke.STEPS)}\b", text)


@pytest.mark.parametrize("ch,row", [
    ("q", " 'q' $71 c7r6"),
    ("^", " '^' $5e c6r6"),
    ("A", " 'A' $41 c1r2+SH"),
    ("?", " '?' $3f c6r7+SH"),
    ("0", " '0' $30 c4r3"),          # plain layer wins over SHIFT+0
    (" ", " ' ' $20 c7r4"),
    ("\x01", " '.' $01 c1r2+CTRL"),
    ("\x7f", " '.' $7f ??"),
])
def test_fmt_char(ch, row):
    assert ke.fmt_char(ch) == row
    assert len(row) < ke.COLS          # one screen row, no wrap


def test_step_rows_fit_and_verdicts():
    for n, s in enumerate(ke.STEPS, 1):
        for v in ("PASS", "FAIL", "SKIP"):
            rows = ke.step_rows(n, s.want, v)
            assert rows[0] == f"> {s.want}"
            assert all(len(r) < ke.COLS for r in rows)
    assert ke.step_rows(1, "q^", "FAIL")[-1] == 'STEP 1 FAIL, want "q"'
    assert ke.result_rows("PFSPP") == ["STEPS PFSPP",
                                       "RESULT pass=3 fail=1 skip=1"]


def _typed(script) -> str:
    """What hle_con.s would deliver for a VICE script: every key that goes
    down (not a modifier, not on a joystick-held row) decoded with the
    modifiers held at that moment (CTRL > SHIFT > plain; C= nothing)."""
    out, held = "", set()
    for keys, joy, n, _secs in script:
        keys = set(keys)
        new = keys - held - set(ke.MODS)
        held = keys
        if ke.visible(tuple(keys), joy) is None:
            assert n == 0
            continue
        assert len(new) == n or (n == 0 and not new)
        for idx in sorted(new):
            if run_con.CTRL in keys:
                out += run_con.ctrl_code(idx) or ""
            elif run_con.LSHIFT in keys or run_con.RSHIFT in keys:
                out += run_con.KEYMAP_SHIFT[idx] or ""
            else:
                out += run_con.KEYMAP_PLAIN[idx] or ""
    return out


def test_vice_scripts_type_the_expected_lines():
    for step, script in zip(ke.STEPS, ke.STEP_SCRIPTS, strict=True):
        assert _typed(script) == step.want + "\r"
    assert _typed(ke.FREE_SCRIPT) == "".join(l + "\r" for l in ke.FREE_LINES) \
        + "\r"
    # the phantom's shape: UP-ARROW (c6r6) decodes before Q (c7r6)
    assert _typed(ke.TEETH_SCRIPT) == "^q\r\r" + run_con.CTRL_D
    assert _typed(ke.MANNER_SCRIPT) == "q\r\rzip\rA?q\r" + run_con.CTRL_D


def test_joystick_step_uses_only_unmasked_rows():
    """A port-1 stick holds rows 0-4 low (up, down, left, right, fire):
    every direction must leave HOSE typeable, so the operator may hold the
    stick any way (RETURN, row 1, comes after the release)."""
    for ch in "hose":
        assert run_con.key_for(ch)[0] % 8 >= 5
    assert ke.visible((ke.K("x"),), ke.JOY2_UP) is None
    assert ke.visible((ke.K("h"),), 0) == ke.bits_mat([ke.K("h")])


def test_rollover_keys_form_no_ghost_rectangle():
    """Z, I, P: no three of them at three corners of a rectangle, so a
    ghost fourth key cannot appear on real hardware."""
    ks = [run_con.key_for(c)[0] for c in "zip"]
    for a in ks:
        for b in ks:
            for c in ks:
                if len({a, b, c}) == 3:
                    # a,b share a column and b,c share a row -> ghost
                    assert not (a // 8 == b // 8 and b % 8 == c % 8)


def test_bleed_suspect_and_events():
    q, up = ke.K("q"), ke.K("^")
    assert ke.bleed_suspect(up, {q, up}) == q
    assert ke.bleed_suspect(q, {q}) is None
    assert ke.bleed_suspect(ke.K("l"), {ke.K(";")}) == ke.K(";")
    ev = ke.key_events({q}, {q, up}, 1)
    assert ev == ["key DOWN c6r6 UPARROW   <- COLUMN BLEED? c7r6 Q is held"]
    assert ke.key_events({q}, set(), 0) == ["key UP   c7r6 Q"]
    assert ke.key_events(set(), {run_con.RSHIFT}, 0) == \
        ["mod DOWN c6r4 RSHIFT"]
    assert ke.key_events(set(), set(), 2)[-1].startswith("(+2 key event")


def test_mat_bits_roundtrip_and_joy_rows():
    idxs = {0, 9, 63, run_con.RSHIFT}
    assert ke.mat_bits(ke.bits_mat(idxs)) == idxs
    assert ke.joy_rows(0xFF) == []
    assert ke.joy_rows(0xEE) == [0, 4]


def test_new_rows_scroll_and_in_place():
    old = [f"r{i}" for i in range(ke.ROWS)]
    assert ke.new_rows(old, old) == []
    scrolled = old[2:] + ["x", "y"]
    assert ke.new_rows(old, scrolled) == ["x", "y"]
    edited = old[:]
    edited[24] = "r24 more"
    assert ke.new_rows(old, edited) == ["r24 more"]
    blank = [""] * ke.ROWS
    assert ke.new_rows(blank, ["a"] + [""] * 24) == ["a"]


def test_smoke_input_is_every_answer():
    assert ke.smoke_input() == b"q\r;/\rzip\rA?q\r" + b"hose\r" * 5 \
        + b"ok\r\r"
    assert len(ke.smoke_input()) < run_con.QUEUE_LEN


def test_parse_mhz():
    assert ke.parse_mhz("48,1") == [48, 1]
    assert ke.parse_mhz(None) == []
    with pytest.raises(ValueError):
        ke.parse_mhz("16")


def test_main_refuses_bad_arguments(tmp_path, monkeypatch):
    prg = tmp_path / "r.prg"
    prg.write_bytes(b"\x01\x08")
    (tmp_path / "r.lbl").write_text("")
    monkeypatch.delenv("U64_HOST", raising=False)
    assert ke.main(["--prg", str(prg), "--backend", "u64"]) == 2
    assert ke.main(["--prg", str(prg), "--backend", "u64", "--smoke"]) == 2
    assert ke.main(["--prg", str(prg), "--mhz", "48"]) == 2
    assert ke.main(["--prg", str(prg), "--backend", "u64", "--smoke",
                    "--mhz", "20"]) == 2
    assert ke.main(["--prg", str(prg), "--legs", "nope"]) == 2
    assert ke.main(["--prg", str(prg), "--runs", "fixed@20"]) == 2
    assert ke.main(["--prg", str(prg), "--runs", "broken@48"]) == 2
    monkeypatch.setenv("U64_HOST", "192.0.2.1")                # never reached
    # the manual mode takes --runs, not --mhz
    assert ke.main(["--prg", str(prg), "--backend", "u64", "--mhz", "48"]) == 2


def test_checklist_names_every_step_and_labels_controls():
    fixed = "\n".join(ke.checklist(ke.Run("fixed", 48)))
    for n, st in enumerate(ke.STEPS, 1):
        assert f"STEP {n}  {st.title}" in fixed
    assert "control port 2" in fixed.lower()
    for name, steps in ke.CONTROL_STEPS.items():
        text = "\n".join(ke.checklist(ke.Run(name, 48)))
        assert "SUPPOSED TO SHOW THE DEFECT" in text
        assert [n for n in range(1, ke.NSTEP + 1)
                if f"STEP {n}  " in text] == list(steps)


class _Labels:
    def __init__(self, d):
        self.d = d

    def address(self, name):
        return self.d.get(name.lstrip("."))


class _Sess:
    def __init__(self, d):
        self.labels = _Labels(d)


HLE_CON = Path(__file__).resolve().parents[2] / "runtime" / "ops" / "hle_con.s"


def test_bss_layout_matches_declarations():
    lay = ke.bss_layout(HLE_CON.read_text())
    assert lay["hle_con_magic"] == 0
    assert lay["hle_con_nkey"] == 7
    assert lay["hle_con_idle"] == lay["hle_con_stab"] + 1
    assert lay["hle_con_mat"] == lay["hle_con_smp"] + 8
    # hle_con_line is CON_LINE_MAX + 1 bytes, the last symbol
    assert lay["hle_con_line"] == lay["hle_con_q"] + 256


def test_con_label_derives_unexported_symbols_only_when_consistent():
    lay = ke.bss_layout(HLE_CON.read_text())
    base = 0x5D7E
    exported = {n: base + lay[n] for n in
                ("hle_con_magic", "hle_con_nkey", "hle_con_mat", "hle_con_q",
                 "hle_con_qh", "hle_con_qt", "hle_con_src", "hle_con_nread")}
    sess = _Sess(exported)
    assert ke.con_label(sess, "hle_con_mat") == exported["hle_con_mat"]
    assert ke.con_label(sess, "hle_con_idle") == base + lay["hle_con_idle"]
    # a layout that no longer reproduces an exported label: refuse
    bad = dict(exported, hle_con_mat=exported["hle_con_mat"] + 1)
    assert ke.con_label(_Sess(bad), "hle_con_idle") is None
    assert ke.con_label(_Sess({}), "hle_con_idle") is None


class _FakeLock:
    events: list = []

    def __init__(self, key, allow_nested=False):
        assert allow_nested            # u64_hold's manager must join it
        self.key = key

    def acquire_or_raise(self, timeout, **kw):
        _FakeLock.events.append("acquire")

    def release(self):
        _FakeLock.events.append("release")


def _fake_client(infos):
    class _Client:
        def __init__(self, host):
            self.host = host

        def get_info(self):
            _FakeLock.events.append("get_info")
            return infos.pop(0)

        def close(self):
            pass

        def __getattr__(self, name):   # anything else would touch the device
            raise AssertionError(f"firmware gate called client.{name}")
    return _Client


SPIKE = {"git_commit_hash": "4b02e67c", "core_version": "1.50"}
REST = {"git_commit_hash": ke.EXPECT_GIT, "core_version": ke.EXPECT_CORE}


def _gate(monkeypatch, infos, hours=1.0):
    import c64_test_harness as h
    _FakeLock.events = []
    monkeypatch.setattr(h, "DeviceLock", _FakeLock)
    monkeypatch.setattr(h, "Ultimate64Client", _fake_client(infos))
    monkeypatch.setattr(ke, "FW_REQUEUE_S", 0.0)
    logs = []
    lock, info = ke.firmware_gate("192.0.2.1", ke.EXPECT_GIT, ke.EXPECT_CORE,
                                  hours, logs.append)
    return lock, info, logs


def test_firmware_gate_releases_on_a_spike_and_requeues(monkeypatch):
    lock, info, logs = _gate(monkeypatch, [SPIKE, dict(SPIKE), REST])
    assert lock is not None and info == REST
    assert _FakeLock.events == ["acquire", "get_info", "release"] * 2 + \
        ["acquire", "get_info"]           # held on success, for u64_hold
    assert sum("NOT the resting build" in l for l in logs) == 2
    assert "4b02e67c" in logs[0]


def test_firmware_gate_gives_up(monkeypatch):
    monkeypatch.setattr(ke, "FW_REQUEUE_S", 0.0)
    lock, info, logs = _gate(monkeypatch, [SPIKE], hours=0.0)
    assert lock is None and info == SPIKE
    assert _FakeLock.events == ["acquire", "get_info", "release"]


def test_firmware_ok_needs_git_and_core():
    assert ke.firmware_ok(REST, ke.EXPECT_GIT, ke.EXPECT_CORE)
    assert not ke.firmware_ok(SPIKE, ke.EXPECT_GIT, ke.EXPECT_CORE)
    assert not ke.firmware_ok(dict(REST, core_version="1.4F"),
                              ke.EXPECT_GIT, ke.EXPECT_CORE)


# --- fix1: manner judge, verdicts, controls, gate ------------------------------

def _stream(samples, bleeds=()):
    st = ke.Stream.__new__(ke.Stream)
    st.samples = [ke.Sample(t, step, frozenset(keys), idle)
                  for t, step, keys, idle in samples]
    st.bleeds = list(bleeds)
    return st


def _held(step, keys, t0, t1, idle=0xFF, dt=0.1):
    out, t = [], t0
    while t <= t1 + 1e-9:
        out.append((t, step, keys, idle))
        t += dt
    return out


Q, UP, A, SL = ke.K("q"), ke.K("^"), ke.K("a"), ke.K("/")


def test_judge_hold_needs_1_5_s_and_flags_extra_keys():
    j = ke.judge_step(_stream(_held(1, {Q}, 0, 2.0)), 1)
    assert j.performed and not j.defects
    j = ke.judge_step(_stream(_held(1, {Q}, 0, 0.5)), 1)      # a tap
    assert not j.performed
    j = ke.judge_step(_stream(_held(1, {Q, UP}, 0, 2.0)), 1)  # the phantom
    assert j.performed and j.defects and "c6r6" in j.defects[0]
    # samples of another step do not count
    assert not ke.judge_step(_stream(_held(2, {Q}, 0, 2.0)), 1).performed


def test_judge_rollover_needs_three_down_at_once():
    z, i, p = ke.K("z"), ke.K("i"), ke.K("p")
    seq = [(0, 3, {z}, 0xFF), (1, 3, {i}, 0xFF), (2, 3, {p}, 0xFF)]
    assert not ke.judge_step(_stream(seq), 3).performed
    assert ke.judge_step(_stream(seq + [(3, 3, {z, i, p}, 0xFF)]),
                         3).performed
    ghost = ke.judge_step(_stream([(0, 3, {z, i, p, ke.K("l")}, 0xFF)]), 3)
    assert ghost.defects


def test_judge_mods_needs_rshift_and_cbm_and_no_lshift():
    R, C, L = ke.RSHIFT, ke.CBM, ke.LSHIFT
    good = [(0, 4, {R, A}, 0xFF), (1, 4, {R, SL}, 0xFF), (2, 4, {C, Q}, 0xFF)]
    assert ke.judge_step(_stream(good), 4).performed
    lshift = [(0, 4, {L, A}, 0xFF), (1, 4, {L, SL}, 0xFF), (2, 4, {C, Q}, 0xFF)]
    assert not ke.judge_step(_stream(lshift), 4).performed
    mixed = good + [(3, 4, {L}, 0xFF)]
    assert not ke.judge_step(_stream(mixed), 4).performed
    noc = [(0, 4, {R, A}, 0xFF), (1, 4, {R, SL}, 0xFF), (2, 4, {Q}, 0xFF)]
    assert not ke.judge_step(_stream(noc), 4).performed


@pytest.mark.parametrize("n", range(5, 10))
def test_judge_stick_row_per_direction(n):
    row = ke.STEPS[n - 1].row
    idle = 0xFF & ~(1 << row)
    typed = [(0.2 * k, n, {ke.K(c)}, idle) for k, c in enumerate("hose")]
    assert ke.judge_step(_stream(typed), n).performed
    # the same polls 1 s apart at the default poll: stalls (fix5 F3), so
    # not judgeable rather than performed
    slow = [(k, n, {ke.K(c)}, idle) for k, c in enumerate("hose")]
    j = ke.judge_step(_stream(slow), n)
    assert not j.performed and "gap" in j.not_judgeable
    # no stick (Demo A's "typed without the stick"): not performed
    nostick = [(k, n, {ke.K(c)}, 0xFF) for k, c in enumerate("hose")]
    assert not ke.judge_step(_stream(nostick), n).performed
    # the wrong direction: not performed
    other = 0xFF & ~(1 << ((row + 1) % 5))
    wrong = [(k, n, {ke.K(c)}, other) for k, c in enumerate("hose")]
    assert not ke.judge_step(_stream(wrong), n).performed
    # unmasked row (nojoymask): keys on the row read down = a defect; a
    # control only needs the stick seen
    rowkeys = {c * 8 + row for c in range(8)}
    bad = [(0, n, rowkeys, idle)]
    j = ke.judge_step(_stream(bad), n, control=True)
    assert j.performed and j.defects


def test_judge_counts_bleeds_of_its_step_only():
    st = _stream(_held(1, {Q}, 0, 2.0), bleeds=[(1, "BLEED a"), (2, "x")])
    assert ke.judge_step(st, 1).defects == ["BLEED a"]


def _j(performed=True, defects=()):
    return ke.StepJudge(performed, list(defects), [])


ALL = range(1, ke.NSTEP + 1)


def test_judge_run_fixed_never_passes_a_skip_or_a_bad_manner():
    ok = {n: _j() for n in ALL}
    assert ke.judge_run(ke.FIXED, "P" * 9, ok, ALL) == "PASS"
    # Demo A: step 5 skipped -> never PASS
    assert ke.judge_run(ke.FIXED, "PPPPSPPPP", ok, ALL) == "INCOMPLETE"
    assert ke.judge_run(ke.FIXED, "P" * 9, {**ok, 5: _j(False)},
                        ALL) == "INCOMPLETE"
    assert ke.judge_run(ke.FIXED, "PPPPFPPPP", ok, ALL) == "FAIL"
    assert ke.judge_run(ke.FIXED, "P" * 9, {**ok, 1: _j(True, ["x"])},
                        ALL) == "FAIL"
    assert ke.judge_run(ke.FIXED, None, ok, ALL) == "INCOMPLETE"


def test_judge_run_control():
    """fix2 F1: only the control's own signature is DEFECT SHOWN; an F, S
    or stray key without it is operator error (INCOMPLETE)."""
    r = range(1, 3)
    v = "-" * 7
    sig = ke.StepJudge(True, ["x"], [], ["phantom"])
    assert ke.judge_run(ke.CONTROL, "FP" + v, {1: sig, 2: _j()},
                        r) == "DEFECT SHOWN"
    assert ke.judge_run(ke.CONTROL, "PP" + v, {1: _j(), 2: sig},
                        r) == "DEFECT SHOWN"
    assert ke.judge_run(ke.CONTROL, "FP" + v, {1: _j(), 2: _j()},
                        r) == "INCOMPLETE"
    assert ke.judge_run(ke.CONTROL, "PS" + v, {1: _j(), 2: _j()},
                        r) == "INCOMPLETE"
    assert ke.judge_run(ke.CONTROL, "PP" + v, {1: _j(), 2: _j(True, ["b"])},
                        r) == "INCOMPLETE"
    assert ke.judge_run(ke.CONTROL, "PP" + v, {1: _j(), 2: _j()},
                        r) == "INCONCLUSIVE"
    # fix7 K4 (was INCOMPLETE): a signature is an observed fact, and an
    # unperformed step elsewhere does not hide it
    assert ke.judge_run(ke.CONTROL, "FS" + v, {1: sig, 2: _j(False)},
                        r) == "DEFECT SHOWN"
    assert ke.judge_run(ke.CONTROL, "PS" + v, {1: _j(), 2: _j(False)},
                        r) == "INCOMPLETE"


def test_control_verdict_needs_the_defect_signature():
    """Adopted from the fix1 re-review (test_fix1_attacks.py)."""
    class S:
        bleeds = []
        samples = [ke.Sample(0.1 * i, 1, frozenset({ke.K("q")}), 0xFF)
                   for i in range(25)] + \
                  [ke.Sample(2.5 + 0.1 * i, 2, frozenset({ke.K(";")}), 0xFF)
                   for i in range(20)] + \
                  [ke.Sample(5.0 + 0.1 * i, 2, frozenset({ke.K("/")}), 0xFF)
                   for i in range(20)]
    judges = {n: ke.judge_step(S, n, control=True) for n in (1, 2)}
    assert all(j.performed and not j.defects for j in judges.values())
    # Q typed then an extra w (typo): program F; no phantom anywhere
    got = ke.judge_run(ke.CONTROL, "FP-------", judges, range(1, 3))
    assert got != "DEFECT SHOWN", got


@pytest.mark.parametrize("held,ph", [("q", "^"), (";", "l"), ("/", ",")])
def test_settle_signature_is_the_c_minus_1_phantom_in_the_same_sample(held,
                                                                      ph):
    n = 1 if held == "q" else 2
    k, p = ke.K(held), ke.K(ph)
    assert p == k - 8                          # column c-1, same row
    same = _held(n, {k, p}, 0, 2.0)
    j = ke.judge_step(_stream(same), n, control=True)
    assert j.signature and ke.KEY_NAMES[p] in j.signature[0]
    # the same key pressed LATER (a stray press, not the phantom): no sig
    later = _held(n, {k}, 0, 1.0) + _held(n, {k, p}, 1.1, 2.0)
    j = ke.judge_step(_stream(later), n, control=True)
    assert j.defects and not j.signature
    # another stray key with Q in the same sample: not the signature
    if held == "q":
        j = ke.judge_step(_stream(_held(1, {k, ke.K("w")}, 0, 2.0)), 1,
                          control=True)
        assert j.defects and not j.signature


def test_mask_signature_and_a_stick_step_without_it():
    row = ke.STEPS[4].row
    idle = 0xFF & ~(1 << row)
    rowkeys = {c * 8 + row for c in range(8)}
    j = ke.judge_step(_stream([(0, 5, rowkeys, idle)]), 5, control=True)
    assert j.signature
    assert ke.judge_run(ke.CONTROL, "----S----", {5: j},
                        range(5, 6)) == "DEFECT SHOWN"
    # the reviewer's case: stick held, empty line, no row keys: INCOMPLETE
    j = ke.judge_step(_stream(_held(5, set(), 0, 2.0, idle=idle)), 5,
                      control=True)
    assert j.performed and not j.signature
    assert ke.judge_run(ke.CONTROL, "----S----", {5: j},
                        range(5, 6)) == "INCOMPLETE"


def test_session_verdict():
    good = {"fixed@48": "PASS", "nosettle@48": "DEFECT SHOWN",
            "nojoymask@48": "DEFECT SHOWN", "fixed@1": "PASS"}
    assert ke.session_verdict(good) == "VALIDATED"
    assert ke.session_verdict(dict(good, **{"nosettle@48": "INCONCLUSIVE"})) \
        == "INCONCLUSIVE"
    assert ke.session_verdict(dict(good, **{"fixed@48": "FAIL"})) == "FAIL"
    assert ke.session_verdict(dict(good, **{"fixed@1": "INCOMPLETE"})) \
        == "INCOMPLETE"
    assert ke.session_verdict(dict(good, **{"nojoymask@48": "INCOMPLETE"})) \
        == "INCOMPLETE"
    del good["nojoymask@48"]
    assert ke.session_verdict(good) == "INCOMPLETE"


def test_manual_plan_has_both_positive_controls_at_48_and_a_1_mhz_run():
    runs = [str(r) for r in ke.parse_runs(ke.MANUAL_PLAN)]
    assert runs[0] == "fixed@48" and "fixed@1" in runs
    for c in ke.CONTROLS:
        assert f"{c}@48" in runs
    assert set(ke.CONTROLS) == set(ke.CONTROL_STEPS)
    assert ke.control_args("nosettle") == b"CONTROL nosettle 1 2\n"
    assert all(len(ke.control_args(c)) <= 31 for c in ke.CONTROLS)


def test_c_program_takes_the_control_arguments():
    text = SRC.read_text()
    assert 'strcmp(argv[1], "CONTROL")' in text
    assert "SUPPOSED TO FAIL" in text


RUNNER = Path(__file__).resolve().parents[2] / "build" / "w6-kbd" / \
    "runner.prg"


def test_find_site_is_the_scan_column_loop():
    """On a real build: the pattern matches once, its and-operand is
    hle_con_idle, and the NOP-ed spans are exactly jsr+bcc and the and."""
    import subprocess
    import tempfile
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["make", "-s", "runtime", f"BUILD={d}"], cwd=root,
                       check=True, capture_output=True)
        prg = (Path(d) / "runner.prg").read_bytes()
        lbl = (Path(d) / "runner.lbl").read_text()
    labels = {m[1]: int(m[0], 16) for m in
              re.findall(r"al ([0-9A-F]+) \.(\w+)", lbl)}
    lay = ke.bss_layout(HLE_CON.read_text())
    idle = labels["hle_con_magic"] + lay["hle_con_idle"]
    site = ke.find_site(prg, idle)
    load = prg[0] | prg[1] << 8
    code = prg[2 + site - load:2 + site - load + 20]
    assert code[0] == 0x20 and code[3] == 0x90           # jsr / bcc
    assert code[16:19] == bytes([0x2D, idle & 0xFF, idle >> 8])
    assert ke.CONTROLS["nosettle"] == (0, 5)
    assert ke.CONTROLS["nojoymask"] == (16, 3)
    assert labels["hle_con_scan"] < site < labels["hle_con_scan"] + 0x80
    with pytest.raises(RuntimeError):
        ke.find_site(prg[:2] + prg[2:] + prg[2:], idle)   # twice: refuse


def test_gate_releases_when_get_info_raises_and_retries(monkeypatch):
    import c64_test_harness as h
    _FakeLock.events = []
    calls = []

    class Flaky:
        def __init__(self, host):
            pass

        def get_info(self):
            calls.append(1)
            _FakeLock.events.append("get_info")
            if len(calls) == 1:
                raise OSError("device rebooting")
            return dict(REST)

        def close(self):
            pass
    monkeypatch.setattr(h, "DeviceLock", _FakeLock)
    monkeypatch.setattr(h, "Ultimate64Client", Flaky)
    monkeypatch.setattr(ke, "FW_REQUEUE_S", 0.0)
    logs = []
    lock, info = ke.firmware_gate("192.0.2.1", ke.EXPECT_GIT,
                                  ke.EXPECT_CORE, 1.0, logs.append)
    assert lock is not None and info == REST
    assert _FakeLock.events == ["acquire", "get_info", "release",
                                "acquire", "get_info"]
    assert "device rebooting" in logs[0]


def test_gate_fail_fast_on_wrong_firmware(monkeypatch):
    import c64_test_harness as h
    _FakeLock.events = []
    monkeypatch.setattr(h, "DeviceLock", _FakeLock)
    monkeypatch.setattr(h, "Ultimate64Client", _fake_client([SPIKE, REST]))
    monkeypatch.setattr(ke.time, "sleep",
                        lambda s: pytest.fail("manual mode must not wait"))
    logs = []
    lock, info = ke.firmware_gate("192.0.2.1", ke.EXPECT_GIT, ke.EXPECT_CORE,
                                  12.0, logs.append, fail_fast=True)
    assert lock is None and info == SPIKE
    assert _FakeLock.events == ["acquire", "get_info", "release"]


def test_gate_fail_fast_on_a_busy_lock_names_the_holder(monkeypatch):
    import c64_test_harness as h
    from c64_test_harness import DeviceLockTimeout

    class Busy(_FakeLock):
        def acquire_or_raise(self, timeout, **kw):
            assert kw.get("progress_window", 60.0) is None   # no queueing
            assert timeout <= ke.LOCK_FAST_S
            raise DeviceLockTimeout(
                device_host="192.0.2.1", holder_pid=3533, pid_alive=True,
                lockfile_age_seconds=1.0, device_reachable_rest=True,
                timeout=ke.LOCK_FAST_S)
    monkeypatch.setattr(h, "DeviceLock", Busy)
    monkeypatch.setattr(h, "Ultimate64Client", _fake_client([]))
    logs = []
    lock, info = ke.firmware_gate("192.0.2.1", ke.EXPECT_GIT, ke.EXPECT_CORE,
                                  12.0, logs.append, fail_fast=True)
    assert lock is None and info["holder_pid"] == 3533
    assert "3533" in logs[0]


def test_confirm_refuses_without_a_terminal(monkeypatch):
    import io
    monkeypatch.setattr(ke.sys, "stdin", io.StringIO("\n"))
    logs = []
    assert not ke.confirm(ke.parse_runs(ke.MANUAL_PLAN), logs.append)
    assert "not a terminal" in logs[0]


def test_scr_text_shows_the_phantom_glyph():
    assert ke.shown("> ^q") == "> ^Q"
    assert ke.scr_text(bytes([0x1C, 0x1E, 0x1F, 0x00])) == "\\^_@"


def test_gate_releases_on_ctrl_c_during_the_get(monkeypatch):
    """L5: the release is in a finally, so even a ^C (BaseException) in
    the GET cannot leave the lock held."""
    import c64_test_harness as h
    _FakeLock.events = []

    class Interrupted:
        def __init__(self, host):
            pass

        def get_info(self):
            raise KeyboardInterrupt

        def close(self):
            pass
    monkeypatch.setattr(h, "DeviceLock", _FakeLock)
    monkeypatch.setattr(h, "Ultimate64Client", Interrupted)
    with pytest.raises(KeyboardInterrupt):
        ke.firmware_gate("192.0.2.1", ke.EXPECT_GIT, ke.EXPECT_CORE, 1.0,
                         print)
    assert _FakeLock.events == ["acquire", "release"]


# --- fix2 LOW: the Patcher's own guards, with a fake C64 RAM --------------------

class _Ram:
    def __init__(self, site, image, lie=False):
        self.mem = bytearray(0x10000)
        self.mem[site:site + len(image)] = image
        self.lie = lie                 # readback returns what was there
        self.writes = []


def _patcher(monkeypatch, image, lie=False):
    import c64_test_harness as h
    site = 0x4000
    ram = _Ram(site, image, lie)

    def wb(t, addr, data):
        ram.writes.append((addr, bytes(data)))
        if not ram.lie:
            ram.mem[addr:addr + len(data)] = data

    def rb(t, addr, n):
        return bytes(ram.mem[addr:addr + n])
    monkeypatch.setattr(h, "write_bytes", wb)
    monkeypatch.setattr(h, "read_bytes", rb)
    pt = ke.Patcher.__new__(ke.Patcher)
    pt.sess = types.SimpleNamespace(target=types.SimpleNamespace(
        transport=None))
    pt.site = site
    orig = bytes([0x20, 0x34, 0x12, 0x90, 0x10, 0xBD, 0, 0, 0x8D, 0, 0xDC,
                  0xAD, 1, 0xDC, 0x49, 0xFF, 0x2D, 0x92, 0x5D, 0x9D])
    pt.orig = {n: orig[o:o + ln] for n, (o, ln) in ke.CONTROLS.items()}
    pt.active = None
    return pt, ram, orig



def test_patcher_applies_and_removes_on_a_clean_image(monkeypatch):
    pt, ram, orig = _patcher(monkeypatch, bytes(20))
    ram.mem[pt.site:pt.site + 20] = orig
    pt.apply("nosettle")
    assert ram.mem[pt.site:pt.site + 5] == b"\xea" * 5
    pt.remove()
    assert pt.check_clean() and pt.active is None


def test_patcher_refuses_a_dirty_image(monkeypatch):
    """apply_noclean: another control still applied (or anything not the
    original): no write at all."""
    pt, ram, orig = _patcher(monkeypatch, bytes(20))
    ram.mem[pt.site:pt.site + 20] = orig
    ram.mem[pt.site + 16:pt.site + 19] = b"\xea" * 3      # nojoymask left
    with pytest.raises(RuntimeError, match="not the original"):
        pt.apply("nosettle")
    assert ram.writes == [] and pt.active is None


def test_patcher_raises_when_the_readback_differs(monkeypatch):
    """put_noreadback: a write that does not land must raise, and the
    patch must not be recorded as active."""
    pt, ram, orig = _patcher(monkeypatch, bytes(20), lie=True)
    ram.mem[pt.site:pt.site + 20] = orig
    with pytest.raises(RuntimeError, match="readback"):
        pt.apply("nosettle")
    assert pt.active is None


def test_control_case_scripts_type_what_they_claim():
    typed = {name: _typed(script) for name, script, *_ in ke.CONTROL_CASES}
    assert typed == {"honest": "q\r;/\r", "typo": "qw\r;/\r",
                     "phantom": "^q\r;/\r"}
    assert [c[4] for c in ke.CONTROL_CASES] == ["INCONCLUSIVE", "INCOMPLETE",
                                                "DEFECT SHOWN"]
