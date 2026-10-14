"""W17-K: amiga64 device detection, the session chain and the CLI, with a
fake device (no network, no VICE)."""

import contextlib
import hashlib
import io
import os
import sys
from types import SimpleNamespace

import pytest

from amiga64 import adf as A
from amiga64 import cli
from amiga64 import config as C
from amiga64 import device as D
from amiga64 import kit
from amiga64 import session as S
from host.tests.test_amiga64_adf import MOD, PLAYER, wbdisk

U64E_INFO = {"product": "Ultimate 64 Elite", "firmware_version": "3.15"}
C64U_INFO = {"product": "C64 Ultimate", "firmware_version": "1.1.0"}
FULL = {"C64 and Cartridge Settings": {"REU Size": "2 MB", "Command Interface": "Disabled"},
        "Audio": {"Map Ultimate Audio $DF20-DFFF": "Disabled"},
        "Network Settings": {"Ultimate DMA Service": "Enabled", "FTP File Service": "Enabled"}}


@pytest.fixture(autouse=True)
def _private_paths(tmp_path, monkeypatch):
    """Never the user's own config or state."""
    monkeypatch.setenv("AMIGA64_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("AMIGA64_STATE_DIR", str(tmp_path / "state"))


# --- fakes -------------------------------------------------------------------------

class FakeClient:
    def __init__(self, choices=("128 KB", "16 MB")):
        self.choices = list(choices)
        self.calls = []

    def get_config_choices(self, cat, item):
        self.calls.append(("choices", cat, item))
        return self.choices


class FakeStorage:
    def __init__(self, volumes=("Flash", "Temp", "USB1"), files=None):
        self.vols = list(volumes)
        self.files = dict(files or {})          # full path -> bytes
        self.puts = []

    def storage_writable_volumes(self, host):
        return [v for v in self.vols if v.lower() not in ("flash", "temp")]

    def storage_list_dir(self, host, path):
        pre = path + "/"
        got = [SimpleNamespace(name=p[len(pre):], kind="file", size=len(b))
               for p, b in self.files.items() if p.startswith(pre) and "/" not in p[len(pre):]]
        if not got and not any(p.startswith(pre) for p in self.files):
            raise FileNotFoundError(path)
        return got

    def storage_get_file(self, host, path):
        return self.files[path]

    def storage_put_file(self, host, path, data, overwrite=False, verify=True):
        self.puts.append((path, overwrite, verify))
        if path in self.files and not overwrite:
            raise FileExistsError(path)
        self.files[path] = bytes(data)


def fake_hold(info, full=FULL, client=None, restored=True, events=None):
    client = client or FakeClient()

    @contextlib.contextmanager
    def hold_fn(out, lock_timeout=None):
        h = {"target": SimpleNamespace(transport=SimpleNamespace(client=client)),
             "info": info, "before": full, "restored": False}
        if events is not None:
            events.append("hold")
        yield h
        h["restored"] = restored
    return hold_fn


# --- detection -----------------------------------------------------------------------

@pytest.mark.parametrize("info,kind,mhz,vol,vols", [
    (U64E_INFO, "U64E", 48, "/USB1", ("Flash", "Temp", "USB1")),
    (C64U_INFO, "C64U", 64, "/SD", ("SD", "Flash", "Temp")),
])
def test_probe_detects(tmp_path, info, kind, mhz, vol, vols):
    st = FakeStorage(vols)
    det = D.probe("h", str(tmp_path), hold_fn=fake_hold(info), storage=st)
    assert (det.kind, det.mhz, det.vol) == (kind, mhz, vol)
    assert det.fw_tested and det.reu16 and det.ci and det.sampler and det.restored
    assert det.files == {}


def test_probe_overrides_and_files(tmp_path):
    raw = b"adf-bytes"
    sha = hashlib.sha256(raw).hexdigest()
    own = D.adf_name(sha)
    st = FakeStorage(("SD", "USB1"), {f"/SD/amiga64/{own}": raw, "/SD/amiga64/wb-snap.bin": b"s"})
    det = D.probe("h", str(tmp_path), mhz=16, vol="/sd", adf_sha=sha,
                  hold_fn=fake_hold(U64E_INFO), storage=st)
    assert det.mhz == 16 and det.vol == "/SD" and det.adf_use == own and det.adf_differs == []
    assert set(det.files) == {own, "wb-snap.bin"}
    st.files[f"/SD/amiga64/{own}"] = b"other"
    det = D.probe("h", str(tmp_path), vol="/SD", adf_sha=sha, hold_fn=fake_hold(U64E_INFO), storage=st)
    assert det.adf_use is None and det.adf_differs == [own]


def test_probe_reuses_the_tests_copy_only_with_the_same_bytes(tmp_path):
    raw = b"adf-bytes"
    sha = hashlib.sha256(raw).hexdigest()
    st = FakeStorage(("USB1",), {f"/USB1/amiga64/{D.TESTED_ADF_NAME}": raw})
    det = D.probe("h", str(tmp_path), adf_sha=sha, hold_fn=fake_hold(U64E_INFO), storage=st)
    assert det.adf_use == D.TESTED_ADF_NAME
    st.files[f"/USB1/amiga64/{D.TESTED_ADF_NAME}"] = b"another image"
    det = D.probe("h", str(tmp_path), adf_sha=sha, hold_fn=fake_hold(U64E_INFO), storage=st)
    assert det.adf_use is None and det.adf_differs == [D.TESTED_ADF_NAME]


def test_probe_services_and_temp(tmp_path):
    full = {k: dict(v) for k, v in FULL.items()}
    full["Network Settings"]["Ultimate DMA Service"] = "Disabled"
    st = FakeStorage(("USB1",), {"/Temp/temp0001": b"x", "/Temp/tempABCDEF": b"y",
                                 "/Temp/mine.txt": b"z", "/Temp/temp00x1": b"w"})
    det = D.probe("h", str(tmp_path), hold_fn=fake_hold(U64E_INFO, full=full), storage=st)
    assert det.dma is False and det.ftp is True
    assert det.temp_managed == ["temp0001", "tempABCDEF"]
    full["Network Settings"]["FTP File Service"] = "Disabled"
    with pytest.raises(D.DeviceError) as e:
        D.probe("h", str(tmp_path), hold_fn=fake_hold(U64E_INFO, full=full), storage=st)
    assert "FTP File Service" in str(e.value)


def test_probe_untested_firmware_and_missing_items(tmp_path):
    info = dict(U64E_INFO, firmware_version="3.99")
    det = D.probe("h", str(tmp_path), hold_fn=fake_hold(info, full={"X": {}},
                                                        client=FakeClient(["1 MB"])),
                  storage=FakeStorage())
    assert not det.fw_tested and det.reu16 is False and det.ci is False and det.sampler is False


def test_probe_refuses_another_product(tmp_path):
    with pytest.raises(D.DeviceError) as e:
        D.probe("h", str(tmp_path), hold_fn=fake_hold({"product": "Ultimate-II+"}),
                storage=FakeStorage())
    assert "Ultimate-II+" in str(e.value) and "--kind" in str(e.value)


def test_probe_reports_a_restore_that_failed(tmp_path):
    det = D.probe("h", str(tmp_path), hold_fn=fake_hold(U64E_INFO, restored=False),
                  storage=FakeStorage())
    assert det.restored is False


@pytest.mark.parametrize("exc,words", [
    (type("DeviceLockTimeout", (TimeoutError,), {})("x"), ["busy"]),
    (ConnectionRefusedError("refused"), ["cannot reach", "switched on"]),
])
def test_probe_errors_are_plain(tmp_path, exc, words):
    @contextlib.contextmanager
    def hold_fn(out, lock_timeout=None):
        raise exc
        yield
    with pytest.raises(D.DeviceError) as e:
        D.probe("10.1.2.3", str(tmp_path), hold_fn=hold_fn, storage=FakeStorage())
    for w in words:
        assert w in str(e.value)
    assert "10.1.2.3" in str(e.value)


@pytest.mark.parametrize("vols,pref,over,want", [
    (["USB1", "SD"], "SD", None, "/SD"),
    (["USB1"], "SD", None, "/USB1"),
    (["USB1", "SD"], "SD", "/usb1", "/USB1"),
])
def test_choose_volume(vols, pref, over, want):
    assert D.choose_volume(vols, pref, over) == want


@pytest.mark.parametrize("vols,over,words", [
    ([], None, ["no writable storage"]),
    (["USB1"], "/Flash", ["never writes"]),
    (["USB1"], "/Temp", ["never writes"]),
    (["USB1"], "/SD", ["not a writable volume", "/USB1"]),
])
def test_choose_volume_refuses(vols, over, words):
    with pytest.raises(D.DeviceError) as e:
        D.choose_volume(vols, "SD", over)
    for w in words:
        assert w in str(e.value)


def test_adf_names():
    assert D.adf_name("ab" * 32) == "wb-abababababab.adf"
    assert D.adf_candidates("ab" * 32) == ["wb-abababababab.adf", "amiga-os-310-workbench.adf"]


def test_ensure_adf_uploads_once_never_overwrites():
    st = FakeStorage()
    assert D.ensure_adf("h", "/USB1", "x.adf", b"abc", storage=st) == "uploaded"
    assert st.puts == [("/USB1/amiga64/x.adf", False, True)]
    assert D.ensure_adf("h", "/USB1", "x.adf", b"abc", storage=st) == "present"
    with pytest.raises(D.DeviceError) as e:
        D.ensure_adf("h", "/USB1", "x.adf", b"abd", storage=st)
    assert "never overwrites" in str(e.value)
    assert st.files["/USB1/amiga64/x.adf"] == b"abc"
    assert all(ow is False for _, ow, _ in st.puts)


def test_set_env(monkeypatch):
    monkeypatch.delenv("U64_HOST", raising=False)
    monkeypatch.delenv("U64_PASSWORD", raising=False)
    D.set_env("1.2.3.4", None)
    assert os.environ["U64_HOST"] == "1.2.3.4" and "U64_PASSWORD" not in os.environ
    D.set_env("1.2.3.4", "pw")
    assert os.environ["U64_PASSWORD"] == "pw"


def test_no_password_on_the_command_line():
    for cmd in ("doctor", "run", "resume"):
        with pytest.raises(SystemExit):
            cli.parser().parse_args([cmd, "--password", "x"])


# --- the session chain ---------------------------------------------------------------

def test_hold_loop():
    t = [0.0]
    assert S.hold_loop(0.05, clock=lambda: t[0], sleep=lambda d: t.__setitem__(0, t[0] + d)) is False
    assert t[0] == pytest.approx(3.0)

    def ctrl_c(d):
        raise KeyboardInterrupt
    assert S.hold_loop(None, sleep=ctrl_c) is True

    def term(d):
        raise S.Terminated("15")
    with pytest.raises(S.Terminated):
        S.hold_loop(None, sleep=term)


@pytest.mark.parametrize("code,words", [("E3", "different amiga64 build"), ("E4", "AROS ROM"),
                                        ("E6", "no saved session")])
def test_refusal_messages(code, words):
    m = S.refusal(f"resume refused: ${code} (x)")
    assert words in m and "amiga64 run" in m


def test_make_disk_is_the_checked_session_image(tmp_path):
    raw = wbdisk()
    f = tmp_path / "n.txt"
    f.write_bytes(b"note")
    reqs = [A.Request(str(f))]
    d, rep = S.make_disk(raw, PLAYER, MOD, reqs)
    img, _ = A.session_image(raw, PLAYER, MOD, reqs)
    assert d.image == img and rep.added == ["n.txt"]
    from host import adf_overlay as O
    assert O.apply(raw, d.ov) == img
    assert d.files["ModPlayer"] and d.icon is not None and d.standin is False


class FakeM:
    def __init__(self, ev):
        self.ev, self.prg, self.vice = ev, "/p/runner.prg", False

    def lab(self, n):
        return {"inp_src": 0x100, "inp_stuck": 0x200}[n]

    def w(self, a, b):
        self.ev.append(("w", a, bytes(b)))


@pytest.fixture
def fake_wb(monkeypatch):
    from tests import aros_resume as RS
    from tests import aros_wb as WB
    ev = []
    monkeypatch.setattr(WB, "model_wb", WB.model_wb)    # the globals S._globals sets:
    monkeypatch.setattr(WB, "ADF_NAME", WB.ADF_NAME)    # restored after each test
    for n in ("AUDIO", "AUD_S", "DEMO_MOUSE", "STUCK"):
        monkeypatch.setattr(WB, n, list(getattr(WB, n)))
    monkeypatch.setattr(WB, "session", lambda prg, be, out, mhz, fn: (ev.append(("session", be, mhz)),
                                                                      fn(FakeM(ev)))[1])
    monkeypatch.setattr(WB, "enable_ci", lambda m, rep: ev.append("ci"))

    def boot(m, rom, disk, s, rep, vol, timeout, want_screen=True, t_start=None, after_stage=None):
        ev.append(("boot", want_screen, after_stage is not None, vol))
        after_stage()
        return []
    monkeypatch.setattr(WB, "boot_workbench", boot)
    monkeypatch.setattr(WB, "arm_spr", lambda m: ev.append("spr"))
    monkeypatch.setattr(WB, "demo_mouse", lambda m, s: ev.append(("mouse", WB.DEMO_MOUSE[0])))
    monkeypatch.setattr(WB, "_suspend", lambda m, sn, path, rep: ev.append(("suspend", path)))

    def resume_wb(m, rom, path, rep):
        ev.append(("resume", path))
        return 9.0
    monkeypatch.setattr(WB, "resume_workbench", resume_wb)

    class Snap:
        def __init__(self, m, prg, rid):
            pass

        def declare_rid(self):
            ev.append("rid")
    monkeypatch.setattr(RS, "Snap", Snap)
    monkeypatch.setattr(RS, "rom_id", lambda rom: b"r")
    monkeypatch.setattr(S, "hold_loop", lambda minutes: (ev.append(("hold", minutes)), False)[1])
    yield ev, WB


def _plan(tmp_path, **kw):
    p = dict(backend="u64", prg="/p/runner.prg", host="h", mhz=48, vol="/USB1", out=str(tmp_path),
             minutes=0.0)
    p.update(kw)
    return S.Plan(**p)


def test_run_chain(tmp_path, fake_wb, monkeypatch):
    ev, WB = fake_wb
    st = FakeStorage()
    monkeypatch.setattr(D, "ensure_adf", lambda host, vol, name, raw, **k: (ev.append(("adf", vol, name)),
                                                                        "uploaded")[1])
    disk = SimpleNamespace(raw=b"raw")
    fails, rep = S.run(_plan(tmp_path, upload=True, mouse_port=1, adf_name="x.adf"), "ROM", disk)
    assert fails == []
    assert ev == [("session", "u64", 48), "ci", ("adf", "/USB1", "x.adf"),
                  ("boot", False, True, "/USB1"), "rid",
                  ("w", 0x100, b"\0\0\0"), "spr", ("mouse", 1), ("hold", 0.0)]
    assert disk.adf_name == "x.adf" and WB.AUDIO[0] is True
    assert WB.model_wb("trim")[0] is None           # no boot model in the attended run
    del st


def test_run_save_suspends_to_the_snapshot_file(tmp_path, fake_wb):
    ev, WB = fake_wb
    fails, rep = S.run(_plan(tmp_path, save=True, stuck=1), "ROM", SimpleNamespace(raw=b""))
    assert ("suspend", "/USB1/amiga64/wb-snap.bin") in ev and rep["saved"].endswith("wb-snap.bin")
    assert ("w", 0x200, b"\x01") in ev
    assert ("adf", "/USB1", "amiga-os-310-workbench.adf") not in ev     # upload only on request


def test_run_without_save_never_suspends(tmp_path, fake_wb):
    ev, WB = fake_wb
    S.run(_plan(tmp_path), "ROM", SimpleNamespace(raw=b""))
    assert not any(isinstance(e, tuple) and e[0] == "suspend" for e in ev)


def test_run_boot_failure_is_reported(tmp_path, fake_wb, monkeypatch):
    ev, WB = fake_wb
    monkeypatch.setattr(WB, "boot_workbench", lambda *a, **k: ["w1: never read"])
    fails, rep = S.run(_plan(tmp_path, save=True), "ROM", SimpleNamespace(raw=b""))
    assert fails == ["w1: never read"]
    assert not any(isinstance(e, tuple) and e[0] in ("hold", "suspend") for e in ev)


def test_resume_chain_and_refusal(tmp_path, fake_wb, monkeypatch):
    ev, WB = fake_wb
    fails, rep = S.resume(_plan(tmp_path, mouse_port=2), "ROM")
    assert fails == [] and ("resume", "/USB1/amiga64/wb-snap.bin") in ev and ("mouse", 2) in ev

    def refused(*a):
        raise RuntimeError("resume refused: $E3 (build ID)")
    monkeypatch.setattr(WB, "resume_workbench", refused)
    fails, rep = S.resume(_plan(tmp_path), "ROM")
    assert len(fails) == 1 and "different amiga64 build" in fails[0]


# --- the CLI -------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    for v in ("AMIGA64_HOST", "U64_HOST", "U64_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AMIGA64_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("AMIGA64_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "adf").mkdir()
    (tmp_path / "adf" / "wb.adf").write_bytes(wbdisk())
    monkeypatch.setattr(kit, "verify", lambda: [])
    monkeypatch.setattr(kit, "build_id", lambda: "0304ca617baf5a29")
    monkeypatch.setattr(kit, "player", lambda: PLAYER)
    monkeypatch.setattr(kit, "song", lambda: MOD)
    monkeypatch.setattr(kit, "runner", lambda: "/p/runner.prg")
    monkeypatch.setattr(kit, "rom", lambda: (SimpleNamespace(cache="/x/third_party/aros"), ""))
    return tmp_path


def _det(**kw):
    d = D.Detected("h", product="Ultimate 64 Elite", kind="U64E", firmware="3.15", mhz=48,
                   vol="/USB1", volumes=["USB1"], reu16=True, ci=True, sampler=True, restored=True,
                   dma=True, ftp=True)
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def _run(argv, capsys):
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_doctor_offline(home, capsys):
    rc, out, err = _run(["doctor", "--offline"], capsys)
    assert rc == 0, out + err
    assert "OK   AROS ROM: bundled" in out
    assert "WARN Workbench ADF" in out and "untested" in out
    assert "OK   session disk" in out
    assert "checking the Ultimate" not in out


def test_doctor_no_adf(home, capsys):
    os.remove(home / "adf" / "wb.adf")
    rc, out, err = _run(["doctor", "--offline"], capsys)
    assert rc == 1 and "FAIL Workbench ADF: no Workbench ADF" in out and "--adf" in out


def test_doctor_bad_install(home, capsys, monkeypatch):
    monkeypatch.setattr(kit, "verify", lambda: ["prebuilt/runner.prg: SHA-256 differs"])
    monkeypatch.setattr(kit, "rom", lambda: (None, "aros-rom.bin SHA256 mismatch"))
    rc, out, err = _run(["doctor", "--offline"], capsys)
    assert rc == 1 and "FAIL prebuilt binaries" in out and "FAIL AROS ROM" in out


def test_doctor_device(home, capsys, monkeypatch):
    seen = {}

    def probe(host, out, kind, mhz, vol, adf_sha):
        seen.update(host=host, adf=adf_sha, env=os.environ.get("U64_HOST"))
        return _det(files={"wb-snap.bin": 1})
    monkeypatch.setattr(D, "probe", probe)
    rc, out, err = _run(["doctor", "--host", "10.9.9.9"], capsys)
    assert rc == 0, out
    assert seen["host"] == seen["env"] == "10.9.9.9"
    assert seen["adf"] == hashlib.sha256(wbdisk()).hexdigest()
    assert "OK   device: Ultimate 64 Elite (U64E)" in out
    assert "not yet on /USB1/amiga64/" in out and "wb-snap.bin present" in out
    assert "OK   device settings: restored exactly" in out


@pytest.mark.parametrize("kw,line", [
    (dict(reu16=False), "FAIL REU"), (dict(ci=False), "FAIL Command Interface"),
    (dict(restored=False), "FAIL device settings"), (dict(firmware="9.9"), "WARN firmware"),
    (dict(sampler=False), "WARN audio"), (dict(dma=False), "FAIL DMA service"),
    (dict(temp_managed=["temp0001"]), "OK   /Temp: 1 firmware file (temp0001): normal"),
    (dict(temp_managed=["temp0001", "temp0002"]), "WARN /Temp: 2 leftover firmware files"),
    (dict(adf_use="amiga-os-310-workbench.adf"), "OK   ADF on the device: reusing"),
])
def test_doctor_device_problems(home, capsys, monkeypatch, kw, line):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(**kw))
    rc, out, err = _run(["doctor", "--host", "h"], capsys)
    assert line in out
    assert rc == (1 if line.startswith("FAIL") else 0)


