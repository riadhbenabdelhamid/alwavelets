#!/usr/bin/env python3
"""Record the README demo: real screens of the Alwavelets viewer, animated.

The viewer runs inside a detached tmux session, is driven with the same
keystrokes a user would type, and every resulting screen is captured from the
pane — characters and colours — so each frame is what the app actually
draws, not a mock-up. The frames are assembled into:

  * an animated SVG (CSS keyframes, no scripts): crisp at any size, small,
    and GitHub renders it in a README `<img>`;
  * optionally a GIF via Pillow, for sites that do not animate SVG.

Usage:
    python3 tools/make_demo.py                         # assets/alwavelets-demo.svg
    python3 tools/make_demo.py --gif assets/alwavelets-demo.gif
    python3 tools/make_demo.py --png-dir /tmp/frames   # one PNG per frame

Requires tmux and `pip install pyte` (used to parse the captured colour
codes), plus Pillow for --gif/--png-dir. None of these is a runtime
dependency of Alwavelets; this script is tooling only.
"""
import argparse
import os
import shlex
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

COLS, ROWS = 104, 30

# ── terminal palette used for the rendering (ANSI names come from pyte) ──
TERM_BG = "#0d1117"
DEFAULT_FG = "#d4d8e0"
ANSI = {
    "black": "#3b4252", "red": "#e06c75", "green": "#98c379", "brown": "#e5c07b",
    "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#c8ccd4",
    "brightblack": "#5c6370", "brightred": "#ef7a85", "brightgreen": "#a9d48a",
    "brightbrown": "#f0d58a", "brightblue": "#75bdf5", "brightmagenta": "#d58ae8",
    "brightcyan": "#6fc5d0", "brightwhite": "#ffffff",
}
def color(c, default):
    if c is None or c == "default":
        return default
    if c in ANSI:
        return ANSI[c]
    if len(c) == 6:
        return "#" + c
    return default


# ── pty session ──────────────────────────────────────────────────────

class Session:
    """The viewer running inside a detached tmux session of a fixed size.

    tmux is a real terminal emulator, so what it shows is what a user sees
    (an earlier pyte-only capture mis-handled ncurses' repeat-character
    output and left stale cells). `capture-pane -e` returns each row with
    its colour codes; pyte is used only to parse those into cells.
    """
    def __init__(self, args, cols=COLS, rows=ROWS):
        self.cols, self.rows = cols, rows
        self.name = "alwdemo-%d" % os.getpid()
        env = "TERM=xterm-256color ESCDELAY=300 LANG=C.UTF-8 LC_ALL=C.UTF-8"
        cmd = "cd %s && %s %s; sleep 5" % (shlex.quote(ROOT), env, " ".join(shlex.quote(a) for a in args))
        subprocess.run(["tmux", "new-session", "-d", "-s", self.name, "-x", str(cols), "-y", str(rows), cmd],
                       check=True)
        self._last = None

    def _tmux(self, *args, capture=False):
        r = subprocess.run(["tmux", *args], capture_output=True, text=True)
        return r.stdout if capture else None

    def drain(self, quiet=0.35, limit=6.0):
        """Wait until the pane stops changing (two identical captures)."""
        t0 = time.time()
        prev = None
        while time.time() - t0 < limit:
            time.sleep(quiet)
            cur = self._tmux("capture-pane", "-t", self.name, "-p", "-e", capture=True)
            if cur == prev:
                break
            prev = cur
        self._last = prev

    def send(self, keys, settle=0.35):
        """Send a key: a name tmux knows (Enter, Escape, Right, Tab, F4, F12,
        Space) or a literal string."""
        named = {"ENTER": "Enter", "ESC": "Escape", "RIGHT": "Right", "TAB": "Tab",
                 "SPACE": "Space", "F4": "F4", "F6": "F6", "F7": "F7", "F12": "F12"}
        if keys in named:
            self._tmux("send-keys", "-t", self.name, named[keys])
        else:
            self._tmux("send-keys", "-t", self.name, "-l", self._literal(keys))
        self.drain(quiet=settle)

    @staticmethod
    def _literal(text):
        # A lone ';' argument is tmux's command separator even with -l; it
        # must be escaped or the command prompt never opens.
        return "\\;" if text == ";" else text

    def type(self, text, delay=0.05):
        for ch in text:
            self._tmux("send-keys", "-t", self.name, "-l", self._literal(ch))
            time.sleep(delay)
        self.drain()

    def snapshot(self):
        import pyte
        raw = self._last if self._last is not None else self._tmux(
            "capture-pane", "-t", self.name, "-p", "-e", capture=True)
        screen = pyte.Screen(self.cols, self.rows)
        stream = pyte.Stream(screen)
        lines = raw.split("\n")[:self.rows]
        for y, line in enumerate(lines):
            stream.feed("\x1b[%d;1H%s" % (y + 1, line))
        buf = screen.buffer
        return [[(buf[y][x].data or " ", buf[y][x].fg, buf[y][x].bg,
                  buf[y][x].bold, buf[y][x].reverse) for x in range(self.cols)]
                for y in range(self.rows)]

    def text(self):
        return "\n".join("".join(c[0] for c in row) for row in self.snapshot())

    def focus_row(self):
        for y, row in enumerate(self.snapshot()):
            if y >= 3 and "▸" in "".join(c[0] for c in row[:3]):
                return y
        return None

    def focus_on(self, needle, tries=14):
        """Move the focus (j/k) until the row containing `needle` is focused."""
        for _ in range(tries):
            rows = self.text().splitlines()
            target = next((i for i, r in enumerate(rows) if needle in r and i >= 3), None)
            cur = self.focus_row()
            if target is None or cur is None or target == cur:
                return
            self.send("j" if target > cur else "k", settle=0.2)

    def close(self):
        self._tmux("send-keys", "-t", self.name, "q")
        time.sleep(0.4)
        self._tmux("kill-session", "-t", self.name)


