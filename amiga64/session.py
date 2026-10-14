"""`amiga64 run` and `amiga64 resume`: the attended Workbench session.

In-process, through the tests' own functions (docs/WAVE17_BRIEF.md ruling:
no subprocess fan-out to test scripts):
  tests/aros_wb.session      the device hold (u64_hold), the speed, the reboot,
                             the runner load, the sampler mapped (--audio);
  tests/aros_wb.boot_workbench  ROM + disk staged, the boot watched to the
                             Workbench (no model screen check);
  then port 2 (and the 1351) handed to the user, the pointer armed, and the
  machine kept running until Ctrl-C or --minutes.  `run --save` suspends to
  <vol>/amiga64/wb-snap.bin at the end; `resume` resumes that file
  (tests/aros_wb.resume_workbench), refusing another build or ROM.
This is tests/aros_wb.leg_demo_boot with the modwb disk, plus the ROM id
declared at the staging (as leg w does) so that --save can suspend.

The tree is on sys.path (amiga64/__init__.py).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from dataclasses import dataclass

from amiga64 import adf as A
from amiga64 import config as C
from amiga64 import device as D

SNAP_REFUSALS = {
    0xE3: "it was saved by a different amiga64 build",
    0xE4: "it was saved with a different AROS ROM",
    0xE1: "it is not an amiga64 snapshot (or an older format)",
    0xE6: "there is no saved session on the device (amiga64 run --save makes one)",
}


class SessionError(C.ConfigError):
    """The session failed (plain message)."""


class Terminated(KeyboardInterrupt):
    """SIGTERM: straight to the device restore (no --save on the way out)."""


@dataclass
class Plan:
    backend: str                         # "u64" or "vice"
    prg: str
    host: str | None = None
    mhz: int = 1
    vol: str | None = None
    mouse_port: int = 0
    minutes: float | None = None         # None: until Ctrl-C
    save: bool = False
    audio: bool = True
    stuck: int = 0
    timeout: float = 1800.0
    out: str = ""
    adf_name: str = D.TESTED_ADF_NAME
    upload: bool = False                 # put the ADF on <vol>/amiga64/ first


def say(msg: str) -> None:
    print(f"amiga64: {msg}", file=sys.__stdout__, flush=True)


class _Tee:
    def __init__(self, *streams):
        self.s = streams

    def write(self, x):
        for s in self.s:
            s.write(x)
        return len(x)

    def flush(self):
        for s in self.s:
            s.flush()


@contextlib.contextmanager
def logged(path: str):
    """Everything the test functions print goes to the terminal and *path*."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f, contextlib.redirect_stdout(_Tee(sys.stdout, f)):
        yield


def hold_loop(minutes: float | None, clock=time.monotonic, sleep=time.sleep) -> bool:
    """Keep the session up for *minutes* (None: until Ctrl-C).  -> True
    when Ctrl-C ended it."""
    end = None if minutes is None else clock() + minutes * 60.0
    try:
        while end is None or clock() < end:
            sleep(1.0 if end is None else max(0.0, min(1.0, end - clock())))
        return False
    except Terminated:
        raise
    except KeyboardInterrupt:
        return True


def make_disk(raw: bytes, player: bytes, mod: bytes, reqs: list[A.Request]):
    """aros_modwb.ModDisk (trim + the ModPlayer, its icon and the MOD) with
    the staged files added: (disk, the add report)."""
    from host import adf_files as F
    from host import adf_overlay as O
    from tests import aros_modwb as MW
    d = MW.ModDisk(raw, A.SCRIPT, player, mod, with_info=True, standin=False)
    rep = A.Report()
    if reqs:
        e = F.DiskEdit(d.image)
        rep = A.add(e, reqs)
        img = e.data()
        d.ov = F.overlay_of(raw, img, f"{A.SCRIPT}+modwb+files", d.ov.header, d.ov.data_blocks)
        d.image = O.apply(raw, d.ov)
        if d.image != img:
            raise SessionError("internal: the session overlay does not reproduce the image")
    return d, rep


def _globals(plan: Plan, s: int) -> None:
    """The module flags tests/aros_wb.main sets from its command line."""
    from tests import aros_wb as WB
    WB.AUDIO[0] = plan.audio
    WB.AUD_S[0] = s
    WB.DEMO_MOUSE[0] = plan.mouse_port
    WB.STUCK[0] = plan.stuck
    # the ruling: no model screen check. boot_workbench would otherwise run the
    # boot model (only where an s3probe venv exists, writing into the tree) and
    # compare the serial text with the tests' ADF's, not the user's
    WB.model_wb = no_model


def no_model(script: str):
    return None, "amiga64: no boot model (the attended run)"


def _speed(plan: Plan) -> int:
    from tests import aros_boot as AB
    return AB.BOOT_S_VICE if plan.backend == "vice" else AB.BOOT_S_U64