def test_doctor_unreachable(home, capsys, monkeypatch):
    def probe(*a, **k):
        raise D.DeviceError("cannot reach the Ultimate at h")
    monkeypatch.setattr(D, "probe", probe)
    rc, out, err = _run(["doctor", "--host", "h"], capsys)
    assert rc == 1 and "FAIL device: cannot reach" in out


def test_doctor_without_a_host(home, capsys):
    rc, out, err = _run(["doctor"], capsys)
    assert rc == 1 and "FAIL device: no device address" in out


def test_run_without_a_terminal_names_the_flag(home, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc, out, err = _run(["run"], capsys)
    assert rc == 1 and "--host" in err


def test_run_first_time(home, capsys, monkeypatch):
    """Asked for the host, the copy confirmed, the settings written."""
    calls = {}
    monkeypatch.setattr(D, "probe", lambda host, out, kind, mhz, vol, adf_sha: _det(host=host))

    def fake_run(plan, rom, disk, rep=None):
        calls["plan"] = plan
        return [], {}
    monkeypatch.setattr(S, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", type("T", (io.StringIO,), {"isatty": lambda s: True})(
        "10.43.23.81\ny\n"))
    rc, out, err = _run(["run", "--minutes", "0"], capsys)
    assert rc == 0, out + err
    p = calls["plan"]
    assert (p.host, p.mhz, p.vol, p.upload, p.audio) == ("10.43.23.81", 48, "/USB1", True, True)
    assert p.adf_name.startswith("wb-") and p.minutes == 0.0
    cfg = C.load()
    assert cfg["device"]["host"] == "10.43.23.81"
    assert cfg["disk"]["adf"] == str(home / "adf" / "wb.adf")
    assert "Copy wb.adf (880 KB)" in out


