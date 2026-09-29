"""W6-K fix3: the attended U64E sessions (2026-09-25), replayed offline.

tests/kbd/replay/*.session.log are the three session logs, verbatim except
for relative prg paths (runner build/w6-int/runner.prg, U64E fw 3.15 git c47155b7 core 1.50).
tests.kbd_echo.replay_log rebuilds every run's stream from them; the
pre-fix judge reproduced all 11 run verdicts and 70 step judgments the live
driver logged (build/evidence/w6-kbd/fix3/red).  These tests pin the fixes:
BUG 1 (the STEPS summary on the prompt row), BUG 2 (a letter tapped between
two polls was never "typed with the stick held"), and the SHIFT false bleed.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from tests import kbd_echo as ke

LOGS = Path(__file__).resolve().parents[2] / "tests" / "kbd" / "replay"


# The attended logs were recorded at the default --poll 0.1 but before the
# poll was logged (fix5): these tests pin the judge on them with that poll
# assumed; fix5's tests below cover the unknown-timing default.
ASSUMED = 0.1


def _runs(name):
    return ke.replay_log(str(LOGS / f"{name}.session.log"),
                         assume_poll=ASSUMED)


def _judge(run, st):
    judges = {n: ke.judge_step(st, n, control=run.kind == ke.CONTROL)
              for n in run.steps}
    return judges, ke.judge_run(run.kind, st.verdicts(), judges, run.steps)


def test_bug1_summary_on_the_prompt_row_is_parsed():
    run, st, logged = _runs("kbd-hw-2")[0]
    assert str(run) == "fixed@48" and logged == "INCOMPLETE"
    assert any(r.startswith("> STEPS ") for r in st.screen_log)
    assert st.verdicts() == "PPPPPPPPP"


@pytest.mark.parametrize("row,want", [
    ("STEPS PPPPPPPPP", "PPPPPPPPP"),
    ("> STEPS PPPPPPPPP", "PPPPPPPPP"),
    ("> xySTEPS PP-------", "PP-------"),
    ("STEPS PPP", None),                  # not nine steps: not the summary
])
def test_verdicts_row_forms(row, want):
    st = ke.replay_stream()
    st.screen_log = ["x", row]
    assert st.verdicts() == want


def test_step_tracking_tolerates_prompt_residue():
    st = ke.replay_stream()
    st.screen_step("> STEP 3/9: ROLLOVER")
    assert st.step == 3
    st.screen_step("> STEP 3 PASS")
    assert st.step is None


def test_bug2_stick_letters_tapped_between_polls():
    """kbd-hw-2 fixed@48 steps 5-9: the stick was held throughout HOSE;
    every letter is in the echo line, typed between two polls that both
    show the stick's row held."""
    run, st, _ = _runs("kbd-hw-2")[0]
    judges, got = _judge(run, st)
    for n in range(5, 10):
        assert judges[n].performed, (n, judges[n].notes)
        assert not judges[n].defects, (n, judges[n].defects)
    assert got == "PASS", {n: j.notes for n, j in judges.items()}


def test_bug2_mods_letters_tapped_between_polls():
    run, st, _ = _runs("kbd-hw-2")[0]
    judges, _ = _judge(run, st)
    assert judges[4].performed, judges[4].notes


def test_shift_is_never_a_bleed_source():
    assert ke.bleed_suspect(ke.K("."), {ke.RSHIFT, ke.K(".")}) is None
    assert ke.bleed_suspect(ke.K("^"), {ke.K("q"), ke.K("^")}) == ke.K("q")
    line = ke.key_events({ke.RSHIFT}, {ke.RSHIFT, ke.K(".")}, 1)
    assert not any("BLEED" in l for l in line)


def test_echo_rule_needs_the_stick_at_both_ends():
    """A letter whose interval starts or ends without the stick's row held
    is not 'typed with the stick held' (no weakening)."""
    row = 0
    held = 0xFF & ~(1 << row)
    ss = [ke.Sample(0.0, 5, frozenset(), held, 0, ""),
          ke.Sample(0.2, 5, frozenset(), held, 1, "H"),
          ke.Sample(0.4, 5, frozenset(), 0xFF, 2, "HO"),      # released
          ke.Sample(0.6, 5, frozenset(), held, 3, "HOS"),
          ke.Sample(0.8, 5, frozenset(), held, 4, "HOSE")]
    st = ke.replay_stream()
    st.samples = ss
    j = ke.judge_step(st, 5)
    assert not j.performed
    assert any("O not typed" in n for n in j.notes)
    ss[2] = ke.Sample(0.4, 5, frozenset(), held, 2, "HO")
    assert ke.judge_step(st, 5).performed


# Per-run replay results after fix3 (build/evidence/w6-kbd/fix3/green):
EXPECT = {
    "kbd-hw": ["INCOMPLETE", "FAIL", "FAIL", "INCOMPLETE", "DEFECT SHOWN",
               "PASS"],
    "kbd-hw-2": ["PASS", "INCONCLUSIVE", "DEFECT SHOWN", "PASS"],
    "kbd-hw-sparse": ["INCOMPLETE"],
}


@pytest.mark.parametrize("name", sorted(EXPECT))
def test_replay_results(name):
    got = [_judge(run, st)[1] for run, st, _ in _runs(name)]
    assert got == EXPECT[name]


def test_control_signatures_unchanged_by_the_fix():
    """The nojoymask control still shows its signature; the nosettle
    controls still have none (no weakening of the control logic)."""
    for name in ("kbd-hw", "kbd-hw-2"):
        for run, st, logged in _runs(name):
            if run.kind == ke.CONTROL:
                judges, got = _judge(run, st)
                assert (got == "DEFECT SHOWN") == (run.build == "nojoymask")
                assert bool(any(j.signature for j in judges.values())) == \
                    (run.build == "nojoymask")


@pytest.mark.parametrize("name,want", [("kbd-hw", "FAIL"),
                                       ("kbd-hw-2", "INCONCLUSIVE"),
                                       ("kbd-hw-sparse", "INCOMPLETE")])
def test_replayed_session_verdicts(name, want):
    res = ke.replay_report([str(LOGS / f"{name}.session.log")],
                           out=lambda *a: None, assume_poll=ASSUMED)
    ss = next(iter(res.values()))
    assert ke.file_verdict([s.verdict for s in ss]) == want


def test_raw_diagnostic_shows_a_held_key_the_debounce_never_committed():
    """The sparse-run question: with hle_con_raw in the stream, a key held
    but never debounced (raw has it, mat does not) is reported; a key
    never pressed is not; a modifier alone is not."""
    st = ke.replay_stream()
    st.nkey, st.mat, st.idle, st.raw_extra = 0, bytes(8), 0xFF, set()
    out = []
    st.emit = out.append
    states = iter([
        {"nkey": 0, "idle": 0xFF, "mat": bytes(8), "raw": bytes(8)},
        {"nkey": 0, "idle": 0xFF, "mat": bytes(8),
         "raw": ke.bits_mat({ke.K("q"), ke.K("^")})},
        {"nkey": 0, "idle": 0xFF, "mat": bytes(8),
         "raw": ke.bits_mat({ke.K("q"), ke.K("^")})},       # unchanged
        {"nkey": 0, "idle": 0xFF, "mat": bytes(8),
         "raw": ke.bits_mat({ke.RSHIFT})},
    ])
    st.read_state = lambda: next(states)
    for _ in range(4):
        st.poll(screen=False)
    raws = [l for l in out if l.startswith("raw")]
    assert raws == ["raw (not debounced yet): c6r6 UPARROW, c7r6 Q",
                    "raw (not debounced yet): none"]       # fix5 F5
    assert st.samples[1].raw == {ke.K("q"), ke.K("^")}


def _stick_samples(idles, lines, nkeys):
    return [ke.Sample(0.2 * i, 5, frozenset(), idle, nk, line)
            for i, (idle, line, nk) in enumerate(zip(idles, lines, nkeys))]


HELD0 = 0xFF & ~1


def test_echo_needs_the_stick_before_the_gain_too():
    """The stick pressed only at the poll AFTER a letter's gain: that letter
    was not typed with the stick held (it may have been typed first)."""
    st = ke.replay_stream()
    st.samples = _stick_samples([0xFF, HELD0, HELD0, HELD0, HELD0],
                                ["", "H", "HO", "HOS", "HOSE"],
                                [0, 1, 2, 3, 4])
    j = ke.judge_step(st, 5)
    assert not j.performed and any("H not typed" in n for n in j.notes)


def test_echo_gain_needs_key_events_behind_it():
    """A line gain with no hle_con_nkey growth anywhere near it (a row the
    screen mirror re-emitted) is not a keystroke."""
    st = ke.replay_stream()
    st.samples = _stick_samples([HELD0] * 6, ["", "", "HOSE", "HOSE", "HOSE",
                                             "HOSE"], [7] * 6)
    assert ke.echo_typed(st.samples) == []
    assert not ke.judge_step(st, 5).performed
    st.samples = _stick_samples([HELD0] * 6, ["", "", "HOSE", "HOSE", "HOSE",
                                             "HOSE"], [7, 7, 9, 11, 11, 11])
    assert [c for c, *_ in ke.echo_typed(st.samples)] == list("HOSE")


def test_replay_parses_masked_and_missed_event_lines(tmp_path):
    log = tmp_path / "s.log"
    log.write_text("\n".join([
        "[   0.04] fixed@48 started: operator, follow the C64 screen",
        "[   1.00]   | STEP 1/9: HOLD Q",
        "[   1.00]   | >",
        "[   2.00] key DOWN c7r6 Q",
        "[   2.00] key DOWN c7r0 1",
        "[   2.00] (1 key(s) down without an event: masked or dropped)",
        "[   3.00] (+2 key event(s) released before this poll saw them)",
        "[   4.00] fixed@48: INCOMPLETE (want PASS), STEPS None",
    ]) + "\n")
    (run, st, logged), = ke.replay_log(str(log))
    assert str(run) == "fixed@48" and logged == "INCOMPLETE"
    by_t = {round(s.t, 2): s for s in st.samples}
    assert by_t[2.0].nkey == 1                  # 2 keys down, 1 event
    assert by_t[3.0].nkey == 3
    assert by_t[2.0].keys == {ke.K("q"), ke.K("1")} and by_t[2.0].step == 1


# --- fix4 (fix3 re-review) ----------------------------------------------------

FREE = 0xFF
DT = 0.12                  # a realistic live poll period


def st_of(rows, step=5, dt=DT):
    st = ke.replay_stream()
    st.samples = [ke.Sample(dt * i, step, frozenset(k), idle, nk, line)
                  for i, (idle, line, nk, k) in enumerate(rows)]
    return st


