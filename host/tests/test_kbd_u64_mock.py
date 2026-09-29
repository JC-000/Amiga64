"""tests/kbd_echo.py's u64 control flow, offline (no device, no VICE).

Adopted from the W6-K adversarial review's probes
(build/evidence/w6-kbd-review/test_u64_mock.py) and extended for fix1:
DeviceLock, the Ultimate client, u64_hold, prepare_target, the RAM patcher
and the legs are fakes that log what the driver asks of them, so the
order of lock / GET / hold / speed / patch / restore can be pinned.
"""

from __future__ import annotations

import contextlib
import sys
import types

import pytest

from tests import kbd_echo as ke

REST = {"git_commit_hash": ke.EXPECT_GIT, "core_version": ke.EXPECT_CORE}
SPIKE = {"git_commit_hash": "4b02e67c", "core_version": "1.50"}


class Lock:
    log: list = []

    def __init__(self, key, allow_nested=False):
        assert allow_nested
        self.nested = allow_nested

    def acquire_or_raise(self, timeout, **kw):
        Lock.log.append("acquire")

    def release(self):
        Lock.log.append("release")


def client_factory(infos, boom=None):
    class C:
        def __init__(self, host):
            pass

        def get_info(self):
            Lock.log.append("get_info")
            if boom:
                raise boom
            return infos.pop(0)

        def close(self):
            pass
    return C


class Transport:
    def __init__(self):
        self.speeds = []

    def set_speed(self, m):
        self.speeds.append(m)
        Lock.log.append(f"speed {m}")


class FakePatcher:
    def __init__(self, sess, prg):
        self.active = None

    def check_clean(self):
        return True

    def apply(self, name):
        Lock.log.append(f"patch {name}")
        self.active = name

    def remove(self):
        Lock.log.append(f"unpatch {self.active}")
        self.active = None


def setup(monkeypatch, tmp_path, infos, leg=None, boom=None, manual=False,
          hold_info=None, manual_leg=None):
    import c64_test_harness as h
    import tests.bench.u64_session as us
    import tests.run_dos as rd
    import tests.run_vectors as rv
    Lock.log = []
    monkeypatch.setattr(h, "DeviceLock", Lock)
    monkeypatch.setattr(h, "Ultimate64Client", client_factory(infos, boom))
    monkeypatch.setattr(ke, "FW_REQUEUE_S", 0.0)
    monkeypatch.setattr(ke, "Patcher", FakePatcher)
    tr = Transport()
    target = types.SimpleNamespace(transport=tr, backend="u64")
    restored = []

    @contextlib.contextmanager
    def fake_hold(out_dir):
        Lock.log.append("hold-enter")
        try:
            yield {"info": hold_info or REST, "target": target,
                   "lock_wait_s": 0.0, "restored": True, "reset_ok": True}
        finally:
            restored.append(True)
            Lock.log.append("hold-restore")
    monkeypatch.setattr(us, "u64_hold", fake_hold)
    monkeypatch.setattr(us, "log", lambda *a: None)
    monkeypatch.setattr(rd, "zero_dos_bss", lambda s, l: None)
    monkeypatch.setattr(rd, "hold_checks", lambda s, h: None)
    monkeypatch.setattr(rv, "prepare_target",
                        lambda t, p: Lock.log.append("run_prg"))
    monkeypatch.setattr(ke.time, "sleep", lambda s: None)
    monkeypatch.setattr(ke, "leg_smoke", leg or (lambda *a: None))
    monkeypatch.setattr(ke, "leg_patch_roundtrip", lambda *a: None)

    def fake_manual(sess, blob, run, *a):
        Lock.log.append(f"run {run}")
        return "PASS" if run.kind == ke.FIXED else "DEFECT SHOWN"
    monkeypatch.setattr(ke, "leg_manual", manual_leg or fake_manual)
    monkeypatch.setenv("U64_HOST", "192.0.2.1")
    args = types.SimpleNamespace(
        out_dir=str(tmp_path), smoke=not manual, expect_git=ke.EXPECT_GIT,
        expect_core=ke.EXPECT_CORE, fw_wait_hours=0.0, timeout=1.0,
        budget=1, poll=0.1, max_minutes=1.0, yes=True,
        runs=ke.MANUAL_PLAN)
    return args, tr, restored


@pytest.fixture(autouse=True)
def _stdout():
    saved = sys.stdout
    yield
    sys.stdout = saved


PLAN = ke.parse_runs(ke.MANUAL_PLAN)


def test_mhz_order_reaches_set_speed(monkeypatch, tmp_path):
    args, tr, _ = setup(monkeypatch, tmp_path, [dict(REST)])
    ke.run_u64(args, "x.prg", {}, b"", [48, 1], [])
    assert tr.speeds == [1, 48, 1, 1]          # boot, runs, back to 1


