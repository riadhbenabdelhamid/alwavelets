"""Tests for packed bus value storage: 2..64-bit non-real signals keep their
values in a flat bytearray (2 bits per logic char) instead of a string list.
Covers encode/decode round-trips, the de-pack fallback, and the consumers
that read storage directly (expanded bits, LOD rendering, sorting)."""


def _bus(w, size, changes):
    s = w.VCDSignal("b", "!", size)
    for t, v in changes:
        s.add_change(t, v)
    return s


def test_storage_kinds(w):
    assert type(w.VCDSignal("s", "!", 1)._values) is bytearray        # scalar
    b = w.VCDSignal("s", "!", 8)
    assert b._packed and type(b._values) is bytearray                 # packed bus
    r = w.VCDSignal("s", "!", 1, signal_type="real")
    assert not r._packed and type(r._values) is list                  # real
    wide = w.VCDSignal("s", "!", 128)
    assert not wide._packed and type(wide._values) is list            # >64 bits


def test_roundtrip_binary_and_4state(w):
    vals = ["00000000", "11111111", "10100101", "xxxxxxxx", "zzzzzzzz",
            "1x0z10xz", "01000000"]
    s = _bus(w, 8, list(enumerate(vals)))
    assert [s.value_str(i) for i in range(len(vals))] == vals
    assert [v for _, v in s.changes] == vals
    _, wv = s.changes_in_window(0, len(vals))
    assert wv == vals


def test_short_values_extend_per_vcd_spec(w):
    # 0/1 zero-extend; a leading x/z extends with that char.
    s = _bus(w, 8, [(0, "1"), (1, "101"), (2, "x"), (3, "z01"), (4, "X1")])
    assert s.value_str(0) == "00000001"
    assert s.value_str(1) == "00000101"
    assert s.value_str(2) == "xxxxxxxx"
    assert s.value_str(3) == "zzzzzz01"
    assert s.value_str(4) == "XXXXXXX1".lower()  # decodes lowercase


def test_uppercase_xz_normalize(w):
    s = _bus(w, 4, [(0, "1X0Z")])
    assert s.value_str(0) == "1x0z"


def test_depack_on_unpackable_value(w):
    # A value with a char outside 01xz can't pack: the signal must fall back
    # to string storage and keep every earlier value intact.
    s = _bus(w, 8, [(0, "10101010"), (10, "11110000")])
    assert s._packed
    s.add_change(20, "uuuuuuuu")                 # VHDL 'u' — unpackable
    assert not s._packed and type(s._values) is list
    assert s.value_str(0) == "10101010"
    assert s.value_str(1) == "11110000"
    assert s.value_str(2) == "uuuuuuuu"
    s.add_change(30, "00001111")                 # later values stay strings
    assert s.value_at(30) == "00001111"


def test_depack_on_overlong_value(w):
    s = _bus(w, 4, [(0, "1010")])
    s.add_change(10, "111100001111")             # wider than declared size
    assert not s._packed
    assert s.value_at(0) == "1010" and s.value_at(10) == "111100001111"


def test_wide_bus_boundaries(w):
    # 64-bit packs (16 bytes/change); every bit position survives.
    for size in (17, 33, 64):
        v1 = "1" + "0" * (size - 1)              # MSB set
        v2 = "0" * (size - 1) + "1"              # LSB set
        s = _bus(w, size, [(0, v1), (1, v2)])
        assert s._packed
        assert s.value_str(0) == v1 and s.value_str(1) == v2


def test_expanded_bits_from_packed_bus(w):
    s = _bus(w, 4, [(0, "0000"), (10, "0010"), (20, "1010"), (30, "xz10")])
    bit1 = w.VirtualBitSignal(s, 1)
    assert bit1.value_at(0) == "0"
    assert bit1.value_at(10) == "1"
    assert bit1.value_at(30) == "1"
    bit3 = w.VirtualBitSignal(s, 3)
    assert bit3.value_at(30) == "x"
    assert bit1.next_edge_after(0) == 10
    assert bit1.prev_edge_before(20) == 10
    assert bit3.find_value("1", 0) == 20
    # make_expanded_bit builds a real 1-bit signal from decoded values
    eb = s.make_expanded_bit(1)
    assert eb.value_at(15) == "1"


def test_sort_by_time_packed(w):
    s = w.VCDSignal("b", "!", 8)
    for t, v in [(20, "00000011"), (0, "00000001"), (10, "00000010")]:
        s.add_change(t, v)
    s._sort_by_time()
    assert list(s._times) == [0, 10, 20]
    assert [s.value_str(i) for i in range(3)] == \
        ["00000001", "00000010", "00000011"]


def test_bus_lod_render_packed(w):
    # draw_bus_culled's LOD path reads storage via value_str — must decode.
    s = w.VCDSignal("b", "!", 8)
    for t in range(300):
        s.add_change(t, format(t & 0xFF, "08b"))
    chars, attrs, labels = w.draw_bus_culled(s, 0, 300, 20, w.Radix.HEX)
    assert len(chars) == 20
    assert labels[0] == "00"
    assert w.CH_BUS_T in chars


def test_find_value_and_numeric_on_packed(w):
    s = _bus(w, 8, [(0, "00000000"), (10, "00101010"), (20, "11111111")])
    assert s.find_value("0x2A", 1) == 10
    assert s.numeric_value_at(10) == 42.0
    assert s.numeric_range_in_window(0, 20) == (0.0, 255.0)


def test_memory_is_actually_smaller(w):
    import sys
    strs = []
    s = w.VCDSignal("b", "!", 32)
    for t in range(10_000):
        v = format((t * 2654435761) & 0xFFFFFFFF, "032b")
        s.add_change(t, v)
        strs.append(sys.intern(v))
    packed_mem = sys.getsizeof(s._values)
    string_mem = sys.getsizeof(strs) + sum(sys.getsizeof(x) for x in set(strs))
    assert packed_mem * 4 < string_mem           # ≥4x smaller on entropy data


def test_pack_memos_released_after_parse(w, tmp_path):
    # The encode memos only pay off while values stream in; holding a filled
    # one for the process lifetime is real memory next to a packed trace.
    from alwavelets.core import _PACK_MEMOS
    path = tmp_path / "m.vcd"
    lines = ["$timescale 1ns $end", "$scope module top $end",
             '$var wire 16 " d $end', "$upscope $end", "$enddefinitions $end"]
    for t in range(300):
        lines += [f"#{t}", f'b{t & 0xFFFF:016b} "']
    path.write_text("\n".join(lines) + "\n")

    vcd = w.VCDFile.parse(str(path))
    assert sum(len(m) for m in _PACK_MEMOS.values()) == 0   # released on finish
    sig = vcd.signals['"']
    assert sig.value_str(5) == format(5, "016b")            # data still intact
    assert sig._packed

    # A later append (follow / live reload) refills only what it needs.
    sig.add_change(10_000, "1" * 16)
    assert sig.value_str(sig.num_changes() - 1) == "1" * 16
