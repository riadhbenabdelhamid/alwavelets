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

# Palette index meanings (kept small so sixel stays compact).
BG, HIGH, LOW, EDGE, UNDEF, HIZ, BUS, GRID = range(8)
PALETTE = [
    (16, 18, 26),     # BG    background
    (63, 211, 94),    # HIGH  logic 1
    (31, 122, 58),    # LOW   logic 0
    (232, 80, 80),    # EDGE  transitions
    (232, 200, 64),   # UNDEF x (unknown)
    (111, 170, 255),  # HIZ   z / bus body
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
    raw = bytearray()
    stride = width
    for y in range(height):
        raw.append(0)                                  # filter: none
        raw += indices[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 6)   # 6 is as small as 9 here, ~3x faster
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

def render_waveform_png(vcd, signals=None, t_start=None, t_end=None,
                        width=900, row_h=22, radix=None, arrangement=None):
    """Rasterise selected signals over the window into an indexed PNG (bytes).

    Traces only (single-bit square waves + bus envelopes); names/labels stay as
    terminal text when blitted, or are paired with a text snapshot for export."""
    t_start = 0 if t_start is None else parse_time(vcd, t_start)
    t_end = vcd.max_time if t_end is None else parse_time(vcd, t_end)
    if t_end <= t_start:
        t_end = t_start + 1
    sigs, rx = _resolve_request(vcd, signals, radix, arrangement)
    indices, w, h = _rasterise(vcd, sigs, t_start, t_end, width, row_h, rx)
    return png_encode(w, h, indices)


def _rasterise(vcd, sigs, t_start, t_end, width, row_h, rx):
    width = max(16, int(width))
    height = max(row_h, len(sigs) * row_h)
    cv = Canvas(width, height)
    span = t_end - t_start
    for i, sig in enumerate(sigs):
        y0 = i * row_h
        yhi, ylo = y0 + 3, y0 + row_h - 4
        ymid = (yhi + ylo) // 2
        if sig.size == 1 and getattr(sig, "signal_type", "wire") != "real":
            prev_y = None
            for x in range(width):
                v = sig.value_at(int(t_start + x * span / width))
                if v == "1":
                    y, c = yhi, HIGH
                elif v == "0":
                    y, c = ylo, LOW
                elif v == "x":
                    y, c = ymid, UNDEF
                else:
                    y, c = ymid, HIZ
                if prev_y is not None and prev_y != y:
                    cv.vline(x, prev_y, y, EDGE)
                cv.px(x, y, c)
                prev_y = y
        else:
            tw, vw = sig.changes_in_window(t_start, t_end)
            for j in range(len(tw)):
                xa = int((tw[j] - t_start) / span * width)
                xb = (int((tw[j + 1] - t_start) / span * width)
                      if j + 1 < len(tw) else width)
                undef = any(ch in vw[j] for ch in "xXzZ")
                col = UNDEF if undef else BUS
                cv.hline(xa, xb, yhi, col)
                cv.hline(xa, xb, ylo, col)
                if j > 0:
                    cv.vline(xa, yhi, ylo, EDGE)
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
