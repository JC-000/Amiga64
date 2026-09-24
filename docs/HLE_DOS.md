# HLE DOS: dos.library file I/O (WS-B2)

Owner: WS-B2 (`m2-dos`). This covers dos.library file handles for guest
programs: Open, Close, Read, Write, Seek and IoErr, with a backend seam.
The **RAMDISK** backend is proven under VICE. The **ULTDOS** backend, which
talks to the Ultimate Command Interface, is written and pinned byte for byte
against the firmware source, but it is **hardware-UNVALIDATED**.

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
| `runtime/rc/guest.inc` | `FH_POOL`/`FH_POOL_END` (in `$000C00-$000FFF`), and the `hle` region `$F00000-$FFFFFF` (`dosio_rd_base`/`dosio_rd_end`) |

## Function table

| Function | LVO | HLE index / word | Arguments → result |
|---|---|---|---|
| Open | -30 | `$30` / `$A130` | D1 = name (C string), D2 = accessMode → D0 = BPTR, or 0 with IoErr set |
| Close | -36 | `$31` / `$A131` | D1 = fh → D0 = DOSTRUE (-1), or DOSFALSE (0) with IoErr set |
| Seek | -66 | `$32` / `$A132` | D1 = fh, D2 = position (signed), D3 = mode → D0 = **old** position, or -1 with IoErr set |
| IoErr | -132 | `$33` / `$A133` | → D0 = last error code |
| Read | -42 | WS-B1's index. It does `jmp hle_dos_read` when fh_Args ≥ `$10` | D1 = fh, D2 = buf, D3 = len → D0 = bytes read (0 at EOF), or -1 with IoErr set |
| Write | -48 | WS-B1's index. It does `jmp hle_dos_write` when fh_Args ≥ `$10` | D1 = fh, D2 = buf, D3 = len → D0 = len, or -1 with IoErr set |
| (test seam) | none | `$4E` / `$4F` | Direct entries into `hle_dos_read` / `hle_dos_write`. They are never mapped in the production env; see Testing |

Each function earns its place for a C runtime's `fopen`/`fread`/`fwrite`/`fclose`:

- Open and Close are needed for fopen/fclose.
- Read and Write are needed for fread/fwrite. The console kinds of both belong to WS-B1.
- Seek is needed for fseek/ftell, for `"a"`-mode append (Seek to OFFSET_END), and for rewind.
- IoErr is how an AmigaOS C runtime turns a failed Open/Read/Write into `errno`. It is also the only way to observe the error paths.

Nothing else was added. DeleteFile, Rename, Lock and IsInteractive are not
implemented. If WS-B1's vbcc run traps on one of them, see Limitations.

### Semantics

- **accessMode** (`dos/dos.h`):
  - MODE_OLDFILE 1005: the file must exist; it is opened read/write.
  - MODE_NEWFILE 1006: create, or truncate to 0.
  - MODE_READWRITE 1004: open, or create if missing, never truncating.
  - Any other value → 0 with ERROR_ACTION_NOT_KNOWN (209).
  - Every handle can both read and write, as on AmigaOS.
- **Names** are read through the memory API, at most 63 characters.
  - An empty name, or one with no NUL within 63 characters → ERROR_INVALID_COMPONENT_NAME (210).
  - RAMDISK names are at most 31 characters (longer → 210). They compare case-insensitively (ASCII) and are stored with case preserved.
  - Names are taken verbatim. There are no paths and no device prefixes, so `RAM:x` is a file literally named `RAM:x`.
- **Read** is clamped to `size - position`. At EOF it returns 0 and leaves IoErr unchanged. A length of 0 returns 0.
- **Write** with a length of 0 returns 0.
  - For RAMDISK, a write whose end position would exceed the file's extent fails as a whole: D0 = -1, ERROR_DISK_FULL (221), nothing is written.
  - A successful write extends the file size to the end position when that is larger.
- **Seek** modes are OFFSET_BEGINNING -1, OFFSET_CURRENT 0 and OFFSET_END 1.
  - The target must lie in 0..size. Seeking past EOF is an error, as on AmigaOS.
  - A bad mode or an out-of-range target → -1 with ERROR_SEEK_ERROR (219), and the position is unchanged.
