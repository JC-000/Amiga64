"""c64u ser-ring (round 1, R5): chip.s stores the SERDAT byte BEFORE it
bumps chip_sern, on every path.

The host reads the ring while the guest runs (tests/aros_boot.py): a slot
below the count it read must already hold its byte.  The final chip state
cannot show the order (run_chip compares state after the run), so this is
a static check of the source: every control-flow path from `ser:` to
`sinc:` (the count bump) executes exactly one store into chip_ser and no
write to chip_sern, and `sinc:` is the only place the count is written.
The self-tests show it fires on a count bumped before the store and on a
path that skips the store.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHIP = Path(__file__).resolve().parents[2] / "runtime" / "ops" / "chip.s"
BRANCH = {"bcc", "bcs", "beq", "bne", "bmi", "bpl", "bvc", "bvs"}
RMW = {"sta", "stx", "sty", "inc", "dec", "asl", "lsr", "rol", "ror"}
CARRY = {"asl", "lsr", "rol", "ror", "adc", "sbc", "cmp", "cpx", "cpy", "sec", "clc"}


def parse(src: str) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """(mnemonic, operand) per instruction of the ser block, label -> index."""
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("ser:"))
    ins, labels = [], {}
    for l in lines[start:]:
        l = l.split(";")[0].rstrip()
        m = re.match(r"^(\w+):\s*(.*)$", l)
        if m:
            labels[m.group(1)] = len(ins)
            l = m.group(2)
        l = l.strip()
        if not l:
            continue
        if l.startswith("."):           # .endproc
            break
        op, _, arg = l.partition(" ")
        ins.append((op.lower(), arg.strip()))
    return ins, labels


def paths_ok(src: str) -> list[str]:
    ins, lab = parse(src)
    errs: list[str] = []
    assert "sinc" in lab

    def walk(i: int, stores: int, seen: frozenset, c) -> None:
        """c: the carry flag if known on this path (0/1), else None; a
        bcc/bcs on a known carry has one successor (chip.s's "always")."""
        if i == lab["sinc"]:
            if stores != 1:
                errs.append(f"a path reaches sinc with {stores} store(s) into chip_ser")
            return
        if (i, c) in seen or i >= len(ins):
            errs.append(f"a path from ser never reaches sinc (at {i})")
            return
        seen = seen | {(i, c)}
        op, arg = ins[i]
        if op in RMW and arg.startswith("chip_sern"):
            errs.append(f"chip_sern written before sinc: {op} {arg}")
        if op == "sta" and re.match(r"chip_ser(\+\d+)?,x$", arg):
            stores += 1
        if op in CARRY:
            c = None if op not in ("sec", "clc") else int(op == "sec")
        if op == "rts":
            errs.append("rts before sinc")
            return
        if op == "jmp":
            walk(lab[arg], stores, seen, c)
            return
        if op in ("bcc", "bcs"):
            take = 1 if op == "bcs" else 0
            if c is None or c == take:
                walk(lab[arg], stores, seen, take)
            if c is None or c != take:
                walk(i + 1, stores, seen, 1 - take)
            return
        if op in BRANCH:
            walk(lab[arg], stores, seen, c)
        walk(i + 1, stores, seen, c)

    walk(lab["ser"], 0, frozenset(), None)
    # sinc: the count bump itself (low byte, then the carry into the high byte)
    k = lab["sinc"]
    if ins[k] != ("inc", "chip_sern"):
        errs.append(f"sinc does not start with inc chip_sern: {ins[k]}")
    return errs


def test_store_before_count_on_every_path():
    assert paths_ok(CHIP.read_text()) == []


def test_count_written_only_at_sinc():
    src = CHIP.read_text()
    body = src[src.index("ser:    ldx chip_sern"):src.index("sinc:")]
    assert not re.search(r"^\s*(sta|stx|sty|inc|dec)\s+chip_sern", body, re.M)


def test_selftest_count_before_store_fires():
    src = CHIP.read_text()
    bad = src.replace("s3:     sta chip_ser+768,x      ; page 3\n",
                      "s3:     inc chip_sern\n        sta chip_ser+768,x\n")
    assert bad != src
    assert any("written before sinc" in e for e in paths_ok(bad))


def test_selftest_skipped_store_fires():
    src = CHIP.read_text()
    bad = src.replace("        sta chip_ser+256,x      ; page 1\n", "        nop\n")
    assert bad != src
    assert any("0 store" in e for e in paths_ok(bad))


@pytest.mark.parametrize("page", [0, 256, 512, 768])
def test_every_page_has_a_store(page):
    ins, _ = parse(CHIP.read_text())
    want = "chip_ser,x" if page == 0 else f"chip_ser+{page},x"
    assert ("sta", want) in ins
