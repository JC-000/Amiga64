"""W6-O fix1 (F8): tools/aros/fetch.sh fails closed — black-box, against a
local fake archive (tools/aros/test_fetch.sh; no AROS bytes).  Opt-in like
the FS-UAE legs (AMIGA64_AROS_ORACLE=1), so make host-test stays fast and
offline; it also needs zip, bsdtar, curl and shasum."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests.aros_oracle import PROJECT_ROOT

OPT_IN = "AMIGA64_AROS_ORACLE"
TOOLS = ("bash", "zip", "unzip", "bsdtar", "curl", "shasum")


def _why_not() -> str | None:
    if os.environ.get(OPT_IN) != "1":
        return f"opt-in: set {OPT_IN}=1"
    missing = [t for t in TOOLS if shutil.which(t) is None]
    return f"missing tools: {missing}" if missing else None


@pytest.mark.skipif(_why_not() is not None, reason=_why_not() or "")
def test_fetch_fails_closed(tmp_path):
    aros = os.path.join(PROJECT_ROOT, "tools", "aros")
    r = subprocess.run(["bash", os.path.join(aros, "test_fetch.sh"),
                        os.path.join(aros, "fetch.sh"), str(tmp_path / "t")],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.strip().splitlines()[-1] == "14 passed, 0 failed"
