#!/usr/bin/env python3
"""Generate the Alwavelets app icon — a diamond mark with a bold "W", echoing the
"◆ Alwavelets" header. The W's zig-zag also reads as a little waveform.

Two colour schemes are produced (pick whichever you prefer as the canonical
icon; "black" is the default, matching the dark UI):
    black : dark diamond, white W, thin green edge
    white : white diamond, dark W, thin green edge

Pure stdlib (math + zlib + struct), no dependencies — matching the project's
zero-dependency rule. Emits a scalable SVG (the master) and an anti-aliased
RGBA PNG per scheme, and prints an ASCII preview.

    python3 tools/make_icon.py
"""
import os
import shutil
import struct
import zlib

SIZE = 256
CX = CY = SIZE / 2
R = 116                         # diamond "radius" (centre -> vertex)
BORDER = 7                      # green edge thickness
WTH = 17                        # W stroke thickness

GREEN = (63, 211, 94, 255)      # brand accent (alwavelets/graphics.py HIGH)
DARK  = (16, 18, 26, 255)       # UI background
WHITE = (244, 246, 250, 255)
TRANSPARENT = (0, 0, 0, 0)

# W as a 5-point polyline (top-left, valley, centre peak, valley, top-right).
W_PTS = [(70, 92), (99, 168), (128, 116), (157, 168), (186, 92)]

SCHEMES = {
    "black": dict(diamond=DARK,  stroke=WHITE),
    "white": dict(diamond=WHITE, stroke=DARK),
}


# ── SVG (vector master) ──────────────────────────────────────────────

def _rgb(c):
    return f"rgb({c[0]},{c[1]},{c[2]})"


def write_svg(path, scheme):
    s = SCHEMES[scheme]
    pts = " ".join(f"{CX+dx:.0f},{CY+dy:.0f}" for dx, dy in
                   ((0, -R), (R, 0), (0, R), (-R, 0)))
    wp = " ".join(f"{x},{y}" for x, y in W_PTS)
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" height="{SIZE}">
  <polygon points="{pts}" fill="{_rgb(s['diamond'])}" stroke="{_rgb(GREEN)}" stroke-width="{2*BORDER}" stroke-linejoin="round"/>
  <polyline points="{wp}" fill="none" stroke="{_rgb(s['stroke'])}" stroke-width="{WTH}" stroke-linejoin="round" stroke-linecap="round"/>
</svg>
"""
    with open(path, "w") as f:
        f.write(svg)


# ── PNG (anti-aliased RGBA via supersampling) ────────────────────────

SS = 4
HI = SIZE * SS


def _png_rgba(width, height, buf):
    def chunk(typ, data):
        body = typ + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xffffffff))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += buf[y * width * 4:(y + 1) * width * 4]
    idat = zlib.compress(bytes(raw), 9)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _in_diamond(x, y, r):
    return abs(x - CX * SS) + abs(y - CY * SS) <= r * SS


def _set(buf, x, y, rgba):
    if 0 <= x < HI and 0 <= y < HI:
        i = (y * HI + x) * 4
        buf[i], buf[i + 1], buf[i + 2], buf[i + 3] = rgba


def _stamp(buf, cx, cy, rad, rgba):
    r2 = rad * rad
    for dy in range(-rad, rad + 1):
        for dx in range(-rad, rad + 1):
            if dx * dx + dy * dy <= r2:
                _set(buf, cx + dx, cy + dy, rgba)


def _draw_w(buf, color):
    rad = int(WTH * SS / 2)
    for (x0, y0), (x1, y1) in zip(W_PTS, W_PTS[1:]):
        x0, y0, x1, y1 = x0 * SS, y0 * SS, x1 * SS, y1 * SS
        steps = int(max(abs(x1 - x0), abs(y1 - y0))) + 1
        for i in range(steps + 1):
            t = i / steps
            _stamp(buf, int(x0 + (x1 - x0) * t), int(y0 + (y1 - y0) * t), rad, color)


def _downsample(hi):
    out = bytearray(SIZE * SIZE * 4)
    n = SS * SS
    for y in range(SIZE):
        for x in range(SIZE):
            r = g = b = a = 0
            for dy in range(SS):
                base = ((y * SS + dy) * HI + x * SS) * 4
                for dx in range(SS):
                    j = base + dx * 4
                    r += hi[j]; g += hi[j + 1]; b += hi[j + 2]; a += hi[j + 3]
            o = (y * SIZE + x) * 4
            out[o] = r // n; out[o + 1] = g // n; out[o + 2] = b // n; out[o + 3] = a // n
    return out


def write_png(path, scheme):
    s = SCHEMES[scheme]
    hi = bytearray(HI * HI * 4)                       # transparent
    for y in range(HI):
        for x in range(HI):
            if _in_diamond(x, y, R):                  # green edge
                _set(hi, x, y, GREEN)
    for y in range(HI):
        for x in range(HI):
            if _in_diamond(x, y, R - BORDER):         # diamond fill
                _set(hi, x, y, s["diamond"])
    _draw_w(hi, s["stroke"])
    with open(path, "wb") as f:
        f.write(_png_rgba(SIZE, SIZE, _downsample(hi)))


# ── ASCII preview ────────────────────────────────────────────────────

def ascii_preview(cols=33, rows=17):
    grid = [[" "] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            x = c / (cols - 1) * SIZE
            y = r / (rows - 1) * SIZE
            if abs(x - CX) / R + abs(y - CY) / R <= 1:
                grid[r][c] = "."
    # overlay W
    for (x0, y0), (x1, y1) in zip(W_PTS, W_PTS[1:]):
        steps = 60
        for i in range(steps + 1):
            t = i / steps
            x = x0 + (x1 - x0) * t
            y = y0 + (y1 - y0) * t
            c = int(round(x / SIZE * (cols - 1)))
            r = int(round(y / SIZE * (rows - 1)))
            if 0 <= r < rows and 0 <= c < cols:
                grid[r][c] = "#"
    print("\n".join("  " + "".join(row) for row in grid))


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(here, "assets")
    os.makedirs(assets, exist_ok=True)
    for scheme in SCHEMES:
        write_svg(os.path.join(assets, f"alwavelets_icon_{scheme}.svg"), scheme)
        write_png(os.path.join(assets, f"alwavelets_icon_{scheme}.png"), scheme)
    # canonical icon = black scheme
    for ext in ("svg", "png"):
        shutil.copyfile(os.path.join(assets, f"alwavelets_icon_black.{ext}"),
                        os.path.join(assets, f"alwavelets_icon.{ext}"))
    print("wrote assets/alwavelets_icon_{black,white}.{svg,png} (+ canonical alwavelets_icon.*)\n")
    ascii_preview()


if __name__ == "__main__":
    main()
