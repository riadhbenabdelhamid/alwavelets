"""Drawing methods for Alwavelets: header, ruler, signals, overlays, panels.
Mixed into Alwavelets; all state is on self."""
import curses
import math
import os
import time

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401

# Timescale unit → seconds, for turning tick deltas into real frequencies.
_UNIT_S = {'s': 1.0, 'ms': 1e-3, 'us': 1e-6, 'ns': 1e-9, 'ps': 1e-12, 'fs': 1e-15}


def _fmt_freq(hz):
    """Compact engineering format for a frequency in Hz."""
    for scale, unit in ((1e9, 'GHz'), (1e6, 'MHz'), (1e3, 'kHz')):
        if hz >= scale:
            return f"{hz / scale:.4g}{unit}"
    return f"{hz:.4g}Hz"



class DrawingMixin:
    def draw_manual_overlay(self):
        """Draw the extensive manual with navigation."""
        h, w = self.height, self.width
        self.stdscr.erase()

        title, lines = self.manual.current()
        total = len(self.manual.pages)
        page_n = self.manual.page_idx + 1

        # Header bar
        hdr = f" ALWAVELETS MANUAL │ {title} │ Page {page_n}/{total}"
        nav = " [n/Space] next  [p] prev  [g] goto  [↑↓] scroll  [q/Esc] close "
        try:
            self.stdscr.addstr(0, 0, hdr[:w-1].ljust(w-1),
                              curses.color_pair(self.C_HEADER) | curses.A_BOLD)
            self.stdscr.addstr(1, 0, nav[:w-1].ljust(w-1),
                              curses.color_pair(self.C_MENUKEY) | curses.A_DIM)
        except curses.error:
            pass

        # Body
        body_rows = h - 3
        visible = lines[self.manual.scroll:self.manual.scroll + body_rows]

        for i, line in enumerate(visible):
            row = 2 + i
            if row >= h - 1:
                break
            # Colored rendering for section headers and examples
            attr = curses.A_NORMAL
            stripped = line.strip()
            if stripped.startswith("─── "):  # section header
                attr = curses.color_pair(self.C_HEADER) | curses.A_BOLD
            elif stripped.startswith("•") or stripped.startswith("−"):
                attr = curses.color_pair(self.C_HIGH)
            elif "  " in line and ("→" in line or ":" in line[:20]):
                # Likely a command description — highlight key at start
                attr = curses.A_NORMAL
            try:
                self.stdscr.addstr(row, 0, line[:w-1], attr)
            except curses.error:
                pass

        # Footer: scroll indicator
        more_above = self.manual.scroll > 0
        more_below = self.manual.scroll + body_rows < len(lines)
        footer = ""
        if more_above and more_below:
            footer = " ▲ more above    more below ▼ "
        elif more_above:
            footer = " ▲ more above "
        elif more_below:
            footer = "    more below ▼ "

        try:
            if footer:
                self.stdscr.addstr(h-1, max(0, (w - len(footer)) // 2), footer,
                                  curses.color_pair(self.C_MENUKEY) | curses.A_DIM)
            else:
                pagebar = f" Page {page_n}/{total} "
                self.stdscr.addstr(h-1, max(0, (w - len(pagebar)) // 2), pagebar,
                                  curses.color_pair(self.C_HEADER))
        except curses.error:
            pass

    def manual_handle_key(self, key):
        """Handle key in manual mode. Returns True to stay, False to exit."""
        if key in (ord('q'), 27):
            return False

        if key in (ord('n'), ord(' '), curses.KEY_RIGHT, ord('l')):
            # Next page (or scroll down if not at bottom of current page)
            title, lines = self.manual.current()
            if self.manual.scroll + (self.height - 3) < len(lines):
                self.manual.scroll_down(self.height - 4)
            else:
                self.manual.next_page()
        elif key in (ord('p'), curses.KEY_LEFT, ord('h')):
            if self.manual.scroll > 0:
                self.manual.scroll_up(self.height - 4)
            else:
                self.manual.prev_page()
        elif key in (curses.KEY_DOWN, ord('j')):
            self.manual.scroll_down(1)
        elif key in (curses.KEY_UP, ord('k')):
            self.manual.scroll_up(1)
        elif key == curses.KEY_NPAGE:
            self.manual.scroll_down(self.height - 4)
        elif key == curses.KEY_PPAGE:
            self.manual.scroll_up(self.height - 4)
        elif key == curses.KEY_HOME:
            self.manual.scroll = 0
        elif key == curses.KEY_END:
            title, lines = self.manual.current()
            self.manual.scroll = max(0, len(lines) - (self.height - 4))
        elif key == ord('g'):
            # Goto page
            page_str = self.prompt_input(f"Goto page (1-{len(self.manual.pages)}): ")
            try:
                n = int(page_str) - 1
                self.manual.goto(n)
            except ValueError:
                pass
        elif key == ord('?'):
            # Toggle to quick help
            return False
        elif ord('0') <= key <= ord('9'):
            # Quick jump to page 0-9
            idx = key - ord('0')
            if idx == 0:
                self.manual.goto(0)  # TOC
            elif idx <= len(self.manual.pages) - 1:
                self.manual.goto(idx)

        return True


    def draw_register_panel(self):
        """Draw per-thread register file snapshot panel."""
        if not self.instr_trace.built:
            return

        h, w = self.height, self.width
        panel_w = min(40, w // 3)
        start_x = w - panel_w

        from .instruction_store import InstructionResults
        trace, cursor = self.instr_trace, self.cursor_time
        if isinstance(trace.entries, InstructionResults):
            from .background import BackgroundQueries
            if not hasattr(self, '_queries'):
                self._queries = BackgroundQueries()
            state = self._queries.request('registers', (id(trace.entries), cursor), 'panel',
                                          lambda cancel: trace.register_panel_state(cursor, cancel=cancel),
                                          (0, [0] + [None] * 31, set(), None), cancellable=True)
        else:
            state = trace.register_panel_state(cursor)
        cur_hart, regs, recent, current = state

        # Header
        hdr = f" Hart {cur_hart} Regs @t={self.cursor_time}"
        try:
            self.stdscr.addstr(2, start_x, hdr[:panel_w].ljust(panel_w),
                              curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        # Register grid: 2 columns of 16 regs each
        for i in range(32):
            row = 3 + (i % 16)
            col_offset = 0 if i < 16 else panel_w // 2
            x = start_x + col_offset

            if row >= h - 1:
                break

            name = RV32Decoder.REG_NAMES[i]
            val = regs[i]
            if val is None:
                val_str = "--------"
            elif val == 0 and i == 0:
                val_str = "00000000"
            else:
                val_str = f"{val:08x}"

            # Highlight recently-written registers
            is_recent = i in recent

            label = f"{name:>4}={val_str}"
            attr = curses.color_pair(self.C_HIGH) | curses.A_BOLD if is_recent else curses.A_NORMAL
            if i == 0:
                attr = curses.A_DIM  # x0 is always 0

            try:
                self.stdscr.addstr(row, x, label[:panel_w//2], attr)
            except curses.error:
                pass

        # Separator line
        for row in range(2, min(20, h - 1)):
            try:
                self.stdscr.addch(row, start_x - 1, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

        # Info footer
        info_row = 19 if 19 < h - 1 else h - 2
        # Find current instruction
        cur_instr = "---"
        cur_pc = "---"
        if current is not None:
            cur_instr = current['decoded']['asm'][:20]
            cur_pc = f"0x{current['pc']:08x}"
        try:
            self.stdscr.addstr(info_row, start_x,
                              f" PC={cur_pc}"[:panel_w].ljust(panel_w),
                              curses.color_pair(self.C_VAL))
            if info_row + 1 < h - 1:
                self.stdscr.addstr(info_row + 1, start_x,
                                  f" {cur_instr}"[:panel_w].ljust(panel_w),
                                  curses.color_pair(self.C_MENUKEY))
        except curses.error:
            pass


    def _draw_separator(self, row):
        """Thin horizontal rule across the width (divides header regions)."""
        if not (0 <= row < self.height):
            return
        try:
            self.stdscr.addstr(row, 0, '─' * (self.width - 1),
                               curses.color_pair(self.C_MENUSEP) | curses.A_DIM)
        except curses.error:
            pass

    def draw_header(self):
        """Row 0: title bar · row 1: separator · row 2: menu bar · row 3: separator."""
        w = self.width

        # ── Row 0: Title bar ──
        total = sum(s.num_changes() for s in self.vcd.signals.values())
        parts = [
            " ◆ Alwavelets",
            os.path.basename(self.vcd._filename),
            f"TS:{self.vcd.timescale}",
            f"Sig:{len(self.display_order)}",
            f"Chg:{total:,}",
            f"T:{self.vcd.max_time:,}",
        ]
        title = " │ ".join(parts)
        title = title[:w - 1].ljust(w - 1)
        try:
            self.stdscr.addstr(0, 0, title, curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        # ── Row 1: separator under the title bar ──
        self._draw_separator(1)

        # ── Row 2: Menu bar with command guide (F2 hides it for more rows) ──
        if not self.show_menu_bar:
            return
        # Each entry: (key_label, description)
        all_menu_items = [
            ("?", "Quick help"),
            ("F1", "Full manual"),
            ("F2", "Hide this bar"),
            # ── Navigation ──
            ("←→", "Scroll time"),
            ("+/-", "Zoom in/out"),
            ("Z", "Fit all"),
            ("W", "Fit A↔B"),
            ("↑↓", "Navigate signals"),
            ("Home", "Start of file"),
            ("End", "End of file"),
            # ── VIM-style ──
            ("gg", "First in scope level"),
            ("G", "Last in scope level"),
            ("V", "Visual line select"),
            ("BS", "Remove selected"),
            ("S↑↓", "Jump to prev/next module"),
            ("S←→", "Collapse/expand scope"),
            # ── Cursors & Markers ──
            ("c", "Toggle cursor"),
            ("12", "Cursor A/B"),
            ("S", "Snap to edge"),
            ("m", "Place marker"),
            ("M", "Clear markers"),
            ("<>", "Jump markers"),
            ("gt", "Go to time"),
            ("e", "Edge-to-edge"),
            # ── Edge & Search ──
            ("][", "Next/prev edge"),
            ("f", "Find value"),
            ("P", "Pattern trigger"),
            ("/", "Fuzzy find signal"),
            ("n", "Next result"),
            # ── Signal Management ──
            ("s", "Panel mode (split)"),
            ("B", "Scope tree (fullscreen)"),
            ("Tab", "Switch panel focus"),
            ("x", "Expand bus bits"),
            ("F", "Float decode (IEEE754)"),
            ("Tab", "Fold/unfold group header"),
            ("{", "Collapse all modules"),
            ("}", "Expand all modules"),
            ("J/K", "Reorder signals"),
            ("d/u", "Delete/undo signal"),
            ("Spc", "Highlight signal"),
            # ── Display ──
            ("r", "Global radix"),
            ("R", "Signal radix"),
            ("v", "Signal color"),
            ("H", "Signal height"),
            ("L", "Waveform label size"),
            ("w", "Wave style"),
            ("()", "Name column ±"),
            ("|", "Signal spacing"),
            ("i", "Indent hierarchy mode"),
            ("Ent", "Full/short names"),
            ("a", "Analog toggle"),
            ("t", "Values toggle"),
            ("T", "Time units"),
            ("Q", "Grid (off/vert/both)"),
            ("\\", "Cycle theme"),
            ("C", "Color guide"),
            ("0/9", "Menu page ◀▶"),
            # ── Protocol & Export ──
            ("D", "Load decode file"),
            ("I", "Protocol decode"),
            (":", "Tcl command"),
            (";", "CLI (diff, theme, trace, query ...)"),
            ("`", "Trace query (ISA debug)"),
            ("p", "Register panel"),
            ("A", "Add annotation"),
            (";formal", "CEX analyzer"),
            (";power", "Power heatmap"),
            (";axi", "Decode AXI"),
            (";bm", "Bookmark"),
            ("t", "Tag signal (in B tree)"),
            ("T", "Load tag file (in B tree)"),
            ("E", "Export menu"),
            ("X", "SVG export"),
            ("F5", "Live reload (watch file)"),
            ("q", "Quit"),
        ]

        # Paginate: show items that fit in menu bar width
        items_per_page = max(1, (w - 10) // 12)  # ~12 chars per item
        total_pages = max(1, (len(all_menu_items) + items_per_page - 1) // items_per_page)
        self.menu_page = self.menu_page % total_pages
        page_start = self.menu_page * items_per_page
        page_end = min(page_start + items_per_page, len(all_menu_items))
        menu_items = all_menu_items[page_start:page_end]

        col = 0
        row = self.menu_row
        try:
            # Fill background
            self.stdscr.addstr(row, 0, ' ' * (w - 1), curses.color_pair(self.C_MENU))
        except curses.error:
            pass

        # Page indicator at start
        if total_pages > 1:
            page_ind = f"◀{self.menu_page+1}/{total_pages}▶"
            try:
                self.stdscr.addstr(row, col, page_ind, curses.color_pair(self.C_HEADER) | curses.A_DIM)
                col += len(page_ind)
            except curses.error:
                pass

        for key, desc in menu_items:
            entry = f" {key}"
            entry_desc = f":{desc}"
            if col + len(entry) + len(entry_desc) >= w - 1:
                break
            try:
                self.stdscr.addstr(row, col, entry, curses.color_pair(self.C_MENUKEY) | curses.A_BOLD)
                col += len(entry)
                self.stdscr.addstr(row, col, entry_desc, curses.color_pair(self.C_MENU))
                col += len(entry_desc)
                # Separator dot
                if col + 2 < w:
                    self.stdscr.addstr(row, col, " ·", curses.color_pair(self.C_MENUSEP))
                    col += 2
            except curses.error:
                pass

        # ── Row 3: separator under the menu bar, dividing it from the ruler ──
        self._draw_separator(self.ruler_row - 1)

    def _nice_tick_interval(self, span, n_ticks=8):
        """Compute a human-friendly tick interval for the given span."""
        if span <= 0:
            return max(1, span)
        raw = span / max(n_ticks, 1)
        if raw < 1:
            return 1
        mag = 10 ** math.floor(math.log10(raw))
        residual = raw / mag
        if residual <= 1.5:
            nice = 1
        elif residual <= 3.5:
            nice = 2
        elif residual <= 7.5:
            nice = 5
        else:
            nice = 10
        return max(1, int(nice * mag))

    def draw_ruler(self):
        row = self.ruler_row
        ruler = [' '] * self.wave_width
        span = self.time_end - self.time_start
        if span <= 0:
            span = 1

        # Compute aligned ticks at round time intervals
        tick_interval = self._nice_tick_interval(span)
        first_tick = ((self.time_start + tick_interval - 1) // tick_interval) * tick_interval
        if first_tick > self.time_start + span:
            first_tick = self.time_start

        t = first_tick
        while t <= self.time_end:
            c = self.time_to_col(t)
            if 0 <= c < self.wave_width:
                ruler[c] = '┬'
                label = self.format_ruler_time(t)
                start = c + 1
                if start + len(label) < self.wave_width:
                    for j, ch in enumerate(label):
                        if start + j < self.wave_width:
                            ruler[start + j] = ch
            t += tick_interval

        name_header = "Signal".center(self.name_width)
        try:
            self.stdscr.addstr(row, 0, name_header, curses.color_pair(self.C_HEADER))
            self.stdscr.addstr(row, self.name_width, '│', curses.color_pair(self.C_HEADER))
            self.stdscr.addstr(row, self.name_width + 1, ''.join(ruler)[:self.wave_width], curses.A_DIM)
        except curses.error:
            pass

        # Draw cursor A on ruler
        if self.cursor_on:
            cc = self.time_to_col(self.cursor_a)
            ac = self.name_width + 1 + cc
            if 0 <= cc < self.wave_width and ac < self.width:
                try:
                    self.stdscr.addch(row, ac, 'A', curses.color_pair(self.C_CURSOR) | curses.A_BOLD)
                except curses.error:
                    pass

        # Draw cursor B on ruler
        if self.cursor_b_on:
            cc = self.time_to_col(self.cursor_b)
            ac = self.name_width + 1 + cc
            if 0 <= cc < self.wave_width and ac < self.width:
                try:
                    self.stdscr.addch(row, ac, 'B', curses.color_pair(self.C_CURSORB) | curses.A_BOLD)
                except curses.error:
                    pass

    def _render_row(self, kind, sig, sig_id, extra=None):
        """Memoized wrapper around the pure per-row render functions.

        Every visible row shares one time window, so the cache only lives as
        long as the current (window, width, wave style): zoom/pan/resize
        clear it, while cursor movement, focus changes and vertical scrolling
        redraw straight from cache instead of re-rendering each row.
        sig.num_changes() in the key invalidates a row when a background
        load / live reload / follow appends new data to that signal."""
        wkey = (self.time_start, self.time_end, self.wave_width,
                self.line_thickness)
        if wkey != self._row_cache_window:
            self._row_cache_window = wkey
            self._row_cache.clear()
            self._row_extras = {}
        key = (kind, sig_id, sig.num_changes(), extra)
        out = self._row_cache.get(key)
        if out is None:
            t0, t1, ww = self.time_start, self.time_end, self.wave_width
            def compute():
                k = kind[1:] if kind.startswith('d') else kind   # diff-row kinds
                if k == 'bit':
                    return draw_single_bit_culled(sig, t0, t1, ww)
                elif k == 'sub':
                    return draw_single_bit_subcell(sig, t0, t1, ww)
                elif k == 'bus':
                    return draw_bus_culled(sig, t0, t1, ww, extra)
                elif k == 'analog':
                    return draw_analog_culled(sig, t0, t1, ww)
                elif k == 'abraille':                     # hi-res analog (w style)
                    return draw_analog_braille(sig, t0, t1, ww, extra)
                else:                                     # 'amulti'
                    return draw_analog_multi(sig, t0, t1, ww, extra)
            if getattr(self.vcd, '_store', None) is not None:
                from .background import BackgroundQueries
                if not hasattr(self, '_queries'):
                    self._queries = BackgroundQueries()
                k = kind.lstrip('d')
                blank = ([' '] * ww, [0] * ww)
                placeholder = (*blank, {}) if k == 'bus' else [blank] * extra if k in ('abraille', 'amulti') else blank
                def compute_disk():
                    result = compute()
                    labels = {col: sig.value_at(int(t0 + col / max(ww, 1) * (t1 - t0)))
                              for col in result[2]} if k == 'bus' else {}
                    limits = sig.numeric_range_in_window(t0, t1) if k in ('analog', 'abraille', 'amulti') else None
                    return result, labels, limits
                result, labels, limits = self._queries.request(
                    'rows', (id(self.vcd), wkey), key, compute_disk, (placeholder, {}, None))
                if len(self._row_extras) > 512:
                    self._row_extras.clear()
                self._row_extras[(kind, sig_id)] = labels, limits
                return result
            out = compute()
            # Bound it: within one window the live keys are the visible rows,
            # but a background load bumps num_changes on every redraw, so a
            # long load would otherwise accumulate a row per frame per signal.
            if len(self._row_cache) > 512:
                self._row_cache.clear()
            self._row_cache[key] = out
        return out

    def draw_signals(self):
        start_row = self.wave_top
        # Compute row stride: all non-zero spacing adds at least 1 separator row
        if self.signal_spacing > 0 and self.signal_spacing < 1:
            row_stride = 2  # signal + thin separator
        elif self.signal_spacing >= 1:
            row_stride = 1 + int(self.signal_spacing)
        else:
            row_stride = 1
        frac_spacing = self.signal_spacing if 0 < self.signal_spacing < 1 else 0
        self._rebuild_view_rows()      # refresh the group overlay (cheap, O(rows))
        visible = self.view_rows[self.sig_offset:self.sig_offset + self.visible_signals]

        # Ensure data for current view window is loaded (lazy mode)
        if self.vcd._lazy_mode:
            self.vcd.ensure_loaded(self.time_start, self.time_end)

        prev_scope = None
        extra_rows = 0  # track separator rows consumed

        for idx, sig_id in enumerate(visible):
            row = start_row + idx * row_stride + extra_rows
            if row >= self.height - 1:
                break

            if self.is_group_header(sig_id):       # a collapsible group header row
                self._draw_group_header(row, sig_id[1], idx == self.focus_idx)
                continue

            sig = self.get_signal(sig_id)
            if not sig:
                continue

            # Module separator between different scopes
            # Compare scopes at 2nd level (e.g. top.alu vs top.memory)
            scope_parts = sig.scope.split('.') if sig.scope else []
            cur_scope = '.'.join(scope_parts[:2]) if len(scope_parts) >= 2 else (sig.scope or '')
            if self.show_module_sep and prev_scope is not None and cur_scope != prev_scope and cur_scope:
                # Show the last part of the scope as separator label
                sep_label = scope_parts[-1] if scope_parts else cur_scope
                self._draw_scope_separator(row, sep_label)
                row += 1
                extra_rows += 1
                if row >= self.height - 1:
                    break
            prev_scope = cur_scope
            is_selected = sig_id in self.selected_signals
            is_focused = (idx == self.focus_idx)
            is_alt_row = (idx % 2 == 1)
            is_analog = sig_id in self.analog_signals or sig.analog_mode
            is_bus = sig.size > 1 and not is_analog
            # Analog signals may span several rows (H key); others are 1 row.
            sig_rows = self.sig_height.get(sig_id, 1) if is_analog else 1
            sig_rows = max(1, min(sig_rows, self.height - 1 - row))
            custom_color = self.sig_color.get(sig_id, 0)
            has_diff = sig_id in self.diff_ranges and self.diff_ranges[sig_id]

            # ── Alternating row background (across the signal's rows) ──
            if is_alt_row:
                for rr in range(row, row + sig_rows):
                    try:
                        self.stdscr.addstr(rr, 0, ' ' * (self.width - 1), curses.color_pair(self.C_ROWALT))
                    except curses.error:
                        pass

            # ── Signal name with colored parts ──
            if sig_id in self.analog_signals or sig.analog_mode:
                type_icon = '~'
            elif sig.size > 1:
                type_icon = '═'
            else:
                type_icon = '─'
            indent_str = ""
            if self.indent_mode:
                depth = len(sig.scope.split('.')) if sig.scope else 0
                indent_str = "  " * min(depth, 5)
            prefix = f"{indent_str}▸{type_icon}" if is_focused else f"{indent_str} {type_icon}"
            scope_part = ""
            if self.show_full_names:
                name_part = sig.full_name if hasattr(sig, 'full_name') and sig.full_name else sig.name
                scope_part = ""
            else:
                name_part = sig.name
                if sig.scope and not self.indent_mode:
                    short = sig.scope.split('.')[-1] if '.' in sig.scope else sig.scope
                    if sig.scope in self.collapsed_scopes:
                        scope_part = f"▶{short}."
                    else:
                        scope_part = f"{short}."

            # Compute value string
            val_str = ""
            if self.show_values and self.cursor_on:
                val_str = self.format_signal_value(sig, sig_id)

            # Calculate available widths
            avail = self.name_width - 1
            prefix_w = 2 + len(indent_str)
            scope_w = len(scope_part)
            name_w = len(name_part)
            val_w = len(val_str) + 1 if val_str else 0

            max_name_area = avail - prefix_w - val_w
            if scope_w + name_w > max_name_area:
                if name_w > max_name_area:
                    scope_part = ""
                    name_part = name_part[:max(max_name_area, 3)]
                else:
                    scope_part = scope_part[:max(max_name_area - name_w, 0)]

            content_len = prefix_w + len(scope_part) + len(name_part) + val_w
            pad = max(0, avail - content_len)

            # ── Name color: bus vs wire distinction ──
            col = 0
            if is_selected:
                name_attr = curses.color_pair(self.C_SELECT)
            elif is_focused:
                name_attr = curses.color_pair(self.C_FOCUS) | curses.A_BOLD
            elif custom_color:
                name_attr = curses.color_pair(custom_color) | curses.A_BOLD
            elif has_diff:
                name_attr = curses.color_pair(self.C_DIFF_ED) | curses.A_BOLD
            elif is_bus:
                name_attr = curses.color_pair(self.C_BUSNAME)
            else:
                name_attr = curses.A_NORMAL

            try:
                # Render prefix: indent + focus marker + type icon
                if len(prefix) >= 2:
                    # Indent portion (spaces)
                    indent_len = len(indent_str)
                    if indent_len > 0:
                        self.stdscr.addstr(row, col, indent_str, curses.A_DIM)
                        col += indent_len
                    # Focus marker (▸ or space)
                    focus_ch = prefix[indent_len] if indent_len < len(prefix) else ' '
                    self.stdscr.addstr(row, col, focus_ch, name_attr)
                    col += 1
                    # Type icon (═ ─ ~)
                    icon_ch = prefix[indent_len + 1] if indent_len + 1 < len(prefix) else ' '
                    icon_attr = curses.color_pair(self.C_BUSNAME if is_bus else self.C_SCOPE) | curses.A_DIM
                    self.stdscr.addstr(row, col, icon_ch, icon_attr)
                    col += 1
                if scope_part:
                    self.stdscr.addstr(row, col, scope_part, curses.color_pair(self.C_SCOPE))
                    col += len(scope_part)
                self.stdscr.addstr(row, col, name_part, name_attr)
                col += len(name_part)
                if pad > 0:
                    self.stdscr.addstr(row, col, ' ' * pad, curses.A_NORMAL)
                    col += pad
                if val_str:
                    self.stdscr.addstr(row, col, ' ', curses.A_NORMAL)
                    col += 1
                    self.stdscr.addstr(row, col, val_str, curses.color_pair(self.C_VAL) | curses.A_BOLD)
                    col += len(val_str)
                self.stdscr.addstr(row, self.name_width, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

            # ── Waveform ──
            sig_rx = self.get_signal_radix(sig_id)
            if is_analog:
                if sig_rows > 1:
                    # Hi-res wave style (w) renders analog as a braille curve
                    # (2x4 sub-pixels per cell) instead of block columns.
                    akind = 'abraille' if self.line_thickness == 2 else 'amulti'
                    arows = self._render_row(akind, sig, sig_id, sig_rows)
                    for sr, (a_chars, a_attrs) in enumerate(arows):
                        self._batched_addstr(row + sr, self.name_width + 1, a_chars, a_attrs)
                    extra_rows += sig_rows - 1   # reserve the extra rows
                    chars = arows[0][0]
                    wave_rows = [a[0] for a in arows]   # per-row glyphs for the band
                else:
                    chars, attrs = self._render_row('analog', sig, sig_id)
                    self._batched_addstr(row, self.name_width + 1, chars, attrs)
            elif sig.size == 1 and getattr(sig, 'signal_type', 'wire') != "real":
                chars, attrs = self._render_row(
                    'sub' if self.line_thickness == 2 else 'bit', sig, sig_id)
                self._batched_addstr(row, self.name_width + 1, chars, attrs)
            else:
                chars, attrs, labels = self._render_row('bus', sig, sig_id, sig_rx)
                self._batched_addstr(row, self.name_width + 1, chars, attrs)
                if self.value_label_size > 0:
                    max_lbl_len = 4 if self.value_label_size == 1 else 999
                    for bcol, lbl in labels.items():
                        t_at_col = self.col_to_time(bcol)
                        if getattr(self.vcd, '_store', None) is not None:
                            raw_val = self._row_extras.get(('bus', sig_id), ({}, None))[0].get(bcol, 'x')
                        else:
                            raw_val = sig.value_at(t_at_col)
                        translated = self.translations.translate(sig.name, raw_val, sig.size)
                        display_lbl = translated if translated else lbl
                        display_lbl = display_lbl[:max_lbl_len]
                        ac = self.name_width + 1 + bcol
                        if ac < self.width - 1 and bcol < self.wave_width:
                            dl = display_lbl[:min(len(display_lbl), self.width - ac - 1)]
                            try:
                                self.stdscr.addstr(row, ac, dl, curses.color_pair(self.C_BUSLBL))
                            except curses.error:
                                pass

            # ── Grid lines ──
            if self.show_grid >= 1:
                grid_attr = curses.color_pair(self.C_SCOPE) | curses.A_DIM
                # Vertical grid
                grid_step = max(self.wave_width // 8, 1)
                for gc in range(0, self.wave_width, grid_step):
                    gac = self.name_width + 1 + gc
                    if gac < self.width:
                        try:
                            self.stdscr.addch(row, gac, '┊', grid_attr)
                        except curses.error:
                            pass
            if self.show_grid >= 2:
                # Horizontal separator after each signal
                sep_row = row + row_stride - 1 if row_stride > 1 else row
                if sep_row < self.height - 1 and sep_row != row:
                    try:
                        sep_line = '┈' * (self.width - 1)
                        self.stdscr.addstr(sep_row, 0, sep_line, curses.color_pair(self.C_SCOPE) | curses.A_DIM)
                    except curses.error:
                        pass

            # ── Split diff view: draw second waveform below with delimiters ──
            if has_diff and sig_id in self.diff_sig_map:
                diff_row = row + 1
                if diff_row < self.height - 1:
                    extra_rows += 1
                    sig_b = self.diff_sig_map[sig_id]
                    sig_rx = self.get_signal_radix(sig_id)

                    # Draw diff label
                    diff_label = "  ▿ diff"[:self.name_width-1].ljust(self.name_width-1)
                    try:
                        self.stdscr.addstr(diff_row, 0, diff_label, curses.color_pair(self.C_DIFF_ED) | curses.A_DIM)
                        self.stdscr.addstr(diff_row, self.name_width, '│', curses.color_pair(self.C_MENUSEP))
                    except curses.error:
                        pass

                    # Draw diff waveform ('d'-prefixed kinds keep the diff
                    # file's rows from colliding with the main trace's)
                    if sig_b.size == 1 and getattr(sig_b, 'signal_type', 'wire') != "real":
                        d_chars, d_attrs = self._render_row(
                            'dsub' if self.line_thickness == 2 else 'dbit',
                            sig_b, sig_id)
                    elif getattr(sig_b, 'analog_mode', False):
                        d_chars, d_attrs = self._render_row('danalog', sig_b, sig_id)
                    else:
                        d_chars, d_attrs, _ = self._render_row('dbus', sig_b, sig_id, sig_rx)
                    self._batched_addstr(diff_row, self.name_width + 1, d_chars, d_attrs)

                    # Draw vertical delimiter columns at diff boundaries
                    span = self.time_end - self.time_start
                    if span > 0:
                        for d_start, d_end in self.diff_ranges[sig_id]:
                            if d_end < self.time_start or d_start > self.time_end:
                                continue
                            c0 = max(0, int((max(d_start, self.time_start) - self.time_start) / span * self.wave_width))
                            c1 = min(self.wave_width - 1, int((min(d_end, self.time_end) - self.time_start) / span * self.wave_width))
                            # Draw delimiter at start and end of diff region
                            for dc in (c0, c1):
                                gac = self.name_width + 1 + dc
                                if gac < self.width:
                                    try:
                                        self.stdscr.addch(row, gac, '┃', curses.color_pair(self.C_DIFF_ED) | curses.A_BOLD)
                                        self.stdscr.addch(diff_row, gac, '┃', curses.color_pair(self.C_DIFF_ED) | curses.A_BOLD)
                                    except curses.error:
                                        pass
                            # Highlight diff region background on both rows
                            for dc in range(c0, c1 + 1):
                                gac = self.name_width + 1 + dc
                                if gac < self.width and dc % 4 == 0:
                                    try:
                                        self.stdscr.addch(row, gac, '░', curses.color_pair(self.C_DIFF_BG))
                                        self.stdscr.addch(diff_row, gac, '░', curses.color_pair(self.C_DIFF_BG))
                                    except curses.error:
                                        pass

            # ── Decode overlays ──
            self.draw_decode_overlay(row, sig_id)

            # ── A↔B measurement band ──
            # Highlight the columns between cursors A and B so the measured
            # interval is visible across every row. We re-emit the waveform
            # glyphs in the band range with the delta colour (which also labels
            # ΔAB in the status bar), preserving the waveform shape. (chgat /
            # a fresh colour pair proved unreliable in this curses build.)
            if not (is_analog and sig_rows > 1):
                wave_rows = [chars]
            if self.cursor_on and self.cursor_b_on:
                ca = self.time_to_col(self.cursor_a)
                cb = self.time_to_col(self.cursor_b)
                lo_c, hi_c = (ca, cb) if ca <= cb else (cb, ca)
                start_rel = max(0, lo_c + 1)
                end_rel = min(self.wave_width, hi_c)        # exclusive of B's column
                bx = self.name_width + 1 + start_rel
                count = min(end_rel - start_rel, self.width - 1 - bx)
                if count > 0:
                    for sr in range(sig_rows):
                        wc = wave_rows[sr] if sr < len(wave_rows) else wave_rows[-1]
                        try:
                            glyphs = "".join(wc[start_rel:start_rel + count])
                            self.stdscr.addstr(row + sr, bx, glyphs, curses.color_pair(self.C_DELTA))
                        except (curses.error, NameError, TypeError):
                            pass

            # ── Cursor A / B (span all of a multi-row signal's rows) ──
            if self.cursor_on:
                cc = self.time_to_col(self.cursor_a)
                ac = self.name_width + 1 + cc
                if 0 <= cc < self.wave_width and ac < self.width:
                    for rr in range(row, row + sig_rows):
                        try:
                            self.stdscr.addch(rr, ac, '┃', curses.color_pair(self.C_CURSOR) | curses.A_BOLD)
                        except curses.error:
                            pass

            if self.cursor_b_on:
                cc = self.time_to_col(self.cursor_b)
                ac = self.name_width + 1 + cc
                if 0 <= cc < self.wave_width and ac < self.width:
                    for rr in range(row, row + sig_rows):
                        try:
                            self.stdscr.addch(rr, ac, '┃', curses.color_pair(self.C_CURSORB) | curses.A_BOLD)
                        except curses.error:
                            pass

            # ── Markers ──
            for marker in self.markers:
                mc = self.time_to_col(marker.time)
                ac = self.name_width + 1 + mc
                if 0 <= mc < self.wave_width and ac < self.width:
                    for rr in range(row, row + sig_rows):
                        try:
                            self.stdscr.addch(rr, ac, '▎', curses.color_pair(self.C_MARKER) | curses.A_BOLD)
                        except curses.error:
                            pass

            # ── Analog scale labels (window min/max at the right edge) ──
            # Make the normalised bar plot quantitative: max at the top row,
            # min at the bottom row (single-row signals show 'lo‥hi').
            limits = None
            if is_analog:
                if getattr(self.vcd, '_store', None) is not None:
                    limits = self._row_extras.get((akind if sig_rows > 1 else 'analog', sig_id), ({}, None))[1]
                else:
                    limits = sig.numeric_range_in_window(self.time_start, self.time_end)
            if limits is not None:
                vmin, vmax = limits
                attr = curses.color_pair(self.C_VAL) | curses.A_DIM
                right = self.width - 2          # leave the scrollbar column free
                left = self.name_width + 1
                if sig_rows > 1:
                    for r_idx, val in ((row, vmax), (row + sig_rows - 1, vmin)):
                        lbl = self._fmt_num(val)
                        lx = right - len(lbl)
                        if lx > left:
                            try:
                                self.stdscr.addstr(r_idx, lx, lbl, attr)
                            except curses.error:
                                pass
                else:
                    lbl = "%s‥%s" % (self._fmt_num(vmin), self._fmt_num(vmax))
                    lx = right - len(lbl)
                    if lx > left:
                        try:
                            self.stdscr.addstr(row, lx, lbl, attr)
                        except curses.error:
                            pass

            # ── Fractional spacing: visual sub-row gap using block characters ──
            if frac_spacing > 0:
                sep_row = row + sig_rows
                if sep_row < self.height - 1:
                    gap_attr = curses.color_pair(self.C_SCOPE) | curses.A_DIM
                    if frac_spacing >= 0.5:
                        # 0.5 = lower half block — looks like half a blank row
                        gap_ch = '▄'
                    else:
                        # 0.25 = lower eighth block — looks like thin gap
                        gap_ch = '▁'
                    try:
                        gap_line = gap_ch * (self.width - 1)
                        self.stdscr.addstr(sep_row, 0, gap_line, gap_attr)
                    except curses.error:
                        pass

        # ── Scroll-position indicator (right edge) ──
        # When there are more signals than fit, a thumb on the rightmost column
        # (which the waveform leaves free) shows the visible window's position.
        total = len(self.display_order)
        if total > self.visible_signals and self.width > self.name_width + 4:
            track_top = self.wave_top
            track_bot = self.height - 2
            track_h = max(1, track_bot - track_top + 1)
            thumb_h = max(1, min(track_h, round(track_h * self.visible_signals / total)))
            thumb_top = track_top + min(track_h - thumb_h,
                                        int(track_h * self.sig_offset / total))
            sx = self.width - 1
            for r in range(track_top, track_bot + 1):
                on = thumb_top <= r < thumb_top + thumb_h
                try:
                    self.stdscr.addch(r, sx, '█' if on else '│',
                                      curses.color_pair(self.C_SCOPE)
                                      | (curses.A_BOLD if on else curses.A_DIM))
                except curses.error:
                    pass

    def _draw_group_header(self, row, name, focused):
        """Draw a collapsible group header row (▼ expanded / ▶ collapsed)."""
        if row >= self.height - 1 or row < self.wave_top:
            return
        g = self._group_by_name.get(name, {})
        arrow = '▶' if g.get('collapsed') else '▼'
        n = len(g.get('members', ()))
        kids = sum(1 for og in self.groups if og.get('parent') == name)
        count = f"{n}" if not kids else (f"{kids}▸" if not n else f"{n}+{kids}▸")
        focus = '▸' if focused else ' '
        indent = "  " * self._group_depth.get(name, 0)   # nesting depth
        label = f"{focus}{indent}{arrow} {name}  ({count})"
        attr = curses.color_pair(self.C_HEADER) | curses.A_BOLD
        try:
            self.stdscr.addstr(row, 0, label[:self.width - 1].ljust(self.width - 1), attr)
        except curses.error:
            pass

    def _draw_scope_separator(self, row, scope_name):
        """Draw a scope separator with highlighted module name."""
        if row >= self.height - 1 or row < self.wave_top:
            return
        try:
            # Draw dim separator line across full width
            sep_line = '┄' * (self.width - 1)
            self.stdscr.addstr(row, 0, sep_line, curses.color_pair(self.C_SCOPE) | curses.A_DIM)
            # Overdraw the module name with highlighted background
            label = f" {scope_name} "
            x = 2
            if x + len(label) < self.width:
                self.stdscr.addstr(row, x, label, curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

    def draw_drag_selection(self):
        """Invert the columns spanned by an in-progress mouse drag (the
        drag-to-zoom live highlight; see handle_mouse)."""
        if self._drag_anchor is None or self._drag_cur is None:
            return
        x0 = self.time_to_col(self._drag_anchor)
        x1 = self.time_to_col(self._drag_cur)
        if x1 < x0:
            x0, x1 = x1, x0
        x0 = max(0, x0)
        x1 = min(self.wave_width - 1, x1)
        if x1 < x0:
            return
        off = self.name_width + 1
        for row in range(self.ruler_row, self.height - 1):
            try:
                self.stdscr.chgat(row, off + x0, x1 - x0 + 1, curses.A_REVERSE)
            except curses.error:
                pass

    def draw_status_bar(self):
        row = self.height - 1
        w = self.width

        # Active flash message takes over the status row until it expires
        # (set by the non-blocking _show_message; cleared on an idle tick).
        if self._message is not None and time.monotonic() < self._message_expire:
            try:
                self.stdscr.addstr(row, 0, f" {self._message}"[:w - 1].ljust(w - 1),
                                   curses.color_pair(self.C_PROMPT))
            except curses.error:
                pass
            return

        # Fill background
        try:
            self.stdscr.addstr(row, 0, ' ' * (w - 1), curses.color_pair(self.C_HEADER))
        except curses.error:
            pass

        col = 1
        def _put(text, attr):
            nonlocal col
            if col + len(text) >= w:
                return
            try:
                self.stdscr.addstr(row, col, text, attr)
                col += len(text)
            except curses.error:
                pass

        def _sep():
            _put(" │ ", curses.color_pair(self.C_HEADER) | curses.A_DIM)

        # Active cursor
        if self.cursor_on:
            cur_lbl = "A" if self.active_cursor == 'a' else "B"
            _put(f"Cur", curses.color_pair(self.C_HEADER))
            attr = curses.color_pair(self.C_CURSOR) if self.active_cursor == 'a' else curses.color_pair(self.C_CURSORB)
            _put(cur_lbl, attr | curses.A_BOLD)
            _put(f" t=", curses.color_pair(self.C_HEADER))
            ct_str = self.format_ruler_time(self.cursor_time)
            _put(ct_str, curses.color_pair(self.C_HEADER) | curses.A_BOLD)

        # Delta — plus the measurement readout: Δt as a real frequency
        # (1/Δt via the trace timescale) and the focused signal's transition
        # count inside [A, B].
        if self.cursor_b_on:
            _sep()
            _put("ΔAB=", curses.color_pair(self.C_HEADER))
            delta_str = self.format_ruler_time(self.cursor_delta)
            _put(delta_str, curses.color_pair(self.C_DELTA) | curses.A_BOLD)
            delta = self.cursor_delta
            if delta:
                _put(f" ({_fmt_freq(1.0 / (delta * self.tick_seconds()))}",
                     curses.color_pair(self.C_HEADER))
                sid = self.focused_signal_id()
                sig = self.get_signal(sid) if sid else None
                if sig is not None:
                    lo, hi = sorted((self.cursor_a, self.cursor_b))
                    nt = sig.transition_count_in_window(lo, hi)
                    if nt:
                        _put(f" · {nt}t", curses.color_pair(self.C_HEADER) | curses.A_DIM)
                _put(")", curses.color_pair(self.C_HEADER))

        # View range
        _sep()
        span = self.time_end - self.time_start
        ts_str = self.format_ruler_time(self.time_start)
        te_str = self.format_ruler_time(self.time_end)
        _put(f"[{ts_str}‥{te_str}]", curses.color_pair(self.C_HEADER))

        # Signal range
        _sep()
        ts = len(self.display_order)
        end = min(self.sig_offset + self.visible_signals, ts)
        _put(f"Sig {self.sig_offset+1}-{end}/{ts}", curses.color_pair(self.C_HEADER))

        # Loading progress
        job = getattr(self, '_analysis_job', None)
        if job is not None:
            _sep()
            pct = ('%d%%' % min(100, job.progress[0] * 100 // job.progress[1])) if job.progress and job.progress[1] else '…'
            label = getattr(job, 'label', 'Search')
            _put(f"{label} {pct} · cancel", curses.color_pair(self.C_PROMPT))
        if self.vcd.is_loading:
            _sep()
            pct = self.vcd.load_progress
            chg = self.vcd.load_changes
            if self.vcd._lazy_mode or pct < 1:
                _put(f"⟳ Indexing {pct}%", curses.color_pair(self.C_PROMPT) | curses.A_BOLD)
            else:
                _put(f"⟳ Loading {pct}% ({chg:,})", curses.color_pair(self.C_PROMPT) | curses.A_BOLD)
        elif self.vcd._load_error:
            _sep()
            _put(f"⚠ {self.vcd._load_error[:20]}", curses.color_pair(self.C_PROMPT))
        elif self.vcd._lazy_mode:
            _sep()
            n_loaded = len(self.vcd._loaded_ranges)
            _put(f"Lazy:{n_loaded}chk", curses.color_pair(self.C_HEADER) | curses.A_DIM)

        # Diff indicator
        if self.diff_ranges:
            _sep()
            n_diff = sum(len(r) for r in self.diff_ranges.values())
            _put(f"Diff:{n_diff}▵", curses.color_pair(self.C_DIFF_ED) | curses.A_BOLD)

        # Panel mode indicator
        if self.panel_mode:
            _sep()
            focus_name = "Tree" if self.panel_focus == 0 else "Wave"
            _put(f"Panel:{focus_name}", curses.color_pair(self.C_PROMPT) | curses.A_BOLD)

        # Spacing indicator
        if self.signal_spacing > 0:
            _sep()
            sp_str = f"{self.signal_spacing:g}" if self.signal_spacing != int(self.signal_spacing) else f"{int(self.signal_spacing)}"
            _put(f"Spc:{sp_str}", curses.color_pair(self.C_HEADER) | curses.A_DIM)

        # Markers
        if self.markers:
            _sep()
            _put(f"Mrk:{len(self.markers)}", curses.color_pair(self.C_MARKER))

        # Agent control: the user must always be able to see that something
        # else can drive this view, and when it last did.
        if self.control is not None:
            _sep()
            n = self.control.clients
            recent = (time.monotonic() - self.control.last_activity) < 2.0
            if recent:
                label, attr = "◆AGENT", curses.color_pair(self.C_PROMPT) | curses.A_BOLD
            elif n:
                label, attr = f"◆agent:{n}", curses.color_pair(self.C_MARKER)
            else:
                label, attr = "◇ctl", curses.color_pair(self.C_HEADER) | curses.A_DIM
            _put(label, attr)

        # Focused signal name
        sid = self.focused_signal_id()
        if sid:
            sig = self.get_signal(sid)
            if sig:
                _sep()
                _put(f"▸{sig.name}", curses.color_pair(self.C_HEADER) | curses.A_BOLD)
                # Per-signal radix indicator
                sig_rx = self.get_signal_radix(sid)
                _put(f"[{sig_rx.value}]", curses.color_pair(self.C_VAL))
            # Frequency info
            freq = self.get_frequency_info()
            if freq:
                _put(f" {freq}", curses.color_pair(self.C_VAL))
            # Transition count in view
            tc = self.get_transition_count()
            if tc is not None and tc > 0:
                _put(f" Δ{tc}", curses.color_pair(self.C_HEADER) | curses.A_DIM)
            # Collapsed scopes indicator
            if self.collapsed_scopes:
                _sep()
                _put(f"Fld:{len(self.collapsed_scopes)}", curses.color_pair(self.C_HEADER) | curses.A_DIM)

    def draw_help_overlay(self):
        """Draw a centered quick-help overlay.

        The box is built programmatically so every row is padded to the same
        width and the borders line up regardless of the content."""
        title = " ALWAVELETS QUICK HELP \u2014 F1 for full manual "
        sections = [
            ("Navigation", [
                "\u2190\u2192/hl Scroll \u00b7 \u2191\u2193/jk Signals \u00b7 +/- Zoom",
                "Z Fit all \u00b7 W Fit A\u2194B \u00b7 S Snap \u00b7 Home/End \u00b7 PgUp/Dn",
            ]),
            ("VIM-style", [
                "gg First in scope \u00b7 G Last in scope",
                "V Visual select (\u2191\u2193 extends) \u00b7 Backspace remove",
                "Shift+\u2191\u2193 Prev/next module \u00b7 Shift+\u2190\u2192 Fold scope",
                "i Hierarchical indent mode",
            ]),
            ("Cursors & Markers", [
                "c Cursor \u00b7 1/2 A/B \u00b7 m Mark \u00b7 M Clear",
                "< > . Marker jump \u00b7 gt Go to time",
                "e Edge-to-edge \u00b7 f Find value \u00b7 ]/[ Edge (focused)",
                "P Pattern \u00b7 / Find \u00b7 n Next",
            ]),
            ("Signals", [
                "s Panel mode \u00b7 Tab Panel focus / fold group",
                "B Scope tree \u00b7 { Collapse all \u00b7 } Expand all",
                "x Expand bus \u00b7 F Float decode (S|E|M)",
                "J/K Reorder \u00b7 d/u Del/undo \u00b7 Space Highlight",
                "Enter Full/short names \u00b7 L Label size",
            ]),
            ("Display", [
                "r Radix(all) \u00b7 R Radix(sig) \u00b7 v Color \u00b7 H Height",
                "a Analog \u00b7 t Values \u00b7 T Time \u00b7 D Decode \u00b7 Q Grid",
                "w Wave style \u00b7 () Name col \u00b7 | Spacing \u00b7 \\ Theme",
                "C Colors \u00b7 0/9 Menu page",
            ]),
            ("ISA Debug", [
                "` Trace query \u00b7 p Register panel \u00b7 ;trace Build",
                "B\u2192t Tag \u00b7 B\u2192T Load tags \u00b7 ;autotag Auto-detect",
                "Queries: mn | thread | ipc | fairness | diverge",
            ]),
            ("Debug & Analysis", [
                "A Annotation \u00b7 ;formal CEX \u00b7 ;power Hot scopes",
                ";axi ;ahb ;wb Bus decode",
                ";bm Bookmark \u00b7 ;gb Goto \u00b7 ;bookmarks List",
            ]),
            ("Commands & Export", [
                ": Tcl \u00b7 ; CLI (diff, theme, trace, query)",
                "E Export menu \u00b7 X SVG \u00b7 I Protocol decode",
                "F5 Live reload (watch file, auto-refresh)",
            ]),
        ]
        footer = [
            "Formats: VCD \u00b7 FST (native) \u00b7 LXT / GHW",
            "Mouse: Click=cursor \u00b7 Right=marker \u00b7 Scroll=zoom",
            "MCP: run --mcp for agentic snapshots (F1 \u2192 27)",
        ]
        close = "Press any key to close"

        # Inner-text rows (no borders/padding yet).
        rows = []
        for name, items in sections:
            rows.append(" " + name)
            rows.extend("  " + it for it in items)
            rows.append("")
        rows.extend("  " + f for f in footer)

        inner = max([len(title), len(close)] + [len(r) for r in rows])
        out = ["\u2554" + title.center(inner, "\u2550") + "\u2557"]
        for r in rows:
            out.append("\u2551" + r.ljust(inner) + "\u2551")
        out.append("\u2551" + close.center(inner) + "\u2551")
        out.append("\u255a" + "\u2550" * inner + "\u255d")

        h = len(out)
        w = inner + 2
        start_y = max(0, (self.height - h) // 2)
        start_x = max(0, (self.width - w) // 2)
        for i, line in enumerate(out):
            y = start_y + i
            if y >= self.height:
                break
            try:
                self.stdscr.addstr(y, start_x, line[:self.width - start_x],
                                   curses.color_pair(self.C_HELP))
            except curses.error:
                pass

    def draw_color_guide_overlay(self):
        """Draw a centered color legend with live-colored swatches."""
        # We'll build this procedurally to use actual curses color pairs
        W = 54  # inner width of the box
        lines_meta = []  # list of (text, [(col_offset, text, attr), ...])

        def plain(text):
            """A line with only the box-drawing frame colored."""
            return (text, [])

        def swatch(label, chars, attr_code, note=""):
            """Build a line: ║  <label>  <colored swatch>  <note>  ║"""
            lbl = f"  {label:<16s}"
            sw = chars
            nt = f"  {note}" if note else ""
            pad = W - len(lbl) - len(sw) - len(nt)
            full = f"║{lbl}{sw}{' ' * max(pad, 0)}{nt}║"
            # attr spans for the swatch portion
            sw_start = 1 + len(lbl)
            return (full, [(sw_start, sw, attr_code)])

        def multi_swatch(label, swatches, note=""):
            """Multiple colored chunks: swatches = [(chars, attr_code), ...]"""
            lbl = f"  {label:<16s}"
            sw_total = sum(len(s[0]) for s in swatches)
            nt = f"  {note}" if note else ""
            pad = W - len(lbl) - sw_total - len(nt)
            # Build full text with placeholder spaces for swatches
            full = f"║{lbl}{''.join(s[0] for s in swatches)}{' ' * max(pad, 0)}{nt}║"
            spans = []
            offset = 1 + len(lbl)
            for chars, attr_code in swatches:
                spans.append((offset, chars, attr_code))
                offset += len(chars)
            return (full, spans)

        # Build the overlay content
        lines_meta.append(plain(f"╔{'═' * W}╗"))
        lines_meta.append(plain(f"║{'COLOR GUIDE':^{W}}║"))
        lines_meta.append(plain(f"╠{'═' * W}╣"))
        lines_meta.append(plain(f"║{'':^{W}}║"))

        # ── Single-bit signals ──
        lines_meta.append(plain(f"║{'  ── Single-Bit Signals ──':<{W}}║"))
        lines_meta.append(swatch("High (1)", "▔▔▔▔▔▔▔▔▔▔", 0, "logic high"))
        lines_meta.append(swatch("Low (0)", "▁▁▁▁▁▁▁▁▁▁", 5, "logic low"))
        lines_meta.append(swatch("Transition", "▔▔│▁▁│▔▔│▁▁", -1, "value change"))  # -1 = special
        lines_meta.append(swatch("Unknown (x)", "░░░░░░░░░░", 2, "uninitialized"))
        lines_meta.append(swatch("High-Z (z)", "──────────", 3, "floating"))
        lines_meta.append(plain(f"║{'':^{W}}║"))

        # ── Bus signals ──
        lines_meta.append(plain(f"║{'  ── Bus / Multi-Bit Signals ──':<{W}}║"))
        lines_meta.append(swatch("Bus body", "══════════", 6, "stable value"))
        lines_meta.append(swatch("Bus transition", "═══╳══════", -2, "value change"))  # -2 = special
        lines_meta.append(swatch("Bus label", " 3F  A0  ", -3, "hex/dec/bin value"))  # -3 = special
        lines_meta.append(plain(f"║{'':^{W}}║"))

        # ── Analog signals ──
        lines_meta.append(plain(f"║{'  ── Analog Signals (heat-map) ──':<{W}}║"))
        lines_meta.append(multi_swatch("Hot (high)", [("██▇▇", 7)], "top of range"))
        lines_meta.append(multi_swatch("Warm (mid)", [("▅▅▄▄", 8)], "mid range"))
        lines_meta.append(multi_swatch("Cool (low)", [("▂▂▁▁", 9)], "bottom of range"))
        lines_meta.append(multi_swatch("Gradient",
            [("▁▁", 9), ("▂▃", 9), ("▄▅", 8), ("▆▇", 8), ("██", 7)], "full sweep"))
        lines_meta.append(plain(f"║{'':^{W}}║"))

        # ── UI elements ──
        lines_meta.append(plain(f"║{'  ── UI Elements ──':<{W}}║"))
        lines_meta.append(swatch("Cursor A", "┃", -4, "primary cursor (blue)"))
        lines_meta.append(swatch("Cursor B", "┃", -5, "secondary cursor (magenta)"))
        lines_meta.append(swatch("Marker", "▎", -6, "time marker (pink)"))
        lines_meta.append(swatch("Cursor value", "FF", -7, "value at cursor"))
        lines_meta.append(swatch("Scope prefix", "top.", -8, "module hierarchy"))
        lines_meta.append(plain(f"║{'':^{W}}║"))
        lines_meta.append(plain(f"║{'Press any key to close':^{W}}║"))
        lines_meta.append(plain(f"╚{'═' * W}╝"))

        # ── Render ──
        h = len(lines_meta)
        bw = W + 2  # box width with borders
        start_y = max(0, (self.height - h) // 2)
        start_x = max(0, (self.width - bw) // 2)

        for i, (text, spans) in enumerate(lines_meta):
            y = start_y + i
            if y >= self.height:
                break
            display = text[:self.width - start_x]
            # Draw full line in help color (frame)
            try:
                self.stdscr.addstr(y, start_x, display, curses.color_pair(self.C_HELP))
            except curses.error:
                pass

            # Overlay colored swatches
            for offset, sw_text, attr_code in spans:
                sx = start_x + offset
                if sx >= self.width:
                    continue
                sw_display = sw_text[:self.width - sx]

                # Map special codes to actual color pairs
                if attr_code == -1:  # transition: mix of high, edge, low
                    # Draw char by char
                    for ci, ch in enumerate(sw_display):
                        cx = sx + ci
                        if cx >= self.width:
                            break
                        if ch == '│':
                            a = curses.color_pair(self.C_EDGE) | curses.A_BOLD
                        elif ch == '▔':
                            a = curses.color_pair(self.C_HIGH)
                        elif ch == '▁':
                            a = curses.color_pair(self.C_LOW)
                        else:
                            a = curses.color_pair(self.C_HELP)
                        try:
                            self.stdscr.addch(y, cx, ch, a)
                        except curses.error:
                            pass
                elif attr_code == -2:  # bus transition: mix of bus body and edge
                    for ci, ch in enumerate(sw_display):
                        cx = sx + ci
                        if cx >= self.width:
                            break
                        if ch == '╳':
                            a = curses.color_pair(self.C_EDGE) | curses.A_BOLD
                        elif ch == '═':
                            a = curses.color_pair(self.C_BUS)
                        else:
                            a = curses.color_pair(self.C_HELP)
                        try:
                            self.stdscr.addch(y, cx, ch, a)
                        except curses.error:
                            pass
                elif attr_code == -3:  # bus label
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_BUSLBL))
                    except curses.error:
                        pass
                elif attr_code == -4:  # cursor A
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_CURSOR) | curses.A_BOLD)
                    except curses.error:
                        pass
                elif attr_code == -5:  # cursor B
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_CURSORB) | curses.A_BOLD)
                    except curses.error:
                        pass
                elif attr_code == -6:  # marker
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_MARKER) | curses.A_BOLD)
                    except curses.error:
                        pass
                elif attr_code == -7:  # cursor value
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_VAL) | curses.A_BOLD)
                    except curses.error:
                        pass
                elif attr_code == -8:  # scope prefix
                    try:
                        self.stdscr.addstr(y, sx, sw_display, curses.color_pair(self.C_SCOPE))
                    except curses.error:
                        pass
                else:
                    # Normal waveform attr code via _attr_for
                    try:
                        self.stdscr.addstr(y, sx, sw_display, self._attr_for(attr_code))
                    except curses.error:
                        pass
