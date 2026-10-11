"""Waveform rendering — pure functions that turn signal data into
character/attribute arrays (single-bit, bus, analog). No curses.
"""
from .core import time_bisect_left

from .core import _B2S, format_typed_value


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


def _value_bisect_left(times, timestamp):
    # Column boundaries are followed by a value read. Disk-backed signals can
    # load both together rather than inflating a timestamp prefix and upgrading.
    lookup = getattr(times, 'bisect_values', None)
    return lookup(timestamp) if lookup else time_bisect_left(times, timestamp)


def draw_single_bit_culled(sig, t_start, t_end, width):
    """Draw single-bit waveform with window culling and level-of-detail."""
    chars = [' '] * width
    attrs = [0] * width
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    # ── LOD fast path: per-column binary search when transitions approach
    # pixel count. Chosen by a bisect-only transition count so a dense scalar
    # never materializes the whole window's value list (which would cost
    # O(window) per frame at full zoom-out).
    if getattr(sig, '_scalar', False) and \
            sig.transition_count_in_window(t_start, t_end) > width:
        _bisect_left = _value_bisect_left
        inv_width = span / width  # float division for consistent column widths
        # Packed-scalar storage: read byte codes from the full arrays and
        # decode via _B2S.
        all_times = sig._times
        all_values = sig._values
        for col in range(width):
            # Half-open [start, end) float boundaries so every transition
            # belongs to exactly one column. int() truncation used to overlap
            # adjacent columns and double-count boundary edges, making a
            # periodic signal's columns bunch unevenly (e.g. 1,1,1,1,1,4).
            col_t_start = t_start + col * inv_width
            col_t_end = t_start + (col + 1) * inv_width
            lo = _bisect_left(all_times, col_t_start)
            hi = _bisect_left(all_times, col_t_end)

            # Value at column start (before any transitions in this column)
            val_before_idx = lo - 1
            val_before = _B2S[all_values[val_before_idx]] if val_before_idx >= 0 else sig.value_at(t_start)

            if hi > lo:
                last_val = _B2S[all_values[hi - 1]]
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

    # ── LOD generic path: non-packed single-bit signals — e.g. expanded-bus
    # bit lanes (VirtualBitSignal), whose _values are the parent's raw bus
    # values (so the byte-code fast path above would misread them). Bucket
    # over the already bit-extracted windowed (times, values).
    if len(times) > width:
        _bisect_left = _value_bisect_left
        inv_width = span / width
        start_val = sig.value_at(t_start)
        for col in range(width):
            col_t_start = t_start + col * inv_width
            col_t_end = t_start + (col + 1) * inv_width
            lo = _bisect_left(times, col_t_start)
            hi = _bisect_left(times, col_t_end)
            val_before = values[lo - 1] if lo - 1 >= 0 else start_val
            if hi > lo:
                last_val = values[hi - 1]
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
    chars = [' '] * width
    attrs = [0] * width
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    n = 2 * width                      # sub-columns
    lev = [None] * n
    busy = [False] * n

    # LOD fast path first: a bisect-only transition count picks it, so a
    # dense scalar never materializes the whole window's value list (see
    # draw_single_bit_culled).
    if getattr(sig, '_scalar', False) and \
            sig.transition_count_in_window(t_start, t_end) > n:
        # Per sub-column binary search, half-open [start, end) float bounds;
        # packed-scalar byte codes decoded via _B2S.
        _bl = _value_bisect_left
        inv = span / n
        first_val = sig.value_at(t_start)
        all_t = sig._times
        all_v = sig._values
        for s in range(n):
            lo = _bl(all_t, t_start + s * inv)
            hi = _bl(all_t, t_start + (s + 1) * inv)
            if hi > lo:
                lev[s] = _B2S[all_v[hi - 1]]
                busy[s] = (hi - lo) > 1
            else:
                lev[s] = _B2S[all_v[lo - 1]] if lo > 0 else first_val
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
        # LOD generic path: non-packed single-bit signals (expanded-bus
        # VirtualBitSignal) — bucket over the already bit-extracted windowed
        # (times, values), which are strings, not byte codes.
        _bl = _value_bisect_left
        inv = span / n
        first_val = sig.value_at(t_start)
        for s in range(n):
            lo = _bl(times, t_start + s * inv)
            hi = _bl(times, t_start + (s + 1) * inv)
            if hi > lo:
                lev[s] = values[hi - 1]
                busy[s] = (hi - lo) > 1
            else:
                lev[s] = values[lo - 1] if lo > 0 else first_val
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
        _bl = _value_bisect_left
        at = sig._times
        vstr = sig.value_str            # decodes packed bus storage
        prev = sig.value_at(t_start)
        labels[0] = format_typed_value(sig, prev, radix)
        inv_w = span / width
        for col in range(width):
            # Half-open [start, end) float boundaries — see draw_single_bit_culled.
            ct0 = t_start + col * inv_w
            ct1 = t_start + (col + 1) * inv_w
            lo = _bl(at, ct0); hi = _bl(at, ct1)
            if hi > lo:
                last = vstr(hi - 1)
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


def draw_analog_culled(sig, t_start, t_end, width, height_chars=1):
    """
    Draw analog waveform using Unicode block characters with heat-map colors.
    Returns (chars, attrs) where each char is a block element.
    Attr codes: 9=cool(low), 8=warm(mid), 7=hot(high)
    """
    chars = [' '] * width
    attrs = [0] * width

    vmin, vmax = sig.numeric_range_in_window(t_start, t_end)
    span = t_end - t_start
    if span <= 0:
        return chars, attrs

    scale = width / span
    vrange = vmax - vmin if vmax != vmin else 1.0
    nblocks = len(ANALOG_BLOCKS) - 1  # 0..8

    prev_level = None
    for col in range(width):
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


def draw_analog_braille(sig, t_start, t_end, width, height):
    """Analog line graph at braille sub-pixel resolution: each character cell
    holds 2x4 dots, so `height` rows give 4*height vertical steps and every
    column two samples. Adjacent samples are connected vertically, drawing a
    continuous curve instead of stacked bars. Same contract as
    draw_analog_multi: a list of `height` (chars, attrs) tuples, top first.
    Pure Unicode (U+2800 block) — needs no graphics protocol."""
    height = max(1, height)
    rows = [([' '] * width, [0] * width) for _ in range(height)]
    span = t_end - t_start
    if span <= 0:
        return rows
    vmin, vmax = sig.numeric_range_in_window(t_start, t_end)
    vrange = vmax - vmin if vmax != vmin else 1.0
    vres = 4 * height                       # braille rows top→bottom
    nsub = 2 * width                        # braille columns
    inv = span / nsub

    cells = [[0] * width for _ in range(height)]     # dot bitmasks
    heat = [[0] * width for _ in range(height)]      # attr per cell
    prev_y = None
    for s in range(nsub):
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


def draw_analog_multi(sig, t_start, t_end, width, height):
    """Render an analog signal across `height` character rows (top row = high
    values). Returns a list of `height` (chars, attrs) tuples, top first."""
    rows = [([' '] * width, [0] * width) for _ in range(max(1, height))]
    vmin, vmax = sig.numeric_range_in_window(t_start, t_end)
    span = t_end - t_start
    if span <= 0:
        return rows
    scale = width / span
    vrange = vmax - vmin if vmax != vmin else 1.0
    nsub = len(ANALOG_BLOCKS) - 1          # 8 sub-levels per row
    total = height * nsub                   # full vertical resolution

    for col in range(width):
        t = t_start + col / scale
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
