"""W6-O: the AROS oracle end to end in FS-UAE (opt-in, never in the gate).

Runs only with AMIGA64_AROS_ORACLE=1 AND FS-UAE, the verified AROS bundle
(third_party/aros/) and vbcc present; otherwise every test skips.  The
teeth: a program run on AROS matches its expectation, and the same run
against a wrong expectation, or a mutated binary, is a DIFF.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess

import pytest

from tests import aros_oracle as ao
from tests import run_vbcc

OPT_IN = "AMIGA64_AROS_ORACLE"


def _why_not() -> str | None:
    if os.environ.get(OPT_IN) != "1":
        return f"opt-in: set {OPT_IN}=1"
    if not os.access(ao.FS_UAE, os.X_OK):
        return f"FS-UAE not found at {ao.FS_UAE}"
    cache, why = ao.find_bundle()
    if cache is None:
        return why
    try:
        run_vbcc.vbcc_home()
    except SystemExit as e:
        return str(e)
    return None


pytestmark = pytest.mark.skipif(_why_not() is not None,
                                reason=_why_not() or "")


@pytest.fixture(scope="module")
def cache():
    return ao.find_bundle()[0]


@pytest.fixture(scope="module")
def hello(tmp_path_factory):
    out = tmp_path_factory.mktemp("vbcc")
    return run_vbcc.compile_c("hello", str(out))


def run(p, blob, cache):
    return ao.run_aros(p, blob, cache, ao.FS_UAE, timeout=120)


def test_hello_matches_on_aros(hello, cache):
    p = run_vbcc.programs()["hello"]
    got = run(p, hello, cache)
    assert got.stdout == b"Hello from vbcc!\n2 + 40 = 42\n"
    assert ao.verdict(ao.table_outcome(p), got) == ("MATCH", [])
    # teeth: the same AROS run against a wrong expectation
    wrong = dataclasses.replace(p, lines=["Hello from vbcc!", "2 + 40 = 41"],
                                exit_code=1)
    v, diffs = ao.verdict(ao.table_outcome(wrong), got)
    assert v == "DIFF"
    assert any("row 1" in d for d in diffs) and \
        any("exit code" in d for d in diffs), diffs


def test_mutated_binary_is_diff(hello, cache):
    p = run_vbcc.programs()["hello"]
    i = hello.index(b"Hello from vbcc!")
    bad = hello[:i] + b"Jello" + hello[i + 5:]
    v, diffs = ao.verdict(ao.table_outcome(p), run(p, bad, cache))
    assert v == "DIFF" and any("row 0" in d for d in diffs), diffs


def test_files_round_trip_on_aros(cache, tmp_path):
    # staged files in, written files back out (fio_seek reads in.txt)
    p = run_vbcc.programs()["fio_seek"]
    blob = run_vbcc.compile_c(p.name, str(tmp_path))
    assert ao.verdict(ao.table_outcome(p), run(p, blob, cache))[0] == "MATCH"


def test_kept_run_dir_holds_no_aros_material(hello, cache, tmp_path):
    p = run_vbcc.programs()["hello"]
    ao.run_aros(p, hello, cache, ao.FS_UAE, timeout=120, keep=str(tmp_path))
    kept = tmp_path / "hello"
    assert (kept / "hd" / "o" / "stdout.txt").exists()
    aros_files = {ao._sha256(os.path.join(cache, f.path))
                  for f in ao.read_manifest(ao.MANIFEST).files}
    for root, _dirs, files in os.walk(kept):
        assert os.path.basename(root) != "C"
        for f in files:
            assert ao._sha256(os.path.join(root, f)) not in aros_files, f


def _compile_rc(tmp_path, rc: int) -> bytes:
    home = run_vbcc.vbcc_home()
    src = tmp_path / f"rc{rc & 0xFFFFFFFF}.c"
    src.write_text('#include <stdio.h>\nint main(void)\n'
                   f'{{ printf("rc {rc}\\n"); return {rc}; }}\n')
    exe = tmp_path / src.stem
    env = dict(os.environ, VBCC=home,
               PATH=os.path.join(home, "bin") + os.pathsep + os.environ["PATH"])
    subprocess.run(["vc", "+aos68k", "-o", str(exe), str(src)], env=env,
                   check=True, capture_output=True)
    return exe.read_bytes()


@pytest.mark.parametrize("rc", [1000, 70000, -1])
def test_big_and_negative_exit_codes_on_aros(cache, tmp_path, rc):  # F4
    p = dataclasses.replace(run_vbcc.programs()["hello"], lines=[f"rc {rc}"],
                            exit_code=rc & 0xFFFF_FFFF)
    got = run(p, _compile_rc(tmp_path, rc), cache)
    assert ao.verdict(ao.table_outcome(p), got) == ("MATCH", []), got.halt
