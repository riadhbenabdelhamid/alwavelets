"""Waveform rendering — pure functions that turn signal data into
character/attribute arrays (single-bit, bus, analog). No curses.
"""
from .core import time_bisect_left, time_bisect_right, VirtualBitSignal, format_typed_value
from .streaming import check_read_cancel


# ══════════════════════════════════════════════════════════════
#  WAVEFORM DRAWING ENGINE (batched writes, analog support)
# ══════════════════════════════════════════════════════════════

# ── Waveform character sets indexed by line width ──
# Width 0 = ultra-thin, 1 = thin (default)
WAVE_CHARS = [
    # Ultra-thin — minimal strokes
    {'HIGH': '‾', 'LOW': '_', 'EDGE': '¦', 'X': '·', 'Z': '·', 'BUS_T': '┊', 'BUS_H': '─', 'BUSY': '▒'},
    # Thin (default) — standard block characters
    {'HIGH': '▔', 'LOW': '▁', 'EDGE': '│', 'X': '░', 'Z': '─', 'BUS_T': '╳', 'BUS_H': '═', 'BUSY': '▓'},
]

# Active width (0-1), set by viewer — default to thin (1)
_wave_width_idx = 1

# Convenience accessors (used throughout drawing code)
def _get_ch():
    return WAVE_CHARS[_wave_width_idx]

CH_HIGH  = '▔'
CH_LOW   = '▁'
CH_EDGE  = '│'
CH_X     = '░'
CH_Z     = '─'
CH_BUS_T = '╳'
CH_BUS_H = '═'
CH_BUSY  = '▓'   # aliased: many transitions collapsed into one column

def _update_wave_chars(idx):
    """Update the module-level waveform characters for the active width."""
    global CH_HIGH, CH_LOW, CH_EDGE, CH_X, CH_Z, CH_BUS_T, CH_BUS_H, CH_BUSY, _wave_width_idx
    _wave_width_idx = max(0, min(1, idx))
    wc = WAVE_CHARS[_wave_width_idx]
    CH_HIGH  = wc['HIGH']
    CH_LOW   = wc['LOW']
    CH_EDGE  = wc['EDGE']
    CH_X     = wc['X']
    CH_Z     = wc['Z']
    CH_BUS_T = wc['BUS_T']
    CH_BUS_H = wc['BUS_H']
    CH_BUSY  = wc['BUSY']

# Analog block characters (8 vertical levels)
ANALOG_BLOCKS = ' ▁▂▃▄▅▆▇█'


def _bit_char(val):
    if val == '1':   return CH_HIGH, 0   # attr 0 = high (bright green)
    elif val == '0': return CH_LOW, 5    # attr 5 = low (dim green)
    elif val == 'x': return CH_X, 2      # attr 2 = unknown
    elif val == 'z': return CH_Z, 3      # attr 3 = high-z
    return ' ', 5


def _column_samples(times, value_str, start, end, width, transform=None, lo=0, hi=None):
    # Adjacent columns share a half-open boundary. Sample each boundary once,
    # keeping the original float arithmetic so glyphs and labels stay exact.
    step = (end - start) / width
    boundaries = [start + i * step for i in range(width + 1)]
    sample = getattr(times, 'sample_boundaries', None)
    if sample is not None:
        result = sample(boundaries, lo, hi, transform)
        if result is not None:
            return result
    result = []
    last_index, value = None, None
    stop = len(times) if hi is None else hi
    for timestamp in boundaries:
        check_read_cancel()
        index = max(lo, min(stop, time_bisect_left(times, timestamp)))
        if index != last_index:
            value = value_str(index - 1) if index > lo else None
        result.append((index - lo, value))
        last_index = index
    return result


def _bit_lod_samples(sig, start, end, columns):
    times = sig._times
    lo, hi = 0, None
    if getattr(sig, '_scalar', False):
        if sig.transition_count_in_window(start, end) <= columns:
            return None
    elif isinstance(sig, VirtualBitSignal):
        # Match the generic window's baseline and inclusive end, but extract
        # only sampled bits instead of materializing the full bus window.
        lo = max(0, time_bisect_left(times, start) - 1)
        hi = time_bisect_right(times, end)
        if hi - lo <= columns:
            return None
    else:
        return None
    samples = _column_samples(times, sig.value_str, start, end, columns,
                              getattr(sig, '_extract_bit', None), lo, hi)
    first = sig.value_at(start) if samples[0][0] == 0 else samples[0][1]
    return samples, first


