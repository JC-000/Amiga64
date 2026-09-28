"""Floppy drive, disk DMA and the HLE trackdisk hook for aros_boot_model.py.

WB-measure (docs/WB_BOOT.md, task 2).  Two ways to serve DF0: from an ADF:

* **Disk DMA** (``Floppy`` + ``mfm_track``): the model's chipset answers the
  drive lines on CIA-A PRA / CIA-B PRB and completes a DSKLEN read at once:
  the track is MFM-encoded on the host straight from the ADF and written to
  DSKPT, then INTREQ DSKBLK is raised.  The unmodified AROS trackdisk.device
  (arch/m68k-amiga/devs/trackdisk) then decodes it with the CPU and the
  blitter, so its instructions are counted as they would run on an A500.
  There is no bit-cell timing, no index pulse and no rotational delay.
* **HLE trackdisk** (``TrackdiskHLE``): the smallest hook that removes MFM
  from the guest.  trackdisk.device's BeginIO (its LVO -30 JMP target, found
  in SysBase->DeviceList at run time) is trapped; a quick (DoIO) CMD_READ /
  ETD_READ copies io_Length bytes of the ADF at io_Offset to io_Data, sets
  io_Actual / io_Error and returns (RTS).  Everything else, and any
  non-quick read, takes the normal path, and is counted.  On the C64 this is
  one REU DMA from the staged image; here it is a host copy.

Structure offsets are the public AmigaOS ones (exec/io.h, exec/nodes.h).
"""
from __future__ import annotations

import struct

from adf import AdfImage, SECTORS, BLOCK

DSK_WORDS_DEFAULT = 13630 // 2      # AROS DISK_BUFFERSIZE / 2 (trackdisk_hw.h)
TRACK_WORDS = 6400                  # one DD revolution at 2 us/bit cell ~ 6250-6400 words
SYNC = 0x4489

CMD_READ, ETD_READ = 2, 0x8002
IOF_QUICK = 1
IO_UNIT, IO_COMMAND, IO_FLAGS, IO_ERROR = 24, 28, 30, 31
IO_ACTUAL, IO_LENGTH, IO_DATA, IO_OFFSET = 32, 36, 40, 44


def _odd_even(longs):
    """AmigaDOS sector encoding: all odd bits, then all even bits (data only)."""
    return ([(v >> 1) & 0x55555555 for v in longs] +
            [v & 0x55555555 for v in longs])


def _words(longs):
    out = []
    for v in longs:
        out += [v >> 16, v & 0xFFFF]
    return out


def _clock(words, prev):
    """Insert MFM clock bits: a clock is 1 iff both neighbouring data bits
    are 0.  *prev* is the previous word (its bit 0 precedes bit 15)."""
    out = []
    for w in words:
        d = w & 0x5555
        clk = ~((d << 1) | (d >> 1) | ((prev & 1) << 15)) & 0xAAAA
        out.append(d | clk)
        prev = d
    return out


def mfm_sector(track: int, sector: int, data: bytes, prev: int = 0) -> list[int]:
    """544 words: 2 gap words ($00 bytes), 2 sync words, info, label,
    checksums, data.  *prev* is the word before it on the track."""
    info = (0xFF << 24) | (track << 16) | (sector << 8) | (SECTORS - sector)
    hdr = _odd_even([info]) + _odd_even([0, 0, 0, 0])
    hchk = 0
    for v in hdr:
        hchk ^= v
    dl = list(struct.unpack(">128I", data))
    body = _odd_even(dl)
    dchk = 0
    for v in body:
        dchk ^= v
    words = _clock([0, 0], prev) + [SYNC, SYNC]
    words += _clock(_words(hdr + _odd_even([hchk]) + _odd_even([dchk]) + body), SYNC)
    return words


def mfm_track(adf: AdfImage, track: int, nwords: int) -> bytes:
    """What a WORDSYNC read of *nwords* words returns: the stream starts
    right after sector 0's first sync word, runs through the 11 sectors and
    the track gap, and wraps (a DMA longer than one revolution repeats)."""
    raw = adf.track(track)
    rev = []
    for s in range(SECTORS):
        rev += mfm_sector(track, s, raw[s * BLOCK:(s + 1) * BLOCK], rev[-1] if rev else 0)
    rev += _clock([0] * (TRACK_WORDS - len(rev)), rev[-1])
    start = 3                                  # after gap, gap, first SYNC
    out = [rev[(start + i) % len(rev)] for i in range(nwords)]
    return struct.pack(f">{nwords}H", *out)


