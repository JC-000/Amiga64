"""W15-U final round (rev15-ui L1): a static check of ui.s's operands.

The chip log records custom WRITES only, so run_modui cannot see a read of
a CIA or of the replay's registers.  This test resolves ui.s's equates and
fails on any operand that addresses
  * a CIA ($BF0000-$BFFFFF),
  * INTENA / INTREQ ($DFF09A, $DFF09C),
  * Paula's audio registers ($DFF0A0-$DFF0DF),
as a literal address ($bfdd00, $dff09a) or as a custom-register name
relative to a base register or CUSTOM (INTENA(a6), CUSTOM+INTREQ).
Reads and writes alike: the UI touches none of them (I-2).  Limit: an
operand computed from equates by an expression (NAME-$x+$y) is not
resolved."""

import os
import re

import pytest

from host import ui_preview as U

UI_S = os.path.join(U.ROOT, "fixtures", "mod", "ui", "ui.s")
CUSTOM = 0xDFF000


def forbidden(addr: int) -> bool:
    return (0xBF0000 <= addr <= 0xBFFFFF or addr in (CUSTOM + 0x09A, CUSTOM + 0x09C)
            or CUSTOM + 0x0A0 <= addr <= CUSTOM + 0x0DF)


def _num(tok: str):
    try:
        return int(tok[1:], 16) if tok.startswith("$") else int(tok)
    except ValueError:
        return None


def violations(src: str) -> list[str]:
    eq = {}
    for ln in src.splitlines():
        m = re.match(r"^(\w+)\s+equ\s+(\$[0-9A-Fa-f]+|\d+)\s*(;.*)?$", ln.strip())
        if m:
            eq[m.group(1)] = _num(m.group(2))
    out = []
    for i, ln in enumerate(src.splitlines(), 1):
        code = ln.split(";")[0]
        if re.match(r"^\s*\w+\s+equ\b", code) or not code.strip():
            continue
        body = code                     # labels carry no addresses
        # literal addresses (immediates #... are values, not addresses)
        for m in re.finditer(r"(?<![#\w])\$([0-9A-Fa-f]{5,6})\b", body):
            if forbidden(int(m.group(1), 16)):
                out.append(f"line {i}: {ln.strip()}")
        # NAME(an) / NAME(pc) with NAME a custom offset; CUSTOM+NAME
        for m in re.finditer(r"(?:CUSTOM\+)?\b(\w+)\((a[0-7]|pc)\)|CUSTOM\+(\w+)", body):
            name = m.group(1) or m.group(3)
            v = eq.get(name)
            if v is not None and v < 0x200 and forbidden(CUSTOM + v):
                out.append(f"line {i}: {ln.strip()}")
        # bare small offsets used as custom displacements: $09a(a6)
        for m in re.finditer(r"(?<![#\w])\$([0-9A-Fa-f]{1,3})\((a[0-7])\)", body):
            if forbidden(CUSTOM + int(m.group(1), 16)):
                out.append(f"line {i}: {ln.strip()}")
    return out


def test_ui_s_touches_no_cia_or_replay_register():
    assert violations(open(UI_S).read()) == []


@pytest.mark.parametrize("line", [
    "        tst.b   $bfdd00",                    # rev15-ui's mutant: a CIA-B read
    "        move.b  $bfe001,d0",                 # CIA-A PRA read
    "        move.w  $dff09a,d0",                 # INTENAR... at the write address
    "        move.w  #$4000,$dff09c",             # INTREQ write
    "        move.w  d0,$0a8(a6)",                # AUD0VOL as a bare offset
    "        move.w  #0,DMACON-$096+$09a(a6)",    # (not resolvable: documented gap)
])
def test_static_check_catches(line):
    src = open(UI_S).read().replace("_ui_frame:\n", "_ui_frame:\n" + line + "\n", 1)
    if "DMACON-$096" in line:
        pytest.skip("expressions over equates are not resolved (documented limit)")
    assert violations(src), line


def test_static_check_catches_named_register():
    src = open(UI_S).read()
    src = src.replace("COLOR00         equ     $180",
                      "COLOR00         equ     $180\nINTENA          equ     $09a", 1)
    src = src.replace("_ui_frame:\n", "_ui_frame:\n        move.w  INTENA(a6),d0\n", 1)
    assert violations(src)
