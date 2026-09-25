#!/bin/sh
# WS-A full VICE gate (docs/WAVE4_BRIEF.md C5.3), logged per step.
#   sh tests/rec_gate.sh [BUILD_DIR] [EVIDENCE_DIR]
# Assumes `make vectors` and `python3 -m host.handvec --out build/vectors-hand`
# (HAND.bin 48 cases + WRAP.bin 512K-straddle/bank-carry cases)
# have been run.  Exit status = number of failed steps.
B=${1:-build/ws-a}
E=${2:-build/evidence/ws-a/green/gate}
mkdir -p "$E"
fails=0
step() {
    name=$1; shift
    echo "\$ $*" > "$E/$name.txt"
    "$@" >> "$E/$name.txt" 2>&1
    rc=$?
    echo "exit=$rc" >> "$E/$name.txt"
    printf '%-16s exit=%s  %s\n' "$name" "$rc" "$(grep -v '^exit=' "$E/$name.txt" | tail -1)"
    [ $rc -eq 0 ] || fails=$((fails + 1))
}
step host-test   make host-test
step runtime     make runtime BUILD=$B
step vectors     python3 -m tests.run_vectors --prg $B/runner.prg --vectors build/vectors --jobs 4
step hand        python3 -m tests.run_vectors --prg $B/runner.prg --vectors build/vectors-hand
step run_program python3 -m tests.run_program --prg $B/runner.prg
step run_hello   python3 -m tests.run_hello --prg $B/runner.prg
step rescheck    make rescheck
step fetchdirect python3 -m tests.rec_build --out $B-fetchdirect FETCH_DIRECT
step rec_budget  python3 -m tests.rec_budget --prg $B/runner.prg
step bss_boot    python3 -m tests.bss_boot --prg $B/runner.prg
step rec_wrap    python3 -m tests.rec_wrap --prg $B/runner.prg
step rec_apisweep python3 -m tests.rec_apisweep --prg $B/runner.prg
[ -n "$SKIP_MATRIX" ] || step rec_matrix  python3 -m tests.rec_matrix
exit $fails
