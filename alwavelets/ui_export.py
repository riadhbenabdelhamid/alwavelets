"""SVG and PDF export for Alwavelets (built without external libraries)."""
import curses
import os
import sys

from .graphics import render_inline
from .exporting import atomic_output, write_pdf
from .streaming import iter_segments, iter_window

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401


class ExportMixin:
    def export_image(self):
        """Render the current view as a pixel-accurate waveform image.

        If the terminal supports a graphics protocol (kitty / iTerm2 / sixel)
        the image is displayed inline (curses is suspended for the blit, then
        restored); otherwise a PNG is saved to a file. Either way it's a cheap
        one-shot render, so it never affects the live redraw performance."""
        visible = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]
        names = [self.get_signal(s).full_name for s in visible if self.get_signal(s)]
        if not names:
            self._show_message("No signals to render")
            return
        try:
            proto, payload = render_inline(
                self.vcd, names, self.time_start, self.time_end,
                width=max(400, self.wave_width * 8), row_h=22,
                cols=self.width - 2, rows=min(len(names) * 2, self.height - 2))
        except Exception as e:                       # never break the viewer
            self._show_message(f"Image render error: {e}")
            return

        if proto:
            # Suspend curses, blit the image in cooked-terminal mode, wait, resume.
            try:
                curses.def_prog_mode()
                curses.endwin()
                sys.stdout.write("\r\n")
                sys.stdout.flush()
                sys.stdout.buffer.write(payload.encode("latin-1", "replace"))
                sys.stdout.write(f"\r\n  [{proto}] waveform image — press Enter to return…")
                sys.stdout.flush()
                try:
                    sys.stdin.readline()
                except Exception:
                    pass
            finally:
                curses.reset_prog_mode()
                self.stdscr.refresh()
                self._needs_layout = True
                self._dirty = True
            self._show_message(f"Displayed inline ({proto})")
        else:
            path = "/tmp/alwavelets_waveform.png"
            try:
                with open(path, "wb") as f:
                    f.write(payload)
                self._show_message(f"No terminal graphics — saved PNG → {path}")
            except Exception as e:
                self._show_message(f"Save error: {e}")

    def export_svg(self):
        """Export current waveform view to SVG file."""
        fname = "/tmp/vcd_waveform.svg"
        try:
            self._generate_svg(fname)
            self._show_message(f"SVG exported → {fname}")
        except Exception as e:
            self._show_message(f"SVG error: {e}")

    def _generate_svg(self, filename):
        self.vcd.require_loaded(self.time_start, self.time_end)
        with atomic_output(filename) as stream:
            for line in self._iter_svg_lines():
                stream.write(line + '\n')

    def _iter_svg_lines(self):
        """Generate SVG with waveform rendering."""
        margin = 20
        name_w = 140
        wave_w = 600
        row_h = 24
        total_w = margin * 2 + name_w + wave_w
        visible = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]
        total_h = margin * 2 + row_h * (len(visible) + 2)  # +2 for header/ruler

        yield (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="{total_h}">')
        yield (f'<rect width="{total_w}" height="{total_h}" fill="#1a1a2e"/>')
        yield ('<style>')
        yield ('  text { font-family: monospace; font-size: 11px; fill: #e0e0e0; }')
        yield ('  .sig-name { fill: #a0a0c0; }')
        yield ('  .sig-val { fill: #75b8ff; font-weight: bold; }')
        yield ('  .high { stroke: #2fd85e; stroke-width: 2; fill: none; }')
        yield ('  .low { stroke: #1a8a3a; stroke-width: 2; fill: none; }')
        yield ('  .edge { stroke: #e85050; stroke-width: 2; fill: none; }')
        yield ('  .bus { stroke: #6faaff; stroke-width: 2; fill: none; }')
        yield ('  .undef { stroke: #e8c840; stroke-width: 2; fill: none; stroke-dasharray: 3,2; }')
        yield ('  .hiz { stroke: #5588cc; stroke-width: 1; fill: none; stroke-dasharray: 5,3; }')
        yield ('  .cursor { stroke: #4080ff; stroke-width: 1; stroke-dasharray: 4,2; }')
        yield ('  .ruler { fill: #606080; font-size: 9px; }')
        yield ('  .header { fill: #c0c0e0; font-size: 12px; font-weight: bold; }')
        yield ('  .grid { stroke: #252540; stroke-width: 0.5; }')
        yield ('</style>')

        # Header
        title = f"Alwavelets — {os.path.basename(self.vcd._filename)} [{self.vcd.time_to_str(self.time_start)}..{self.vcd.time_to_str(self.time_end)}]"
        yield (f'<text x="{margin}" y="{margin + 12}" class="header">{xml_escape(title)}</text>')

        # Ruler
        ruler_y = margin + row_h
        step = max(wave_w // 8, 1)
        for c in range(0, wave_w, step):
            t = self.col_to_time(int(c / wave_w * self.wave_width))
            x = margin + name_w + c
            label = self.format_ruler_time(t)
            yield (f'<text x="{x}" y="{ruler_y + 10}" class="ruler">{xml_escape(label)}</text>')
            yield (f'<line x1="{x}" y1="{ruler_y + 14}" x2="{x}" y2="{total_h - margin}" class="grid"/>')

        # Cursor
        if self.cursor_on:
            cx = margin + name_w + int((self.cursor_time - self.time_start) / max(self.time_end - self.time_start, 1) * wave_w)
            yield (f'<line x1="{cx}" y1="{ruler_y + 14}" x2="{cx}" y2="{total_h - margin}" class="cursor"/>')

        # Signals
        for idx, sig_id in enumerate(visible):
            sig = self.get_signal(sig_id)
            if sig is None:
                continue
            y_base = margin + row_h * (idx + 2)
            y_mid = y_base + row_h // 2
            y_top = y_base + 4
            y_bot = y_base + row_h - 4

            # Name
            val = format_typed_value(sig, sig.value_at(self.cursor_time), self.get_signal_radix(sig_id))
            yield (f'<text x="{margin}" y="{y_mid + 4}" class="sig-name">{xml_escape(sig.name)}</text>')
            yield (f'<text x="{margin + name_w - 40}" y="{y_mid + 4}" class="sig-val">{xml_escape(val)}</text>')

            # Waveform
            if sig.size == 1 and sig.signal_type not in ('real', 'realtime'):
                # Draw single bit as path
                path_parts = []
                for c in range(wave_w):
                    t = self.time_start + c / wave_w * (self.time_end - self.time_start)
                    v = sig.value_at(int(t))
                    x = margin + name_w + c
                    if v == '1':
                        y = y_top
                        cls = 'high'
                    elif v == '0':
                        y = y_bot
                        cls = 'low'
                    elif v == 'x':
                        y = y_mid
                        cls = 'undef'
                    else:
                        y = y_mid
                        cls = 'hiz'
                    if c == 0:
                        path_parts.append(f'M{x},{y}')
                    else:
                        path_parts.append(f'L{x},{y}')
                yield (f'<path d="{" ".join(path_parts)}" class="high"/>')
            else:
                # Bus: simple rectangles with transitions
                span = max(self.time_end - self.time_start, 1)
                for ti, (begin, end, v) in enumerate(iter_segments(sig, self.time_start, self.time_end)):
                    x1 = margin + name_w + int((begin - self.time_start) / span * wave_w)
                    x2 = margin + name_w + int((end - self.time_start) / span * wave_w)
                    if any(c in v for c in 'xXzZ'):
                        cls = 'undef'
                    else:
                        cls = 'bus'
                    yield (f'<line x1="{x1}" y1="{y_top}" x2="{x2}" y2="{y_top}" class="{cls}"/>')
                    yield (f'<line x1="{x1}" y1="{y_bot}" x2="{x2}" y2="{y_bot}" class="{cls}"/>')
                    if ti > 0:
                        yield (f'<line x1="{x1}" y1="{y_top}" x2="{x1}" y2="{y_bot}" class="edge"/>')
                    # Label
                    lbl = format_typed_value(sig, v, self.get_signal_radix(sig_id))
                    if x2 - x1 > 20:
                        yield (f'<text x="{x1 + 3}" y="{y_mid + 4}" class="sig-val" font-size="9">{xml_escape(lbl)}</text>')

        yield ('</svg>')

    # ── PDF Export ──

    def export_pdf(self):
        """Export current waveform view to PDF using built-in drawing."""
        fname = "/tmp/vcd_waveform.pdf"
        try:
            self._generate_pdf(fname)
            self._show_message(f"PDF exported → {fname}")
        except Exception as e:
            self._show_message(f"PDF error: {e}")

    def _generate_pdf(self, filename):
        self.vcd.require_loaded(self.time_start, self.time_end)
        write_pdf(filename, self._iter_pdf_content())

    def _iter_pdf_content(self):
        """Generate a PDF file with waveform rendering."""
        # Minimal PDF generation without external libraries
        page_w, page_h = 842, 595  # A4 landscape in points
        margin = 40
        name_w = 120
        wave_w = page_w - 2 * margin - name_w
        row_h = 20
        font_size = 8

        visible = self.display_order[self.sig_offset:self.sig_offset + self.visible_signals]

        # Build PDF content
        y = page_h - margin - 30  # start below title

        # Title
        title = (f"{os.path.basename(self.vcd._filename)} — "
                 f"[{self.format_ruler_time(self.time_start)}.."
                 f"{self.format_ruler_time(self.time_end)}]")
        yield (f"BT /F1 12 Tf {margin} {y + 10} Td ({pdf_escape(title)}) Tj ET")
        y -= 20

        for sig_id in visible:
            if y < margin + 20:
                break
            sig = self.get_signal(sig_id)
            if not sig:
                continue
            height = self.sig_height.get(sig_id, 1) * row_h

            # Signal name
            name = sig.name[:18]
            val = format_typed_value(sig, sig.value_at(self.cursor_time), self.get_signal_radix(sig_id))
            yield (f"BT /F1 {font_size} Tf {margin} {y - font_size} Td ({pdf_escape(name + ' = ' + str(val))}) Tj ET")

            # Waveform path
            x_start = margin + name_w
            span = self.time_end - self.time_start
            if span <= 0:
                y -= height + 4
                continue

            if sig.size == 1 and not sig.analog_mode and sig_id not in self.analog_signals:
                # Digital waveform
                yield (f"0.2 0.8 0.2 RG 0.5 w")  # green stroke
                cur_val = sig.value_at(self.time_start)
                yield f"{x_start} {y - (0 if cur_val == '0' else height - 4)} m"
                for t, value in iter_window(sig, self.time_start, self.time_end, baseline=False):
                    px = int((t - self.time_start) / span * wave_w)
                    h_pos = y - (0 if value == '0' else height - 4)
                    yield f"{x_start + px} {y - (0 if cur_val == '0' else height - 4)} l"
                    yield f"{x_start + px} {h_pos} l"
                    cur_val = value
                yield f"{x_start + wave_w} {y - (0 if cur_val == '0' else height - 4)} l S"
            else:
                # Bus/analog — draw box
                yield (f"0.3 0.5 0.9 RG 0.5 w")
                yield (f"{x_start} {y - height + 4} {wave_w} {height - 8} re S")

            y -= height + 4


    # ── Live Reload ──
