#!/usr/bin/env bash
# tools/aros/test_fetch.sh — black-box fail-closed checks of
# tools/aros/fetch.sh against a LOCAL fake archive (file:// URL, a
# bsdtar-made ISO9660 of fake files: no AROS bytes, no network except a
# throw-away 127.0.0.1 HTTP server for the 404 case).  Written by the W6-O
# adversarial reviewer, landed in fix1 (+ T11).  Run by
# host/tests/test_aros_oracle_fetch.py (opt-in: AMIGA64_AROS_ORACLE=1).
# Usage: tools/aros/test_fetch.sh <path to fetch.sh> <scratch dir>
# Needs: bash, zip, bsdtar, shasum, curl, python3.  Exit 0 = all passed.
set -u; [ -d "$2" ] && chmod -R u+w "$2"
FETCH="$1"; T="$2"; rm -rf "$T"; mkdir -p "$T"
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
mk() {  # $1 = variant dir; builds $1/A.zip
  local d="$1"; mkdir -p "$d/src/A/isoroot/boot/amiga" "$d/src/A/isoroot/C"
  printf 'ROM%.0s' {1..300} >"$d/src/A/isoroot/boot/amiga/aros-rom.bin"
  printf 'EXT%.0s' {1..300} >"$d/src/A/isoroot/boot/amiga/aros-ext.bin"
  echo copy >"$d/src/A/isoroot/C/Copy"; echo assign >"$d/src/A/isoroot/C/Assign"
  echo "APL 1.1 (fake)" >"$d/src/A/LICENSE"; echo ack >"$d/src/A/ACKNOWLEDGEMENTS"
  (cd "$d/src/A/isoroot" && bsdtar -cf ../a.iso --format iso9660 .)
  rm -rf "$d/src/A/isoroot"
  (cd "$d/src" && zip -qr ../A.zip A)
}
manifest() {  # $1 zip url, $2 archive sha, $3 dir with the good src (for member hashes), $4 optional override "path=sha"
  local s="$3/src/A" iso="$T/_iso"; chmod -R u+w "$iso" 2>/dev/null; rm -rf "$iso"; mkdir -p "$iso"; (cd "$iso" && bsdtar -xf "$s/a.iso")
  local rom ext; rom=$(sha "$iso/boot/amiga/aros-rom.bin"); ext=$(sha "$iso/boot/amiga/aros-ext.bin")
  [ "${4:-}" = "rom-bad" ] && rom=$(printf '%064d' 0)
  echo "archive|$1|$2"
  echo "file|aros-rom.bin|A/a.iso!boot/amiga/aros-rom.bin|$rom"
  echo "file|aros-ext.bin|A/a.iso!boot/amiga/aros-ext.bin|$ext"
  echo "file|LICENSE|A/LICENSE|$(sha "$s/LICENSE")"
  echo "file|ACKNOWLEDGEMENTS|A/ACKNOWLEDGEMENTS|$(sha "$s/ACKNOWLEDGEMENTS")"
  echo "file|C/Copy|A/a.iso!C/Copy|$(sha "$iso/C/Copy")"
  [ "${4:-}" = "missing-member" ] && echo "file|C/Assign|A/a.iso!C/NoSuch|$(printf '%064d' 1)" \
    || echo "file|C/Assign|A/a.iso!C/Assign|$(sha "$iso/C/Assign")"
}
pass=0; fail=0
check() { if eval "$2"; then echo "PASS $1"; pass=$((pass+1)); else echo "FAIL $1"; fail=$((fail+1)); fi; }
run() { AROS_MANIFEST="$1" AMIGA64_AROS_CACHE="$2" bash "$FETCH" >"$3" 2>&1; echo $?; }
mk "$T/good"; Z="$T/good/A.zip"; ZS=$(sha "$Z")