# ── the storyboard ───────────────────────────────────────────────────

class Frame:
    def __init__(self, cells, step, keys, title, desc, hold):
        self.cells, self.step, self.keys, self.title, self.desc, self.hold = \
            cells, step, keys, title, desc, hold


def record(verbose=False):
    frames = []
    step = [0]

    def shot(keys, title, desc, hold=2.6, new_step=True):
        if new_step:
            step[0] += 1
        frames.append(Frame(s.snapshot(), step[0], keys, title, desc, hold))
        if verbose:
            print("--- %02d %s\n%s" % (step[0], title, s.text()), file=sys.stderr)

    s = Session([sys.executable, "alwavelets.py", "--demo", "--no-config",
                 "--no-session", "--control"])
    s.drain(quiet=0.8, limit=15)
    shot(["alwavelets design.vcd"], "A waveform viewer that lives in your terminal",
         "Scope tree, grouped signals, buses, analog rows and cursor readouts — pure Python, "
         "no dependencies, no display server. Works the same over SSH, in tmux, on a bare console.", 4.0)

    s.send("s")
    shot(["s"], "Give the waves the whole width",
         "s hides or shows the hierarchy pane; Tab moves focus between the pane and the waves.", 2.4)

    s.send("+"); s.send("+"); s.send("+")
    shot(["+", "-", "←→", "Z"], "Zoom and pan",
         "+ and - zoom around the cursor, ←→ or h/l pan, Z fits the whole trace, W fits between "
         "cursors A and B. Everything is vim-friendly: j/k, gg, G, Shift+↑↓ jump between modules.", 2.8)

    s.send("/"); s.type("sta")
    shot(["/", "sta…"], "Fuzzy-find any signal in the file",
         "/ filters every signal as you type, fzf-style. ↑↓ choose, Tab marks several, "
         "Enter adds them to the view and jumps there.", 2.0)
    s.type("te"); s.send("ENTER")
    shot(["Enter"], "Fuzzy-find any signal in the file",
         "state is now on screen and focused. Signals already displayed are marked •; "
         "the finder never loses your place.", 2.4, new_step=False)

    s.send("x")
    shot(["x"], "Expand a bus into bits",
         "x splits the focused bus into per-bit lanes (x again folds them). F does the same for "
         "float fields: sign, exponent, mantissa, decoded value.", 2.8)

    s.send("R")
    shot(["R"], "Radix per signal",
         "R cycles the focused signal's radix — hex, dec, unsigned, bin, f16/f32/f64 — while r "
         "changes all of them. Enum decode maps (;decode) turn values into names.", 2.6)

    s.send(";"); s.type("when valid && ready")
    shot([";when valid && ready"], "Search with an expression over signals",
         "The ; prompt is the command line. ;when jumps the cursor to the first time a boolean "
         "expression holds: &&, ||, !, comparisons, hex/bin literals, any signal in the file.", 2.2)
    s.send("ENTER")
    shot(["Enter"], "Search with an expression over signals",
         "Cursor A lands at the first handshake. The status line echoes where and why; "
         "n repeats a search, ;find moves focus by name.", 2.8, new_step=False)

    s.send("2"); s.send("]"); s.send("]")
    shot(["2", "]", "]"], "Two cursors, edge snapping",
         "1 and 2 place cursors A and B; ] and [ snap the active cursor to the next or previous "
         "edge of the focused signal. The status bar shows Δ between them.", 2.8)

    s.send("c"); s.send("1")                      # hide B, back to A: later frames stay clean
    s.send(";"); s.type("glitches"); s.send("ENTER")
    shot([";glitches"], "Find glitches and x-hazards",
         ";glitches scans the trace for runt pulses, zero-width glitches and transitions into x, "
         "counts them and jumps to the next one.", 3.0)

    s.send("g"); s.send("g")
    s.send(";"); s.type("stats"); s.send("ENTER")
    shot(["gg", ";stats"], "Measure a signal",
         ";stats reads the focused signal over the window or between cursors: transitions, period "
         "and frequency, duty cycle — min/max for buses. e measures edge to edge.", 3.0)

    s.send("m"); s.type("first handshake"); s.send("ENTER")
    shot(["m", "first handshake"], "Mark what matters",
         "m drops a labelled marker at the cursor; < and > jump between markers, ;marker 220ns "
         "places one by time. A adds a note on a signal or an interval; both persist in sidecars.", 2.6)
    s.send("F12")
    shot(["F12"], "Mark what matters",
         "F12 manages markers: filter by label or time, Enter jumps, u / Ctrl-R undo and redo.",
         2.4, new_step=False)
    s.send("ESC")

    s.send(";"); s.type("group Control rst_n enable"); s.send("ENTER")
    s.focus_on("Control")
    s.send("TAB")
    shot([";group Control rst_n enable", "Tab"], "Group and fold",
         ";group bundles signals under a collapsible header (nest with parent/child), Tab folds "
         "the focused header. V selects a range visually; J/K reorder; u undoes any layout change.", 3.0)

    s.send("ESC")
    s.send("B"); s.send("RIGHT")
    shot(["B", "→"], "Browse the scope tree",
         "B opens the full-screen browser of every module and signal in the file. "
         "→ expands a scope, Space adds or removes a signal, Enter selects a whole scope.", 2.8)
    for _ in range(11):                      # down past top's ten signals to alu/
        s.send("j", settle=0.15)
    s.send("RIGHT"); s.send("j"); s.send("SPACE")
    shot(["j…", "→", "Space"], "Browse the scope tree",
         "Ticks show what is on screen; toggling a signal updates the waves behind the browser.",
         2.2, new_step=False)
    s.send("ESC")

    s.send(";"); s.type("theme light"); s.send("ENTER")
    shot([";theme light", "\\"], "Themes",
         "\\ cycles the built-in themes; ;theme loads one by name or from a JSON file. "
         "True-colour #rrggbb themes quantize gracefully on 256-colour terminals.", 2.2)
    s.send(";"); s.type("theme phosphor"); s.send("ENTER")
    shot([";theme phosphor"], "Themes",
         "Light, dark, calm, minimal, phosphor, futuristic, mono — or your own.", 2.2, new_step=False)

    s.send("F4"); s.type("glit")
    shot(["F4", "glit…"], "Every action, searchable",
         "F4 or Ctrl-P opens a palette of all actions with their shortcuts; type to filter, "
         "Enter runs. ? is the quick reference, F1 the full manual.", 3.0)
    s.send("ESC")

    s.send(";"); s.type("session"); s.send("ENTER")
    shot([";session"], "Keep the session",
         ";session writes a sidecar next to the trace — layout, groups, radixes, colours, zoom, "
         "theme — and reopening restores it. X exports SVG; E opens the export menu "
         "(CSV, PDF, screenshot, pixel-accurate PNG).", 3.0)

    # An agent drives the same live session over the control socket.
    agent_ok = False
    try:
        from alwavelets.control import list_sessions, ControlClient
        live = [i for i in list_sessions()
                if not i.get("stale") and "demo_waveform" in (i.get("trace") or "")]
        info = max(live, key=lambda i: i.get("started", 0)) if live else None
        if info:
            with ControlClient(info) as c:
                c.command("marker 500ns agent was here")
                for _ in range(4):
                    c.command("zoom 300ns 700ns")
                    # the flash message hides the status bar for 1.5 s; the
                    # ◆AGENT badge stays lit for 2 s after the last request
                    time.sleep(1.6)
                    s.drain(quiet=0.15, limit=0.6)
                    if "◆AGENT" in s.text():
                        break
            agent_ok = True
    except Exception as e:                      # noqa: BLE001 — the demo must not die here
        print("agent scene skipped:", e, file=sys.stderr)
    if agent_ok:
        shot(["--control", "viewer_command", "zoom 300ns 700ns"], "Let an agent drive it",
             "Started with --control, an MCP client (or any local agent) can group, search, zoom "
             "and mark this live session while you watch — the ◆AGENT badge shows when it acts. "
             "--mcp also serves headless snapshots of any trace.", 4.0)
    s.close()
    return frames