def test_run_declined_copy_does_nothing(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det())
    monkeypatch.setattr(S, "run", lambda *a: pytest.fail("no session without the ADF"))
    monkeypatch.setattr(sys, "stdin", type("T", (io.StringIO,), {"isatty": lambda s: True})("n\n"))
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 1 and "nothing copied" in out


def test_run_without_sampler_runs_silent(home, capsys, monkeypatch):
    raw = (home / "adf" / "wb.adf").read_bytes()
    name = D.adf_name(hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={name: 1}, adf_use=name,
                                                          sampler=False))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 0 and got["p"].audio is False and got["p"].upload is False


def test_run_reuses_the_tests_copy(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(adf_use=D.TESTED_ADF_NAME))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 0 and got["p"].adf_name == D.TESTED_ADF_NAME and got["p"].upload is False
    assert "reusing /USB1/amiga64/amiga-os-310-workbench.adf" in out


def test_run_refuses_without_the_dma_service(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(dma=False))
    monkeypatch.setattr(S, "run", lambda *a: pytest.fail("never"))
    rc, out, err = _run(["run", "--host", "h", "--yes"], capsys)
    assert rc == 1 and "Ultimate DMA Service is off" in err


def test_resume_needs_a_snapshot(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={}))
    rc, out, err = _run(["resume", "--host", "h"], capsys)
    assert rc == 1 and "no saved session" in err