def draw_single_bit_culled(sig, t_start, t_end, width):
    """Draw single-bit waveform with window culling and level-of-detail."""
    check_read_cancel()
    chars = [' '] * width
    attrs = [0] * width
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    # LOD samples shared boundaries for scalars and expanded bits, without
    # materializing the visible event history at full zoom-out.
    sampled = _bit_lod_samples(sig, t_start, t_end, width)
    if sampled is not None:
        samples, first_val = sampled
        for col in range(width):
            # Half-open [start, end) float boundaries so every transition
            # belongs to exactly one column. int() truncation used to overlap
            # adjacent columns and double-count boundary edges, making a
            # periodic signal's columns bunch unevenly (e.g. 1,1,1,1,1,4).
            lo, val_before = samples[col]
            hi, last_val = samples[col + 1]
            if lo == 0:
                val_before = first_val

            if hi > lo:
                n_trans = hi - lo
                if n_trans > 1:
                    # Aliased: several transitions fall in one column. A solid
                    # edge here is indistinguishable from a single clean edge,
                    # so show a "busy" glyph (a fast clock reads as a band).
                    chars[col], attrs[col] = CH_BUSY, 1
                elif last_val != val_before:
                    chars[col], attrs[col] = CH_EDGE, 1   # single transition
                else:
                    chars[col], attrs[col] = _bit_char(last_val)  # dup timestamp
            else:
                chars[col], attrs[col] = _bit_char(val_before)  # no transitions
        return chars, attrs

    times, values = sig.changes_in_window(t_start, t_end)
    if not times:
        init = sig.value_at(t_start)
        ch, at = _bit_char(init)
        for c in range(width):
            chars[c], attrs[c] = ch, at
        return chars, attrs

    # Generic non-scalar signals provide decoded values through their window.
    if len(times) > width:
        start_val = sig.value_at(t_start)
        samples = _column_samples(times, values.__getitem__, t_start, t_end, width,
                                  getattr(sig, '_extract_bit', None))
        for col in range(width):
            lo, val_before = samples[col]
            hi, last_val = samples[col + 1]
            if lo == 0:
                val_before = start_val
            if hi > lo:
                n_trans = hi - lo
                if n_trans > 1:
                    chars[col], attrs[col] = CH_BUSY, 1
                elif last_val != val_before:
                    chars[col], attrs[col] = CH_EDGE, 1
                else:
                    chars[col], attrs[col] = _bit_char(last_val)
            else:
                chars[col], attrs[col] = _bit_char(val_before)
        return chars, attrs

    # ── Standard path: iterate transitions (fast when few changes) ──
    # Round half-up (int(x + 0.5)) rather than Python's round(): round() uses
    # banker's rounding, so equally-spaced clock edges land inconsistently
    # (round(4.5)=4 but round(7.5)=8), making periods alternate 1,1,2,2 instead
    # of a uniform 1,2,1,2. Round-half-up keeps the period width regular.
    scale = width / span
    trans = [(min(width - 1, int((t - t_start) * scale + 0.5)), v) for t, v in zip(times, values)]
    start_val = sig.value_at(t_start)
    cur = start_val
    ti = 0
    nt = len(trans)

    for col in range(width):
        nxt = cur
        edge = False
        while ti < nt and trans[ti][0] <= col:
            nxt = trans[ti][1]
            if trans[ti][0] == col and nxt != cur:
                edge = True
            ti += 1
        if edge:
            chars[col], attrs[col] = CH_EDGE, 1
        else:
            chars[col], attrs[col] = _bit_char(nxt)
        cur = nxt

    return chars, attrs


