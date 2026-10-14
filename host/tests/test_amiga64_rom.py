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
