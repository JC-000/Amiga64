#!/usr/bin/env bash
# tools/vbcc/install.sh — install the vbcc m68k-amigaos cross toolchain
# (vbcc compiler + vasm m68k/mot + vlink + the m68k-amigaos target: startup
# code and vc.lib) OUTSIDE the repo, from the official distribution only
# (sun.hasenbraten.de / phoenix.owl.de, the vbcc/vasm/vlink authors' sites).
#
# Idempotent: re-running with everything in place only re-verifies.
# Every download is pinned by exact version + SHA256 and verified BEFORE it
# is extracted.  See tools/vbcc/README.md (licence: vbcc is free for
# non-commercial use — nothing built by this script is ever committed).
#
# Usage:  tools/vbcc/install.sh            # installs into $VBCC_PREFIX
#         VBCC_PREFIX=/some/dir tools/vbcc/install.sh
# Default prefix: ~/.local/opt/vbcc  (then: export VBCC=~/.local/opt/vbcc
#                                     PATH=$VBCC/bin:$PATH)
# Needs: curl, shasum, make, a host C compiler, lha (brew install lhasa).
set -euo pipefail

PREFIX="${VBCC_PREFIX:-$HOME/.local/opt/vbcc}"
DL="${VBCC_DL:-$HOME/.local/opt/vbcc-dl}"      # download/build cache

# name | url | sha256   (pinned 2026-09-23; vbcc 0.9h patch 3, vasm 2.0f,
# vlink 0.18a, m68k-amigaos target + unix config of the 2022-05-22 release)
PINS=(
"vbcc0_9hP3.tar.gz|http://phoenix.owl.de/tags/vbcc0_9hP3.tar.gz|cc5571f36ff63158f31b2757c5374b636e073451e147f05575307d56572d9029"
"vasm2_0f.tar.gz|http://phoenix.owl.de/tags/vasm2_0f.tar.gz|c84b2de1cbb87831795fe64a85c5d9a7002a766e3a7c30b0a2d7d5e99d878f49"
"vlink0_18a.tar.gz|http://phoenix.owl.de/tags/vlink0_18a.tar.gz|8d151cdd30a4feb575a364e68810c2bc300fe1a7c074dbbee6fd1175a6c5bfae"
"vbcc_target_m68k-amigaos.lha|http://phoenix.owl.de/vbcc/2022-05-22/vbcc_target_m68k-amigaos.lha|ec734d7115359cdb5d1c70349284ecbd5712ef47e8bc28d4150117c6f8c73289"
"vbcc_unix_config.tar.gz|http://phoenix.owl.de/vbcc/2022-05-22/vbcc_unix_config.tar.gz|c643a095439aaf0cbcf7521920c0ff5d1cb07a4fc6fe8cc59e69ae1dbb329497"
)