# ── sub-cell (2x horizontal resolution) single-bit rendering ──────────
# Each character cell packs two sub-columns (left, right). Logic-1 fills the
# top half, logic-0 the bottom half, so a fractional-cell half-period renders
# evenly — a 1.5-cell half-period becomes 3 even sub-cells instead of wobbling
# +-1 whole cell (the duty-cycle distortion the whole-cell renderer can't avoid).
SUB_HH = '▀'   # left high, right high  -> upper half block
SUB_LL = '▄'   # left low,  right low   -> lower half block
SUB_HL = '▚'   # left high, right low   -> falling edge inside the cell
SUB_LH = '▞'   # left low,  right high  -> rising edge inside the cell


def _pack_subcell(left, right, busy):
    """Map a (left, right) sub-column logic pair to a (glyph, attr) cell."""
    if busy:
        return CH_BUSY, 1
    if left in 'xX' or right in 'xX':
        return CH_X, 2                 # x/z fall back to a whole-cell glyph
    if left in 'zZ' or right in 'zZ':
        return CH_Z, 3
    if left == '1':
        return (SUB_HH, 0) if right == '1' else (SUB_HL, 1)
    return (SUB_LH, 1) if right == '1' else (SUB_LL, 5)


def draw_single_bit_subcell(sig, t_start, t_end, width):
    """Single-bit waveform at 2x horizontal resolution using half/quadrant
    blocks. Same (chars, attrs) contract as draw_single_bit_culled."""
    check_read_cancel()
    chars = [' '] * width
    attrs = [0] * width
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    n = 2 * width                      # sub-columns
    lev = [None] * n
    busy = [False] * n

    # Shared boundary samples also cover expanded bus bits at 2x resolution.
    sampled = _bit_lod_samples(sig, t_start, t_end, n)
    if sampled is not None:
        samples, first_val = sampled
        for s in range(n):
            lo, before = samples[s]
            hi, last = samples[s + 1]
            if hi > lo:
                lev[s] = last
                busy[s] = (hi - lo) > 1
            else:
                lev[s] = before if lo > 0 else first_val
        for c in range(width):
            chars[c], attrs[c] = _pack_subcell(
                lev[2 * c], lev[2 * c + 1], busy[2 * c] or busy[2 * c + 1])
        return chars, attrs

    times, values = sig.changes_in_window(t_start, t_end)
    if not times:
        v = sig.value_at(t_start)
        ch, at = _pack_subcell(v, v, False)
        for c in range(width):
            chars[c], attrs[c] = ch, at
        return chars, attrs

    if len(times) > n:
        # Generic non-scalar signals use decoded window values.
        first_val = sig.value_at(t_start)
        samples = _column_samples(times, values.__getitem__, t_start, t_end, n,
                                  getattr(sig, '_extract_bit', None))
        for s in range(n):
            lo, before = samples[s]
            hi, last = samples[s + 1]
            if hi > lo:
                lev[s] = last
                busy[s] = (hi - lo) > 1
            else:
                lev[s] = before if lo > 0 else first_val
    else:
        # Standard: map each transition to a sub-column (round half-up).
        scale = n / span
        tcols = [min(n - 1, int((t - t_start) * scale + 0.5)) for t in times]
        cur = sig.value_at(t_start)
        ti, nt, prev_col = 0, len(tcols), -1
        for s in range(n):
            while ti < nt and tcols[ti] <= s:
                if tcols[ti] == s and prev_col == s:
                    busy[s] = True     # two edges collapsed into one sub-column
                cur = values[ti]
                prev_col = tcols[ti]
                ti += 1
            lev[s] = cur

    for c in range(width):
        chars[c], attrs[c] = _pack_subcell(
            lev[2 * c], lev[2 * c + 1], busy[2 * c] or busy[2 * c + 1])
    return chars, attrs