def test_adf_add_ls_rm(home, capsys):
    f = home / "Hello.txt"
    f.write_bytes(b"hello")
    rc, out, err = _run(["adf", "add", str(f)], capsys)
    assert rc == 0 and "staged Hello.txt" in out and "unchanged" in out
    rc, out, err = _run(["adf", "ls"], capsys)
    assert rc == 0 and "Hello.txt" in out and "(staged)" in out and "ModPlayer" in out
    rc, out, err = _run(["adf", "ls", "--original"], capsys)
    assert "Hello.txt" not in out and "ModPlayer" not in out
    rc, out, err = _run(["adf", "add", str(f)], capsys)
    assert rc == 1 and "already exists" in err
    rc, out, err = _run(["adf", "rm", "hello.txt"], capsys)
    assert rc == 0 and A.Staged().requests() == []
    rc, out, err = _run(["adf", "rm", "hello.txt"], capsys)
    assert rc == 1 and "removes nothing from the disk" in out


def test_adf_add_out(home, capsys):
    f = home / "Hello.txt"
    f.write_bytes(b"hello")
    rc, out, err = _run(["adf", "add", str(f), "--to", "S", "--out", "new.adf"], capsys)
    assert rc == 0 and (home / "new.adf").exists() and A.Staged().requests() == []
    assert (home / "adf" / "wb.adf").read_bytes() == wbdisk()


