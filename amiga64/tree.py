"""Where the runtime tree is: the repository root in a clone, or the copy
shipped inside the package (``amiga64/_tree/``, see setup.py) in a wheel.

The tree keeps the repository's layout (host/, tests/, tools/aros/,
prebuilt/, third_party/aros/), so every ROOT-relative path in host/ and
tests/ resolves the same way in both cases.  ``activate()`` puts it first on
sys.path so that ``host`` and ``tests`` import as they do in the repository.
"""

from __future__ import annotations

import os
import sys

PKG = os.path.dirname(os.path.abspath(__file__))


def root() -> str:
    shipped = os.path.join(PKG, "_tree")
    if os.path.isdir(os.path.join(shipped, "host")):
        return shipped
    return os.path.dirname(PKG)


def activate() -> str:
    r = root()
    if not sys.path or sys.path[0] != r:
        sys.path.insert(0, r)
    return r
