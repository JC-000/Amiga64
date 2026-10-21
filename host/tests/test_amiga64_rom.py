"""W17-K: the bundled AROS ROM (third_party/aros/) and find_rom.

host/machine.find_rom reads the bundle only (2026-10-21: the download and
the ~/.cache/amiga64/aros cache are gone); its files are verified against
tools/aros/manifest.txt, and a file that does not match is never used
(fail closed)."""

import hashlib
import os
import struct

import pytest

from host import machine


def _banks(seed: int) -> dict[str, bytes]:
    ext = bytes((i * seed) & 0xFF for i in range(machine.BANK_SIZE))
    rom = struct.pack(">II", 0x5A5A0000 + seed, 0x00F800D2) + bytes(machine.BANK_SIZE - 8)
    return {"aros-ext.bin": ext, "aros-rom.bin": rom}


def _setup(tmp_path, bundle=True, tamper=None):
    files = _banks(7)
    tmp_path.mkdir(parents=True, exist_ok=True)
    man = tmp_path / "manifest.txt"
    man.write_text("".join(f"file|{n}|x!{n}|{hashlib.sha256(b).hexdigest()}\n"
                           for n, b in files.items()))
    d = tmp_path / "third_party"
    if bundle:
        d.mkdir(parents=True)
        for n, b in files.items():
            if n == tamper:
                b = bytes([b[0] ^ 0xFF]) + b[1:]
            (d / n).write_bytes(b)
    return str(man), str(d)


def test_bundle_used(tmp_path):
    man, bundle = _setup(tmp_path)
    rom, why = machine.find_rom(man, bundle)
    assert rom is not None and why == ""
    assert rom.bundle == bundle and rom.ext == _banks(7)["aros-ext.bin"]


@pytest.mark.parametrize("name", ["aros-ext.bin", "aros-rom.bin"])
def test_tampered_bundle_never_used(tmp_path, name):
    man, bundle = _setup(tmp_path, tamper=name)
    rom, why = machine.find_rom(man, bundle, checkout=True)
    assert rom is None
    assert "SHA256 mismatch" in why and name in why and "bundle" in why


def test_no_bundle(tmp_path):
    man, bundle = _setup(tmp_path, bundle=False)
    rom, why = machine.find_rom(man, bundle, checkout=True)
    assert rom is None and "no bundle" in why


def test_the_repository_bundle_matches_the_manifest():
    """third_party/aros/ holds the pinned files byte for byte (the ruling)."""
    pinned = machine._manifest(machine.MANIFEST)
    assert {"aros-rom.bin", "aros-ext.bin", "LICENSE", "ACKNOWLEDGEMENTS",
            "C/Copy", "C/Assign"} <= set(pinned)
    for name, want in pinned.items():
        p = os.path.join(machine.BUNDLE, name)
        with open(p, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == want, name
    assert os.path.isfile(os.path.join(machine.BUNDLE, "SOURCE.txt"))
    rom, why = machine.find_rom()
    assert rom is not None, why
    assert rom.bundle == machine.BUNDLE


def test_source_notice_names_the_builds_and_the_licence():
    with open(os.path.join(machine.BUNDLE, "SOURCE.txt")) as f:
        t = f.read()
    assert "APL 1.1" in t and "github.com/aros-development-team/AROS" in t
    for build, commit in (("20260921", "6a327488b5"), ("20261021", "bfe9b1cd67")):
        assert f"nightly2 {build}" in t and commit in t, build


def test_nothing_downloads_aros():
    """2026-10-21: no fetch.sh and no cache path left anywhere in the tree."""
    assert not os.path.exists(os.path.join(machine.ROOT, "tools", "aros", "fetch.sh"))
    for top in ("host", "tests", "tools", "amiga64"):
        for d, _dirs, files in os.walk(os.path.join(machine.ROOT, top)):
            for n in files:
                if n.endswith(".py") and n != os.path.basename(__file__):
                    with open(os.path.join(d, n)) as f:
                        assert ".cache/amiga64/aros/" not in f.read(), n


# --- W17-K2: rev17-k #4 and #6 -------------------------------------------------------

def test_a_broken_bundle_in_an_install_says_reinstall(tmp_path):
    """An installed package is not a checkout: the advice is to reinstall,
    never to restore the bundle with git (rev17-k #4)."""
    man, bundle = _setup(tmp_path, tamper="aros-rom.bin")
    rom, why = machine.find_rom(man, bundle, checkout=False)
    assert rom is None
    assert why.startswith("broken install: ") and "reinstall amiga64" in why
    assert "git" not in why and "delete" not in why


def test_no_bundle_in_an_install_says_reinstall(tmp_path):
    man, bundle = _setup(tmp_path, bundle=False)
    rom, why = machine.find_rom(man, bundle, checkout=False)
    assert rom is None and why.startswith("broken install: ")


def test_a_broken_bundle_in_a_checkout_names_git_not_delete(tmp_path):
    man, bundle = _setup(tmp_path, tamper="aros-rom.bin")
    rom, why = machine.find_rom(man, bundle, checkout=True)
    assert rom is None and "SHA256 mismatch" in why and "third_party" in why
    assert "delete" not in why


def test_the_checkout_is_detected_by_setup_py(tmp_path, monkeypatch):
    assert machine.is_checkout() is True                 # this tree is a checkout
    monkeypatch.setattr(machine, "ROOT", str(tmp_path))  # an installed tree: no setup.py
    (tmp_path / "tools" / "aros").mkdir(parents=True)
    (tmp_path / "tools" / "aros" / "manifest.txt").write_text("")
    assert machine.is_checkout() is False

def test_source_notice_quotes_the_roms_git_revision():
    """SOURCE.txt pins the source revision the ROM itself states (rev17-k #6);
    only that version string is read from the ROM."""
    with open(os.path.join(machine.BUNDLE, "aros-rom.bin"), "rb") as f:
        rom = f.read()
    stamp = b"Version Git 6a327488b5 (https://github.com/aros-development-team/AROS)\nbuilt on 2026-09-21."
    assert stamp in rom
    with open(os.path.join(machine.BUNDLE, "SOURCE.txt")) as f:
        t = f.read()
    assert "6a327488b5" in t and "Version Git 6a327488b5" in t
    assert "github.com/aros-development-team/AROS/commit/6a327488b5" in t
