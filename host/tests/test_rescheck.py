"""Tests for host/rescheck.py — the resource-contract checker.

Two halves:
  1. the real tree must be green (the checker gates `make rescheck` / CI);
  2. NEGATIVE legs: synthetic rc content with an overlap, an out-of-bounds
     region, an equate outside its region, an undeclared gap, and a
     foreign/undeclared literal must each FAIL.  The c64-lib-contract
     lesson (its spec records ld65 failing silently in several modes):
     a guard that has not been proven to fire is not a guard.
"""

from pathlib import Path

from host import rescheck

ROOT = Path(__file__).resolve().parents[2]


# --- 1. the real tree -------------------------------------------------------

def test_real_tree_is_green(capsys):
    assert rescheck.run_checks(ROOT) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "VIOLATION" not in out


def test_real_tree_parses_all_three_spaces():
    spaces, errors = rescheck.load_spaces(ROOT)
    assert errors == []
    assert set(spaces) == {"zp", "ram", "guest"}
    # spot-check values the rest of the repo depends on
    zp = {e.name: e.value for e in spaces["zp"].equates}
    assert zp["r_d0"] == 0x10 and zp["st_halt"] == 0x78
    ram = {e.name: e.value for e in spaces["ram"].equates}
    assert ram["buf_case"] == 0xC000 and ram["buf_result"] == 0xC800
    guest = {e.name: e.value for e in spaces["guest"].equates}
    assert guest["STACK_TOP"] == 0xF000 and guest["PROG_BASE"] == 0x10000


# --- 2a. rc-file negatives --------------------------------------------------

def _space(text):
    space, errors = rescheck.parse_rc_text(text, "synthetic.inc")
    assert space is not None and errors == []
    return space


def test_overlap_fails():
    space = _space(
        "; @space zp $00-$FF\n"
        "; @region core $10-$7F register file\n"
        "; @region g6 $70-$8F scratch grab\n"      # overlaps core $10-$7F
        "; @region free $90-$FF rest\n"
    )
    errors = rescheck.check_space(space)
    assert any("overlaps" in e for e in errors)


def test_out_of_bounds_region_fails():
    space = _space(
        "; @space zp $00-$FF\n"
        "; @region core $00-$FF everything\n"
        "; @region run $100-$10F outside the space\n"
    )
    errors = rescheck.check_space(space)
    assert any("outside" in e and "space" in e for e in errors)


def test_equate_outside_its_region_fails():
    space = _space(
        "; @space zp $00-$FF\n"
        "; @region core $10-$1F small region\n"
        "bad_slot = $40\n"
        "; @region free $00-$0F head\n"
        "; @region free $20-$FF tail\n"
    )
    errors = rescheck.check_space(space)
    assert any("bad_slot" in e and "outside its" in e for e in errors)


def test_undeclared_gap_fails():
    space = _space(
        "; @space zp $00-$FF\n"
        "; @region core $00-$7F low half\n"
        # $80-$FF never declared, not even as free
    )
    errors = rescheck.check_space(space)
    assert any("undeclared gap" in e for e in errors)


def test_clean_synthetic_space_passes():
    space = _space(
        "; @space zp $00-$FF\n"
        "; @region core $00-$7F low half\n"
        "slot = $10\n"
        "; @region free $80-$FF rest\n"
    )
    assert rescheck.check_space(space) == []


# --- 2b. source-scan negatives ----------------------------------------------

def _spaces():
    spaces, errors = rescheck.load_spaces(ROOT)
    assert errors == []
    return spaces


def test_foreign_zp_literal_fails():
    # a group handler poking core's ZP register file by raw address
    findings = rescheck.scan_source(
        "runtime/ops/g3_logic.s", "        sta $78\n", "g3", _spaces())
    assert any(f.fatal and "'core'" in f.message for f in findings)


def test_foreign_absolute_literal_fails():
    # a group handler writing into the runner's case buffer
    findings = rescheck.scan_source(
        "runtime/ops/g1_move.s", "        sta $C000,x\n", "g1", _spaces())
    assert any(f.fatal and "'run'" in f.message for f in findings)


def test_free_region_literal_fails():
    # unallocated space is not claimable by writing to it
    findings = rescheck.scan_source(
        "runtime/core.s", "        sta $A000\n", "core", _spaces())
    assert any(f.fatal and "'free'" in f.message for f in findings)


