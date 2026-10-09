"""Paged instruction matches alongside the waveform; all reads use a worker."""
import curses
import os

from .background import AnalysisJob, BackgroundQueries
from .exporting import write_instruction_csv


class InstructionResultsMixin:
    def _reset_instruction_browser(self):
        queries = getattr(self, '_queries', None)
        if queries is not None:
            queries.invalidate('instruction-results')
        self.show_instruction_results = False
        self._instruction_result_index = 0
        self._instruction_result_synced = None
        self._instruction_close_pending = False

    def _instruction_matches(self):
        outcome = getattr(self, '_instruction_query_result', None)
        return outcome.matches if outcome is not None else None

    def _instruction_page_size(self):
        return min(256, max(1, self.height - max(3, self.height // 2) - 4))

    def _instruction_page(self):
        matches = self._instruction_matches()
        if matches is None or not len(matches):
            return 0, None
        index = min(max(0, getattr(self, '_instruction_result_index', 0)), len(matches) - 1)
        self._instruction_result_index = index
        size = self._instruction_page_size()
        start = index // size * size
        if not hasattr(self, '_queries'):
            self._queries = BackgroundQueries()
        page = self._queries.request('instruction-results', (id(matches), start, size), 'page',
                                     lambda cancel: matches.read_page(start, size, cancel), None, cancellable=True)
        return start, page

    def poll_instruction_results(self):
        if not getattr(self, 'show_instruction_results', False):
            return
        start, page = self._instruction_page()
        index = getattr(self, '_instruction_result_index', 0)
        token = (id(self._instruction_matches()), index)
        if page and token != getattr(self, '_instruction_result_synced', None):
            offset = index - start
            if offset < len(page):
                self.cursor_time = page[offset]['time']
                self.cursor_on = True
                self._ensure_cursor_visible()
                self._instruction_result_synced = token
                self._dirty = True
        if page and getattr(self, '_instruction_close_pending', False):
            self.show_instruction_results = False
            self._instruction_close_pending = False
            self._dirty = True

    def _cmd_instruction_results(self, args=''):
        text = args.strip()
        action, _, value = text.partition(' ')
        if action == 'close':
            self.show_instruction_results = False
            return
        matches = self._instruction_matches()
        if matches is None or not len(matches):
            self._show_message('No instruction matches. Run a trace query first.')
            return
        index = getattr(self, '_instruction_result_index', 0)
        try:
            if action == 'export':
                path = value.strip() or self.prompt_input('Export all matches to CSV: ')
                if path:
                    self._export_instruction_results(path)
                return
            if action == 'next':
                index += 1
            elif action == 'prev':
                index -= 1
            elif action == 'goto':
                index = int(value) - 1
                if not 0 <= index < len(matches):
                    raise ValueError('Row must be between 1 and %d' % len(matches))
            elif action:
                raise ValueError('Usage: results [next|prev|goto N|export PATH|close]')
        except ValueError as exc:
            self._show_message(str(exc))
            return
        self._instruction_result_index = min(max(0, index), len(matches) - 1)
        self._instruction_result_synced = None
        self._instruction_close_pending = False
        self.show_instruction_results = True
        self._dirty = True

    def _export_instruction_results(self, path):
        if getattr(self, '_analysis_job', None) is not None:
            self._show_message('Analysis already running; use cancel before exporting')
            return
        matches = self._instruction_matches()
        if matches is None:
            self._show_message('No instruction query results')
            return
        destination = os.path.abspath(os.path.expanduser(path))
        # A typo must not overwrite the open trace or the instruction database.
        protected = [getattr(self.vcd, '_filename', ''), getattr(getattr(matches, 'source', matches), 'path', '')]
        for source in protected:
            if source and (os.path.realpath(destination) == os.path.realpath(source) or
                           (os.path.exists(destination) and os.path.exists(source) and os.path.samefile(destination, source))):
                self._show_message('Choose an export path different from the source trace and index')
                return
        def compute(cancel, progress):
            return write_instruction_csv(matches, destination, cancel=cancel, progress=progress)
        def complete(count):
            self._show_message(f'Exported {count} instruction matches to {destination}')
        self._analysis_job = AnalysisJob(compute, complete)
        self._analysis_job.kind, self._analysis_job.label = 'instruction-export', 'Export'
        self._show_message('Exporting all instruction matches; use cancel to stop')

    def instruction_results_handle_key(self, key):
        if key in (10, curses.KEY_ENTER):
            self._instruction_close_pending = True
            self.poll_instruction_results()
            return
        if key in (27, ord('q')):
            self.show_instruction_results = False
            return
        if key == ord(';'):
            return self.do_command_line()
        matches = self._instruction_matches()
        if matches is None or not len(matches):
            self.show_instruction_results = False
            return
        index = self._instruction_result_index
        if key in (curses.KEY_DOWN, ord('j'), ord('n'), ord('>')):
            index += 1
        elif key in (curses.KEY_UP, ord('k'), ord('p'), ord('<')):
            index -= 1
        elif key in (curses.KEY_NPAGE, ord(' ')):
            index += self._instruction_page_size()
        elif key == curses.KEY_PPAGE:
            index -= self._instruction_page_size()
        elif key == curses.KEY_HOME:
            index = 0
        elif key in (curses.KEY_END, ord('G')):
            index = len(matches) - 1
        elif key == ord('g'):
            row = self.prompt_input('Result row (1-%d): ' % len(matches))
            if row:
                self._cmd_instruction_results('goto ' + row)
            return
        elif key == ord('e'):
            self._cmd_instruction_results('export')
            return
        self._instruction_result_index = min(max(0, index), len(matches) - 1)
        self._instruction_close_pending = False
        self._dirty = True

    def draw_instruction_results(self):
        matches = self._instruction_matches()
        if matches is None:
            self.show_instruction_results = False
            return
        top = max(3, self.height // 2)
        start, page = self._instruction_page()
        index = getattr(self, '_instruction_result_index', 0)
        def put(row, text, selected=False):
            text = ''.join(c if c.isprintable() else ' ' for c in text)
            try:
                self.stdscr.addstr(row, 0, text[:max(0, self.width - 1)].ljust(max(0, self.width - 1)),
                                   curses.A_REVERSE if selected else curses.A_NORMAL)
            except curses.error:
                pass
        put(top, f' Instruction results | row {index + 1:,} / {len(matches):,}', True)
        put(top + 1, ' Row         Time        Hart Thread PC                 Instruction')
        for i in range(self._instruction_page_size()):
            entry = page[i] if page and i < len(page) else None
            line = ''
            if entry is not None:
                pc = entry.get('pc')
                address = hex(pc) if pc is not None else '-'
                line = f" {start+i+1:<11} {entry['time']:<11} {entry.get('hart_id', 0)!s:<4} {entry.get('thread_id', 0)!s:<6} {address:<18} {entry['decoded'].get('asm', '')}"
            elif page is None and not i:
                line = ' Loading results…'
            put(top + 2 + i, line, start + i == index)
        put(self.height - 2, ' ↑↓/n/p next/prev  PgUp/PgDn page  Home/End  g row  e export all  Enter/Esc close')