def test_A3_delete_shrink_credits_nothing():
    """Adopted from the fix3 review (G1): HOSE typed with no stick, the
    stick grabbed, DEL DEL, S E retyped: H and O were never typed with
    the stick."""
    H, O, S, E = (ke.K(c) for c in "hose")
    rows = [(FREE, "", 0, ()),
            (FREE, "H", 1, (H,)), (FREE, "HO", 2, (O,)),
            (FREE, "HOS", 3, (S,)), (FREE, "HOSE", 4, (E,)),
            (HELD0, "HOSE", 4, ()),              # stick grabbed
            (HELD0, "HOS", 5, ()),               # DEL
            (HELD0, "HO", 6, ()),                # DEL
            (HELD0, "HOS", 7, ()),               # S, stick held
            (HELD0, "HOSE", 8, ())]              # E, stick held
    st = st_of(rows)
    assert [c for c, *_ in ke.echo_typed(st.samples)] == \
        ["H", "O", "S", "E", "S", "E"]
    assert not ke.judge_step(st, 5).performed


def test_echo_del_and_retype_between_two_polls_credits_the_new_char_only():
    rows = [(HELD0, "", 0, ()), (HELD0, "HOSX", 4, ()),
            (HELD0, "HOSE", 6, ()), (HELD0, "HOSE", 6, ())]  # DEL + E
    assert [c for c, *_ in ke.echo_typed(st_of(rows).samples)] == \
        ["H", "O", "S", "X", "E"]


def test_echo_credit_needs_a_poll_gap_of_at_most_0_4_s():
    ok = [(HELD0, "", 0, ()), (HELD0, "HOSE", 4, ()), (HELD0, "HOSE", 4, ())]
    assert ke.judge_step(st_of(ok, dt=0.4), 5).performed
    assert not ke.judge_step(st_of(ok, dt=0.41), 5).performed
    assert ke.ECHO_GAP_MAX == 0.4


def test_stick_continuity_from_the_first_letter_to_the_last():
    """Every poll between the first HOSE letter and the last must show the
    stick, even where no letter was typed."""
    H, O, S, E = (ke.K(c) for c in "hose")
    good = [(HELD0, "", 0, ()), (HELD0, "H", 1, (H,)), (HELD0, "HO", 2, (O,)),
            (HELD0, "HO", 2, ()), (HELD0, "HOS", 3, (S,)),
            (HELD0, "HOSE", 4, (E,))]
    assert ke.judge_step(st_of(good), 5).performed
    gap = list(good)
    gap[3] = (FREE, "HO", 2, ())                 # released between O and S
    j = ke.judge_step(st_of(gap), 5)
    assert not j.performed
    assert any("continuously" in n for n in j.notes), j.notes
    # before the first letter and after the last, the stick may be free
    loose = [(FREE, "", 0, ())] + good + [(FREE, "HOSE", 4, ())]
    assert ke.judge_step(st_of(loose), 5).performed


def test_mods_rshift_may_be_released_between_a_and_slash_not_within():
    """The screen says "RSHIFT+A, RSHIFT+/": RSHIFT let go between the two
    is fine (both attended fixed@1 runs did it); a character whose own
    credit polls lack RSHIFT is not."""
    R, C = ke.RSHIFT, ke.CBM
    A, SL, Q = ke.K("a"), ke.K("/"), ke.K("q")
    good = [(FREE, "", 0, (R,)), (FREE, "A", 1, (R, A)), (FREE, "A", 1, ()),
            (FREE, "A", 1, (R,)), (FREE, "A?", 2, (R, SL)),
            (FREE, "A?", 2, (C,)), (FREE, "A?Q", 3, (C, Q))]
    assert ke.judge_step(st_of(good, step=4), 4).performed
    bad = list(good)
    bad[3] = (FREE, "A", 1, ())                  # / echoed with no RSHIFT
    bad[4] = (FREE, "A?", 2, ())
    assert not ke.judge_step(st_of(bad, step=4), 4).performed


def test_matrix_path_uses_the_first_down_poll_only():
    """first_any: H pressed WITHOUT the stick and kept down, then the stick
    grabbed while H is still down: H was not typed with the stick."""
    H, O, S, E = (ke.K(c) for c in "hose")
    rows = [(FREE, "", 0, ()), (FREE, "H", 1, (H,)), (HELD0, "H", 1, (H,)),
            (HELD0, "HO", 2, (O,)), (HELD0, "HOS", 3, (S,)),
            (HELD0, "HOSE", 4, (E,))]
    j = ke.judge_step(st_of(rows), 5)
    assert not j.performed and any("H not typed" in n for n in j.notes)


def test_replay_exits_0_only_for_a_validated_session(tmp_path, monkeypatch):
    logs = [str(LOGS / f"{n}.session.log") for n in ("kbd-hw", "kbd-hw-2")]
    assert ke.main(["--replay"] + logs) == 1          # FAIL, INCONCLUSIVE
    monkeypatch.setattr(ke, "file_verdict", lambda vs: "VALIDATED")
    assert ke.main(["--replay"] + logs) == 0


def test_stream_logs_a_poll_heartbeat_to_the_session_log_only(monkeypatch):
    printed, logged = [], []
    st = ke.replay_stream()
    st.out = printed.append
    st.nkey, st.mat, st.idle, st.raw_extra = 0, bytes(8), 0xFF, set()
    st.t0 = st.hb_t = st.last_poll = 0.0
    st.hb_n, st.hb_gap = 0, 0.0
    monkeypatch.setattr(ke, "_log_only", logged.append)
    clock = iter([0.1 * i for i in range(1, 40)])
    monkeypatch.setattr(ke.time, "monotonic", lambda: next(clock))
    st.read_state = lambda: {"nkey": 0, "idle": 0xFF, "mat": bytes(8),
                             "raw": bytes(8)}
    for _ in range(25):
        st.poll(screen=False)
    hb = [l for l in logged if "hb polls" in l]
    assert len(hb) == 2 and not any("hb polls" in l for l in printed)
    msg = ke.LOG_LINE.match(hb[0])[2]          # as the replay parses it
    assert ke.HB_LINE.match(msg)[1] == "10"


def test_replay_refuses_a_polling_gap(tmp_path):
    base = ["[   0.04] fixed@48 started: operator, follow the C64 screen",
            "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
            "[   2.00] key DOWN c7r6 Q"]
    tail = ["[   9.00] key UP   c7r6 Q",
            "[  10.00] fixed@48: INCOMPLETE (want PASS), STEPS None"]
    ok = tmp_path / "ok.log"
    ok.write_text("\n".join(base + ["[   5.00] hb polls +30 max gap 0.14s"]
                            + tail) + "\n")
    bad = tmp_path / "bad.log"
    bad.write_text("\n".join(base + ["[   5.00] hb polls +3 max gap 2.10s"]
                             + tail) + "\n")
    none = tmp_path / "none.log"
    none.write_text("\n".join(base + tail) + "\n")
    (_, st_ok, _), = ke.replay_log(str(ok))
    (_, st_bad, _), = ke.replay_log(str(bad))
    (_, st_none, _), = ke.replay_log(str(none))
    assert st_ok.continuity.startswith("proven")
    assert st_bad.continuity.startswith("BROKEN")
    assert st_none.continuity.startswith("unproven")
    out = []
    res = ke.replay_report([str(bad)], out=out.append)
    assert res[str(bad)][0].results["fixed@48"] == "INCOMPLETE"  # no STEPS
    assert any("BROKEN" in l for l in out)


def test_echo_base_starts_empty_even_after_a_stale_line():
    """A step's first poll can still show the previous step's line (HOSE,
    a mirror re-emission): each step starts at an empty prompt, so H
    typed next is new, not a DEL of HOSE."""
    rows = [(HELD0, "HOSE", 0, ()), (HELD0, "H", 1, ()),
            (HELD0, "HO", 2, ()), (HELD0, "HOS", 3, ()),
            (HELD0, "HOSE", 4, ()), (HELD0, "HOSE", 4, ())]
    assert [c for c, *_ in ke.echo_typed(st_of(rows).samples)] == list("HOSE")
    assert ke.judge_step(st_of(rows), 5).performed


def test_a_broken_heartbeat_turns_a_passing_replay_incomplete(tmp_path):
    src = (LOGS / "kbd-hw-2.session.log").read_text().splitlines()
    first = next(i for i, l in enumerate(src) if " fixed@48 started:" in l)
    assert _judge(*_runs("kbd-hw-2")[0][:2])[1] == "PASS"
    for gap, want in (("0.14", "PASS"), ("2.10", "NOT JUDGEABLE")):
        log = tmp_path / f"g{gap}.log"
        log.write_text("\n".join(src[:first + 1] +
                                 [f"[   0.50] hb polls +4 max gap {gap}s"] +
                                 src[first + 1:]) + "\n")
        res = ke.replay_report([str(log)], out=lambda *a: None)
        assert res[str(log)][0].results["fixed@48"] == want, gap


# --- fix5 (fix4 review, build/evidence/w6-kbd-review4/REVIEW.md) -------------
# The reviewer's attacks (test_fix4_attacks.py) asserted the defects; here
# they are adopted inverted, next to new tests for each item.

SPARSE2 = LOGS / "kbd-hw-sparse2.session.log"   # 19:31, --poll 1.0, pre-fix3
HELD = 0xFF & ~1


def live_stream(states, step, dt=0.25, t0=0.0, jumps=()):
    """Drive the real Stream.poll (screen=False) with stubbed reads; the
    clock advances *dt* per poll, plus jumps[i] before poll i."""
    st = ke.replay_stream()
    st.nkey, st.mat, st.idle, st.raw_extra = 0, bytes(8), 0xFF, set()
    st.step = step
    out = []
    st.emit = out.append
    st.t0 = st.hb_t = t0
    it = iter(states)
    st.read_state = lambda: next(it)
    now = [t0]
    real = ke.time.monotonic
    ke.time.monotonic = lambda: now[0]    # emit() reads the clock too
    try:
        for i in range(len(states)):
            now[0] += dict(jumps).get(i, 0.0)
            st.poll(screen=False)
            now[0] += dt
    finally:
        ke.time.monotonic = real
    return st, out


def state(keys=(), nkey=0, idle=0xFF, raw=None):
    return {"nkey": nkey, "idle": idle, "mat": ke.bits_mat(set(keys)),
            "raw": ke.bits_mat(set(raw if raw is not None else keys))}


def write_log(tmp_path, lines, name="s.log"):
    log = tmp_path / name
    log.write_text("\n".join(lines) + "\n")
    return str(log)


def later_nosettle_session(start="started"):
    """The reviewer's F7 session: a nosettle-only run that shows the
    signature, appended to an earlier session's file."""
    return [
        "[19:00:00] kbd_echo MANUAL on 10.0.0.1, runs nosettle@48, prg x",
        f"[   0.04] nosettle@48 {start}: operator, follow the C64 screen",
        "[   1.30]   | STEP 1/9: HOLD Q", "[   1.30]   | >",
        "[   2.00] key DOWN c6r6 UPARROW", "[   2.00] key DOWN c7r6 Q",
        "[   2.25]   | > ^Q",
        "[   5.00] key UP   c6r6 UPARROW", "[   5.00] key UP   c7r6 Q",
        "[   6.20]   | STEP 1 FAIL",
        "[   6.20]   | STEP 2/9: HOLD ; AND /", "[   6.20]   | >",
        "[   7.00] key DOWN c6r2 ;", "[   9.40] key UP   c6r2 ;",
        "[  10.00] key DOWN c6r7 /", "[  12.40] key UP   c6r7 /",
        "[  13.00]   | STEP 2 PASS", "[  13.00]   | STEPS FP-------",
        "[  13.10] nosettle@48: DEFECT SHOWN (want DEFECT SHOWN), STEPS "
        "FP-------",
        "[19:01:00] SESSION VERDICT: INCOMPLETE (not the full plan)"]