def test_manual_plan_order_patches_and_verdict(monkeypatch, tmp_path):
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert tr.speeds == [1, 48, 48, 48, 1, 1]
    seq = [e for e in Lock.log if not e.startswith("speed")]
    assert seq == ["acquire", "get_info", "hold-enter", "run_prg",
                   "run fixed@48",
                   "patch nosettle", "run nosettle@48", "unpatch nosettle",
                   "patch nojoymask", "run nojoymask@48", "unpatch nojoymask",
                   "run fixed@1", "hold-restore", "release"]
    assert restored == [True] and not sess.failures
    assert "session verdict VALIDATED" in sess.checks


def test_manual_clean_control_is_inconclusive_not_validated(monkeypatch,
                                                            tmp_path):
    def leg(sess, blob, run, *a):
        return "PASS" if run.kind == ke.FIXED else "INCONCLUSIVE"
    args, tr, _ = setup(monkeypatch, tmp_path, [dict(REST)], manual=True,
                        manual_leg=leg)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert any("session verdict VALIDATED: INCONCLUSIVE" in f
               for f in sess.failures), sess.failures


def test_patch_removed_when_a_control_run_raises(monkeypatch, tmp_path):
    def leg(sess, blob, run, *a):
        if run.kind == ke.CONTROL:
            raise RuntimeError("boom")
        return "PASS"
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True, manual_leg=leg)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert Lock.log.index("unpatch nosettle") < Lock.log.index("hold-restore")
    assert restored == [True] and sess.failures
    assert Lock.log[-1] == "release"


def test_gate_released_on_interrupt_in_leg(monkeypatch, tmp_path):
    def leg(*a):
        raise KeyboardInterrupt
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)], leg=leg)
    with pytest.raises(KeyboardInterrupt):
        ke.run_u64(args, "x.prg", {}, b"", [48], [])
    assert restored == [True]
    assert Lock.log[-1] == "release"
    assert Lock.log.index("hold-restore") < Lock.log.index("release")


@pytest.mark.parametrize("manual", [False, True])
def test_gate_released_when_get_info_raises(monkeypatch, tmp_path, manual):
    args, tr, _ = setup(monkeypatch, tmp_path, [], boom=OSError("net"),
                        manual=manual)
    sess = ke.run_u64(args, "x.prg", {}, b"", [48], PLAN)
    # every acquire is paired with a release; nothing else happened
    assert Lock.log.count("acquire") == Lock.log.count("release"), Lock.log
    assert set(Lock.log) == {"acquire", "get_info", "release"}
    assert sess.failures and tr.speeds == []


def test_firmware_gate_rejects_prefix_and_longer_hash():
    assert not ke.firmware_ok(dict(REST, git_commit_hash=ke.EXPECT_GIT + "ff"),
                              ke.EXPECT_GIT, ke.EXPECT_CORE)
    assert not ke.firmware_ok(dict(REST, git_commit_hash=ke.EXPECT_GIT[:6]),
                              ke.EXPECT_GIT, ke.EXPECT_CORE)
    assert not ke.firmware_ok({}, ke.EXPECT_GIT, ke.EXPECT_CORE)


@pytest.mark.parametrize("manual", [False, True])
def test_wrong_firmware_runs_nothing(monkeypatch, tmp_path, manual):
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(SPIKE)],
                               manual=manual)
    sess = ke.run_u64(args, "x.prg", {}, b"", [48], PLAN)
    assert tr.speeds == [] and restored == [] and sess.failures
    assert Lock.log == ["acquire", "get_info", "release"]


def test_firmware_rechecked_under_the_hold(monkeypatch, tmp_path):
    """L6: the gate saw the resting build, the hold's own snapshot does not
    (a reflash between them): nothing is uploaded or run."""
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True, hold_info=SPIKE)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert "run_prg" not in Lock.log
    assert not any(e.startswith("run ") for e in Lock.log)
    assert restored == [True] and Lock.log[-1] == "release"
    assert any("re-checked" in f for f in sess.failures)


def test_manual_needs_the_operator_before_anything_is_written(monkeypatch,
                                                              tmp_path):
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True)
    args.yes = False
    monkeypatch.setattr(ke, "confirm", lambda runs, log: False)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert Lock.log == ["acquire", "get_info", "release"]
    assert restored == [] and tr.speeds == [] and sess.failures


# --- leg_manual with its program and stream faked: the verdict path --------------

class FakeStream:
    verdict = "P" * ke.NSTEP

    def __init__(self, sess, out=print):
        self.samples, self.bleeds, self.screen_log = [], [], []
        self.confirmed = set()

    def emit(self, line):
        pass

    def hb_flush(self):
        pass

    def poll(self, screen=True):
        return {}

    def verdicts(self):
        return FakeStream.verdict


