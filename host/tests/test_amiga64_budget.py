"""W17-K: the /Temp budget of the amiga64 paths (user ruling 2026-10-13).

The Ultimate firmware keeps a file in /Temp for every POST with a body.  The
CLI relies on the pinned harness, which sweeps the C64U's /Temp under the
DeviceLock, so the one leaking call stays: client.run_prg inside
tests/run_vectors.prepare_target.  These tests pin the known budget so that
any new leaking call fails them:
  doctor 0;  run 1 (2 with audio);  resume 2 (3 with audio)
and that every device call happens inside a u64_hold (the lock held).
The zero-leak start (B') is parked: build/evidence/w17-k/backlog-bprime/,
harness issue #532."""

import ast
import contextlib
import os
from types import SimpleNamespace

import pytest

from amiga64 import device as D
from amiga64 import session as S

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEAKING = {"run_prg", "load_prg", "run_crt", "sid_play", "mount_disk", "load_rom", "drive_load_rom",
           "set_config_items_batch", "liveness_probe", "assert_healthy", "write_mem", "mod_play"}
RUN_PATH = ["tests/aros_wb.py", "tests/aros_boot.py", "tests/aros_resume.py", "tests/aros_modwb.py",
            "tests/run_vectors.py", "tests/harness_util.py", "tests/run_paula.py",
            "tests/paula/hw16.py", "tests/bench/u64_session.py", "tests/run_program.py",
            "tests/run_spr.py", "host/prebuilt.py"] + \
           [os.path.join("amiga64", f) for f in sorted(os.listdir(os.path.join(ROOT, "amiga64")))
            if f.endswith(".py")]


def test_the_only_leaking_call_site_is_prepare_target():
    sites = []
    for rel in RUN_PATH:
        t = ast.parse(open(os.path.join(ROOT, rel)).read())
        for fn in [n for n in ast.walk(t) if isinstance(n, ast.FunctionDef)]:
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                        and c.func.attr in LEAKING:
                    sites.append((rel, fn.name, c.func.attr))
                if isinstance(c, ast.Assign) and any(isinstance(x, ast.Attribute) and x.attr == "socket_dma"
                                                     for x in c.targets):
                    sites.append((rel, fn.name, "socket_dma ="))
    assert sorted(set(sites)) == [("tests/run_vectors.py", "prepare_target", "run_prg")]


def test_prepare_target_leaks_exactly_one_request():
    t = ast.parse(open(os.path.join(ROOT, "tests", "run_vectors.py")).read())
    fn = next(n for n in ast.walk(t) if isinstance(n, ast.FunctionDef) and n.name == "prepare_target")
    calls = [c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call)
             and isinstance(c.func, ast.Attribute) and c.func.attr in LEAKING]
    assert calls == ["run_prg"]
    assert not any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(fn))   # once per call


# --- the chains, with a fake device whose every call must be inside the hold -----------

class Lock:
    held = False


class Dev:
    """A fake client/transport: records calls, refuses any outside the hold."""

    def __init__(self, name, log):
        self.name, self.log = name, log

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)

        def call(*a, **k):
            assert Lock.held, f"{self.name}.{attr} outside the DeviceLock hold"
            self.log.append(f"{self.name}.{attr}")
            if attr == "list_configs":
                return ["Audio"]
            if attr == "get_config_category":
                return {"Audio": {"Map Ultimate Audio $DF20-DFFF": "Disabled"}}
            if attr == "get_speed":
                return 48
            return None
        return call