def test_mailbox_page_literal_fails():
    # the $CE00 hardware-harness mailbox page is reserved
    findings = rescheck.scan_source(
        "tests/runner/runner.s", "        sta $CE00\n", "run", _spaces())
    assert any(f.fatal and "'mb'" in f.message for f in findings)


def test_own_region_literal_passes():
    findings = rescheck.scan_source(
        "runtime/core.s", "@lp:    sta $10,x\n", "core", _spaces())
    assert findings == []


def test_immediate_and_offset_literals_are_skipped():
    src = (
        "        lda #$78\n"              # immediate, not an address
        "        cmp #$0A\n"
        "FLAG = other + $7FF\n"           # offset arithmetic
    )
    findings = rescheck.scan_source(
        "runtime/ops/g2_arith.s", src, "g2", _spaces())
    assert findings == []


def test_small_value_equate_is_unclassified_not_fatal():
    findings = rescheck.scan_source(
        "runtime/ops/g4_shift.s", "MYSTERY = $42\n", "g4", _spaces())
    assert len(findings) == 1
    assert not findings[0].fatal
    assert "cannot classify" in findings[0].message


def test_zp_equate_with_memory_use_fails_for_foreign_owner():
    src = (
        "sneaky = $02\n"                  # run-owned harness scratch
        "        sta sneaky\n"
    )
    findings = rescheck.scan_source(
        "runtime/ops/g5_flow.s", src, "g5", _spaces())
    assert any(f.fatal and "'run'" in f.message for f in findings)


def test_allowlist_suppresses_a_finding(monkeypatch):
    monkeypatch.setattr(rescheck, "ALLOWLIST", [
        ("runtime/ops/g1_move.s", r"sta \$C000", "test-only entry"),
    ])
    findings = rescheck.scan_source(
        "runtime/ops/g1_move.s", "        sta $C000\n", "g1", _spaces())
    assert findings == []


# --- 2c. host-mirror negative -----------------------------------------------

def test_host_mirror_mismatch_fails(tmp_path):
    (tmp_path / "runtime/rc").mkdir(parents=True)
    (tmp_path / "host").mkdir()
    for name, rel in rescheck.RC_FILES.items():
        src = (ROOT / rel).read_text()
        if name == "guest":
            src = src.replace("DOS_BASE  = $0F0C00", "DOS_BASE  = $0F0C80")
        (tmp_path / rel).write_text(src)
    (tmp_path / "host/hle_env.py").write_text(
        (ROOT / "host/hle_env.py").read_text())
    (tmp_path / "host/image.py").write_text(
        (ROOT / "host/image.py").read_text())
    assert rescheck.run_checks(tmp_path) == 1


def test_missing_rc_file_fails(tmp_path):
    assert rescheck.run_checks(tmp_path) == 1


def test_unregistered_source_fails(tmp_path):
    """A runtime .s file with no registered owner is a violation."""
    import shutil
    from host import rescheck
    root = tmp_path / "repo"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(
        "build", ".git", ".claude", "__pycache__", "*.o"))
    (root / "runtime/ops/zz_new.s").write_text("; empty\n")
    assert rescheck.run_checks(root) == 1


def test_hle_family_globs_are_scanned():
    from host import rescheck
    owners, unregistered = rescheck.source_owners(ROOT)
    assert owners["runtime/ops/hle_dos.s"] == "hle"
    assert unregistered == []


def test_value_tag_exempts_plain_constants():
    from host import rescheck
    spaces, _ = rescheck.load_spaces(ROOT)
    src = "SIZE = $1000  ; @value\n        lda #<SIZE\n"
    assert rescheck.scan_source("x.s", src, "hle", spaces) == []
    # untagged, the same literal is judged as an address in 'core' RAM
    bad = rescheck.scan_source("x.s", "SIZE = $1000\n", "hle", spaces)
    assert any(f.fatal for f in bad)


def test_value_tag_cannot_hide_a_memory_operand():
    from host import rescheck
    spaces, _ = rescheck.load_spaces(ROOT)
    src = "PTR = $1000  ; @value\n        lda PTR\n"
    found = rescheck.scan_source("x.s", src, "hle", spaces)
    assert any(f.fatal and "@value" in f.message for f in found)
