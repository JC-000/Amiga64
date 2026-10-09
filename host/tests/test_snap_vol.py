"""The --vol option of the W12-T device-file legs (tests/ureu_probe.py --adf,
tests/aros_resume.py m3file): the default stays /USB1 (the U64E paths of the
committed evidence), another volume moves every file under <vol>/amiga64,
and Flash/Temp or a malformed volume are refused before any device call."""

from __future__ import annotations

import pytest

from tests import aros_resume as A
from tests import ureu_probe as P


def test_defaults_unchanged():
    assert P.DEV_VOL == "/USB1" and P.DEV_DIR == "/USB1/amiga64"
    assert A.SNAP_FILE == "/USB1/amiga64/snap-test.bin"
    for mod in (P, A):
        ap_default = [a for a in open(mod.__file__).read().split("\n")
                      if '"--vol", default=' in a]
        assert ap_default and all('default="/USB1"' in a for a in ap_default)


def test_sd_moves_every_file(monkeypatch):
    monkeypatch.setattr(P, "DEV_DIR", P.DEV_DIR)
    monkeypatch.setattr(P, "DEV_VOL", P.DEV_VOL)
    monkeypatch.setattr(A, "SNAP_FILE", A.SNAP_FILE)
    P.set_vol("/SD")
    A.set_vol("/SD")
    assert P.DEV_DIR == "/SD/amiga64"
    assert A.SNAP_FILE == "/SD/amiga64/snap-test.bin"


@pytest.mark.parametrize("vol", ["/Flash", "/Temp", "/flash", "/TEMP", "SD", "/SD/amiga64",
                                 "/", ""])
def test_refused(vol):
    for fn in (P.check_vol, A.check_vol):
        with pytest.raises(ValueError):
            fn(vol)
