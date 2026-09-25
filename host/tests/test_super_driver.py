"""Unit tests for tests/run_super_vectors.py's control flow (no VICE).

Review addendum: in sequential mode a wedged target is replaced by a fresh
VICE instance; that replacement must get the same one-off target checks
(boot st_exc/st_exl == 0, RESET-hook probe, mid-run flip) as any fresh
target, or a bad boot state on it would go unnoticed.
"""

from __future__ import annotations

import pytest

import tests.run_super_vectors as D
from tests.run_vectors import RunContext


class FakeMgr:
    def __init__(self) -> None:
        self.n = 0
        self.live: list[str] = []

    def acquire(self) -> str:
        self.n += 1
        t = f"vice{self.n}"
        self.live.append(t)
        return t

    def release(self, t: str) -> None:
        self.live.remove(t)


@pytest.fixture
def calls(monkeypatch):
    log: list[tuple[str, str]] = []
    wedge = {"B"}                       # batch B wedges its target once

    def run_batch(target, addrs, name, cases, ctx):
        log.append(("batch", f"{name}@{target}"))
        if name in wedge:
            wedge.discard(name)
            return False, 1
        return True, 0

    monkeypatch.setattr(D, "prepare_target", lambda t, prg: log.append(("prep", t)))
    monkeypatch.setattr(D, "target_checks",
                        lambda t, addrs, ctx: log.append(("checks", t)))
    monkeypatch.setattr(D, "run_batch", run_batch)
    monkeypatch.setattr(D, "leak_check",
                        lambda t, addrs, golden, ctx: log.append(("leak", t)))
    return log


def _run(log, leak=False) -> FakeMgr:
    mgr = FakeMgr()
    ctx = RunContext(timeout=1.0, max_failures=0, ccr_only=False)
    D.run_sequential(mgr, "x.prg", {"emu_run": 1},
                     [("A", []), ("B", []), ("C", [])],
                     ctx, leak=leak)
    assert mgr.live == []               # every acquired target released
    return mgr


def test_replacement_target_gets_target_checks(calls) -> None:
    _run(calls)
    assert ("prep", "vice2") in calls
    i = calls.index(("prep", "vice2"))
    assert calls[i + 1] == ("checks", "vice2")   # before its first batch
    assert calls[i + 2] == ("batch", "C@vice2")


def test_leak_check_runs_on_the_final_target(calls) -> None:
    _run(calls, leak=None)
    assert calls[-1] == ("leak", "vice2")