def draw_bus_culled(sig, t_start, t_end, width, radix):
    """Draw multi-bit bus waveform with window culling and LOD."""
    check_read_cancel()
    chars = [' '] * width
    attrs = [0] * width
    labels = {}
    size = sig.size
    span = t_end - t_start
    if span <= 0:
        return chars, attrs, labels
    # Bisect-only transition count decides LOD, so a dense bus never decodes
    # the whole window's values just to be culled per column.
    n_changes = sig.transition_count_in_window(t_start, t_end)

    # ── LOD path (activate when transitions > pixels for symmetry) ──
    if n_changes > width:
        at = sig._times
        prev = sig.value_at(t_start)
        labels[0] = format_typed_value(sig, prev, radix)
        samples = _column_samples(at, sig.value_str, t_start, t_end, width)
        for col in range(width):
            # Half-open [start, end) float boundaries — see draw_single_bit_culled.
            check_read_cancel()
            lo = samples[col][0]
            hi, last = samples[col + 1]
            if hi > lo:
                if hi - lo > 1 or last != prev:
                    chars[col], attrs[col] = CH_BUS_T, 1
                    if col + 1 < width:
                        labels[col + 1] = format_typed_value(sig, last, radix)
                else:
                    v = str(last)
                    if 'x' in v or 'X' in v: chars[col], attrs[col] = CH_X, 2
                    elif 'z' in v or 'Z' in v: chars[col], attrs[col] = CH_Z, 3
                    else: chars[col], attrs[col] = CH_BUS_H, 6
                prev = last
            else:
                v = str(prev)
                if 'x' in v or 'X' in v: chars[col], attrs[col] = CH_X, 2
                elif 'z' in v or 'Z' in v: chars[col], attrs[col] = CH_Z, 3
                else: chars[col], attrs[col] = CH_BUS_H, 6
        return chars, attrs, labels

    # ── Standard path ──
    times, values = sig.changes_in_window(t_start, t_end)
    scale = width / span
    # Round half-up for consistent column spacing (see draw_single_bit_culled).
    trans = [(min(width - 1, int((t - t_start) * scale + 0.5)), v) for t, v in zip(times, values)]
    start_val = sig.value_at(t_start)
    labels[0] = format_typed_value(sig, start_val, radix)
    cur = start_val
    ti = 0; nt = len(trans)

    for col in range(width):
        nxt = cur; edge = False
        while ti < nt and trans[ti][0] <= col:
            nxt = trans[ti][1]
            if trans[ti][0] == col and nxt != cur: edge = True
            ti += 1
        if edge:
            chars[col], attrs[col] = CH_BUS_T, 1
            if col + 1 < width:
                labels[col + 1] = format_typed_value(sig, nxt, radix)
        else:
            v = str(nxt)
            if any(c in v for c in 'xX'): chars[col], attrs[col] = CH_X, 2
            elif any(c in v for c in 'zZ'): chars[col], attrs[col] = CH_Z, 3
            else: chars[col], attrs[col] = CH_BUS_H, 6
        cur = nxt

    return chars, attrs, labels


def draw_analog_culled(sig, t_start, t_end, width, height_chars=1, *, limits=None):
    """
    Draw analog waveform using Unicode block characters with heat-map colors.
    Returns (chars, attrs) where each char is a block element.
    Attr codes: 9=cool(low), 8=warm(mid), 7=hot(high)
    """
    check_read_cancel()
    chars = [' '] * width
    attrs = [0] * width

    vmin, vmax = sig.numeric_range_in_window(t_start, t_end) if limits is None else limits
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    scale = width / span
    vrange = vmax - vmin if vmax != vmin else 1.0
    nblocks = len(ANALOG_BLOCKS) - 1  # 0..8

    prev_level = None
    for col in range(width):
        check_read_cancel()
        t = t_start + col / scale
        nv = sig.numeric_value_at(t)
        if nv is None:
            chars[col], attrs[col] = CH_X, 2
            prev_level = None
            continue
        # Normalize to 0..nblocks
        frac = (nv - vmin) / vrange
        level = int(frac * nblocks + 0.5)
        level = max(0, min(nblocks, level))
        chars[col] = ANALOG_BLOCKS[level]

        # Heat-map color: cool → warm → hot
        if frac < 0.33:
            attrs[col] = 9   # cool (teal/blue)
        elif frac < 0.67:
            attrs[col] = 8   # warm (yellow)
        else:
            attrs[col] = 7   # hot (red)

        # Draw connecting lines for large jumps
        if prev_level is not None and abs(level - prev_level) > 2:
            chars[col] = '│'
            attrs[col] = 1   # transition color

        prev_level = level

    return chars, attrs


