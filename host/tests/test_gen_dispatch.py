"""Unit tests for host/gen_dispatch.py (dispatch table generator)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gen_dispatch  # noqa: E402


ALL_HANDLERS = sorted({label for _, _, label in gen_dispatch.FAMILY_MAP})


# --- find_exports ----------------------------------------------------------

def test_find_exports_basic(tmp_path):
    src = tmp_path / "a.s"
    src.write_text(".export fam_move\n.export op_illegal\n")
    assert gen_dispatch.find_exports([src]) == {"fam_move", "op_illegal"}


def test_find_exports_comma_list_and_comments(tmp_path):
    src = tmp_path / "a.s"
    src.write_text(
        "  .export fam_0, fam_b ; trailing comment\n"
        "; .export fam_move  <- commented out, must be ignored\n"
        "\t.EXPORT fam_c\n"
        ".exportzp r_d0, r_pc\n"
        ".export buf_case = $C000\n"
    )
    exports = gen_dispatch.find_exports([src])
    assert exports == {"fam_0", "fam_b", "fam_c", "r_d0", "r_pc", "buf_case"}
    assert "fam_move" not in exports


def test_find_exports_missing_file_is_tolerated(tmp_path):
    assert gen_dispatch.find_exports([tmp_path / "nope.s"]) == set()


# --- build_table -----------------------------------------------------------

def test_table_covers_all_256_pages():
    table = gen_dispatch.build_table()
    assert len(table) == 256
    assert all(isinstance(e, str) and e for e in table)


@pytest.mark.parametrize(
    "op_hi,label",
    [
        (0x00, "fam_0"),
        (0x0F, "fam_0"),
        (0x10, "fam_move"),
        (0x3F, "fam_move"),
        (0x40, "fam_4"),
        (0x4F, "fam_4"),
        (0x50, "fam_5"),
        (0x60, "fam_bcc"),
        (0x70, "fam_moveq"),
        (0x7F, "fam_moveq"),
        (0x80, "fam_8"),
        (0x90, "fam_sub"),
        (0xA0, "op_aline"),
        (0xAF, "op_aline"),
        (0xB0, "fam_b"),
        (0xC0, "fam_c"),
        (0xD0, "fam_add"),
        (0xE0, "fam_shift"),
        (0xEF, "fam_shift"),
        (0xF0, "op_fline"),
        (0xFF, "op_fline"),
    ],
)
def test_table_family_boundaries(op_hi, label):
    assert gen_dispatch.build_table()[op_hi] == label


# --- generate --------------------------------------------------------------

def test_generate_imports_exported_handlers_only():
    out = gen_dispatch.generate({"op_illegal", "fam_move", "fam_moveq"})
    lines = out.splitlines()
    assert ".import fam_move" in lines
    assert ".import fam_moveq" in lines
    assert ".import op_illegal" in lines
    # Handlers not exported anywhere must NOT be imported...
    assert ".import fam_0" not in lines
    # ...they get local (non-exported) stubs jumping to op_illegal instead.
    assert "fam_0:" in lines
    stub_idx = lines.index("fam_0:")
    assert "jmp op_illegal" in lines[stub_idx + 1]
    # Exported handlers must not be stubbed.
    assert "fam_move:" not in lines
    # Stubs are never exported (a later real .export must not collide).
    assert not any(".export fam_0" in ln for ln in lines)


def test_generate_stub_for_every_missing_handler():
    out = gen_dispatch.generate(set())
    for handler in ALL_HANDLERS:
        assert f"{handler}:" in out.splitlines(), handler
    # Without op_illegal anywhere, a self-contained fallback is emitted.
    assert "op_illegal:" in out


def test_generate_table_has_256_lo_and_hi_entries():
    out = gen_dispatch.generate({"op_illegal"})
    assert out.count("<(") == 256
    assert out.count(">(") == 256
    assert "dispatch_lo:" in out
    assert "dispatch_hi:" in out
    assert ".export dispatch_lo, dispatch_hi" in out


def test_generate_is_deterministic():
    exported = {"op_illegal", "fam_b", "fam_shift"}
    assert gen_dispatch.generate(exported) == gen_dispatch.generate(exported)


# --- main() end-to-end with a fake runtime tree ----------------------------

def test_main_end_to_end(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "ops").mkdir(parents=True)
    (runtime / "core.s").write_text(
        ".export op_aline, op_fline, op_illegal\n"
        ".export emu_step\n"
    )
    (runtime / "ops" / "g1_move.s").write_text(
        ".export fam_move\n.export fam_moveq\n"
    )
    out = tmp_path / "gen" / "dispatch.s"

    rc = gen_dispatch.main(["-o", str(out), "--runtime-dir", str(runtime)])
    assert rc == 0

    text = out.read_text()
    lines = text.splitlines()
    # Real handlers imported.
    for h in ("fam_move", "fam_moveq", "op_aline", "op_fline"):
        assert f".import {h}" in lines, h
    # Unlanded families stubbed, not imported.
    for h in ("fam_0", "fam_4", "fam_5", "fam_bcc", "fam_8", "fam_sub",
              "fam_b", "fam_c", "fam_add", "fam_shift"):
        assert f".import {h}" not in lines, h
        assert f"{h}:" in lines, h
    # Full table present.
    assert text.count("<(") == 256
    assert text.count(">(") == 256


def test_main_scans_core_even_without_ops_dir(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "core.s").write_text(".export op_illegal, fam_4\n")
    out = tmp_path / "dispatch.s"
    assert gen_dispatch.main(["-o", str(out), "--runtime-dir", str(runtime)]) == 0
    lines = out.read_text().splitlines()
    assert ".import fam_4" in lines
    assert "fam_4:" not in lines
