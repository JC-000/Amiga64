"""W6-S perf criterion (i): with st_exc clear the hot-path INSTRUCTION streams
are exactly those of the pre-W6-S base (wave6/integration, main b14f204).

Region fingerprints (count + SHA-256 prefix of the normalized instruction
lines: comments, blank lines and leading labels dropped, so layout, label
renames and comment edits do not count) are snapshotted from the base
runtime/core.s.  Adding, removing or changing ANY instruction or assembler
directive in fetch/dispatch, dispatch_next, the memory API, EA resolution or
the sized ALU/CCR primitives fails this test.  The two step-loop ENTRIES are
pinned separately: W6-S turned emu_step's `bne dispatch_go` into a patchable
`jmp` (same 3 cycles) and gave emu_run a `jmp` (once per run) -- audited in
docs/PERF.md (f).  build/evidence/w6-super/perf/opcmp.txt is the matching
opcode-level check of the assembled PRG.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[2] / "runtime" / "core.s"

REGIONS = {  # name: (start marker, end marker) in runtime/core.s
    "dispatch_loop": ("dispatch_go:", "dispatch_done:"),
    "fetch_word": ("; fetch_word — read guest big-endian word",
                   "; Memory API — one REU DMA"),
    "memory_api": ("; Memory API — one REU DMA",
                   "; Effective-address resolution"),
    "ea": ("; Effective-address resolution", "; Sized ALU primitives."),
    "alu_ccr": ("; Sized ALU primitives.", "; Halt / trap handlers"),
}

# Snapshot of the BASE source (wave6/integration:runtime/core.s).
EXPECTED = {
    "dispatch_loop": (43, "23d798b7d4164ace"),
    "fetch_word": (62, "b44789edad7c849e"),    # W12-C miss path -> cw_look (hit path pinned: test_cwin_hotpath)
    "memory_api": (310, "b907cb75c12c719c"),   # W7-T trap hook, W12-C WINCHK (strip proofs: test_chip_hotpath, test_cwin_hotpath)
    "ea": (337, "6b52f2bbd7b65462"),
    "alu_ccr": (293, "ab83e441a4fe7cfe"),
}

ENTRY = """emu_step:
lda #1
sta st_step
jmp dispatch_go
emu_run:
lda #0
sta st_step
jmp dispatch_go
dispatch_go:"""


def norm(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].rstrip()
        line = re.sub(r"^\s*[@A-Za-z_]\w*:\s*", "", line)
        line = " ".join(line.split())
        # page-cross guards are not instructions of their own: BRNC op, t
        # assembles to exactly `op t`, HOTALIGN is never-executed fill
        line = re.sub(r"^BRNC (\w+), (\S+)$", r"\1 \2", line)
        if line.startswith("HOTALIGN"):
            continue
        if line:
            out.append(line)
    return out


def fingerprints(src: str) -> dict[str, tuple[int, str]]:
    fp = {}
    for name, (a, b) in REGIONS.items():
        i = src.index(a)
        j = src.index(b, i + len(a))
        if name == "dispatch_loop":
            j = src.index("rts", j) + 3     # through dispatch_done's rts
        lines = norm(src[i:j])
        fp[name] = (len(lines), hashlib.sha256(
            "\n".join(lines).encode()).hexdigest()[:16])
    return fp


def test_hot_path_instruction_streams_match_base() -> None:
    assert fingerprints(CORE.read_text(encoding="utf-8")) == EXPECTED


def test_step_loop_entries_are_the_audited_form() -> None:
    src = CORE.read_text(encoding="utf-8")
    i = re.search(r"^emu_step:", src, re.M).start()
    j = re.search(r"^dispatch_go:", src, re.M).end()
    got = [ln for ln in (" ".join(l.split(";", 1)[0].split())
                         for l in src[i:j].splitlines()) if ln]
    got = [re.sub(r"^(step_jv|run_jv):\s*", "", ln) for ln in got
           if ln not in ("step_jv:", "run_jv:")]
    assert "\n".join(got) == ENTRY


@pytest.mark.parametrize("region", list(REGIONS))
def test_fingerprint_catches_an_added_instruction(region: str) -> None:
    # teeth: one extra NOP inside the region must change its fingerprint
    src = CORE.read_text(encoding="utf-8")
    a, _ = REGIONS[region]
    i = src.index(a)
    k = src.index("\n", src.index("\n", i) + 1)        # after 2nd line
    mutated = src[:k] + "\n        nop" + src[k:]
    assert fingerprints(mutated)[region] != EXPECTED[region]