GOOD = {"fixed@48": "PASS", "nosettle@48": "DEFECT SHOWN",
        "nojoymask@48": "DEFECT SHOWN", "fixed@1": "PASS"}


# F7: one verdict per session, never better than any session's

def test_F7_appended_sessions_are_judged_one_by_one(tmp_path):
    kbd2 = (LOGS / "kbd-hw-2.session.log").read_text().splitlines()
    log = write_log(tmp_path, kbd2 + later_nosettle_session(), "session.log")
    res = ke.replay_report([log], out=lambda *a: None, assume_poll=0.1)
    ss = res[log]
    assert [s.plan for s in ss] == [ke.MANUAL_PLAN, "nosettle@48"]
    assert ss[1].results == {"nosettle@48": "DEFECT SHOWN"}
    assert [s.verdict for s in ss] == ["INCONCLUSIVE", "INCOMPLETE"]
    assert ke.file_verdict([s.verdict for s in ss]) == "INCOMPLETE"
    assert ke.main(["--replay", log]) == 1


def test_F7_the_file_verdict_is_the_worst_session_verdict():
    V, IC, IP, F = "VALIDATED", "INCONCLUSIVE", "INCOMPLETE", "FAIL"
    assert ke.file_verdict([V]) == V
    assert ke.file_verdict([V, IC]) == IC
    assert ke.file_verdict([IC, V, IP]) == IP
    assert ke.file_verdict([V, F, IC]) == F
    assert ke.file_verdict([]) == IP


def test_F7_a_replayed_session_validates_only_on_the_full_plan_proven():
    rv = ke.replay_session_verdict
    assert rv(GOOD, ke.MANUAL_PLAN, proven=True) == "VALIDATED"
    assert rv(GOOD, "nosettle@48", proven=True) == "INCOMPLETE"
    assert rv(GOOD, None, proven=True) == "INCOMPLETE"      # no header
    assert rv(GOOD, ke.MANUAL_PLAN, proven=False) == "INCOMPLETE"
    assert rv(dict(GOOD, **{"fixed@1": "FAIL"}), "nosettle@48",
              proven=False) == "FAIL"


def test_F7_kbd_hw_holds_three_sessions_judged_separately():
    res = ke.replay_report([str(LOGS / "kbd-hw.session.log")],
                           out=lambda *a: None, assume_poll=0.1)
    ss = next(iter(res.values()))
    assert [list(s.results) for s in ss] == [
        ["fixed@48"], ["fixed@48"],
        ["fixed@48", "nosettle@48", "nojoymask@48", "fixed@1"]]
    assert [s.verdict for s in ss] == ["INCOMPLETE", "FAIL", "FAIL"]


def test_F7_one_log_per_session(tmp_path, monkeypatch):
    """The default --out-dir is new per session, and an --out-dir that
    already holds a session.log is refused before anything runs."""
    prg = tmp_path / "r.prg"
    prg.write_bytes(b"\x01\x08")
    (tmp_path / "r.lbl").write_text("")
    out = tmp_path / "ev"
    out.mkdir()
    (out / "session.log").write_text("old\n")
    monkeypatch.setenv("U64_HOST", "192.0.2.1")
    monkeypatch.setattr(ke, "run_u64",
                        lambda *a: pytest.fail("must not start"))
    assert ke.main(["--prg", str(prg), "--backend", "u64",
                    "--out-dir", str(out)]) == 2
    assert (out / "session.log").read_text() == "old\n"
    a = ke.default_out_dir("20260928-120000")
    assert a.endswith("kbd_echo-u64-20260928-120000")


# F1 / decision (a): bleed hints are defects only in hold steps 1-2

def test_F1_rollover_bleed_hint_is_a_note_not_a_defect():
    Z, I, P = ke.K("z"), ke.K("i"), ke.K("p")
    st, out = live_stream([state(), state({Z}, 1), state({Z, I, P}, 3),
                           state({Z, I, P}, 3), state(set(), 3)], step=3)
    assert any("COLUMN BLEED? c5r1 P" in l for l in out)
    j3 = ke.judge_step(st, 3)
    assert j3.performed and not j3.defects
    assert any("COLUMN BLEED" in n for n in j3.notes)
    judges = {n: ke.StepJudge(True, [], []) for n in range(1, 10)}
    judges[3] = j3
    assert ke.judge_run(ke.FIXED, "PPPPPPPPP", judges, range(1, 10)) == \
        "PASS"


def test_F1_bleeds_in_hold_steps_stay_defects_elsewhere_notes():
    st = ke.replay_stream()
    st.samples = [ke.Sample(0.1 * i, n, frozenset(), 0xFF, 0, "")
                  for n in (2, 5) for i in range(3)]
    st.bleeds = [(2, "key DOWN c5r2 L   <- COLUMN BLEED? c6r2 ; is held"),
                 (5, "key DOWN c4r0 9   <- COLUMN BLEED? c5r0 + is held")]
    assert ke.judge_step(st, 2).defects == [st.bleeds[0][1]]
    j5 = ke.judge_step(st, 5)
    assert not j5.defects and any(st.bleeds[1][1] in n for n in j5.notes)


def test_F1_real_sparse2_fixed48_is_no_longer_a_fail():
    runs = ke.replay_log(str(SPARSE2))
    run, st, logged = runs[1]
    assert str(run) == "fixed@48" and logged == "FAIL"
    assert all(not ke.judge_step(st, n).defects for n in run.steps)
    res = ke.replay_report([str(SPARSE2)], out=lambda *a: None)
    s, = res[str(SPARSE2)]
    assert s.results["fixed@48"] != "FAIL" and s.verdict != "FAIL"


# F2: no invented polls in a log without a poll period

def test_F2_a_log_without_poll_or_heartbeat_has_unknown_timing():
    run, st, _ = ke.replay_log(str(SPARSE2))[1]
    assert st.period is None and "unknown" in st.continuity
    stamps = {round(float(ke.LOG_LINE.match(l)[1]), 2)
              for l in SPARSE2.read_text().splitlines()
              if ke.LOG_LINE.match(l)}
    assert all(round(s.t, 2) in stamps for s in st.samples)   # no fill
    j5 = ke.judge_step(st, 5)
    assert not j5.performed and "unknown" in j5.not_judgeable
    assert ke.judge_run(run.kind, st.verdicts(),
                        {n: ke.judge_step(st, n) for n in run.steps},
                        run.steps) != "PASS"


def test_F2_legacy_kbd_hw_2_passes_only_on_an_assumed_poll():
    log = str(LOGS / "kbd-hw-2.session.log")
    s, = ke.replay_report([log], out=lambda *a: None)[log]
    assert s.results["fixed@48"] == "NOT JUDGEABLE"
    assert s.results["nojoymask@48"] == "DEFECT SHOWN"   # timing-free
    assert s.verdict == "INCOMPLETE"
    s, = ke.replay_report([log], out=lambda *a: None, assume_poll=0.1)[log]
    assert s.results["fixed@48"] == "PASS" and not s.proven
    assert s.verdict == "INCONCLUSIVE"


# decision (b): the poll period is logged and judged

def test_b_run_start_line_carries_the_poll(tmp_path):
    log = write_log(tmp_path, [
        "[   0.04] fixed@48 started (--poll 1s): operator, follow the C64 "
        "screen",
        "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
        "[   1.15] hb polls +1 max gap 1.15s",
        "[   4.00] fixed@48: INCOMPLETE (want PASS), STEPS None"])
    (run, st, _), = ke.replay_log(log)
    assert st.poll_s == 1.0
    assert st.period == pytest.approx(1.0 + ke.POLL_OVERHEAD)
    assert st.continuity.startswith("proven")


def _hose_rows(step, dt, idle=HELD):
    rows = [(idle, "", 0), (idle, "H", 1), (idle, "HO", 2), (idle, "HOS", 3),
            (idle, "HOSE", 4), (0xFF, "HOSE", 4)]
    st = ke.replay_stream()
    st.samples = [ke.Sample(dt * i, step, frozenset(), idl, nk, line)
                  for i, (idl, line, nk) in enumerate(rows)]
    return st


def test_b_sparse_poll_tapped_steps_of_a_fixed_run_are_not_judgeable():
    st = _hose_rows(5, 1.15)
    st.period = ke.poll_period(1.0)
    j = ke.judge_step(st, 5)
    assert not j.performed and "--poll" in j.not_judgeable
    ok = {n: ke.StepJudge(True, [], []) for n in (1, 2)}
    nj = {n: j for n in range(3, 10)}
    assert ke.judge_run(ke.FIXED, "P" * 9, {**ok, **nj}, range(1, 10)) == \
        "NOT JUDGEABLE"
    assert ke.judge_run(ke.FIXED, "PPPPFPPPP", {**ok, **nj},
                        range(1, 10)) == "FAIL"
    bad = {1: ke.StepJudge(True, ["with Q held, also down: x"], []), 2: ok[2]}
    assert ke.judge_run(ke.FIXED, "P" * 9, {**bad, **nj}, range(1, 10)) == \
        "FAIL"
    # the same letters at the default poll with the stick let go early:
    # operator error, judged
    st = _hose_rows(5, 0.25, idle=0xFF)
    j = ke.judge_step(st, 5)
    assert not j.performed and not j.not_judgeable


def test_b_leg_manual_logs_the_poll_and_judges_with_it(monkeypatch):
    from tests.run_program import Session
    lines = []

    class FS(ke.Stream):
        def __init__(self, sess, out=print):
            self.samples, self.bleeds, self.screen_log = [], [], []
            self.confirmed = set()

        def emit(self, line):
            lines.append(line)

        def poll(self, screen=True):
            return {}

        def hb_flush(self):
            pass

        def verdicts(self):
            return "P" * 9
    seen = []
    monkeypatch.setattr(ke, "stage_program", lambda *a: (0, {"stack_top": 0}))
    monkeypatch.setattr(ke, "Stream", FS)
    monkeypatch.setattr(ke, "start_async", lambda *a: None)
    monkeypatch.setattr(ke, "is_done", lambda s: True)
    res = types.SimpleNamespace(halt=1, reason=0, timeout=0, regs={},
                                print_state=lambda: None)
    monkeypatch.setattr(ke, "result", lambda s: res)
    real = ke.judge_step
    monkeypatch.setattr(ke, "judge_step", lambda st, n, control=False: (
        seen.append(st.period), real(st, n, control))[1])
    sess = Session(target=types.SimpleNamespace(backend="u64"), labels={},
                   timeout=1.0)
    got = ke.leg_manual(sess, b"", ke.Run("fixed", 48), 1, 1.0, 1.0)
    assert any(" started (--poll 1s):" in l for l in lines)
    assert set(seen) == {ke.poll_period(1.0)}
    assert got == "INCOMPLETE"          # no samples at all: nothing done


