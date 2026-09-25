"""tests/bench/u64_session.u64_hold under fault injection (no network).

A fake Ultimate client and a fake UnifiedManager stand in for the device;
the harness's real snapshot_state / restore_state run against them.  Every
case must end with the device config equal to the snapshot (or an explicit
restored=False), the lock released, and no flash save.  W5-T fix round 1
(adversarial review of 35c1c1e; reviewer repros build/review-tools/).
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import c64_test_harness  # noqa: E402
from tests.bench import u64_session as us  # noqa: E402

BASE = {
    "U64 Specific Settings": {"Turbo Control": "Off", "CPU Speed": " 1",
                              "Badline Timing": "Enabled"},
    "C64 and Cartridge Settings": {
        "Cartridge": "", "Cartridge Preference": "Auto",
        "Bus Operation Mode": "Quiet", "RAM Expansion Unit": "Disabled",
        "REU Size": "2 MB", "Command Interface": "Disabled"},
    "SID Addressing": {"SID Socket 1 Address": "$D400",
                       "Auto Address Mirroring": "Enabled"},
    "Network Settings": {"Host Name": "u64", "Network Password": "hunter2"},
    "Clock Settings": {"Hour": 1},
}


class Boom(Exception):
    pass


class FakeClient:
    """fail: {key: [countdown, exc]} -- the countdown-th matching call
    raises exc (once); key "always:<item>" makes every PUT of it fail."""

    def __init__(self):
        self.cfg = copy.deepcopy(BASE)
        self.fail = {}
        self.calls = []
        self.saved = False
        self.getcat = None                # optional override of one GET

    def _f(self, key):
        f = self.fail.get(key)              # [countdown, exc(, repeats)]
        if f and f[0] > 0:
            f[0] -= 1
            if f[0] == 0:
                if len(f) > 2 and f[2] > 1:
                    f[2] -= 1
                    f[0] = 1                # the next call fails too
                raise f[1]

    def list_configs(self):
        self._f("list")
        return list(self.cfg)

    def get_config_category(self, c):
        self._f("get")
        if self.getcat is not None:
            r = self.getcat(c)
            if r is not None:
                return r
        return {c: dict(self.cfg[c]), "errors": []}

    def set_config_item(self, c, i, v):
        self.calls.append(("set", c, i, v))
        if f"always:{i}" in self.fail:
            raise Boom(f"HTTP 400 on {i}")
        self._f("set")
        self._f(("set", i))
        self.cfg[c][i] = v

    def set_config_items(self, c, d):
        for k, v in d.items():
            self.set_config_item(c, k, v)

    def get_info(self):
        self._f("info")
        return {"product": "fake", "firmware_version": "3.15"}

    def reset(self):
        self.calls.append(("reset",))
        self._f("reset")

    def save_config_to_flash(self):
        self.saved = True

    def __getattr__(self, n):
        raise AttributeError(f"FAKE: unexpected client call {n}")


class Env:
    def __init__(self, client):
        self.client = client
        self.released = 0

    def manager(self, **kw):
        env = self

        class T:
            pass

        class Mgr:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def acquire(self):
                t = T()
                t.backend = "u64"
                t.transport = T()
                t.transport.client = env.client
                return t

            def release(self, t):
                env.released += 1
        return Mgr()


@pytest.fixture
def env(monkeypatch):
    e = Env(FakeClient())
    monkeypatch.setattr(c64_test_harness, "UnifiedManager", e.manager)
    monkeypatch.setattr(us.time, "sleep", lambda s: None)
    monkeypatch.setattr(us, "log", lambda m: None)
    return e


def dirty(c):
    """What a session does to the device."""
    c.set_config_item("U64 Specific Settings", "Turbo Control", "Manual")
    c.set_config_item("U64 Specific Settings", "CPU Speed", "48")
    c.set_config_item("C64 and Cartridge Settings", "RAM Expansion Unit",
                      "Enabled")
    c.set_config_item("C64 and Cartridge Settings", "REU Size", "16 MB")
    c.set_config_item("C64 and Cartridge Settings", "Command Interface",
                      "Enabled")
    c.set_config_item("Clock Settings", "Hour", 7)


def session(env, tmp_path, fail=None, body_exc=None):
    """Run a dirtying body inside u64_hold; arm *fail* after the body."""
    out = {"hold": None, "exc": None, "body_ran": False}
    try:
        with us.u64_hold(str(tmp_path)) as hold:
            out["hold"] = hold
            out["body_ran"] = True
            dirty(env.client)
            env.client.fail = fail or {}
            if body_exc is not None:
                raise body_exc
    except BaseException as e:  # noqa: BLE001
        out["exc"] = e
    return out


def assert_restored(env, out):
    assert env.client.cfg == BASE
    assert out["hold"]["restored"] is True
    assert env.released == 1
    assert env.client.saved is False
    assert ("reset",) in env.client.calls


def test_clean_exit(env, tmp_path):
    out = session(env, tmp_path)
    assert out["exc"] is None
    assert_restored(env, out)


def test_body_exception_still_restores(env, tmp_path):
    out = session(env, tmp_path, body_exc=RuntimeError("scenario"))
    assert isinstance(out["exc"], RuntimeError)
    assert_restored(env, out)


def test_restore_state_timeout_falls_back_per_item(env, tmp_path):
    # the first PUT of restore_state (Turbo Control) times out: the whole
    # batch is lost, the per-item pass must still put everything back
    out = session(env, tmp_path,
                  fail={("set", "Turbo Control"): [1, Boom("REST timeout")]})
    assert out["exc"] is None
    assert_restored(env, out)


def test_item_put_failure_is_retried(env, tmp_path):
    # restore_state does not touch Command Interface; its per-item PUT
    # fails once and must be retried
    out = session(env, tmp_path,
                  fail={("set", "Command Interface"): [1, Boom("timeout")]})
    assert_restored(env, out)


def test_item_rejected_400_is_reported(env, tmp_path):
    out = session(env, tmp_path, fail={"always:Command Interface": True})
    cfg = env.client.cfg
    assert out["hold"]["restored"] is False             # explicit failure
    assert cfg["C64 and Cartridge Settings"]["Command Interface"] == "Enabled"
    other = copy.deepcopy(cfg)
    other["C64 and Cartridge Settings"]["Command Interface"] = "Disabled"
    assert other == BASE                                 # all else restored
    assert env.released == 1
    after = json.loads((tmp_path / "config_after.json").read_text())
    assert "Command Interface" in json.dumps(after["diff_vs_before"])


def test_list_configs_failure_during_restore_is_retried(env, tmp_path):
    out = session(env, tmp_path, fail={"list": [1, Boom("timeout")]})
    assert_restored(env, out)


def test_reread_failing_twice_is_retried_later(env, tmp_path):
    # the first re-read fails on both of its attempts: a later re-read
    # must still drive the per-item pass
    out = session(env, tmp_path, fail={"list": [1, Boom("timeout"), 2]})
    assert_restored(env, out)


def test_reread_never_works_is_reported(env, tmp_path):
    out = session(env, tmp_path, fail={"list": [1, Boom("down"), 99]})
    assert out["hold"]["restored"] is False
    assert env.released == 1


def test_reset_failure_is_reported_but_config_restored(env, tmp_path):
    out = session(env, tmp_path, fail={"reset": [1, Boom("timeout")]})
    assert env.client.cfg == BASE
    assert out["hold"]["restored"] is True
    assert out["hold"]["reset_ok"] is False
    assert env.released == 1


def test_snapshot_failure_aborts_before_body(env, tmp_path):
    env.client.fail = {"get": [1, Boom("GET timeout")]}
    out = session(env, tmp_path)
    assert out["exc"] is not None
    assert out["body_ran"] is False
    assert [c for c in env.client.calls if c[0] == "set"] == []
    assert env.client.cfg == BASE
    assert env.released == 1


def test_errored_category_in_snapshot_aborts_before_body(env, tmp_path):
    # a 200 answer that carries only an errors list for one category
    seen = {"n": 0}

    def flaky(c):
        if c == "C64 and Cartridge Settings" and seen["n"] == 0:
            seen["n"] += 1
            return {"errors": ["busy"]}
        return None
    env.client.getcat = flaky
    out = session(env, tmp_path)
    assert out["exc"] is not None
    assert out["body_ran"] is False
    assert [c for c in env.client.calls if c[0] == "set"] == []
    assert env.released == 1


def test_item_missing_from_snapshot_is_never_restored_as_none(env, tmp_path):
    # an item that appears only after the snapshot has no "was": it must
    # not be written (as None / "None"), and the diff is reported
    out = {"hold": None}
    with us.u64_hold(str(tmp_path)) as hold:
        out["hold"] = hold
        env.client.cfg["Clock Settings"]["Minute"] = 5
    assert ("set", "Clock Settings", "Minute", None) not in env.client.calls
    assert not [c for c in env.client.calls
                if c[0] == "set" and c[3] in (None, "None")]
    assert hold["restored"] is False
    assert env.released == 1


def test_keyboard_interrupt_in_body(env, tmp_path):
    out = session(env, tmp_path, body_exc=KeyboardInterrupt())
    assert isinstance(out["exc"], KeyboardInterrupt)
    assert_restored(env, out)


def test_keyboard_interrupt_during_restore_finishes_restore(env, tmp_path):
    out = session(env, tmp_path,
                  fail={("set", "Turbo Control"): [1, KeyboardInterrupt()]})
    assert isinstance(out["exc"], KeyboardInterrupt)     # re-raised after
    assert_restored(env, out)


def test_password_items_redacted_in_dumps(env, tmp_path):
    out = session(env, tmp_path)
    assert out["exc"] is None
    for name in ("config_before.json", "config_after.json"):
        text = (tmp_path / name).read_text()
        assert "hunter2" not in text, name
    # the in-memory compare still used the real value
    assert env.client.cfg["Network Settings"]["Network Password"] == "hunter2"


# --- delta round (review of 98a48d3) ----------------------------------------

def test_keyboard_interrupt_between_steps_finishes_restore(env, tmp_path,
                                                            monkeypatch):
    # reviewer case D3: ^C inside round 1's config_diff (outside any step
    # before this round), after restore_state failed
    real = us.config_diff
    n = {"calls": 0}

    def bad_diff(a, b):
        n["calls"] += 1
        if n["calls"] == 1:
            raise KeyboardInterrupt()
        return real(a, b)
    monkeypatch.setattr(us, "config_diff", bad_diff)
    out = session(env, tmp_path,
                  fail={("set", "Turbo Control"): [1, Boom("t/o")]})
    assert isinstance(out["exc"], KeyboardInterrupt)
    assert_restored(env, out)


def test_sigint_during_settle_is_deferred_until_released(env, tmp_path,
                                                         monkeypatch):
    # a real SIGINT during the post-reset settle sleep: the restore must
    # finish (final re-read), the lock be released, the previous handler
    # come back, and only then KeyboardInterrupt be raised
    import os
    import signal
    prev = signal.getsignal(signal.SIGINT)
    seen = {}

    def sleep(s):
        os.kill(os.getpid(), signal.SIGINT)
        seen["released_at_sigint"] = env.released
    monkeypatch.setattr(us.time, "sleep", sleep)
    out = session(env, tmp_path)
    assert isinstance(out["exc"], KeyboardInterrupt)
    assert seen["released_at_sigint"] == 0
    assert_restored(env, out)
    assert signal.getsignal(signal.SIGINT) is prev


def test_errors_list_with_items_is_a_snapshot_failure(env, tmp_path):
    # NIT-a: a category answering WITH its items AND an errors list
    def both(c):
        if c == "SID Addressing":
            return {c: dict(env.client.cfg[c]), "errors": ["partial"]}
        return None
    env.client.getcat = both
    out = session(env, tmp_path)
    assert isinstance(out["exc"], us.SnapshotError)
    assert out["body_ran"] is False
    assert [c for c in env.client.calls if c[0] == "set"] == []
    assert env.released == 1