# Braille cell dot bits, indexed [sub_row 0..3 from the top][sub_col 0..1].
_BRAILLE_BITS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))


def draw_analog_braille(sig, t_start, t_end, width, height, *, limits=None):
    """Analog line graph at braille sub-pixel resolution: each character cell
    holds 2x4 dots, so `height` rows give 4*height vertical steps and every
    column two samples. Adjacent samples are connected vertically, drawing a
    continuous curve instead of stacked bars. Same contract as
    draw_analog_multi: a list of `height` (chars, attrs) tuples, top first.
    Pure Unicode (U+2800 block) — needs no graphics protocol."""
    check_read_cancel()
    height = max(1, height)
    rows = [([' '] * width, [0] * width) for _ in range(height)]
    span = t_end - t_start
    if span <= 0:
        return rows
    vmin, vmax = sig.numeric_range_in_window(t_start, t_end) if limits is None else limits
    vrange = vmax - vmin if vmax != vmin else 1.0
    vres = 4 * height                       # braille rows top→bottom
    nsub = 2 * width                        # braille columns
    inv = span / nsub

    cells = [[0] * width for _ in range(height)]     # dot bitmasks
    heat = [[0] * width for _ in range(height)]      # attr per cell
    prev_y = None
    for s in range(nsub):
        check_read_cancel()
        nv = sig.numeric_value_at(t_start + int(s * inv))
        col, sub = s >> 1, s & 1
        if nv is None:                       # x/z: mark the whole column
            for r in range(height):
                if rows[r][0][col] == ' ':
                    rows[r][0][col], rows[r][1][col] = CH_X, 2
            prev_y = None
            continue
        frac = max(0.0, min(1.0, (nv - vmin) / vrange))
        y = int((1.0 - frac) * (vres - 1) + 0.5)
        cattr = 9 if frac < 0.33 else (8 if frac < 0.67 else 7)
        # Connect to the previous sample so the curve is continuous.
        y0, y1 = (y, y) if prev_y is None else (min(y, prev_y + 1), max(y, prev_y - 1))
        if y0 > y1:
            y0, y1 = y1, y0
        for yy in range(y0, y1 + 1):
            cells[yy >> 2][col] |= _BRAILLE_BITS[yy & 3][sub]
            heat[yy >> 2][col] = cattr
        prev_y = y

    for r in range(height):
        chars, attrs = rows[r]
        crow, hrow = cells[r], heat[r]
        for col in range(width):
            if crow[col]:
                chars[col] = chr(0x2800 + crow[col])
                attrs[col] = hrow[col]
    return rows


def draw_analog_multi(sig, t_start, t_end, width, height, *, limits=None):
    """Render an analog signal across `height` character rows (top row = high
    values). Returns a list of `height` (chars, attrs) tuples, top first."""
    rows = [([' '] * width, [0] * width) for _ in range(max(1, height))]
    check_read_cancel()
    vmin, vmax = sig.numeric_range_in_window(t_start, t_end) if limits is None else limits
    span = t_end - t_start
    if span <= 0:
        return rows
    scale = width / span
    vrange = vmax - vmin if vmax != vmin else 1.0
    nsub = len(ANALOG_BLOCKS) - 1          # 8 sub-levels per row
    total = height * nsub                   # full vertical resolution

    for col in range(width):
        t = t_start + col / scale
        check_read_cancel()
        nv = sig.numeric_value_at(t)
        if nv is None:
            for r in range(height):
                rows[r][0][col], rows[r][1][col] = CH_X, 2
            continue
        frac = max(0.0, min(1.0, (nv - vmin) / vrange))
        lvl = frac * total                  # 0 .. total
        cattr = 9 if frac < 0.33 else (8 if frac < 0.67 else 7)
        for sr in range(height):            # sr=0 is the top screen row
            from_bottom = height - 1 - sr
            cell = int(round(lvl - from_bottom * nsub))
            cell = max(0, min(nsub, cell))
            if cell > 0:
                rows[sr][0][col], rows[sr][1][col] = ANALOG_BLOCKS[cell], cattr
    return rows