# F4: a control's signature is a fact, whatever the polling

def _sparse_nosettle(poll="", gap="1.14"):
    lines = later_nosettle_session(
        start=f"started (--poll {poll}s)" if poll else "started")[1:-1]
    # holds of about 4 s (the sparse protocol), one heartbeat
    lines = [l.replace("[   9.40] key UP   c6r2 ;", "[  11.00] key UP   c6r2 ;")
             .replace("[  10.00] key DOWN c6r7 /", "[  11.50] key DOWN c6r7 /")
             .replace("[  12.40] key UP   c6r7 /", "[  15.50] key UP   c6r7 /")
             .replace("[  13.00]", "[  16.00]").replace("[  13.10]",
                                                        "[  16.10]")
             for l in lines]
    return lines[:6] + [f"[   2.40] hb polls +2 max gap {gap}s"] + lines[6:]


@pytest.mark.parametrize("poll,gap", [("", "1.14"), ("1", "1.14"),
                                      ("1", "3.00"), ("0.1", "1.14")])
def test_F4_a_sparse_or_broken_heartbeat_keeps_a_control_signature(
        tmp_path, poll, gap):
    log = write_log(tmp_path, _sparse_nosettle(poll, gap))
    s, = ke.replay_report([log], out=lambda *a: None)[log]
    assert s.results["nosettle@48"] == "DEFECT SHOWN"


def test_F4_a_broken_heartbeat_never_passes_a_fixed_run(tmp_path):
    src = (LOGS / "kbd-hw-2.session.log").read_text().splitlines()
    first = next(i for i, l in enumerate(src) if " fixed@48 started:" in l)
    log = write_log(tmp_path, src[:first + 1] +
                    ["[   0.50] hb polls +4 max gap 2.10s"] + src[first + 1:])
    s, = ke.replay_report([log], out=lambda *a: None)[log]
    assert s.results["fixed@48"] == "NOT JUDGEABLE"


# F3 + H1: the live judge bounds poll gaps too

def test_F3_a_hold_is_not_credited_across_a_stall():
    Q = ke.K("q")
    st = ke.replay_stream()
    st.samples = [ke.Sample(0.0, 1, frozenset(), 0xFF, 0, ""),
                  ke.Sample(0.25, 1, frozenset({Q}), 0xFF, 1, "Q"),
                  ke.Sample(2.25, 1, frozenset({Q}), 0xFF, 1, "Q"),  # stall
                  ke.Sample(2.50, 1, frozenset(), 0xFF, 1, "Q")]
    j = ke.judge_step(st, 1)
    assert not j.performed and "gap" in j.not_judgeable
    st.samples[2:2] = [ke.Sample(0.25 * i, 1, frozenset({Q}), 0xFF, 1, "Q")
                       for i in range(2, 9)]
    assert ke.judge_step(st, 1).performed


def test_F3_H1_live_stream_tracks_gaps_and_flushes_the_last_window(
        monkeypatch):
    logged = []
    monkeypatch.setattr(ke, "_log_only", logged.append)
    st, _ = live_stream([state()] * 12, step=1, t0=5.0, jumps={6: 1.75})
    assert st.gap_worst == pytest.approx(2.0)
    n_before = len(logged)
    st.hb_flush()
    assert len(logged) == n_before + 1 and "hb polls +" in logged[-1]
    total = sum(int(ke.HB_LINE.match(ke.LOG_LINE.match(l)[2])[1])
                for l in logged)
    assert total == 12


# F5: the "nothing" form and the raw diagnostic

def test_F5_raw_clearing_is_logged():
    Q = ke.K("q")
    st, out = live_stream([state(), state(set(), 0, raw={Q}), state(),
                           state(set(), 0, raw={Q})], step=1)
    raws = [l for l in out if l.startswith("raw")]
    assert raws == ["raw (not debounced yet): c7r6 Q",
                    "raw (not debounced yet): none",
                    "raw (not debounced yet): c7r6 Q"]


def _nothing_form(raw_seen=True, confirmed=False, control=True):
    Q, UP = ke.K("q"), ke.K("^")
    raw = (lambda i: {Q} | ({UP} if i % 2 else set())) if raw_seen else \
        (lambda i: set())
    states = [state()] + [state(set(), 0, raw=raw(i)) for i in range(12)] + \
        [state()]
    st, out = live_stream(states, step=1)
    if confirmed:
        st.confirmed = {1}
    return st, out, ke.judge_step(st, 1, control=control)


def test_F5_fixed_build_raw_only_hold_is_a_defect():
    st, out, j1 = _nothing_form(control=False)
    assert j1.defects and "raw" in j1.defects[0]


def test_F5_replay_rebuilds_raw_and_reads_the_confirmation(tmp_path):
    base = [
        "[   0.04] nosettle@48 started (--poll 0.1s): operator, follow the "
        "C64 screen",
        "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
        "[   2.00] raw (not debounced yet): c6r6 UPARROW, c7r6 Q",
        "[   2.25] raw (not debounced yet): c7r6 Q",
        "[   5.00] raw (not debounced yet): none",
        "[   6.00]   | > ", "[   6.00]   | STEP 1 SKIP",
        "[   6.00]   | STEP 2/9: HOLD ; AND /", "[   6.00]   | >",
        "[   7.00] key DOWN c6r2 ;", "[   9.40] key UP   c6r2 ;",
        "[  10.00] key DOWN c6r7 /", "[  12.40] key UP   c6r7 /",
        "[  13.00]   | STEP 2 PASS", "[  13.00]   | STEPS SP-------"]
    hb = [f"[{k + 0.1:8.2f}] hb polls +4 max gap 0.26s" for k in range(1, 13)]
    base = sorted(base + hb, key=lambda l: float(ke.LOG_LINE.match(l)[1]))
    end = ["[  13.20] nosettle@48: INCOMPLETE (want DEFECT SHOWN), STEPS "
           "SP-------"]
    Q = ke.K("q")
    (run, st, _), = ke.replay_log(write_log(tmp_path, base + end, "a.log"))
    by_t = {round(s.t, 2): s for s in st.samples}
    assert Q in by_t[2.25].raw and Q not in by_t[5.0].raw
    # fix6 R1: raw-only Q for 3 s is the instrument's evidence; the answer
    # (none, yes, or the fix5 "confirms" form) only vetoes it when "no"
    for name, extra, want in (
            ("a.log", [], "DEFECT SHOWN"),
            ("b.log", ["[  13.10] operator confirms: step 1 held as asked "
                       "(Q)"], "DEFECT SHOWN"),
            ("c.log", ["[  13.10] operator: step 1 held as asked (Q)? yes"],
             "DEFECT SHOWN"),
            # fix7 K5: a "no" never changes an instrument verdict
            ("d.log", ["[  13.10] operator: step 1 held as asked (Q)? no"],
             "DEFECT SHOWN")):
        log = write_log(tmp_path, base + extra + end, name)
        s, = ke.replay_report([log], out=lambda *a: None)[log]
        assert s.results["nosettle@48"] == want, name


# F6: a rejected gain does not slide

def test_F6_no_credit_on_an_interval_where_the_line_did_not_change():
    rows = [(HELD, "", 0), (HELD, "HOSE", 0),        # mirror artefact
            (HELD, "HOSE", 0), (HELD, "HOSE", 4),    # X DEL X DEL
            (HELD, "HOSE", 4)]
    ss = [ke.Sample(0.25 * i, 5, frozenset(), idle, nk, line)
          for i, (idle, line, nk) in enumerate(rows)]
    assert ke._echo_idx(ss) == []


# D1: the documented poll period

def test_D1_doc_gives_the_measured_poll_period():
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    assert "0.12 s" not in doc and "0.25 s" in doc


# fix5 mutant kills (fix5/mutants.txt first pass: gap_nobound,
# confirm_ask_ignored, nothing_any_verdict survived)

def test_mods_echo_credit_is_refused_across_a_gap_over_0_4_s():
    """No continuity rule covers step 4, so the echo gap bound alone keeps
    a letter echoed across a 0.5 s gap from being credited."""
    R, C = ke.RSHIFT, ke.CBM
    rows = [(0xFF, "", 0, (R,)), (0xFF, "A", 1, (R,)), (0xFF, "A", 1, (R,)),
            (0xFF, "A?", 2, (R,)), (0xFF, "A?", 2, (C,)),
            (0xFF, "A?Q", 3, (C,)), (0xFF, "A?Q", 3, ())]
    assert ke.judge_step(st_of(rows, step=4, dt=0.2), 4).performed
    assert not ke.judge_step(st_of(rows, step=4, dt=0.5), 4).performed




# --- fix6 (fix5 review, build/evidence/w6-kbd-review5/REVIEW.md) -------------
# Adopted inverted from test_fix5_attacks.py where noted.  Each fails on
# 8df7fa3 by its behaviour (build/evidence/w6-kbd/fix6/red), except the
# guards marked "guard", which pin what must stay clean.

UPK = ke.K("^")


def fstate(keys=(), nkey=0, idle=0xFF, raw=None, ras=None):
    s = state(keys, nkey, idle, raw)
    if ras is not None:
        s["ras"] = ras
    return s


def delayed_commit_states():
    """Q raw-only for 3 s (flickering with the UPARROW phantom: the
    debounce keeps restarting), then committed alone."""
    return [state()] + [state(set(), 0, raw={Q_, UPK} if i % 2 else {Q_})
                        for i in range(12)] + \
        [state({Q_}, 1)] * 8 + [state(set(), 1)]


Q_ = ke.K("q")


def ctl(st, answer=None):
    if answer is not None:
        (st.confirmed if answer else st.denied).add(1)
    j1 = ke.judge_step(st, 1, control=True)
    return j1, ke.judge_run(ke.CONTROL, "SP-------",
                            {1: j1, 2: ke.StepJudge(True, [], [])},
                            range(1, 3))


# R1: the operator's "y" alone never scores

def test_R1_confirmation_alone_is_not_judgeable():
    """review5 test_R1_confirmation_alone…, inverted: raw is known and
    saw no Q; the operator says held -> NOT JUDGEABLE, not DEFECT SHOWN."""
    st, out = live_stream([state()] * 14, step=1)
    j1, got = ctl(st, answer=True)
    assert not j1.signature and "saw no" in j1.not_judgeable
    assert got == "NOT JUDGEABLE"


def test_R1_a_one_poll_raw_tap_is_not_judgeable_and_not_never():
    """review5 test_R1_…tap…, inverted; and R6: one raw poll is not
    'raw never showed it'."""
    st, out = live_stream([state()] * 4 + [state(set(), 0, raw={Q_})] +
                          [state()] * 9, step=1)
    j1, got = ctl(st, answer=True)
    assert got == "NOT JUDGEABLE" and not j1.signature
    assert "raw showed Q at 1 poll" in j1.not_judgeable
    assert "never" not in j1.not_judgeable


