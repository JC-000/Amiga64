#!/usr/bin/env bash
# tools/aros/fetch.sh — fetch the pinned AROS m68k ROM (main + extended
# bank) and the few AROS files the reference oracle needs, OUTSIDE the repo,
# from the official AROS nightly builds on SourceForge.
#
# Everything is pinned in tools/aros/manifest.txt: the archive by exact URL
# + SHA256 (verified BEFORE anything is extracted), and every extracted file
# by SHA256 (verified before it is installed).  Any mismatch fails closed.
#
# Target: ~/.cache/amiga64/aros/<archive sha256>/ holding aros-rom.bin,
# aros-ext.bin, C/Copy, the AROS LICENSE (APL 1.1) + ACKNOWLEDGEMENTS as
# shipped in the archive, and SOURCE.txt (where the build and its sources
# come from).  Nothing is ever written under the repo or build/.
# Idempotent: re-running with everything in place only re-verifies.
#
# Usage:  tools/aros/fetch.sh
#         AMIGA64_AROS_CACHE=/some/dir tools/aros/fetch.sh
#         AROS_KEEP_ARCHIVE=1 tools/aros/fetch.sh   # keep the 115 MB zip
#         (AROS_MANIFEST=<file> overrides the manifest: tests only)
# Needs: curl, shasum, unzip, bsdtar (macOS: built in; Linux: libarchive-tools).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${AROS_MANIFEST:-$HERE/manifest.txt}"
ROOT="${AMIGA64_AROS_CACHE:-$HOME/.cache/amiga64/aros}"

say() { printf '[aros-fetch] %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { say "missing tool: $1 ${2:-}"; exit 1; }; }
need curl; need shasum; need unzip; need bsdtar "(apt install libarchive-tools)"

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
sha_ok() { [ -f "$1" ] && [ "$(sha "$1")" = "$2" ]; }

archive_url=""; archive_sha=""; FILES=()
while IFS= read -r line; do
  case "$line" in
    archive\|*) IFS='|' read -r _ archive_url archive_sha <<<"$line" ;;
    file\|*)    FILES+=("$line") ;;
  esac
done <"$MANIFEST"
[ -n "$archive_url" ] && [ -n "$archive_sha" ] && [ "${#FILES[@]}" -gt 0 ] \
  || { say "bad manifest: $MANIFEST"; exit 1; }

DEST="$ROOT/$archive_sha"
all_ok() {
  local f
  for f in "${FILES[@]}"; do
    IFS='|' read -r _ path _member want <<<"$f"
    sha_ok "$DEST/$path" "$want" || return 1
  done
  [ -f "$DEST/SOURCE.txt" ]
}
if all_ok; then say "ok (cached)  $DEST"; exit 0; fi

# 2026-10-21: SourceForge rotates nightlies away.  When the download fails,
# install the manifest files that third_party/aros/ bundles (the ROM banks,
# LICENSE, ACKNOWLEDGEMENTS: same pinned SHA256s) and return 0.  The files
# only the archive has (C/Copy, C/Assign) stay missing, so the FS-UAE oracle
# still fails closed (tests/aros_oracle.py find_cache); a re-pin fixes that.
BUNDLE="$HERE/../../third_party/aros"
seed_from_bundle() {
  local f path want n=0
  for f in "${FILES[@]}"; do
    IFS='|' read -r _ path _member want <<<"$f"
    sha_ok "$BUNDLE/$path" "$want" || continue
    mkdir -p "$(dirname "$DEST/$path")"
    cp "$BUNDLE/$path" "$DEST/$path"; chmod 0644 "$DEST/$path"
    say "seeded       $path (third_party/aros)"; n=$((n + 1))
  done
  sha_ok "$DEST/aros-rom.bin" "$(want_of aros-rom.bin)" \
    && sha_ok "$DEST/aros-ext.bin" "$(want_of aros-ext.bin)" || return 0
  cat >"$DEST/SOURCE.txt" <<EOF
AROS m68k ROM banks seeded by tools/aros/fetch.sh from the Amiga64 repo's
third_party/aros/ (identical pinned SHA256s; see its SOURCE.txt), because
$archive_url
could not be downloaded.  Files only that archive has are absent.
Seeded:    $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
  for f in "${FILES[@]}"; do
    IFS='|' read -r _ path _ want <<<"$f"
    sha_ok "$DEST/$path" "$want" || say "MISSING      $path (archive only: the FS-UAE oracle needs a re-pin)"
  done
  say "ready (ROM only, $n file(s) from the bundle): $DEST"
  exit 0
}
want_of() {
  local f path want
  for f in "${FILES[@]}"; do
    IFS='|' read -r _ path _ want <<<"$f"
    [ "$path" = "$1" ] && { echo "$want"; return; }
  done
}

