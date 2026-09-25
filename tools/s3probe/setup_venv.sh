#!/bin/sh
# tools/s3probe/setup_venv.sh -- W6-D analysis venv for aros_boot_model.py.
#
# Builds machine68k 0.4.1 (Python binding of the Musashi 68000 core, used as
# a HOST-SIDE reference CPU only) into ~/.cache/amiga64/s3probe-venv, outside
# the repo, from the pinned sdist (fails closed on a hash mismatch), with one
# local patch: expose Musashi's m68k_set_irq() as CPU.set_irq(level), which
# the 0.4.1 binding does not wrap.  Nothing here is needed by the gate.
set -eu
VENV="${S3PROBE_VENV:-$HOME/.cache/amiga64/s3probe-venv}"
SDIST_URL="https://files.pythonhosted.org/packages/source/m/machine68k/machine68k-0.4.1.tar.gz"
SDIST_SHA="a58ae3f0f5f32639296e7eaf564f2aa96852462078e35b0c6d8e714d2510a9cb"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

curl -fsSL -o "$WORK/m.tar.gz" "$SDIST_URL"
got="$(shasum -a 256 "$WORK/m.tar.gz" | cut -d' ' -f1)"
[ "$got" = "$SDIST_SHA" ] || { echo "hash mismatch: $got" >&2; exit 1; }
tar -xzf "$WORK/m.tar.gz" -C "$WORK"
SRC="$WORK/machine68k-0.4.1/src"
# patch: declare + wrap m68k_set_irq
sed -i.bak 's/^  void m68k_end_timeslice()$/  void m68k_end_timeslice()\
  void m68k_set_irq(unsigned int int_level)/' "$SRC/m68k.pyx"
sed -i.bak 's/^  def pulse_reset(self):$/  def set_irq(self, unsigned int level):\
    m68k_set_irq(level)\
\
  def pulse_reset(self):/' "$SRC/pycpu.pyx"
grep -q "m68k_set_irq(unsigned int int_level)" "$SRC/m68k.pyx"
grep -q "def set_irq" "$SRC/pycpu.pyx"

python3 -m venv "$VENV"
"$VENV/bin/pip" install -q "$WORK/machine68k-0.4.1"
"$VENV/bin/python" -c "import machine68k as m; assert hasattr(m.CPU, 'set_irq'); print('machine68k with set_irq ok')"
