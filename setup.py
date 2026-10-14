"""W17-K: the amiga64 wheel ships the runtime tree inside the package.

The tree (host/, tests/, tools/aros/manifest.txt, prebuilt/,
third_party/aros/) keeps the repository layout under amiga64/_tree/, so its
ROOT-relative paths resolve as in a clone (amiga64/tree.py), and no generic
top-level `host` or `tests` package is installed into site-packages.  The
metadata is in pyproject.toml.
"""

import os

from setuptools import setup
from setuptools.command.build_py import build_py

HERE = os.path.dirname(os.path.abspath(__file__))
T = "amiga64._tree"


def subdirs(top: str) -> list[str]:
    out = []
    for d, dirs, _files in os.walk(os.path.join(HERE, top)):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        out.append(os.path.relpath(d, HERE))
    return out


packages = ["amiga64", T]
package_dir = {"amiga64": "amiga64", T: "."}
package_data = {T: ["LICENSE", "NOTICE", "third_party/aros/*", "tools/aros/manifest.txt",
                    "prebuilt/*"]}
for d in ["host"] + subdirs("tests"):           # host/tests (the pytest suite) stays out
    name = T + "." + d.replace(os.sep, ".")
    packages.append(name)
    package_dir[name] = d
    package_data[name] = ["*.s", "*.inc", "*.cfg"]

class BuildPy(build_py):
    """W17-K2: amiga64._tree maps to the repository root, whose only .py
    file is this setup.py: it is not part of the runtime tree."""

    def find_package_modules(self, package, package_dir):
        return [m for m in super().find_package_modules(package, package_dir)
                if (m[0], m[1]) != (T, "setup")]


setup(packages=packages, package_dir=package_dir, package_data=package_data,
      include_package_data=False, cmdclass={"build_py": BuildPy})
