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

from pathlib import Path

import pytest

from tests import kbd_echo as ke

LOGS = Path(__file__).resolve().parents[2] / "tests" / "kbd" / "replay"


def _runs(name):
    return ke.replay_log(str(LOGS / f"{name}.session.log"))


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
                           out=lambda *a: None)
    assert ke.session_verdict(next(iter(res.values()))) == want


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
    assert raws == ["raw (not debounced yet): c6r6 UPARROW, c7r6 Q"]
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
    monkeypatch.setattr(ke, "session_verdict", lambda res: "VALIDATED")
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
    assert res[str(bad)]["fixed@48"] == "INCOMPLETE"
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
    for gap, want in (("0.14", "PASS"), ("2.10", "INCOMPLETE")):
        log = tmp_path / f"g{gap}.log"
        log.write_text("\n".join(src[:first + 1] +
                                 [f"[   0.50] hb polls +4 max gap {gap}s"] +
                                 src[first + 1:]) + "\n")
        res = ke.replay_report([str(log)], out=lambda *a: None)
        assert res[str(log)]["fixed@48"] == want, gap
