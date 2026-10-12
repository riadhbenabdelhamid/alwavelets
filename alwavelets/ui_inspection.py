"""Optional signal details, metadata-only overview, and empty-view guidance."""
import curses

from .background import BackgroundQueries
from .rendering import time_to_column
from .streaming import read_cancellation
from .ui_actions import shortcut_for
from .ui_chrome import elide, signal_origin, text_width, wave_bottom, wave_origin, wave_right
from .ui_navigation import set_time_range


class InspectionMixin:
    def toggle_inspector(self):
        self.show_inspector = not self.show_inspector
        if not self.show_inspector and hasattr(self, '_queries'):
            self._queries.invalidate('inspector')
        self.update_layout()
        self._needs_layout = self._dirty = True

    def toggle_overview(self):
        self.show_overview = not self.show_overview
        self.update_layout()
        self._needs_layout = self._dirty = True

    def _detail_line(self, row, text, attr=0, left=0):
        if not 0 <= row < self.height - 1:
            return
        right = wave_right(self) if row < getattr(self, 'inspector_top', self.height - 1) else self.width
        room = max(0, right - left - 1)
        text = elide(text, room)
        try:
            self.stdscr.addstr(row, left, text + ' ' * (room - text_width(text)), attr)
        except curses.error:
            pass

    def draw_empty_view(self):
        if self.vcd._load_error:
            title, hint = 'Trace could not be loaded', str(self.vcd._load_error)
        elif self.vcd.is_loading:
            title, hint = 'Loading trace…', f'{self.vcd.load_progress}% · Signal selection remains optional'
        elif not len(self.vcd.signals):
            title, hint = 'No signals in this trace', 'Check that the file contains signal declarations'
        elif getattr(self, '_empty_view_folded', False):
            title = 'No visible signals · scopes folded'
            hint = f"{shortcut_for(self, '_kb_expand_all', 1)} Expand scopes · {shortcut_for(self, 'open_finder', 1)} Find signals"
        else:
            title = 'No signals selected'
            hint = f"{shortcut_for(self, 'open_finder', 1)} Find signals · {shortcut_for(self, '_kb_show_tree', 1)} Browse scopes"
        row = min(self.wave_top + 1, wave_bottom(self) - 1)
        self._detail_line(row, title, curses.A_BOLD, signal_origin(self))
        if row + 1 < wave_bottom(self):
            self._detail_line(row + 1, hint, curses.A_DIM, signal_origin(self))

    def _inspector_window(self):
        return (id(self.vcd), self.sig_offset, self.focus_idx,
                self.time_start, self.time_end, self.cursor_a, self.cursor_b, self.cursor_b_on)

    def prepare_inspector(self):
        """Cancel obsolete reads before the drawing pass touches catalog pages."""
        window = self._inspector_window() if self.show_inspector else None
        if window != getattr(self, '_inspector_read_window', None):
            if hasattr(self, '_queries'):
                self._queries.invalidate('inspector')
            self._inspector_read_window = window

    def inspector_details(self, sid, sig):
        if self.vcd._load_error:
            return {'status': 'Trace read failed; details unavailable'}
        if self.vcd.is_loading:
            return {'status': 'Loading trace… details will update when ready'}
        if not hasattr(self, '_queries'):
            self._queries = BackgroundQueries()
        self.prepare_inspector()
        vcd = self.vcd
        start, end, a, b = self.time_start, self.time_end, self.cursor_a, self.cursor_b
        b_on = self.cursor_b_on

        def loaded(lo, hi):
            return not vcd._lazy_mode or not vcd._uncovered_ranges(lo, hi)

        # Snapshot coverage on the UI thread. The inspector never expands a
        # lazy trace's loaded ranges just to inspect an off-screen cursor.
        a_ready = 0 <= a <= vcd.max_time and loaded(a, a)
        b_ready = b_on and 0 <= b <= vcd.max_time and loaded(b, b)
        range_ready = loaded(start, end)

        def compute(cancel):
            with read_cancellation(cancel):
                return {'a': sig.value_at(a) if a_ready else None,
                        'b': sig.value_at(b) if b_ready else None,
                        'transitions': sig.transition_count_in_window(start, end) if range_ready else None}

        key = (sid, sig.num_changes(), a_ready, b_ready, range_ready)
        # Point values and an indexed transition count only: opening this
        # panel must not start a whole-window scan for clock statistics.
        window = self._inspector_window()
        result = self._queries.request('inspector', window, key, compute,
                                       None, cancellable=True, replace='focused')
        if result is None and ('inspector', window, key) in self._queries.ready:
            return {'status': 'Read failed · close and reopen inspector to retry'}
        return result

    def draw_inspector(self):
        if not self.inspector_rows:
            return
        top = getattr(self, 'inspector_top', wave_bottom(self))
        room = max(1, self.width - 2)
        key = shortcut_for(self, 'toggle_inspector', 1)
        lines = [f' Signal inspector · {key} close']
        sid = self.focused_signal_id()
        sig = self.get_signal(sid) if sid else None
        if sig is None:
            lines += [' Focus a waveform to inspect it', ' / Find signals · ↑↓ Choose a row']
        else:
            # Use two rows for the path, retaining more hierarchy than the
            # waveform label column can show. Clip only at cell boundaries.
            from .ui_prompt import clip_cells
            path = sig.full_name or sig.name
            first, _ = clip_cells(path, room)
            lines.append(' ' + first)
            if len(first) < len(path):
                lines.append(' ' + path[len(first):])
            lines.append(f' {sig.size} bit · {sig.signal_type} · {self.get_signal_radix(sid).value}')
            details = self.inspector_details(sid, sig)
            if details is None:
                lines += [' Reading focused signal…']
            elif 'status' in details:
                lines += [' ' + details['status']]
            else:
                def value(which, tick):
                    raw = details[which]
                    if raw is None:
                        return 'outside trace' if tick < 0 or tick > self.vcd.max_time else 'not loaded'
                    return self._format_signal_raw_value(sig, sid, raw)
                lines.append(f' A {self.format_ruler_time(self.cursor_a)} = {value("a", self.cursor_a)}')
                lines.append(f' B {self.format_ruler_time(self.cursor_b)} = {value("b", self.cursor_b)}'
                             if self.cursor_b_on else ' B off · 2 Activate cursor B')
                count = details['transitions']
                lines.append(f' View transitions: {count:,}' if count is not None else ' View data not loaded')
                lines.append(f' Δ A–B: {self.format_ruler_time(self.cursor_delta)}'
                             if self.cursor_b_on else ' Enable both cursors to compare values')
        for offset in range(self.inspector_rows):
            self._detail_line(top + offset, lines[offset] if offset < len(lines) else '',
                              curses.color_pair(self.C_HEADER) | curses.A_BOLD if offset == 0 else curses.A_NORMAL)

    def draw_overview(self):
        if self.overview_row is None:
            return
        row, left, width = self.overview_row, wave_origin(self), max(1, self.wave_width - 1)
        known = getattr(self.vcd, '_extent_known', True)
        end = self.vcd.max_time
        label = ' Overview' if known else ' Extent…'
        try:
            self.stdscr.addstr(row, signal_origin(self), elide(label, self.name_width), curses.A_DIM)
            if end <= 0:
                self._detail_line(row, 'No time range yet', curses.A_DIM, left)
                return
            def column(tick):
                return max(0, min(width - 1, time_to_column(tick, 0, end, max(1, width - 1))))
            cells = ['─'] * width
            lo, hi = column(self.time_start), column(self.time_end)
            for col in range(lo, hi + 1):
                cells[col] = '━'
            cells[lo], cells[hi] = '[', ']'
            if lo == hi:
                cells[lo] = '◆'
            for marker in self.markers:
                if 0 <= marker.time <= end:
                    cells[column(marker.time)] = '│'
            if self.cursor_on and 0 <= self.cursor_a <= end:
                cells[column(self.cursor_a)] = 'A'
            if self.cursor_b_on and 0 <= self.cursor_b <= end:
                cells[column(self.cursor_b)] = 'B'
            self.stdscr.addstr(row, left, ''.join(cells), curses.color_pair(self.C_SCOPE))
        except curses.error:
            pass

    def overview_click(self, column):
        if not getattr(self.vcd, '_extent_known', True) or self.vcd.max_time <= 0:
            self._show_message('Trace extent is still loading')
            return
        width = max(1, self.wave_width - 1)
        col = max(0, min(width - 1, column))
        center = col * self.vcd.max_time // max(1, width - 1)
        span = self.time_end - self.time_start
        start = max(0, min(center - span // 2, self.vcd.max_time - span))
        set_time_range(self, start, start + span)
