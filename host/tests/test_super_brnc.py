"""Teeth for the core.s hot-path page-cross guards (docs/PERF.md (f)).

Copies runtime/ + tests/runner/ to a scratch dir, links it once (it must link
with the guards enforcing), then inserts never-executed fill inside
mem_read16, before its first guarded branch (STRADDLE `bcc`), sized so the
branch's next-instruction address lands on $xxFF: the taken target is then in
the next page.  The normal link must FAIL with the BRNC message; the same copy
linked with BRNC_WARN_ONLY must succeed (warning only) and export
emu_brnc_enforced = 0.  Skips without cl65.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(os.environ.get("AMIGA64_ROOT",
                           Path(__file__).resolve().parents[2]))
MSG = "hot-path branch crosses a page"

pytestmark = pytest.mark.skipif(shutil.which("cl65") is None,
                                reason="cl65 (cc65) not installed")


def _tree(tmp: Path) -> Path:
    src = tmp / "src"
    shutil.copytree(ROOT / "runtime", src / "runtime")
    shutil.copytree(ROOT / "tests" / "runner", src / "tests" / "runner")
    subprocess.run([sys.executable, str(ROOT / "host" / "gen_dispatch.py"),
                    "-o", str(tmp / "dispatch.s"), "--runtime-dir",
                    str(src / "runtime")], check=True)
    return src


def _ops() -> list[str]:
    # the Makefile's own module order (tests/rec_build.make_sources)
    r = subprocess.run(["make", "-s", "--no-print-directory", "-f", "-", "x"],
                       cwd=ROOT, capture_output=True, text=True, check=True,
                       input="include Makefile\nx:\n\t@echo $(RUNTIME_SRC)\n")
    return [p for p in r.stdout.split() if p.startswith("runtime/ops/")]


def _link(src: Path, tmp: Path, *defines: str) -> subprocess.CompletedProcess:
    cmd = ["cl65", "-t", "none", "-C", "runtime/c64.cfg",
           "-o", str(tmp / "t.prg"), "-Ln", str(tmp / "t.lbl")]
    for d in defines:
        cmd += ["--asm-define", d]
    cmd += ["runtime/core.s", *_ops(), "tests/runner/run_program.s",
            "tests/runner/runner.s", str(tmp / "dispatch.s")]
    return subprocess.run(cmd, cwd=src, capture_output=True, text=True)


def _labels(tmp: Path) -> dict[str, int]:
    out = {}
    for line in (tmp / "t.lbl").read_text().splitlines():
        _, addr, name = line.split()
        out[name.lstrip(".")] = int(addr, 16)
    return out


def test_guard_fails_the_link_and_warn_only_escapes(tmp_path: Path) -> None:
    src = _tree(tmp_path)
    ok = _link(src, tmp_path)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert MSG not in ok.stdout + ok.stderr
    lab = _labels(tmp_path)
    assert lab["emu_brnc_enforced"] == 1
    a = lab["mem_read16"]
    prg = (tmp_path / "t.prg").read_bytes()
    load = prg[0] | prg[1] << 8
    # STRADDLE 2: lda m_addr (2) / cmp #imm (2) / bcc no (2) -> branch at +4
    assert prg[2 + a + 4 - load] == 0x90, "mem_read16 layout changed"
    k = (0xFF - ((a + 6) & 0xFF)) & 0xFF
    assert k, "branch already ends a page: the guard would have fired"
    core = src / "runtime" / "core.s"
    text = core.read_text(encoding="utf-8")
    anchor = ".proc mem_read16\n"
    assert text.count(anchor) == 1
    core.write_text(text.replace(anchor, anchor + f"        .res {k}, $EA\n"),
                    encoding="utf-8")
    bad = _link(src, tmp_path)
    assert bad.returncode != 0
    assert MSG in bad.stdout + bad.stderr
    esc = _link(src, tmp_path, "BRNC_WARN_ONLY")
    assert esc.returncode == 0, esc.stdout + esc.stderr
    assert MSG in esc.stdout + esc.stderr            # reported, as a warning
    assert _labels(tmp_path)["emu_brnc_enforced"] == 0