class Floppy:
    """DF0: with a disk.  CIA-B PRB drives it; CIA-A PRA bits 2-5 read it."""

    def __init__(self, adf: AdfImage | None, write_protect: bool = True):
        self.adf = adf
        self.wp = write_protect
        self.prb = 0xFF
        self.selected = False
        self.motor = False
        self.cyl = 0
        self.head = 0
        self.changed = False          # /CHNG high once a step saw the disk
        self.steps = 0
        self.reads = []               # (icount, track) per DMA read
        self.writes = 0

    def write_prb(self, v: int) -> None:
        old, self.prb = self.prb, v
        sel = not v & 0x08
        if sel and old & 0x08:                       # /SEL0 falling edge:
            self.motor = not v & 0x80                # the motor bit is latched
        self.selected = sel
        self.head = 0 if v & 0x04 else 1
        if sel and not old & 0x01 and v & 0x01:     # /STEP rising edge
            self.steps += 1
            if v & 0x02:
                self.cyl = max(0, self.cyl - 1)
            else:
                self.cyl = min(83, self.cyl + 1)
            if self.adf is not None:
                self.changed = True

    def pra_bits(self) -> int:
        """CIA-A PRA inputs bits 2-5 (/CHNG /WPRO /TK0 /RDY)."""
        if not self.selected:
            return 0x3C
        v = 0
        if not self.motor:
            v |= 0x20                                # /RDY high: not ready
        if self.cyl != 0:
            v |= 0x10                                # /TK0 low at cylinder 0
        if not (self.wp and self.adf is not None):
            v |= 0x08                                # /WPRO low = protected
        if self.changed:
            v |= 0x04                                # /CHNG high = disk present
        return v

    @property
    def track(self) -> int:
        return self.cyl * 2 + self.head


class TrackdiskHLE:
    """Trap trackdisk.device BeginIO and serve quick reads from the ADF."""

    def __init__(self, model, adf: AdfImage):
        self.m, self.adf = model, adf
        self.beginio = None
        self.base = None
        self.served = 0
        self.bytes = 0
        self.passed = {}              # (command, quick) -> count

    def locate(self) -> bool:
        base = self.m.find_node(350, "trackdisk.device")    # SysBase->DeviceList
        if not base:
            return False
        mem = self.m.mem
        if mem.r16(base - 30) != 0x4EF9:
            return False
        self.base = base
        self.beginio = mem.r32(base - 28) & 0xFFFFFF
        return True

    def trap(self) -> bool:
        """Called with PC at BeginIO.  True if the request was served."""
        cpu, mem = self.m.cpu, self.m.mem
        io = cpu.r_reg(9) & 0xFFFFFF                          # A1
        cmd = mem.r16(io + IO_COMMAND)
        quick = mem.r8(io + IO_FLAGS) & IOF_QUICK
        if cmd not in (CMD_READ, ETD_READ) or not quick:
            k = f"cmd{cmd:#x}{'' if quick else ' (not quick)'}"
            self.passed[k] = self.passed.get(k, 0) + 1
            return False
        off, n = mem.r32(io + IO_OFFSET), mem.r32(io + IO_LENGTH)
        data = mem.r32(io + IO_DATA) & 0xFFFFFF
        chunk = self.adf.data[off:off + n]
        wb = self.m.wb
        wb.mark("disk_first_read")
        if off < 1024 and "bootblock" not in wb.markers:
            wb.bb_pending = True
        mem.w_block(data, chunk)
        mem.w32(io + IO_ACTUAL, len(chunk))
        mem.w8(io + IO_ERROR, 0 if len(chunk) == n else 0xFC)  # TDERR_NotSpecified-ish
        sp = cpu.r_reg(15)
        cpu.w_pc(mem.r32(sp) & 0xFFFFFF)                        # RTS
        cpu.w_reg(15, sp + 4)
        self.served += 1
        self.bytes += len(chunk)
        return True


# ---- boot milestones and attribution ----------------------------------------

EXEC_THISTASK, EXEC_DEVLIST, EXEC_LIBLIST = 276, 350, 378
EXEC_TASKREADY, EXEC_TASKWAIT = 406, 420
NT_PROCESS = 13
PR_TASKNUM, PR_CLI, CLI_RETURNCODE, CLI_COMMANDNAME = 140, 172, 12, 16
IB_FIRSTSCREEN, SC_TITLE, SC_FIRSTWINDOW, WIN_TITLE = 60, 22, 4, 32


