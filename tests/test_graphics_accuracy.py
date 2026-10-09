"""PNG pixels must retain activity hidden between sampling points."""
import pytest

from alwavelets.core import VCDSignal
from alwavelets import graphics as g


def draw(changes, width=1, start=0, end=100):
    signal = VCDSignal("data", "!", size=width)
    for t, value in changes:
        signal.add_change(t, value)
    return g._rasterise(None, [signal], start, end, 20, 20, None)


def at(buf, x, y, width=20):
    return buf[y * width + x]


@pytest.mark.parametrize("changes", [
    [(0, "0"), (11, "1"), (12, "0")],
    [(0, "0"), (11, "1"), (11, "0")],
])
def test_subpixel_and_same_tick_pulses_are_visible(changes):
    buf, _, _ = draw(changes)
    assert at(buf, 2, 8) == g.EDGE
    assert at(buf, 1, 8) == g.BG and at(buf, 3, 8) == g.BG


def test_x_and_z_pulses_survive_later_edges_in_the_same_pixel():
    buf, _, _ = draw([(0, "0"), (11, "x"), (12, "z"), (13, "0")])
    assert at(buf, 2, 9) == g.UNDEF
    assert at(buf, 2, 10) == g.HIZ
    assert g.PALETTE[g.UNDEF] != g.PALETTE[g.HIZ] != g.PALETTE[g.BUS]


def test_final_tick_and_large_absolute_time_are_not_lost():
    origin = 2**60
    buf, _, _ = draw([(origin, "0"), (origin + 37, "1"), (origin + 100, "0")],
                     start=origin, end=origin + 100)
    assert at(buf, 7, 8) == g.EDGE
    assert at(buf, 19, 8) == g.EDGE


def test_steady_predecessor_and_unknown_prefix_are_drawn():
    known, _, _ = draw([(0, "1")], start=20, end=100)
    assert all(at(known, x, 3) == g.HIGH for x in range(20))
    late, _, _ = draw([(50, "1")])
    assert at(late, 0, 9) == g.UNDEF and at(late, 19, 3) == g.HIGH


def test_duplicate_values_do_not_create_edges():
    buf, _, _ = draw([(0, "0"), (11, "0"), (12, "0")])
    assert g.EDGE not in buf


def test_bus_high_impedance_is_distinct_from_unknown():
    buf, _, _ = draw([(0, "zzzz"), (50, "xxxx")], width=4)
    assert at(buf, 0, 3) == g.HIZ
    assert at(buf, 19, 3) == g.UNDEF


def test_empty_selection_is_an_error_instead_of_a_blank_png(w):
    from test_graphics import DEMO
    vcd = w.VCDFile.parse(DEMO)
    with pytest.raises(ValueError, match="No signals matched"):
        w.render_waveform_png(vcd, ["nonexistent"])


def test_mcp_png_includes_signal_names_values_and_time_window(w):
    from alwavelets.mcp_server import _h_render_png
    from test_graphics import DEMO
    blocks = _h_render_png(DEMO, ["top.clk", "top.state"], 0, "300ns")
    assert blocks[0]["mimeType"] == "image/png"
    text = "\n".join(b["text"] for b in blocks if b["type"] == "text")
    assert "top.clk" in text and "top.state" in text and "300ns" in text


def test_short_bus_labels_cannot_be_mistaken_for_different_values():
    from alwavelets.snapshot import _apply_bus_labels
    assert _apply_bus_labels(list("════"), {0: "deadbeef"}) == "…═══"
    assert _apply_bus_labels(list("════"), {0: "a6"}) == "a6══"