def test_adf_add_as_needs_one_path(home, capsys):
    rc, out, err = _run(["adf", "add", "a", "b", "--as", "X"], capsys)
    assert rc == 1 and "--as names one file" in err


def test_config_shows_sources_not_the_password(home, capsys, monkeypatch):
    C.save({"device": {"host": "1.2.3.4", "password": "sekrit"}})
    rc, out, err = _run(["config"], capsys)
    assert "1.2.3.4 (from config)" in out and "sekrit" not in out and "password:    set" in out


def test_run_on_vice_needs_no_device(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: pytest.fail("VICE needs no device"))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    rc, out, err = _run(["run", "--backend", "vice", "--minutes", "0"], capsys)
    assert rc == 0, err
    p = got["p"]
    assert (p.backend, p.host, p.vol, p.audio, p.upload) == ("vice", None, None, False, False)
    rc, out, err = _run(["run", "--backend", "vice", "--save"], capsys)
    assert rc == 1 and "--save needs an Ultimate" in err


def test_stage_disk_loads_the_named_file(monkeypatch):
    """tests/aros_wb.stage_disk: the device file is the disk's adf_name (the
    CLI's), else the tests' default name."""
    import struct
    from host import machine
    from tests import aros_wb as WB
    from tests import harness_util as HU
    monkeypatch.setattr(HU, "call_runner", lambda *a, **k: None)
    img = bytes(64)

    class M:
        vice = False
        t = SimpleNamespace(socket_dma_reu_write=lambda a, b: None)
        tg = None

        def __init__(self):
            self.writes = {}

        def lab(self, n):
            return hash(n) & 0xFFFF

        def w(self, a, b):
            self.writes[a] = bytes(b)

        def r(self, a, n):
            return bytes(n)

        def call(self, name):
            return struct.pack("<II", 0xFF, machine.fletcher(img))

    for name, want in ((None, b"/USB1/amiga64/amiga-os-310-workbench.adf"),
                       ("wb-0123456789ab.adf", b"/USB1/amiga64/wb-0123456789ab.adf")):
        disk = SimpleNamespace(script="trim", image=img, ov=SimpleNamespace(blocks=[]))
        if name:
            disk.adf_name = name
        m = M()
        monkeypatch.setattr(WB.O, "reu_writes", lambda ov, base: [])
        WB.stage_disk(m, disk, "/USB1")
        assert m.writes[m.lab("dosio_name")] == want


