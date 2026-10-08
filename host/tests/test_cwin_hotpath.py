"""W12-C (code-window cache): the per-instruction paths stay as they were.

docs/WAVE12_BRIEF.md W12-C: the cache may cost more on a page change, but
the fetch hit path must not change.  Pinned here (instruction streams,
normalized like host/tests/test_super_hotpath.py):

  * dispatch_go's inlined hit path and fetch_word's hit path (serve: ..
    its rts) are the base instructions; only the two load operands'
    high bytes are re-pointed at run time (cw_look's sel), and the source
    operand stays `code_win` (= cwin_base, window 0);
  * fetch_word's miss path is the banking prologue + jmp cw_look only;
  * the production WINCHK_B / WINCHK_W common path is exactly
    lda m_addr+1 / sta cwlk+1 / lda cwin_map / bmi|bne cwslow (13 cycles);
  * outside the WINCHK macro block and the three WINCHK_SLOW_* lines the
    memory-API region is the base one: replacing the block with the base
    text, dropping the slow-stub lines and stripping W7-T's tagged lines
    gives back the base fingerprint (test_chip_hotpath's proof, extended).

test_super_hotpath.EXPECTED carries the new fetch_word / memory_api
fingerprints; these tests say what changed in them and what did not.
"""

from __future__ import annotations

import re
from pathlib import Path

from host.tests import test_chip_hotpath as ch
from host.tests import test_super_hotpath as hp

CORE = Path(__file__).resolve().parents[2] / "runtime" / "core.s"

# runtime/core.s at wave12/integration e9c8cdd: the WINCHK block
BASE_WINCHK = '.if .defined(FETCH_DIRECT) .or .defined(WINDOW_NOINVAL)\n.macro WINCHK_B\n.endmacro\n.macro WINCHK_W lastoff\n.endmacro\n.else\n.macro WINCHK_B\n        .local kill, done\n        lda m_addr+1            ; 3\n        eor w_pg0               ; 3\n        BRNC bne, done                ; 3 taken = common exit (9 cycles)\n        lda m_addr+2\n        eor w_pg1\n        BRNC bne, done\nkill:   lda #1\n        sta w_inv\ndone:\n.endmacro\n\n.macro WINCHK_W lastoff\n        .local chkhi, cross, kill, done\n        lda m_addr+1            ; 3\n        eor w_pg0               ; 3\n        BRNC beq, chkhi               ; 2\n        lda m_addr              ; 3   page differs: overlap only possible if\n        cmp #$100 - lastoff     ; 2   the write spills into the next page\n        BRNC bcc, done                ; 3 taken = common exit (16 cycles)\ncross:  lda m_addr+1            ; C=1 from the cmp: A = write page + 1\n        adc #0\n        eor w_pg0\n        BRNC bne, done\n        lda m_addr+2\n        adc #0                  ; propagate the +1 carry (eor kept C)\n        eor w_pg1\n        BRNC bne, done\n        BRNC beq, kill                ; always\nchkhi:  lda m_addr+2\n        eor w_pg1\n        BRNC bne, done\nkill:   lda #1\n        sta w_inv\ndone:\n.endmacro\n.endif\n\n'

BASE_HIT = [            # fetch_word serve: .. rts (base e9c8cdd)
    "ldx r_pc", "lda code_win,x", "sta op_hi", "sta t_tmp+1",
    "lda code_win+1,x", "sta op_lo", "sta t_tmp", "inx", "inx",
    "stx r_pc", "beq wrap", "rts"]

BASE_DG = [             # dispatch_go's inlined hit path, ldx .. beq dg_wrap
    "ldx r_pc", "lda code_win,x", "sta op_hi", "sta t_tmp+1",
    "lda code_win+1,x", "sta op_lo", "sta t_tmp", "inx", "inx",
    "stx r_pc", "beq dg_wrap"]


def _src() -> str:
    return CORE.read_text(encoding="utf-8")


def _block(src: str) -> tuple[int, int]:
    i = src.index(".if .defined(FETCH_DIRECT) .or .defined(WINDOW_NOINVAL)\n.macro WINCHK_B")
    return i, src.index("REU_CMD_FETCH_AL = ", i)


def test_fetch_word_hit_path_is_the_base() -> None:
    src = _src()
    i = src.index("serve:  ldx r_pc")
    j = src.index("wrap:", i)
    assert hp.norm(src[i:j]) == BASE_HIT


def test_dispatch_go_hit_path_is_the_base() -> None:
    src = _src()
    i = src.index("        ldx r_pc                ; 3   in-window offset", src.index("dispatch_go:"))
    j = src.index(".endif", i)
    got = hp.norm(src[i:j])
    assert got[:len(BASE_DG)] == BASE_DG


def test_miss_path_is_prologue_and_jump() -> None:
    src = _src()
    i = src.index("miss:", src.index(".proc fetch_word", src.index("; Windowed build")))
    j = src.index(".endproc", i)
    lines = [ln for ln in hp.norm(src[i:j]) if not ln.startswith(".")]
    assert lines == ["lda CPU_PORT", "and #3", "cmp #3", "bne nobas",
                     "dec CPU_PORT", "jmp cw_look"]


def test_winchk_common_path_is_four_instructions() -> None:
    src = _src()
    i, j = _block(src)
    blk = src[i:j]
    k = blk.index(".else\n; W12-C")
    for name, br in (("WINCHK_B", "bmi cwslow"), ("WINCHK_W lastoff", "bne cwslow")):
        a = blk.index(f".macro {name}\n", k)
        b = blk.index(".endmacro", a)
        body = hp.norm(blk[a:b])[1:]            # drop the .macro line
        assert body == ["lda m_addr+1", "sta cwlk+1", "lda cwin_map", br], name


def restore_base(src: str) -> str:
    i, j = _block(src)
    src = src[:i] + BASE_WINCHK + src[j:]
    return "".join(ln for ln in src.splitlines(keepends=True)
                   if not re.match(r"^\s+WINCHK_SLOW_[BW]\b.*; W12-C", ln))


def test_memory_api_outside_winchk_is_the_base() -> None:
    fp = hp.fingerprints(ch.strip_w7t(restore_base(_src())))
    assert fp["memory_api"] == ch.BASE_MEMORY_API


def test_restore_catches_a_change_outside_the_block() -> None:
    src = _src()
    k = src.index("        MIO_PROG 1\n", src.index(".proc mem_write8"))
    mutated = src[:k] + "        nop\n" + src[k:]
    fp = hp.fingerprints(ch.strip_w7t(restore_base(mutated)))
    assert fp["memory_api"] != ch.BASE_MEMORY_API
