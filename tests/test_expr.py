"""Tests for the cross-signal expression search engine."""
import pytest

VCD = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! valid $end
$var wire 4 " state $end
$var wire 8 # addr $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
b0 "
b0 #
$end
#10
1!
b11 "
b1000000 #
#20
0!
b101 "
b10000000 #
#30
1!
b11 "
"""


@pytest.fixture
def vcd(tmp_path):
    from alwavelets.parsers import VCDFile
    p = tmp_path / "e.vcd"
    p.write_text(VCD)
    return VCDFile.parse(str(p))


# ── parser ────────────────────────────────────────────────────────────

def test_parse_extracts_signals(w):
    from alwavelets import expr
    ast, sigs = expr.parse("valid && state == 3 || !addr")
    assert sigs == {"valid", "state", "addr"}
    assert ast[0] == "or"


def test_number_literals(w):
    from alwavelets import expr
    assert expr._parse_number("0x40") == 64
    assert expr._parse_number("0b1011") == 11
    assert expr._parse_number("42") == 42
    assert expr._parse_number("3.5") == 3.5


def test_bad_syntax_raises(w):
    from alwavelets import expr
    with pytest.raises(expr.ExprError):
        expr.parse("valid &&")
    with pytest.raises(expr.ExprError):
        expr.parse("a == == b")
    with pytest.raises(expr.ExprError):
        expr.parse("@bad")


# ── evaluation + search ───────────────────────────────────────────────

def test_find_logical_and(vcd):
    from alwavelets import expr
    assert expr.find_expression(vcd, "valid && state == 3", 0) == 10
    assert expr.find_all_expression(vcd, "valid && state == 3", 0) == [10, 30]


def test_find_range_compare(vcd):
    from alwavelets import expr
    # addr in [0x40, 0x80): true only at t=10 (0x40), false at 0x80
    assert expr.find_expression(vcd, "addr >= 0x40 && addr < 0x80", 0) == 10
    assert expr.find_all_expression(vcd, "addr >= 0x40 && addr < 0x80", 0) == [10]


def test_not_and_start(vcd):
    from alwavelets import expr
    assert expr.find_expression(vcd, "!valid", 0) == 0      # valid=0 at t=0
    assert expr.find_expression(vcd, "!valid", 11) == 20    # next low after 11


def test_equality_decimal_hex_binary(vcd):
    from alwavelets import expr
    assert expr.find_expression(vcd, "state == 5", 0) == 20
    assert expr.find_expression(vcd, "state == 0b101", 0) == 20
    assert expr.find_expression(vcd, "addr == 128", 0) == 20   # 0x80
    assert expr.find_expression(vcd, "state == 7", 0) is None  # never


def test_unknown_signal_raises(vcd):
    from alwavelets import expr
    with pytest.raises(expr.ExprError):
        expr.find_expression(vcd, "valid && nope", 0)


def test_parens_and_precedence(vcd):
    from alwavelets import expr
    # valid is 0 at t=20; (state==5 || valid) is true there
    assert expr.find_expression(vcd, "(state == 5 || valid) && !valid", 0) == 20


def test_arithmetic_and_bitwise(vcd):
    from alwavelets import expr
    assert expr.find_expression(vcd, "state << 1 == 6", 0) == 10        # 3<<1
    assert expr.find_expression(vcd, "state + 1 == 6", 0) == 20         # 5+1
    assert expr.find_expression(vcd, "addr & 0xF0 == 0x40", 0) == 10    # & binds tighter than ==
    assert expr.find_expression(vcd, "addr + 0x40 == 0x80", 0) == 10
    assert expr.find_expression(vcd, "(state | 0) == 5", 0) == 20
    assert expr.find_expression(vcd, "~state == 5", 0) is None          # ~3=-4, never 5


def test_make_derived_signal_arithmetic(vcd):
    from alwavelets import expr
    sig = expr.make_derived_signal(vcd, "sp1", "state + 1")
    assert sig.numeric_value_at(12) == 4      # state=3 -> 4 at t>=10
    assert sig.numeric_value_at(22) == 6      # state=5 -> 6 at t>=20


def test_make_derived_bus_from_bits(w, tmp_path):
    from alwavelets.parsers import VCDFile
    from alwavelets import expr
    v = """$timescale 1ns $end
$scope module top $end
$var wire 1 ! b1 $end
$var wire 1 " b0 $end
$upscope $end
$enddefinitions $end
$dumpvars
0!
0"
$end
#10
1"
#20
1!
"""
    (tmp_path / "b.vcd").write_text(v)
    vcd = VCDFile.parse(str(tmp_path / "b.vcd"))
    bus = expr.make_derived_signal(vcd, "bus", "(b1 << 1) | b0")
    assert bus.numeric_value_at(5) == 0       # 00
    assert bus.numeric_value_at(15) == 1      # 01
    assert bus.numeric_value_at(25) == 3      # 11
