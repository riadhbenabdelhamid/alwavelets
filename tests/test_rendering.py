"""Tests for the pure waveform-rendering functions. These return character /
attribute arrays (and bus labels) without touching curses, so they're directly
testable — and they're the core of what the viewer draws."""


def _sig(w, size, changes):
    s = w.VCDSignal("s", "!", size)
    for t, v in changes:
        s.add_change(t, v)
    return s


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
