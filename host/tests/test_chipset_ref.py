"""Unit tests for host/chipset_ref.py (W7-M, S3.0).

Expected values are worked out by hand from the HRM / 8520 rules the model
claims (each test says which), not captured from the model.  The FS-UAE
comparison is test_chipset_ref_oracle.py.
"""
import pytest

from host import chipset_ref as cr


# ---- SET/CLR and Paula --------------------------------------------------------

def test_setclr():
    assert cr.setclr(0x0000, 0x8003) == 0x0003
    assert cr.setclr(0x0003, 0x0001) == 0x0002
    assert cr.setclr(0x7FFF, 0x7FFF) == 0x0000
    assert cr.setclr(0x0000, 0xFFFF) == 0x7FFF          # bit 15 never stored


def test_paula_level_priority_and_master():
    p = cr.Paula()
    p.write(0x09C, 0x8000 | 0x0020 | 0x0008)             # VERTB (3), PORTS (2)
    assert p.level() == 0                                # nothing enabled
    p.write(0x09A, 0x8000 | 0x0028)
    assert p.level() == 0                                # no INTEN master
    p.write(0x09A, 0xC000)
    assert p.level() == 3
    p.write(0x09A, 0x8000 | 0x2000)
    p.request(13)                                        # EXTER -> 6
    assert p.level() == 6
    assert p.level(mask=0x3FFF & ~(1 << 13)) == 3
    p.write(0x09C, 0x2020)
    assert p.level() == 2


@pytest.mark.parametrize("bit,lvl", [(0, 1), (1, 1), (2, 1), (3, 2), (4, 3), (5, 3), (6, 3),
                                     (7, 4), (8, 4), (9, 4), (10, 4), (11, 5), (12, 5), (13, 6)])
def test_paula_autovector_table(bit, lvl):
    p = cr.Paula()
    p.write(0x09A, 0xC000 | (1 << bit))
    p.request(bit)
    assert p.level() == lvl


def test_paula_dmaconr_bltpri_bzero_no_bbusy():
    p = cr.Paula()
    p.write(0x096, 0x8000 | 0x0400 | 0x0240)
    assert p.dmaconr() == 0x0640                         # BLTPRI shows (FS-UAE)
    p.bzero = True
    assert p.dmaconr() == 0x2640                         # BZERO, never BBUSY
    assert not p.write(0x040, 0x1234)                    # not a Paula register


# ---- CIA ------------------------------------------------------------------------

def cia():
    hits = []
    c = cr.CIA(lambda: hits.append(1))
    return c, hits


def test_cia_stopped_latch_write_loads_counter():
    c, _ = cia()
    c.write(4, 0x34)
    c.write(5, 0x12)
    assert (c.read(4), c.read(5), c.read(14)) == (0x34, 0x12, 0x00)


def test_cia_running_latch_write_does_not_load_force_load_does():
    c, _ = cia()
    c.write(14, 0x01)                                    # start continuous at $FFFF
    c.eclock(0x10)
    c.write(4, 0x34)
    c.write(5, 0x12)
    assert c.read(5) == 0xFF                              # counter untouched
    c.write(14, 0x10)                                    # LOAD strobe, stopped
    assert (c.read(4), c.read(5), c.read(14)) == (0x34, 0x12, 0x00)   # LOAD reads 0


def test_cia_oneshot_hi_write_starts_and_underflow_stops_reloads():
    c, hits = cia()
    c.write(14, 0x08)
    c.write(4, 0x05)
    c.write(5, 0x00)                                     # one-shot: the hi write starts it
    assert c.read(14) == 0x09
    c.eclock(5)                                          # 5 -> 0: no underflow yet
    assert c.read(4) == 0 and c.read(14) & 1
    c.eclock(1)                                          # period = latch + 1
    assert c.read(14) == 0x08 and c.read(4) == 5         # stopped, reloaded
    assert c.read(13) == 0x01 and c.read(13) == 0x00     # flag, then read-clear
    assert hits == []                                    # masked off: no /INT


def test_cia_continuous_underflow_count_and_phase():
    c, _ = cia()
    c.write(4, 9)
    c.write(5, 0)
    c.write(14, 0x01)
    c.eclock(25)                 # underflows at ticks 10 and 20, then 5 more ticks
    assert c.read(4) == 4 and c.read(14) == 0x01
    assert c.read(13) == 0x01