def test_R1_replay_of_a_proven_log_needs_raw_evidence(tmp_path):
    """review5 test_R1_replay…, inverted."""
    lines = [
        "[   0.04] nosettle@48 started (--poll 1s): operator, follow the C64 "
        "screen",
        "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
        "[   8.00]   | > ", "[   8.00]   | STEP 1 SKIP",
        "[   8.00]   | STEP 2/9: HOLD ; AND /", "[   8.00]   | >",
        "[   9.15] key DOWN c6r2 ;", "[  12.60] key UP   c6r2 ;",
        "[  13.75] key DOWN c6r7 /", "[  17.20] key UP   c6r7 /",
        "[  18.35]   | STEP 2 PASS", "[  18.35]   | STEPS SP-------"]
    hb = [f"[{k + 0.5:8.2f}] hb polls +1 max gap 1.15s" for k in range(1, 18)]
    body = sorted(lines + hb, key=lambda l: float(ke.LOG_LINE.match(l)[1]))
    log = write_log(tmp_path, [
        "[10:00:00] kbd_echo MANUAL on 192.0.2.1, runs nosettle@48, "
        "poll 1s, prg x"] + body +
        ["[  18.40] operator confirms: step 1 held as asked (Q)",
         "[  18.50] nosettle@48: DEFECT SHOWN (want DEFECT SHOWN), STEPS "
         "SP-------"])
    s, = ke.replay_report([log], out=lambda *a: None)[log]
    assert s.proven and s.results["nosettle@48"] == "NOT JUDGEABLE"


def test_R1_raw_only_hold_scores_without_the_answer_and_no_vetoes_it():
    raw = [state()] + [state(set(), 0, raw={Q_} | ({UPK} if i % 2 else set()))
                       for i in range(12)] + [state()]
    j1, got = ctl(live_stream(raw, step=1)[0])
    assert got == "DEFECT SHOWN" and "raw-only" in j1.signature[0]
    j1, got = ctl(live_stream(raw, step=1)[0], answer=True)
    assert got == "DEFECT SHOWN"
    j1, got = ctl(live_stream(raw, step=1)[0], answer=False)
    assert got == "DEFECT SHOWN"          # fix7 K5: "no" changes nothing


def test_R1_row6_idle_low_is_noted_never_scored():
    """review5 test_R1_row6…: r6 low at idle with Q never in raw at all is
    no raw-only evidence: a "yes" is NOT JUDGEABLE, never a signature.
    (fix7 K3: r6 low at idle IS settle evidence, but only next to a
    debounce that kept restarting; see test_K3_*.)"""
    r6 = 0xFF & ~(1 << 6)
    st, out = live_stream([state()] + [state(idle=r6)] * 12 + [state()],
                          step=1)
    j1, got = ctl(st, answer=True)
    assert got == "NOT JUDGEABLE" and not j1.signature
    assert "saw no Q" in j1.not_judgeable


def test_R1_a_log_without_raw_scores_nothing_on_the_answer(tmp_path):
    lines = ["[   0.04] nosettle@48 started: operator, follow the C64 screen",
             "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
             "[   6.00]   | > ", "[   6.00]   | STEP 1 SKIP",
             "[   6.00]   | STEP 2/9: HOLD ; AND /", "[   6.00]   | >",
             "[   7.00] key DOWN c6r2 ;", "[   9.40] key UP   c6r2 ;",
             "[  10.00] key DOWN c6r7 /", "[  12.40] key UP   c6r7 /",
             "[  13.00]   | STEP 2 PASS", "[  13.00]   | STEPS SP-------",
             "[  13.10] operator confirms: step 1 held as asked (Q)",
             "[  13.20] nosettle@48: DEFECT SHOWN (want DEFECT SHOWN), STEPS "
             "SP-------"]
    log = write_log(tmp_path, lines)
    s, = ke.replay_report([log], out=lambda *a: None, assume_poll=0.1)[log]
    assert s.results["nosettle@48"] == "NOT JUDGEABLE"


# R2: the delayed commit

def test_R2_fixed_build_delayed_commit_is_a_fail():
    """review5 test_R2_fixed…, inverted."""
    st, _ = live_stream(delayed_commit_states(), step=1)
    j1 = ke.judge_step(st, 1)
    assert any("delayed commit" in d for d in j1.defects)
    judges = {n: ke.StepJudge(True, [], []) for n in range(1, 10)}
    judges[1] = j1
    assert ke.judge_run(ke.FIXED, "P" * 9, judges, range(1, 10)) == "FAIL"


def test_R2_nosettle_delayed_commit_is_the_signature():
    """review5 test_R2_nosettle…, inverted."""
    st, _ = live_stream(delayed_commit_states(), step=1)
    j1 = ke.judge_step(st, 1, control=True)
    assert any("delayed commit" in s for s in j1.signature)
    assert ke.judge_run(ke.CONTROL, "PP-------",
                        {1: j1, 2: ke.StepJudge(True, [], [])},
                        range(1, 3)) == "DEFECT SHOWN"


def test_R2_guard_a_press_seen_raw_only_at_one_poll_is_clean():
    """guard: a poll that lands inside the 4 ms debounce window sees Q
    raw-only once; the next poll has it committed."""
    st, _ = live_stream([state(), state(set(), 0, raw={Q_})] +
                        [state({Q_}, 1)] * 10 + [state(set(), 1)], step=1)
    j1 = ke.judge_step(st, 1)
    assert j1.performed and not j1.defects


def test_R2_guard_replay_within_one_poll_is_no_delay(tmp_path):
    """guard: in a replay one poll's state line and screen line are about
    0.12 s apart; a raw-only Q seen at the state read and committed before
    the next poll is not a delayed commit (DELAY_MIN 0.2 s)."""
    lines = ["[   0.04] fixed@48 started (--poll 0.1s): operator, follow the "
             "C64 screen",
             "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
             "[   2.00] raw (not debounced yet): c7r6 Q",
             "[   2.12]   | > Q",
             "[   2.25] key DOWN c7r6 Q", "[   2.25] raw (not debounced yet): "
             "none", "[   5.00] key UP   c7r6 Q",
             "[   5.50]   | STEP 1 PASS",
             "[   6.00] fixed@48: INCOMPLETE (want PASS), STEPS None"]
    hb = [f"[{k + 0.1:8.2f}] hb polls +4 max gap 0.26s" for k in range(1, 6)]
    log = write_log(tmp_path, sorted(lines + hb, key=lambda l: float(
        ke.LOG_LINE.match(l)[1])))
    (run, st, _), = ke.replay_log(log)
    j1 = ke.judge_step(st, 1)
    assert j1.performed and not j1.defects, (j1.notes, j1.defects)


# R3: between --poll 0.1 and 0.25

def test_R3_gaps_over_0_4_s_make_a_tapped_step_not_judgeable():
    """review5 test_R3_…, inverted."""
    rows = [(HELD, "", 0), (HELD, "H", 1), (HELD, "HO", 2), (HELD, "HOS", 3),
            (HELD, "HOSE", 4), (0xFF, "HOSE", 4)]
    st = ke.replay_stream()
    st.period = ke.poll_period(0.2)
    st.samples = [ke.Sample(0.42 * i, 5, frozenset(), idl, nk, line)
                  for i, (idl, line, nk) in enumerate(rows)]
    j = ke.judge_step(st, 5)
    assert not j.performed and "0.42" in j.not_judgeable


# R5: one log per session, for real

def test_R5_tee_refuses_an_existing_log(tmp_path):
    """review5 test_R5_tee…, inverted."""
    p = tmp_path / "session.log"
    p.write_text("OLD SESSION\n")
    with pytest.raises(FileExistsError):
        ke.Tee(str(p), sys.__stdout__)
    assert p.read_text() == "OLD SESSION\n"


def test_R5_default_out_dir_is_unique():
    """review5 test_R5_default…, inverted."""
    assert ke.default_out_dir() != ke.default_out_dir()


def test_R5_a_non_empty_out_dir_is_refused(tmp_path, monkeypatch):
    prg = tmp_path / "r.prg"
    prg.write_bytes(b"\x01\x08")
    (tmp_path / "r.lbl").write_text("")
    out = tmp_path / "ev"
    out.mkdir()
    (out / "config_before.json").write_text("{}")   # a crashed session's
    monkeypatch.setenv("U64_HOST", "192.0.2.1")
    monkeypatch.setattr(ke, "run_u64",
                        lambda *a: pytest.fail("must not start"))
    assert ke.main(["--prg", str(prg), "--backend", "u64",
                    "--out-dir", str(out)]) == 2
    assert (out / "config_before.json").read_text() == "{}"


# R6: the question is asked for every nosettle hold step, neutrally

def _leg(monkeypatch, samples, verdicts, run):
    from tests.run_program import Session
    lines, asked = [], []

    class FS(ke.Stream):
        def __init__(self, sess, out=print):
            self.samples, self.bleeds = samples, []
            self.screen_log = []
            self.confirmed, self.denied = set(), set()

        def emit(self, line):
            lines.append(line)

        def poll(self, screen=True):
            return {}

        def hb_flush(self):
            pass

        def verdicts(self):
            return verdicts
    monkeypatch.setattr(ke, "stage_program", lambda *a: (0, {"stack_top": 0}))
    monkeypatch.setattr(ke, "control_args", lambda b: b"\n")
    monkeypatch.setattr(ke, "Stream", FS)
    monkeypatch.setattr(ke, "start_async", lambda *a: None)
    monkeypatch.setattr(ke, "is_done", lambda s: True)
    res = types.SimpleNamespace(halt=1, reason=0, timeout=0, regs={},
                                print_state=lambda: None)
    monkeypatch.setattr(ke, "result", lambda s: res)
    sess = Session(target=types.SimpleNamespace(backend="u64"), labels={},
                   timeout=1.0)
    got = ke.leg_manual(sess, b"", run, 1, 0.1, 1.0,
                        ask=lambda q: asked.append(q) or True)
    return got, lines, asked


def _clean_holds():
    Sm, SL = ke.K(";"), ke.K("/")
    return ([ke.Sample(0.25 * i, 1, frozenset({Q_}), 0xFF, 1)
             for i in range(9)] +
            [ke.Sample(4.0 + 0.25 * i, 2, frozenset({Sm}), 0xFF, 2)
             for i in range(9)] +
            [ke.Sample(7.0 + 0.25 * i, 2, frozenset({SL}), 0xFF, 3)
             for i in range(9)])


def test_R6_every_nosettle_hold_step_is_asked_whatever_the_outcome(
        monkeypatch):
    got, lines, asked = _leg(monkeypatch, _clean_holds(), "PP-------",
                             ke.Run("nosettle", 48))
    assert got == "INCONCLUSIVE"
    assert [q.split(":")[0] for q in asked] == \
        ["nosettle@48 step 1", "nosettle@48 step 2"]
    assert not any("never" in q or "committed" in q for q in asked)
    assert "operator: step 1 held as asked (Q)? yes" in lines
    got, lines, asked = _leg(monkeypatch, _clean_holds(), "P" * 9,
                             ke.Run("fixed", 48))
    assert asked == []


