"""Responsive register snapshots, kept separate from waveform drawing."""
import curses
import math
import textwrap

from .background import BackgroundQueries
from .decoders import RV32Decoder
from .instruction_store import InstructionResults
from .ui_actions import shortcut_for
from .ui_chrome import elide, text_width, wave_bottom


def register_layout(viewer, value_width=8):
    """Prefer two columns; widen on short screens before resorting to pages."""
    top = max(1, getattr(viewer, 'ruler_row', 2))
    available = max(0, wave_bottom(viewer) - top)
    cell = 9 + value_width  # xNN, ABI name, and the complete hexadecimal value
    columns = 2 if available >= 22 else 4
    width = max(0, min(viewer.width - 2, columns * (cell + 2) + 2))
    columns = max(1, min(4, (width - 2) // (cell + 2)))
    rows = max(0, min(math.ceil(32 / columns), available - 6))
    return {'left': max(0, viewer.width - width - 1), 'top': top,
            'width': width, 'height': min(available, rows + 6),
            'columns': columns, 'rows': rows, 'cell': cell,
            'fits': width >= cell + 4 and rows > 0}


def register_state(viewer):
    trace, cursor = viewer.instr_trace, viewer.cursor_time
    if not trace.built:
        return None, ('Waiting for instruction trace…' if getattr(viewer, '_analysis_job', None)
                      else 'No instruction trace. Tag instruction signals in B, then reopen with p.')
    if not isinstance(trace.entries, InstructionResults):
        return trace.register_panel_state(cursor), None
    if not hasattr(viewer, '_queries'):
        viewer._queries = BackgroundQueries()
    window = (id(trace.entries), cursor)
    state = viewer._queries.request('registers', window, 'panel',
                                    lambda cancel: trace.register_panel_state(cursor, cancel=cancel),
                                    None, cancellable=True)
    failed = state is None and ('registers', window, 'panel') in viewer._queries.ready
    return state, ('Register read failed' if failed else 'Reading registers…') if state is None else None


class RegisterPanelMixin:
    def close_register_panel(self):
        self.show_reg_panel = False
        self._register_panel_layout = None
        self._pending_g = False
        queries = getattr(self, '_queries', None)
        if queries is not None:
            queries.invalidate('registers')
        self._dirty = True

    def page_register_panel(self, step):
        self._pending_g = False
        layout = getattr(self, '_register_panel_layout', None) or register_layout(self)
        size = max(1, layout['rows'] * layout['columns'])
        pages = math.ceil(32 / size)
        self._register_panel_page = min(pages - 1, max(0, getattr(self, '_register_panel_page', 0) + step))
        self._dirty = True

    def register_panel_handle_key(self, key):
        if key == 27:
            self.close_register_panel()
        elif key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
            self.page_register_panel(-1 if key == curses.KEY_PPAGE else 1)
        else:
            return False
        return True

    def register_panel_mouse(self, x, y, state):
        layout = getattr(self, '_register_panel_layout', None)
        if layout is None:
            return False
        left, top, width, height = (layout[k] for k in ('left', 'top', 'width', 'height'))
        if not (left <= x < left + width and top <= y < top + height):
            return False
        self._drag_anchor = self._drag_cur = None
        if state & curses.BUTTON4_PRESSED:
            self.page_register_panel(-1)
        elif state & (getattr(curses, 'BUTTON5_PRESSED', 0) | (1 << 21) | 0x200000):
            self.page_register_panel(1)
        # Never move the waveform cursor or select a hidden signal through
        # the panel, including when no data has loaded yet.
        return True

    def draw_register_panel(self):
        try:
            RegisterPanelMixin._draw_register_panel(self)
        except curses.error:
            # A terminal may resize while this frame is being painted.
            self._needs_layout = self._dirty = True

    def _draw_register_panel(self):
        state, message = register_state(self)
        values = ([('--------' if value is None else f'{value:08x}') for value in state[1]]
                  if state is not None else ['--------'] * 32)
        value_width = max(8, max(map(len, values), default=8))
        layout = self._register_panel_layout = register_layout(self, value_width)
        left, top, width, height = (layout[k] for k in ('left', 'top', 'width', 'height'))
        if width < 4 or height < 2:
            return
        room = width - 4
        header = curses.color_pair(self.C_HEADER)
        border = curses.color_pair(self.C_MENUSEP) | curses.A_DIM
        # Clear every cell, including unused grid cells, so old register
        # strings and waveforms cannot bleed through the panel.
        for row in range(top, top + height):
            self.stdscr.addstr(row, left, ' ' * width, curses.A_NORMAL)
            self.stdscr.addch(row, left, '│', border)
            self.stdscr.addch(row, left + width - 1, '│', border)
        self.stdscr.addstr(top, left, '┌' + '─' * (width - 2) + '┐', border)
        self.stdscr.addstr(top + height - 1, left, '└' + '─' * (width - 2) + '┘', border)

        def put(row, text, attr=curses.A_NORMAL):
            if top <= row < top + height - 1:
                self.stdscr.addstr(row, left + 2, elide(text, room), attr)

        key = shortcut_for(self, '_kb_reg_panel', 1) if hasattr(self, 'keymap') else 'p'
        key = key or 'p'
        put(top, 'Registers · hex', header | curses.A_BOLD)
        if not layout['fits']:
            put(top + 1, 'Enlarge terminal', curses.A_DIM)
            put(top + height - 2, key + '/Esc close', header)
            return
        rows = layout['rows']
        size = rows * layout['columns']
        pages = math.ceil(32 / size)
        page = self._register_panel_page = min(pages - 1, max(0, getattr(self, '_register_panel_page', 0)))
        info_row = top + 2 + rows
        if state is not None:
            hart, _, recent, current = state
            context = f'Hart {hart}' if current is not None else 'No instruction at cursor'
            put(top + 1, context + f' · t={self.cursor_time}', header)
            for index in range(page * size, min(32, (page + 1) * size)):
                offset = index - page * size
                row = top + 2 + offset % rows
                col = left + 2 + (offset // rows) * (layout['cell'] + 2)
                label = f'x{index:<2} {RV32Decoder.REG_NAMES[index]:>4} {values[index]:>{value_width}}'
                attr = curses.color_pair(self.C_HIGH) | curses.A_BOLD if index in recent else curses.A_NORMAL
                if index == 0:
                    attr = curses.A_DIM
                self.stdscr.addstr(row, col, label, attr)
            put(info_row, f"PC=0x{current['pc']:08x}" if current else 'PC=--------', curses.color_pair(self.C_VAL))
            put(info_row + 1, current['decoded']['asm'] if current else 'No retired instruction',
                curses.color_pair(self.C_MENUKEY))
        else:
            for offset, line in enumerate(textwrap.wrap(message, max(1, room))[:rows + 2]):
                put(top + 1 + offset, line, curses.A_DIM)
        hint = f'{key}/Esc close'
        if pages > 1:
            hint = f'{key} close · PgUp/PgDn · {page + 1}/{pages}'
            if text_width(hint) > room:
                hint = f'PgUp/Dn {page + 1}/{pages} · {key}'
        put(info_row + 2, hint, header)
