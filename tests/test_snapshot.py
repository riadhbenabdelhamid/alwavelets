"""Tests for the headless waveform snapshot renderer (text + SVG)."""
import os
import xml.etree.ElementTree as ET

DEMO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "demo_waveform.vcd")


def test_resolve_signals_globs(w):
    vcd = w.VCDFile.parse(DEMO)
    sigs = w.resolve_signals(vcd, ["top.clk", "top.alu.*"])
    names = [s.full_name for s in sigs]
    assert "top.clk" in names
    assert any(n.startswith("top.alu.") for n in names)
    # request order is preserved and there are no duplicates
    assert names[0] == "top.clk"
    assert len(names) == len(set(names))


def test_resolve_signals_no_match(w):
    vcd = w.VCDFile.parse(DEMO)
    assert w.resolve_signals(vcd, ["does.not.exist"]) == []


def test_parse_time_units(w):
    vcd = w.VCDFile.parse(DEMO)            # timescale 1 ns
    assert w.parse_time(vcd, 200) == 200
    assert w.parse_time(vcd, "200") == 200
    assert w.parse_time(vcd, "500ns") == 500
    assert w.parse_time(vcd, "1us") == 1000
    assert w.parse_time(vcd, "1.5us") == 1500


def test_list_signals(w):
    vcd = w.VCDFile.parse(DEMO)
    assert w.list_signals(vcd, "top.alu.*")
    assert all(n.startswith("top.alu.") for n in w.list_signals(vcd, "top.alu.*"))
    assert len(w.list_signals(vcd, "*", limit=3)) == 3


def test_render_text(w):
    vcd = w.VCDFile.parse(DEMO)
    out = w.render_text(vcd, ["top.clk", "top.state"], 0, "300ns", width=80)
    lines = out.splitlines()
    # header + ruler + one row per signal
    assert any("top.clk" in line for line in lines)
    assert any("top.state" in line for line in lines)
    assert "300ns" in out                  # ruler shows the window end
    # every signal row is at most `width` columns
    assert all(len(line) <= 80 for line in lines)


def test_render_text_no_match(w):
    vcd = w.VCDFile.parse(DEMO)
    out = w.render_text(vcd, ["nope"], 0, 100)
    assert "no signals matched" in out


def test_render_text_full_extent_default(w):
    vcd = w.VCDFile.parse(DEMO)
    # start/end default to the full trace window
    out = w.render_text(vcd, ["top.clk"])
    assert vcd.time_to_str(vcd.max_time) in out


def test_render_svg_wellformed(w):
    vcd = w.VCDFile.parse(DEMO)
    svg = w.render_svg(vcd, ["top.clk", "top.state", "top.alu.result"], 0, "500ns")
    root = ET.fromstring(svg)               # raises if malformed
    assert root.tag.endswith("svg")


def test_render_svg_theme(w):
    vcd = w.VCDFile.parse(DEMO)
    assert "#fbfbfb" in w.render_svg(vcd, ["top.clk"], 0, 200, theme="light")
    # a theme-JSON file is converted from xterm-256 and stays valid SVG
    theme = os.path.join(os.path.dirname(DEMO), "theme_solarized.json")
    ET.fromstring(w.render_svg(vcd, ["top.clk"], 0, 200, theme=theme))


def test_measure(w):
    vcd = w.VCDFile.parse(DEMO)
    out = w.measure(vcd, ["top.clk", "top.alu.result"], 0, "2us")
    assert "top.clk" in out
    assert "transitions=" in out
    assert "100MHz" in out                  # clk has a 10ns period
    assert "not periodic" in out            # the data bus is not a clock
    assert "edge-delta" in out


def test_load_arrangement_and_render(w, tmp_path):
    import json
    vcd = w.VCDFile.parse(DEMO)
    p = tmp_path / "arr.json"
    p.write_text(json.dumps({"display_order": vcd.signal_order[:2], "radix": "bin"}))
    names, radix = w.load_arrangement(vcd, str(p))
    assert len(names) == 2 and radix == "bin"
    # render using the saved view (no explicit `signals`)
    out = w.render_text(vcd, arrangement=str(p), t_start=0, t_end=200, width=70)
    assert names[0].split(".")[-1] in out