# --- W17-K2: the rev17-k fixes ---------------------------------------------------------

def _tty(text):
    return type("T", (io.StringIO,), {"isatty": lambda s: True})(text)


def _no_dt_disk():
    from host.tests.test_adf_files import blob, disk
    return disk(True, {"Classes/DataTypes/other.datatype": blob(100)})


def test_run_and_doctor_refuse_a_disk_without_the_datatype(home, capsys, monkeypatch):
    """rev17-k #2: a plain message before any device contact, never a traceback."""
    (home / "adf" / "wb.adf").write_bytes(_no_dt_disk())
    monkeypatch.setattr(D, "probe", lambda *a, **k: pytest.fail("no device contact"))
    rc, out, err = _run(["run", "--host", "192.0.2.1"], capsys)
    assert rc == 1 and "lacks Classes/DataTypes/amigaguide.datatype" in err
    assert "Traceback" not in out + err
    rc, out, err = _run(["doctor", "--offline"], capsys)
    assert rc == 1 and "FAIL Workbench ADF" in out and "use a Workbench 3.1 disk" in out


def test_make_disk_turns_an_adf_error_into_a_plain_one(monkeypatch):
    from host import adf_overlay as O
    from tests import aros_modwb as MW

    def boom(*a, **k):
        raise O.AdfError("Classes/DataTypes/amigaguide.datatype: not on the disk")
    monkeypatch.setattr(MW, "ModDisk", boom)
    with pytest.raises(C.ConfigError) as e:
        S.make_disk(wbdisk(), PLAYER, MOD, [])
    assert "amigaguide.datatype" in str(e.value) and "session disk" in str(e.value)


def _own(home):
    return D.adf_name(hashlib.sha256((home / "adf" / "wb.adf").read_bytes()).hexdigest())


def test_run_offers_to_replace_its_own_broken_copy(home, capsys, monkeypatch):
    """rev17-k #3: wb-<sha12>.adf is amiga64's own; other bytes there can only
    be an interrupted copy: replaced after a yes."""
    name = _own(home)
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={name: 4096}, adf_differs=[name]))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    monkeypatch.setattr(sys, "stdin", _tty("y\n"))
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 0, out + err
    assert "not a whole copy" in out and "Replace it" in out
    assert got["p"].upload is True and got["p"].replace is True and got["p"].adf_name == name


@pytest.mark.parametrize("answer", ["n\n", "\n"])      # the default is no
def test_run_declines_to_replace(home, capsys, monkeypatch, answer):
    name = _own(home)
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={name: 4096}, adf_differs=[name]))
    monkeypatch.setattr(S, "run", lambda *a: pytest.fail("never"))
    monkeypatch.setattr(sys, "stdin", _tty(answer))
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 1 and "nothing replaced" in out
    assert not os.path.exists(C.config_path())          # declined: no settings written


def test_run_without_a_terminal_names_the_replace_flag(home, capsys, monkeypatch):
    name = _own(home)
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={name: 4096}, adf_differs=[name]))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    rc, out, err = _run(["run", "--host", "h", "--yes"], capsys)      # --yes alone never replaces
    assert rc == 1 and "--replace-adf" in err and not got
    rc, out, err = _run(["run", "--host", "h", "--replace-adf"], capsys)
    assert rc == 0, err
    assert got["p"].upload is True and got["p"].replace is True


def test_run_never_replaces_the_tests_copy(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={D.TESTED_ADF_NAME: 901120},
                                                          adf_differs=[D.TESTED_ADF_NAME]))
    got = {}
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (got.setdefault("p", plan), ([], {}))[1])
    rc, out, err = _run(["run", "--host", "h", "--yes"], capsys)
    assert rc == 0, err
    assert got["p"].adf_name == _own(home) and got["p"].upload and not got["p"].replace


def test_ensure_adf_replaces_only_its_own_name():
    raw = b"the user's adf"
    own = D.adf_name(hashlib.sha256(raw).hexdigest())
    st = FakeStorage(files={f"/USB1/amiga64/{own}": b"trunc", f"/USB1/amiga64/{D.TESTED_ADF_NAME}": b"t"})
    assert D.ensure_adf("h", "/USB1", own, raw, storage=st, replace=True) == "replaced"
    assert st.files[f"/USB1/amiga64/{own}"] == raw and st.puts[-1] == (f"/USB1/amiga64/{own}", True, True)
    for name in (D.TESTED_ADF_NAME, "wb-000000000000.adf"):
        with pytest.raises(D.DeviceError):
            D.ensure_adf("h", "/USB1", name, raw, storage=st, replace=True)
    assert st.files[f"/USB1/amiga64/{D.TESTED_ADF_NAME}"] == b"t"


def test_doctor_names_the_replace_path(home, capsys, monkeypatch):
    name = _own(home)
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(files={name: 4096}, adf_differs=[name]))
    rc, out, err = _run(["doctor", "--host", "h"], capsys)
    assert "WARN ADF on the device" in out and "interrupted copy" in out and "--replace-adf" in out
    assert rc == 0


@pytest.mark.parametrize("v", ["0x100", "-1", "256", "x"])
def test_stuck_is_validated_by_argparse(home, capsys, v):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "--stuck", v])
    assert e.value.code == 2 and "--stuck" in capsys.readouterr().err


