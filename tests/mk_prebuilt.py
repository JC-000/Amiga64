#!/usr/bin/env python3
"""W17-B: build prebuilt/ from source (`make prebuilt`) or check it (`make prebuilt-check`).

Every artifact host/prebuilt.ARTIFACTS names, built by the same functions a
session calls (AMIGA64_PREBUILT=0 while building, so nothing is loaded from
prebuilt/), plus prebuilt/MANIFEST.  The runner links in build/prebuilt-src
(a fixed relative directory: ld65's map names build/<...>/gen/dispatch.s,
so the map is byte-identical only from the same relative path).

    python3 -m tests.mk_prebuilt            # rebuild prebuilt/
    python3 -m tests.mk_prebuilt --check    # rebuild to a temp dir, compare; exit 1 on any difference
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from host import prebuilt as P  # noqa: E402

RUNNER_BUILD = os.path.join("build", "prebuilt-src")    # relative to ROOT (see above)


def _run(cmd: list[str], cwd: str = ROOT) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n{r.stdout}{r.stderr}")


def _put(out: str, name: str, data: bytes) -> None:
    with open(os.path.join(out, name), "wb") as f:
        f.write(data)


def build_all(out: str) -> None:
    """Every artifact and MANIFEST into *out* (created)."""
    from tests import aros_boot as AB
    from tests import aros_modwb as MW
    from tests import aros_wb as WB
    from tests import run_paula as RP
    from tests import run_spr as SP
    if WB.TD_MUTANT[0] or WB.TD_DEFINES or AB.DRV_OVERRIDE[0]:
        raise RuntimeError("mk_prebuilt: a td mutant/define or driver override is set")
    missing = [t for t, (cmd, _n) in P.TOOLS.items() if not P.have(cmd[0])]
    if missing:
        raise RuntimeError(f"mk_prebuilt: the toolchain is incomplete: {missing} not found")
    old = os.environ.get("AMIGA64_PREBUILT")
    os.environ["AMIGA64_PREBUILT"] = "0"
    os.makedirs(out, exist_ok=True)
    try:
        _run(["make", "-s", "-B", "runtime", f"BUILD={RUNNER_BUILD}"])
        for f in P.ARTIFACTS["runner"]:
            shutil.copyfile(os.path.join(ROOT, RUNNER_BUILD, f), os.path.join(out, f))
        lbl = os.path.join(out, "runner.lbl")
        with tempfile.TemporaryDirectory(prefix="mk_prebuilt-") as tmp:
            sub = lambda n: os.path.join(tmp, n)   # noqa: E731
            b, _ = AB.build_driver(lbl, sub("mboot"))
            _put(out, "mboot.bin", b)
            shutil.copyfile(os.path.join(sub("mboot"), "mboot.lbl"), os.path.join(out, "mboot.lbl"))
            b, _ = WB.wb_driver(lbl, sub("wbboot"))
            _put(out, "wbboot.bin", b)
            shutil.copyfile(os.path.join(sub("wbboot"), "wbboot.lbl"), os.path.join(out, "wbboot.lbl"))
            _put(out, "td.bin", WB.build_td(lbl, sub("td")))
            _put(out, "td_var.bin", WB.build_td(lbl, sub("td_var"), ("TD_VARIANT=$5A",)))
            b, _ = SP.build_spr(sub("spr"), lbl)
            _put(out, "spr.bin", b)
            shutil.copyfile(os.path.join(sub("spr"), "spr.lbl"), os.path.join(out, "spr.lbl"))
            a, b = WB.build_inpm2(lbl, sub("inpm"))
            _put(out, "inpm.a.bin", a)
            _put(out, "inpm.b.bin", b)
            a, b, _ = RP.build_aud(lbl, sub("aud"))
            _put(out, "aud.a.bin", a)
            _put(out, "aud.b.bin", b)
            shutil.copyfile(os.path.join(sub("aud"), "aud.lbl"), os.path.join(out, "aud.lbl"))
            mw = sub("modwb")
            _run(["make", "-s", "-C", os.path.join(ROOT, "fixtures", "modwb"), f"OUT={mw}"])
            for f in P.ARTIFACTS["modwb"]:
                shutil.copyfile(os.path.join(mw, f), os.path.join(out, f))
            _put(out, "modwb_standin", MW.build_standin(sub("standin")))
            _put(out, "modwb_standin_noprof", MW.build_standin(sub("standin"), ("STANDIN_NOPROF",)))
    finally:
        if old is None:
            os.environ.pop("AMIGA64_PREBUILT", None)
        else:
            os.environ["AMIGA64_PREBUILT"] = old
    write_manifest(out)


def write_manifest(out: str) -> None:
    import hashlib
    from host import snapfmt
    files = [f for files in P.ARTIFACTS.values() for f in files]
    blobs = {f: open(os.path.join(out, f), "rb").read() for f in files}
    lines = ["# prebuilt/MANIFEST: written by `make prebuilt` (tests/mk_prebuilt.py); do not edit.",
             "# Checked by host/prebuilt.verify and host/tests/test_prebuilt.py; by hand:",
             "#   cd prebuilt && grep -v '^#' MANIFEST | shasum -a 256 -c",
             "#: build_id " + snapfmt.read_bid(os.path.join(out, "runner.prg"),
                                               os.path.join(out, "runner.lbl")).hex()]
    for t, v in P.versions().items():
        lines.append(f"#: tool {t} {v}")
    for name, fields in P.IDS.items():
        lines.append(f"#: id {name} {P.module_id(blobs, fields)[0]}")
    for f in files:
        lines.append(f"{hashlib.sha256(blobs[f]).hexdigest()}  {f}")
    with open(os.path.join(out, "MANIFEST"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def compare(a: str, b: str) -> list[str]:
    """The files (MANIFEST included) that differ between two prebuilt trees."""
    diff = []
    for f in ["MANIFEST"] + [f for files in P.ARTIFACTS.values() for f in files]:
        pa, pb = os.path.join(a, f), os.path.join(b, f)
        if not (os.path.isfile(pa) and os.path.isfile(pb)):
            diff.append(f"{f}: missing in {a if not os.path.isfile(pa) else b}")
        elif open(pa, "rb").read() != open(pb, "rb").read():
            diff.append(f"{f}: differs")
    return diff


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="rebuild to a temp dir and compare with prebuilt/ (exit 1 if stale)")
    a = ap.parse_args(argv)
    if a.check:
        with tempfile.TemporaryDirectory(prefix="prebuilt-check-") as tmp:
            build_all(tmp)
            diff = compare(tmp, P.DIR)
        probs = P.verify()
        for x in diff:
            print(f"STALE {x}")
        for x in probs:
            print(f"BAD {x}")
        print(f"prebuilt-check: {'OK' if not diff and not probs else 'FAIL (run `make prebuilt`)'}")
        return 1 if diff or probs else 0
    with tempfile.TemporaryDirectory(prefix="prebuilt-") as tmp:
        build_all(tmp)
        os.makedirs(P.DIR, exist_ok=True)
        for f in ["MANIFEST"] + [f for files in P.ARTIFACTS.values() for f in files]:
            shutil.copyfile(os.path.join(tmp, f), os.path.join(P.DIR, f))
    probs = P.verify()
    for x in probs:
        print(f"BAD {x}")
    print(f"prebuilt: {P.DIR} ({'OK' if not probs else 'FAIL'}); build ID {P.manifest()['build_id']}")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
