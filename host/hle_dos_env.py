"""File-DOS half of the HLE environment (owner: WS-B2, docs/WAVE4_BRIEF.md C3).

``DOS_FILE_FUNCS`` maps dos.library function name -> (LVO, HLE index) for the
file-I/O functions serviced by ``runtime/ops/hle_dos.s``.  Indices MUST lie
in $30-$4F (hle.s routes that range to ``hle_dos_dispatch``).
``host/hle_env.py`` merges this table into ``DOS_FUNCS``.
"""

from __future__ import annotations

DOS_FILE_FUNCS: dict[str, tuple[int, int]] = {}