@pytest.mark.parametrize("verdict,performed,want_ok", [
    ("P" * 9, True, True),
    ("PPPPFPPPP", True, False),        # a FAIL from the program
    ("PPPPSPPPP", True, False),        # a SKIP is never a pass
    ("P" * 9, False, False),           # right characters, wrong manner
])
def test_leg_manual_verdict(monkeypatch, verdict, performed, want_ok):
    from tests.run_program import Session
    FakeStream.verdict = verdict
    monkeypatch.setattr(ke, "stage_program", lambda *a: (0, {"stack_top": 0}))
    monkeypatch.setattr(ke, "Stream", FakeStream)
    monkeypatch.setattr(ke, "start_async", lambda *a: None)
    monkeypatch.setattr(ke, "is_done", lambda s: True)
    res = types.SimpleNamespace(halt=1, reason=0, timeout=0,
                                regs={"D0": verdict.count("F")},
                                print_state=lambda: None)
    monkeypatch.setattr(ke, "result", lambda s: res)
    monkeypatch.setattr(ke, "judge_step",
                        lambda st, n, control=False: ke.StepJudge(
                            performed, [], []))
    sess = Session(target=types.SimpleNamespace(backend="u64"), labels={},
                   timeout=1.0)
    got = ke.leg_manual(sess, b"", ke.Run("fixed", 48), 1, 0.0, 1.0)
    assert (got == "PASS") == want_ok
    assert (not sess.failures) == want_ok, sess.failures


def test_manual_never_requeues_even_with_hours_to_wait(monkeypatch, tmp_path):
    """M1: a wrong firmware in the manual mode fails at once, whatever
    --fw-wait-hours says (no session starting hours later, unattended)."""
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(SPIKE),
                                                       dict(REST)],
                               manual=True)
    args.fw_wait_hours = 12.0
    monkeypatch.setattr(ke.time, "sleep",
                        lambda s: pytest.fail("manual mode must not wait"))
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert Lock.log == ["acquire", "get_info", "release"]
    assert restored == [] and sess.failures


def test_stuck_run_breaks_and_leaves_the_patch_for_the_reset(monkeypatch,
                                                             tmp_path):
    """fix2 nit: a program that never ended stops the session (break); the
    RAM patch is not rewritten under a CPU that may be executing it; the
    restore (its C64 reset) still runs, then the gate is released."""
    def leg(sess, blob, run, *a):
        Lock.log.append(f"run {run}")
        if run.kind == ke.CONTROL:
            raise ke.ProgramStuck("never ended")
        return "PASS"
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True, manual_leg=leg)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    L = Lock.log
    assert "run nosettle@48" in L and "unpatch nosettle" not in L
    assert "run nojoymask@48" not in L and "run fixed@1" not in L
    assert L[-2:] == ["hold-restore", "release"] and restored == [True]
    assert sess.failures


def test_fixed_run_needs_a_clean_image(monkeypatch, tmp_path):
    class Dirty(FakePatcher):
        def check_clean(self):
            return False
    args, tr, restored = setup(monkeypatch, tmp_path, [dict(REST)],
                               manual=True)
    monkeypatch.setattr(ke, "Patcher", Dirty)
    sess = ke.run_u64(args, "x.prg", {}, b"", [], PLAN)
    assert not any(e.startswith("run ") for e in Lock.log)
    assert sess.failures and restored == [True]


def test_no_cli_switch_skips_the_confirmation(tmp_path):
    with pytest.raises(SystemExit):
        ke.main(["--prg", str(tmp_path / "x.prg"), "--backend", "u64",
                 "--yes"])


def test_leg_manual_raises_when_the_program_never_ends(monkeypatch):
    """fix2 nit: timeout, CTRL+D fed, still running -> ProgramStuck (after
    the failed check), so run_held stops the session."""
    from tests.run_program import Session
    fed = []
    monkeypatch.setattr(ke, "stage_program", lambda *a: (0, {"stack_top": 0}))
    monkeypatch.setattr(ke, "Stream", FakeStream)
    monkeypatch.setattr(ke, "start_async", lambda *a: None)
    monkeypatch.setattr(ke, "is_done", lambda s: False)
    monkeypatch.setattr(ke, "queue_keys", lambda s, d: fed.append(d))
    monkeypatch.setattr(ke.time, "sleep", lambda s: None)
    clock = iter(range(0, 10 ** 6, 20))
    monkeypatch.setattr(ke.time, "monotonic", lambda: next(clock))
    sess = Session(target=types.SimpleNamespace(backend="u64"), labels={},
                   timeout=1.0)
    with pytest.raises(ke.ProgramStuck):
        ke.leg_manual(sess, b"", ke.Run("fixed", 48), 1, 0.0, 0.01)
    assert fed and set(fed[0]) == {4}          # CTRL+D was fed first
    assert any("program ended" in f for f in sess.failures)