def test_stuck_accepts_a_mask():
    a = cli.parser().parse_args(["run", "--stuck", "0x1f"])
    assert a.stuck == 0x1F


SPEEDS_U64E = [" 1", " 2", " 3", " 4", " 5", " 6", " 8", "10", "12", "14", "16", "20", "24", "32",
               "40", "48"]
FULL_SPEED = dict(FULL, **{"U64 Specific Settings": {"CPU Speed": "48", "Turbo Control": "Off"}})


class SpeedClient(FakeClient):
    def __init__(self, speeds):
        super().__init__()
        self.speeds = speeds

    def get_config_choices(self, cat, item):
        return self.speeds if item == "CPU Speed" else super().get_config_choices(cat, item)


@pytest.mark.parametrize("kw", [dict(mhz=64), dict(kind="C64U")])
def test_probe_refuses_a_speed_the_device_lacks(tmp_path, kw):
    """rev17-k #5: checked in the probe, before the session's hold."""
    with pytest.raises(D.DeviceError) as e:
        D.probe("h", str(tmp_path), hold_fn=fake_hold(U64E_INFO, full=FULL_SPEED,
                                                      client=SpeedClient(SPEEDS_U64E)),
                storage=FakeStorage(), **kw)
    assert "64 MHz" in str(e.value) and "48" in str(e.value) and "--mhz" in str(e.value)
    det = D.probe("h", str(tmp_path), mhz=40, hold_fn=fake_hold(U64E_INFO, full=FULL_SPEED,
                                                                client=SpeedClient(SPEEDS_U64E)),
                  storage=FakeStorage())
    assert det.mhz == 40


@pytest.mark.parametrize("section,key,val", [("device", "mhz", "x"), ("input", "mouse_port", "y")])
def test_bad_config_values_are_plain(home, capsys, monkeypatch, section, key, val):
    cfg = {"device": {"host": "h"}}
    cfg.setdefault(section, {})[key] = val
    C.save(cfg)
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det())
    monkeypatch.setattr(S, "run", lambda *a: pytest.fail("never"))
    rc, out, err = _run(["run", "--yes"], capsys)
    assert rc == 1 and key in err and "Traceback" not in err


def test_an_unexpected_error_is_one_line_and_logged(home, capsys, monkeypatch):
    """Never a traceback (the hold restores the device first): one line, the
    details in the state directory."""
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(adf_use=_own(home)))

    def boom(*a):
        raise ValueError("speed 64 not supported")
    monkeypatch.setattr(S, "run", boom)
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 1 and "Traceback" not in err
    assert "speed 64 not supported" in err and "last-error.txt" in err
    assert "Traceback" in open(os.path.join(C.state_dir(), "last-error.txt")).read()


