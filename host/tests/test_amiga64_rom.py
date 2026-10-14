"""W17-K: the bundled AROS ROM (third_party/aros/) and find_rom's order.

host/machine.find_rom prefers the bundle and falls back to the
~/.cache/amiga64/aros cache; both are verified against the same
tools/aros/manifest.txt hashes, and a file that does not match is never
used (fail closed)."""

import hashlib
import os
import struct

import pytest

from host import machine


def _banks(seed: int) -> dict[str, bytes]:
    ext = bytes((i * seed) & 0xFF for i in range(machine.BANK_SIZE))
    rom = struct.pack(">II", 0x5A5A0000 + seed, 0x00F800D2) + bytes(machine.BANK_SIZE - 8)
    return {"aros-ext.bin": ext, "aros-rom.bin": rom}


def _setup(tmp_path, bundle=True, cache=True, tamper_bundle=None, tamper_cache=None):
    files = _banks(7)
    tmp_path.mkdir(parents=True, exist_ok=True)
    arch = "cd" * 32
    man = tmp_path / "manifest.txt"
    man.write_text(f"archive|https://example.invalid/x.zip|{arch}\n" +
                   "".join(f"file|{n}|x!{n}|{hashlib.sha256(b).hexdigest()}\n"
                           for n, b in files.items()))
    dirs = {}
    for what, on, tamper, d in (("bundle", bundle, tamper_bundle, tmp_path / "third_party"),
                                ("cache", cache, tamper_cache, tmp_path / "cache" / arch)):
        dirs[what] = d
        if not on:
            continue
        d.mkdir(parents=True)
        for n, b in files.items():
            if n == tamper:
                b = bytes([b[0] ^ 0xFF]) + b[1:]
            (d / n).write_bytes(b)
    return str(man), str(tmp_path / "cache"), str(dirs["bundle"])


def test_bundle_preferred(tmp_path):
    man, root, bundle = _setup(tmp_path)
    rom, why = machine.find_rom(man, root, bundle)
    assert rom is not None and why == ""
    assert rom.cache == bundle


def test_cache_when_no_bundle(tmp_path):
    man, root, bundle = _setup(tmp_path, bundle=False)
    rom, why = machine.find_rom(man, root, bundle)
    assert rom is not None and rom.cache.startswith(root)


@pytest.mark.parametrize("name", ["aros-ext.bin", "aros-rom.bin"])
def test_tampered_bundle_never_used(tmp_path, name):
    man, root, bundle = _setup(tmp_path, tamper_bundle=name)
    rom, why = machine.find_rom(man, root, bundle)
    assert rom is not None and rom.cache.startswith(root)     # the verified cache
    assert rom.ext == _banks(7)["aros-ext.bin"]
    man, root, bundle = _setup(tmp_path / "b", tamper_bundle=name, cache=False)
    rom, why = machine.find_rom(man, root, bundle)
    assert rom is None
    assert "SHA256 mismatch" in why and name in why and "bundle" in why


def test_neither(tmp_path):
    man, root, bundle = _setup(tmp_path, bundle=False, cache=False)
    rom, why = machine.find_rom(man, root, bundle)
    assert rom is None and "fetch.sh" in why


def test_the_repository_bundle_matches_the_manifest():
    """third_party/aros/ holds the pinned files byte for byte (the ruling)."""
    _arch, pinned = machine._manifest(machine.MANIFEST)
    for name in ("aros-rom.bin", "aros-ext.bin", "LICENSE", "ACKNOWLEDGEMENTS"):
        p = os.path.join(machine.BUNDLE, name)
        with open(p, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == pinned[name], name
    assert os.path.isfile(os.path.join(machine.BUNDLE, "SOURCE.txt"))
    rom, why = machine.find_rom()
    assert rom is not None, why
    assert rom.cache == machine.BUNDLE


def test_source_notice_names_the_build_and_the_licence():
    with open(os.path.join(machine.BUNDLE, "SOURCE.txt")) as f:
        t = f.read()
    arch, _ = machine._manifest(machine.MANIFEST)
    assert arch in t and "APL 1.1" in t and "github.com/aros-development-team/AROS" in t


# --- W17-K2: rev17-k #4 and #6 -------------------------------------------------------

@pytest.mark.parametrize("cache", [False, True])
def test_a_broken_bundle_in_an_install_says_reinstall(tmp_path, cache):
    """An installed package has no tools/aros/fetch.sh: the advice is to
    reinstall, never to delete the bundle or run fetch.sh (rev17-k #4)."""
    man, root, bundle = _setup(tmp_path, tamper_bundle="aros-rom.bin", cache=cache,
                               tamper_cache="aros-rom.bin" if cache else None)
    rom, why = machine.find_rom(man, root, bundle, checkout=False)
    assert rom is None
    assert why.startswith("broken install: ") and "reinstall amiga64" in why
    assert "fetch.sh" not in why and "delete" not in why


def test_no_bundle_in_an_install_says_reinstall(tmp_path):
    man, root, bundle = _setup(tmp_path, bundle=False, cache=False)
    rom, why = machine.find_rom(man, root, bundle, checkout=False)
    assert rom is None and why.startswith("broken install: ") and "fetch.sh" not in why


def test_a_broken_bundle_in_a_checkout_names_git_not_delete(tmp_path):
    man, root, bundle = _setup(tmp_path, tamper_bundle="aros-rom.bin", cache=False)
    rom, why = machine.find_rom(man, root, bundle, checkout=True)
    assert rom is None and "SHA256 mismatch" in why and "third_party" in why
    assert "delete the directory" not in why.split("bundle:")[1]


def test_the_checkout_is_detected_by_fetch_sh(tmp_path, monkeypatch):
    assert machine.is_checkout() is True                 # this tree is a checkout
    monkeypatch.setattr(machine, "ROOT", str(tmp_path))  # an installed tree: no fetch.sh
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