def _handover(m, plan: Plan, s: int) -> None:
    from tests import aros_wb as WB
    if plan.stuck:
        m.w(m.lab("inp_stuck"), bytes([plan.stuck]))
    m.w(m.lab("inp_src"), b"\0\0\0")     # port 2 is the user's (inp_src 0)
    WB.arm_spr(m)
    WB.demo_mouse(m, s)


def _ready(plan: Plan, what: str) -> None:
    inp = "the joystick in port 2" + (f" and the 1351 in port {plan.mouse_port}"
                                       if plan.mouse_port else "")
    how = (f"for {plan.minutes:g} min" if plan.minutes is not None else "until you press Ctrl-C")
    say(f"{what}: use {inp}; double-click ModPlayer to hear Paula, click to return. "
        f"F1 toggles the 1:1 view. Running {how}" +
        ("; Ctrl-C saves the session to the device first" if plan.save else ""))


def run(plan: Plan, rom, disk) -> tuple[list[str], dict]:
    """Boot the Workbench from *disk* and hold it for the user."""
    from tests import aros_resume as RS
    from tests import aros_wb as WB
    s = _speed(plan)
    _globals(plan, s)
    rep: dict = {"backend": plan.backend, "host": plan.host, "mhz": plan.mhz, "vol": plan.vol,
                 "adf_name": plan.adf_name, "mach_s": s}
    snap = f"{plan.vol}/{D.DIR}/{D.SNAP}" if plan.save and plan.vol else None
    disk.adf_name = plan.adf_name       # tests/aros_wb.stage_disk loads <vol>/amiga64/<this>

    def body(m) -> list[str]:
        t_run = time.monotonic()
        WB.enable_ci(m, rep)
        if plan.upload:
            say(f"copying your ADF to {plan.vol}/{D.DIR}/{plan.adf_name} ...")
            rep["adf"] = D.ensure_adf(plan.host, plan.vol, plan.adf_name, disk.raw)
        sn = RS.Snap(m, m.prg, RS.rom_id(rom))
        say("booting AROS and your Workbench (" + ("VICE: many minutes" if m.vice else
                                                   "about 2-4 minutes") + ") ...")
        fails = WB.boot_workbench(m, rom, disk, s, rep.setdefault("boot", {}), plan.vol,
                                  plan.timeout, want_screen=False, t_start=t_run,
                                  after_stage=sn.declare_rid)
        if fails:
            return fails
        _handover(m, plan, s)
        rep["up_s"] = round(time.monotonic() - t_run, 1)
        _ready(plan, f"the Workbench is up after {rep['up_s']:.0f} s")
        rep["ended_by"] = "ctrl-c" if hold_loop(plan.minutes) else "time"
        if snap:
            say(f"saving the session to {snap} ...")
            WB._suspend(m, sn, snap, rep.setdefault("save", {}))
            rep["saved"] = snap
            say(f"saved; `amiga64 resume` brings it back")
        return []

    return _session(plan, body), rep


def resume(plan: Plan, rom) -> tuple[list[str], dict]:
    """Resume <vol>/amiga64/wb-snap.bin and hold it for the user."""
    from tests import aros_wb as WB
    s = _speed(plan)
    _globals(plan, s)
    path = f"{plan.vol}/{D.DIR}/{D.SNAP}"
    rep: dict = {"backend": plan.backend, "host": plan.host, "mhz": plan.mhz, "snap": path}

    def body(m) -> list[str]:
        WB.enable_ci(m, rep)
        say(f"resuming {path} ...")
        try:
            el = WB.resume_workbench(m, rom, path, rep)
        except RuntimeError as e:
            return [refusal(str(e))]
        WB.demo_mouse(m, s)
        if plan.stuck:
            m.w(m.lab("inp_stuck"), bytes([plan.stuck]))
        _ready(plan, f"the saved Workbench runs again ({el:.0f} s)")
        rep["ended_by"] = "ctrl-c" if hold_loop(plan.minutes) else "time"
        return []

    return _session(plan, body), rep


def refusal(msg: str) -> str:
    """A resume error as a plain sentence (snap_st codes: host/snapfmt)."""
    import re
    m = re.search(r"\$([0-9A-F]{2})", msg)
    code = int(m.group(1), 16) if m and "refused" in msg else None
    if code in SNAP_REFUSALS:
        return (f"cannot resume the saved session: {SNAP_REFUSALS[code]}. Start a new one "
                f"with `amiga64 run` (add --save to keep it)")
    return f"cannot resume the saved session: {msg}"


def _session(plan: Plan, body) -> list[str]:
    from tests import aros_wb as WB
    os.makedirs(plan.out, exist_ok=True)
    return WB.session(plan.prg, plan.backend, plan.out, plan.mhz, body)


def write_report(plan: Plan, rep: dict, fails: list[str]) -> str:
    rep["fails"] = fails
    p = os.path.join(plan.out, "session.json")
    with open(p, "w") as f:
        json.dump(rep, f, indent=1, default=str)
    return p
