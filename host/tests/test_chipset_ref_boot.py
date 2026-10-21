"""aros_boot_model.py runs on host/chipset_ref.py, with the counts it had before (W7-M).

The pinned numbers were measured with the pre-S3.0 model (W6-D/WB-measure's
own chipset classes) and are unchanged after the promotion
(build/evidence/w7-models/green/bit_identical*.txt compares whole reports).
Images are synthetic (adf.build_adf, as test_wb_disk.py); the legs that boot
the AROS ROM skip without the ROM cache or the s3probe venv.
"""
import glob
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "tools" / "s3probe" / "aros_boot_model.py"
VENV_PY = Path.home() / ".cache" / "amiga64" / "s3probe-venv" / "bin" / "python"
sys.path.insert(0, str(ROOT / "tools" / "s3probe"))

import adf  # noqa: E402

FILES = {"S/Startup-Sequence": b"Echo hello\n" * 70, "C/Tool": bytes(range(256)) * 5,
         "Empty": b""}
BOOTCODE = bytes.fromhex("2E3CC0DEF00D60FE")         # move.l #$C0DEF00D,d7 ; bra.s *


def test_boot_model_uses_the_reference_classes():
    src = MODEL.read_text()
    assert "from host.chipset_ref import" in src
    assert "class Amiga(Chipset):" in src
    for gone in ("class CIA", "def blit_line", "_setclr", "CUSTOM_NAMES = {"):
        assert gone not in src, gone


def _run(tmp_path, *args):
    if not glob.glob(str(ROOT / "third_party" / "aros" / "aros-rom.bin")):
        pytest.skip("no AROS ROM in third_party/aros")
    if not VENV_PY.exists():
        pytest.skip("no s3probe venv (tools/s3probe/setup_venv.sh)")
    out = tmp_path / "out"
    subprocess.run([str(VENV_PY), str(MODEL), *args, "--out", str(out)], check=True,
                   capture_output=True)
    return json.loads((out / "boot_report.json").read_text())


def test_insert_disk_count_unchanged(tmp_path):
    rep = _run(tmp_path, "--seconds", "5.6", "--snap-every", "0.5")
    snaps = {t["snapshot_t_s"]: t["icount"] for t in rep["timeline"] if "snapshot_t_s" in t}
    assert snaps[5.5] == 3_333_413                      # S3_PLAN §3.2 no-media frame
    assert rep["code_page_changes"]["until_mark"] == 193_692


@pytest.mark.parametrize("hle,first_read,bootblock", [(False, 2_753_521, 2_919_227),
                                                      (True, 2_729_237, 2_754_556)])
def test_synthetic_adf_phase_counts_unchanged(tmp_path, hle, first_read, bootblock):
    img = tmp_path / "synth.adf"
    img.write_bytes(adf.build_adf(FILES, bootcode=BOOTCODE))
    rep = _run(tmp_path, "--adf", str(img), "--seconds", "7", "--snap-every", "100",
               *(["--td-hle"] if hle else []))
    m = rep["wb"]["markers"]
    assert m["disk_first_read"]["icount"] == first_read   # WB_BOOT phase (i)
    assert m["bootblock"]["icount"] == bootblock           # phase (ii) ends