# ── SVG writer ───────────────────────────────────────────────────────

CW, CH, FS = 7.8, 17, 13            # cell width/height, font size (px)
PAD, BAR, CAP = 14, 30, 82          # outer padding, title bar, caption height
MONO = "'SFMono-Regular', Consolas, 'Liberation Mono', 'DejaVu Sans Mono', Menlo, monospace"
SANS = "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def runs(row):
    """Group a row's cells into runs of identical style: (x, text, fg, bg, bold)."""
    out, start = [], 0
    while start < len(row):
        ch, fg, bg, bold, rev = row[start]
        end = start + 1
        while end < len(row) and row[end][1:] == (fg, bg, bold, rev):
            end += 1
        text = "".join(c[0] for c in row[start:end])
        f, b = color(fg, DEFAULT_FG), color(bg, TERM_BG)
        if rev:
            f, b = b, f
        out.append((start, text, f, b, bold))
        start = end
    return out


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines[:2]


def write_svg(frames, path):
    W = int(PAD * 2 + COLS * CW)
    term_h = ROWS * CH
    H = PAD + BAR + term_h + CAP + PAD
    total = sum(f.hold for f in frames)
    fade = 0.35 / total * 100
    css = ["text{white-space:pre}",
           ".t{font-family:%s;font-size:%dpx}" % (MONO, FS),
           ".b{font-weight:bold}",
           ".cap{font-family:%s}" % SANS,
           ".k{font-family:%s;font-size:11.5px;font-weight:600;fill:#e6edf3}" % MONO]
    t = 0.0
    for i, f in enumerate(frames):
        a, b = t / total * 100, (t + f.hold) / total * 100
        if i == 0:
            kf = "0%%,%.3f%%{opacity:1}%.3f%%,100%%{opacity:0}" % (b, min(100, b + fade))
        elif i == len(frames) - 1:
            kf = "0%%,%.3f%%{opacity:0}%.3f%%,100%%{opacity:1}" % (a, min(100, a + fade))
        else:
            kf = "0%%,%.3f%%{opacity:0}%.3f%%,%.3f%%{opacity:1}%.3f%%,100%%{opacity:0}" % (
                a, min(100, a + fade), b, min(100, b + fade))
        css.append("@keyframes f%d{%s}" % (i, kf))
        css.append(".f%d{animation:f%d %.2fs linear infinite}" % (i, i, total))
        t += f.hold
    css.append("@keyframes bar{from{width:0}to{width:%dpx}}" % (W - 2 * PAD))
    css.append(".bar{animation:bar %.2fs linear infinite}" % total)
    # Pause while hovered (works when the SVG is opened as a document; an
    # <img> in a README gets no pointer events) and respect reduced motion
    # by holding the first frame.
    css.append("svg:hover g[class^=f],svg:hover .bar{animation-play-state:paused}")
    css.append("@media (prefers-reduced-motion:reduce){g[class^=f],.bar{animation:none}"
               "g[class^=f]{opacity:0}.f0{opacity:1}}")

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
           'font-size="%d">' % (W, H, W, H, FS),
           "<style>%s</style>" % "\n".join(css),
           '<rect width="100%%" height="100%%" rx="10" fill="#161b22"/>',
           # window chrome
           '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s"/>' % (
               PAD, PAD, W - 2 * PAD, BAR + term_h, TERM_BG),
           '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="#21262d"/>' % (PAD, PAD, W - 2 * PAD, BAR),
           '<rect x="%d" y="%d" width="%d" height="8" fill="#21262d"/>' % (PAD, PAD + BAR - 8, W - 2 * PAD),
           '<circle cx="%d" cy="%d" r="5.5" fill="#ff5f57"/>' % (PAD + 18, PAD + BAR / 2),
           '<circle cx="%d" cy="%d" r="5.5" fill="#febc2e"/>' % (PAD + 38, PAD + BAR / 2),
           '<circle cx="%d" cy="%d" r="5.5" fill="#28c840"/>' % (PAD + 58, PAD + BAR / 2),
           '<text x="%d" y="%d" class="cap" font-size="12.5" fill="#8b949e" text-anchor="middle">'
           'alwavelets — demo_waveform.vcd</text>' % (W / 2, PAD + BAR / 2 + 4.5)]
    x0, y0 = PAD, PAD + BAR
    cap_y = y0 + term_h
    for i, f in enumerate(frames):
        g = ['<g class="f%d">' % i]
        for y, row in enumerate(f.cells):
            ty = y0 + y * CH + CH - 4.5
            rects, spans = [], []
            for x, text, fg, bg, bold in runs(row):
                if bg != TERM_BG:
                    rects.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s"/>' % (
                        x0 + x * CW, y0 + y * CH, len(text) * CW, CH, bg))
                if text.strip():
                    spans.append('<tspan x="%.1f" textLength="%.1f" lengthAdjust="spacingAndGlyphs"'
                                 '%s%s>%s</tspan>' % (
                                     x0 + x * CW, len(text) * CW,
                                     ' fill="%s"' % fg if fg != DEFAULT_FG else "",
                                     ' class="b"' if bold else "", esc(text)))
            g.extend(rects)
            if spans:
                g.append('<text class="t" y="%.1f" fill="%s" xml:space="preserve">%s</text>' % (
                    ty, DEFAULT_FG, "".join(spans)))
        # caption
        cy = cap_y + 26
        cx = x0 + 4
        g.append('<text x="%d" y="%d" class="cap" font-size="11" fill="#6e7681">%02d</text>' % (cx, cy, f.step))
        cx += 26
        for k in f.keys:
            w = len(k) * 7.0 + 14
            g.append('<rect x="%d" y="%d" width="%d" height="20" rx="5" fill="#30363d" stroke="#484f58"/>' % (
                cx, cy - 14.5, w))
            g.append('<text x="%d" y="%d" class="k">%s</text>' % (cx + 7, cy, esc(k)))
            cx += w + 6
        g.append('<text x="%d" y="%d" class="cap" font-size="15" font-weight="600" fill="#e6edf3">%s</text>' % (
            cx + 6, cy + 0.5, esc(f.title)))
        for j, line in enumerate(wrap(f.desc, 128)):
            g.append('<text x="%d" y="%d" class="cap" font-size="12.5" fill="#9da7b3">%s</text>' % (
                x0 + 4, cy + 21 + j * 17, esc(line)))
        g.append("</g>")
        out.extend(g)
    out.append('<rect x="%d" y="%d" height="3" rx="1.5" fill="#30363d" width="%d"/>' % (PAD, H - PAD - 3, W - 2 * PAD))
    out.append('<rect class="bar" x="%d" y="%d" height="3" rx="1.5" fill="#58a6ff" width="0"/>' % (PAD, H - PAD - 3))
    out.append("</svg>")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    return os.path.getsize(path), total


