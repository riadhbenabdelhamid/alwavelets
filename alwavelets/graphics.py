"""Pixel-accurate waveform rendering for terminals with a graphics protocol.

Everything here is pure Python (stdlib only). The waveform is rasterised into a
small palette-indexed pixel buffer, which is then encoded as a PNG (for the
kitty and iTerm2 protocols) or as sixel. Terminals without graphics support are
detected and callers fall back to the normal character-cell rendering, so this
never hinders portability.
"""
import base64
import os
import struct
import zlib

from .snapshot import _resolve_request, parse_time
from .streaming import iter_window, check_cancel

# Palette index meanings (kept small so sixel stays compact).
BG, HIGH, LOW, EDGE, UNDEF, HIZ, BUS, GRID = range(8)
PALETTE = [
    (16, 18, 26),     # BG    background
    (63, 211, 94),    # HIGH  logic 1
    (31, 122, 58),    # LOW   logic 0
    (232, 80, 80),    # EDGE  transitions
    (232, 200, 64),   # UNDEF x (unknown)
    (192, 149, 255),  # HIZ   high impedance (distinct from unknown and bus)
    (111, 170, 255),  # BUS   bus body
    (60, 64, 84),     # GRID  faint guide
]


# ── palette-indexed canvas ───────────────────────────────────────────

class Canvas:
    def __init__(self, w, h, bg=BG):
        self.w, self.h = w, h
        self.buf = bytearray([bg]) * (w * h)

    def px(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.buf[y * self.w + x] = c

    def hline(self, x0, x1, y, c):
        if 0 <= y < self.h:
            if x0 > x1:
                x0, x1 = x1, x0
            row = y * self.w
            for x in range(max(0, x0), min(self.w, x1 + 1)):
                self.buf[row + x] = c

    def vline(self, x, y0, y1, c):
        if 0 <= x < self.w:
            if y0 > y1:
                y0, y1 = y1, y0
            for y in range(max(0, y0), min(self.h, y1 + 1)):
                self.buf[y * self.w + x] = c


# ── PNG (indexed colour) encoder ─────────────────────────────────────

def png_encode(width, height, indices, palette=PALETTE):
    """Encode a palette-indexed buffer as an 8-bit indexed PNG (bytes)."""
    def chunk(typ, data):
        body = typ + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xffffffff))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)  # 8-bit, indexed
    plte = b"".join(struct.pack("BBB", *c) for c in palette)
    encoder = zlib.compressobj(6)
    parts = []
    for y in range(height):
        parts.append(encoder.compress(b'\0' + indices[y * width:(y + 1) * width]))
    parts.append(encoder.flush())
    idat = b''.join(parts)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"PLTE", plte)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


# ── sixel encoder (palette-indexed, run-length encoded) ──────────────

