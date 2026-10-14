"""W17-K: amiga64/config.py -- the config file, the precedence rules and the
first-run prompts (docs/WAVE17_BRIEF.md "Rulings in force")."""

import io
import os
import stat

import pytest

from amiga64 import config as C


@pytest.fixture
def env(tmp_path, monkeypatch):
    for v in ("AMIGA64_HOST", "U64_HOST", "U64_PASSWORD", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
              "AMIGA64_CONFIG", "AMIGA64_STATE_DIR"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def test_config_path_follows_xdg(env, monkeypatch):
    assert C.config_path() == str(env / "xdg" / "amiga64" / "config.toml")
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert C.config_path() == str(env / "home" / ".config" / "amiga64" / "config.toml")


def test_state_dir_follows_xdg(env, monkeypatch):
    assert C.state_dir() == str(env / "home" / ".local" / "state" / "amiga64")
    monkeypatch.setenv("XDG_STATE_HOME", str(env / "st"))
    assert C.state_dir() == str(env / "st" / "amiga64")


def test_explicit_overrides(env, monkeypatch):
    monkeypatch.setenv("AMIGA64_CONFIG", str(env / "c.toml"))
    monkeypatch.setenv("AMIGA64_STATE_DIR", str(env / "s"))
    assert C.config_path() == str(env / "c.toml") and C.state_dir() == str(env / "s")


def test_round_trip_and_private_mode(env):
    data = {"device": {"host": "10.0.0.5", "mhz": 48, "password": 'a"b\\c'},
            "disk": {"adf": "/Users/x/My Disks/wb ä.adf"}, "input": {"mouse_port": 1}}
    p = C.save(data)
    assert C.load() == data
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600      # a password may be in it
    assert "# amiga64" in open(p).read()


def test_missing_file_is_empty(env):
    assert C.load() == {}


def test_bad_file_names_the_path(env):
    p = C.config_path()
    os.makedirs(os.path.dirname(p))
    with open(p, "w") as f:
        f.write("[device\nhost=")
    with pytest.raises(C.ConfigError) as e:
        C.load()
    assert p in str(e.value)


def test_unknown_types_are_refused(env):
    with pytest.raises(C.ConfigError):
        C.save({"device": {"host": ["a"]}})


@pytest.mark.parametrize("flag,amiga,u64,cfg,want", [
    ("1.1.1.1", "2.2.2.2", "3.3.3.3", "4.4.4.4", ("1.1.1.1", "--host")),
    (None, "2.2.2.2", "3.3.3.3", "4.4.4.4", ("2.2.2.2", "AMIGA64_HOST")),
    (None, None, "3.3.3.3", "4.4.4.4", ("3.3.3.3", "U64_HOST")),
    (None, None, None, "4.4.4.4", ("4.4.4.4", "config")),
    (None, None, None, None, (None, None)),
])
def test_host_precedence(env, monkeypatch, flag, amiga, u64, cfg, want):
    if amiga:
        monkeypatch.setenv("AMIGA64_HOST", amiga)
    if u64:
        monkeypatch.setenv("U64_HOST", u64)
    conf = {"device": {"host": cfg}} if cfg else {}
    assert C.resolve_host(flag, conf) == want


def test_password_from_env_or_config_only(env, monkeypatch):
    assert C.resolve_password({}) is None
    assert C.resolve_password({"device": {"password": "cfg"}}) == "cfg"
    monkeypatch.setenv("U64_PASSWORD", "envpw")
    assert C.resolve_password({"device": {"password": "cfg"}}) == "envpw"


def _adfdir(tmp_path, names):
    d = tmp_path / "work"
    (d / "adf").mkdir(parents=True)
    for n in names:
        (d / "adf" / n).write_bytes(b"x")
    return d


def test_adf_precedence(env, tmp_path):
    cwd = _adfdir(tmp_path, ["wb.adf"])
    f = tmp_path / "flag.adf"
    f.write_bytes(b"x")
    cf = tmp_path / "cfg.adf"
    cf.write_bytes(b"x")
    conf = {"disk": {"adf": str(cf)}}
    assert C.resolve_adf(str(f), conf, str(cwd)) == (str(f), "--adf", [])
    assert C.resolve_adf(None, conf, str(cwd)) == (str(cf), "config", [])
    assert C.resolve_adf(None, {}, str(cwd)) == (str(cwd / "adf" / "wb.adf"), "./adf/", [])


def test_adf_several_in_the_default_dir_is_a_choice(env, tmp_path):
    cwd = _adfdir(tmp_path, ["b.adf", "a.ADF", "notes.txt"])
    path, src, cands = C.resolve_adf(None, {}, str(cwd))
    assert path is None and src is None
    assert cands == [str(cwd / "adf" / "a.ADF"), str(cwd / "adf" / "b.adf")]


def test_adf_none(env, tmp_path):
    assert C.resolve_adf(None, {}, str(tmp_path)) == (None, None, [])


def test_a_missing_config_adf_falls_through(env, tmp_path):
    cwd = _adfdir(tmp_path, ["wb.adf"])
    conf = {"disk": {"adf": str(tmp_path / "gone.adf")}}
    assert C.resolve_adf(None, conf, str(cwd))[0] == str(cwd / "adf" / "wb.adf")


def test_prompt_needs_a_terminal():
    p = C.Prompter(stdin=io.StringIO("10.0.0.9\n"), stdout=io.StringIO(), tty=False)
    with pytest.raises(C.ConfigError) as e:
        p.ask_host()
    assert "--host" in str(e.value)
    with pytest.raises(C.ConfigError) as e:
        p.ask_adf("/x", [])
    assert "--adf" in str(e.value)
    with pytest.raises(C.ConfigError) as e:
        p.confirm("Copy?")
    assert "--yes" in str(e.value)


def test_prompt_host_strips_and_repeats_on_empty():
    out = io.StringIO()
    p = C.Prompter(stdin=io.StringIO("\n  10.0.0.9 \n"), stdout=out, tty=True)
    assert p.ask_host() == "10.0.0.9"
    assert out.getvalue().count("Address of your Ultimate") == 2


def test_prompt_adf_default_dir_and_choice(tmp_path):
    cwd = _adfdir(tmp_path, ["one.adf"])
    out = io.StringIO()
    p = C.Prompter(stdin=io.StringIO("\n"), stdout=out, tty=True)
    assert p.ask_adf(str(cwd), []) == str(cwd / "adf" / "one.adf")   # empty = ./adf/
    p = C.Prompter(stdin=io.StringIO("2\n"), stdout=io.StringIO(), tty=True)
    assert p.ask_adf(str(cwd), ["/a.adf", "/b.adf"]) == "/b.adf"
    p = C.Prompter(stdin=io.StringIO("~/x.adf\n"), stdout=io.StringIO(), tty=True)
    assert p.ask_adf(str(cwd), []) == os.path.expanduser("~/x.adf")


def test_prompt_eof_is_an_error():
    p = C.Prompter(stdin=io.StringIO(""), stdout=io.StringIO(), tty=True)
    with pytest.raises(C.ConfigError):
        p.ask_host()


@pytest.mark.parametrize("ans,want", [("\n", True), ("y\n", True), ("Yes\n", True),
                                      ("n\n", False), ("no\n", False)])
def test_confirm(ans, want):
    p = C.Prompter(stdin=io.StringIO(ans), stdout=io.StringIO(), tty=True)
    assert p.confirm("Copy?") is want
    assert C.Prompter(stdin=io.StringIO(""), stdout=io.StringIO(), tty=False,
                      yes=True).confirm("Copy?") is True