mkdir -p "$ROOT"
WORK="$(mktemp -d "$ROOT/.fetch.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
zip="$ROOT/$(basename "$archive_url")"
if sha_ok "$zip" "$archive_sha"; then
  say "ok (cached)  $(basename "$zip")"
else
  say "download     $archive_url"
  curl -sSfL --retry 3 -o "$zip.part" "$archive_url" \
    || { rm -f "$zip.part"; say "download failed (nightly rotated)"; seed_from_bundle
      say "re-pin tools/aros/manifest.txt"; exit 1; }
  if ! sha_ok "$zip.part" "$archive_sha"; then
    say "SHA256 MISMATCH for $(basename "$zip") — refusing to extract"
    say "  want $archive_sha"; say "  got  $(sha "$zip.part")"
    rm -f "$zip.part"; exit 1
  fi
  mv "$zip.part" "$zip"; say "verified     $(basename "$zip")"
fi
# (the archive is verified; only now is anything extracted)

for f in "${FILES[@]}"; do
  IFS='|' read -r _ path member want <<<"$f"
  out="$WORK/files/$path"; mkdir -p "$(dirname "$out")"
  case "$member" in
    *!*)
      iso="${member%%!*}"; inner="${member#*!}"
      if [ ! -f "$WORK/$iso" ]; then
        mkdir -p "$(dirname "$WORK/$iso")"
        unzip -p "$zip" "$iso" >"$WORK/$iso"
      fi
      bsdtar -xOf "$WORK/$iso" "$inner" >"$out" ;;
    *) unzip -p "$zip" "$member" >"$out" ;;
  esac
  if ! sha_ok "$out" "$want"; then
    say "SHA256 MISMATCH for $path ($member) — refusing to install"
    say "  want $want"; say "  got  $(sha "$out")"; exit 1
  fi
done
for f in "${FILES[@]}"; do
  IFS='|' read -r _ path _ _ <<<"$f"
  mkdir -p "$(dirname "$DEST/$path")"
  cp "$WORK/files/$path" "$DEST/$path"; chmod 0644 "$DEST/$path"
  say "installed    $path"
done
chmod 0755 "$DEST/C/"* 2>/dev/null || true
cat >"$DEST/SOURCE.txt" <<EOF
AROS m68k (amiga-m68k) nightly build, fetched by tools/aros/fetch.sh
(Amiga64 repo) for LOCAL use as a reference oracle only.  Not redistributed.

Archive:   $archive_url
SHA256:    $archive_sha
Licence:   AROS Public License 1.1 (see LICENSE in this directory, as shipped
           in the archive; third-party notices in ACKNOWLEDGEMENTS).
Sources:   https://github.com/aros-development-team/AROS (the AROS source
           tree these nightlies are built from); source snapshots are also
           published beside the binaries under
           https://sourceforge.net/projects/aros/files/nightly2/
Fetched:   $(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
if [ "${AROS_KEEP_ARCHIVE:-0}" != 1 ]; then rm -f "$zip"; fi
all_ok || { say "post-install verification failed"; exit 1; }
say "ready: $DEST"