- **Close(0)** is a harmless DOSTRUE (V36+ behaviour).
- **Invalid handles** fail with ERROR_INVALID_LOCK (211). That is DOSFALSE for Close, and -1 for Read, Write and Seek. A handle is invalid when any of these holds:
  - D1 << 2 is not exactly a pool slot address.
  - The slot is closed.
  - The guest `fh_Args` no longer reads `$10+n`.

  AmigaOS has no dedicated code for this; 211 is the project's choice.
- **Handle exhaustion**: 8 slots. A 9th Open → 0 with ERROR_NO_FREE_STORE (103).
- **IoErr** is set only by failing calls, so it is sticky like `pr_Result2`. `hle_dos_init` resets it to 0.
  - It lives in C64 BSS as `hle_dos_ioerr` (u32 LE, exported).
  - WS-B1 handlers may store into it so that console errors show up in IoErr too.
- **Multiple handles on one file** keep their own position. On RAMDISK they share the size, which is re-read from the directory on every use.

## FileHandle pool (C2)

- **Location**: slot `n` (0-7) is a 44-byte `struct FileHandle` at guest `FH_POOL + 44*n`. `FH_POOL` is `$000C00`, so the pool runs to `$000D5F`, all longword aligned.
- **BPTR**: `(addr >> 2)`, which is `$300 + 11*n`.
- **On Open**: the whole struct is written through `mem_write32`. Every field is 0 except `fh_Args` (offset 36), which is `$10+n`.
- **On Close**: `fh_Args := $FFFFFFFF`. That value is still ≥ `$10`, so a stale handle keeps routing to WS-B2 and fails cleanly with 211, instead of aliasing console kind 0.
- **Other slot state** lives in C64 BSS: the open flag, the 32-bit position, and the backend fields.

## Backend seam

`hle_dos.s` owns argument decoding, handle validation, positions, IoErr, and
the transfer loop. A backend implements nine routines. They are reached through
`dosio_vec`, which is copied from `dosio_vectab` at init. Each routine is
entered by `jmp` with X = `dosio_slot` and returns with C=0 on success. On
failure it returns C=1 with an AmigaOS code in `dosio_err`.

| Op | Contract |
|---|---|
| `init` | Once per `hle_dos_init` |
| `open` | `dosio_name`, `dosio_nlen` (1-63), `dosio_mode` (0 OLD / 1 NEW / 2 RW) |
| `close` | Release the slot's backend state |
| `size` | → `dosio_arg` = current file size |
| `setsize` | File size := `dosio_arg`. Called after a write that grew the file |
| `seek` | The backend positions itself at absolute `dosio_arg`. hle_dos.s has already bounds-checked it |
| `rdchunk` | `dosio_buf` ← file[`dosio_arg`], `dosio_len` (1-256) bytes. On return `dosio_len` = bytes delivered; fewer means EOF |
| `wrchunk` | file[`dosio_arg`] ← `dosio_buf`, `dosio_len` bytes |
| `wcheck` | Can the file grow to end position `dosio_arg`? (disk-full pre-check) |

### Chunking and C1

- **Chunk size**: guest-side transfers go in chunks of at most 256 bytes that never cross a guest page. They bounce through the 256-byte BSS buffer `dosio_buf`.
- **DMA**: `dosio_dma` programs the REC directly (`$DF02-$DF0A`, command `$DF01`) and always ends with `jsr mem_rec_dirty`, as C1 requires.
- **Fetch window**: every DMA that writes guest memory goes through `dosio_stash_guest`. That routine writes the first and last byte of the span again through `mem_write8`, with the same values.
  - The memory API's window-overlap check then drops any cached instruction page the DMA overwrote. This implements the CONTRACTS.md `w_inv` rule through the stable memory API. `w_inv` itself is not exported.
  - Spans are at most 256 bytes, so first and last byte cover both pages a span can touch.
  - This applies to guest buffers and also to RAMDISK metadata and data writes (the RAMDISK is guest space too).
- **Selection** happens in `hle_dos_init`. `dosio_uci_detect` reads `$DF1D`: `$C9` means the UCI is present (`$49` while its IRQ is pending), and gives ULTDOS (`dosio_backend = 1`). Anything else gives RAMDISK (`dosio_backend = 0`). VICE reads `$FF` there, so it gets RAMDISK; `tests/run_dos.py` asserts this.
- **Init**: BSS is not cleared at boot, so every entry first checks `hle_dos_magic == "DOS!"` and runs `hle_dos_init` lazily. `hle_dos_init` is exported and host-callable (jsr/rts) for a future runner HLE-init hook. `tests/run_dos.py` calls it between programs.

