"""W17-K: amiga64/adf.py -- the ADF checks, `adf add` (staged onto the
session's REU copy, or a new local ADF with --out) and `adf ls`.

Synthetic disks (host/tests/test_adf_files.disk); the user's Workbench disk
only in the local-only test at the end (read-only; nothing from it is
printed or kept)."""

import hashlib
import os
import struct

import pytest

from amiga64 import adf as A
from host import adf_files as F
from host import adf_overlay as O
from host.tests.test_adf_files import USER_ADF, blob, disk

PLAYER = b"\0\0\x03\xf3" + blob(3000, 5)          # stands in for the ModPlayer (size only)
MOD = blob(6460, 3)


def wbdisk(ffs: bool = True, extra: dict | None = None) -> bytes:
    """A synthetic Workbench-like disk the ModPlayer files fit on."""
    x = {A.MODWB_DROP: blob(9000, 11)}
    x.update(extra or {})
    return disk(ffs, x)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    """Never the user's own config or state (the staged list is in the config)."""
    monkeypatch.setenv("AMIGA64_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("AMIGA64_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _file(tmp_path, name: str, data: bytes) -> str:
    p = tmp_path / "in" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


def _adf(tmp_path, raw: bytes, name="wb.adf") -> str:
    p = tmp_path / name
    p.write_bytes(raw)
    return str(p)


# --- the checks ----------------------------------------------------------------

def test_inspect_a_good_untested_disk(tmp_path):
    raw = wbdisk()
    info = A.inspect(_adf(tmp_path, raw))
    assert info.sha256 == hashlib.sha256(raw).hexdigest()
    assert info.tested is False and "untested" in info.verdict
    assert info.dostype == 1 and info.free > 0


def test_inspect_knows_the_tested_image(tmp_path, monkeypatch):
    raw = wbdisk()
    monkeypatch.setitem(A.TESTED, hashlib.sha256(raw).hexdigest(), "the test image")
    info = A.inspect(_adf(tmp_path, raw))
    assert info.tested and info.verdict.startswith("tested")


def test_the_tested_list_is_the_wave_13_to_16_image():
    assert "24dbf267a7f7b01757025fa682a173cef4092a915eff36a5844b264c13fc729c" in A.TESTED


@pytest.mark.parametrize("mutate,words", [
    (lambda r: r[:-512], ["900,608", "901,120"]),
    (lambda r: r + bytes(901120), ["1,802,240", "HD"]),
    (lambda r: b"KICK" + r[4:], ["not an AmigaDOS disk"]),
    (lambda r: r[:3] + b"\x04" + r[4:], ["DOS\\4", "directory cache"]),
    (lambda r: r[:3] + b"\x07" + r[4:], ["DOS\\7"]),
])
def test_inspect_refuses_with_a_plain_message(tmp_path, mutate, words):
    with pytest.raises(A.AdfProblem) as e:
        A.inspect(_adf(tmp_path, mutate(wbdisk())))
    for w in words:
        assert w in str(e.value)


def test_inspect_missing_file(tmp_path):
    with pytest.raises(A.AdfProblem) as e:
        A.inspect(str(tmp_path / "nope.adf"))
    assert "no file" in str(e.value)


def test_inspect_refuses_a_broken_file_system(tmp_path):
    raw = bytearray(wbdisk())
    k = O.ROOT
    struct.pack_into(">I", raw, k * 512 + 24 + 4 * 5, 1759)   # a hash slot to a junk block
    blk = raw[k * 512:(k + 1) * 512]
    struct.pack_into(">I", blk, 20, O.block_checksum(blk))
    raw[k * 512:(k + 1) * 512] = blk
    with pytest.raises(A.AdfProblem) as e:
        A.inspect(_adf(tmp_path, bytes(raw)))
    assert "file system" in str(e.value)


def test_inspect_needs_a_startup_sequence(tmp_path):
    raw = with_no_ss = disk(True, {A.MODWB_DROP: blob(100)})
    e = F.DiskEdit(raw)
    e.delete("S/Startup-Sequence")
    with pytest.raises(A.AdfProblem) as ex:
        A.inspect(_adf(tmp_path, e.data()))
    assert "Startup-Sequence" in str(ex.value)
    del with_no_ss


# --- adf add: the plan and the limits ------------------------------------------------

def test_add_a_file_and_a_drawer(tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "Hello.txt", b"hi there\n")
    d = tmp_path / "in" / "Tools"
    (d / "Sub").mkdir(parents=True)
    (d / "a.bin").write_bytes(blob(700))
    (d / "Sub" / "b").write_bytes(b"b")
    (d / ".DS_Store").write_bytes(b"junk")
    reqs = [A.Request(f), A.Request(str(d))]
    img, report = A.session_image(raw, PLAYER, MOD, reqs)
    F.fsck(img)
    a = O.Adf(img)
    assert a.read_file(a.lookup("Hello.txt")) == b"hi there\n"
    assert a.read_file(a.lookup("Tools/a.bin")) == blob(700)
    assert a.read_file(a.lookup("Tools/Sub/b")) == b"b"
    assert a.lookup("Tools/.DS_Store") is None
    assert any(".DS_Store" in s for s in report.skipped)
    assert a.lookup(A.MODWB_TOOL) is not None and a.lookup(A.MODWB_DROP) is None
    assert report.added == ["Hello.txt", "Tools", "Tools/Sub", "Tools/Sub/b", "Tools/a.bin"]


def test_add_into_a_drawer_with_another_name(tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "x.txt", b"x")
    img, rep = A.session_image(raw, PLAYER, MOD, [A.Request(f, to="S", as_name="Y.txt")])
    a = O.Adf(img)
    assert a.read_file(a.lookup("S/Y.txt")) == b"x" and rep.added == ["S/Y.txt"]


@pytest.mark.parametrize("name,words", [
    ("a" * 31, ["31 characters", "at most 30", "--as"]),
    ("a:b", ["':'"]),
    ("snow☃", ["Latin-1"]),
])
def test_bad_names(tmp_path, name, words):
    f = _file(tmp_path, "ok", b"1")
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(f, as_name=name)])
    for w in words:
        assert w in str(e.value)