# ── PNG / GIF writer (Pillow, optional) ──────────────────────────────

def _glyph_font(ch, mono, fallback, _cache={}):
    """DejaVu Sans Mono lacks braille and a few symbols; fall back to DejaVu
    Sans for any character it would render as the .notdef box."""
    if ch not in _cache:
        from PIL import Image
        bits = lambda c: Image.Image()._new(mono.getmask(c)).tobytes()
        _cache[ch] = fallback if bits(ch) == bits("\ue000") else mono
    return _cache[ch]


def render_png(frame, fonts):
    from PIL import Image, ImageDraw
    mono, mono_b, sans, sans_b, small, chip, fallback = fonts
    W = int(PAD * 2 + COLS * CW)
    term_h = ROWS * CH
    H = PAD + BAR + term_h + CAP + PAD
    im = Image.new("RGB", (W, H), "#161b22")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((PAD, PAD, W - PAD, PAD + BAR + term_h), 8, fill=TERM_BG)
    d.rounded_rectangle((PAD, PAD, W - PAD, PAD + BAR), 8, fill="#21262d")
    d.rectangle((PAD, PAD + BAR - 8, W - PAD, PAD + BAR), fill="#21262d")
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        cx = PAD + 18 + 20 * i
        d.ellipse((cx - 5.5, PAD + BAR / 2 - 5.5, cx + 5.5, PAD + BAR / 2 + 5.5), fill=c)
    title = "alwavelets — demo_waveform.vcd"
    d.text((W / 2 - d.textlength(title, font=small) / 2, PAD + BAR / 2 - 7), title, font=small, fill="#8b949e")
    x0, y0 = PAD, PAD + BAR
    for y, row in enumerate(frame.cells):
        for x, text, fg, bg, bold in runs(row):
            if bg != TERM_BG:
                d.rectangle((x0 + x * CW, y0 + y * CH, x0 + (x + len(text)) * CW, y0 + (y + 1) * CH), fill=bg)
            if text.strip():
                f = mono_b if bold else mono
                for k, ch in enumerate(text):
                    if ch != " ":
                        d.text((x0 + (x + k) * CW, y0 + y * CH + 1), ch,
                               font=_glyph_font(ch, f, fallback), fill=fg)
    cy = y0 + term_h + 26
    cx = x0 + 4
    d.text((cx, cy - 9), "%02d" % frame.step, font=small, fill="#6e7681")
    cx += 26
    for k in frame.keys:
        w = len(k) * 7.0 + 14
        d.rounded_rectangle((cx, cy - 14.5, cx + w, cy + 5.5), 5, fill="#30363d", outline="#484f58")
        d.text((cx + 7, cy - 8), k, font=chip, fill="#e6edf3")
        cx += w + 6
    d.text((cx + 6, cy - 11), frame.title, font=sans_b, fill="#e6edf3")
    for j, line in enumerate(wrap(frame.desc, 128)):
        d.text((x0 + 4, cy + 10 + j * 17), line, font=sans, fill="#9da7b3")
    return im