say() { printf '[vbcc-install] %s\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { say "missing tool: $1 ${2:-}"; exit 1; }; }
need curl; need shasum; need make; need cc
need lha "(brew install lhasa)"

sha_ok() { [ -f "$1" ] && [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ]; }

mkdir -p "$DL" "$PREFIX/bin"
for p in "${PINS[@]}"; do
  IFS='|' read -r name url sha <<<"$p"
  f="$DL/$name"
  if sha_ok "$f" "$sha"; then say "ok (cached)  $name"; continue; fi
  say "download     $url"
  curl -sSfL -o "$f.part" "$url"
  if ! sha_ok "$f.part" "$sha"; then
    say "SHA256 MISMATCH for $name — refusing to extract"
    say "  want $sha"; say "  got  $(shasum -a 256 "$f.part" | cut -d' ' -f1)"
    rm -f "$f.part"; exit 1
  fi
  mv "$f.part" "$f"; say "verified     $name"
done
# (every archive above is verified; only now is anything extracted)

SRC="$DL/src"; mkdir -p "$SRC"

# --- vasm (m68k CPU, Motorola syntax) -> vasmm68k_mot --------------------
if [ ! -x "$PREFIX/bin/vasmm68k_mot" ]; then
  rm -rf "$SRC/vasm"; tar xzf "$DL/vasm2_0f.tar.gz" -C "$SRC"
  make -C "$SRC/vasm" CPU=m68k SYNTAX=mot >/dev/null
  cp "$SRC/vasm/vasmm68k_mot" "$SRC/vasm/vobjdump" "$PREFIX/bin/"
  say "built vasmm68k_mot + vobjdump"
fi

# --- vlink ------------------------------------------------------------------
if [ ! -x "$PREFIX/bin/vlink" ]; then
  rm -rf "$SRC/vlink"; tar xzf "$DL/vlink0_18a.tar.gz" -C "$SRC"
  mkdir -p "$SRC/vlink/objects"
  make -C "$SRC/vlink" >/dev/null
  cp "$SRC/vlink/vlink" "$PREFIX/bin/"
  say "built vlink"
fi

# --- vbcc (m68k backend) -> vbccm68k, vc ---------------------------------
if [ ! -x "$PREFIX/bin/vbccm68k" ] || [ ! -x "$PREFIX/bin/vc" ]; then
  rm -rf "$SRC/vbcc"
  # macOS bsdtar warns about self-referencing hardlinks for a few non-m68k
  # backend files (alpha/ppc/6502); the regular files are still extracted.
  tar xzf "$DL/vbcc0_9hP3.tar.gz" -C "$SRC" 2>/dev/null || true
  [ -f "$SRC/vbcc/machines/m68k/machine.c" ] || { say "vbcc source extract failed"; exit 1; }
  mkdir -p "$SRC/vbcc/bin"
  # dtgen asks host-type questions; the empty answers take its defaults
  # ("cross-compiler: y" and auto-detected host types).
  # (a finite stream of newlines, not `yes`: `yes` dies of SIGPIPE, which
  # pipefail would report as a build failure)
  printf '\n%.0s' $(seq 1 200) | make -C "$SRC/vbcc" TARGET=m68k >/dev/null 2>&1 \
    || { say "vbcc build failed (rerun: make -C $SRC/vbcc TARGET=m68k)"; exit 1; }
  cp "$SRC/vbcc/bin/vbccm68k" "$SRC/vbcc/bin/vc" "$PREFIX/bin/"
  say "built vbccm68k + vc"
fi

# --- m68k-amigaos target (startup.o, vc.lib, headers) + unix config -------
if [ ! -f "$PREFIX/targets/m68k-amigaos/lib/startup.o" ]; then
  rm -rf "$SRC/target"; mkdir -p "$SRC/target"
  (cd "$SRC/target" && lha xq "$DL/vbcc_target_m68k-amigaos.lha")
  mkdir -p "$PREFIX/targets"
  rm -rf "$PREFIX/targets/m68k-amigaos"
  cp -R "$SRC/target/vbcc_target_m68k-amigaos/targets/m68k-amigaos" "$PREFIX/targets/"
  say "installed m68k-amigaos target"
fi
if [ ! -f "$PREFIX/config/aos68k" ]; then
  tar xzf "$DL/vbcc_unix_config.tar.gz" -C "$PREFIX"
  say "installed unix config (config/aos68k etc.)"
fi

# --- smoke: compile + link a trivial program --------------------------------
t="$(mktemp -d)"; trap 'rm -rf "$t"' EXIT
printf 'int main(void){return 0;}\n' >"$t/t.c"
VBCC="$PREFIX" PATH="$PREFIX/bin:$PATH" vc +aos68k -o "$t/t" "$t/t.c" >/dev/null
[ "$(head -c4 "$t/t" | od -An -tx1 | tr -d ' \n')" = "000003f3" ] \
  || { say "smoke FAILED: output is not a Hunk executable"; exit 1; }
say "ready: export VBCC=$PREFIX; export PATH=\$VBCC/bin:\$PATH; vc +aos68k ..."