def test_cia_tb_counts_ta_underflows():
    c, _ = cia()
    c.write(6, 0x00)
    c.write(7, 0x01)                                     # TB = $0100
    c.write(15, 0x41)                                    # start, INMODE %10
    c.write(4, 1)
    c.write(5, 0)
    c.write(14, 0x01)                                    # TA period 2
    c.eclock(10)                                         # 5 TA underflows
    assert (c.read(6), c.read(7)) == (0xFB, 0x00)
    c.write(15, 0x01)                                    # INMODE %00: counts phi2
    c.eclock(3)
    assert c.read(6) == 0xF8


def test_cia_tb_cnt_mode_does_not_count():
    c, _ = cia()
    c.write(15, 0x21)                                    # INMODE %01: CNT edges (none)
    c.eclock(100)
    assert (c.read(6), c.read(7)) == (0xFF, 0xFF)


def test_cia_icr_mask_ir_and_int():
    c, hits = cia()
    c.write(13, 0x81)                                    # mask TA
    assert hits == []
    c.write(4, 0)
    c.write(5, 0)
    c.write(14, 0x09)                                    # one-shot latch 0, start
    c.eclock(1)
    assert hits                                          # /INT asserted
    assert c.read(13) == 0x81 and c.read(13) == 0x00
    c.write(13, 0x01)                                    # bit 7 = 0: clear TA only
    c.write(13, 0x82)                                    # set TB
    assert c.icr_mask == 0x02
    c.icr_data = 0x01                                    # a TA flag, unmasked
    assert c.read(13) == 0x01                            # no IR


def test_cia_mask_set_over_pending_flag_asserts_int():
    c, hits = cia()
    c.icr_data = 0x01
    c.write(13, 0x81)
    assert hits and c.read(13) == 0x81


def test_cia_tod_alarm_latch_halt():
    c, _ = cia()
    c.write(15, 0x80)                                    # alarm writes
    c.write(10, 0x00)
    c.write(9, 0x00)
    c.write(8, 0x03)
    c.write(15, 0x00)
    c.tod_tick(3)
    assert c.read(13) & 0x04                             # alarm at TOD == 3
    assert c.read(10) == 0                               # TODHI read latches ...
    c.tod_tick(1)
    assert c.read(8) == 3                                # ... until TODLO is read
    assert c.read(8) == 4
    c.write(10, 0x00)                                    # TODHI write halts ...
    c.tod_tick(5)
    assert c.read(8) == 4
    c.write(8, 0x10)                                     # ... until TODLO
    c.tod_tick(1)
    assert c.read(8) == 0x11


def test_cia_todmid_write_does_not_halt():
    c, _ = cia()
    c.write(9, 0x12)                                     # 8520: only TODHI halts
    c.tod_tick(2)
    assert (c.read(9), c.read(8)) == (0x12, 0x02)


def test_cia_latch0_underflows_every_tick_and_tb_counts_them():
    c, _ = cia()
    c.write(6, 0x10)
    c.write(7, 0x00)                                     # TB = $10
    c.write(15, 0x41)                                    # TB counts TA underflows
    c.write(4, 0)
    c.write(5, 0)
    c.write(14, 0x01)                                    # TA latch 0, continuous
    c.eclock(5)
    assert (c.read(4), c.read(5)) == (0, 0)             # never negative
    assert (c.read(6), c.read(7)) == (0x0B, 0x00)       # 5 underflows in 5 ticks


def test_cia_ports_ddr():
    c, _ = cia()
    c.pra_in = 0xEB
    c.write(2, 0x03)
    c.write(0, 0x02)
    assert c.read(0) == (0xEB & 0xFC) | 0x02


# ---- blitter ------------------------------------------------------------------------

def blitter(words_at=None):
    ram = cr.Ram(0x10000)
    for addr, ws in (words_at or {}).items():
        for i, v in enumerate(ws):
            ram.w16(addr + 2 * i, v)
    return ram, cr.Blitter(ram), [0] * 256