def test_R6_a_no_answer_is_replayed_as_a_denial(tmp_path):
    st = ke._replay_run(ke.LoggedRun(
        ke.Run("nosettle", 48), 0.1, True, 0.0,
        [(1.0, "operator: step 1 held as asked (Q)? no"),
         (1.1, "operator: step 2 held as asked (;, /)? yes")], "INCOMPLETE"),
        None)
    assert st.denied == {1} and st.confirmed == {2}


# hle_con_ras: the stream proves the scanner was sampling

def test_ras_liveness_is_streamed_and_replayed(tmp_path):
    rs = [10, 20, 30, 30, 30, 30, 40, 50]
    st, out = live_stream([fstate(ras=r) for r in rs], step=1)
    lv = [l for l in out if l.startswith("scanner:")]
    assert lv == ["scanner: not sampling (hle_con_ras unchanged over 3 "
                  "polls)", "scanner: sampling"]
    assert [s.live for s in st.samples] == [None, None, True, True, False,
                                            False, True, True]
    assert "hle_con_ras" in ke.STREAM_LABELS


# the one-row-per-run replay summary

def test_replay_prints_one_summary_row_per_run(tmp_path):
    log = str(LOGS / "kbd-hw-2.session.log")
    out = []
    ke.replay_report([log], out=out.append, assume_poll=0.1)
    i = next(i for i, l in enumerate(out) if l.startswith("SUMMARY"))
    rows = out[i + 2:i + 6]
    assert [r.split()[1] for r in rows] == ["fixed@48", "nosettle@48",
                                            "nojoymask@48", "fixed@1"]
    assert "clean commit" in rows[1]           # step 1 of nosettle@48


def test_ras_liveness_is_replayed_and_named_in_a_not_judgeable(tmp_path):
    """fix6 mutant kill (live_replay_off): the replay rebuilds Sample.live
    from the scanner lines, and a 'yes' over a dead scanner says so."""
    lines = ["[   0.04] nosettle@48 started (--poll 0.1s): operator, follow "
             "the C64 screen",
             "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
             "[   2.00] scanner: not sampling (hle_con_ras unchanged over 3 "
             "polls)", "[   5.00] scanner: sampling",
             "[   6.00]   | > ", "[   6.00]   | STEP 1 SKIP",
             "[   6.00]   | STEP 2/9: HOLD ; AND /", "[   6.00]   | >",
             "[   7.00] key DOWN c6r2 ;", "[   9.40] key UP   c6r2 ;",
             "[  10.00] key DOWN c6r7 /", "[  12.40] key UP   c6r7 /",
             "[  13.00]   | STEP 2 PASS", "[  13.00]   | STEPS SP-------",
             "[  13.10] operator: step 1 held as asked (Q)? yes",
             "[  13.20] nosettle@48: NOT JUDGEABLE (want DEFECT SHOWN), "
             "STEPS SP-------"]
    hb = [f"[{k + 0.1:8.2f}] hb polls +4 max gap 0.26s" for k in range(1, 13)]
    log = write_log(tmp_path, sorted(lines + hb, key=lambda l: float(
        ke.LOG_LINE.match(l)[1])))
    (run, st, _), = ke.replay_log(log)
    by_t = {round(s.t, 2): s for s in st.samples}
    assert by_t[1.0].live is None and by_t[3.0].live is False
    assert by_t[5.0].live is True
    j1 = ke.judge_step(st, 1, control=True)
    assert "not sampling" in j1.not_judgeable


# --- fix7 (fix6 review, build/evidence/w6-kbd-review6/REVIEW.md) -------------
# The review's attacks (test_fix6_attacks.py) adopted inverted, at the
# A/B's --poll 1.0; its guards G1, G2 adopted as they are.

P1 = ke.poll_period(1.0)                 # 1.15 s
R6LOW = 0xFF & ~(1 << 6)
SEMI_, SLASH_ = ke.K(";"), ke.K("/")


def live1(states, step):
    """live_stream at --poll 1.0, the judge told so."""
    st, out = live_stream(states, step=step, dt=P1)
    st.period = P1
    return st, out


def ctl7(j1, v="SP-------", j2=None):
    return ke.judge_run(ke.CONTROL, v, {1: j1, 2: j2 or ke.StepJudge(
        True, [], [])}, range(1, 3))


def fixed7(j1, v="PPSSSSSSS"):
    js = {n: ke.StepJudge(False, [], [], [], "period") for n in range(3, 10)}
    js.update({1: j1, 2: ke.StepJudge(True, [], [])})
    return ke.judge_run(ke.FIXED, v, js, range(1, 10))


def row7(st, run, got):
    st.continuity = "proven (test)"
    return ke.step1_row(st, run, got, 1, "INCOMPLETE")


def test_G1_one_poll_in_the_debounce_window_is_not_a_delay():
    """review6 guard G1, adopted."""
    st, _ = live1([state(), state(set(), 0, raw={Q_})] + [state({Q_}, 1)] * 4
                  + [state(set(), 1)], step=1)
    j1 = ke.judge_step(st, 1)
    assert j1.performed and not j1.defects, j1.notes
    assert ke._raw_only([s for s in st.samples if s.step == 1], Q_,
                        ke.gap_max(P1)) == (0.0, 1)


def test_G2_replay_worst_single_sighting_stays_below_delay_min(tmp_path):
    """review6 guard G2, adopted: fill + the state/screen split give at
    most GAP_SLACK (0.15 s) < DELAY_MIN (0.2 s)."""
    lines = ["[   0.04] fixed@48 started (--poll 1s): operator, follow the "
             "C64 screen",
             "[   1.00]   | STEP 1/9: HOLD Q", "[   1.00]   | >",
             "[   2.00] raw (not debounced yet): c7r6 Q",
             "[   2.14]   | > q",
             "[   3.29] key DOWN c7r6 Q",
             "[   3.29] raw (not debounced yet): none",
             "[   7.90] key UP   c7r6 Q", "[   9.00]   | STEP 1 PASS",
             "[   9.10] fixed@48: INCOMPLETE (want PASS), STEPS None"]
    hb = [f"[{k + 0.5:8.2f}] hb polls +1 max gap 1.29s" for k in range(1, 9)]
    log = write_log(tmp_path, sorted(lines + hb, key=lambda l: float(
        ke.LOG_LINE.match(l)[1])))
    (run, st, _), = ke.replay_log(log)
    assert st.continuity.startswith("proven")
    ss = [s for s in st.samples if s.step == 1]
    raw_s, _ = ke._raw_only(ss, Q_, ke.gap_max(P1))
    assert 0.14 <= raw_s < ke.DELAY_MIN < ke.GAP_SLACK + 0.1
    j1 = ke.judge_step(st, 1)
    assert j1.performed and not j1.defects
    assert "clean commit" in row7(st, ke.Run("fixed", 48), "x")


# K1: only settle-specific evidence is a settle signature

