# AROS m68k ROM (reference oracle only, test-time only)

> **Since Wave 17** the same pinned ROM files (`aros-rom.bin`,
> `aros-ext.bin`, byte-identical to this manifest) are also **bundled** in
> [`third_party/aros/`](../../third_party/aros/), unmodified, with their
> APL 1.1 licence, acknowledgements and `SOURCE.txt`; that copy is what
> `amiga64` boots (`host/machine.find_rom` prefers it and falls back to
> this cache). `fetch.sh` remains for the oracle's FS-UAE side, which also
> needs `C/Copy` and `C/Assign` from the ISO, and as a developer cache.

`tools/aros/fetch.sh` fetches the AROS m68k ROM that the reference oracle
(`tests/aros_oracle.py`, see `docs/ORACLE.md`) boots in FS-UAE. Nothing it
fetches lives in the repository or under `build/`, and no test or gate
requires it: the oracle prints a SKIP line and exits 0 without it.

## What and where

Pinned in `tools/aros/manifest.txt` (the script and the oracle both read it):

| Item | Source | SHA256 |
|---|---|---|
| Archive: AROS nightly2 20260921, `amiga-m68k` boot ISO (zip, 115 MB) | `https://downloads.sourceforge.net/project/aros/nightly2/20260921/Binaries/AROS-20260921-amiga-m68k-boot-iso.zip` | `981817b3421a6ed245d0e259712bb876fd2e279930e4e5d2b4d55e1de7744b31` |
| `aros-rom.bin`: main bank, 512 KB at `$F80000` | ISO `boot/amiga/aros-rom.bin` | `5f6a80d73b4339134f7a00046ee77912e2c6eac342caa950f402eceda0a74272` |
| `aros-ext.bin`: extended bank, 512 KB at `$E00000` | ISO `boot/amiga/aros-ext.bin` | `33d2c1fbe732033a265159d50e7cd364d5c9668dd5fffb4120ee1f5a1af44250` |
| `C/Copy`, `C/Assign`: the two AmigaDOS commands the oracle's boot script needs that are not resident in the ROM (the oracle copies only these pinned files) | ISO `C/` | see the manifest |
| `LICENSE` (APL 1.1) and `ACKNOWLEDGEMENTS`, as shipped | zip | see the manifest |

- Cache: `~/.cache/amiga64/aros/<archive sha256>/` (override the root with
  `AMIGA64_AROS_CACHE`). The script adds `SOURCE.txt`, which records the
  archive URL, its hash, the licence and where the sources are.
- The archive is SHA256-verified **before** anything is extracted, and every
  extracted file is verified again **before** it is installed. Any mismatch
  aborts and installs nothing. The oracle re-verifies every cached file on
  each run and refuses (SKIP) a cache that does not match.
- The zip and the 410 MB ISO inside it are deleted after extraction
  (`AROS_KEEP_ARCHIVE=1` keeps the zip). Re-running is idempotent.
- `tools/aros/test_fetch.sh` runs black-box checks that `fetch.sh` fails
  closed, 14 cases, against a fake local archive with no AROS bytes: hash
  mismatches, a truncated download, an HTTP 404 through `curl -f`, a
  missing member, a stale cached zip, a corrupted install and a bad
  manifest. It runs opt-in through `host/tests/test_aros_oracle_fetch.py`
  (`AMIGA64_AROS_ORACLE=1`).
- Needs `curl`, `shasum`, `unzip` and `bsdtar` (built into macOS; on Linux
  the `libarchive-tools` package).
- **Rotation.** SourceForge keeps only a few nightlies. The 20260921 set is
  the newest one that ships the m68k boot ISO (20260925 has only the boot
  floppy). When the URL stops working, pin a newer `amiga-m68k-boot-iso`
  (every hash in the manifest), re-run the oracle and re-baseline the table
  in `docs/ORACLE.md`.

Use:

```sh
tools/aros/fetch.sh
python3 -m tests.aros_oracle --prg build/<pkg>/runner.prg
```

## Licence note

AROS is distributed under the **AROS Public License 1.1** (APL, derived from
the MPL 1.1). The full text is in the cache as `LICENSE`, exactly as the
archive ships it, next to `SOURCE.txt`.

- Only the two ROM files are redistributed, unmodified, in
  `third_party/aros/` (with the licence, the acknowledgements and
  `SOURCE.txt`). Everything else this script fetches (the ISO, `C/Copy`,
  `C/Assign`) stays in the local cache. No AROS source is in this
  repository.
- AROS sources: <https://github.com/aros-development-team/AROS> (the tree
  the nightlies are built from). Source snapshots are also published next
  to the binaries under <https://sourceforge.net/projects/aros/files/nightly2/>.
- Any further AROS material that is redistributed (for example a modified
  ROM) falls under the APL too: include the licence text, keep the
  notices, say where the source is (APL §3), and keep it in clearly
  separated files (README "Licence and third-party material").
- The project's own code stays clean-room: since Wave 10 the briefs forbid
  reading the AROS sources at all (the Wave 6 planning documents, S3_PLAN
  and WB_BOOT, still cite AROS source files from that earlier analysis).
