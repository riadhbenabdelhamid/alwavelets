"""Responsive register snapshots, kept separate from waveform drawing."""
import curses
import math
import textwrap

from .background import BackgroundQueries
from .decoders import RV32Decoder
from .instruction_store import InstructionResults
from .ui_actions import shortcut_for
from .ui_chrome import elide, menu_pages, text_width, wave_bottom


REGISTER_POSITIONS = ('right', 'bottom', 'top', 'left', 'overlay')
PLACEMENT_KEYS = [('3', 'Right'), ('4', 'Bottom'), ('5', 'Top'), ('6', 'Left'), ('7', 'Overlay')]


def register_position(viewer):
    position = getattr(viewer, 'register_panel_position', 'right')
    return position if position in REGISTER_POSITIONS else 'right'


def register_layout(viewer, value_width=8):
    """Allocate a stable pane, then fit complete values and paged rows inside it."""
    top, bottom = getattr(viewer, '_register_area',
                          (max(1, getattr(viewer, 'ruler_row', 2)), wave_bottom(viewer)))
    available = max(0, bottom - top)
    position = register_position(viewer)
    if position in ('left', 'right'):
        # Leave room for signal names and a usable timeline, even when narrow.
        remaining = min(28, max(8, viewer.width // 3))
        width = max(0, min(40, viewer.width - remaining))
        left = 0 if position == 'left' else viewer.width - width
    elif position in ('top', 'bottom'):
        left, width = 0, viewer.width
    else:
        columns = 2 if available >= 24 else 4
        width = max(0, min(viewer.width - 2, columns * (11 + value_width) + 2))
        left = max(0, viewer.width - width - 1)
    choices = PLACEMENT_KEYS if width >= 30 else list(zip('34567', ('→', '↓', '↑', '←', 'Overlay')))
    commands = menu_pages(choices, max(1, width - 4), gap=2)
    overhead = 6 + len(commands)
    base_columns = max(1, min(8, (width - 2) // 19))
    needed = math.ceil(32 / base_columns) + overhead
    if position in ('top', 'bottom'):
        height = min(needed, max(0, available - 3), max(10, available // 2))
        if position == 'bottom':
            top = bottom - height
    elif position == 'overlay':
        height = min(available, needed)
    else:
        height = available
    cell = 9 + value_width  # xNN, ABI name, and the complete hexadecimal value
    columns = max(1, min(8, (width - 2) // (cell + 2)))
    rows = max(0, min(math.ceil(32 / columns), height - overhead))
    return {'left': left, 'top': top, 'width': width, 'height': height,
            'columns': columns, 'rows': rows, 'cell': cell,
            'commands': commands, 'grid_offset': 2 + len(commands),
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
    def set_register_position(self, position):
        if position not in REGISTER_POSITIONS:
            return False
        self.register_panel_position = position
        self._register_panel_page = 0
        self._register_panel_buttons = []
        self._drag_anchor = self._drag_cur = None
        self._pending_g = False
        self.update_layout()
        self._needs_layout = self._dirty = True
        return True

    def close_register_panel(self):
        self.show_reg_panel = False
        self._register_panel_layout = None
        self._pending_g = False
        queries = getattr(self, '_queries', None)
        if queries is not None:
            queries.invalidate('registers')
        self.update_layout()
        self._needs_layout = self._dirty = True

    def page_register_panel(self, step):
        self._pending_g = False
        layout = getattr(self, '_register_panel_layout', None) or register_layout(self)
        size = max(1, layout['rows'] * layout['columns'])
        pages = math.ceil(32 / size)
        self._register_panel_page = min(pages - 1, max(0, getattr(self, '_register_panel_page', 0) + step))
        self._dirty = True

    def register_panel_handle_key(self, key):
        if ord('3') <= key <= ord('7'):
            self.set_register_position(REGISTER_POSITIONS[key - ord('3')])
        elif key == 27:
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
        if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            for row, x0, x1, position in getattr(self, '_register_panel_buttons', ()):
                if y == row and x0 <= x < x1:
                    self.set_register_position(position)
                    break
        elif state & curses.BUTTON4_PRESSED:
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
        self._register_panel_buttons = []
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
        put(top, 'Registers · ' + register_position(self) + ' · hex', header | curses.A_BOLD)
        for offset, commands in enumerate(layout['commands'], 1):
            row, col = top + offset, left + 2
            if row >= top + height - 1:
                break
            for shortcut, label in commands:
                position = REGISTER_POSITIONS[int(shortcut) - 3]
                text = elide(shortcut + ' ' + label, max(0, left + width - 2 - col))
                attr = header | (curses.A_REVERSE if position == register_position(self) else curses.A_BOLD)
                self.stdscr.addstr(row, col, text, attr)
                self._register_panel_buttons.append((row, col, col + text_width(text), position))
                col += text_width(text) + 2
        if not layout['fits']:
            if height > len(layout['commands']) + 2:
                put(top + len(layout['commands']) + 1, 'Enlarge terminal', curses.A_DIM)
            return
        rows = layout['rows']
        size = rows * layout['columns']
        pages = math.ceil(32 / size)
        page = self._register_panel_page = min(pages - 1, max(0, getattr(self, '_register_panel_page', 0)))
        grid_top = top + layout['grid_offset']
        info_row = top + height - 4
        if state is not None:
            hart, _, recent, current = state
            context = f'Hart {hart}' if current is not None else 'No instruction at cursor'
            put(grid_top - 1, context + f' · t={self.cursor_time}', header)
            for index in range(page * size, min(32, (page + 1) * size)):
                offset = index - page * size
                row = grid_top + offset % rows
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
            for offset, line in enumerate(textwrap.wrap(message, max(1, room))[:rows + 3]):
                put(grid_top - 1 + offset, line, curses.A_DIM)
        hint = f'{key}/Esc close'
        if pages > 1:
            hint = f'{key} close · PgUp/PgDn · {page + 1}/{pages}'
            if text_width(hint) > room:
                hint = f'PgUp/Dn {page + 1}/{pages} · {key}'
        put(info_row + 2, hint, header)
