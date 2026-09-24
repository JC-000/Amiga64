"""HUNK_RELOC32SHORT ($3FC / $3F7-in-a-load-file) support in host/hunk.py.

vlink -Rshort — part of vbcc's stock ``+aos68k`` link line — emits the
short form as $3F7.  The fixtures here are fixturegen's RELOC32 builds
rewritten into the short form, so both encodings must parse identically.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host import fixturegen, hunk  # noqa: E402
from host.fixturegen import HunkBuilder  # noqa: E402

RELOC32SHORT = 0x3FC     # literal ids: the test must not depend on hunk.py
DREL32 = 0x3F7           # exporting them


def _u32s(blob, pos, n):
    return list(struct.unpack_from(f">{n}I", blob, pos)), pos + 4 * n


def to_short(blob: bytes, type_id: int = RELOC32SHORT) -> bytes:
    """Rewrite every HUNK_RELOC32 block of a fixturegen file into the
    16-bit short form (count, target, offsets..., 0, pad-to-longword)."""
    out = bytearray()
    pos = 0
    (magic, zero, n, first, last), pos = _u32s(blob, pos, 5)
    sizes, pos = _u32s(blob, pos, n)
    out += struct.pack(">5I", magic, zero, n, first, last)
    out += struct.pack(f">{n}I", *sizes)
    while pos < len(blob):
        (t,), pos = _u32s(blob, pos, 1)
        tid = t & 0x3FFFFFFF
        if tid in (hunk.HUNK_CODE, hunk.HUNK_DATA):
            (sz,), pos = _u32s(blob, pos, 1)
            out += struct.pack(">II", t, sz) + blob[pos:pos + 4 * (sz & 0x3FFFFFFF)]
            pos += 4 * (sz & 0x3FFFFFFF)
        elif tid == hunk.HUNK_BSS:
            (sz,), pos = _u32s(blob, pos, 1)
            out += struct.pack(">II", t, sz)
        elif tid == hunk.HUNK_END:
            out += struct.pack(">I", t)
        elif tid == hunk.HUNK_RELOC32:
            words = []
            while True:
                (cnt,), pos = _u32s(blob, pos, 1)
                if cnt == 0:
                    break
                (tgt,), pos = _u32s(blob, pos, 1)
                offs, pos = _u32s(blob, pos, cnt)
                words += [cnt, tgt, *offs]
            words.append(0)
            if len(words) % 2:
                words.append(0)
            out += struct.pack(">I", type_id) + struct.pack(f">{len(words)}H", *words)
        else:
            raise AssertionError(f"unexpected block ${tid:X}")
    return bytes(out)


def _norm(exe):
    return [(s.kind, s.data, s.alloc_size, s.relocs) for s in exe.segments]


@pytest.mark.parametrize("type_id", [RELOC32SHORT, DREL32])
@pytest.mark.parametrize("fixture", [fixturegen.fixture_code_data_reloc,
                                     fixturegen.fixture_hello])
def test_short_form_parses_like_long_form(fixture, type_id):
    long_blob = fixture()
    short_blob = to_short(long_blob, type_id)
    assert short_blob != long_blob
    assert struct.pack(">I", type_id) in short_blob
    assert _norm(hunk.parse(short_blob)) == _norm(hunk.parse(long_blob))


def _multi(offsets_a, offsets_b):
    b = HunkBuilder()
    code = b.add_code([0x4E71] * 16)            # 32 bytes
    data = b.add_data(bytes(16))
    b.add_reloc32(code, data, offsets_a)
    if offsets_b:
        b.add_reloc32(code, code, offsets_b)
    return b.build()


@pytest.mark.parametrize("a,bb", [([0], []),              # 4 words: no pad
                                  ([0, 4], []),           # 5 words: pad
                                  ([0, 8], [12, 20, 28]),  # 2 groups
                                  ([4], [0])])
def test_padding_and_multiple_targets(a, bb):
    long_blob = _multi(a, bb)
    exe = hunk.parse(to_short(long_blob))
    assert _norm(exe) == _norm(hunk.parse(long_blob))
    assert exe.segments[0].relocs[1] == sorted(a)
    if bb:
        assert exe.segments[0].relocs[0] == sorted(bb)


def test_short_offset_out_of_range_rejected():
    blob = bytearray(to_short(_multi([0], [])))
    i = blob.index(struct.pack(">I", RELOC32SHORT))
    struct.pack_into(">H", blob, i + 8, 30)      # offset 30 + 4 > 32 bytes
    with pytest.raises(hunk.HunkError, match="offset 30 out of range"):
        hunk.parse(bytes(blob))


def test_short_truncated_rejected():
    blob = to_short(_multi([0, 4], []))
    i = blob.index(struct.pack(">I", RELOC32SHORT))
    with pytest.raises(hunk.HunkError, match="truncated"):
        hunk.parse(blob[:i + 9])


def test_short_target_out_of_range_rejected():
    blob = bytearray(to_short(_multi([0], [])))
    i = blob.index(struct.pack(">I", RELOC32SHORT))
    struct.pack_into(">H", blob, i + 6, 7)
    with pytest.raises(hunk.HunkError, match="target hunk 7 out of range"):
        hunk.parse(bytes(blob))


@pytest.mark.parametrize("tid", [0x3F8, 0x3F9, 0x3FD, 0x3FE])
def test_baserel_and_other_relocs_still_fail_loudly(tid):
    blob = bytearray(to_short(_multi([0], [])))
    i = blob.index(struct.pack(">I", RELOC32SHORT))
    struct.pack_into(">I", blob, i, tid)
    with pytest.raises(hunk.HunkError, match=f"unsupported hunk type \\${tid:X}"):
        hunk.parse(bytes(blob))


VBCC = Path(os.environ.get("VBCC", Path.home() / ".local/opt/vbcc"))


@pytest.mark.skipif(not (VBCC / "bin" / "vc").exists(),
                    reason="vbcc not installed (tools/vbcc/install.sh)")
def test_real_vbcc_stock_link_uses_short_relocs(tmp_path):
    """Stock +aos68k output (vlink -Rshort) parses to the same image as a
    link without -Rshort."""
    env = dict(os.environ, VBCC=str(VBCC),
               PATH=f"{VBCC / 'bin'}{os.pathsep}{os.environ['PATH']}")
    src = ROOT / "fixtures" / "c" / "hello.c"
    stock = tmp_path / "stock"
    subprocess.run(["vc", "+aos68k", "-O2", "-o", str(stock), str(src)],
                   check=True, env=env)
    cfg = tmp_path / "longcfg"
    cfg.write_text((VBCC / "config" / "aos68k").read_text().replace(" -Rshort", ""))
    long_ = tmp_path / "long"
    subprocess.run(["vc", f"+{cfg}", "-O2", "-o", str(long_), str(src)],
                   check=True, env=env)
    sb, lb = stock.read_bytes(), long_.read_bytes()
    assert struct.pack(">I", DREL32) in sb
    assert _norm(hunk.parse(sb)) == _norm(hunk.parse(lb))