class WbTracker:
    """Milestones, per-task/per-command instruction counts and phase
    snapshots for a floppy boot.  All guest structures are read through the
    model's flat memory; nothing here changes guest state."""

    MARKERS = ("disk_first_read", "bootblock", "first_command", "loadwb",
               "wb_window", "idle")
    # idle = the start of the first window of stop_after_idle virtual
    # seconds after LoadWB with no disk read and >= idle_frac of the time
    # STOPped.  AROS's Workbook and input.device keep ~20% busy afterwards
    # (docs/WB_BOOT.md), so 0.7 rather than "fully idle".

    def __init__(self, model, adf: AdfImage):
        self.m, self.adf = model, adf
        self.bootcode = adf.data[12:76]
        self.bb_first = struct.unpack(">I", adf.data[12:16])[0]
        self.bb_pending = False
        self.markers: dict[str, dict] = {}
        self.task_counts: dict[str, int] = {}
        self.key = "(reset)"
        self.proc_cmd: dict[int, str] = {}
        self.pending: dict[int, str] = {}
        self.commands: list[tuple] = []          # (icount, virtual s, process, command)
        self.tasks_seen: dict[str, int] = {}
        self.screens: list[tuple] = []           # (icount, virtual s, titles)
        self.last_titles = None
        self.disk_ops = 0
        self.tracks_read: list[int] = []
        self.idle_hist: list[tuple] = []         # (cycles, idle cycles, disk ops) per 0.5 s
        self.done = False
        self.stop_after_idle = 3.0
        self.idle_frac = 0.7

    # -- guest memory helpers
    def sysbase(self) -> int | None:
        mem = self.m.mem
        sb = mem.r32(4)
        if sb & 1 or not 0x400 <= sb < 0x1000000:
            return None
        if mem.r32(sb + 38) != (~sb) & 0xFFFFFFFF:      # ChkBase
            return None
        return sb

    def cstr(self, a: int, n: int = 40) -> str:
        if not a or a & 0xFF000000 or a >= 0x1000000:
            return ""
        b = bytes(self.m.mem.r_block(a, n))
        return b.split(b"\0")[0].decode("latin-1")

    def bstr(self, bptr: int) -> str:
        a = (bptr << 2) & 0xFFFFFFFF
        if not a or a >= 0x1000000:
            return ""
        n = self.m.mem.r8(a)
        return bytes(self.m.mem.r_block(a + 1, n)).decode("latin-1") if n else ""

    def walk(self, lh: int, limit: int = 200):
        mem = self.m.mem
        n = mem.r32(lh)
        while n and mem.r32(n) and limit and n < 0x1000000:
            yield n
            n = mem.r32(n)
            limit -= 1

    def find_node(self, list_off: int, name: str) -> int | None:
        sb = self.sysbase()
        if sb is None:
            return None
        for n in self.walk(sb + list_off):
            if self.cstr(self.m.mem.r32(n + 10)) == name:
                return n
        return None

    # -- per slice / per frame
    def task_key(self) -> str:
        sb = self.sysbase()
        if sb is None:
            return "(boot, no ExecBase)"
        mem = self.m.mem
        tt = mem.r32(sb + EXEC_THISTASK)
        if not tt or tt >= 0x1000000:
            return "(no task)"
        if mem.r8(tt + 8) == NT_PROCESS:
            cli = mem.r32(tt + PR_CLI)
            if cli and (cli << 2) < 0x1000000:
                # AROS renames a CLI process to its command while it runs,
                # so a shell is keyed by its CLI number, not its task name
                name = f"CLI{mem.r32(tt + PR_TASKNUM)}"
                cmd = self.bstr(mem.r32((cli << 2) + CLI_COMMANDNAME))
                cur = self.proc_cmd.get(tt, "")
                if cmd != cur:
                    # the shell copies the name in place: accept a new name
                    # only when two consecutive slices agree on it
                    if cmd.isprintable() and self.pending.get(tt) == cmd:
                        self.proc_cmd[tt] = cur = cmd
                        if cmd:
                            # cli_ReturnCode still holds the previous command's RC
                            self.on_command(name, cmd, mem.r32((cli << 2) + CLI_RETURNCODE))
                    else:
                        self.pending[tt] = cmd
                return f"{name} > {cur}" if cur else name
        return self.cstr(mem.r32(tt + 10)) or f"task@{tt:06X}"

    def slice(self, n: int) -> None:
        if n:
            self.task_counts[self.key] = self.task_counts.get(self.key, 0) + n
        self.key = self.task_key()

    def on_command(self, proc: str, cmd: str, prev_rc: int = 0) -> None:
        m = self.m
        rc = prev_rc - (1 << 32) if prev_rc & 0x80000000 else prev_rc
        self.commands.append((m.icount, round(m.cycles / m.CPU_HZ, 3), proc, cmd, rc))
        if "bootblock" in self.markers:
            self.mark("first_command")
        if cmd.lower().endswith("loadwb"):
            self.mark("loadwb")

    def on_disk_read(self, track: int) -> None:
        self.disk_ops += 1
        self.tracks_read.append(track)
        self.mark("disk_first_read")
        if track == 0 and "bootblock" not in self.markers:
            self.bb_pending = True

    def check_bootblock(self, pc: int) -> None:
        mem = self.m.mem
        if mem.r32(pc) != self.bb_first:
            return
        if bytes(mem.r_block(pc, len(self.bootcode))) == self.bootcode:
            self.bb_pending = False
            self.mark("bootblock", pc=pc)

    def mark(self, name: str, **extra) -> None:
        if name in self.markers:
            return
        m = self.m
        self.markers[name] = {
            "icount": m.icount, "t_s": round(m.cycles / m.CPU_HZ, 3),
            "idle_s": round(m.idle_cycles / m.CPU_HZ, 3),
            "code_page_changes": m.win_changes, "blit_words": m.blit_words,
            "code_window_lru_misses": dict(m.win_miss),
            "io_calls": m.io_calls, "disk_reads": self.disk_ops,
            "tasks": dict(self.task_counts), "pcblk": dict(m.pcblk), **extra}
        m.snapshot(f"{m.out}/mark_{len(self.markers):02d}_{name}.png")

    def frame(self, frames: int) -> None:
        m = self.m
        if m.hle is not None and m.hle.beginio is None and frames % 10 == 0:
            m.hle.locate()
        if frames % 25:
            return
        sb = self.sysbase()
        if sb is None:
            return
        mem = m.mem
        for lh in (EXEC_TASKREADY, EXEC_TASKWAIT):
            for t in self.walk(sb + lh):
                self.tasks_seen.setdefault(self.cstr(mem.r32(t + 10)), m.icount)
        ib = self.find_node(EXEC_LIBLIST, "intuition.library")
        if ib:
            titles = []
            scr = mem.r32(ib + IB_FIRSTSCREEN)
            for _ in range(8):
                if not scr or scr >= 0x1000000:
                    break
                titles.append("screen " + self.cstr(mem.r32(scr + SC_TITLE)))
                win = mem.r32(scr + SC_FIRSTWINDOW)
                for _ in range(16):
                    if not win or win >= 0x1000000:
                        break
                    titles.append("  window " + self.cstr(mem.r32(win + WIN_TITLE)))
                    win = mem.r32(win)
                scr = mem.r32(scr)
            if titles != self.last_titles:
                self.last_titles = titles
                self.screens.append((m.icount, round(m.cycles / m.CPU_HZ, 2), titles))
                if "loadwb" in self.markers and any("window" in t for t in titles):
                    self.mark("wb_window")         # Workbench's first (backdrop) window
        ops = self.disk_ops + (m.hle.served if m.hle else 0)
        self.idle_hist.append((m.cycles, m.idle_cycles, ops, m.icount))
        if "loadwb" in self.markers and not self.done:
            span = int(self.stop_after_idle * 2)
            h = self.idle_hist
            if len(h) > span:
                c0, i0, o0, ic0 = h[-1 - span]
                c1, i1, o1, _ = h[-1]
                if o1 == o0 and (i1 - i0) >= self.idle_frac * (c1 - c0):
                    # boot idle = the start of this quiet window; the phase
                    # snapshot is taken at its end (a few % of background)
                    self.mark("idle", detected_icount=m.icount,
                              detected_t_s=round(m.cycles / m.CPU_HZ, 3))
                    self.markers["idle"]["icount"] = ic0
                    self.markers["idle"]["t_s"] = round(c0 / m.CPU_HZ, 3)
                    self.done = True
