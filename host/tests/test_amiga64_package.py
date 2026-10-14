"""W17-K: the packaging -- the wheel carries the runtime tree inside
amiga64/_tree/, the console script, the licences; nothing generic at the top
level.  The static checks always run (setup.py with a stand-in setup());
the wheel build runs where setuptools is installed (else it skips)."""

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
import types
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def setup_kwargs() -> dict:
    got = {}
    fake = types.ModuleType("setuptools")
    fake.setup = lambda **kw: got.update(kw)
    saved = sys.modules.get("setuptools")
    sys.modules["setuptools"] = fake
    try:
        spec = importlib.util.spec_from_file_location("_amiga64_setup", os.path.join(ROOT, "setup.py"))
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    finally:
        if saved is None:
            del sys.modules["setuptools"]
        else:
            sys.modules["setuptools"] = saved
    return got


def test_pyproject_metadata():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        p = tomllib.load(f)["project"]
    assert p["name"] == "amiga64" and p["version"] == "0.17.0"
    assert p["requires-python"] == ">=3.11" and p["license"] == "MIT"
    assert p["scripts"] == {"amiga64": "amiga64.cli:main"}
    dep = [d for d in p["dependencies"] if d.startswith("c64-test-harness")]
    assert dep and "git+https://github.com/JC-000/c64-test-harness@" in dep[0]
    pin = dep[0].rsplit("@", 1)[1]
    assert pin == "v0.13.0"                         # the tag (= 4f51020), supervisor 2026-10-13
    assert not any(d.startswith("numpy") for d in p["dependencies"])     # optional only
    from amiga64 import __version__
    assert __version__ == p["version"]


def test_the_tree_maps_into_the_package():
    kw = setup_kwargs()
    pk, pd, data = kw["packages"], kw["package_dir"], kw["package_data"]
    assert all(n == "amiga64" or n.startswith("amiga64.") for n in pk)   # no top-level host/tests
    assert pd["amiga64._tree"] == "."
    for n in ("amiga64._tree.host", "amiga64._tree.tests", "amiga64._tree.tests.bench",
              "amiga64._tree.tests.paula", "amiga64._tree.tests.aros"):
        assert n in pk, n
    assert not any(n.startswith("amiga64._tree.host.tests") for n in pk)
    pat = data["amiga64._tree"]
    for want in ("third_party/aros/*", "tools/aros/manifest.txt", "prebuilt/*", "LICENSE", "NOTICE"):
        assert want in pat


def test_tree_root_prefers_the_shipped_copy(tmp_path):
    pkg = tmp_path / "amiga64"
    shutil.copytree(os.path.join(ROOT, "amiga64"), pkg)
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); from amiga64 import tree;"
                        "print(tree.root())", str(tmp_path)], capture_output=True, text=True)
    assert r.stdout.strip() == str(tmp_path)                 # a clone: the repo root
    (pkg / "_tree" / "host").mkdir(parents=True)
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); from amiga64 import tree;"
                        "print(tree.root())", str(tmp_path)], capture_output=True, text=True)
    assert r.stdout.strip() == str(pkg / "_tree")


