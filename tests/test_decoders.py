"""Tests for the protocol decoders, instruction decoder, float formatting,
and core signal helpers — the pure-Python analysis layer (no curses needed)."""
import json
import os

import pytest

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "demo_waveform.vcd")


# ── helpers ──────────────────────────────────────────────────────────

def _bits_msb(byte):
    """Bits of `byte`, most-significant first (SPI/I2C wire order)."""
    return [(byte >> i) & 1 for i in range(7, -1, -1)]


def _serial_byte(w, byte, edges):
    """A 1-bit data line driven to `byte` (MSB first), changing just before
    each sample edge."""
    sig = w.VCDSignal("d", "!", 1)
    sig.add_change(0, "1")
    for e, bit in zip(edges, _bits_msb(byte)):
        sig.add_change(e - 5, "1" if bit else "0")
    return sig


def _clock(w, edges, period=10):
    """A clock that produces a rising edge at each time in `edges`."""
    clk = w.VCDSignal("clk", "k", 1)
    clk.add_change(0, "0")
    for e in edges:
        clk.add_change(e, "1")
        clk.add_change(e + period // 2, "0")
    return clk


# ── Protocol decoders ────────────────────────────────────────────────

def test_uart_decode(w):
    baud, start, byte = 10, 100, 0x55
    sig = w.VCDSignal("rx", "!", 1)
    sig.add_change(0, "1")              # idle high
    sig.add_change(start, "0")          # start bit
    t = start + baud
    for i in range(8):                  # data bits, LSB first on the wire
        sig.add_change(t, "1" if (byte >> i) & 1 else "0")
        t += baud
    sig.add_change(t, "1")              # stop / idle
    frames = w.UARTDecoder(sig, baud).decode(0, 400)
    assert len(frames) == 1
    assert "0x55" in frames[0].label and "'U'" in frames[0].label


def test_spi_decode(w):
    edges = [20 + 20 * i for i in range(8)]
    cs = w.VCDSignal("cs", "c", 1)
    cs.add_change(0, "1")
    cs.add_change(10, "0")              # active low
    cs.add_change(300, "1")
    spi = w.SPIDecoder(_clock(w, edges), _serial_byte(w, 0xA5, edges),
                       cs_sig=cs, cpol=0, cpha=0)
    frames = spi.decode(0, 300)
    assert [f.label for f in frames] == ["0xA5"]


def test_i2c_decode(w):
    edges = [20 + 20 * i for i in range(8)]
    i2c = w.I2CDecoder(_serial_byte(w, 0xA5, edges), _clock(w, edges))
    frames = i2c.decode(0, 300)
    assert [f.label for f in frames] == ["0xA5"]


# ── RISC-V compressed (C extension) ──────────────────────────────────

@pytest.mark.parametrize("code,mnemonic", [
    (0x0001, "c.nop"),
    (0x0081, "c.addi"),
    (0x4081, "c.li"),
    (0x8082, "c.jr"),
])
def test_rv32_compressed(w, code, mnemonic):
    r = w.RV32Decoder().decode(code)
    assert r["mnemonic"] == mnemonic
    assert r["is_compressed"] is True
    assert r["length"] == 2


# ── RISC-V base (32-bit) decode ──────────────────────────────────────

@pytest.mark.parametrize("code,mnemonic,flag", [
    (0x123450B7, "lui", None),
    (0x12345097, "auipc", None),
    (0x008000EF, "jal", "is_jal"),
    (0x000100E7, "jalr", "is_jalr"),
    (0x00208063, "beq", "is_branch"),
    (0x0020A103, "lw", "is_load"),
    (0x00112023, "sw", "is_store"),
    (0x003100B3, "add", None),
    (0x40310133, "sub", None),
    (0x00102073, "csrrs", "is_csr"),
])
def test_rv32_base_decode(w, code, mnemonic, flag):
    r = w.RV32Decoder().decode(code)
    assert r["mnemonic"] == mnemonic
    assert r["is_compressed"] is False and r["length"] == 4
    if flag:
        assert r[flag] is True


def test_make_float_field_signals(w):
    parent = w.VCDSignal("f", "!", 32, scope="top")
    parent.add_change(0, format(0x3F800000, "032b"))
    fields = w.make_float_field_signals(parent, w.Radix.FLOAT32)
    assert [(name, sig.name, sig.size) for name, sig in fields] == [
        ("sign", "f.sign", 1),
        ("exp", "f.exp", 8),
        ("mant", "f.mant", 23),
        ("value", "f.value", 1),
    ]


# ── IEEE-754 float decode / format ───────────────────────────────────

@pytest.mark.parametrize("bits,fmt,value,special", [
    (0x3F800000, "FLOAT32", 1.0, None),
    (0xC0000000, "FLOAT32", -2.0, None),
    (0x00000000, "FLOAT32", 0.0, "+0"),
    (0x80000000, "FLOAT32", 0.0, "-0"),
    (0x3C00, "FLOAT16", 1.0, None),
])
def test_ieee754_decode(w, bits, fmt, value, special):
    fval, sign, exp_raw, exp_real, man_raw, sp = w.ieee754_decode(bits, w.Radix[fmt])
    assert fval == value
    assert sp == special


def test_ieee754_inf_nan(w):
    assert w.ieee754_decode(0x7F800000, w.Radix.FLOAT32)[5] == "inf"
    assert w.ieee754_decode(0xFF800000, w.Radix.FLOAT32)[5] == "-inf"
    assert w.ieee754_decode(0x7FC00000, w.Radix.FLOAT32)[5] == "nan"


def test_format_float_value(w):
    f = lambda bits, n=32: w.format_float_value(format(bits, f"0{n}b"), w.Radix.FLOAT32)
    assert f(0x3F800000) == "1"
    assert f(0x7FC00000) == "NaN"
    assert f(0x7F800000) == "inf"
    assert w.format_float_value("xxxx", w.Radix.FLOAT32) == "XXXX"


# ── Virtual (per-bit) signal ─────────────────────────────────────────

def test_virtual_bit_signal(w):
    bus = w.VCDSignal("data", "!", 4)
    bus.add_change(0, "1010")          # = 0b1010
    b0 = w.VirtualBitSignal(bus, 0)
    b1 = w.VirtualBitSignal(bus, 1)
    assert b0.value_at(0) == "0"       # LSB
    assert b1.value_at(0) == "1"
    assert b0.name == "data[0]" and b0.size == 1
    # x/z bits propagate
    bus.add_change(10, "10x0")
    assert w.VirtualBitSignal(bus, 1).value_at(10) == "x"


# ── Translation (enum) map ───────────────────────────────────────────

def test_translation_map(w):
    tm = w.TranslationMap()
    tm.add("state", {"0": "IDLE", "2": "STOP"})
    assert tm.translate("state", "00") == "IDLE"   # 0b00 == 0
    assert tm.translate("state", "10") == "STOP"   # 0b10 == 2
    assert tm.translate("state", "01") is None     # 0b01 == 1, unmapped
    assert tm.translate("other", "0") is None      # unknown signal


def test_translation_map_load_file(w, tmp_path):
    p = tmp_path / "tr.json"
    p.write_text(json.dumps({"fsm": {"0": "RESET", "1": "RUN"}}))
    tm = w.TranslationMap.load_file(str(p))
    assert tm.translate("fsm", "1") == "RUN"


def test_format_value_decoded(w):
    assert w.format_value_decoded("11", {"11": "RUN"}, w.Radix.HEX, 2) == "RUN"   # direct
    assert w.format_value_decoded("10", {"2": "STOP"}, w.Radix.HEX, 2) == "STOP"  # int match
    assert w.format_value_decoded("1010", {}, w.Radix.HEX, 4) == "A"             # fallback


# ── Formal assertion detection ───────────────────────────────────────

def test_formal_detect_assertion_signals(w):
    class _VCD:
        pass
    vcd = _VCD()
    vcd.signals, vcd.signal_order = {}, []
    for nm in ["assert_fifo_valid", "data", "clk", "sva_overflow"]:
        vcd.signals[nm] = w.VCDSignal(nm, nm, 1, scope="top")
        vcd.signal_order.append(nm)
    found = {name for name, _sig in w.FormalAnalyzer(vcd).detect_assertion_signals()}
    assert found == {"assert_fifo_valid", "sva_overflow"}


# ── Scope tree ───────────────────────────────────────────────────────

def test_build_scope_tree(w):
    vcd = w.VCDFile.parse(DEMO)
    root = w.build_scope_tree(vcd)
    assert "top" in root.children
    assert root.total_signals() == len(vcd.signal_order) == 19


# ── FST detection ────────────────────────────────────────────────────

@pytest.mark.parametrize("first_bytes,expected", [
    (b"\x00\x00", True),     # FST header block type 0
    (b"\xfe\x00", True),     # FST zwrapper (254)
    (b"$date", False),       # VCD
    (b"\x99zz", False),      # random
])
def test_is_fst(w, tmp_path, first_bytes, expected):
    p = tmp_path / "f.bin"
    p.write_bytes(first_bytes)
    assert w.FSTReader.is_fst(str(p)) is expected


def test_is_fst_missing_file(w):
    assert w.FSTReader.is_fst("/no/such/file.fst") is False


# ── AXI decoder primitives ───────────────────────────────────────────

def test_axi_to_int(w):
    assert w.AXIDecoder._to_int("1010") == 10
    assert w.AXIDecoder._to_int("xxxx") == 0          # undefined → default
    assert w.AXIDecoder._to_int("z1") == 0
    assert w.AXIDecoder._to_int("") == 0
    assert w.AXIDecoder._to_int("zzz", default=7) == 7


def test_axi_handshake_times(w):
    valid = w.VCDSignal("v", "v", 1)
    for t, x in [(0, "0"), (10, "1"), (30, "0")]:
        valid.add_change(t, x)
    ready = w.VCDSignal("r", "r", 1)
    for t, x in [(0, "0"), (20, "1"), (40, "0")]:
        ready.add_change(t, x)
    # Both high only at t=20 (valid since 10, ready since 20).
    assert w.AXIDecoder._handshake_times(valid, ready) == [20]
    assert w.AXIDecoder._handshake_times(None, ready) == []


# ── Signal query helpers ─────────────────────────────────────────────

def _ramp_signal(w):
    s = w.VCDSignal("x", "!", 4)
    for t, v in [(0, "0000"), (10, "0011"), (20, "1010"), (30, "0000")]:
        s.add_change(t, v)
    return s


def test_find_value(w):
    s = _ramp_signal(w)
    assert s.find_value("1010") == 20     # binary match
    assert s.find_value("3") == 10        # decimal 3 == 0b0011
    assert s.find_value("0xA") == 20      # hex 0xA == 0b1010
    assert s.find_value("1111") is None


def test_transition_count(w):
    s = _ramp_signal(w)
    assert s.transition_count_in_window(0, 30) == 4
    assert s.transition_count_in_window(15, 25) == 1


def test_numeric_value_at_binary_bus(w):
    # A pure-binary bus value is read as unsigned binary (used for analog
    # rendering + the value readout), not misparsed as decimal.
    bus = w.VCDSignal("d", "!", 8)
    bus.add_change(0, "00101010")     # 42
    bus.add_change(10, "11111111")    # 255
    assert bus.numeric_value_at(0) == 42.0
    assert bus.numeric_value_at(10) == 255.0
    assert bus.numeric_range_in_window(0, 10) == (42.0, 255.0)


def test_numeric_value_at_real(w):
    r = w.VCDSignal("temp", "r", 1, signal_type="real")
    r.add_change(0, "1.5")
    r.add_change(10, "3.0")
    assert r.numeric_value_at(0) == 1.5
    assert r.numeric_value_at(10) == 3.0
    assert r.numeric_range_in_window(0, 10) == (1.5, 3.0)


def test_find_pattern(w):
    sig = w.VCDSignal("a", "!", 4)
    for t, v in [(0, "0000"), (10, "1010"), (20, "0011")]:
        sig.add_change(t, v)
    other = w.VCDSignal("b", "#", 1)
    for t, v in [(0, "0"), (10, "1"), (20, "1")]:
        other.add_change(t, v)
    # sig==1010 while other==1 happens at t=10
    assert sig.find_pattern("1010", [(other, "1")]) == 10
    # sig==0011 while other==0 never happens (other is 1 at t=20)
    assert sig.find_pattern("0011", [(other, "0")]) is None


def test_bit_value_at(w):
    sig = w.VCDSignal("a", "!", 4)
    sig.add_change(10, "1010")          # MSB-first; LSB (bit 0) = 0
    assert sig.bit_value_at(10, 0) == "0"
    assert sig.bit_value_at(10, 1) == "1"
    assert sig.bit_value_at(10, 3) == "1"


def test_power_estimator(w):
    vcd = w.VCDFile.parse(DEMO)
    pe = w.PowerEstimator(vcd)
    activity = pe.compute(0, vcd.max_time)
    assert "top" in activity
    # clk toggles most, so it should be the hottest signal
    assert pe.hot_signals(1)[0][0].name == "clk"
    assert pe.hot_scopes(1)[0][0] == "top"