def sixel_encode(width, height, indices, palette=PALETTE):
    out = ["\x1bP0;1;0q"]
    for i, (r, g, b) in enumerate(palette):
        out.append("#%d;2;%d;%d;%d" % (i, r * 100 // 255, g * 100 // 255, b * 100 // 255))
    for band in range(0, height, 6):
        first = True
        for ci in range(len(palette)):
            if not first:
                out.append("$")          # overlay this colour on the same band
            first = False
            out.append("#%d" % ci)
            run_ch, run_n = None, 0
            for x in range(width):
                bits = 0
                for row in range(6):
                    y = band + row
                    if y < height and indices[y * width + x] == ci:
                        bits |= 1 << row
                ch = chr(63 + bits)
                if ch == run_ch:
                    run_n += 1
                else:
                    if run_ch is not None:
                        out.append(run_ch if run_n == 1 else "!%d%s" % (run_n, run_ch))
                    run_ch, run_n = ch, 1
            if run_ch is not None:
                out.append(run_ch if run_n == 1 else "!%d%s" % (run_n, run_ch))
        out.append("-")                  # next band
    out.append("\x1b\\")
    return "".join(out)


# ── waveform rasteriser ──────────────────────────────────────────────

def _png_chunk(kind, data):
    body = kind + data
    return struct.pack('>I', len(data)) + body + struct.pack('>I', zlib.crc32(body) & 0xffffffff)


def iter_waveform_png(vcd, signals=None, t_start=None, t_end=None,
                       width=900, row_h=22, radix=None, arrangement=None, *,
                       cancel=None, progress=None):
    """Encode rows incrementally, with working memory independent of events."""
    check_cancel(cancel)
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1
    from .core import ensure_trace_loaded
    ensure_trace_loaded(vcd, t_start, t_end)
    sigs, rx = _resolve_request(vcd, signals, radix, arrangement)
    if not sigs:
        raise ValueError("No signals matched; specify signals or a saved arrangement")
    width, row_h = max(16, int(width)), max(8, int(row_h))
    yield b'\x89PNG\r\n\x1a\n'
    yield _png_chunk(b'IHDR', struct.pack('>IIBBBBB', width, row_h * len(sigs), 8, 3, 0, 0, 0))
    yield _png_chunk(b'PLTE', b''.join(bytes(color) for color in PALETTE))
    encoder, pending = zlib.compressobj(6), bytearray()
    for i, sig in enumerate(sigs):
        check_cancel(cancel)
        indices, _, _ = _rasterise(vcd, [sig], t_start, t_end, width, row_h, rx, cancel)
        for y in range(row_h):
            pending.extend(encoder.compress(b'\0' + indices[y * width:(y + 1) * width]))
            if len(pending) >= 65536:
                yield _png_chunk(b'IDAT', pending)
                pending.clear()
        if progress is not None:
            progress(i + 1, len(sigs))
    pending.extend(encoder.flush())
    if pending:
        yield _png_chunk(b'IDAT', pending)
    check_cancel(cancel)
    yield _png_chunk(b'IEND', b'')


def render_waveform_png(vcd, signals=None, t_start=None, t_end=None,
                        width=900, row_h=22, radix=None, arrangement=None):
    """Return PNG bytes; write_waveform_png() streams large exports to disk."""
    return b''.join(iter_waveform_png(vcd, signals, t_start, t_end, width, row_h, radix, arrangement))


def write_waveform_png(vcd, destination, *args, cancel=None, progress=None, **kwargs):
    """Write PNG with at most one signal row resident; replace paths atomically."""
    from .exporting import atomic_output
    written = 0
    with atomic_output(destination, binary=True) as stream:
        for chunk in iter_waveform_png(vcd, *args, cancel=cancel, progress=progress, **kwargs):
            written += stream.write(chunk)
    return written


def _rasterise(vcd, sigs, t_start, t_end, width, row_h, rx, cancel=None):
    width = max(16, int(width))
    height = max(row_h, len(sigs) * row_h)
    cv = Canvas(width, height)
    span = t_end - t_start
    def column(t):
        # Avoid float rounding at large absolute timestamps; the final tick
        # belongs to the final pixel rather than falling beyond the canvas.
        return max(0, min(width - 1, (t - t_start) * width // span))

    for i, sig in enumerate(sigs):
        y0 = i * row_h
        yhi, ylo = y0 + 3, y0 + row_h - 4
        ymid = (yhi + ylo) // 2
        scalar = sig.size == 1 and getattr(sig, "signal_type", "wire") not in ('real', 'realtime')
        levels = {"1": (yhi, HIGH), "0": (ylo, LOW), "x": (ymid, UNDEF), "z": (ymid, HIZ)}
        # Paint levels immediately. Edges go into a pixel-sized overlay so
        # they still win over all levels, as in the original two-pass renderer.
        # This retains same-tick pulses and x/z history without event lists.
        overlay = Canvas(width, row_h, bg=255)
        exceptional = bytearray(width)
        def segment(start, end, value):
            xa = column(start)
            xb = width - 1 if end == t_end else max(xa, column(end) - 1)
            if scalar:
                y, color = levels.get(value, (ymid, UNDEF))
                cv.hline(xa, xb, y, color)
            else:
                color = UNDEF if "x" in value else HIZ if "z" in value else BUS
                cv.hline(xa, xb, yhi, color)
                cv.hline(xa, xb, ylo, color)

        cursor, current = t_start, "x"
        initialized = False
        for t, value in iter_window(sig, t_start, t_end, cancel=cancel):
            value = str(value).lower()
            if t < t_start:
                current, initialized = value, True
                continue
            if t > cursor:
                segment(cursor, t, current)
            if value != current and (initialized or t > t_start):
                x = column(t)
                if scalar:
                    ya = levels.get(current, (ymid, UNDEF))[0]
                    yb = levels.get(value, (ymid, UNDEF))[0]
                    overlay.vline(x, min(ya, yb) - y0, max(ya, yb) - y0, EDGE)
                    overlay.px(x, yb - y0, levels.get(value, (ymid, UNDEF))[1])
                else:
                    overlay.vline(x, yhi - y0, ylo - y0, EDGE)
                if 'x' in current or 'x' in value:
                    exceptional[x] |= 1
                if 'z' in current or 'z' in value:
                    exceptional[x] |= 2
            cursor, current, initialized = t, value, True
        segment(cursor, t_end, current)
        for pos, color in enumerate(overlay.buf):
            if color != 255:
                cv.buf[y0 * width + pos] = color
        for x, states in enumerate(exceptional):
            if states & 1:
                cv.px(x, ymid, UNDEF)
            if states & 2:
                cv.px(x, ymid + (1 if states & 1 else 0), HIZ)
    return cv.buf, width, height


# ── terminal graphics protocol: detection + encoding ─────────────────

def detect_protocol(env=None):
    """Return 'kitty', 'iterm', 'sixel', or None based on the environment."""
    e = env if env is not None else os.environ
    if e.get("KITTY_WINDOW_ID") or "kitty" in e.get("TERM", "").lower() \
            or e.get("TERM_PROGRAM") == "ghostty":
        return "kitty"
    if e.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm") or e.get("LC_TERMINAL") == "iTerm2":
        return "iterm"
    term = e.get("TERM", "").lower()
    if "sixel" in term or any(t in term for t in ("foot", "mlterm", "yaft")):
        return "sixel"
    return None


def kitty_sequence(png, cols=None, rows=None):
    """kitty graphics protocol: transmit+display a PNG, chunked, base64."""
    data = base64.b64encode(png).decode("ascii")
    ctrl = "f=100,a=T"
    if cols:
        ctrl += ",c=%d" % cols
    if rows:
        ctrl += ",r=%d" % rows
    out, first, n = [], True, len(data)
    for i in range(0, n, 4096):
        part = data[i:i + 4096]
        more = 1 if i + 4096 < n else 0
        head = (ctrl + ",m=%d" % more) if first else "m=%d" % more
        out.append("\x1b_G%s;%s\x1b\\" % (head, part))
        first = False
    return "".join(out)


def iterm_sequence(png, cols=None, rows=None):
    """iTerm2 inline image escape."""
    data = base64.b64encode(png).decode("ascii")
    args = "inline=1;preserveAspectRatio=0"
    if cols:
        args += ";width=%d" % cols
    if rows:
        args += ";height=%d" % rows
    return "\x1b]1337;File=%s:%s\x07" % (args, data)


def render_inline(vcd, signals=None, t_start=None, t_end=None, width=900,
                  row_h=22, radix=None, arrangement=None, cols=None, rows=None,
                  protocol=None):
    """Render the waveform for inline terminal display.

    Returns (protocol, escape_sequence) for a supported terminal, or
    (None, png_bytes) when there is no graphics protocol — so the caller can
    fall back to saving the PNG. One-shot and cheap (~5-10ms)."""
    protocol = protocol if protocol is not None else detect_protocol()
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1
    sigs, rx = _resolve_request(vcd, signals, radix, arrangement)
    indices, w, h = _rasterise(vcd, sigs, t_start, t_end, width, row_h, rx)
    if protocol == "sixel":
        return protocol, sixel_encode(w, h, indices)
    png = png_encode(w, h, indices)
    if protocol == "kitty":
        return protocol, kitty_sequence(png, cols, rows)
    if protocol == "iterm":
        return protocol, iterm_sequence(png, cols, rows)
    return None, png


def blit_sequence(png, indices=None, dims=None, cols=None, rows=None, protocol=None):
    """Return the escape sequence to display `png`/image inline for the detected
    (or given) protocol, or None if the terminal has no graphics support."""
    protocol = protocol or detect_protocol()
    if protocol == "kitty":
        return kitty_sequence(png, cols, rows)
    if protocol == "iterm":
        return iterm_sequence(png, cols, rows)
    if protocol == "sixel" and indices is not None and dims is not None:
        return sixel_encode(dims[0], dims[1], indices)
    return None