def _copy_tree(dst: str) -> str:
    keep = ("amiga64", "host", "tests", "tools", "third_party", "prebuilt")
    for n in keep:
        if os.path.exists(os.path.join(ROOT, n)):
            shutil.copytree(os.path.join(ROOT, n), os.path.join(dst, n),
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for n in ("setup.py", "pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        shutil.copy(os.path.join(ROOT, n), dst)
    return dst


@pytest.mark.skipif(importlib.util.find_spec("setuptools") is None, reason="no setuptools here")
def test_the_wheel(tmp_path):
    src = _copy_tree(str(tmp_path / "src"))
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "-w", str(tmp_path / "w"), src], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    (whl,) = os.listdir(tmp_path / "w")
    z = zipfile.ZipFile(tmp_path / "w" / whl)
    names = set(z.namelist())
    for want in ("amiga64/cli.py", "amiga64/_tree/host/machine.py", "amiga64/_tree/tests/aros_wb.py",
                 "amiga64/_tree/tests/bench/u64_session.py", "amiga64/_tree/tests/paula/hw16.py",
                 "amiga64/_tree/tests/aros/model_frames.py", "amiga64/_tree/tools/aros/manifest.txt",
                 "amiga64/_tree/third_party/aros/aros-rom.bin",
                 "amiga64/_tree/third_party/aros/SOURCE.txt", "amiga64/_tree/NOTICE"):
        assert want in names, want
    if os.path.isdir(os.path.join(ROOT, "prebuilt")):
        for f in os.listdir(os.path.join(ROOT, "prebuilt")):
            assert f"amiga64/_tree/prebuilt/{f}" in names, f
    assert not any(n.startswith(("host/", "tests/", "amiga64/_tree/host/tests/")) for n in names)
    ep = next(n for n in names if n.endswith("entry_points.txt"))
    assert "amiga64 = amiga64.cli:main" in z.read(ep).decode()
    lic = [n for n in names if "/licenses/" in n]
    assert any(n.endswith("/LICENSE") for n in lic) and any(n.endswith("/NOTICE") for n in lic)
    # the installed layout finds the bundled ROM through the shipped tree
    site = tmp_path / "site"
    z.extractall(site)
    r = subprocess.run([sys.executable, "-I", "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); from amiga64 import tree;"
                        "tree.activate(); from host import machine; rom, why = machine.find_rom();"
                        "print(rom.cache if rom else why)", str(site)], capture_output=True, text=True)
    assert r.stdout.strip() == str(site / "amiga64" / "_tree" / "third_party" / "aros"), r.stderr


IMPORTS = ("import amiga64.cli, amiga64.session, amiga64.device, amiga64.adf, amiga64.kit;"
           "from amiga64 import tree; r = tree.activate();"
           "import tests.aros_wb, tests.aros_modwb, tests.aros_resume, tests.bench.u64_session,"
           " tests.run_vectors, tests.paula.hw16, host.machine, host.adf_files;"
           "assert tests.aros_wb.__file__.startswith(r), tests.aros_wb.__file__;"
           "from host import machine; rom, why = machine.find_rom(); assert rom, why;"
           "print('IMPORTS-OK', r)")


@pytest.mark.skipif(importlib.util.find_spec("setuptools") is None, reason="no setuptools here")
def test_clean_venv_install_imports_from_outside_the_repo(tmp_path):
    """The supervisor's check: `pip install .` into a clean venv, run from
    outside the repository, imports cleanly.  Offline by default (the
    harness is copied in from this interpreter's install); with
    AMIGA64_NET_TESTS=1 pip resolves the pinned harness tag from GitHub."""
    import c64_test_harness
    src = _copy_tree(str(tmp_path / "src"))
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    py = str(venv / "bin" / "python")
    w = tmp_path / "w"
    r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                        "-w", str(w), src], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    whl = str(w / os.listdir(w)[0])
    net = os.environ.get("AMIGA64_NET_TESTS") == "1"
    r = subprocess.run([py, "-m", "pip", "install", "-q", *([] if net else ["--no-deps"]), whl],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    if not net:
        site = subprocess.run([py, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, check=True).stdout.strip()
        shutil.copytree(os.path.dirname(c64_test_harness.__file__),
                        os.path.join(site, "c64_test_harness"),
                        ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "elsewhere"
    out.mkdir()
    r = subprocess.run([py, "-I", "-c", IMPORTS], capture_output=True, text=True, cwd=out)
    assert "IMPORTS-OK" in r.stdout, r.stdout + r.stderr
    assert "/site-packages/amiga64/_tree" in r.stdout
    r = subprocess.run([str(venv / "bin" / "amiga64"), "--version"], capture_output=True,
                       text=True, cwd=out)
    assert r.stdout.strip() == "amiga64 0.17.0", r.stderr


USER_ADF = os.path.expanduser("~/Documents/Amiga Disk Images/amiga-os-310-workbench.adf")


def _installed_layout(site: str) -> str:
    """The wheel's layout without building one: site/amiga64/ with the tree
    in site/amiga64/_tree/ (what setup.py ships)."""
    pkg = os.path.join(site, "amiga64")
    shutil.copytree(os.path.join(ROOT, "amiga64"), pkg,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "_tree"))
    t = os.path.join(pkg, "_tree")
    for n in ("host", "tests", "tools/aros", "third_party/aros", "prebuilt"):
        shutil.copytree(os.path.join(ROOT, n), os.path.join(t, n),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests" if n == "host" else "\0"))
    return pkg


def _snapshot(top: str) -> dict:
    out = {}
    for d, dirs, files in os.walk(top):
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, top)] = hashlib.sha256(fh.read()).hexdigest()
        for x in dirs:
            out[os.path.relpath(os.path.join(d, x), top) + "/"] = "dir"
    return out


@pytest.mark.skipif(os.environ.get("AMIGA64_VICE_TESTS") != "1",
                    reason="VICE leg (several minutes): AMIGA64_VICE_TESTS=1")
@pytest.mark.skipif(not os.path.isdir(os.path.join(ROOT, "prebuilt")), reason="no prebuilt/")
@pytest.mark.skipif(not os.path.exists(USER_ADF), reason="the user's ADF is not on this machine")
def test_a_read_only_installed_tree_is_unchanged_by_a_run(tmp_path):
    """Supervisor ruling (3): `amiga64 run --backend vice --minutes 0` from an
    installed, read-only tree writes nothing into it (out dirs are the
    user's state dir; session_log is never called)."""
    site = str(tmp_path / "site")
    pkg = _installed_layout(site)
    before = _snapshot(pkg)
    for d, dirs, files in os.walk(pkg):
        for x in dirs + files:
            os.chmod(os.path.join(d, x), 0o555 if x in dirs else 0o444)
    os.chmod(pkg, 0o555)
    env = dict(os.environ, AMIGA64_CONFIG=str(tmp_path / "cfg" / "config.toml"),
               AMIGA64_STATE_DIR=str(tmp_path / "state"), PYTHONDONTWRITEBYTECODE="1")
    work = tmp_path / "work"
    work.mkdir()
    try:
        r = subprocess.run([sys.executable, "-I", "-B", "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); from amiga64.cli import main;"
                            "sys.exit(main(sys.argv[2:]))", site, "run", "--backend", "vice",
                            "--minutes", "0", "--adf", USER_ADF],
                           capture_output=True, text=True, cwd=work, env=env, timeout=1800)
        assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
        assert "the Workbench is up" in r.stdout
        assert _snapshot(pkg) == before
        assert os.listdir(tmp_path / "state" / "holds")          # the reports went there
    finally:
        for d, dirs, files in os.walk(pkg):
            os.chmod(d, 0o755)
