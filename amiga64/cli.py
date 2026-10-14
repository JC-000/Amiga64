"""The `amiga64` command.

    amiga64 doctor [--offline]        check the device, the ROM, the ADF, the install
    amiga64 run [--save]              boot Workbench on the Ultimate (attended)
    amiga64 resume                    resume the session `run --save` saved
    amiga64 adf add PATH... [--to DRAWER] [--as NAME] [--out NEW.adf]
    amiga64 adf ls [DRAWER] | adf rm DISKPATH | adf clear
    amiga64 config                    show the settings and where they come from

docs/WAVE17_BRIEF.md "W17-K"; the user guide is docs/guide/QUICKSTART.md.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from dataclasses import dataclass

from amiga64 import __version__
from amiga64 import config as C

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "--"


@dataclass
class Check:
    status: str
    title: str
    msg: str

    def line(self) -> str:
        return f"{self.status:<4} {self.title}: {self.msg}"


# --------------------------------------------------------------------------
# shared resolution
# --------------------------------------------------------------------------

def _cfg() -> dict:
    return C.load()


def _host(a, cfg: dict, prompter: C.Prompter | None) -> tuple[str, str]:
    host, src = C.resolve_host(getattr(a, "host", None), cfg)
    if host is None:
        if prompter is None:
            raise C.ConfigError("no device address: give --host, set AMIGA64_HOST, or run "
                                "`amiga64 run` once to be asked")
        host, src = prompter.ask_host(), "prompt"
    return host, src


def _adf_path(a, cfg: dict, prompter: C.Prompter | None) -> tuple[str, str]:
    path, src, cands = C.resolve_adf(getattr(a, "adf", None), cfg, os.getcwd())
    if path is None:
        if prompter is None:
            if cands:
                raise C.ConfigError(f"several ADFs in ./adf/ ({', '.join(map(os.path.basename, cands))}): "
                                    f"choose one with --adf")
            raise C.ConfigError("no Workbench ADF: put your Workbench 3.1 ADF in ./adf/ or "
                                "give --adf PATH")
        path, src = prompter.ask_adf(os.getcwd(), cands), "prompt"
    return path, src


def _overrides(a, cfg: dict) -> tuple[str | None, int | None, str | None]:
    kind = getattr(a, "kind", None) or C.get(cfg, "device", "kind")
    mhz = getattr(a, "mhz", None) or C.get(cfg, "device", "mhz")
    vol = getattr(a, "vol", None) or C.get(cfg, "device", "volume")
    if kind and kind not in ("U64E", "C64U"):
        raise C.ConfigError(f"kind {kind!r}: U64E or C64U")
    return kind, int(mhz) if mhz else None, vol


def _remember(cfg: dict, host_src: str, host: str, adf_src: str | None, adf: str | None) -> None:
    """The first run writes what it asked, and what --host/--adf/./adf/
    gave while the file has no value yet; it never replaces a value the
    file has unless the user just typed a new one."""
    changed = False
    if host_src == "prompt" or (host_src == "--host" and not C.get(cfg, "device", "host")):
        C.put(cfg, "device", "host", host)
        changed = True
    if adf and (adf_src == "prompt" or (adf_src in ("--adf", "./adf/")
                                        and not C.get(cfg, "disk", "adf"))):
        C.put(cfg, "disk", "adf", adf)
        changed = True
    if changed:
        print(f"amiga64: saved your settings in {C.save(cfg)}", flush=True)


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------

def doctor_offline(a, cfg: dict) -> tuple[list[Check], dict]:
    from amiga64 import adf as A
    from amiga64 import kit
    out: list[Check] = []
    ctx: dict = {}
    v = sys.version_info
    out.append(Check(OK if v >= (3, 11) else FAIL, "Python",
                     f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 11) else " (3.11 or newer needed)")))
    try:
        import importlib.metadata as M

        import c64_test_harness as H
        try:
            ver = M.version("c64-test-harness")
        except M.PackageNotFoundError:
            ver = "version unknown"
        out.append(Check(OK, "c64-test-harness", f"{ver} ({os.path.dirname(H.__file__)})"))
    except ImportError as e:
        out.append(Check(FAIL, "c64-test-harness", f"not installed ({e}); reinstall amiga64 with pip"))
    probs = kit.verify()
    out.append(Check(FAIL if probs else OK, "prebuilt binaries",
                     "; ".join(probs) if probs else f"runner and ModPlayer verified (build ID {kit.build_id()})"))
    rom, why = kit.rom()
    if rom is None:
        out.append(Check(FAIL, "AROS ROM", why))
    else:
        where = "bundled (third_party/aros)" if os.path.basename(rom.cache) == "aros" \
            and os.path.basename(os.path.dirname(rom.cache)) == "third_party" else f"cache {rom.cache}"
        out.append(Check(OK, "AROS ROM", f"{where}, hashes match tools/aros/manifest.txt"))
    try:
        path, src = _adf_path(a, cfg, None)
    except C.ConfigError as e:
        out.append(Check(FAIL, "Workbench ADF", str(e)))
        return out, ctx
    try:
        info = A.inspect(path)
    except A.AdfProblem as e:
        out.append(Check(FAIL, "Workbench ADF", str(e)))
        return out, ctx
    ctx["adf"] = (path, info)
    out.append(Check(OK if info.tested else WARN, "Workbench ADF",
                     f"{path} (from {src}): DOS\\{info.dostype}, {info.verdict}"))
    staged = A.Staged().requests()
    if not probs:
        try:
            img, _rep = A.session_image(A.read(path), kit.player(), kit.song(), staged)
            from host import adf_files as F
            out.append(Check(OK, "session disk",
                             f"ModPlayer files fit; {len(staged)} staged add(s); "
                             f"{F.DiskEdit(img).free_count()} blocks free"))
        except C.ConfigError as e:
            out.append(Check(FAIL, "session disk", f"{e} (amiga64 adf rm / adf clear)"))
        except Exception as e:          # noqa: BLE001 - e.g. a disk the ModPlayer needs room on
            out.append(Check(FAIL, "session disk", f"the ModPlayer files do not fit: {e}"))
    return out, ctx


def doctor_device(a, cfg: dict, ctx: dict) -> list[Check]:
    from amiga64 import adf as A
    from amiga64 import device as D
    out: list[Check] = []
    try:
        host, src = _host(a, cfg, None)
    except C.ConfigError as e:
        return [Check(FAIL, "device", str(e))]
    kind, mhz, vol = _overrides(a, cfg)
    D.set_env(host, C.resolve_password(cfg))
    adf = ctx["adf"][1].sha256 if "adf" in ctx else None
    print(f"checking the Ultimate at {host} (from {src}); it is held briefly and reset "
          f"when released ...", flush=True)
    try:
        det = D.probe(host, D.hold_dir("doctor"), kind, mhz, vol, adf)
    except D.DeviceError as e:
        return [Check(FAIL, "device", str(e))]
    out.append(Check(OK, "device", f"{det.product} ({det.kind}) at {host}"))
    out.append(Check(OK if det.fw_tested else WARN, "firmware",
                     det.firmware + ("" if det.fw_tested else
                                     f" (untested; tested: {', '.join(D.TESTED_FW[det.kind])})")))
    out.append(Check(INFO, "speed", f"{det.mhz} MHz turbo for the session"))
    out.append(Check(OK if det.reu16 else FAIL, "REU",
                     "16 MB available (set for the session, restored after)" if det.reu16
                     else "this device offers no 16 MB REU; amiga64 needs one"))
    out.append(Check(OK if det.ci else FAIL, "Command Interface",
                     "available (enabled for the session, restored after)" if det.ci
                     else "no 'Command Interface' setting: the disk cannot be loaded"))
    out.append(Check(OK if det.sampler else WARN, "audio",
                     "the Ultimate Audio sampler can be mapped (Paula sound)" if det.sampler
                     else "no 'Map Ultimate Audio' setting: the session runs without sound"))
    out.append(Check(OK if det.dma else FAIL, "DMA service",
                     "Ultimate DMA Service enabled" if det.dma else
                     "Network Settings / Ultimate DMA Service is off: the disk cannot be staged. "
                     "Turn it on (the C64 Ultimate turns it off at every power-on)"))
    out.append(Check(OK, "FTP service", "FTP File Service enabled"))
    n = len(det.temp_managed)
    out.append(Check(WARN if n > D.TEMP_KEEP else OK, "/Temp",
                     "no leftover firmware files" if n == 0 else
                     f"1 firmware file ({det.temp_managed[0]}): normal, the harness's sweep keeps "
                     f"the newest" if n <= D.TEMP_KEEP else
                     f"{n} leftover firmware files ({', '.join(det.temp_managed[:5])}"
                     f"{' ...' if n > 5 else ''}): the harness's sweep keeps only the newest, so "
                     f"sweeps are failing; a full /Temp can crash a C64 Ultimate: a power-cycle "
                     f"clears it"))
    out.append(Check(OK, "storage", f"{det.vol} (writable: {', '.join('/' + v for v in det.volumes)})"))
    if adf:
        own = D.adf_name(adf)
        if det.adf_use == own:
            out.append(Check(OK, "ADF on the device", f"{det.vol}/{D.DIR}/{own}, same bytes"))
        elif det.adf_use:
            out.append(Check(OK, "ADF on the device",
                             f"reusing {det.vol}/{D.DIR}/{det.adf_use}: same bytes as your ADF "
                             f"(so {own} is not copied)"))
        elif own in det.adf_differs:
            out.append(Check(FAIL, "ADF on the device",
                             f"{det.vol}/{D.DIR}/{own} differs from your ADF; amiga64 never "
                             f"overwrites it: move it away on the device"))
        else:
            out.append(Check(INFO, "ADF on the device",
                             f"not yet on {det.vol}/{D.DIR}/; `amiga64 run` copies it as {own} "
                             f"(after asking)"))
    out.append(Check(INFO, "saved session",
                     f"{det.vol}/{D.DIR}/{D.SNAP} present (`amiga64 resume`)" if D.SNAP in det.files
                     else "none (`amiga64 run --save` makes one)"))
    out.append(Check(OK if det.restored else FAIL, "device settings",
                     "restored exactly after the check" if det.restored
                     else "NOT restored exactly: see the report in " + os.path.dirname(D.hold_dir("x"))))
    return out


def cmd_doctor(a) -> int:
    cfg = _cfg()
    print(f"amiga64 {__version__} doctor (config: {C.config_path()})")
    checks, ctx = doctor_offline(a, cfg)
    for c in checks:
        print(c.line(), flush=True)
    if not a.offline:
        dev = doctor_device(a, cfg, ctx)
        for c in dev:
            print(c.line(), flush=True)
        checks += dev
    bad = [c for c in checks if c.status == FAIL]
    print(f"{len(bad)} problem(s)" if bad else "all good: `amiga64 run` boots Workbench")
    return 1 if bad else 0


# --------------------------------------------------------------------------
# run / resume
# --------------------------------------------------------------------------

def _plan_device(a, cfg: dict, prompter: C.Prompter, adf_sha: str | None):
    from amiga64 import device as D
    host, src = _host(a, cfg, prompter)
    kind, mhz, vol = _overrides(a, cfg)
    D.set_env(host, C.resolve_password(cfg))
    print(f"amiga64: checking the Ultimate at {host} ...", flush=True)
    det = D.probe(host, D.hold_dir("probe"), kind, mhz, vol, adf_sha)
    if not det.dma:
        raise C.ConfigError("Network Settings / Ultimate DMA Service is off: the disk cannot be "
                            "staged. Turn it on (the C64 Ultimate turns it off at every power-on)")
    if len(det.temp_managed) > D.TEMP_KEEP:
        print(f"amiga64: note: {len(det.temp_managed)} leftover firmware files in /Temp "
              f"(amiga64 doctor)", flush=True)
    if not det.reu16:
        raise C.ConfigError("this device offers no 16 MB REU (amiga64 doctor)")
    if not det.ci:
        raise C.ConfigError("this device has no 'Command Interface' setting (amiga64 doctor)")
    if not det.restored:
        raise C.ConfigError("the device settings were not restored exactly after the check; "
                            "see amiga64 doctor")
    print(f"amiga64: {det.product} ({det.kind}, fw {det.firmware}), {det.mhz} MHz, {det.vol}",
          flush=True)
    return host, src, det


def _mouse(a, cfg: dict) -> int:
    v = a.mouse_port if a.mouse_port is not None else C.get(cfg, "input", "mouse_port", 0)
    if int(v) not in (0, 1, 2):
        raise C.ConfigError(f"mouse_port {v}: 0 (none), 1 or 2")
    return int(v)


def cmd_run(a) -> int:
    from amiga64 import adf as A
    from amiga64 import device as D
    from amiga64 import kit
    from amiga64 import session as S
    cfg = _cfg()
    pr = C.Prompter(yes=a.yes)
    path, adf_src = _adf_path(a, cfg, pr)
    info = A.inspect(path)
    print(f"amiga64: {path}: {info.verdict}", flush=True)
    raw = A.read(path)
    rom, why = kit.rom()
    if rom is None:
        raise C.ConfigError(f"broken install: the AROS ROM: {why}")
    prg = kit.runner()
    disk, addrep = S.make_disk(raw, kit.player(), kit.song(), A.Staged().requests())
    if addrep.added:
        print(f"amiga64: staged files on the session disk: {', '.join(addrep.added)}")
    plan = S.Plan(backend=a.backend, prg=prg, mouse_port=_mouse(a, cfg), minutes=a.minutes,
                  save=a.save, audio=not a.no_audio, stuck=a.stuck,
                  adf_name=D.adf_name(info.sha256))
    if a.backend == "vice":
        if a.save:
            raise C.ConfigError("--save needs an Ultimate (VICE keeps no file)")
        plan.audio = False
    else:
        host, host_src, det = _plan_device(a, cfg, pr, info.sha256)
        plan.host, plan.mhz, plan.vol = host, det.mhz, det.vol
        plan.audio = plan.audio and bool(det.sampler)
        _remember(cfg, host_src, host, adf_src, path)
        if det.adf_use:
            plan.adf_name = det.adf_use
            if det.adf_use != D.adf_name(info.sha256):
                print(f"amiga64: reusing {det.vol}/{D.DIR}/{det.adf_use} (same bytes as your ADF)")
        elif plan.adf_name in det.adf_differs:
            raise C.ConfigError(f"{det.vol}/{D.DIR}/{plan.adf_name} on the device differs from "
                                f"your ADF; amiga64 never overwrites it: move it away on the device")
        else:
            if not pr.confirm(f"Copy {os.path.basename(path)} ({len(raw) // 1024} KB) to "
                              f"{det.vol}/{D.DIR}/{plan.adf_name} on {host}?"):
                print("amiga64: nothing copied; the session needs the ADF on the device")
                return 1
            plan.upload = True
        if a.save and D.SNAP in det.files:
            print(f"amiga64: --save replaces the saved session {det.vol}/{D.DIR}/{D.SNAP}")
    plan.out = D.hold_dir("run")
    with S.logged(os.path.join(plan.out, "session.log")):
        fails, rep = S.run(plan, rom, disk)
    return _finish(plan, rep, fails, "run")


def cmd_resume(a) -> int:
    from amiga64 import device as D
    from amiga64 import kit
    from amiga64 import session as S
    cfg = _cfg()
    pr = C.Prompter()
    rom, why = kit.rom()
    if rom is None:
        raise C.ConfigError(f"broken install: the AROS ROM: {why}")
    host, host_src, det = _plan_device(a, cfg, pr, None)
    if D.SNAP not in det.files:
        raise C.ConfigError(f"no saved session on {det.vol}/{D.DIR}/ (`amiga64 run --save` makes one)")
    _remember(cfg, host_src, host, None, None)
    plan = S.Plan(backend="u64", prg=kit.runner(), host=host, mhz=det.mhz, vol=det.vol,
                  mouse_port=_mouse(a, cfg), minutes=a.minutes, audio=not a.no_audio and bool(det.sampler),
                  stuck=a.stuck, out=D.hold_dir("resume"))
    with S.logged(os.path.join(plan.out, "session.log")):
        fails, rep = S.resume(plan, rom)
    return _finish(plan, rep, fails, "resume")


def _finish(plan, rep: dict, fails: list[str], what: str) -> int:
    from amiga64 import session as S
    p = S.write_report(plan, rep, fails)
    for f in fails:
        print(f"amiga64: FAILED: {f}")
    dev = "VICE stopped" if plan.backend == "vice" else "the device settings are restored"
    print(f"amiga64: {what} {'ended' if not fails else 'failed'}; {dev}. "
          f"Log and report: {os.path.dirname(p)}")
    return 1 if fails else 0


# --------------------------------------------------------------------------
# adf
# --------------------------------------------------------------------------

def _session_parts(a, cfg):
    from amiga64 import adf as A
    from amiga64 import kit
    path, _src = _adf_path(a, cfg, C.Prompter())
    raw = A.read(path)
    A.check(raw, path)
    return path, raw, kit.player(), kit.song()


def cmd_adf(a) -> int:
    from amiga64 import adf as A
    cfg = _cfg()
    st = A.Staged()
    if a.adf_cmd == "add":
        reqs = [A.Request(os.path.abspath(p), a.to or "", a.as_name) for p in a.paths]
        if a.as_name and len(reqs) != 1:
            raise C.ConfigError("--as names one file: give one PATH with it")
        if a.out:
            path, _ = _adf_path(a, cfg, C.Prompter())
            rep = A.write_out(path, reqs, os.path.abspath(a.out))
            print(f"wrote {a.out}: {path} plus {', '.join(rep.added)} ({rep.blocks} blocks); "
                  f"your ADF is unchanged")
        else:
            _p, raw, player, mod = _session_parts(a, cfg)
            rep = st.add(raw, player, mod, reqs)
            print(f"staged {', '.join(rep.added)} ({rep.blocks} blocks): every `amiga64 run` "
                  f"adds it to the session's copy of the disk; your ADF is unchanged")
        for s in rep.skipped:
            print(f"  skipped hidden file {s}")
        return 0
    if a.adf_cmd == "ls":
        path, raw, player, mod = _session_parts(a, cfg)
        if a.original:
            img = raw
            print(f"{path} (your file, as it is):")
        else:
            img, _ = A.session_image(raw, player, mod, st.requests())
            print(f"the session disk ({os.path.basename(path)} + the ModPlayer files"
                  f"{' + staged files' if st.requests() else ''}; "
                  f"{A.MODWB_DROP} removed from the copy):")
        staged = {A.Staged.disk_path(r).lower() for r in st.requests()}
        for line in A.listing(img, a.drawer or "", staged):
            print(line)
        return 0
    if a.adf_cmd == "rm":
        n = st.remove(a.diskpath)
        print(f"unstaged {a.diskpath}" if n else f"{a.diskpath} is not staged "
              f"(amiga64 removes nothing from the disk itself)")
        return 0 if n else 1
    if a.adf_cmd == "clear":
        st.clear()
        print("no files staged")
        return 0
    return 2


def cmd_config(a) -> int:
    cfg = _cfg()
    host, src = C.resolve_host(None, cfg)
    adf, asrc, _ = C.resolve_adf(None, cfg, os.getcwd())
    print(f"config file: {C.config_path()}{'' if os.path.exists(C.config_path()) else ' (none yet)'}")
    print(f"state:       {C.state_dir()}")
    print(f"device:      {host or '(none)'}" + (f" (from {src})" if src else ""))
    print(f"password:    {'set' if C.resolve_password(cfg) else 'none'}")
    print(f"ADF:         {adf or '(none)'}" + (f" (from {asrc})" if asrc else ""))
    for k in ("kind", "mhz", "volume"):
        if C.get(cfg, "device", k):
            print(f"{k + ':':<12} {C.get(cfg, 'device', k)} (config override)")
    print(f"mouse_port:  {C.get(cfg, 'input', 'mouse_port', 0)}")
    return 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="amiga64", description="Boot Workbench 3.1 on AROS on a "
                                 "C64 Ultimate or an Ultimate 64 Elite.")
    ap.add_argument("--version", action="version", version=f"amiga64 {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def dev(p, kind=True):
        p.add_argument("--host", help="the Ultimate's address (else AMIGA64_HOST, U64_HOST, the config)")
        if kind:
            p.add_argument("--kind", choices=("U64E", "C64U"), help="override the detected device")
            p.add_argument("--mhz", type=int,
                           help="turbo speed (default: U64E 48, C64U 64)")
            p.add_argument("--vol", help="storage volume (default: U64E /USB1, C64U /SD)")

    p = sub.add_parser("doctor", help="check everything and say what is wrong")
    dev(p)
    p.add_argument("--adf", help="your Workbench 3.1 ADF")
    p.add_argument("--offline", action="store_true", help="skip the device")
    p.set_defaults(fn=cmd_doctor)

    for name, fn, h in (("run", cmd_run, "boot Workbench on the Ultimate"),
                        ("resume", cmd_resume, "resume the session `run --save` saved")):
        p = sub.add_parser(name, help=h)
        dev(p)
        p.add_argument("--mouse-port", type=int, choices=(0, 1, 2), default=None,
                       help="the 1351 mouse's port (0: joystick only; default: the config, else 0)")
        p.add_argument("--minutes", type=float, default=None,
                       help="end after this many minutes (default: Ctrl-C)")
        p.add_argument("--no-audio", action="store_true", help="no Paula sound")
        p.add_argument("--stuck", type=lambda v: int(v, 0), default=0,
                       help="mask a stuck joystick switch (e.g. 0x01 = up)")
        if name == "run":
            p.add_argument("--adf", help="your Workbench 3.1 ADF")
            p.add_argument("--save", action="store_true",
                           help="at the end, save the session to <vol>/amiga64/wb-snap.bin")
            p.add_argument("--yes", action="store_true", help="copy the ADF without asking")
            p.add_argument("--backend", choices=("u64", "vice"), default="u64",
                           help=argparse.SUPPRESS)      # vice: developers (needs x64sc)
        p.set_defaults(fn=fn)

    p = sub.add_parser("adf", help="files onto the session disk; list it")
    asub = p.add_subparsers(dest="adf_cmd", required=True)
    q = asub.add_parser("add", help="add host files or directories")
    q.add_argument("paths", nargs="+")
    q.add_argument("--to", default="", help="the drawer on the disk (default: the root)")
    q.add_argument("--as", dest="as_name", help="the name on the disk (one PATH)")
    q.add_argument("--out", help="write a NEW local ADF (your ADF plus the files) instead")
    q.add_argument("--adf", help="your Workbench 3.1 ADF")
    q = asub.add_parser("ls", help="list the session disk")
    q.add_argument("drawer", nargs="?", default="")
    q.add_argument("--original", action="store_true", help="your ADF as it is")
    q.add_argument("--adf", help="your Workbench 3.1 ADF")
    q = asub.add_parser("rm", help="unstage a file added with `adf add`")
    q.add_argument("diskpath")
    asub.add_parser("clear", help="unstage everything")
    p.set_defaults(fn=cmd_adf)

    p = sub.add_parser("config", help="show the settings")
    p.set_defaults(fn=cmd_config)
    return ap


def _term(signum, frame):
    from amiga64 import session as S
    raise S.Terminated(f"signal {signum}")


def main(argv=None) -> int:
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, _term)
    a = parser().parse_args(argv)
    try:
        return a.fn(a)
    except C.ConfigError as e:
        print(f"amiga64: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("amiga64: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
