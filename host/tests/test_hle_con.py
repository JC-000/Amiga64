"""W5-K console key map: runtime/ops/hle_con.s tables vs the host mirror.

tests/run_con.py types characters through VICE's key matrix using its
KEYMAP_PLAIN / KEYMAP_SHIFT tables; the 6502 decodes with hle_con_kplain /
hle_con_kshift.  If the two drift, the matrix leg goes red in VICE, but
only for the characters it happens to type; this checks all 128 entries
without an emulator, plus the seam symbols run_con.py relies on.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests import run_con

SRC = Path(__file__).resolve().parents[2] / "runtime" / "ops" / "hle_con.s"


def _equates(text: str) -> dict[str, int]:
    out = {}
    for m in re.finditer(r"^(\w+)\s*=\s*\$([0-9A-Fa-f]+)\b", text, re.M):
        out[m.group(1)] = int(m.group(2), 16)
    return out


def _byte_items(line: str) -> list[str]:
    """Split a `.byte` operand list on commas outside double quotes."""
    items, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    items.append(cur.strip())
    return items


def _code(line: str) -> str:
    """The line without its comment (a ';' inside a string is data)."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ";" and not quoted:
            return line[:i]
    return line


def _table(text: str, name: str) -> bytes:
    eq = _equates(text)
    lines = text[text.index(f"\n{name}:") + len(name) + 2:].splitlines()
    out = bytearray()
    for ln in lines:
        ln = _code(ln).strip()
        if not ln:
            continue
        if not ln.startswith(".byte"):
            break
        for item in _byte_items(ln[len(".byte"):]):
            if item.startswith('"'):
                out += item[1:-1].encode("ascii")   # no escapes in ca65
            elif item.startswith("$"):
                out.append(int(item[1:], 16))
            elif item.isdigit():
                out.append(int(item))
            else:
                out.append(eq[item])
    return bytes(out)


def _host(table: list[str | None]) -> bytes:
    return bytes(ord(c) if c is not None else 0 for c in table)


def test_plain_table_matches_host_mirror():
    text = SRC.read_text()
    assert _table(text, "hle_con_kplain")[:64] == _host(run_con.KEYMAP_PLAIN)


def test_shift_table_matches_host_mirror():
    text = SRC.read_text()
    assert _table(text, "hle_con_kshift")[:64] == _host(run_con.KEYMAP_SHIFT)


def test_modifier_mask_matches_host_modifiers():
    text = SRC.read_text()
    mask = _table(text, "hle_con_nomod")
    assert len(mask) == 8
    for idx in range(64):
        col, row = divmod(idx, 8)
        is_mod = idx in (run_con.LSHIFT, run_con.RSHIFT, run_con.CTRL,
                         run_con.CBM)
        assert bool(mask[col] & (1 << row)) == (not is_mod), idx


def test_every_line_discipline_code_is_typable():
    for ch in (run_con.KEYS_TYPED + run_con.RET + run_con.EOF_KEY
               + run_con.CTRL_D):
        idx, mods = run_con.key_for(ch)
        if run_con.CTRL in mods:
            assert run_con.ctrl_code(idx) == ch
        elif mods:
            assert run_con.KEYMAP_SHIFT[idx] == ch
        else:
            assert run_con.KEYMAP_PLAIN[idx] == ch


def test_seam_symbols_exported():
    text = SRC.read_text()
    exported = set()
    for m in re.finditer(r"^\.export\s+([^;\n]+)", text, re.M):
        exported |= {s.strip() for s in m.group(1).split(",")}
    assert set(run_con.SEAM_LABELS) <= exported