def setr(regs, **kw):
    offs = {"con0": 0x040, "con1": 0x042, "fwm": 0x044, "lwm": 0x046, "amod": 0x064,
            "bmod": 0x062, "cmod": 0x060, "dmod": 0x066, "adat": 0x074, "bdat": 0x072,
            "cdat": 0x070}
    ptrs = {"apt": 0x050, "bpt": 0x04C, "cpt": 0x048, "dpt": 0x054}
    for k, v in kw.items():
        if k in ptrs:
            regs[ptrs[k] >> 1], regs[(ptrs[k] + 2) >> 1] = v >> 16, v & 0xFFFF
        else:
            regs[offs[k] >> 1] = v & 0xFFFF


def words(ram, addr, n):
    return [ram.r16(addr + 2 * i) for i in range(n)]


def test_blit_ascending_shift_carries_old_word():
    ram, b, r = blitter({0x1000: [0x1234, 0x5678]})
    setr(r, con0=0x49F0, fwm=0xFFFF, lwm=0xFFFF, apt=0x1000, dpt=0x2000)
    assert b.blit(r, (1 << 6) | 2) == ("AD lf=F0 shift", 2)
    assert words(ram, 0x2000, 2) == [0x0123, 0x4567]


def test_blit_descending_shift_is_a_left_shift():
    ram, b, r = blitter({0x1000: [0x1234, 0x5678]})
    setr(r, con0=0x49F0, con1=0x0002, fwm=0xFFFF, lwm=0xFFFF, apt=0x1002, dpt=0x2002)
    b.blit(r, (1 << 6) | 2)
    assert words(ram, 0x2000, 2) == [0x2345, 0x6780]


def test_blit_masks_first_last_and_width1_both():
    ram, b, r = blitter({0x1000: [0x1234, 0x5678, 0xFFFF]})
    setr(r, con0=0x09F0, fwm=0xFF00, lwm=0x00FF, apt=0x1000, dpt=0x2000)
    b.blit(r, (1 << 6) | 2)
    assert words(ram, 0x2000, 2) == [0x1200, 0x0078]
    setr(r, apt=0x1004, dpt=0x3000, fwm=0x0FF0, lwm=0x3C3C)
    b.blit(r, (1 << 6) | 1)
    assert words(ram, 0x3000, 1) == [0x0C30]


def test_blit_minterm_ca_cookie_cut():
    ram, b, r = blitter({0x1000: [0xFF00], 0x1100: [0x0F0F], 0x1200: [0x3333]})
    setr(r, con0=0x0FCA, fwm=0xFFFF, lwm=0xFFFF, apt=0x1000, bpt=0x1100, cpt=0x1200,
         dpt=0x2000)
    b.blit(r, (1 << 6) | 1)
    assert words(ram, 0x2000, 1) == [0x0F33]             # A.B + ~A.C


def test_blit_b_not_masked_and_bdat_preload():
    ram, b, r = blitter({0x1000: [0xABCD]})
    setr(r, con0=0x05CC, fwm=0, lwm=0, bpt=0x1000, dpt=0x2000)
    b.blit(r, (1 << 6) | 1)
    assert words(ram, 0x2000, 1) == [0xABCD]
    setr(r, con0=0x01CC, dpt=0x2100)
    b.write_bdat(r, 0x1234)                              # CPU write, BSH 0
    b.blit(r, (2 << 6) | 2)
    assert words(ram, 0x2100, 4) == [0x1234] * 4


def test_blit_bdat_is_shifted_once_at_write():
    # B DMA off, BSH 5: the hold register is ($0000 << 16 | $F00F) >> 5 = $0780
    # in every word (FS-UAE b_off_bsh), not re-shifted with itself per word
    ram, b, r = blitter()
    setr(r, con0=0x01CC, con1=0x5000, dpt=0x2000)
    b.write_bdat(r, 0xF00F)
    b.blit(r, (2 << 6) | 3)
    assert words(ram, 0x2000, 6) == [0x0780] * 6


def test_blit_fetched_words_stay_in_the_data_registers():
    ram, b, r = blitter({0x1000: [0x1111, 0x86B5], 0x1100: [0x2222, 0x7257]})
    setr(r, con0=0x0BF0, fwm=0xFFFF, lwm=0xFFFF, apt=0x1000, cpt=0x1100, dpt=0x2000)
    b.blit(r, (1 << 6) | 2)
    assert (r[0x074 >> 1], r[0x070 >> 1]) == (0x86B5, 0x7257)
    setr(r, con0=0x01AA, dpt=0x3000)                    # C DMA off: D = C = the held word
    b.blit(r, (1 << 6) | 2)
    assert words(ram, 0x3000, 2) == [0x7257] * 2