def test_K1_A4_chatter_without_settle_evidence_is_not_a_signature():
    """review6 A4, inverted: Q raw-only at two polls, never with UPARROW
    in raw, no r6 low at idle, then committed: a chattering switch looks
    the same in either build.  nosettle: NOT JUDGEABLE ('defect-like, not
    settle-specific'), never DEFECT SHOWN; fixed: FAIL, said to be the
    keyboard/debounce, not the settle."""
    chat = [state()] + [state(set(), 0, raw={Q_})] * 2 + \
        [state({Q_}, 1)] * 3 + [state(set(), 1)]
    st, _ = live1(chat, step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert not jc.signature and not jc.settle
    assert "not settle-specific" in jc.not_judgeable
    assert ctl7(jc, "PP-------") == "NOT JUDGEABLE"
    jf = ke.judge_step(st, 1)
    assert fixed7(jf) == "FAIL" and not jf.settle
    assert any("not settle-specific" in d for d in jf.defects)


def test_K1_the_c_minus_1_key_in_raw_makes_it_settle_specific():
    """The same two raw-only polls with UPARROW (c6r6, Q's c-1) in raw:
    the settle mechanism's own trace.  nosettle DEFECT SHOWN (settle), the
    fixed build FAIL with a settle signature."""
    st, _ = live1([state()] + [state(set(), 0, raw={Q_, UPK})] * 2 +
                  [state({Q_}, 1)] * 3 + [state(set(), 1)], step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert ctl7(jc, "PP-------") == "DEFECT SHOWN"
    assert any("settle-specific" in x and "UPARROW" in x
               for x in jc.signature)
    jf = ke.judge_step(st, 1)
    assert fixed7(jf) == "FAIL"
    assert any("settle-specific" in d for d in jf.defects)
    assert jc.settle and jf.settle


# K2: the same raw-only span, committed or not

def test_K2_A2_never_committed_two_raw_only_polls_is_judged_like_a_delay():
    """review6 A2, inverted: two raw-only polls (1.15 s) with the
    settle trace (UPARROW in raw), never committed: the fixed build FAILs
    and nosettle shows the signature, exactly as when it commits."""
    never = [state()] + [state(set(), 0, raw={Q_, UPK})] * 2 + [state()] * 3
    st, _ = live1(never, step=1)
    jf = ke.judge_step(st, 1)
    assert jf.defects and fixed7(jf, "SPSSSSSSS") == "FAIL"
    jc = ke.judge_step(st, 1, control=True)
    assert jc.settle and ctl7(jc) == "DEFECT SHOWN"
    assert "settle: nothing" in row7(st, ke.Run("nosettle", 48), "x")


# K3: the idle-mask flicker is settle evidence

def test_K3_A3_idle_mask_flicker_scores_in_nosettle():
    """review6 A3, inverted: {Q, UP} / {} with r6 low at idle,
    alternating, never committed.  Raw-only polls are counted, not only
    consecutive seconds, for a key never committed: 2 polls with the
    settle trace -> DEFECT SHOWN."""
    flick = [state()] + [state(set(), 0, raw={Q_, UPK}) if i % 2 == 0 else
                         state(set(), 0, idle=R6LOW, raw=set())
                         for i in range(5)] + [state()]
    st, out = live1(flick, step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert ctl7(jc) == "DEFECT SHOWN"
    assert any("r6 low" in x for x in jc.signature)


def test_K3_row6_low_at_idle_alone_is_settle_evidence_for_q():
    """Only the fixed build's settle before column 6 waits for row 6 after
    Q's column 7; nosettle reads the next idle with row 6 maybe still low.
    r6 is no port-1 joystick row (r0-r4).  Q never debounced, raw-only at
    2 polls, r6 low at idle: settle-specific."""
    st, _ = live1([state()] + [state(set(), 0, raw={Q_}),
                               state(set(), 0, idle=R6LOW, raw=set()),
                               state(set(), 0, raw={Q_})] + [state()] * 2,
                  step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert ctl7(jc) == "DEFECT SHOWN"
    assert any("r6 low" in x for x in jc.signature)


def test_K3_a_joystick_row_low_at_idle_is_not_settle_evidence():
    """guard: ; is c6r2: row 2 is a port-1 joystick line (LEFT), so r2 low
    at idle proves nothing about the settle."""
    r2 = 0xFF & ~(1 << 2)
    st, _ = live1([state()] + [state(set(), 0, raw={SEMI_}),
                               state(set(), 0, idle=r2, raw=set()),
                               state(set(), 0, raw={SEMI_})] + [state()] * 2,
                  step=2)
    jc = ke.judge_step(st, 2, control=True)
    assert not jc.signature and ctl7(ke.StepJudge(True, [], []), "PP-------",
                                     jc) != "DEFECT SHOWN"


def test_K3_the_doc_no_longer_says_both_builds():
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    src = Path(ke.__file__).read_text()
    for text in (doc, src):
        assert "both builds read the idle mask before any settle" not in text
        assert "both builds read idle before any settle" not in text


# K4: a signature is never buried by another step

def test_K4_A6_a_phantom_survives_an_undone_step_2():
    """review6 A6, inverted."""
    st, _ = live1([state(), state({Q_, UPK}, 2)] + [state({Q_, UPK}, 2)] * 3 +
                  [state(set(), 2)], step=1)
    j1 = ke.judge_step(st, 1, control=True)
    s2, _ = live1([state()] + [state({SEMI_}, 1)] * 2 + [state(set(), 1)] +
                  [state({SLASH_}, 2)] * 2 + [state(set(), 2)], step=2)
    j2 = ke.judge_step(s2, 2, control=True)
    assert not j2.performed
    assert ctl7(j1, "FP-------", j2) == "DEFECT SHOWN"


# K5: the answer never changes an instrument verdict

@pytest.mark.parametrize("raw", [{Q_}, {Q_, UPK}])
def test_K5_A5_no_answer_changes_any_form(raw):
    """review6 A5, inverted: 'no' (or 'yes') changes nothing where the
    instrument saw a raw-only Q, whether the form is settle-specific or
    not, committed or not."""
    for tail in ([state()] * 2, [state({Q_}, 1)] * 3 + [state(set(), 1)]):
        base = [state()] + [state(set(), 0, raw=raw)] * 3 + tail
        got = set()
        for answer in (None, True, False):
            st, _ = live1(base, step=1)
            if answer is not None:
                (st.confirmed if answer else st.denied).add(1)
            got.add(ctl7(ke.judge_step(st, 1, control=True),
                         "SP-------" if len(tail) == 2 else "PP-------"))
        assert len(got) == 1, (raw, got)


# K6: a dead scanner gates the verdict

def test_K6_A8_a_dead_scanner_is_not_judgeable_and_shown():
    """review6 A8, inverted: fixed build, hle_con_ras frozen through step
    1, Q never seen: NOT JUDGEABLE 'scanner not sampling' (the A/B stops
    on it), not 'Q never pressed'; SUMMARY shows the dead polls."""
    dead, out = live1([fstate(ras=7)] * 6, step=1)
    j = ke.judge_step(dead, 1)
    assert not j.performed and "not sampling" in j.not_judgeable
    r = row7(dead, ke.Run("fixed", 48), "NOT JUDGEABLE")
    assert "scanner dead" in r
    alive, _ = live1([fstate(ras=r) for r in (1, 2, 3, 4, 5, 6)], step=1)
    j = ke.judge_step(alive, 1)
    assert not j.not_judgeable


# D1 / D2 / D3 and the A/B reading rule

def _hold(keys_by_step, n_polls=5):
    """A clean nosettle/fixed step-1 and step-2 stream at --poll 1.0."""
    out = {}
    for n, keys in keys_by_step.items():
        states = [state()]
        nk = 0
        for k in keys:
            nk += 1
            states += [state({k}, nk)] * n_polls + [state(set(), nk)]
        out[n] = live1(states, step=n)[0]
    return out


def _judges(streams, control):
    return {n: ke.judge_step(st, n, control=control)
            for n, st in streams.items()}


def _clean(control, q_states=None):
    ss = _hold({1: [Q_], 2: [SEMI_, SLASH_]})
    if q_states is not None:
        ss[1] = live1(q_states, step=1)[0]
    return _judges(ss, control)


PHANTOM = [state()] + [state({Q_, UPK}, 2)] * 5 + [state(set(), 2)]
CHATTER = [state()] + [state(set(), 0, raw={Q_})] * 2 + [state({Q_}, 1)] * 4 \
    + [state(set(), 1)]


def pair(i, fixed=None, nosettle=None, poll=1.0, proven=True):
    return ke.ABPair(f"pair{i}", poll, proven,
                     fixed or _clean(False), nosettle or _clean(True))


def test_ab_necessary_needs_a_settle_specific_signature():
    ab = ke.ab_verdict([pair(1, nosettle=_clean(True, PHANTOM))] +
                       [pair(i) for i in range(2, 6)])
    assert ab.startswith("NECESSARY") and "pair1" in ab


def test_ab_A4_chatter_never_reads_as_necessary():
    ab = ke.ab_verdict([pair(1, nosettle=_clean(True, CHATTER))] +
                       [pair(i) for i in range(2, 6)])
    assert not ab.startswith("NECESSARY")
    assert "not settle-specific" in ab


def test_ab_not_reproduced_needs_five_kept_pairs_and_states_the_limits():
    ab = ke.ab_verdict([pair(i) for i in range(1, 6)])
    assert ab.startswith("NOT REPRODUCED")
    assert "45%" in ab and "1.15" in ab
    ab = ke.ab_verdict([pair(i) for i in range(1, 5)])
    assert ab.startswith("REDO")


def test_ab_A7_a_tapped_fixed_q_discards_the_pair():
    """review6 A7, inverted: the fixed partner must HOLD Q (performed),
    not tap it."""
    tap = [state(), state({Q_}, 1), state(set(), 1)] + [state()] * 3
    ab = ke.ab_verdict([pair(1, fixed=_clean(False, tap))] +
                       [pair(i) for i in range(2, 6)])
    assert ab.startswith("REDO") and "pair1" in ab


def test_ab_a_settle_signature_in_the_fixed_build_stops():
    ab = ke.ab_verdict([pair(1, fixed=_clean(False, PHANTOM))] +
                       [pair(i) for i in range(2, 6)])
    assert ab.startswith("STOP") and "pair1" in ab


def test_ab_a_single_step_2_phantom_does_not_count():
    """; and / phantoms carry R4 (a brushed neighbour reads the same at
    --poll 1.0): they count only when repeated in another kept pair."""
    L = ke.K("l")
    s2 = [state()] + [state({SEMI_, L}, 2)] * 5 + [state(set(), 2)] + \
        [state({SLASH_}, 3)] * 5 + [state(set(), 3)]
    ns = _clean(True)
    ns[2] = ke.judge_step(live1(s2, step=2)[0], 2, control=True)
    assert ns[2].settle
    one = ke.ab_verdict([pair(1, nosettle=ns)] + [pair(i) for i in
                                                   range(2, 6)])
    # fix8 F3 (was NOT REPRODUCED): a single one asks for more pairs
    assert one.startswith("REDO") and "not yet repeated" in one
    two = ke.ab_verdict([pair(1, nosettle=ns), pair(2, nosettle=ns)] +
                        [pair(i) for i in range(3, 6)])
    assert two.startswith("NECESSARY")


def test_ab_line_and_logged_column_in_the_replay(tmp_path):
    """D3: the SUMMARY shows the logged verdict next to the replayed one;
    a replay of A/B sessions ends with one 'A/B:' line."""
    log = str(LOGS / "kbd-hw-2.session.log")
    out = []
    ke.replay_report([log], out=out.append, assume_poll=0.1)
    i = next(i for i, l in enumerate(out) if l.startswith("SUMMARY"))
    assert "logged" in out[i + 1]
    assert out[i + 2].split()[2] == "INCOMPLETE"      # fixed@48 logged
    assert not any(l.startswith("A/B") for l in out)   # not an A/B session
    body = (tmp_path / "ab.log")
    body.write_text("[10:00:00] kbd_echo MANUAL on 192.0.2.1, runs "
                    "nosettle@48, fixed@48, poll 1s, prg x\n")
    out = []
    ke.replay_report([str(body)], out=out.append)
    assert out[-1].startswith("A/B (docs/HLE_CON.md 'The settle A/B'): REDO")


def test_D1_script_and_question_ask_for_a_long_hold():
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    ab = doc[doc.index("### The settle A/B"):]
    assert "count of 10" in ab and "slow count of 5" not in ab
    assert "45%" in ab and "not reproduced" in ab.lower()
    assert "about 2 s" not in Path(ke.__file__).read_text()


def test_ab_unproven_polling_discards_the_pair():
    """fix7 mutant kill (ab_unproven_ok): a pair whose polling no
    heartbeat proves is discarded, even with a phantom in it."""
    ab = ke.ab_verdict([pair(1, nosettle=_clean(True, PHANTOM),
                             proven=False)] +
                       [pair(i) for i in range(2, 5)])
    assert ab.startswith("REDO") and "pair1: polling not proven" in ab


# --- fix8 (fix7 review, build/evidence/w6-kbd-review7/REVIEW.md) -------------
# The review's attacks on real A/B session logs, adopted inverted; its
# guards G1-G8 run unchanged in test_fix7_attacks.py (fix8/green).

from tests import kbd_ab_logs as abl  # noqa: E402


def test_F1_A1_uparrow_in_raw_with_q_committed_is_the_signature():
    """review7 A1, inverted: Q commits alone, then UPARROW (Q's c-1) keeps
    flickering into raw while Q is held: the settle's own trace.
    nosettle: signature; fixed: a settle defect (the A/B STOPs)."""
    st = abl.live(abl.UPRAW1, step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert ke.judge_run(ke.CONTROL, "PP-------",
                        {1: jc, 2: ke.StepJudge(True, [], [])},
                        range(1, 3)) == "DEFECT SHOWN"
    jf = ke.judge_step(st, 1)
    assert any("UPARROW" in d for d in jf.defects)
    assert jc.settle and jf.settle


def test_F1_A1_five_such_pairs_read_necessary(tmp_path):
    """review7 A1 end to end, inverted."""
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.UPRAW1, abl.CLEAN2)})]
                  * 5)
    assert v.startswith("NECESSARY"), v


def test_F2_A2_uparrow_committed_mid_hold_is_the_signature(tmp_path):
    """review7 A2, inverted: UPARROW committed two polls into Q's hold
    (COLUMN BLEED? c7r6 Q is held) is a settle signature, never a kept
    zero."""
    st = abl.live(abl.UPLATE1, step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert jc.signature
    v, out = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.UPLATE1,
                                                   abl.CLEAN2)})] +
                    [abl.CLEAN] * 4)
    assert v.startswith("NECESSARY"), v


def test_F2_A2b_the_same_in_the_fixed_build_stops(tmp_path):
    """review7 A2b, inverted: STOP, not REDO 'stray key'."""
    v, _ = abl.ab(tmp_path, [(1.0, {"fixed": (abl.UPLATE1, abl.CLEAN2)})] +
                  [abl.CLEAN] * 4)
    assert v.startswith("STOP") and "pair1#1" in v, v