def load_fonts():
    from PIL import ImageFont
    base = "/usr/share/fonts/truetype/dejavu/"
    return (ImageFont.truetype(base + "DejaVuSansMono.ttf", FS),
            ImageFont.truetype(base + "DejaVuSansMono-Bold.ttf", FS),
            ImageFont.truetype(base + "DejaVuSans.ttf", 12),
            ImageFont.truetype(base + "DejaVuSans-Bold.ttf", 14),
            ImageFont.truetype(base + "DejaVuSans.ttf", 11),
            ImageFont.truetype(base + "DejaVuSansMono-Bold.ttf", 11),
            ImageFont.truetype(base + "DejaVuSans.ttf", FS))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=os.path.join(ROOT, "assets", "alwavelets-demo.svg"))
    ap.add_argument("--gif", help="also write an animated GIF here (needs Pillow)")
    ap.add_argument("--png-dir", help="also write one PNG per frame here (needs Pillow)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every captured screen")
    args = ap.parse_args()
    try:
        import pyte  # noqa: F401
    except ImportError:
        sys.exit("pip install pyte  (parses the captured colour codes)")
    if subprocess.run(["tmux", "-V"], capture_output=True).returncode != 0:
        sys.exit("tmux is required (the viewer is recorded inside a tmux pane)")
    frames = record(args.verbose)
    size, total = write_svg(frames, args.out)
    print("%s: %d frames, %.1fs loop, %d KB" % (args.out, len(frames), total, size // 1024))
    if args.gif or args.png_dir:
        fonts = load_fonts()
        images = [render_png(f, fonts) for f in frames]
        if args.png_dir:
            os.makedirs(args.png_dir, exist_ok=True)
            for i, im in enumerate(images):
                im.save(os.path.join(args.png_dir, "frame_%02d.png" % i))
        if args.gif:
            pal = [im.quantize(colors=128) for im in images]
            pal[0].save(args.gif, save_all=True, append_images=pal[1:], loop=0, optimize=True,
                        duration=[int(f.hold * 1000) for f in frames])
            print("%s: %d KB" % (args.gif, os.path.getsize(args.gif) // 1024))


if __name__ == "__main__":
    main()
