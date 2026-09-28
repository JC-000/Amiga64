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


@pytest.mark.parametrize("raw_seen", [True, False])
def test_F5_nothing_form_scores_only_with_the_operator_confirmation(raw_seen):
    j2 = ke.StepJudge(True, [], [])
    st, out, j1 = _nothing_form(raw_seen)
    assert not j1.performed and not j1.signature
    assert ke.judge_run(ke.CONTROL, "SP-------", {1: j1, 2: j2},
                        range(1, 3)) == "INCOMPLETE"
    assert ke.nothing_form_steps(st, ke.Run("nosettle", 48),
                                 "SP-------") == [1]
    st, out, j1 = _nothing_form(raw_seen, confirmed=True)
    assert j1.performed and j1.signature
    assert ("raw showed" if raw_seen else "raw never showed") in \
        j1.signature[0]
    assert ke.judge_run(ke.CONTROL, "SP-------", {1: j1, 2: j2},
                        range(1, 3)) == "DEFECT SHOWN"


def test_F5_no_confirmation_is_asked_when_q_was_committed():
    Q = ke.K("q")
    st, _ = live_stream([state()] + [state({Q}, 1)] * 8 + [state()], step=1)
    assert ke.nothing_form_steps(st, ke.Run("nosettle", 48),
                                 "PP-------") == []
    assert ke.nothing_form_steps(st, ke.Run("fixed", 48), "S" * 9) == []


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
    conf = ["[  13.10] operator confirms: step 1 held as asked (Q)"]
    Q = ke.K("q")
    (run, st, _), = ke.replay_log(write_log(tmp_path, base + end, "a.log"))
    by_t = {round(s.t, 2): s for s in st.samples}
    assert Q in by_t[2.25].raw and Q not in by_t[5.0].raw
    assert ke.replay_report([str(tmp_path / "a.log")], out=lambda *a: None)[
        str(tmp_path / "a.log")][0].results["nosettle@48"] == "INCOMPLETE"
    log = write_log(tmp_path, base + conf + end, "b.log")
    s, = ke.replay_report([log], out=lambda *a: None)[log]
    assert s.results["nosettle@48"] == "DEFECT SHOWN"


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


def test_nothing_form_is_asked_only_after_an_empty_line():
    st, _, _ = _nothing_form()
    run = ke.Run("nosettle", 48)
    assert ke.nothing_form_steps(st, run, "SP-------") == [1]
    assert ke.nothing_form_steps(st, run, "PP-------") == []
    assert ke.nothing_form_steps(st, run, "FP-------") == []


@pytest.mark.parametrize("answer,want", [(True, "DEFECT SHOWN"),
                                         (False, "INCOMPLETE")])
def test_leg_manual_asks_and_uses_the_operator_answer(monkeypatch, answer,
                                                      want):
    from tests.run_program import Session
    src, _, _ = _nothing_form()
    Sm, SL = ke.K(";"), ke.K("/")
    src.samples += [ke.Sample(4.0 + 0.25 * i, 2, frozenset({Sm}), 0xFF, 1)
                    for i in range(9)]
    src.samples += [ke.Sample(7.0 + 0.25 * i, 2, frozenset({SL}), 0xFF, 2)
                    for i in range(9)]
    lines, asked = [], []

    class FS(ke.Stream):
        def __init__(self, sess, out=print):
            self.samples, self.bleeds = src.samples, []
            self.screen_log, self.confirmed = [], set()

        def emit(self, line):
            lines.append(line)

        def poll(self, screen=True):
            return {}

        def hb_flush(self):
            pass

        def verdicts(self):
            return "SP-------"
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
    got = ke.leg_manual(sess, b"", ke.Run("nosettle", 48), 1, 0.1, 1.0,
                        ask=lambda q: asked.append(q) or answer)
    assert len(asked) == 1 and "step 1" in asked[0]
    assert any(l.startswith("operator confirms: step 1") == answer and
               "step 1 held as asked" in l for l in lines)
    assert got == want
