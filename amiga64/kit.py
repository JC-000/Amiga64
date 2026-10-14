"""What ships with amiga64: the prebuilt runner and ModPlayer (W17-B's
prebuilt/), the bundled AROS ROM (third_party/aros/) and the demo MOD.
The tree is on sys.path (amiga64/__init__.py)."""

from __future__ import annotations

from amiga64 import config as C

SONG = "demo"                           # host/modgen: ModPlayer.mod on the session disk


class KitError(C.ConfigError):
    """A shipped file is missing or altered (a broken install)."""


def _prebuilt():
    try:
        from host import prebuilt as P
    except ImportError as e:
        raise KitError(f"the prebuilt loader (host/prebuilt.py) is missing: {e}") from None
    return P


def runner() -> str:
    """The prebuilt runner.prg (its .lbl beside it), checked against MANIFEST."""
    P = _prebuilt()
    try:
        return P.runner()[0]
    except P.PrebuiltError as e:
        raise KitError(f"broken install: {e}") from None


def player() -> bytes:
    """The ModPlayer hunk executable (fixtures/modwb, prebuilt)."""
    P = _prebuilt()
    try:
        return P.module("modwb")
    except P.PrebuiltError as e:
        raise KitError(f"broken install: {e}") from None


def song() -> bytes:
    from host import modgen
    return modgen.songs()[SONG]


def verify() -> list[str]:
    """Problems with prebuilt/ (empty: all good)."""
    P = _prebuilt()
    try:
        return list(P.verify())
    except P.PrebuiltError as e:
        return [str(e)]


def build_id() -> str | None:
    P = _prebuilt()
    try:
        return P.manifest().get("build_id")
    except P.PrebuiltError:
        return None


def rom():
    """(Rom, "") or (None, why): host/machine.find_rom (the bundle first)."""
    from host import machine
    return machine.find_rom()
