"""amiga64: boot Workbench 3.1 on AROS on a C64 Ultimate or an Ultimate 64 Elite.

The host command (``amiga64 doctor``, ``run``, ``resume``, ``adf``) drives the
Ultimate over the network through c64-test-harness.  docs/WAVE17_BRIEF.md
"W17-K"; the user guide is docs/guide/.
"""

__version__ = "0.17.0"

# The one place the runtime tree goes on sys.path (supervisor ruling): every
# amiga64 module may then import host.* and tests.* as the repository does.
from amiga64 import tree as _tree  # noqa: E402

_tree.activate()
