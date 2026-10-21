"""W6-O: the AROS reference oracle (tests/aros_oracle.py, docs/ORACLE.md).

Host-only legs: no FS-UAE, no VICE, no ROM.  The oracle's verdict logic must
have teeth (a wrong expectation, exit code, file or halt is a DIFF), its
screen model must be hle_putc's, and it must skip cleanly (exit 0, a SKIP
line) when FS-UAE or the ROM cache is missing or does not verify.  The
FS-UAE legs are in test_aros_oracle_fsuae.py (opt-in).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time

import pytest

from tests import aros_oracle as ao
from tests import run_vbcc

HELLO_OUT = b"Hello from vbcc!\n2 + 40 = 42\n"


def aros(stdout: bytes, rc: int | None = 0, files=None, timeout=False):
    """An AROS-side outcome as run_aros() builds it from a finished run."""
    return ao.aros_outcome(stdout, rc, files or {}, timed_out=timeout,
                           wall=1.0)


def prog(name: str) -> run_vbcc.Program:
    return run_vbcc.programs()[name]


# --- teeth: the verdict must see every kind of difference ---------------------

def test_hello_matches_its_own_expectation():
    v, diffs = ao.verdict(ao.table_outcome(prog("hello")), aros(HELLO_OUT))
    assert (v, diffs) == ("MATCH", [])


def test_teeth_wrong_expected_line_is_diff():
    wrong = dataclasses.replace(prog("hello"),
                                lines=["Hello from vbcc!", "2 + 40 = 43"])
    v, diffs = ao.verdict(ao.table_outcome(wrong), aros(HELLO_OUT))
    assert v == "DIFF"
    assert any("row 1" in d for d in diffs), diffs


def test_teeth_missing_line_is_diff():
    v, _ = ao.verdict(ao.table_outcome(prog("hello")),
                      aros(b"Hello from vbcc!\n"))
    assert v == "DIFF"


def test_teeth_extra_output_is_diff():
    v, _ = ao.verdict(ao.table_outcome(prog("hello")),
                      aros(HELLO_OUT + b"extra\n"))
    assert v == "DIFF"


def test_teeth_exit_code_is_diff():
    v, diffs = ao.verdict(ao.table_outcome(prog("exit")),
                          aros(b"exit(7) via stdio\n", rc=0))
    assert v == "DIFF" and any("exit code" in d for d in diffs), diffs
    v, _ = ao.verdict(ao.table_outcome(prog("exit")),
                      aros(b"exit(7) via stdio\n", rc=7))
    assert v == "MATCH"


def test_teeth_negative_rc_is_32_bit():
    p = dataclasses.replace(prog("exit"), exit_code=0xFFFF_FFFF)
    assert ao.verdict(ao.table_outcome(p),
                      aros(b"exit(7) via stdio\n", rc=-1))[0] == "MATCH"


def test_teeth_file_contents_are_diff():
    p = prog("fio_exit")
    good = {"unclosed.txt": b"left open 42\n"}
    assert ao.verdict(ao.table_outcome(p),
                      aros(b"exit without fclose\n", files=good))[0] == "MATCH"
    bad = {"unclosed.txt": b"left open 43\n"}
    v, diffs = ao.verdict(ao.table_outcome(p),
                          aros(b"exit without fclose\n", files=bad))
    assert v == "DIFF" and any("unclosed.txt" in d for d in diffs), diffs
    v, _ = ao.verdict(ao.table_outcome(p),
                      aros(b"exit without fclose\n", files={}))
    assert v == "DIFF"                       # missing file


def test_teeth_file_that_must_not_exist():
    p = prog("fio_remove")
    files = {"gone.txt": b"bye", "keep.txt": b"keep"}   # gone.txt survived
    v, diffs = ao.verdict(ao.table_outcome(p), aros(b"", files=files))
    assert v == "DIFF" and any("gone.txt" in d for d in diffs), diffs


def test_teeth_amiga64_halt_is_diff():
    # readitem halts in an unimplemented-LVO slot on Amiga64 (empty screen);
    # an AROS run that completes silently must still be a DIFF
    v, diffs = ao.verdict(ao.table_outcome(prog("readitem")), aros(b"", rc=0))
    assert v == "DIFF" and any("HALT_ALINE" in d for d in diffs), diffs


def test_teeth_aros_timeout_is_diff():
    v, diffs = ao.verdict(ao.table_outcome(prog("hello")),
                          aros(HELLO_OUT, rc=None, timeout=True))
    assert v == "DIFF" and any("timed out" in d for d in diffs), diffs


def test_teeth_case_counts_against_the_table():
    # run_vbcc's lines keep case, so --table compares it too
    v, diffs = ao.verdict(ao.table_outcome(prog("hello")),
                          aros(HELLO_OUT.replace(b"vbcc", b"VBCC")))
    assert v == "DIFF" and diffs == [
        "row 0 (case): Amiga64 |Hello from vbcc!| AROS |Hello from VBCC!|"]


def test_case_is_folded_against_a_c64_screen():
    # a VICE run yields screen codes only: no case, that is the resolution
    screen_only = dataclasses.replace(ao.table_outcome(prog("hello")),
                                      text=None)
    assert ao.verdict(screen_only, aros(HELLO_OUT.upper()))[0] == "MATCH"


# --- the screen model is hle_putc's (runtime/ops/hle.s) -------------------------

def rows(screen: bytes) -> list[str]:
    return [run_vbcc.screen_to_text(screen[r * 40:(r + 1) * 40])
            for r in range(25)]


def test_render_plain_lines():
    assert ao.render_screen(HELLO_OUT) == run_vbcc.expected_screen(
        ["Hello from vbcc!", "2 + 40 = 42"])


def test_render_wraps_at_40_and_a_full_row_then_newline_leaves_a_blank():
    r = rows(ao.render_screen(b"A" * 45 + b"\n" + b"B" * 40 + b"\nC"))
    assert r[:5] == ["A" * 40, "AAAAA", "B" * 40, "", "C"]


def test_render_scrolls_lazily_like_hle_putc():
    out = b"".join(f"L{n}\n".encode() for n in range(30))
    r = rows(ao.render_screen(out))
    # 30 lines + the trailing newline: the pending cursor at 1000 does not
    # scroll until something is printed, so rows show L5..L29
    assert r[0] == "L5" and r[24] == "L29"
    r = rows(ao.render_screen(out + b"x"))
    assert r[0] == "L6" and r[23] == "L29" and r[24] == "X"


def test_render_scroll_matches_run_vbcc_scroll_expectation():
    out = b"".join((run_vbcc._scroll_line(n) + "\n").encode()
                   for n in range(1, 31))
    assert ao.render_screen(out) == run_vbcc.expected_screen(
        prog("scroll").lines)


def test_render_control_and_high_bytes():
    data = b"a\tb\rc\x08d\x00\x1be\x80\xff"
    assert rows(ao.render_screen(data))[0] == "ABCDE??"
    assert ao.render_text(data)[0] == "abcde??"


def test_render_text_follows_the_same_cursor():
    data = b"".join(f"Line{n}\n".encode() for n in range(30)) + b"x" * 45
    t = ao.render_text(data)
    assert t[0] == "Line7" and t[22] == "Line29" and t[23] == "x" * 40 \
        and t[24] == "xxxxx"
    assert [s.upper() for s in t] == rows(ao.render_screen(data))


# --- program coverage: every registered program gets a verdict path ------------

def test_skip_and_triage_tables_name_registered_programs_only():
    names = set(run_vbcc.programs())
    assert set(ao.SKIP) <= names
    assert set(ao.TRIAGE) <= names
    assert not set(ao.SKIP) & set(ao.TRIAGE)
    for why in ao.SKIP.values():
        assert why.split(":")[0] in ("interactive", "timing-dependent",
                                     "ULTDOS"), why


def test_skip_set_is_pinned():
    # a new SKIP must be an explicit change of this test (review R14): a
    # SKIP entry could otherwise silently swallow a real program
    assert set(ao.SKIP) == {"tspin"}


def test_triage_categories():
    assert ao.CATEGORIES == ("artifact", "HLE limitation", "AROS divergence",
                             "our bug")
    for t in ao.TRIAGE.values():
        assert t.note and t.lines
        for cat, line in t.lines:
            assert cat in ao.CATEGORIES and line, (cat, line)


# --- skips cleanly -----------------------------------------------------------------

def test_skip_without_fs_uae(tmp_path, capsys):
    rc = ao.main(["--table", "--fs-uae", str(tmp_path / "no-fs-uae"),
                  "--bundle", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0 and "SKIP" in out and "FS-UAE" in out


def test_skip_without_bundle(tmp_path, capsys):
    fake = tmp_path / "fs-uae"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    rc = ao.main(["--table", "--fs-uae", str(fake),
                  "--bundle", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert rc == 0 and "SKIP" in out and "no AROS bundle" in out


def _fake_bundle(root, corrupt: str | None = None):
    """A bundle laid out per a manifest we write, with files whose hashes
    match (or, for `corrupt`, do not)."""
    files = {"aros-rom.bin": b"R" * 64, "aros-ext.bin": b"E" * 64,
             "LICENSE": b"APL", "C/Copy": b"copy"}
    lines = []
    d = root / "bundle"
    for path, data in files.items():
        lines.append(f"file|{path}|x!{path}|{hashlib.sha256(data).hexdigest()}")
        (d / path).parent.mkdir(parents=True, exist_ok=True)
        (d / path).write_bytes(data + (b"!" if path == corrupt else b""))
    man = root / "manifest.txt"
    man.write_text("\n".join(lines) + "\n")
    return man, d


def test_bundle_verifies(tmp_path):
    man, d = _fake_bundle(tmp_path)
    got, why = ao.find_bundle(str(man), str(d))
    assert got == str(d), why


def test_bundle_hash_mismatch_fails_closed(tmp_path, capsys):
    man, d = _fake_bundle(tmp_path, corrupt="aros-rom.bin")
    got, why = ao.find_bundle(str(man), str(d))
    assert got is None and "aros-rom.bin" in why and "mismatch" in why
    fake = tmp_path / "fs-uae"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    rc = ao.main(["--table", "--fs-uae", str(fake), "--bundle", str(d),
                  "--manifest", str(man)])
    out = capsys.readouterr().out
    assert rc == 0 and "SKIP" in out and "mismatch" in out


def test_real_manifest_parses():
    m = ao.read_manifest(ao.MANIFEST)
    paths = {f.path for f in m.files}
    assert {"aros-rom.bin", "aros-ext.bin", "LICENSE", "C/Copy", "C/Assign"} <= paths
    assert all(len(f.sha256) == 64 for f in m.files)


# --- never part of the gate ----------------------------------------------------

def test_not_in_rec_gate():
    with open(os.path.join(ao.PROJECT_ROOT, "tests", "rec_gate.sh")) as f:
        assert "aros" not in f.read().lower()


def test_fsuae_leg_is_opt_in():
    # the FS-UAE legs must not run inside `make host-test` (rec_gate.sh)
    # unless explicitly requested
    import host.tests.test_aros_oracle_fsuae as leg
    assert leg.OPT_IN == "AMIGA64_AROS_ORACLE"


@pytest.mark.parametrize("name", sorted(run_vbcc.programs()))
def test_every_program_has_a_table_outcome(name):
    o = ao.table_outcome(prog(name))
    assert len(o.screen) == 1000


# --- the driver: exit status, triage annotation, timeout retry -----------------

@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """main() with FS-UAE, the bundle and vbcc faked and run_aros scripted.
    Yields (script: AROS outcomes run_aros returns in order, calls: what
    was compiled and run, argv prefix)."""
    fake = tmp_path / "fs-uae"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ao, "find_bundle", lambda *a: (str(tmp_path), ""))
    monkeypatch.setattr(run_vbcc, "vbcc_home", lambda: str(tmp_path))
    script: list = []
    calls: list = []

    def compile_c(name, *a, **k):
        calls.append(f"compile {name}")
        return b"hunk"
    monkeypatch.setattr(run_vbcc, "compile_c", compile_c)

    def run_aros(p, blob, *a, **k):
        calls.append(p.name)
        return script.pop(0)
    monkeypatch.setattr(ao, "run_aros", run_aros)
    base = ["--table", "--fs-uae", str(fake), "--out", str(tmp_path / "o")]
    yield script, calls, base
    # (every scripted outcome consumed: no silent extra/missing runs)
    assert script == []


def _runs(calls):
    return [c for c in calls if not c.startswith("compile ")]


def test_main_match_exits_0(fake_env, capsys):
    script, _, base = fake_env
    script.append(aros(HELLO_OUT))
    assert ao.main(base + ["--only", "hello"]) == 0
    assert "MATCH hello" in capsys.readouterr().out


def test_main_untriaged_diff_exits_1(fake_env, capsys):
    script, _, base = fake_env
    script.append(aros(b"Hello from vbcc!\n"))
    assert ao.main(base + ["--only", "hello"]) == 1
    out = capsys.readouterr().out
    assert "DIFF  hello" in out and "[UNTRIAGED: 1 new" in out
    assert "[NEW] row 1:" in out


def test_main_triaged_diff_is_reported_and_exits_0(fake_env, capsys):
    script, _, base = fake_env
    script.append(aros(b"opened 10\n"))
    assert ao.main(base + ["--only", "leak_open"]) == 0
    out = capsys.readouterr().out
    assert "DIFF  leak_open" in out and "[HLE limitation:" in out
    script.append(aros(b"opened 10\n"))
    assert ao.main(base + ["--only", "leak_open", "--strict"]) == 1


def test_main_skip_table_is_honoured(fake_env, capsys):
    _, calls, base = fake_env
    assert ao.main(base + ["--only", "tspin"]) == 0
    # a SKIP program is neither compiled nor run
    assert calls == [] and "SKIP  tspin: timing-dependent" in \
        capsys.readouterr().out


def test_main_retries_a_stall_once_and_says_so(fake_env, capsys, tmp_path):
    # a stall: the program exited (RC recorded), the boot script timed out
    script, calls, base = fake_env
    script += [aros(HELLO_OUT, rc=0, timeout=True), aros(HELLO_OUT)]
    js = tmp_path / "r.json"
    assert ao.main(base + ["--only", "hello", "--json", str(js)]) == 0
    out = capsys.readouterr().out
    assert _runs(calls) == ["hello", "hello"]
    assert "RETRY-MATCH hello" in out and "retried after 1 stall" in out
    assert "0 MATCH, 1 RETRY-MATCH, 0 DIFF (0 untriaged), 0 SKIP; " \
        "1 program(s) retried" in out
    rep = json.loads(js.read_text())
    assert rep["retried"] == 1 and rep["counts"]["RETRY-MATCH"] == 1
    prog = rep["programs"][0]
    assert prog["verdict"] == "RETRY-MATCH" and prog["retries"] == 1
    assert "stalled" in prog["retried_stalls"][0]


def test_retry_match_fails_strict(fake_env, capsys):
    script, _, base = fake_env
    script += [aros(HELLO_OUT, rc=0, timeout=True), aros(HELLO_OUT)]
    assert ao.main(base + ["--only", "hello", "--strict"]) == 1


def test_a_guest_hang_is_not_retried(fake_env, capsys):
    # timed out with no $RC: the program never exited -> DIFF, no retry
    script, calls, base = fake_env
    script += [aros(b"", rc=None, timeout=True)]
    assert ao.main(base + ["--only", "hello"]) == 1
    out = capsys.readouterr().out
    assert _runs(calls) == ["hello"]
    assert "DIFF  hello" in out and "timed out" in out and "retr" not in \
        out.split("DIFF  hello")[1].split("\n")[0]


def test_a_repeated_stall_is_a_diff(fake_env, capsys):
    script, calls, base = fake_env
    script += [aros(HELLO_OUT, rc=0, timeout=True)] * 2
    assert ao.main(base + ["--only", "hello"]) == 1
    assert _runs(calls) == ["hello", "hello"]
    assert "timed out" in capsys.readouterr().out
    script += [aros(HELLO_OUT, rc=0, timeout=True)]
    assert ao.main(base + ["--only", "hello", "--retries", "0"]) == 1
    assert _runs(calls) == ["hello"] * 3


def test_skip_without_vbcc(fake_env, monkeypatch, capsys):
    _, calls, base = fake_env

    def no_vbcc():
        raise SystemExit("error: vbcc not found at /nowhere")
    monkeypatch.setattr(run_vbcc, "vbcc_home", no_vbcc)
    assert ao.main(base) == 0
    out = capsys.readouterr().out
    assert "SKIP aros_oracle: error: vbcc not found" in out and calls == []


# --- F1: triage pins the exact diff lines ----------------------------------------

# What AROS printed / returned / left in RAM: for each triaged program in the
# baseline run (build/evidence/w6-oracle/runs/table3.json).  Replaying them
# must give exactly the pinned diff lines: no NEW line, none missing.
FR_FILES = {"again": b"reused", "f04": b"", "f31": b"", "more": b"",
            **{f"f{n:02d}": b"" for n in range(41) if n != 5}}
AROS_BASELINE = {
    "sysbase": (b"ThisTask=2335f0\nAttnFlags=0\nMaxLocMem=100000\nexec ver=51\n"
                b"dos ver=50\ndos v40 ok\ndos v41 ok\ndos v50 ok\ndos.fake NULL\n"
                b"utility ok\nLibList BAD\n", 0, {}),
    "time": (b"time=1790319825\nsame=1\n", 0, {}),
    "system": (b"hi\nsystem(NULL)=1\nsystem(cmd)=0\n", 0, {}),
    "readitem": (b"ReadItem=0\n", 0, {}),
    "putstr": (b"before\nafter\nPutStr works\n", 3, {}),
    "leak_open": (b"opened 10\n", 0, {}),
    "fio_big": (b"straddle=0 odd=1\n", 10, {"big.bin": b"", "pad": b""}),
    "fio_reuse": (b"created=41\nrm=0\nagain=open\nmore=open\n", 11, FR_FILES),
    "heap_stress1": (b"FAIL 100 top\na=c01118 b=0\nop=0 seed=6\n", 100, {}),
    "heap_stress2": (b"FAIL 100 top\na=c01118 b=0\nop=0 seed=7\n", 100, {}),
}


def test_every_triaged_program_has_a_baseline():
    assert set(AROS_BASELINE) == set(ao.TRIAGE)


@pytest.mark.parametrize("name", sorted(AROS_BASELINE))
def test_baseline_diff_is_exactly_the_pinned_triage(name):
    out, rc, files = AROS_BASELINE[name]
    v, diffs = ao.verdict(ao.table_outcome(prog(name)), aros(out, rc, files))
    known, new, missing = ao.triage_check(name, diffs)
    assert v == "DIFF" and new == [] and missing == [], (new, missing)
    assert len(known) == len(ao.TRIAGE[name].lines)


def _untriaged(name, out, rc, files=None):
    v, diffs = ao.verdict(ao.table_outcome(prog(name)), aros(out, rc, files))
    _known, new, missing = ao.triage_check(name, diffs)
    return v == "DIFF" and bool(new or missing)


def test_new_line_in_a_triaged_program_is_untriaged():
    # the reviewer's triage_masking_demo cases
    assert _untriaged("fio_reuse", b"created=41\nrm=-1\nagain=NULL\n", 10)
    assert _untriaged("leak_open", b"opened 10\nGURU 8000000B\n", 5)
    base = AROS_BASELINE["sysbase"][0]
    for old, new in ((b"AttnFlags=0", b"AttnFlags=4"),
                     (b"dos v40 ok", b"dos v40 NULL"),
                     (b"dos.fake NULL", b"dos.fake ok")):
        assert _untriaged("sysbase", base.replace(old, new), 0), old


def test_a_vanished_pinned_line_is_untriaged():
    out, rc, files = AROS_BASELINE["fio_reuse"]
    # created=32 on AROS would drop the 'row 0' pinned line
    assert _untriaged("fio_reuse", out.replace(b"=41", b"=32"), rc, files)


def test_volatile_patterns_are_narrow():
    base = AROS_BASELINE["sysbase"][0]
    for task, ok in ((b"2335f0", True), (b"1f00", True), (b"a40x", False),
                     (b"123456789", False), (b"", False)):
        bad = base.replace(b"2335f0", task)
        assert _untriaged("sysbase", bad, 0) != ok, task
    for t, ok in ((b"1790319825", True), (b"999999999", True),
                  (b"12345678", False), (b"17903198251", False),
                  (b"-1", False)):
        assert _untriaged("time", b"time=%s\nsame=1\n" % t, 0) != ok, t
    assert _untriaged("time", b"time=1790319825\nsame=0\n", 0)


def test_main_triaged_program_with_a_new_line_exits_1(fake_env, capsys):
    script, _, base = fake_env
    script.append(aros(b"created=41\nrm=-1\nagain=NULL\n", 10))
    assert ao.main(base + ["--only", "fio_reuse"]) == 1
    out = capsys.readouterr().out
    assert "[UNTRIAGED:" in out and "[NEW] row 1:" in out
    assert "[MISSING pinned]" in out


def test_main_stale_triage_exits_1(fake_env, capsys):
    # a triaged program that now MATCHes: its pinned triage is stale
    script, _, base = fake_env
    script.append(aros(b"opened 8\n"))
    assert ao.main(base + ["--only", "leak_open"]) == 1
    assert "[TRIAGE STALE: 1 pinned line(s) absent]" in capsys.readouterr().out


# --- F2: teeth the reviewer's surviving mutants showed missing ------------------

def test_teeth_difference_on_the_last_row_only():            # R1
    lines = [f"l{n}" for n in range(25)]
    p = dataclasses.replace(prog("hello"), lines=lines)
    out = "".join(f"{x}\n" for x in lines[:24]).encode() + b"L24X"
    v, diffs = ao.verdict(ao.table_outcome(p), aros(out))
    assert v == "DIFF" and diffs == ["row 24: Amiga64 |L24| AROS |L24X|"]


def test_teeth_exit_code_when_amiga64_returns_0():          # R7
    v, diffs = ao.verdict(ao.table_outcome(prog("hello")),
                          aros(HELLO_OUT, rc=10))
    assert (v, diffs) == ("DIFF", ["exit code: Amiga64 0, AROS 10"])


def test_teeth_exit_code_compared_even_when_a_side_did_not_exit():  # R7
    v, diffs = ao.verdict(ao.table_outcome(prog("readitem")),
                          aros(b"", rc=0))
    assert "exit code: Amiga64 None, AROS 0" in diffs


def test_teeth_longer_aros_file_is_diff():                   # R8
    p = prog("fio_exit")
    v, diffs = ao.verdict(ao.table_outcome(p), aros(
        b"exit without fclose\n",
        files={"unclosed.txt": b"left open 42\nEXTRA"}))
    assert v == "DIFF" and any("unclosed.txt" in d for d in diffs)


PINNED = ["aros-rom.bin", "aros-ext.bin", "LICENSE", "C/Copy"]


@pytest.mark.parametrize("path", PINNED)
def test_bundle_hash_mismatch_in_any_pinned_file(tmp_path, path):   # R16
    man, d = _fake_bundle(tmp_path, corrupt=path)
    got, why = ao.find_bundle(str(man), str(d))
    assert got is None and path in why and "mismatch" in why


@pytest.mark.parametrize("path", PINNED)
def test_bundle_missing_pinned_file_fails_closed(tmp_path, path):   # R17
    man, d = _fake_bundle(tmp_path)
    (d / path).unlink()
    got, why = ao.find_bundle(str(man), str(d))
    assert got is None and path in why and "missing" in why


# --- run_aros against a fake FS-UAE ------------------------------------------------
# The fake reads the run's config and boot script and plays the script:
# FailAt, the program (its "binary" is a JSON behaviour spec), Echo "$RC",
# the RAM: copies and done.txt.  It then idles like FS-UAE until killed.

FAKE_FSUAE = r'''#!PYTHON
import json, os, re, shutil, sys, time
cfg = open(sys.argv[1]).read()
hd = re.search(r"^hard_drive_0 = (.*)$", cfg, re.M).group(1)
ram = os.path.join(os.path.dirname(hd), "fakeram")
os.makedirs(ram, exist_ok=True)
with open(os.path.join(hd, "o", "clist.txt"), "w") as f:
    f.write("\n".join(sorted(os.listdir(os.path.join(hd, "C")))))
def path(x):
    return os.path.join(hd, x[4:]) if x.startswith("DH0:") else x
failat, rc, spec = 10, None, {}
for line in open(os.path.join(hd, "S", "Startup-Sequence")):
    w = line.split()
    if not w:
        continue
    if w[0] == "FailAt":
        failat = int(w[1])
    elif w[0].startswith("DH0:bin/"):
        spec = json.load(open(path(w[0])))
        if spec.get("hang"):
            break
        open(path(w[2][1:]), "w").write(spec.get("stdout", ""))
        for n, d in spec.get("ram", {}).items():
            open(os.path.join(ram, n), "w").write(d)
        rc = spec["rc"]
        if rc >= failat:
            break                      # the script aborts, as AmigaDOS does
    elif w[0] == "Echo" and w[1] == '"$RC"':
        if not spec.get("no_rc"):
            open(path(w[2][1:]), "w").write(f"{rc}\n")
        if spec.get("stall"):
            break
    elif w[0] == "Copy" and w[1].startswith("DH0:w/"):
        for n in os.listdir(os.path.join(hd, "w")):
            shutil.copy(os.path.join(hd, "w", n), ram)
    elif w[0] == "Copy" and w[1].startswith("RAM:"):
        for n in os.listdir(ram):
            shutil.copy(os.path.join(ram, n), os.path.join(hd, "ram"))
    elif w[0] == "Echo" and w[1] == '"done"':
        open(path(w[2][1:]), "w").write("done\n")
while True:
    time.sleep(1)
'''


@pytest.fixture
def fsuae(tmp_path):
    f = tmp_path / "fake-fs-uae"
    f.write_text(FAKE_FSUAE.replace("#!PYTHON", "#!" + sys.executable))
    f.chmod(0o755)
    return str(f)


@pytest.fixture
def cache(tmp_path):
    """A fake bundle + manifest with the two pinned commands, plus an
    UNPINNED file dropped into C/."""
    files = {"aros-rom.bin": b"R", "aros-ext.bin": b"E", "LICENSE": b"L",
             "C/Copy": b"copy", "C/Assign": b"assign"}
    d = tmp_path / "cache"
    lines = []
    for pth, data in files.items():
        (d / pth).parent.mkdir(parents=True, exist_ok=True)
        (d / pth).write_bytes(data)
        lines.append(f"file|{pth}|x!{pth}|{hashlib.sha256(data).hexdigest()}")
    (d / "C" / "Evil").write_bytes(b"not pinned")
    (tmp_path / "manifest.txt").write_text("\n".join(lines) + "\n")
    return str(d), str(tmp_path / "manifest.txt")


def run_fake(fsuae, cache, spec, name="hello", timeout=10.0, keep=None):
    """run_aros in a daemon thread: a run_aros that never returns (no
    timeout) fails the test after 30 s instead of hanging the suite."""
    import threading
    d, man = cache
    box = []
    t = threading.Thread(daemon=True, target=lambda: box.append(ao.run_aros(
        prog(name), json.dumps(spec).encode(), d, fsuae, timeout, keep=keep,
        manifest=man)))
    t.start()
    t.join(30)
    if not box:
        subprocess.run(["pkill", "-f", fsuae])
        pytest.fail("run_aros did not return within 30 s")
    return box[0]


def test_fake_run_matches(fsuae, cache):
    o = run_fake(fsuae, cache, {"stdout": HELLO_OUT.decode(), "rc": 0})
    assert ao.verdict(ao.table_outcome(prog("hello")), o) == ("MATCH", [])


def test_run_aros_copies_only_pinned_commands(fsuae, cache, tmp_path):  # F6
    run_fake(fsuae, cache, {"stdout": "", "rc": 0}, keep=str(tmp_path / "k"))
    clist = (tmp_path / "k" / "hello" / "hd" / "o" / "clist.txt").read_text()
    assert clist.split() == ["Assign", "Copy"]


def test_run_aros_times_out_and_kills_the_emulator(fsuae, cache):  # R15
    t0 = time.monotonic()
    o = run_fake(fsuae, cache, {"hang": True}, timeout=1.5)
    assert time.monotonic() - t0 < 15
    assert o.halt.startswith("AROS run timed out") and not o.stalled
    assert "the program did not exit" in o.halt
    left = subprocess.run(["pgrep", "-f", fsuae], capture_output=True)
    assert left.stdout == b""


def test_run_aros_stall_after_exit_is_marked(fsuae, cache):         # F5
    o = run_fake(fsuae, cache, {"stdout": "x\n", "rc": 0, "stall": True},
                 timeout=1.5)
    assert o.stalled and "RC=0; the boot script stalled" in o.halt


def test_run_aros_missing_rc_is_not_rc_0(fsuae, cache):             # R20
    o = run_fake(fsuae, cache, {"stdout": HELLO_OUT.decode(), "rc": 0,
                                "no_rc": True})
    assert o.exit_code is None and "no exit code" in o.halt
    assert ao.verdict(ao.table_outcome(prog("hello")), o)[0] == "DIFF"


@pytest.mark.parametrize("rc", [0, 7, 999, 1000, 70000, 2147483646, -1])
def test_big_and_negative_exit_codes_are_recorded(fsuae, cache, rc):  # F4
    o = run_fake(fsuae, cache, {"stdout": "", "rc": rc})
    assert o.halt == "HALT_OK" and o.exit_code == rc & 0xFFFF_FFFF


def test_failat_is_the_largest_long():                                # F4
    with open(os.path.join(ao.ORACLE_DIR, "Startup-Sequence.in")) as f:
        assert f.readline().split() == ["FailAt", "2147483647"]


def test_staged_files_round_trip_through_ram(fsuae, cache):
    p = prog("fio_seek")
    d, man = cache
    o = ao.run_aros(p, json.dumps({"stdout": "", "rc": 0}).encode(), d,
                    fsuae, 10, manifest=man)
    assert o.files == {"in.txt": run_vbcc.SEEK_DATA}


def test_teeth_screen_codes_not_their_text_rendering():        # review R5
    # '[' is screen code $1B and '.' is $2E; screen_to_text shows both as
    # '.', so only a code-level compare sees the difference (VICE runs have
    # no case text to fall back on)
    p = dataclasses.replace(prog("hello"), lines=["a.b"])
    screen_only = dataclasses.replace(ao.table_outcome(p), text=None,
                                      exit_code=0)
    v, diffs = ao.verdict(screen_only, aros(b"a[b\n"))
    assert v == "DIFF" and diffs == ["row 0: Amiga64 |A.B| AROS |A.B|"]


def test_pinned_lines_match_whole_lines_only():                 # F1e
    out, _rc, files = AROS_BASELINE["fio_big"]
    # pinned "exit code: Amiga64 0, AROS 10" must not absorb "... AROS 100"
    assert _untriaged("fio_big", out, 100, files)
