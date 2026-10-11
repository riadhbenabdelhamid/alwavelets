"""Tests for the pure waveform-rendering functions. These return character /
attribute arrays (and bus labels) without touching curses, so they're directly
testable — and they're the core of what the viewer draws."""
from fractions import Fraction
from types import SimpleNamespace

import pytest

from alwavelets import rendering
from alwavelets.core import Radix, TimeScaledSignal, VCDSignal
from alwavelets.ui import Alwavelets


def _sig(w, size, changes):
    s = w.VCDSignal("s", "!", size)
    for t, v in changes:
        s.add_change(t, v)
    return s


def _clock_frame(kind, offset=0, start=0, end=100, width=70, ratio=1):
    sig = VCDSignal('clk', '!', 2 if kind == 'bus' else 1)
    for t in range(0, 305, 5):
        sig.add_change(offset + t, format((t // 5) % 2, f'0{sig.size}b'))
    if ratio != 1:
        sig = TimeScaledSignal(sig, ratio)
    draw = {'bit': rendering.draw_single_bit_culled,
            'sub': rendering.draw_single_bit_subcell,
            'bus': rendering.draw_bus_culled}[kind]
    args = (Radix.HEX,) if kind == 'bus' else ()
    return draw(sig, (offset + start) * ratio, (offset + end) * ratio, width, *args)


@pytest.mark.parametrize('kind', ['bit', 'sub', 'bus'])
def test_clock_periods_stay_uniform_at_half_column_ties(kind):
    # Ten ticks occupy exactly seven columns (sub-columns in hi-res mode).
    # Float scaling used to shift t=45 and t=85 left, creating 6/8-wide periods.
    width = 35 if kind == 'sub' else 70
    chars = _clock_frame(kind, width=width)[0]
    if kind == 'sub':
        unpack = {rendering.SUB_HH: '11', rendering.SUB_LL: '00',
                  rendering.SUB_HL: '10', rendering.SUB_LH: '01'}
        levels = ''.join(unpack[ch] for ch in chars)
        edges = [i for i in range(1, len(levels)) if levels[i] != levels[i - 1]]
    else:
        glyph = rendering.CH_BUS_T if kind == 'bus' else rendering.CH_EDGE
        edges = [i for i, ch in enumerate(chars) if ch == glyph]
    assert edges == [4, 7, 11, 14, 18, 21, 25, 28, 32, 35, 39, 42, 46, 49, 53, 56, 60, 63, 67]
    assert {b - a for a, b in zip(edges, edges[2:])} == {7}
    viewer = SimpleNamespace(time_start=0, time_end=100, wave_width=70)
    assert [Alwavelets.time_to_col(viewer, t) for t in range(5, 100, 5)] == edges


@pytest.mark.parametrize('kind', ['bit', 'sub', 'bus'])
@pytest.mark.parametrize('width', [21, 35, 41, 70, 82])
def test_dense_and_detailed_clock_rendering_agree(monkeypatch, kind, width):
    frames = [_clock_frame(kind, start=start, end=start + 100, width=width)
              for start in (0, 1, 3, 5)]
    # Changing the LOD choice alone must not shift edges or change duty cycle.
    monkeypatch.setattr(VCDSignal, 'transition_count_in_window', lambda *args: 10000)
    assert frames == [_clock_frame(kind, start=start, end=start + 100, width=width)
                      for start in (0, 1, 3, 5)]


@pytest.mark.parametrize('kind', ['bit', 'sub', 'bus'])
@pytest.mark.parametrize('width', [13, 20, 21, 35, 70])
def test_clock_shape_is_independent_of_time_origin_and_units(kind, width):
    expected = _clock_frame(kind, start=1, end=101, width=width)
    for offset in (0, (1 << 53) + 3):
        for ratio in (1, Fraction(2, 3)):
            assert _clock_frame(kind, offset=offset, start=1, end=101,
                                width=width, ratio=ratio) == expected


@pytest.mark.parametrize('kind', ['bit', 'sub', 'bus'])
def test_clock_window_end_does_not_create_a_last_column_edge(kind):
    # The next edge lies at column 20, just outside this viewport.
    frame = _clock_frame(kind, end=5, width=20)
    glyph = {'bit': rendering.CH_LOW, 'sub': rendering.SUB_LL,
             'bus': rendering.CH_BUS_H}[kind]
    assert frame[0] == [glyph] * 20


def test_sparse_collapsed_pulse_is_busy(w):
    sig = _sig(w, 1, [(0, '0'), (40, '1'), (41, '0')])
    chars, _ = rendering.draw_single_bit_culled(sig, 0, 100, 20)
    assert chars[8] == rendering.CH_BUSY


# ── single-bit ───────────────────────────────────────────────────────

def test_bit_char(w):
    assert w._bit_char("1") == (w.CH_HIGH, 0)
    assert w._bit_char("0") == (w.CH_LOW, 5)
    assert w._bit_char("x") == (w.CH_X, 2)
    assert w._bit_char("z") == (w.CH_Z, 3)


def test_draw_single_bit_constant(w):
    sig = _sig(w, 1, [(0, "1")])
    chars, attrs = w.draw_single_bit_culled(sig, 0, 100, 16)
    assert len(chars) == 16 and len(attrs) == 16
    assert set(chars) == {w.CH_HIGH} and set(attrs) == {0}


def test_draw_single_bit_transition(w):
    sig = _sig(w, 1, [(0, "0"), (10, "1"), (20, "0")])
    chars, attrs = w.draw_single_bit_culled(sig, 0, 20, 20)
    assert len(chars) == 20
    assert w.CH_EDGE in chars          # the rising/falling edges are drawn
    assert chars[0] == w.CH_LOW        # starts low


def test_draw_single_bit_aliased_is_busy(w):
    # A signal toggling faster than one transition per column is undersampled;
    # those columns get the distinct "busy" glyph, not a plain edge.
    sig = w.VCDSignal("s", "!", 1)
    for t in range(200):
        sig.add_change(t, "1" if t % 2 else "0")
    chars, attrs = w.draw_single_bit_culled(sig, 0, 200, 20)  # 200 changes, 20 cols
    assert w.CH_BUSY in chars
    assert w.CH_BUSY != w.CH_EDGE


def test_draw_single_bit_undefined(w):
    sig = _sig(w, 1, [])               # no data → 'x' everywhere
    chars, attrs = w.draw_single_bit_culled(sig, 0, 10, 8)
    assert set(chars) == {w.CH_X} and set(attrs) == {2}


def test_expanded_bus_bit_lod_does_not_crash(w):
    # Regression: an expanded-bus bit lane (VirtualBitSignal) exposes the
    # parent's bus-value STRINGS in _values, not packed byte codes. The
    # single-bit LOD paths must not index _B2S with those strings — doing so
    # raised "TypeError: list indices must be integers" once a bus had more
    # transitions than columns. Guarded by the _scalar check in
    # draw_single_bit_culled / draw_single_bit_subcell.
    bus = w.VCDSignal("adc", "!", 8)
    for t in range(300):                       # > the widths below → LOD branch
        bus.add_change(t, format(t & 0xFF, "08b"))
    bit = w.VirtualBitSignal(bus, 0)           # LSB toggles every step
    for width in (20, 50):
        c1, a1 = w.draw_single_bit_culled(bit, 0, 300, width)
        c2, a2 = w.draw_single_bit_subcell(bit, 0, 300, width)
        assert len(c1) == width == len(a1)
        assert len(c2) == width == len(a2)
        assert w.CH_BUSY in c1                  # fast-toggling LSB reads as busy


# ── sub-cell (2x horizontal resolution) single-bit ───────────────────

def test_subcell_constant_and_dims(w):
    sig = _sig(w, 1, [(0, "1")])
    chars, attrs = w.draw_single_bit_subcell(sig, 0, 100, 16)
    assert len(chars) == 16 and len(attrs) == 16
    assert set(chars) == {w.SUB_HH} and set(attrs) == {0}    # all upper-half block
    lo = _sig(w, 1, [(0, "0")])
    lchars, _ = w.draw_single_bit_subcell(lo, 0, 100, 16)
    assert set(lchars) == {w.SUB_LL}                          # all lower-half block


def test_subcell_pack_mapping(w):
    from alwavelets import rendering
    p = rendering._pack_subcell                # left/right logic pair -> glyph + attr
    assert p("1", "1", False) == (w.SUB_HH, 0)
    assert p("0", "0", False) == (w.SUB_LL, 5)
    assert p("1", "0", False) == (w.SUB_HL, 1)  # falling, edge attr
    assert p("0", "1", False) == (w.SUB_LH, 1)  # rising,  edge attr
    assert p("x", "1", False) == (w.CH_X, 2)    # x -> whole-cell glyph
    assert p("0", "z", False) == (w.CH_Z, 3)
    assert p("1", "0", True) == (w.CH_BUSY, 1)  # aliased -> busy


def test_subcell_evens_out_half_periods(w):
    # A clock toggling every 5 ticks at a zoom where the half-period is ~1.5
    # whole cells: whole-cell render wobbles 1,2,1,2; sub-cell makes every
    # half-period an even 3 sub-cells.
    clk = w.VCDSignal("clk", "!", 1)
    for t in range(0, 205, 5):
        clk.add_change(t, "1" if (t // 5) % 2 else "0")
    chars, _ = w.draw_single_bit_subcell(clk, 0, 200, 60)
    unpack = {w.SUB_HH: (1, 1), w.SUB_LL: (0, 0), w.SUB_HL: (1, 0), w.SUB_LH: (0, 1)}
    sub = []
    for ch in chars:
        sub += list(unpack[ch])                 # every cell is a clean 2-level pair
    runs, cur, n = [], sub[0], 0
    for v in sub:
        if v == cur:
            n += 1
        else:
            runs.append(n); cur, n = v, 1
    runs.append(n)
    # 200 ticks / 60 cells * 2 = exactly 3.0 sub-cells per 5-tick half-period.
    # Interior runs are uniformly 3 (trim ends to drop window-clamp artifacts).
    assert set(runs[2:-2]) == {3}


# ── analog (multi-row) ───────────────────────────────────────────────

def test_draw_analog_multi(w):
    sig = w.VCDSignal("a", "!", 8)
    sig.add_change(0, "00000000")     # 0 (min)
    sig.add_change(10, "10000000")    # 128 (mid)
    sig.add_change(20, "11111111")    # 255 (max)
    rows = w.draw_analog_multi(sig, 0, 30, 24, 3)
    assert len(rows) == 3
    assert all(len(chars) == 24 and len(attrs) == 24 for chars, attrs in rows)
    col = lambda t: int(t / 30 * 24)
    # a mid value fills from the bottom up (top empty, bottom full)
    assert rows[0][0][col(15)] == " "
    assert rows[2][0][col(15)] == "█"
    # the maximum fills the whole height
    assert all(rows[r][0][col(25)] == "█" for r in range(3))


# ── multi-bit bus ────────────────────────────────────────────────────

def test_draw_bus_labels_and_transition(w):
    bus = _sig(w, 4, [(0, "0000"), (10, "1010")])
    chars, attrs, labels = w.draw_bus_culled(bus, 0, 20, 20, w.Radix.HEX)
    assert len(chars) == 20 and len(attrs) == 20
    assert labels[0] == "0"            # start value, hex
    assert "A" in labels.values()      # 0b1010 = 0xA after the transition
    assert w.CH_BUS_T in chars         # a transition marker is drawn


def test_draw_bus_undefined_segment(w):
    bus = _sig(w, 4, [(0, "00xx")])
    chars, attrs, labels = w.draw_bus_culled(bus, 0, 10, 8, w.Radix.HEX)
    assert set(attrs) == {2}           # x-attr across the whole (undefined) span
    assert set(chars) == {w.CH_X}


def test_draw_analog_braille(w):
    import math
    sig = w.VCDSignal("a", "!", 8)
    for t in range(0, 400, 4):
        sig.add_change(t, format(int(127.5 + 127 * math.sin(t / 20)) & 0xFF, "08b"))
    rows = w.draw_analog_braille(sig, 0, 400, 40, 3)
    assert len(rows) == 3
    assert all(len(c) == 40 and len(a) == 40 for c, a in rows)
    # every column carries part of the curve, in braille glyphs
    for col in range(40):
        glyphs = [rows[r][0][col] for r in range(3)]
        assert any(0x2800 <= ord(ch) <= 0x28FF for ch in glyphs if ch != ' '), col
        assert any(ch != ' ' for ch in glyphs)
    # peaks reach the top row, troughs the bottom row
    assert any(ch != ' ' for ch in rows[0][0])
    assert any(ch != ' ' for ch in rows[2][0])


def test_draw_analog_braille_undefined(w):
    sig = w.VCDSignal("a", "!", 8)
    sig.add_change(0, "xxxxxxxx")
    rows = w.draw_analog_braille(sig, 0, 100, 10, 2)
    assert all(set(chars) == {w.CH_X} for chars, _ in rows)   # x fills, no crash
