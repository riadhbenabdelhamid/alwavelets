"""Headless waveform snapshots.

Render a chosen set of signals over a time window into a static text (Unicode)
or SVG snapshot — without curses. This is what powers the MCP server, but it is
useful on its own (tests, scripting, embedding).
"""
import fnmatch
import json

from .core import Radix, format_value, xml_escape
from .rendering import draw_single_bit_culled, draw_bus_culled

_RADIX = {
    "bin": Radix.BIN, "binary": Radix.BIN,
    "hex": Radix.HEX, "hexadecimal": Radix.HEX,
    "dec": Radix.DEC, "decimal": Radix.DEC, "signed": Radix.DEC,
    "unsigned": Radix.UNSIGNED, "uint": Radix.UNSIGNED,
}

_UNIT_SECONDS = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6,
                 "ms": 1e-3, "s": 1.0}


def parse_time(vcd, value):
    """Convert a time given as ticks (int / numeric str) or a unit string like
    '500ns' / '1.5us' into raw timeline ticks."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    try:
        return int(s)
    except ValueError:
        pass
    for unit in ("fs", "ps", "ns", "us", "ms", "s"):
        if s.endswith(unit):
            num = float(s[:-len(unit)])
            seconds = num * _UNIT_SECONDS[unit]
            base = _UNIT_SECONDS.get(vcd.timescale_unit, 1e-9) * vcd.timescale_number
            return int(round(seconds / base))
    raise ValueError(f"cannot parse time: {value!r}")


# ── SVG colour themes ────────────────────────────────────────────────

_DEFAULT_PALETTE = {"bg": "#10121a", "hi": "#3fd35e", "lo": "#1f7a3a",
                    "bus": "#6faaff", "ud": "#e8c840", "nm": "#99aabb",
                    "rl": "#667", "val": "#dddeee"}
_BUILTIN_PALETTES = {
    "dark": _DEFAULT_PALETTE,
    "light": {"bg": "#fbfbfb", "hi": "#1f7a1f", "lo": "#155515",
              "bus": "#1f5fbf", "ud": "#b07000", "nm": "#445",
              "rl": "#889", "val": "#222"},
    "phosphor": {"bg": "#020802", "hi": "#33ff33", "lo": "#119911",
                 "bus": "#22dd66", "ud": "#cccc33", "nm": "#5b5",
                 "rl": "#383", "val": "#7f7"},
}
_XTERM_BASE16 = ["#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee",
                 "#cd00cd", "#00cdcd", "#e5e5e5", "#7f7f7f", "#ff0000",
                 "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff"]


def _xterm_to_hex(idx):
    """Convert an xterm-256 colour index to '#rrggbb' (None for default -1)."""
    if idx is None or idx < 0:
        return None
    if idx < 16:
        return _XTERM_BASE16[idx]
    if idx < 232:
        idx -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (steps[idx // 36], steps[(idx % 36) // 6], steps[idx % 6])
    v = 8 + (idx - 232) * 10
    return "#%02x%02x%02x" % (v, v, v)


def _resolve_palette(theme):
    """Resolve `theme` (a built-in name, a theme-JSON path, or None) to an SVG
    colour palette."""
    if not theme:
        return _DEFAULT_PALETTE
    if theme in _BUILTIN_PALETTES:
        return _BUILTIN_PALETTES[theme]
    try:
        with open(theme) as f:
            t = json.load(f)
    except (OSError, ValueError):
        return _BUILTIN_PALETTES.get(str(theme).lower(), _DEFAULT_PALETTE)

    def fg(key, default):
        c = _xterm_to_hex(t.get(key, [None])[0]) if key in t else None
        return c or default

    def bg(key, default):
        c = _xterm_to_hex(t.get(key, [None, None])[1]) if key in t else None
        return c or default

    return {"bg": bg("HEADER", _DEFAULT_PALETTE["bg"]), "hi": fg("HIGH", _DEFAULT_PALETTE["hi"]),
            "lo": fg("LOW", _DEFAULT_PALETTE["lo"]), "bus": fg("BUS", _DEFAULT_PALETTE["bus"]),
            "ud": fg("UNDEF", _DEFAULT_PALETTE["ud"]), "nm": fg("SCOPE", _DEFAULT_PALETTE["nm"]),
            "rl": fg("SCOPE", _DEFAULT_PALETTE["rl"]), "val": fg("VAL", _DEFAULT_PALETTE["val"])}


# ── Saved-view (arrangement) loading ─────────────────────────────────

def load_arrangement(vcd, path):
    """Read a saved arrangement file and return (signal_full_names, radix_name).

    Arrangements store display order as VCD signal identifiers; we map them back
    to full names (skipping any no longer present) and read the global radix."""
    with open(path) as f:
        data = json.load(f)
    names = []
    for sid in data.get("display_order", []):
        sig = vcd.signals.get(sid)
        if sig is not None:
            names.append(sig.full_name)
    return names, data.get("radix")


def _resolve_request(vcd, signals, radix, arrangement):
    """Resolve the effective (signal objects, Radix) from explicit args plus an
    optional arrangement (explicit args win)."""
    names = list(signals) if signals else []
    arr_radix = None
    if arrangement:
        arr_names, arr_radix = load_arrangement(vcd, arrangement)
        if not names:
            names = arr_names
    sigs = resolve_signals(vcd, names) if names else []
    rx = None
    if radix:
        rx = _RADIX.get(str(radix).lower())
    if rx is None and arr_radix:
        rx = _RADIX.get(str(arr_radix).lower())
    return sigs, (rx or Radix.HEX)


def resolve_signals(vcd, patterns):
    """Resolve names/globs to signal objects, in request order, de-duplicated.

    A pattern matches a signal's short name or full (scoped) name, exactly or
    as an fnmatch glob (e.g. 'clk', 'top.alu.*', 'data*')."""
    out, seen = [], set()
    for pat in patterns:
        for sid in vcd.signal_order:
            sig = vcd.signals[sid]
            if (sig.full_name == pat or sig.name == pat
                    or fnmatch.fnmatch(sig.full_name, pat)
                    or fnmatch.fnmatch(sig.name, pat)):
                if sig.full_name not in seen:
                    seen.add(sig.full_name)
                    out.append(sig)
    return out


def list_signals(vcd, pattern="*", limit=0):
    """Return signal full-names matching `pattern` (glob), optionally capped."""
    names = [vcd.signals[s].full_name for s in vcd.signal_order
             if fnmatch.fnmatch(vcd.signals[s].full_name, pattern)
             or fnmatch.fnmatch(vcd.signals[s].name, pattern)]
    return names[:limit] if limit else names


def _ruler(vcd, t_start, t_end, name_width, wave_w):
    span = max(1, t_end - t_start)
    line = [" "] * (name_width + 1 + wave_w)
    n = max(2, wave_w // 14)
    last_end = -1
    for i in range(n + 1):
        frac = i / n
        col = name_width + 1 + min(wave_w - 1, int(frac * wave_w))
        t = t_start + int(frac * span)
        label = "┬" + vcd.time_to_str(t)
        if col <= last_end + 1:
            continue
        for k, ch in enumerate(label):
            if col + k < len(line):
                line[col + k] = ch
        last_end = col + len(label) - 1
    return "".join(line).rstrip()


def _apply_bus_labels(chars, labels):
    """Overlay bus value labels onto the bus-fill characters."""
    row = list(chars)
    n = len(row)
    cols = sorted(labels)
    for i, col in enumerate(cols):
        nxt = cols[i + 1] if i + 1 < len(cols) else n
        avail = nxt - col - 1
        if avail <= 0:
            continue
        for k, ch in enumerate(labels[col][:avail]):
            if col + k < n:
                row[col + k] = ch
    return "".join(row)


def render_text(vcd, signals=None, t_start=None, t_end=None, width=100,
                name_width=None, radix=None, header=True, arrangement=None):
    """Render selected signals over [t_start, t_end] as a monospace text block.

    `signals` is a list of names/globs (optional if `arrangement` is given, in
    which case the saved view's signals + radix are used). Times may be ticks or
    unit strings ('500ns'); None means the full trace extent. Returns a string."""
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1

    sigs, rx = _resolve_request(vcd, signals, radix, arrangement)
    if not sigs:
        return "(no signals matched: %s)" % ", ".join(signals or ["(none given)"])

    if name_width is None:
        name_width = min(32, max(len(s.full_name) for s in sigs) + 1)
    wave_w = max(8, int(width) - name_width - 1)

    lines = []
    if header:
        lines.append("%s  [%s .. %s]" % (
            "waveform", vcd.time_to_str(t_start), vcd.time_to_str(t_end)))
    lines.append(_ruler(vcd, t_start, t_end, name_width, wave_w))
    for sig in sigs:
        label = sig.full_name[-name_width:].ljust(name_width)
        if sig.size == 1 and sig.signal_type != "real":
            chars, _attrs = draw_single_bit_culled(sig, t_start, t_end, wave_w)
            wave = "".join(chars)
        else:
            chars, _attrs, labels = draw_bus_culled(sig, t_start, t_end, wave_w, rx)
            wave = _apply_bus_labels(chars, labels)
        lines.append(label + " " + wave)
    return "\n".join(lines)


def render_svg(vcd, signals=None, t_start=None, t_end=None, width=900,
               row_h=26, name_w=140, radix=None, theme=None, arrangement=None):
    """Render selected signals over the window as a standalone SVG string.

    `theme` may be a built-in name (dark/light/phosphor) or a theme-JSON path;
    `arrangement` is a saved-view file supplying signals + radix."""
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1
    sigs, rx = _resolve_request(vcd, signals, radix, arrangement)
    pal = _resolve_palette(theme)

    margin = 12
    wave_w = max(80, int(width) - 2 * margin - name_w)
    span = t_end - t_start
    total_h = margin * 2 + row_h * (len(sigs) + 1)
    total_w = margin * 2 + name_w + wave_w

    out = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d">' % (total_w, total_h),
        '<rect width="%d" height="%d" fill="%s"/>' % (total_w, total_h, pal["bg"]),
        '<style>text{font-family:monospace;font-size:12px;fill:%(val)s}'
        '.nm{fill:%(nm)s}.hi{stroke:%(hi)s;stroke-width:1.5;fill:none}'
        '.lo{stroke:%(lo)s;stroke-width:1.5;fill:none}'
        '.bus{stroke:%(bus)s;stroke-width:1.5;fill:none}'
        '.ud{stroke:%(ud)s;stroke-width:1.5;fill:none}.rl{fill:%(rl)s}</style>' % pal,
    ]
    # ruler
    ry = margin + 12
    out.append('<text x="%d" y="%d" class="rl">%s</text>'
               % (margin, ry, xml_escape(vcd.time_to_str(t_start))))
    out.append('<text x="%d" y="%d" class="rl" text-anchor="end">%s</text>'
               % (total_w - margin, ry, xml_escape(vcd.time_to_str(t_end))))

    for i, sig in enumerate(sigs):
        y0 = margin + row_h * (i + 1)
        ytop, ybot, ymid = y0 + 4, y0 + row_h - 6, y0 + row_h // 2
        x0 = margin + name_w
        out.append('<text x="%d" y="%d" class="nm">%s</text>'
                   % (margin, ymid + 4, xml_escape(sig.full_name[-22:])))
        if sig.size == 1 and sig.signal_type != "real":
            pts, cls = [], "hi"
            for c in range(wave_w):
                t = t_start + c * span / wave_w
                v = sig.value_at(int(t))
                y = ytop if v == "1" else ybot if v == "0" else ymid
                cls = "ud" if v in ("x", "z") else "hi" if v == "1" else "lo"
                pts.append("%d,%d" % (x0 + c, y))
            out.append('<polyline points="%s" class="hi"/>' % " ".join(pts))
        else:
            tw, vw = sig.changes_in_window(t_start, t_end)
            for j in range(len(tw)):
                xa = x0 + int((tw[j] - t_start) / span * wave_w)
                xb = x0 + (int((tw[j + 1] - t_start) / span * wave_w)
                           if j + 1 < len(tw) else wave_w)
                cls = "ud" if any(c in vw[j] for c in "xXzZ") else "bus"
                out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="%s"/>'
                           % (xa, ytop, xb, ytop, cls))
                out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" class="%s"/>'
                           % (xa, ybot, xb, ybot, cls))
                if xb - xa > 22:
                    lbl = format_value(vw[j], rx, sig.size)
                    out.append('<text x="%d" y="%d" font-size="10">%s</text>'
                               % (xa + 3, ymid + 3, xml_escape(lbl)))
    out.append("</svg>")
    return "\n".join(out)


# ── Measurements ─────────────────────────────────────────────────────

def _freq_hz(vcd, period_ticks):
    base = _UNIT_SECONDS.get(vcd.timescale_unit, 1e-9) * vcd.timescale_number
    secs = period_ticks * base
    return (1.0 / secs) if secs > 0 else None


def _fmt_hz(hz):
    if hz is None:
        return "?"
    for unit, scale in (("GHz", 1e9), ("MHz", 1e6), ("kHz", 1e3)):
        if hz >= scale:
            return "%.3g%s" % (hz / scale, unit)
    return "%.3gHz" % hz


def measure(vcd, signals, t_start=None, t_end=None):
    """Measure per-signal stats (transitions, period/frequency) over the window,
    plus the edge-to-edge delta between the first two signals."""
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1
    sigs = resolve_signals(vcd, signals)
    if not sigs:
        return "(no signals matched: %s)" % ", ".join(signals or ["(none given)"])

    lines = ["measurements  [%s .. %s]" % (vcd.time_to_str(t_start), vcd.time_to_str(t_end))]
    for sig in sigs:
        trans = sig.transition_count_in_window(t_start, t_end)
        period, _ = sig.detect_frequency()
        if period:
            info = "period=%s freq=%s" % (vcd.time_to_str(period), _fmt_hz(_freq_hz(vcd, period)))
        else:
            info = "not periodic"
        lines.append("  %-28s transitions=%-5d %s" % (sig.full_name[-28:], trans, info))

    if len(sigs) >= 2:
        a, b = sigs[0], sigs[1]
        ea = a.next_edge_after(t_start - 1)
        if ea is not None and ea <= t_end:
            eb = b.next_edge_after(ea - 1)
            if eb is not None and eb <= t_end:
                lines.append("  edge-delta: %s@%s -> %s@%s = %s" % (
                    a.name, vcd.time_to_str(ea), b.name, vcd.time_to_str(eb),
                    vcd.time_to_str(eb - ea)))
    return "\n".join(lines)