@pytest.fixture
def rig(monkeypatch, tmp_path):
    from c64_test_harness.backends import ultimate64_helpers as UH
    from tests import aros_resume as RS
    from tests import aros_wb as WB
    from tests import run_vectors as RV
    from tests.bench import u64_session as US
    from tests.paula import hw16
    log = []
    client = Dev("client", log)
    transport = Dev("transport", log)
    transport.client = client
    tg = SimpleNamespace(transport=transport, backend="u64")
    leaks = []

    @contextlib.contextmanager
    def hold(out, lock_timeout=None):
        Lock.held = True
        try:
            yield {"target": tg, "info": {"product": "Ultimate 64 Elite"}, "before": {}}
        finally:
            Lock.held = False

    def prepare(target, prg):               # the real one's leak, counted
        assert Lock.held, "prepare_target outside the hold"
        leaks.append("run_prg")
    monkeypatch.setattr(US, "u64_hold", hold)
    monkeypatch.setattr(RV, "prepare_target", prepare)
    monkeypatch.setattr(UH, "reboot", lambda c: c.reboot())
    monkeypatch.setattr(UH, "reset", lambda c: c.reset())
    monkeypatch.setattr(hw16, "read_window_t", lambda t: b"\0\x10" * 112)
    monkeypatch.setattr(WB, "Labels", None, raising=False)
    import c64_test_harness as H
    monkeypatch.setattr(H.Labels, "from_file", staticmethod(lambda p: {}))
    monkeypatch.setattr(WB.time, "sleep", lambda s: None)

    class M:
        vice = False

        def __init__(self, target, labels, prg):
            self.tg, self.t, self.prg = target, target.transport, prg
            self.aud = WB.AUDIO[0]

        def lab(self, n):
            return 0x100

        def w(self, a, b):
            self.t.write_memory(a, b)

        def load_driver(self):
            self.t.write_memory(0xC000, b"\0")
    monkeypatch.setattr(WB, "WbMachine", M)
    monkeypatch.setattr(WB, "enable_ci", lambda m, rep: m.t.client.set_config_item("ci"))
    monkeypatch.setattr(WB, "boot_workbench", lambda m, *a, **k: (k["after_stage"](), [])[1])
    monkeypatch.setattr(WB, "arm_spr", lambda m: None)
    monkeypatch.setattr(WB, "demo_mouse", lambda m, s: None)
    monkeypatch.setattr(WB, "_suspend", lambda m, sn, path, rep: m.t.write_memory(0, b"\0"))

    def resume_wb(m, rom, path, rep):        # its one leaking step: _resume -> fresh_load
        RS.fresh_load(m, m.prg)
        return 1.0
    monkeypatch.setattr(WB, "resume_workbench", resume_wb)
    monkeypatch.setattr(RS, "scrub", lambda m: None)

    class Snap:
        def __init__(self, *a):
            pass

        def declare_rid(self):
            assert Lock.held
    monkeypatch.setattr(RS, "Snap", Snap)
    monkeypatch.setattr(RS, "rom_id", lambda rom: b"r")
    monkeypatch.setattr(S, "hold_loop", lambda minutes: False)
    for n in ("AUDIO", "AUD_S", "DEMO_MOUSE", "STUCK", "AUD_HW"):
        monkeypatch.setattr(WB, n, list(getattr(WB, n)))
    monkeypatch.setattr(WB, "model_wb", WB.model_wb)
    return SimpleNamespace(leaks=leaks, log=log, tmp=tmp_path)


def _plan(rig, audio, **kw):
    return S.Plan(backend="u64", prg=str(rig.tmp / "runner.prg"), host="h", mhz=48, vol="/USB1",
                  out=str(rig.tmp / "out"), minutes=0.0, audio=audio, **kw)


@pytest.mark.parametrize("audio,want", [(False, 1), (True, 2)])
def test_run_budget(rig, audio, want):
    fails, _ = S.run(_plan(rig, audio, save=True), "ROM", SimpleNamespace(raw=b""))
    assert fails == []
    assert len(rig.leaks) == want
    assert not Lock.held


@pytest.mark.parametrize("audio,want", [(False, 2), (True, 3)])
def test_resume_budget(rig, audio, want):
    fails, _ = S.resume(_plan(rig, audio), "ROM")
    assert fails == []
    assert len(rig.leaks) == want


def test_doctor_budget_and_hold(tmp_path):
    from host.tests.test_amiga64_cli import FakeStorage, FULL, U64E_INFO
    log = []

    class St(FakeStorage):
        def __getattribute__(self, n):
            if n.startswith("storage_"):
                assert Lock.held, f"{n} outside the hold"
                log.append(n)
            return object.__getattribute__(self, n)

    @contextlib.contextmanager
    def hold(out, lock_timeout=None):
        Lock.held = True
        try:
            yield {"target": SimpleNamespace(transport=SimpleNamespace(client=Dev("client", log))),
                   "info": U64E_INFO, "before": FULL}
        finally:
            Lock.held = False
    D.probe("h", str(tmp_path), adf_sha="0" * 64, hold_fn=hold, storage=St())
    assert log and not any(x.split(".")[-1] in LEAKING for x in log)
    assert not Lock.held