def test_F2_a_nosettle_stray_key_discards_the_pair(tmp_path):
    """A nosettle stray key that is not the settle's (W with Q) is no
    clean zero: the pair is discarded."""
    W = ke.K("w")
    stray = [abl.state()] + [abl.state({abl.Q, W}, 2)] * 5 + \
        [abl.state(set(), 2)]
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (stray, abl.CLEAN2)})] +
                  [abl.CLEAN] * 4)
    assert v.startswith("REDO") and "pair1#1" in v and "stray" in v, v


def test_F3_A3_a_step_2_signature_never_reads_not_reproduced(tmp_path):
    """review7 A3, inverted: one step-2 settle signature (/ raw-only, r7
    low at idle) in a kept pair: REDO ('not yet repeated'), never NOT
    REPRODUCED."""
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.CLEAN1,
                                                 abl.SLASH_R7)})] +
                  [abl.CLEAN] * 4)
    assert not v.startswith("NOT REPRODUCED"), v
    assert v.startswith("REDO") and "not yet repeated" in v, v


def test_F3_a_defect_like_pair_never_reads_not_reproduced(tmp_path):
    """A nosettle run 'defect-like, not settle-specific' (a chattering Q)
    next to 5 clean pairs: REDO, not NOT REPRODUCED."""
    chat = [abl.state()] + [abl.state(set(), 0, raw={abl.Q})] * 2 + \
        [abl.state({abl.Q}, 1)] * 4 + [abl.state(set(), 1)]
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (chat, abl.CLEAN2)})] +
                  [abl.CLEAN] * 5)
    assert v.startswith("REDO") and "not settle-specific" in v, v


def test_F5_A4_a_log_passed_twice_counts_once(tmp_path):
    """review7 A4, inverted."""
    v, out = abl.ab(tmp_path, [abl.CLEAN] * 3, extra_paths=(0, 1))
    assert v.startswith("REDO: 3 of 5"), v
    assert sum("passed twice" in l for l in out) == 2


def test_F6_A5_row_low_at_idle_before_the_press_does_not_count():
    """review7 A5, inverted: r6 low at idle BEFORE Q goes down, then a
    chattering Q: no settle evidence (only polls while Q is held or raw
    count)."""
    s = [abl.state(idle=abl.R6LOW), abl.state()] + \
        [abl.state(set(), 0, raw={abl.Q})] * 2 + \
        [abl.state({abl.Q}, 1)] * 3 + [abl.state(set(), 1)]
    st = abl.live(s, step=1)
    jc = ke.judge_step(st, 1, control=True)
    assert not jc.settle and "not settle-specific" in jc.not_judgeable


def test_D2_the_host_checklist_and_the_c64_screen_say_count_10():
    how = ke.checklist(ke.parse_runs("nosettle@48")[0])
    assert any("count of 10" in l for l in how)
    assert not any("2 s" in l for l in how)
    c = (Path(ke.PROJECT_ROOT) / "fixtures" / "c" / "kbd_echo.c").read_text()
    assert "count 10" in c and "2s" not in c


def test_D1_doc_drifts_are_gone():
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    assert "raw-only (in the raw samples, not the debounced matrix) for " \
        "1.5 s and never committed" not in doc
    assert "row 6 low at the idle read" not in doc
    ab = doc[doc.index("### The settle A/B"):]
    for must in ("SHIFT LOCK", "touch no other key", "HOLD Q (COUNT 10)",
                 "RETURN alone", "once"):
        assert must in ab, must


def test_K5_no_vetoes_nothing_with_idle_evidence_only():
    """fix8 mutant kill (no_vetoes): with r6-low-at-idle as the only
    settle evidence (no UPARROW, so the fix8 F1 path cannot add it back),
    a 'no' still changes nothing."""
    s = [state()] + [state(set(), 0, raw={Q_}),
                     state(set(), 0, idle=R6LOW, raw=set()),
                     state(set(), 0, raw={Q_})] + [state()] * 2
    got = set()
    for answer in (None, False):
        st, _ = live1(s, step=1)
        if answer is False:
            st.denied.add(1)
        got.add(ctl7(ke.judge_step(st, 1, control=True)))
    assert got == {"DEFECT SHOWN"}


# --- fix9 (review8 N1-N3; the Blingboard wake, attended A/B 2026-09-28) ------

import re  # noqa: E402

AB_LOGS = LOGS / "kbd-ab-20260928"
NOTHING1 = [abl.state()] * 7          # step 1: Q never in raw nor matrix
WAKE = "keyboard not awake? (Blingboard idle; tap SHIFT first)"


def test_N1_X1_a_signature_in_a_discarded_pair_blocks_not_reproduced(
        tmp_path):
    """review8 X1, inverted: a phantom with a tapped fixed partner, then 5
    clean pairs: REDO naming the discarded signature, never NOT
    REPRODUCED."""
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.PHANTOM1, abl.CLEAN2),
                                    "fixed": (abl.TAP1, abl.CLEAN2)})] +
                  [abl.CLEAN] * 5)
    assert v.startswith("REDO") and "pair1#1" in v and "phantom" in v, v


def test_N1_X2_the_same_in_an_unproven_pair(tmp_path):
    """review8 X2, inverted."""
    p = [abl.Log().session(abl.plan(0), 1.0,
                           nosettle=(abl.UPLATE1, abl.CLEAN2))
         .write(tmp_path / "pair1" / "session.log")]
    txt = open(p[0]).read()
    open(p[0], "w").write(re.sub(r"(?m)^.*hb polls.*\n", "", txt))
    for i in range(1, 6):
        p.append(abl.Log().session(abl.plan(i), 1.0)
                 .write(tmp_path / f"pair{i + 1}" / "session.log"))
    out = []
    ke.replay_report(p, out=out.append)
    line = [l for l in out if l.startswith("A/B")][0]
    assert "NOT REPRODUCED" not in line and "pair1#1" in line, line


def test_N2_redo_names_its_stop_rule(tmp_path):
    """review8 X3: a single step-2 signature never resolves by itself;
    the line says how far to go (3 extra pairs, then REDO unresolved)."""
    s2 = [abl.state()] + abl.hold_states(abl.SEMI) + \
        [abl.state({abl.SLASH, abl.COMMA}, 2)] * 5 + [abl.state(set(), 2)]
    v, _ = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.CLEAN1, s2)})] +
                  [abl.CLEAN] * 4)
    assert v.startswith("REDO") and "at most 3 extra pairs" in v, v
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    assert "REDO (unresolved)" in doc


def test_N3_the_c_minus_1_form_has_its_own_summary_class(tmp_path):
    v, out = abl.ab(tmp_path, [(1.0, {"nosettle": (abl.UPRAW1,
                                                   abl.CLEAN2)})] * 5)
    rows = [l for l in out if re.match(r"^ +[0-9]+ nosettle@48 ", l)]
    assert rows and all("settle: ^ while Q held" in r for r in rows), rows


def test_wake_no_q_seen_at_all_is_its_own_discard(tmp_path):
    """Q never in raw nor in the matrix in a run's step 1 (the Blingboard
    after idle): REDO with the wake reason, never a signature, in either
    build; the SUMMARY names it too."""
    for build in ("nosettle", "fixed"):
        v, out = abl.ab(tmp_path / build,
                        [(1.0, {build: (NOTHING1, abl.CLEAN2)})] +
                        [abl.CLEAN] * 4)
        assert v.startswith("REDO") and WAKE in v and "pair1#1" in v, v
        assert not v.startswith(("NECESSARY", "STOP"))
        assert any(re.match(rf"^ +1 {build}@48 .*no Q at all: not awake\?", l)
                   for l in out), out


def test_wake_the_attended_a_b_replays_as_redo_not_awake():
    """The attended A/B (2026-09-28): every session's FIRST run had step 1
    with no Q at all, in both builds; all four pairs are wake discards."""
    logs = sorted(str(p) for p in AB_LOGS.glob("pair*/session.log"))
    assert len(logs) == 4
    out = []
    ke.replay_report(logs, out=out.append)
    line = [l for l in out if l.startswith("A/B")][0]
    assert line.split("'): ", 1)[1].startswith("REDO"), line
    for p in ("pair1#1", "pair2#1", "pair4#1", "pair5#1"):
        assert f"discarded {p}: {WAKE}" in line, (p, line)


def test_wake_a_lone_shift_tap_is_harmless_in_both_builds(tmp_path):
    """guard: the wake step, a LEFT SHIFT tap at step 1's prompt, then Q
    held: neither a stray key, nor a bleed, nor a hold problem, in either
    build; 5 such pairs read NOT REPRODUCED like clean ones."""
    LS = ke.LSHIFT
    tap = [abl.state(), abl.state({LS}), abl.state(set())] + \
        abl.hold_states(abl.Q)
    for control in (True, False):
        st = abl.live(tap, step=1)
        j = ke.judge_step(st, 1, control=control)
        assert j.performed and not j.defects and not j.signature, j
        assert not j.not_judgeable
    v, out = abl.ab(tmp_path, [(1.0, {"nosettle": (tap, abl.CLEAN2),
                                      "fixed": (tap, abl.CLEAN2)})] * 5)
    assert v.startswith("NOT REPRODUCED"), v


def test_wake_step_in_the_checklist_c64_prompt_and_docs():
    lines = ke.checklist(ke.parse_runs("fixed@48")[0])
    assert any("tap LEFT SHIFT" in l for l in lines)
    c = (Path(ke.PROJECT_ROOT) / "fixtures" / "c" / "kbd_echo.c").read_text()
    assert "tap SHIFT" in c
    doc = (Path(ke.PROJECT_ROOT) / "docs" / "HLE_CON.md").read_text()
    ab = doc[doc.index("### The settle A/B"):]
    assert "Blingboard" in ab and "tap LEFT SHIFT" in ab
    assert "no evidence for the settle defect" in doc
    hw = (Path(ke.PROJECT_ROOT) / "docs" / "HARDWARE.md").read_text()
    assert "Blingboard" in hw and "2026-09-28" in hw


def test_wake_is_only_step_1_with_nothing_at_all(tmp_path):
    """fix9 mutant kills: a key missing in step 2, or Q seen raw-only at
    one poll in step 1, is not the wake: other discard reasons."""
    s2_none = [abl.state()] * 6 + [abl.state({abl.SLASH}, 1)] * 5 + \
        [abl.state(set(), 1)]
    raw_brief = [abl.state(), abl.state(set(), 0, raw={abl.Q})] + \
        [abl.state()] * 5
    for name, runs in (("s2", (abl.CLEAN1, s2_none)),
                       ("brief", (raw_brief, abl.CLEAN2))):
        v, out = abl.ab(tmp_path / name, [(1.0, {"nosettle": runs})] +
                        [abl.CLEAN] * 4)
        assert v.startswith("REDO") and "pair1#1" in v, v
        assert WAKE not in v, (name, v)


def test_wake_line_is_general_in_every_checklist():
    for run in ("nojoymask@48", "nosettle@48", "fixed@1"):
        lines = ke.checklist(ke.parse_runs(run)[0])
        assert any(l.startswith("At the run's FIRST '>' prompt: tap LEFT "
                                "SHIFT") for l in lines), run
