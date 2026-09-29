#!/bin/sh
# tools/s3probe/ablate.sh -- W6-D: rerun the AROS boot-model ablation matrix.
# Each run switches ONE chipset feature off and reports whether the
# insert-disk screen is still reached within 12 virtual seconds.
# Needs tools/s3probe/setup_venv.sh and the W6-O ROM cache.
set -eu
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${S3PROBE_VENV:-$HOME/.cache/amiga64/s3probe-venv}/bin/python"
EV="$ROOT/build/evidence/w6-s3plan"
[ -f "$EV/boot/insert_disk_frames.txt" ] || { echo "run the baseline first (see docs/S3_PLAN.md)"; exit 1; }
: > "$EV/ablate/matrix.txt"
for ab in none no_blitter no_copper ciaa_tod_frozen ciab_tod_frozen beam_frozen \
          no_vertb cia_timers_frozen ciab_timers_frozen no_irq ports_irq_off \
          exter_irq_off softint_off blit_irq_off; do
    out="$EV/ablate/$ab"; mkdir -p "$out"
    arg=""; [ "$ab" = none ] || arg="--ablate $ab"
    "$PY" "$ROOT/tools/s3probe/aros_boot_model.py" --seconds 12 --snap-every 0.5 \
        --out "$out" $arg --expect "$EV/boot/insert_disk_frames.txt" > "$out/run.log" 2>&1
    python3 - "$out/boot_report.json" "$ab" >> "$EV/ablate/matrix.txt" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"{sys.argv[2]:20s} reached={r['insert_disk_reached']} instr={r['instructions']} "
      f"idle_s={r['idle_seconds']} serial_tail={r['serial'][-30:]!r}")
PY
done
cat "$EV/ablate/matrix.txt"
