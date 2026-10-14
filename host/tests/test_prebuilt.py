"""W17-B: the prebuilt path (prebuilt/, host/prebuilt.py, tests/mk_prebuilt.py).

Always: prebuilt/MANIFEST verifies (every file's SHA-256, the runner's build
ID and every module ID re-derived), the loader refuses a labels file other
than prebuilt/runner.lbl and a tampered file, and the tests/ builders return
the prebuilt bytes without running a tool when AMIGA64_PREBUILT=1.
With the pinned toolchain: a rebuild from source is byte-identical to
prebuilt/ (the staleness check; `make prebuilt-check` runs the same).
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from host import prebuilt as P

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def forced(monkeypatch):
    """AMIGA64_PREBUILT=1 and no tool may run (subprocess.run raises)."""
    monkeypatch.setenv("AMIGA64_PREBUILT", "1")

    def no_tool(cmd, *a, **k):
        raise AssertionError(f"a tool ran in prebuilt mode: {cmd}")
    monkeypatch.setattr(subprocess, "run", no_tool)
    return P.runner()[1]


def test_manifest_verifies():
    assert P.verify() == []
    m = P.manifest()
    assert set(m["files"]) == {f for files in P.ARTIFACTS.values() for f in files}
    assert set(m["ids"]) == set(P.IDS)
    for tool in P.TOOLS:
        assert m["tools"].get(tool), f"MANIFEST pins no {tool} version"


def test_runner_is_stamped():
    from host import snapfmt
    prg, lbl = P.runner()
    bid = snapfmt.read_bid(prg, lbl)
    assert bid != bytes(8)
    assert bid.hex() == P.manifest()["build_id"]


def _copy(tmp_path) -> str:
    d = str(tmp_path / "prebuilt")
    shutil.copytree(P.DIR, d)
    return d


def test_verify_catches_a_tampered_file(tmp_path, monkeypatch):
    d = _copy(tmp_path)
    monkeypatch.setattr(P, "DIR", d)
    p = os.path.join(d, "td.bin")
    b = bytearray(open(p, "rb").read())
    b[-1] ^= 0xFF
    open(p, "wb").write(b)
    probs = P.verify()
    assert any("td.bin" in x and "SHA-256" in x for x in probs), probs
    with pytest.raises(P.PrebuiltError, match="td.bin"):
        P.module("td", P.runner()[1])


def test_verify_catches_a_wrong_id(tmp_path, monkeypatch):
    """The file and MANIFEST agree, but the stamped ID is not SHA-1[:4] of
    the module: the ID check, not the hash, must catch it."""
    import hashlib
    d = _copy(tmp_path)
    monkeypatch.setattr(P, "DIR", d)
    p = os.path.join(d, "inpm.b.bin")
    b = bytearray(open(p, "rb").read())
    b[-1] ^= 0xFF
    open(p, "wb").write(b)
    mf = os.path.join(d, "MANIFEST")
    text = open(mf).read()
    old = P.manifest()["files"]["inpm.b.bin"]
    open(mf, "w").write(text.replace(old, hashlib.sha256(b).hexdigest()))
    probs = P.verify()
    assert probs and all("inpm" in x and "ID" in x for x in probs), probs


def test_other_labels_refused(tmp_path):
    lbl = tmp_path / "runner.lbl"
    text = open(P.runner()[1]).read()
    lbl.write_text(text.replace("al 00C200 .TD_ORG", "al 00C201 .TD_ORG"))
    assert text != lbl.read_text()
    with pytest.raises(P.PrebuiltError, match="runner.lbl"):
        P.module("td", str(lbl))
    # the 68k files do not depend on the runner
    assert P.module("modwb") == open(os.path.join(P.DIR, "modwb"), "rb").read()


def test_wanted(monkeypatch):
    monkeypatch.setenv("AMIGA64_PREBUILT", "1")
    assert P.wanted("sh")
    monkeypatch.setenv("AMIGA64_PREBUILT", "0")
    assert not P.wanted("no-such-tool-w17b")
    monkeypatch.delenv("AMIGA64_PREBUILT")
    assert P.wanted("no-such-tool-w17b")
    assert P.wanted("/no/such/dir/vasmm68k_mot")
    assert not P.wanted("sh")


def test_builders_load_prebuilt(forced, tmp_path):
    from tests import aros_boot as AB
    from tests import aros_modwb as MW
    from tests import aros_wb as WB
    from tests import run_paula as RP
    from tests import run_spr as SP
    lbl, out = forced, str(tmp_path / "out")
    rd = lambda n: open(os.path.join(P.DIR, n), "rb").read()   # noqa: E731
    b, lab = AB.build_driver(lbl, out)
    assert b == rd("mboot.bin") and lab == P.labels("mboot")
    b, lab = WB.wb_driver(lbl, out)
    assert b == rd("wbboot.bin") and lab == P.labels("wbboot")
    assert WB.build_td(lbl, out) == rd("td.bin")
    assert WB.build_td(lbl, out, ("TD_VARIANT=$5A",)) == rd("td_var.bin")
    assert WB.build_spr_mod(lbl, out) == rd("spr.bin")
    b, lab = SP.build_spr(out, lbl)
    assert b == rd("spr.bin") and lab == P.labels("spr")
    assert WB.build_inpm2(lbl, out) == (rd("inpm.a.bin"), rd("inpm.b.bin"))
    assert WB.build_inpm(lbl, out) == rd("inpm.a.bin")
    assert WB.build_inpm_b(lbl, out) == rd("inpm.b.bin")
    a, b, lab = RP.build_aud(lbl, out)
    assert (a, b, lab) == (rd("aud.a.bin"), rd("aud.b.bin"), P.labels("aud"))
    assert MW.build_standin(out) == rd("modwb_standin")
    assert MW.build_standin(out, ("STANDIN_NOPROF",)) == rd("modwb_standin_noprof")
    assert not os.path.exists(out), "prebuilt mode wrote into the build directory"


def test_variants_need_the_toolchain(forced, tmp_path, monkeypatch):
    from tests import aros_modwb as MW
    from tests import aros_wb as WB
    from tests import run_paula as RP
    lbl, out = forced, str(tmp_path / "out")
    monkeypatch.setattr(P, "have", lambda *t: False)
    with pytest.raises(P.PrebuiltError, match="TD_ZPJUNK"):
        WB.build_td(lbl, out, ("TD_ZPJUNK=$A5",))
    with pytest.raises(P.PrebuiltError, match="MAXF"):
        MW.build_standin(out, ("STANDIN_NOPROF", "MAXF=200"))
    with pytest.raises(P.PrebuiltError, match="AUD_LOG"):
        RP.build_aud(lbl, out, None, ("AUD_LOG=$C000",))
    with pytest.raises(P.PrebuiltError, match="drv.s"):
        RP.build_drv(lbl, out)
    WB.TD_MUTANT[0] = "anymutant"
    try:
        with pytest.raises(P.PrebuiltError, match="anymutant"):
            WB.build_td(lbl, out)
    finally:
        WB.TD_MUTANT[0] = None


def test_wb_machine_modules_are_prebuilt(forced):
    """The module table WbMachine stages: every entry from prebuilt/."""
    from tests import aros_wb as WB
    for name, fn, org, end in WB.MODULES:
        b = fn(forced, "/nonexistent/w17b")
        assert org + len(b) <= end, name


def _toolchain_skip():
    have = P.versions()
    if None in have.values():
        pytest.skip(f"toolchain absent: {[t for t, v in have.items() if v is None]}")
    pin = P.manifest()["tools"]
    if have != pin:
        pytest.skip(f"toolchain differs from prebuilt/MANIFEST: {have} != {pin}")


def test_rebuild_is_byte_identical(tmp_path):
    """The staleness check: rebuild everything from source; every file must
    equal prebuilt/ byte for byte (else run `make prebuilt`)."""
    _toolchain_skip()
    from tests import mk_prebuilt as MK
    out = str(tmp_path / "rebuilt")
    MK.build_all(out)
    diff = MK.compare(out, P.DIR)
    assert diff == [], f"prebuilt/ is stale (run `make prebuilt`): {diff}"


def test_auto_without_toolchain(monkeypatch, tmp_path):
    """AMIGA64_PREBUILT unset and no toolchain: prebuilt; a variant: a plain error."""
    from tests import aros_wb as WB
    monkeypatch.delenv("AMIGA64_PREBUILT", raising=False)
    monkeypatch.setattr(P, "have", lambda *t: False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError(f"a tool ran: {a}")))
    lbl = P.runner()[1]
    assert WB.build_td(lbl, str(tmp_path)) == open(os.path.join(P.DIR, "td.bin"), "rb").read()
    with pytest.raises(P.PrebuiltError, match="needs ca65, ld65"):
        WB.build_inpm2(lbl, str(tmp_path), None, ("INPM_X=1",))


def test_prebuilt_runner_never_builds(monkeypatch, tmp_path):
    """A session on prebuilt/runner.prg loads the prebuilt modules even with
    the toolchain installed (it would otherwise build into prebuilt/);
    AMIGA64_PREBUILT=0 still builds."""
    monkeypatch.delenv("AMIGA64_PREBUILT", raising=False)
    monkeypatch.setattr(P, "have", lambda *t: True)
    lbl = P.runner()[1]
    assert P.use(("ca65", "ld65"), "td", "", lbl)
    copy = tmp_path / "runner.lbl"
    shutil.copyfile(lbl, copy)
    assert not P.use(("ca65", "ld65"), "td", "", str(copy))
    monkeypatch.setenv("AMIGA64_PREBUILT", "0")
    assert not P.use(("ca65", "ld65"), "td", "", lbl)


def test_find_dir_is_beside_host(monkeypatch):
    """One lookup: $AMIGA64_PREBUILT_DIR, else prebuilt/ beside host/ (a
    checkout, or the wheel's amiga64/_tree/)."""
    monkeypatch.delenv("AMIGA64_PREBUILT_DIR", raising=False)
    host_dir = os.path.dirname(os.path.abspath(P.__file__))
    assert P.find_dir() == os.path.join(os.path.dirname(host_dir), "prebuilt")
    monkeypatch.setenv("AMIGA64_PREBUILT_DIR", "/x/y")
    assert P.find_dir() == "/x/y"


def test_read_only_tree(forced, tmp_path, monkeypatch):
    """Prebuilt mode writes nothing into the tree: every builder, with its
    out_dir inside a read-only copy of prebuilt/ (an installed package)."""
    from tests import aros_boot as AB
    from tests import aros_modwb as MW
    from tests import aros_wb as WB
    from tests import run_paula as RP
    d = _copy(tmp_path)
    monkeypatch.setattr(P, "DIR", d)
    lbl = os.path.join(d, "runner.lbl")
    out = os.path.join(d, "aros-wb-mod")
    for root, dirs, files in os.walk(d):
        for n in files:
            os.chmod(os.path.join(root, n), 0o444)
    os.chmod(d, 0o555)
    try:
        before = sorted(os.listdir(d))
        AB.build_driver(lbl, out)
        WB.wb_driver(lbl, out)
        for _n, fn, _org, _end in WB.MODULES:
            fn(lbl, out)
        WB.build_td(lbl, out, ("TD_VARIANT=$5A",))
        RP.build_aud(lbl, out)
        MW.build_standin(out)
        assert P.module("modwb") == open(P.path("modwb"), "rb").read()
        assert sorted(os.listdir(d)) == before
    finally:
        os.chmod(d, 0o755)


@pytest.mark.parametrize("damage", ["missing", "altered", "manifest"])
def test_errors_are_one_line_naming_the_file(tmp_path, monkeypatch, damage):
    d = _copy(tmp_path)
    monkeypatch.setattr(P, "DIR", d)
    target = "MANIFEST" if damage == "manifest" else "modwb"
    p = os.path.join(d, target)
    if damage == "altered":
        open(p, "ab").write(b"\0")
    else:
        os.remove(p)
    with pytest.raises(P.PrebuiltError) as e:
        P.module("modwb")
    msg = str(e.value)
    assert "\n" not in msg and p in msg, msg