## RAMDISK format (guest/REU `$F00000-$FFFFFF`)

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
| +0 | u8 flags (bit 0 = used) |
| +1 | u8 name_len |
| +2 | u16 0 |
| +4 | u32 size |
| +8 | u32 capacity |
| +12 | u32 data (REU address) |
| +16 | 16 reserved |
| +32 | name[32] (≤ 31 characters, NUL padded) |

- **Host staging**: `hle_dos_env.ramdisk_chunks({"name": bytes, ...})` lays files out from `+$1000`, each extent page aligned with a capacity of max(len, 64 KB). Stage the chunks with the program.
- **Host read-back**: `hle_dos_env.parse_directory(read_fn)` reads files back after a run.
- **Auto-format**: an invalid header (wrong magic, version or entry count) is formatted in place on the first Open. This writes a fresh header and zeroes the 2 KB directory.
- **Guest-created files** get an extent of `RD_NEW_CAP` = 64 KB, or whatever space remains.
- **Not supported**: delete, rename, or growing past the extent.

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
| (after Open) | `t 07` FILE_INFO. The reply's first 4 bytes are the size (u32 LE), cached per slot | `"00"`. Otherwise the size is unknown and set to `$7FFFFFFF` |
| Close | `t 03` | ignored |
| Seek | `t 06 p0 p1 p2 p3` (absolute position, u32 LE). hle_dos.s has already bounds-checked it | `"00"`, else ERROR_SEEK_ERROR |
| Read chunk | `t 04 lo hi` (u16 LE, ≤ 256). Response bytes go to `dosio_buf` | Count of bytes received. The success status is empty |
| Write chunk | `t 05 00 00 payload…` (≤ 256 bytes; dos.cc ignores bytes 2-3) | `"00"`, else ERROR_DISK_FULL |
| init | ABORT, wait for idle, clear ERROR, then `1 03` and `2 03` to close anything a previous run leaked | none |

- **Target limit**: there are two DOS targets, so at most **two files** can be open at once on ULTDOS. A third Open fails with ERROR_NO_FREE_STORE. The driver never sends OPEN to a busy target, because the firmware would leak the old handle (dos.cc:113).
- **Error mapping**: any failed open → ERROR_OBJECT_NOT_FOUND (205). A transport timeout or ERROR bit on read or seek → ERROR_SEEK_ERROR (219). A write status other than `"00"` → ERROR_DISK_FULL (221).
- **Timeouts**: every wait is bounded at 65536 status polls, each including a fence. On timeout the driver aborts and fails.

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

Mutation checks (a bit-0 wait, `$06` acknowledge, CREATE_NEW for NEWFILE)
each turn tests red.

**Not proven.**

- Real timing and fence adequacy at 48/64 MHz.
- Real FILE_INFO sizes for files with pending writes.
- The firmware's read-error status. It is unreliable per dos.cc:795-798, so the driver trusts the byte count.
- Path semantics. The target's current directory defaults to `/`, so guests need names like `SD/dir/file`.
- Coexistence with the hardware test mailbox.

Hardware status: **UNVALIDATED**. The supervisor will schedule a hardware
leg (WS-A owns the device this wave).

## Testing

- `host/tests/test_hle_dos_env.py`: the LVO table and index range, collision-free merging into DOSBase, env slot staging, the `rc/guest.inc` mirrors, asm/host constant mirrors, and a RAMDISK format round-trip.
- `host/tests/test_hle_dos_uci.py`: the UCI byte-level test described above.
- `tests/run_dos.py` (VICE, RAMDISK end to end): `python3 -m tests.run_dos --prg build/ws-b2/runner.prg`.

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
- **Missing functions**: no DeleteFile, Rename, Lock/Examine or IsInteractive, and no NIL:/`*`/CONSOLE: names through Open. Add them only when a real program traps on them.
- **RAMDISK**: 32 files, 31-character names, fixed extents (64 KB for guest-created files), no deletion.
- **ULTDOS**: two open files at most; names relative to the firmware's current path; unvalidated on hardware.
- **Checker coverage**: `host/rescheck.py` `FILE_OWNERS` (supervisor-frozen) does not list `hle_dos.s`/`dosio_*.s`, so their raw-literal scan does not run. The files use only rc equates and contain no raw addresses.
