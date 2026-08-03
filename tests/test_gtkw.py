"""Tests for GTKWave .gtkw save-file interop (read, write, round-trip)."""

GTKW = """[*] GTKWave Analyzer save file
[*] Sat
[dumpfile] "/path/to/design.vcd"
[timestart] 0
[size] 1920 1080
[treeopen] top.
@28
top.clk
@22
top.alu.result[7:0]
@10
top.state[3:0]
[pattern_trace] 1
"""


def test_load_gtkw_fields(w, tmp_path):
    from alwavelets import gtkw
    p = tmp_path / "v.gtkw"
    p.write_text(GTKW)
    data = gtkw.load_gtkw(str(p))
    assert data['dumpfile'] == "/path/to/design.vcd"
    assert data['timestart'] == 0
    names = [n for n, _ in data['signals']]
    assert names == ["top.clk", "top.alu.result[7:0]", "top.state[3:0]"]


def test_load_gtkw_radix_flags(w, tmp_path):
    from alwavelets import gtkw
    from alwavelets.core import Radix
    p = tmp_path / "v.gtkw"
    p.write_text(GTKW)
    sigs = dict(gtkw.load_gtkw(str(p))['signals'])
    # @22 has TR_HEX(0x02); @10 has TR_BIN(0x10)
    assert sigs["top.alu.result[7:0]"] == Radix.HEX
    assert sigs["top.state[3:0]"] == Radix.BIN


def test_base_name_strips_range(w):
    from alwavelets import gtkw
    assert gtkw.base_name("top.alu.result[7:0]") == "top.alu.result"
    assert gtkw.base_name("top.data[3]") == "top.data"
    assert gtkw.base_name("top.clk") == "top.clk"


def test_save_then_load_roundtrip(w, tmp_path):
    from alwavelets import gtkw
    from alwavelets.core import Radix
    sigs = [("top.clk", Radix.BIN, 1),
            ("top.bus", Radix.HEX, 8),
            ("top.cnt", Radix.UNSIGNED, 4)]
    p = tmp_path / "out.gtkw"
    gtkw.save_gtkw(str(p), "/x/y.vcd", sigs, 0, 100)
    data = gtkw.load_gtkw(str(p))
    assert data['dumpfile'] == "/x/y.vcd"
    names = [n for n, _ in data['signals']]
    assert names == ["top.clk", "top.bus[7:0]", "top.cnt[3:0]"]
    rdx = dict(data['signals'])
    assert rdx["top.bus[7:0]"] == Radix.HEX
    assert rdx["top.clk"] == Radix.BIN
    assert rdx["top.cnt[3:0]"] == Radix.UNSIGNED   # TR_DEC without TR_SIGNED