# T1 good install
manifest "file://$Z" "$ZS" "$T/good" >"$T/m1"; C1="$T/c1"
rc=$(run "$T/m1" "$C1" "$T/t1.log")
check "T1 good archive installs (rc=$rc)" '[ $rc = 0 ] && [ -f "$C1/$ZS/aros-rom.bin" ] && [ -f "$C1/$ZS/SOURCE.txt" ] && [ -x "$C1/$ZS/C/Copy" ]'
check "T1 archive zip deleted after install" '[ ! -e "$C1/A.zip" ]'
check "T1 no .fetch.* work dir left" '[ -z "$(ls -A "$C1" | grep "^\.fetch")" ]'
# T2 idempotent
rc=$(run "$T/m1" "$C1" "$T/t2.log"); check "T2 re-run is cached no-op (rc=$rc)" '[ $rc = 0 ] && grep -q "ok (cached)" "$T/t2.log"'
# T3 archive hash mismatch
manifest "file://$Z" "$(printf '%064d' 7)" "$T/good" >"$T/m3"; C3="$T/c3"
rc=$(run "$T/m3" "$C3" "$T/t3.log")
check "T3 archive SHA mismatch fails closed (rc=$rc)" '[ $rc != 0 ] && [ ! -d "$C3/$(printf %064d 7)" ] && grep -q "MISMATCH" "$T/t3.log"'
check "T3 no zip / .part / work dir left" '[ -z "$(ls -A "$C3")" ]'
# T4 member hash mismatch
manifest "file://$Z" "$ZS" "$T/good" rom-bad >"$T/m4"; C4="$T/c4"
rc=$(run "$T/m4" "$C4" "$T/t4.log")
check "T4 member SHA mismatch fails closed, nothing installed (rc=$rc)" '[ $rc != 0 ] && [ -z "$(ls -A "$C4/$ZS" 2>/dev/null)" ]'
# T5 partial download (truncated archive served)
head -c 2000 "$Z" >"$T/trunc.zip"; manifest "file://$T/trunc.zip" "$ZS" "$T/good" >"$T/m5"; C5="$T/c5"
rc=$(run "$T/m5" "$C5" "$T/t5.log")
check "T5 truncated download fails closed (rc=$rc)" '[ $rc != 0 ] && [ ! -d "$C5/$ZS" ] && [ -z "$(ls "$C5" | grep -e part -e zip)" ]'
# T6 404
manifest "file://$T/nope.zip" "$ZS" "$T/good" >"$T/m6"; C6="$T/c6"
rc=$(run "$T/m6" "$C6" "$T/t6.log"); check "T6 download failure fails (rc=$rc)" '[ $rc != 0 ] && [ ! -d "$C6/$ZS" ]'
# T7 corrupted installed file is repaired on re-run
echo x >>"$C1/$ZS/aros-ext.bin"; rc=$(run "$T/m1" "$C1" "$T/t7.log")
check "T7 corrupted cache file re-fetched (rc=$rc)" '[ $rc = 0 ] && grep -q "installed    aros-ext.bin" "$T/t7.log"'
# T8 member missing from the ISO
manifest "file://$Z" "$ZS" "$T/good" missing-member >"$T/m8"; C8="$T/c8"
rc=$(run "$T/m8" "$C8" "$T/t8.log"); check "T8 missing member fails, nothing installed (rc=$rc)" '[ $rc != 0 ] && [ -z "$(ls -A "$C8/$ZS" 2>/dev/null)" ]'
# T9 stale cached zip with wrong bytes at ROOT/A.zip is not trusted
C9="$T/c9"; mkdir -p "$C9"; echo junk >"$C9/A.zip"; rc=$(run "$T/m1" "$C9" "$T/t9.log")
check "T9 stale wrong cached zip re-downloaded (rc=$rc)" '[ $rc = 0 ] && grep -q "download" "$T/t9.log"'
# T10 bad manifest
echo "garbage" >"$T/m10"; rc=$(run "$T/m10" "$T/c10" "$T/t10.log"); check "T10 bad manifest fails (rc=$rc)" '[ $rc != 0 ]'
# T11 an HTTP 404 is a download failure (curl -f), not a saved error page
W="$T/www"; mkdir -p "$W"
PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
python3 -m http.server "$PORT" --bind 127.0.0.1 --directory "$W" >/dev/null 2>&1 &
SRV=$!
for _ in $(seq 50); do curl -s -o /dev/null "http://127.0.0.1:$PORT/" && break; sleep 0.1; done
manifest "http://127.0.0.1:$PORT/nope.zip" "$ZS" "$T/good" >"$T/m11"; C11="$T/c11"
rc=$(run "$T/m11" "$C11" "$T/t11.log"); kill $SRV 2>/dev/null
check "T11 HTTP 404 is 'download failed' (curl -f), nothing kept (rc=$rc)" '[ $rc != 0 ] && grep -q "download failed" "$T/t11.log" && ! grep -q MISMATCH "$T/t11.log" && [ ! -d "$C11/$ZS" ] && [ -z "$(ls "$C11" | grep -e part -e zip)" ]'
echo "$pass passed, $fail failed"
[ $fail = 0 ]