def test_a_long_host_name_is_refused_not_cut(tmp_path):
    f = _file(tmp_path, "b" * 40 + ".txt", b"1")
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(f)])
    assert "44 characters" in str(e.value)


def test_never_overwrites(tmp_path):
    f = _file(tmp_path, "Startup-Sequence", b"echo hi\n")
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(f, to="S")])
    assert "S/Startup-Sequence already exists" in str(e.value) and "never overwrites" in str(e.value)
    g = _file(tmp_path, "ModPlayer", b"1")         # nor the session's own files
    with pytest.raises(A.AddError):
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(g)])


def test_no_such_drawer(tmp_path):
    f = _file(tmp_path, "x", b"1")
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(f, to="Nope/Deeper")])
    assert "--to Nope/Deeper" in str(e.value) and "no such drawer" in str(e.value)
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(f, to="S/Startup-Sequence")])
    assert "not a drawer" in str(e.value)


def test_full(tmp_path):
    raw = wbdisk()
    free = F.DiskEdit(A.session_image(raw, PLAYER, MOD, [])[0]).free_count()
    big = _file(tmp_path, "Big.mod", blob(free * 512 + 10))
    with pytest.raises(A.AddError) as e:
        A.session_image(raw, PLAYER, MOD, [A.Request(big)])
    msg = str(e.value)
    assert "Not enough room" in msg and "Big.mod" in msg
    assert f"{free} free" in msg and "never deletes" in msg


def test_blocks_needed_matches_add_file():
    for ffs in (True, False):
        for n in (0, 1, 487, 488, 489, 512, 513, 72 * 512, 72 * 512 + 1, 200 * 512):
            e = F.DiskEdit(wbdisk(ffs))
            before = e.free_count()
            e.add_file("T", blob(n))
            assert before - e.free_count() == A.blocks_needed(n, ffs), (ffs, n)


def test_missing_and_special_host_paths(tmp_path):
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(str(tmp_path / "gone"))])
    assert "no such file" in str(e.value)
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(str(fifo))])
    assert "only plain files and directories" in str(e.value)
    link = tmp_path / "link"
    link.symlink_to(_file(tmp_path, "target", b"t"))
    with pytest.raises(A.AddError) as e:
        A.session_image(wbdisk(), PLAYER, MOD, [A.Request(str(link))])
    assert "symbolic link" in str(e.value)


# --- the staged list -------------------------------------------------------------

def test_staging_round_trip_and_duplicates(state, tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "a.txt", b"a")
    s = A.Staged()
    assert s.requests() == []
    s.add(raw, PLAYER, MOD, [A.Request(f)])
    assert [r.host for r in A.Staged().requests()] == [f]
    with pytest.raises(A.AddError) as e:          # the same disk name twice
        A.Staged().add(raw, PLAYER, MOD, [A.Request(f)])
    assert "already exists" in str(e.value)
    assert len(A.Staged().requests()) == 1        # a refused add changes nothing
    assert A.Staged().remove("A.TXT") == 1        # AmigaDOS names: case-insensitive
    assert A.Staged().requests() == []


def test_a_refused_add_is_not_staged(state, tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "c" * 31, b"1")
    with pytest.raises(A.AddError):
        A.Staged().add(raw, PLAYER, MOD, [A.Request(f)])
    assert A.Staged().requests() == []


