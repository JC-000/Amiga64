"""rescheck: `@under` regions, i.e. claims on the RAM under the I/O window (W7-V).

The VIC-II reads RAM under $D000-$DFFF while the CPU sees I/O there, so a
claim on that RAM must not be forced to split the `sys` I/O region (whose
register equates such as VIC_RASTER stay where they are).  An `@under`
region may overlap `sys` only: it must lie inside ONE sys region, never
overlap another `@under` region, and every equate under it must carry its
owner's prefix (so an I/O register equate cannot be moved into it).
Ordinary `@region` claims overlapping sys stay errors, and so do raw
literals into that range (the scan still sees the sys region).
"""

from __future__ import annotations

from pathlib import Path

from host import rescheck

ROOT = Path(__file__).resolve().parents[2]

HEAD = """; @space ram $0100-$FFFF
; @region free $0100-$CFFF free
; @region sys $D000-$DEFF I/O
VIC_RASTER = $D012
"""
TAIL = """; @region sys $DF00-$DFFF expansion I/O
; @region free $E000-$FFFF free
"""


def _errors(body: str) -> list[str]:
    space, errs = rescheck.parse_rc_text(HEAD + body + TAIL, "ram.inc")
    return errs + rescheck.check_space(space)


def test_real_ram_inc_has_screen_under_io():
    spaces, errs = rescheck.load_spaces(ROOT)
    assert not errs
    ram = spaces["ram"]
    under = [r for r in ram.under if r.owner == "c2v"]
    assert [(r.lo, r.hi) for r in under] == [(0xD000, 0xD3FF)]
    scr = next(e for e in ram.equates if e.name == "c2v_screen")
    assert scr.value == 0xD000 and scr.region is under[0]
    raster = next(e for e in ram.equates if e.name == "VIC_RASTER")
    assert raster.region.owner == "sys"
    assert not rescheck.check_space(ram)


def test_under_io_claim_accepted():
    body = ("; @under c2v $D000-$D3FF screen RAM under I/O\n"
            "c2v_screen = $D000\n")
    assert _errors(body) == []
    space, _ = rescheck.parse_rc_text(HEAD + body + TAIL, "ram.inc")
    assert [r.owner for r in space.under] == ["c2v"]
    assert space.region_at(0xD000).owner == "sys"     # the scan still sees I/O


def test_teeth_plain_region_over_io_rejected():
    errs = _errors("; @region c2v $D000-$D3FF screen RAM\nc2v_screen = $D000\n")
    assert any("overlaps" in e for e in errs)


def test_teeth_io_equate_moved_under_rejected():
    body = ("; @under c2v $D000-$D3FF screen RAM under I/O\n"
            "c2v_screen = $D000\n"
            "VIC_BORDER = $D020\n")
    assert any("VIC_BORDER" in e and "prefix" in e for e in _errors(body))


def test_teeth_under_outside_io_rejected():
    body = ("; @under c2v $D000-$D3FF screen RAM under I/O\n"
            "; @under c2v $E000-$E3FF not I/O\n")
    assert any("$E000-$E3FF" in e and "sys" in e for e in _errors(body))


def test_teeth_under_spanning_two_sys_regions_rejected():
    assert any("sys" in e for e in _errors("; @under c2v $DE00-$DF7F spans\n"))


def test_teeth_under_overlapping_under_rejected():
    body = ("; @under c2v $D000-$D3FF a\n"
            "; @under hle $D300-$D5FF b\n")
    assert any("overlaps" in e for e in _errors(body))


def test_teeth_under_reserved_owner_rejected():
    assert any("owner" in e for e in _errors("; @under free $D000-$D3FF x\n"))


def test_teeth_raw_literal_under_io_still_violation():
    spaces, _ = rescheck.load_spaces(ROOT)
    findings = rescheck.scan_source(
        "runtime/ops/c2v.s", "        sta $D000,x\n", "c2v", spaces)
    assert any(f.fatal and "'sys'" in f.message for f in findings)


# --- fix round 1 (review F1): @under only in space ram, only in $D000-$DFFF,
# only for a registered module owner ------------------------------------------

ABUSE_HEAD = """; @space ram $0100-$FFFF
; @region sys $0100-$01FF stack
; @region sys $0200-$03FF KERNAL workspace
; @region free $0400-$CFFF free
; @region sys $D000-$DEFF I/O
VIC_RASTER = $D012
"""


def _abuse(body: str) -> list[str]:
    space, errs = rescheck.parse_rc_text(ABUSE_HEAD + body + TAIL, "ram.inc")
    return errs + rescheck.check_space(space)


def test_f1_stack_under_rejected():
    assert _abuse("; @under c2v $0100-$01FF my stack\nc2v_stk = $0100\n")


def test_f1_kernal_vectors_under_rejected():
    assert _abuse("; @under hle $0300-$03FF vectors\nhle_vec = $0314\n")


def test_f1_owner_vic_cannot_hide_io_equates():
    errs = _abuse("; @under VIC $D000-$D3FF x\nVIC_BORDER = $D020\nVIC_BG = $D021\n")
    assert any("owner" in e and "VIC" in e for e in errs)


def test_f1_unregistered_owner_rejected():
    assert any("owner" in e for e in
               _abuse("; @under nosuch $D000-$D3FF x\nnosuch_a = $D000\n"))


def test_f1_guest_vector_page_under_rejected():
    text = ("; @space guest $000000-$FFFFFF\n; @region sys $000000-$0003FF vectors\n"
            "; @region free $000400-$FFFFFF free\n"
            "; @under hle $000000-$0003FF vec page\nhle_x = $000004\n")
    space, errs = rescheck.parse_rc_text(text, "guest.inc")
    assert any("space" in e for e in errs + rescheck.check_space(space))


def test_f1_zp_cpu_port_under_rejected():
    text = ("; @space zp $00-$FF\n; @region sys $00-$01 port\n; @region free $02-$FF f\n"
            "; @under c2v $00-$01 port\nc2v_port = $01\n")
    space, errs = rescheck.parse_rc_text(text, "zp.inc")
    assert any("space" in e for e in errs + rescheck.check_space(space))


def test_f1_window_is_d000_dfff_only():
    ok = _abuse("; @under c2v $D400-$D7FF inside the window\n")
    assert ok == []
    assert rescheck.UNDER_WINDOWS == {"ram": ((0xD000, 0xDFFF),)}
