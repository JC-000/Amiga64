# HLE DOS: dos.library file I/O (WS-B2)

Owner: WS-B2 (`m2-dos`; post-merge round: `m2-post`). This covers
dos.library file handles for guest programs: Open, Close, Read, Write, Seek,
IoErr, DeleteFile and ChangeMode, with a backend seam.
The **RAMDISK** backend is proven under VICE and is **always the default**.
The **ULTDOS** backend, which talks to the Ultimate Command Interface, is
written and pinned byte for byte against the firmware source, and
**hardware-VALIDATED** on the U64E (fw 3.15, 2026-09-24; see "Hardware
validation" below). It remains **opt-in only** (see Backend selection);
making it the default is a separate decision.

Binding contracts are in `docs/WAVE4_BRIEF.md`:

- C1 is REC ownership.
- C2 is the FileHandle model.
- C3 is the HLE index range `$30-$4F`.
- C4 is the guest regions.

The console half (Input/Output, console Read/Write, exec) is in `docs/HLE.md`
and is owned by WS-B1.

| File | Role |
|---|---|
| `runtime/ops/hle_dos.s` | Generic layer: dispatch, FileHandle pool, argument checks, IoErr, the chunked guest-to-backend transfer loop, the DMA helpers, and backend selection |
| `runtime/ops/dosio_ram.s` | RAMDISK backend |
| `runtime/ops/dosio_uci.s` | ULTDOS backend (UCI DOS target) |
| `host/hle_dos_env.py` | LVO table `DOS_FILE_FUNCS`, merged into DOSBase by `host/hle_env.py`. Also the guest-layout mirror, and the RAMDISK image builder and parser used to stage files and read them back |
| `runtime/rc/guest.inc` | `FH_POOL`/`FH_POOL_END` (in `$000C00-$000FFF`), and the `hle` region `$E00000-$FFFFFF`, 2 MB (`dosio_rd_base`/`dosio_rd_end`) |

## Function table

| Function | LVO | HLE index / word | Arguments → result |
|---|---|---|---|
| Open | -30 | `$30` / `$A130` | D1 = name (C string), D2 = accessMode → D0 = BPTR, or 0 with IoErr set |
| Close | -36 | `$31` / `$A131` | D1 = fh → D0 = DOSTRUE (-1), or DOSFALSE (0) with IoErr set |
| Seek | -66 | `$32` / `$A132` | D1 = fh, D2 = position (signed), D3 = mode → D0 = **old** position, or -1 with IoErr set |
| IoErr | -132 | `$33` / `$A133` | → D0 = last error code |
| DeleteFile | -72 | `$34` / `$A134` | D1 = name → D0 = DOSTRUE, or DOSFALSE with IoErr set |
| ChangeMode | -450 | `$35` / `$A135` | D1 = type, D2 = object, D3 = newmode → D0 = DOSTRUE, or DOSFALSE with IoErr set. Only `(CHANGE_FH, open file, EXCLUSIVE_LOCK)` is implemented |
| Read | -42 | WS-B1's index. It does `jmp hle_dos_read` when fh_Args ≥ `$10` | D1 = fh, D2 = buf, D3 = len → D0 = bytes read (0 at EOF), or -1 with IoErr set |
| Write | -48 | WS-B1's index. It does `jmp hle_dos_write` when fh_Args ≥ `$10` | D1 = fh, D2 = buf, D3 = len → D0 = len, or -1 with IoErr set |
| (test seam) | none | `$4E` / `$4F` | Direct entries into `hle_dos_read` / `hle_dos_write`. They are never mapped in the production env; see Testing |

Each function earns its place for a C runtime's `fopen`/`fread`/`fwrite`/`fclose`:

- Open and Close are needed for fopen/fclose.
- Read and Write are needed for fread/fwrite. The console kinds of both belong to WS-B1.
- Seek is needed for fseek/ftell, for `"a"`-mode append (Seek to OFFSET_END), and for rewind.
- IoErr is how an AmigaOS C runtime turns a failed Open/Read/Write into `errno`. It is also the only way to observe the error paths.
- DeleteFile is `remove()` (vc.lib `stdio/remove.c` calls LVO -72 and returns 0 on DOSTRUE, else 1).
- ChangeMode is what vc.lib's `fopen` calls for `"a"`, `"a+"` and `"r+"` (not for `"w"`/`"w+"`; see below).

Nothing else was added. Rename, Lock/Examine and IsInteractive (WS-B1 owns
that one) are not implemented here. If a vbcc run traps on one of them, see
Limitations.

### What vc.lib's fopen passes (disassembled, m2-post)

`vc +aos68k` links `stdio/fopen.c` (`__open_file`) from vc.lib (0.9h P3,
2022-05-22 target). Disassembled with capstone from a linked executable
(vlink `-M` map for the symbol addresses):

- Mode `'w'` → `Open(name, MODE_NEWFILE)`; `'a'` → `MODE_READWRITE` and an
  append flag; anything else (`'r'`) → `MODE_OLDFILE`. Stream flags: `'r'` →
  read, otherwise write; a `'+'` (after an optional `'b'`) adds both.
- **If the stream is writable and the Open mode is not MODE_NEWFILE** —
  that is `"a"`, `"a+"`, `"r+"`, not `"w"`/`"w+"` — it calls
  **`ChangeMode(D1 = CHANGE_FH (1), D2 = the FileHandle BPTR from Open,
  D3 = -1)`**. -1 is `EXCLUSIVE_LOCK` (= `ACCESS_WRITE`, `dos/dos.h`). On
  DOSFALSE it `Close`s the handle and fopen returns NULL.
- Then `IsInteractive(fh)`; for `'a'` finally `fseek(f, 0, SEEK_END)`
  (vc.lib's `SEEK_END` is 1 = OFFSET_END → `Seek(fh, 0, OFFSET_END)`).

That is the only ChangeMode call vc.lib makes, so it is the only one
implemented (see Semantics).

### Semantics

- **accessMode** (`dos/dos.h`):
  - MODE_OLDFILE 1005: the file must exist; it is opened read/write.
  - MODE_NEWFILE 1006: create, or truncate to 0.
  - MODE_READWRITE 1004: open, or create if missing, never truncating.
  - Any other value → 0 with ERROR_ACTION_NOT_KNOWN (209).
  - Every handle can both read and write, as on AmigaOS.
- **Open(NULL)** (D1 = 0) → 0 with ERROR_OBJECT_NOT_FOUND (205). The dos.library Open autodoc does not define a NULL name; there is no object, so 205 (review ruling 5).
- **Names** are read through the memory API, at most 63 characters.
  - An empty name, or one with no NUL within 63 characters → ERROR_INVALID_COMPONENT_NAME (210).
  - RAMDISK names are at most 31 characters (longer → 210). They compare case-insensitively (ASCII) and are stored with case preserved.
  - Names are taken verbatim, except that the **RAMDISK backend strips one leading `RAM:`** (ASCII case-insensitive; the RAMDISK is the flat root of `RAM:`), so `RAM:x`, `ram:X` and `x` are the same file. The 31-character limit applies after the strip; `RAM:` alone is an empty name (210). No other device names or paths: `RAM:dir/x` is a file literally named `dir/x`, `DF0:x` one named `DF0:x`. ULTDOS sends names verbatim (firmware paths).
- **Read** is clamped to `size - position`. At EOF it returns 0 and leaves IoErr unchanged. A length of 0 returns 0.
- **Write** with a length of 0 returns 0.
  - For RAMDISK, a write whose end position would exceed the file's extent fails as a whole: D0 = -1, ERROR_DISK_FULL (221), nothing is written. **Per-file limit: 64 KB** for guest-created files; host-staged files are limited by their staged capacity. Tested by the `diskfull` scenario.
  - A successful write extends the file size to the end position when that is larger.
- **Seek** modes are OFFSET_BEGINNING -1, OFFSET_CURRENT 0 and OFFSET_END 1.
  - The target must lie in 0..size. Seeking past EOF is an error, as on AmigaOS.
  - A bad mode or an out-of-range target → -1 with ERROR_SEEK_ERROR (219), and the position is unchanged.
- **Close(0)** is a harmless DOSTRUE (V36+ behaviour).
- **Close on a console handle** — one of the process's three staged FileHandles `FH_CONOUT`/`FH_CONIN`/`FH_NIL` (`Output()`, `Input()`, NIL:) whose `fh_Args` still reads 0-2 — is DOSTRUE, closes nothing and leaves IoErr alone, as AmigaOS programs expect from `Close(Output())`; the console keeps working. Any other non-pool BPTR is still DOSFALSE + 211 (a stray BPTR whose memory happens to hold 0 at +36 is not a console).
- **DeleteFile(name)**:
  - NULL → DOSFALSE + ERROR_OBJECT_NOT_FOUND (205); name errors as Open (210).
  - Missing file → DOSFALSE + 205. On RAMDISK an invalid header means "no files": 205, and nothing is formatted.
  - **Any handle open on the file (any mode) → DOSFALSE + ERROR_OBJECT_IN_USE (202)**, nothing changed. The generic check is `dosio_delcheck` (file id as for the sharing rules).
  - Otherwise DOSTRUE. RAMDISK: the directory entry is freed and keeps its extent for reuse (see RAMDISK format). ULTDOS: `DELETE_FILE`, see the UCI table.
- **ChangeMode(type, object, newmode)** — only the call vc.lib makes:
  - `type = CHANGE_FH (1)`, `newmode = EXCLUSIVE_LOCK (-1)`, `object` = BPTR of an open file handle (validated like Read/Write/Seek, `fh_Args` included): refused with DOSFALSE + **ERROR_OBJECT_IN_USE (202) while any *other* handle has the same file open** (the same test an exclusive MODE_NEWFILE Open applies); otherwise the handle becomes **exclusive** — every later Open of that file fails 202 until it is closed, exactly like a MODE_NEWFILE handle — and D0 = DOSTRUE. Repeating it on an already exclusive handle is DOSTRUE.
  - An invalid or console handle → DOSFALSE + ERROR_INVALID_LOCK (211).
  - Every other `type` (CHANGE_LOCK 0: there are no locks) or `newmode` (SHARED_LOCK -2, MODE_* values, …) → DOSFALSE + ERROR_ACTION_NOT_KNOWN (209). Not needed by vc.lib; add them when a program traps on it.
  - D1-D3 are preserved.
  - Effect on stdio: `fopen(f, "a"/"r+")` fails (NULL) while another stream has `f` open, and while such a stream is open every other `fopen(f, …)` fails — AmigaOS behaviour.
- **Invalid handles** fail with ERROR_INVALID_LOCK (211). That is DOSFALSE for Close, and -1 for Read, Write and Seek. A handle is invalid when any of these holds:
  - D1 << 2 is not exactly a pool slot address.
  - The slot is closed.
  - For Read, Write and Seek only: the guest `fh_Args` no longer reads `$10+n`. **Close** validates by slot address and open state alone (review ruling 8), so a guest that scribbled over `fh_Args` can still release its slot; Close then writes `fh_Args = $FFFFFFFF`.

  AmigaOS has no dedicated code for this; 211 is the project's choice.
- **Sharing (AmigaOS locking, review ruling 7)**: MODE_NEWFILE takes an exclusive lock. Open fails with 0 and ERROR_OBJECT_IN_USE (202) when:
  - it is MODE_NEWFILE and any handle has the file open, or
  - any mode, while an exclusive (MODE_NEWFILE) handle has the file open.

  MODE_OLDFILE and MODE_READWRITE share with each other. The check runs before any side effect, so a refused NEWFILE never truncates.
  - The generic check is `dosio_lockcheck` in `hle_dos.s`.
  - Identity is the backend's file id (`dosio_curfid`). On RAMDISK it is the directory index. On ULTDOS it is the name, compared case-insensitively against the other target's open file (`uci_pick_fid`).
- **Handle exhaustion**: 8 slots. A 9th Open → 0 with ERROR_NO_FREE_STORE (103).
- **IoErr** is set only by failing calls, so it is sticky like `pr_Result2`. `hle_dos_init` resets it to 0.
  - It lives in C64 BSS as `hle_dos_ioerr` (u32 LE, exported).
  - WS-B1 handlers may store into it so that console errors show up in IoErr too. `GetVar` does (205); it calls the exported `hle_dos_ensure` first, so a lazy first-call init cannot wipe the value afterwards.
- **Multiple handles on one file** keep their own position. On RAMDISK they share the size, which is re-read from the directory on every use.

## FileHandle pool (C2)

- **Location**: slot `n` (0-7) is a 44-byte `struct FileHandle` at guest `FH_POOL + 44*n`. `FH_POOL` is `$000C00`, so the pool runs to `$000D5F`, all longword aligned.
- **BPTR**: `(addr >> 2)`, which is `$300 + 11*n`.
- **On Open**: the whole struct is written through `mem_write32`. Every field is 0 except `fh_Args` (offset 36), which is `$10+n`.
- **On Close**: `fh_Args := $FFFFFFFF`. That value is still ≥ `$10`, so a stale handle keeps routing to WS-B2 and fails cleanly with 211, instead of aliasing console kind 0.
- **Other slot state** lives in C64 BSS: the open flag, the 32-bit position, and the backend fields.

## Backend seam

`hle_dos.s` owns argument decoding, handle validation, positions, IoErr, and
the transfer loop. A backend implements ten routines. They are reached through
`dosio_vec`, which is copied from `dosio_vectab` at init. Each routine is
entered by `jmp` with X = `dosio_slot` and returns with C=0 on success. On
failure it returns C=1 with an AmigaOS code in `dosio_err`.

| Op | Contract |
|---|---|
| `init` | Once per `hle_dos_init` |
| `open` | `dosio_name`, `dosio_nlen` (1-63), `dosio_mode` (0 OLD / 1 NEW / 2 RW). Once it knows which file the name refers to, and before any side effect, it must call `dosio_lockcheck` (A = file id). That call either refuses with ERROR_OBJECT_IN_USE or sets `dosio_curfid` |
| `close` | Release the slot's backend state |
| `size` | → `dosio_arg` = current file size |
| `setsize` | File size := `dosio_arg`. Called after a write that grew the file |
| `seek` | The backend positions itself at absolute `dosio_arg`. hle_dos.s has already bounds-checked it |
| `rdchunk` | `dosio_buf` ← file[`dosio_arg`], `dosio_len` (1-256) bytes. On return `dosio_len` = bytes delivered; fewer means EOF |
| `wrchunk` | file[`dosio_arg`] ← `dosio_buf`, `dosio_len` bytes |
| `wcheck` | Can the file grow to end position `dosio_arg`? (disk-full pre-check) |
| `delete` | Delete the file `dosio_name`/`dosio_nlen` (`dosio_slot` is stale: not a slot operation). Once it knows the file id, and before any side effect, it must call `dosio_delcheck` (A = file id), which refuses with ERROR_OBJECT_IN_USE while any handle has it open |

### Chunking and C1

- **Chunk size**: guest-side transfers go in chunks of at most 256 bytes that never cross a guest page. They bounce through the 256-byte BSS buffer `dosio_buf`.
- **DMA**: `dosio_dma` programs the REC directly (`$DF02-$DF0A`, command `$DF01`) and always ends with `jsr mem_rec_dirty`, as C1 requires.
- **Fetch window**: every DMA that writes guest memory goes through `dosio_stash_guest`. That routine writes the first and last byte of the span again through `mem_write8`, with the same values.
  - The memory API's window-overlap check then drops any cached instruction page the DMA overwrote. This implements the CONTRACTS.md `w_inv` rule through the stable memory API. `w_inv` itself is not exported.
  - Spans are at most 256 bytes, so first and last byte cover both pages a span can touch.
  - This applies to guest buffers and also to RAMDISK metadata and data writes (the RAMDISK is guest space too).
### 512 KB REU blocks (review item 11)

The REC's REU address counter increments only the low 19 bits (VICE `reu.c`
1085-1093; the 1541U reportedly behaves the same). A DMA that straddles
`$x7FFFF` → `$x80000` therefore wraps to the start of the same 512 KB block.

- **Guest side is safe by construction.** Every guest-memory DMA (the Read
  stash, the Write fetch) is one `dosio_chunk` of
  `req = min(rem, 256 - (ga & $FF))` bytes starting at `ga`. So it lies
  inside the single 256-byte page `ga & ~$FF .. ga | $FF`. Every 512 KB
  boundary is a multiple of 256, so it can only fall *between* pages, never
  inside one.
- **RAMDISK metadata is safe by placement.** The header (32 bytes at
  `$E00000`) and the directory (`$E00020-$E0081F`, including the 8 pages
  `rd_format` zeroes) sit at the start of block `$E00000-$E7FFFF`.
- **RAMDISK file data was NOT safe.** Extents start at `$E01000 + k·64 KB`, so
  the boundaries `$E80000`, `$F00000` and `$F80000` fall inside extents. A
  chunk's REU-side address is `extent + position`, which is not
  page-aligned relative to the guest buffer, so a chunk could straddle a
  boundary.
  - Reproduced: the `wrap512` scenario went RED. The write wrapped into
    `$E00000` and destroyed the directory
    (`build/evidence/ws-b2/fix1/red/run_dos_wrap512.txt`).
  - Fixed in `dosio_dma`: when a transfer starts in a block's last 64 KB
    and is longer than the room left, it is split into two DMAs at the
    boundary. This covers every caller (guest and RAMDISK sides), and the
    caller's arguments are preserved.
- **Host test reads** (`tests/run_dos.py` `Guest.read`) split at 512 KB
  blocks too.
- **Test.** The `wrap512` scenario reads and writes a file through a guest
  buffer straddling `$180000` (`$17FF80`, 256 bytes), and reads and writes
  file data straddling `$E80000` (odd guest buffer). It checks:
  - the bytes on both sides of each boundary;
  - that the sentinels at `$100000-$10007F` and `$180080-$1800FF` are
    untouched;
  - that the RAMDISK header and every file survive.

  The `nosplit` mutant (split disabled) turns it red **through the
  RAMDISK-side legs only** (file data across `$E80000`). The guest-buffer
  legs (steps 6 and 9) cannot exercise the split: guest chunks are
  page-bounded, so no guest-side DMA ever straddles `$180000`, split or not
  (m2-post fix1 F3). They stay as regression coverage of the page-bounded
  chunking. `run_vbcc`'s `fio_big` exercises the split from C the same way:
  big.bin's extent is staged across `$E80000` (see `docs/HLE.md`).

### Backend selection (review ruling 1)

`hle_dos_init` picks the backend. **RAMDISK (`dosio_backend = 0`) is always
the default.** ULTDOS (`dosio_backend = 1`) is used only when both hold:

1. The host wrote the 4-byte key `"UCI!"` (`hle_dos_env.UCI_OPT_IN_KEY`) to the exported BSS symbol `dosio_want_uci`. The key is read **only when `hle_dos_init` runs**, which is either the first HLE DOS call after boot (lazy init) or an explicit host `jsr hle_dos_init`. So write it before the first dos call of the boot, or call `hle_dos_init` after writing it. Writing it mid-run has no effect until the next init. Nothing ever clears the key; to opt out again, overwrite it and re-run `hle_dos_init`.
2. `dosio_uci_detect` sees the UCI: `$DF1D` reads `$C9` (`$49` while its IRQ is pending).

The opt-in is a 4-byte key rather than a flag byte because BSS holds
power-on garbage (it is never cleared). A garbage nonzero byte must not
switch a program onto the UCI backend. The backend passed its hardware leg
(2026-09-24) but stays opt-in until a default change is decided.

`tests/run_dos.py`'s `selection` leg builds a TEST-ONLY `DOSIO_FAKE_UCI`
variant whose detection always reports a UCI. On that build it checks:

- the boot state, `want = 0`, `$01010101`, `"UCI?"` and `$FFFFFFFF` all give RAMDISK;
- the `writes` scenario runs correctly;
- the key gives ULTDOS;
- clearing the key gives RAMDISK again.

The plain VICE build reads `$FF` at `$DF1D` and is asserted to use RAMDISK.

### Init

BSS is not cleared at boot, so every entry first checks `hle_dos_magic == "DOS!"` and runs `hle_dos_init` lazily. `hle_dos_init` is exported and host-callable (jsr/rts) for a future runner HLE-init hook. `tests/run_dos.py` calls it between programs.

## RAMDISK format (guest/REU `$E00000-$FFFFFF`, 2 MB)

This is a flat, non-hierarchical store. All integers are **little-endian**:
the store is C64-side metadata that the guest never sees.

| Offset | Size | Field |
|---|---|---|
| `+$0000` | 4 | magic `"RDSK"` |
| `+$0004` | 1 | version = 1 |
| `+$0005` | 1 | number of directory entries = 32 |
| `+$0006` | 2 | 0 |
| `+$0008` | 4 | `free_ptr`: next unallocated REU address (data area) |
| `+$000C` | 4 | `data_end`: `dosio_rd_end + 1` = `$01000000` |
| `+$0010` | 16 | reserved |
| `+$0020` | 32 × 64 | directory entries (below) |
| `+$1000` | … | file data, one contiguous extent per file, never moved |

Each directory entry (64 bytes) has this layout:

| Offset | Field |
|---|---|
| +0 | u8 flags (bit 0 = used, bit 1 = owns an extent: set on every create, kept after a delete) |
| +1 | u8 name_len |
| +2 | u16 0 |
| +4 | u32 size |
| +8 | u32 capacity |
| +12 | u32 data (REU address) |
| +16 | 16 reserved |
| +32 | name[32] (≤ 31 characters, NUL padded) |

- **Host staging**: `hle_dos_env.ramdisk_chunks({"name": bytes, ...})` lays files out from `+$1000`, each extent page aligned with a capacity of max(len, 64 KB). Stage the chunks with the program.
- **Host read-back**: `hle_dos_env.parse_directory(read_fn)` reads files back after a run.
- **Auto-format**: an invalid header (wrong magic, version or entry count) is formatted in place on the next Open. This writes a fresh header and zeroes the 2 KB directory.
  - **Never while a handle is open** (review fix2 item 1). A format would wipe and reuse the directory entries those handles still point at. While any handle is open, an Open that finds an invalid header fails with 0 and ERROR_NOT_A_DOS_DISK (225), and leaves the header untouched. The format happens on the first Open after every handle is closed. Tested by the `fmtlock` scenario.
  - Every `dosio_lockcheck` call handles the refusal carry. A create can only be refused if the guest scribbled on the directory under an open handle, and then the Open fails and changes nothing.
- **Guest-created files** get an extent of `RD_NEW_CAP` = 64 KB, or whatever space remains. 32 × 64 KB is exactly the 2 MB region, and the 4 KB header/directory page comes out of it. All 32 directory entries are allocatable, but the 32nd guest-created extent is 60 KB (tested by `dirfull`).
  - This is where ruling 6's "32 × 64 KB" comes up 4 KB short. Growing the region further would reach into `$DFxxxx`, which the ruling did not grant.
- **Delete and space reuse (m2-post)**: DeleteFile rewrites the entry's flags to `$02` (not used, extent kept); name, size, capacity and data address stay. A create picks the first unused entry as before. It takes over that entry's extent — capacity included, `free_ptr` left alone — only when all of these hold (`rd_ext_ok`):
  - bit 1 is set;
  - the extent lies inside the data area: `RD_DATA <= data` and `data + capacity <= $1000000` with no 32-bit carry, so a guest that scribbled on the directory cannot aim writes elsewhere (`extscribble`, `exthi`, `extcarry`, `exthi64`, `extcarry64`);
  - **the capacity is nonzero and at least `min(64 KB, data_end - free_ptr)`** (fix1 N2): a create never gets less room than a fresh extent would give it (`smallext`).

  Otherwise it allocates from `free_ptr` as before, and a too-small extent is abandoned (leaked until the next format). So a full RAMDISK (32 entries, or 2 MB) gets both an entry and 64 KB back from each delete (`delreuse`, `fio_reuse`); when fresh space is short, a deleted extent at least that large is still reused (the 60 KB last extent qualifies once `free_ptr` is exhausted).
- **Not supported**: rename, growing past the extent, freeing an extent back to `free_ptr` (extents are only reused through their directory entry).

## ULTDOS backend: UCI mapping (HARDWARE-UNVALIDATED)

**Sources followed.** Firmware is `~/Documents/1541ultimate` at `880de4dd`.
Hardware-tested 6502 is `~/Documents/c64-https` at `1b9b49e`.

- **Registers**: `fpga/io/command_interface/vhdl_source/command_protocol.vhd`.
  - Status bits: lines 54-61.
  - Read mux: lines 97-106. `$DF1D` reads `$C9`.
  - Control writes: lines 148-170. PUSH (bit 0) also latches bits 7-5, so we write exactly `$01`. DATA_ACC is bit 1, ABORT is bit 2, and bit 3 clears ERROR.
  - Queue pointers: lines 176-185.
- **Reply timing**: `software/io/command_interface/command_intf.cc` lines 141-191. Bit 0 clears *before* `copy_result` fills the queues. The driver therefore waits for state bit 5 or ERROR (`status & $28`), never for bit 0. c64-test-harness `uci_network.py` 125-133 (issue #486) states the same rule.
- **DOS target**: `software/filemanager/dos.h` lines 11-38, and `dos.cc`:
  - Lines 12-13: two targets, 1 and 2, each with one open file.
  - Lines 111-153: OPEN, CLOSE, SEEK.
  - Lines 154-182: FILE_INFO.
  - Lines 467-492: READ_DATA, WRITE_DATA.
  - Lines 784-802: read blocks of at most 512 bytes; the success status is empty.
- **Open flags**: `software/filesystem/fs_errors_flags.h` 44-53. Semantics: `software/filemanager/filemanager.cc` `fopen_impl` 668-689.
- **Transaction shape**: c64-https `src/net/uci/uci_cmd.s`, with drain_resp → drain_status → a single DATA_ACC (header 27-29), abort at 69, and push_wait at 289. The turbo fence constants (5 × 217) come from `uci_regs.inc` 93-109. Our fence adds a `sec` per iteration, which makes it at least as long as the proven one.

| Operation | UCI command bytes (target t = 1 or 2) | Success test |
|---|---|---|
| Open OLDFILE | `t 02 03 name…` (FA_READ\|FA_WRITE). On failure, retry `t 02 01 name…` for read-only media | status starts `"00"` |
| Open NEWFILE | `t 02 0B name…` (FA_READ\|FA_WRITE\|FA_CREATE_ALWAYS: create or truncate) | `"00"` |
| Open READWRITE | `t 02 13 name…` (FA_READ\|FA_WRITE\|FA_OPEN_ALWAYS) | `"00"` |
| (after Open) | `t 07` FILE_INFO. The reply's first 4 bytes are the size (u32 LE), cached per slot | `"00"` with at least 4 bytes. Otherwise the Open **fails**: `t 03` closes the target and IoErr = ERROR_OBJECT_NOT_FOUND (205). No unknown-size handles (review ruling 4) |
| Close | `t 03` | ignored |
| Seek | `t 06 p0 p1 p2 p3` (absolute position, u32 LE). hle_dos.s has already bounds-checked it | `"00"`, else ERROR_SEEK_ERROR |
| Read chunk | `t 04 lo hi` (u16 LE, ≤ 256). Response bytes go to `dosio_buf` | Count of bytes received. The success status is empty |
| Write chunk | `t 05 00 00 payload…` (≤ 256 bytes; dos.cc ignores bytes 2-3) | `"00"`, else ERROR_DISK_FULL |
| init | ABORT, wait for idle, clear ERROR, then `1 03` and `2 03` to close anything a previous run leaked | none |
| DeleteFile | `1 09 name…` (`DOS_CMD_DELETE_FILE`, `dos.h` 19, `dos.cc` 208-221; always target 1 — it never touches the target's open file). Sent only after `dosio_delcheck` passed for the name's file id (`uci_pick_fid`) | `"00"`; status `WRITE PROTECTED` (FatFS `FR_WRITE_PROTECTED`, `file_system.cc` 50-51; the only error string starting `WR`, checked on the first two status bytes) → ERROR_DISK_WRITE_PROTECTED (214); any other status → ERROR_OBJECT_NOT_FOUND (205; e.g. `FILE DOESN'T EXIST`, `ACCESS DENIED`, `file_system.cc` 28-60); no reply → ERROR_DEVICE_NOT_MOUNTED (218), the file may or may not be gone |

- **Target limit**: there are two DOS targets, so at most **two files** can be open at once on ULTDOS. A third Open fails with ERROR_NO_FREE_STORE. The driver never sends OPEN to a busy target, because the firmware would leak the old handle (dos.cc:113).
- **Error mapping**:
  - An Open refused with a definitive status → ERROR_OBJECT_NOT_FOUND (205).
  - A sharing conflict → ERROR_OBJECT_IN_USE (202), with nothing sent.
  - A transport timeout or ERROR bit on read or seek → ERROR_SEEK_ERROR (219).
  - A write status other than `"00"` → ERROR_DISK_FULL (221).
- **Stale ERROR bit (review ruling 3)**: `uci_begin` writes `$08` (clear ERROR) once the interface is idle, before the command bytes. The only setter of that bit is a PUSH while busy (VHDL 155-160). A bit left by someone else therefore can no longer make our accepted PUSH look refused.
- **No double OPEN (review ruling 3)**: the MODE_OLDFILE read-only retry happens only after a *definitive* refusal, that is, a reply with a non-`"00"` status.
  - If the OPEN transaction itself failed (timeout, refused push), the firmware may already have the file open.
  - In that case the driver sends `t 03` and fails with ERROR_DEVICE_NOT_MOUNTED (218). It never sends a second OPEN to that target.
- **Timeouts**: every wait is bounded at 256 × `DOSIO_UCI_POLLS_HI` status polls (default 65536), each including a fence. On timeout the driver aborts and fails.
- **Throughput (review nit 9)**: roughly **65 bytes/s at 1 MHz** (measured on the U64E: read 61 B/s, write 114 B/s at 1 MHz; read ~2.7 KB/s, write ~4.8-5.1 KB/s at 48 MHz).
  - Every UCI register access is followed by the turbo fence from c64-https: 5 × 217 loop iterations, about 7.6k cycles, about 5.5 ms at 1 MHz.
  - Each payload byte costs about two register accesses (data plus status poll).
  - Speed scales with the CPU clock, to about 3 KB/s at 48 MHz. The fence is tuned for correctness at turbo speed, not for throughput.

**What is pinned.** `host/tests/test_hle_dos_uci.py` assembles
`dosio_uci.s` standalone with cl65 and **executes** it in a small 6502
emulator. `$DF1C-$DF1F` are wired to a model of the VHDL handshake and the
dos.cc/filemanager parser, including the "bit 0 clears before the queues are
valid" hazard, with a configurable latency. The tests assert:

- the exact command bytes for every operation;
- PUSH written only as `$01`;
- no PUSH while busy;
- one DATA_ACC per reply;
- no OPEN on a busy target;
- no seek past EOF;
- the resulting file-system state.

The fix round added tests for:

- a stale ERROR bit before a push;
- a lost OPEN reply, which must not lead to a second OPEN;
- a read-only retry after a definitive refusal;
- NEWFILE never being retried read-only;
- a FILE_INFO failure failing the Open;
- the sharing check preceding the OPEN, with file ids following names.

Each mutation check turns tests red. The original ones are a bit-0 wait,
a `$06` acknowledge and CREATE_NEW for NEWFILE; the fix-round mutants are
listed in `build/evidence/ws-b2/fix1/mutants.txt`.

**Not proven.**

- Real timing and fence adequacy at 48/64 MHz.
- Real FILE_INFO sizes for files with pending writes.
- The firmware's read-error status. It is unreliable per dos.cc:795-798, so the driver trusts the byte count.
- Path semantics. The target's current directory defaults to `/`, so guests need names like `SD/dir/file`.
- Coexistence with the hardware test mailbox.

Hardware status: **VALIDATED** (2026-09-24). See "Hardware validation".

## Hardware validation (U64E, fw 3.15, 2026-09-24)

First run of the ULTDOS backend on silicon, opted in with the `"UCI!"` key,
Command Interface enabled for the session only (runtime config, never saved;
full-config re-read afterwards IDENTICAL). All writes were confined to a
scratch folder `/USB1/amiga64-test` that did not exist beforehand; it was
removed afterwards and the `/USB1` top-level listing was identical before
and after (76 entries, nothing added or removed).

| Scenario | Result |
|---|---|
| `u_main` (37 steps: missing-file 205, NEWFILE create, 300-byte write from an odd buffer, zero-length write, Seek BEGINNING/CURRENT/END incl. negative offsets and old-position returns, read-back into an unaligned buffer, EOF, read clamp, overwrite, reopen OLDFILE, READWRITE create on the second target, third Open refused 103, delete-while-open 202, NEWFILE vs open 202, OLDFILE sharing with independent positions, NEWFILE exclusivity vs OLD/RW 202, delete-missing 205) | 56/56 checks |
| `u_delete` (delete, reopen gone 205, delete again 205) | 9/9 checks |
| Throughput (`tp_*`, 2 reps each at 1 and 48 MHz) | all checks ok |
| Independent FTP read-back of `a.bin` (300 B), `b.bin` (10 B), `t.bin` (1024 B at both speeds) | byte-identical |

Guest paths use the medium name without a leading slash: `USB1/amiga64-test/x`
(`/USB1`, `usb1` and `Usb1` resolve the same; an unknown root answers
`PATH DOESN'T EXIST`).

Model-vs-silicon notes (none affected results):
- Firmware 3.15 answers FatFS errors without the numeric `NN,` prefix:
  `FILE DOESN'T EXIST`, `PATH DOESN'T EXIST` (`file_system.cc`
  `get_error_string`). Its fixed DOS messages keep the prefix:
  `84,NO FILE TO CLOSE`, `85,NO FILE OPEN`, `82,FILE NOT FOUND`
  (`dos.cc` lines 16-30). No DOS-target error has both forms, in
  `~/Documents/1541ultimate` @ bce4535e (the device's build) or in
  `~/Documents/1541u-315preview`. The driver treats any non-`00` status as
  failure, so the mapping held.
- W5-T (wave 5) aligned the Python model in `host/tests/test_hle_dos_uci.py`
  with fw 3.15. The model already used the unprefixed FatFS texts. It now
  also models:
  - missing directories, which answer `PATH DOESN'T EXIST` before any
    create flag is honoured;
  - a failed FILE_INFO, which answers `82,FILE NOT FOUND` (the model used
    `88,NO INFORMATION AVAILABLE` before);
  - a DELETE of an open file, which succeeds (`FF_FS_LOCK` is 0). The
    driver never sends one, because `dosio_delcheck` refuses first.

  The driver, unchanged, passes all 42 tests against the updated model:
  missing directory → 205 in every open mode and on delete.
- A read error is not reported reliably by the firmware: `get_more_data`
  (`dos.cc` 795-801) fills the status text but never points the reply at
  it, so the previous status goes out. The driver does not rely on the
  read status (see `dosio_uci_rdchunk`).
- `FILE_STAT` on an existing file by its guest-relative name answered
  `82,FILE NOT FOUND` in a raw probe. The driver does not use FILE_STAT
  (it uses FILE_INFO on the open target), so this is informational.
- `IDENTIFY` answers `ULTIMATE-II DOS V1.2`; `GET_PATH` answers `/`.

## Testing

- `host/tests/test_hle_dos_env.py`: the LVO table and index range, collision-free merging into DOSBase, env slot staging, the `rc/guest.inc` mirrors, asm/host constant mirrors, and a RAMDISK format round-trip.
- `host/tests/test_hle_dos_uci.py`: the UCI byte-level test described above.
- `tests/run_dos.py`: RAMDISK end to end in VICE, plus the backend-selection leg on a second VICE instance. Run it with `python3 -m tests.run_dos --prg build/ws-b2/runner.prg`. The PRG's directory must contain `gen/dispatch.s`, as `make runtime` leaves it, for the `DOSIO_FAKE_UCI` variant build.
- `tests/run_dos.py --backend u64` (W5-T): the same scenarios on the Ultimate 64 at `U64_HOST`, at 1 MHz, in one DeviceLock hold with a full-config snapshot/restore (`tests/bench/u64_session.u64_hold`, JSON in `--out-dir`). It is RAMDISK only:
  - It fails closed. Every preflight failure below aborts the run before the first scenario; the device config is still restored.
  - It refuses to run unless the session's config snapshot shows `Command Interface` = `Disabled`. A missing item counts as not Disabled.
  - After boot it zeroes `dosio_want_uci` and `hle_dos_magic` and proves both by readback. Before the wave-5 boot-stub fix (supervisor, `tests/bss_boot.py`), C64 BSS was not cleared at boot on hardware: a residue of `"UCI!"` would select ULTDOS, and a residue of `"DOS!"` would skip the lazy init and keep stale backend vectors. The boot stub now clears BSS; the zeroing stays as defence in depth.
  - It runs `hle_dos_init` once and proves that it selected RAMDISK, then zeroes `hle_dos_magic` again, so the first scenario still starts with a lazy init.
  - It checks that `dosio_backend` reads RAMDISK after every scenario.
  - The `selection` leg never touches the device, whatever `--backend` says: it boots its own `DOSIO_FAKE_UCI` build in a local VICE instance. With `--backend u64` it runs there after the hardware leg, and its checks are labelled `[local VICE]`.

  Results are in `docs/HARDWARE.md`, section "Wave 5: run_dos on hardware".

### Why `run_dos.py` uses a test env

Until WS-B1's hle.s routes file kinds to `hle_dos_read`/`hle_dos_write` (C2), a
guest Read or Write through DOSBase never reaches WS-B2. The driver therefore
stages the production env from `hle_env.env_chunks()`, which already carries
Open/Close/Seek/IoErr. It then **overrides** the Read (-42) and Write (-48)
slots with `$A14E`/`$A14F`.

### The guest program

The guest is a hand-encoded "call script" interpreter. It walks a table of
(LVO, D1 literal or "D0 of step k", D2, D3) entries. For each step it records:

- D0;
- IoErr();
- the byte at [D2], read back through the memory API right after the call;
- D1.

### Scenarios

| Scenario | Covers |
|---|---|
| `reads` | Missing file with IoErr; reads of more than 256 bytes into odd buffers; zero-length read; EOF and reads past EOF; Seek in all three modes returning the old position; bad mode, negative and past-EOF seeks; double Close; Read/Write/Seek on a closed handle; Close(0); bogus and mid-slot BPTRs; case-insensitive names; name too long; bad mode. Starts without an init call, so it exercises lazy init |
| `writes` | Auto-format of an invalid header; NEWFILE create; unaligned write of 300 bytes, then read back; overwrite in the middle; size persistence across reopen; OLDFILE append; READWRITE create, and open-existing without truncation; NEWFILE truncation. The host reads every file back from the RAMDISK |
| `handles` | 8 handles, then exhaustion (103); slot reuse; independent positions; shared size across handles |
| `codepage` | A Read over the executing code page. Only passes if the fetch window was invalidated. With a `DOSIO_NOINVAL` build it FAILS (teeth) |
| `diskfull` | The 64 KB extent filled exactly; one more byte, and a length of -1, fail with 221 and write nothing; Seek to END+1 fails; a Read of length -1 is clamped to the file |
| `dirfull` | All 32 directory entries allocatable in the 2 MB region; the 33rd create fails with 221; the last (60 KB) extent is usable |
| `staledir` | A directory with a used entry but an invalid header. Auto-format must wipe the entry, so the stale name is not found and a new file of that name is empty |
| `lock` | OLDFILE and READWRITE share; NEWFILE refused (202) while any handle is open, with provably zero side effects (Seek END/BEG shows the unchanged size, and a Read shows the unchanged bytes; the `lockorder` mutant goes red); every Open refused while a NEWFILE handle is open; released on Close; case-insensitive identity |
| `dirscribble` | The guest zeroes the directory flag of a file it holds exclusively. A second NEWFILE of that name creates at the same index (the same file id), the lockcheck refuses, and the Open fails with 202 and writes nothing (the `nocarry` mutant goes red) |
| `fmtlock` | The guest clobbers the RAMDISK magic while two handles are open. Opens then fail with 225 and leave the header alone; the open handles keep working; after they are closed, the format happens and a new file persists (reviewer's `adv_fmt`) |
| `fhargs` | The guest overwrites `fh_Args` through a Read into the handle pool. Read, Write and Seek refuse (211), Close still releases the slot, and the slot is reusable |
| `nullname` | Open(NULL) → 0 with 205 |
| `delete` | DeleteFile: missing (205), open file (202, nothing changed), after Close (DOSTRUE, then Open fails 205), `RAM:`/`ram:` prefixes on DeleteFile and Open, `RAM:` alone (210), NULL (205), a 31-character name behind `Ram:`; re-created files take over the deleted entries' extents (addresses checked) |
| `delreuse` | 32 files fill the directory; the 33rd create fails 221; DeleteFile of one; the create now succeeds in the freed entry *and extent* (address checked) and takes a full 64 KB; the next create fails 221 again |
| `extscribble`, `exthi`, `extcarry`, `exthi64`, `extcarry64` | A forged "deleted, keeps extent" entry whose extent lies below `RD_DATA`, past `$FFFFFF`, or whose end overflows 32 bits is not reused: the create allocates from `free_ptr` (address checked). `exthi64`/`extcarry64` use a 64 KB capacity so the N2 capacity rule cannot hide a broken range check (`m2_extupper` goes red on `exthi64`) |
| `smallext` | A deleted host-staged extent of capacity `$100`, and one of capacity 0, are not reused (fresh extents, a 512-byte write fits); a deleted 64 KB extent is |
| `changemode` | ChangeMode(CHANGE_FH, fh, EXCLUSIVE_LOCK) on a sole handle → DOSTRUE and every Open then fails 202; repeated → DOSTRUE; released by Close; refused 202 while another handle shares the file; CHANGE_LOCK and SHARED_LOCK → 209; a closed handle → 211 |
| `selection` | Backend selection, described above |

### REC-shadow leg

The per-step `[D2]` read-back (guest `mem_read8` right after a DMA Read), and
the memory-API calls that follow every Write's DMA, are the checks that will
catch a missing `mem_rec_dirty` once WS-A's REC shadow exists. They cannot go
red today, because `mem_rec_dirty` is still `rts` and the memory API
reprograms every register.

## Limitations / open items

- **Integration**: needs WS-B1's hle.s routing for kinds ≥ `$10`.
  - Kind `$FFFFFFFF` (a closed handle) must also route here.
  - The supervisor's end-to-end fopen test must stage a RAMDISK with `hle_dos_env.ramdisk_chunks` if the program opens existing files. It is not needed for NEWFILE, because of auto-format.
- **Init**: there is no runner HLE-init hook yet. Handles leak across programs run in one boot unless something calls `hle_dos_init`.
- **Missing functions**: no Rename, Lock/Examine, and no NIL:/`*`/CONSOLE: names through Open; ChangeMode only for the vc.lib case. Add them only when a real program traps on them.
- **RAMDISK**: 32 files, 31-character names (after `RAM:`), fixed extents (64 KB for guest-created files, 60 KB for the 32nd), reused only through a deleted file's directory entry.
- **ULTDOS DeleteFile** is pinned against the firmware model and validated on the U64E (2026-09-24).
- **ULTDOS file identity is the name string** (inherited from Open's `uci_pick_fid`): `a` and `/a` (or any two spellings of one path) get different file ids, so the sharing rules and DeleteFile's open-file check can be bypassed by spelling the same file differently. Not changed; the firmware keeps its own lock state.
- **ULTDOS**: opt-in only; two open files at most; names relative to the firmware's current path (`USB1/...`); about 61-114 B/s at 1 MHz, ~2.7-5 KB/s at 48 MHz; validated on hardware 2026-09-24.
- **Checker coverage**: `host/rescheck.py` scans `hle_dos.s` and `dosio_*.s` (owner `hle`, via `OWNER_GLOBS`); plain-value constants carry `; @value`.