def test_staged_files_are_read_again_at_run(state, tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "grow.bin", b"1")
    A.Staged().add(raw, PLAYER, MOD, [A.Request(f)])
    free = F.DiskEdit(A.session_image(raw, PLAYER, MOD, [])[0]).free_count()
    with open(f, "wb") as fh:
        fh.write(blob(free * 512 + 1))
    with pytest.raises(A.AddError) as e:
        A.session_image(raw, PLAYER, MOD, A.Staged().requests())
    assert "Not enough room" in str(e.value)
    A.Staged().clear()
    assert A.Staged().requests() == []


# --- the session disk: the REU copy only --------------------------------------------

def test_session_overlay_reproduces_the_image(tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "n.txt", blob(2000))
    img, _ = A.session_image(raw, PLAYER, MOD, [A.Request(f)])
    ov = F.overlay_of(raw, img, "trim+modwb+files")
    assert O.apply(raw, ov) == img
    assert all(k >= 2 for k, _ in ov.blocks)


def test_the_users_file_is_never_written(state, tmp_path):
    raw = wbdisk()
    p = _adf(tmp_path, raw)
    st = os.stat(p)
    f = _file(tmp_path, "z.txt", b"z")
    A.inspect(p)
    A.Staged().add(raw, PLAYER, MOD, [A.Request(f)])
    out = str(tmp_path / "out.adf")
    A.write_out(p, [A.Request(f)], out)
    assert open(p, "rb").read() == raw
    assert os.stat(p).st_mtime_ns == st.st_mtime_ns


# --- --out -------------------------------------------------------------------------

def test_out_is_the_original_plus_the_files(tmp_path):
    raw = wbdisk()
    p = _adf(tmp_path, raw)
    f = _file(tmp_path, "z.txt", b"zz")
    out = str(tmp_path / "new.adf")
    rep = A.write_out(p, [A.Request(f, to="S")], out)
    new = open(out, "rb").read()
    F.fsck(new)
    a = O.Adf(new)
    assert a.read_file(a.lookup("S/z.txt")) == b"zz"
    assert a.lookup(A.MODWB_DROP) is not None and a.lookup(A.MODWB_TOOL) is None
    assert a.read_file(a.lookup("S/Startup-Sequence")) == O.Adf(raw).read_file(
        O.Adf(raw).lookup("S/Startup-Sequence"))       # no script overlay either
    assert rep.added == ["S/z.txt"]


def test_out_never_replaces_a_file(tmp_path):
    raw = wbdisk()
    p = _adf(tmp_path, raw)
    f = _file(tmp_path, "z.txt", b"zz")
    with pytest.raises(A.AddError) as e:
        A.write_out(p, [A.Request(f)], p)
    assert "exists" in str(e.value)
    assert open(p, "rb").read() == raw
    other = _adf(tmp_path, b"keep", "other.adf")
    with pytest.raises(A.AddError):
        A.write_out(p, [A.Request(f)], other)
    assert open(other, "rb").read() == b"keep"


# --- adf ls ------------------------------------------------------------------------

def test_listing(tmp_path):
    raw = wbdisk()
    f = _file(tmp_path, "Notes.txt", b"n" * 1000)
    img, _ = A.session_image(raw, PLAYER, MOD, [A.Request(f)])
    lines = A.listing(img, "", staged={"notes.txt"})
    text = "\n".join(lines)
    assert "Notes.txt" in text and "1000" in text
    row = next(x for x in lines if "Notes.txt" in x)
    assert "staged" in row
    assert "S/" in text or any(x.strip().startswith("S") for x in lines)
    assert lines[-1].startswith(f"{F.DiskEdit(img).free_count()} blocks free")
    sub = A.listing(img, "S")
    assert any("Startup-Sequence" in x for x in sub)
    with pytest.raises(A.AddError):
        A.listing(img, "Nope")


# --- the user's disk (local only) --------------------------------------------------

@pytest.mark.skipif(not os.path.exists(USER_ADF), reason="the user's ADF is not on this machine")
def test_the_users_disk_is_tested_and_has_room():
    info = A.inspect(USER_ADF)
    assert info.tested and info.ffs
    img, _ = A.session_image(open(USER_ADF, "rb").read(), PLAYER, MOD, [])
    assert F.DiskEdit(img).free_count() > 0


def test_the_staged_list_lives_in_the_config(tmp_path):
    from amiga64 import config as C
    C.save({"device": {"host": "1.2.3.4"}})
    f = _file(tmp_path, "s.txt", b"s")
    A.Staged().add(wbdisk(), PLAYER, MOD, [A.Request(f, to="S", as_name="T.txt")])
    cfg = C.load()
    assert cfg["staged"] == [{"host": f, "to": "S", "as": "T.txt"}]
    assert cfg["device"]["host"] == "1.2.3.4"           # the rest of the file is kept
    A.Staged().clear()
    assert "staged" not in C.load() and C.load()["device"]["host"] == "1.2.3.4"