def test_ping_dirs_on_path(monkeypatch):
    """rev17-k #8: the harness's liveness probe runs a bare `ping`."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    D.set_env("h", None)
    parts = os.environ["PATH"].split(os.pathsep)
    assert parts[:2] == ["/usr/bin", "/bin"] and "/sbin" in parts and "/usr/sbin" in parts
    D.set_env("h", None)
    assert os.environ["PATH"].split(os.pathsep).count("/sbin") == 1


def test_doctor_ftp_unknown(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(ftp=None))
    rc, out, err = _run(["doctor", "--host", "h"], capsys)
    assert "FTP service: unknown" in out and "FTP File Service enabled" not in out


def test_an_interrupted_run_writes_its_report(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det(adf_use=_own(home)))

    def run(plan, rom, disk, rep=None):
        rep["boot"] = {"stage": "rom"}
        raise KeyboardInterrupt
    monkeypatch.setattr(S, "run", run)
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 130
    import glob
    import json
    reps = glob.glob(os.path.join(C.state_dir(), "holds", "*-run", "session.json"))
    assert len(reps) == 1
    r = json.load(open(reps[0]))
    assert r["ended_by"] == "interrupted" and r["boot"] == {"stage": "rom"}


def test_a_declined_copy_saves_no_settings(home, capsys, monkeypatch):
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det())
    monkeypatch.setattr(S, "run", lambda *a: pytest.fail("never"))
    monkeypatch.setattr(sys, "stdin", _tty("n\n"))
    rc, out, err = _run(["run", "--host", "h"], capsys)
    assert rc == 1 and "nothing copied" in out
    assert not os.path.exists(C.config_path()) and "saved your settings" not in out


def test_doctor_asks_for_the_address_on_a_terminal(home, capsys, monkeypatch):
    """rev17-docs A: like run, doctor asks on a terminal and saves the answer
    after a probe that worked."""
    seen = {}
    monkeypatch.setattr(D, "probe", lambda host, *a, **k: (seen.setdefault("host", host), _det())[1])
    monkeypatch.setattr(sys, "stdin", _tty("10.43.23.81\n"))
    rc, out, err = _run(["doctor"], capsys)
    assert rc == 0, out + err
    assert seen["host"] == "10.43.23.81" and "Address of your Ultimate" in out
    assert C.load()["device"]["host"] == "10.43.23.81"


def test_doctor_saves_no_address_when_the_probe_fails(home, capsys, monkeypatch):
    def probe(*a, **k):
        raise D.DeviceError("cannot reach the Ultimate at 10.9.9.9")
    monkeypatch.setattr(D, "probe", probe)
    monkeypatch.setattr(sys, "stdin", _tty("10.9.9.9\n"))
    rc, out, err = _run(["doctor"], capsys)
    assert rc == 1 and "FAIL device: cannot reach" in out
    assert not os.path.exists(C.config_path())


def test_doctor_without_a_terminal_names_the_flag(home, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(D, "probe", lambda *a, **k: pytest.fail("no address"))
    rc, out, err = _run(["doctor"], capsys)
    assert rc == 1 and "FAIL device: no device address" in out and "--host" in out


def test_the_lock_wait_message_matches_the_harness():
    """rev17-docs B: the harness extends the wait while the same live holder
    makes progress; the message says what a timeout really means."""
    e = type("DeviceLockTimeout", (TimeoutError,), {})("x")
    m = D.explain("10.1.2.3", e)
    assert "stayed busy" in m and "stopped making progress for 300 s" in m
    assert "did not become free in" not in m


# --- W17-K2 (supervisor): the DeviceLock key, and the environment untouched ----------------

def test_doctor_names_the_lock_key(home, capsys, monkeypatch, tmp_path):
    """The key the harness's DeviceLock uses for this address: a uid- key
    when devices.toml (or C64_DEVICE_ALIASES) lists it, else the address."""
    from c64_test_harness.backends import device_aliases as DA
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det())
    monkeypatch.delenv("C64_DEVICE_ALIASES", raising=False)
    monkeypatch.setenv("C64_DEVICE_ALIASES_FILE", str(tmp_path / "none.toml"))
    DA._reset_alias_cache()
    rc, out, err = _run(["doctor", "--host", "10.43.23.81"], capsys)
    assert ("OK   device lock: 10.43.23.81 (address key; tools using another address for the "
            "same device won't queue behind it)") in out
    monkeypatch.setenv("C64_DEVICE_ALIASES", "601A96=10.43.23.81")
    DA._reset_alias_cache()
    rc, out, err = _run(["doctor", "--host", "10.43.23.81"], capsys)
    DA._reset_alias_cache()
    assert ("OK   device lock: uid-601a96 (shared with other harness tools via devices.toml)"
            in out), out


ENV_KEYS = ("HOME", "XDG_CONFIG_HOME", "C64_DEVICE_ALIASES", "C64_DEVICE_ALIASES_FILE")


def test_the_cli_passes_home_and_the_alias_settings_through(home, capsys, monkeypatch, tmp_path):
    """tonight's collision: a scratch HOME hid devices.toml and the lock fell
    back to the address key.  amiga64 never changes these variables."""
    want = {"HOME": str(tmp_path / "h"), "XDG_CONFIG_HOME": str(tmp_path / "x"),
            "C64_DEVICE_ALIASES": "601a96=10.9.9.9", "C64_DEVICE_ALIASES_FILE": str(tmp_path / "d.toml")}
    for k, v in want.items():
        monkeypatch.setenv(k, v)
    seen = []
    monkeypatch.setattr(D, "probe", lambda *a, **k: (seen.append({k2: os.environ.get(k2) for k2 in ENV_KEYS}),
                                                     _det(adf_use=_own(home)))[1])
    monkeypatch.setattr(S, "run", lambda plan, rom, disk, rep=None: (
        seen.append({k2: os.environ.get(k2) for k2 in ENV_KEYS}), ([], {}))[1])
    for argv in (["doctor", "--host", "10.9.9.9"], ["run", "--host", "10.9.9.9"]):
        _run(argv, capsys)
    assert len(seen) == 3 and all(s == want for s in seen)
    assert {k: os.environ.get(k) for k in ENV_KEYS} == want


def _guarded(v) -> bool:
    return isinstance(v, str) and (v in ("HOME", "XDG_CONFIG_HOME") or v.startswith("C64_DEVICE_ALIASES"))


def env_writes(src: str) -> list:
    """Writes to HOME, XDG_CONFIG_HOME or C64_DEVICE_ALIASES*: environ[k] = / del,
    pop/setdefault/putenv/unsetenv(k), and any environ.update/clear (reads are fine)."""
    import ast
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and \
                isinstance(n.slice, ast.Constant) and _guarded(n.slice.value):
            out.append((n.lineno, n.slice.value))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("pop", "setdefault", "putenv", "unsetenv") and n.args and \
                    isinstance(n.args[0], ast.Constant) and _guarded(n.args[0].value):
                out.append((n.lineno, n.func.attr))
            if n.func.attr in ("update", "clear") and isinstance(n.func.value, ast.Attribute) \
                    and n.func.value.attr == "environ":
                out.append((n.lineno, "environ." + n.func.attr))
    return out


def test_no_code_writes_home_or_the_alias_settings():
    import glob
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    bad = [(os.path.basename(p), w) for p in glob.glob(os.path.join(root, "amiga64", "*.py"))
           for w in env_writes(open(p).read())]
    assert bad == []


@pytest.mark.parametrize("src", ["import os\nos.environ['HOME'] = '/tmp'\n",
                                 "import os\nos.environ.pop('C64_DEVICE_ALIASES', None)\n",
                                 "import os\ndel os.environ['XDG_CONFIG_HOME']\n",
                                 "import os\nos.environ.update(HOME='/x')\n"])
def test_the_env_scan_sees_writes(src):
    assert env_writes(src)


def test_a_broken_alias_config_is_a_warning(home, capsys, monkeypatch):
    from c64_test_harness.backends import device_aliases as DA
    monkeypatch.setattr(D, "probe", lambda *a, **k: _det())
    monkeypatch.setenv("C64_DEVICE_ALIASES", "not an alias list")
    DA._reset_alias_cache()
    try:
        rc, out, err = _run(["doctor", "--host", "10.43.23.81"], capsys)
    finally:
        monkeypatch.delenv("C64_DEVICE_ALIASES")
        DA._reset_alias_cache()
    assert "WARN device lock: key unknown" in out and rc == 0
