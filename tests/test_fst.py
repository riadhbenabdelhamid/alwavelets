"""Unit tests for the native FST reader's low-level binary primitives.

The full FST parser needs a real FST fixture, but its building blocks — the
LEB128 varint / zigzag svint / C-string / big-endian u64 readers — are pure
and account for most of the format's parsing risk, so we pin them here."""
import io
import struct


def test_read_varint(w):
    R = w.FSTReader._read_varint
    assert R(b"\x00", 0) == (0, 1)
    assert R(b"\x7f", 0) == (127, 1)
    assert R(b"\xac\x02", 0) == (300, 2)        # 0x2C | (0x02 << 7)
    # continues from an arbitrary position and reports the new position
    assert R(b"\xff\xac\x02", 1) == (300, 3)


def test_read_svint_zigzag(w):
    R = w.FSTReader._read_svint
    assert R(b"\x00", 0) == (0, 1)
    assert R(b"\x01", 0) == (-1, 1)
    assert R(b"\x02", 0) == (1, 1)
    assert R(b"\x03", 0) == (-2, 1)
    assert R(b"\x04", 0) == (2, 1)


def test_read_cstring(w):
    R = w.FSTReader._read_cstring
    assert R(b"hello\x00rest", 0) == ("hello", 6)
    assert R(b"ab\x00cd\x00", 3) == ("cd", 6)
    # no terminator: consume to end
    s, pos = R(b"tail", 0)
    assert s == "tail" and pos == 5


def test_read_u64(w):
    val = 0x0102030405060708
    assert w.FSTReader._read_u64(io.BytesIO(struct.pack(">Q", val))) == val


def test_fst_varint_file(w):
    assert w.FSTReader._fst_varint_file(io.BytesIO(b"\xac\x02")) == 300
    assert w.FSTReader._fst_varint_file(io.BytesIO(b"\x7f")) == 127