@pytest.mark.parametrize("con1,want", [(0x000A, 0x07E0),    # IFE: edges kept
                                       (0x0012, 0x03E0),    # EFE: left edge dropped
                                       (0x001A, 0x03E0),    # both: exclusive (FS-UAE)
                                       (0x000E, 0xFC3F)])   # IFE + FCI: outside filled, edges kept
def test_blit_fill(con1, want):
    ram, b, r = blitter({0x1000: [0x0420]})
    setr(r, con0=0x09F0, con1=con1, fwm=0xFFFF, lwm=0xFFFF, apt=0x1000, dpt=0x2000)
    b.blit(r, (1 << 6) | 1)
    assert words(ram, 0x2000, 1) == [want]


def test_blit_fill_carry_across_words_resets_per_row():
    # rows of 2 words, descending (row 1 runs first).  Row 1's single edge
    # fills to its left end, across the word boundary; row 0 starts from
    # FCI again, so it stays empty.
    ram, b, r = blitter({0x1000: [0x0000, 0x0000, 0x0000, 0x0100]})
    setr(r, con0=0x09F0, con1=0x000A, fwm=0xFFFF, lwm=0xFFFF, apt=0x1006, dpt=0x2006)
    b.blit(r, (2 << 6) | 2)
    assert words(ram, 0x2000, 4) == [0x0000, 0x0000, 0xFFFF, 0xFF00]


def test_blit_modulo_pointer_writeback_and_zero():
    ram, b, r = blitter()
    setr(r, con0=0x0100, dpt=0x1000, dmod=4)
    b.blit(r, (1 << 6) | 2)
    assert (r[0x054 >> 1], r[0x056 >> 1]) == (0, 0x1008)  # after the modulo
    assert b.zero
    setr(r, con0=0x01FF)
    b.blit(r, (1 << 6) | 1)
    assert not b.zero and ram.r16(0x1008) == 0xFFFF


def test_blit_size_zero_means_max():
    ram, b, r = blitter()
    setr(r, con0=0x01FF, dpt=0x1000)
    assert b.blit(r, (1 << 6) | 0)[1] == 64
    assert b.blit(r, (0 << 6) | 1)[1] == 1024
    assert b.blit(r, (5 << 6) | 3, ecs=True)[1] == 15      # ECS: BLTSIZV/H


def test_blit_line_mode_is_only_counted():
    ram, b, r = blitter()
    setr(r, con0=0x0BCA, con1=0x0001, cpt=0x1000, dpt=0x1000)
    assert b.blit(r, (4 << 6) | 2) == ("line", 0)


# ---- copper -------------------------------------------------------------------------

def _coplist(ram, addr, words):
    for i, v in enumerate(words):
        ram.w16(addr + 2 * i, v)


def _copper(ram, cop1, copcon=0, **kw):
    regs = [0] * 256
    regs[0x080 >> 1], regs[0x082 >> 1] = cop1 >> 16, cop1 & 0xFFFF
    regs[0x02E >> 1] = copcon
    got = []

    def write(reg, val):
        regs[reg >> 1] = val
        got.append((reg, val))
    return cr.Copper(**kw).run(ram, regs, write), got


def test_copper_moves_jump_wait_end():
    ram = cr.Ram(0x10000)
    _coplist(ram, 0x1000, [0x0180, 0x0F00, 0x0084, 0x0000, 0x0086, 0x2000, 0x008A, 0x0000])
    _coplist(ram, 0x2000, [0x0182, 0x00F0, 0x2C01, 0xFFFE, 0x0184, 0x000F, 0xFFFF, 0xFFFE])
    n, got = _copper(ram, 0x1000)
    assert got == [(0x180, 0x0F00), (0x084, 0x0000), (0x086, 0x2000), (0x182, 0x00F0),
                   (0x184, 0x000F)]
    assert n == 8                                        # 3 MOVEs, JMP2, MOVE, WAIT, MOVE, END


@pytest.mark.parametrize("reg,copcon,runs", [
    (0x044, 0, False),      # OCS: $40-$7E needs CDANG (FS-UAE cop_blt_nodang)
    (0x044, 2, True),       # ... and runs with it (cop_blt_dang)
    (0x07E, 0, False),
    (0x080, 0, True),       # $80 and up: always
    (0x020, 2, False),      # below $40: never, even with CDANG (cop_low_dang)
    (0x024, 2, False),
])
def test_copper_danger_ocs(reg, copcon, runs):
    ram = cr.Ram(0x1000)
    _coplist(ram, 0, [reg, 0x1234, 0x0180, 0x0ABC, 0xFFFF, 0xFFFE])
    n, got = _copper(ram, 0, copcon)
    assert (got == [(reg, 0x1234), (0x180, 0x0ABC)]) if runs else (got == [] and n == 1)


def test_copper_skip_is_loud():
    ram = cr.Ram(0x1000)
    _coplist(ram, 0, [0x0001, 0xFFFF, 0x0180, 0x0ABC, 0xFFFF, 0xFFFE])
    with pytest.raises(cr.CopperSkipError):
        _copper(ram, 0)
    c = cr.Copper(strict=False)
    regs = [0] * 256
    assert c.run(ram, regs, lambda r, v: None) == 3 and c.skips == 1


def test_copper_limit():
    ram = cr.Ram(0x1000)                                 # all MOVE $180 ... (legal)
    for a in range(0, 0x1000, 4):
        ram.w16(a, 0x0180)
    assert cr.Copper().run(ram, [0] * 256, lambda r, v: None, limit=10) == 10


# ---- Chipset wiring -----------------------------------------------------------------

def test_chipset_fixed_reads_and_beam():
    c = cr.Chipset(cr.Ram(0x1000))
    assert c.custom_read(0x018) == 0x3000
    assert c.custom_read(0x016) == 0xFF00
    assert c.custom_read(0x07C) == 0xFFFF
    assert c.custom_read(0x180) == 0                     # write-only: 0
    assert c.ciaa.read(0) == 0xEB                        # CIA-A PRA inputs, DDRA 0
    c.cycles = 300 * cr.LINE_CYC + 100
    assert c.custom_read(0x004) == 0x8001                # LOF | V8
    assert c.custom_read(0x006) == (300 & 0xFF) << 8 | 50


def test_chipset_serdat_blit_and_intreq_reraise():
    c = cr.Chipset(cr.Ram(0x10000))
    c.custom_write(0x030, 0x0141)
    assert c.serial == b"A" and c.paula.intreq & 1       # TBE
    c.custom_write(0x040, 0x0100)
    c.custom_write(0x054, 0)
    c.custom_write(0x056, 0x100)
    c.custom_write(0x058, (1 << 6) | 1)
    assert c.paula.intreq & 0x40 and c.custom_read(0x002) & 0x2000   # BLIT, BZERO
    c.ciab.write(13, 0x81)
    c.ciab.icr_data = 1
    c.ciab.update()
    assert c.paula.intreq & 0x2000
    c.custom_write(0x09C, 0x2000)                        # /INT still low: re-raised
    assert c.paula.intreq & 0x2000
    c.ciab.read(13)
    c.custom_write(0x09C, 0x2000)
    assert not c.paula.intreq & 0x2000
    c.custom_write(0x09A, 0xE000)
    c.ciab.icr_data = 1
    c.ciab.update()
    assert c.ipl == 6


@pytest.mark.parametrize("addr,want", [(0xBFE001, ("A", 0)), (0xBFEC01, ("A", 12)),
                                       (0xBFD000, ("B", 0)), (0xBFDF00, ("B", 15)),
                                       (0xBFE000, None), (0xBFD001, None),
                                       (0xBFF001, None), (0xBFC000, ("B", 0))])
def test_cia_select(addr, want):
    assert cr.Chipset.cia_select(addr) == want


def test_run_copper_needs_dmaen_and_copen():
    ram = cr.Ram(0x1000)
    ram.w16(0, 0x0180)
    ram.w16(2, 0x0ABC)
    ram.w16(4, 0xFFFF)
    ram.w16(6, 0xFFFE)
    c = cr.Chipset(ram)
    assert c.run_copper() == 0
    c.custom_write(0x096, 0x8280)
    assert c.run_copper() == 2 and c.regs[0x180 >> 1] == 0x0ABC
