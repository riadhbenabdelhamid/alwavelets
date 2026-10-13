"""Marker editing and a paged list shared by keyboard and mouse users."""
import curses

from .marker_editing import delete_markers, rename_marker
from .ui_chrome import elide, menu_pages, text_width
from .ui_navigation import set_active_cursor_visible


_CONTROLS = [('Enter', 'Jump', curses.KEY_ENTER), ('r', 'Rename', ord('r')),
             ('Del', 'Delete', curses.KEY_DC), ('m', 'Add', ord('m')),
             ('M', 'Clear all', ord('M')), ('u', 'Undo', ord('u')),
             ('Ctrl-R', 'Redo', 18), ('Esc', 'Close', 27)]


class MarkerManagerMixin:
    def open_marker_manager(self):
        selected = min(self.markers, key=lambda marker: abs(marker.time - self.cursor_time), default=None)
        self._marker_menu = {'selected': selected, 'index': 0, 'scroll': 0}
        self._signal_menu = None
        self._drag_anchor = self._drag_cur = None
        self._pending_g = False
        self._dirty = True

    def _marker_items(self):
        # Sorting is a view of the list: duplicate times/labels stay separate
        # and browsing never changes capture order or editing history.
        items = sorted(self.markers, key=lambda marker: marker.time)
        menu = self._marker_menu
        selected = menu['selected']
        if selected in items:
            menu['index'] = items.index(selected)
        else:
            menu['index'] = min(menu['index'], max(0, len(items) - 1))
        menu['selected'] = items[menu['index']] if items else None
        return items

    def marker_manager_handle_key(self, key):
        self._dirty = True
        menu = self._marker_menu
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return
            rect = menu.get('rect')
            if rect is None:
                return
            left, top, width, height = rect
            click = state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON1_DOUBLE_CLICKED)
            if not (left <= x < left + width and top <= y < top + height):
                if click:
                    self._marker_menu = None
                return  # A dismissing click must not move the waveform cursor.
            if state & curses.BUTTON4_PRESSED:
                key = curses.KEY_UP
            elif state & (getattr(curses, 'BUTTON5_PRESSED', 0) | (1 << 21)):
                key = curses.KEY_DOWN
            elif click:
                for row, x0, x1, action in menu.get('buttons', ()):
                    if y == row and x0 <= x < x1:
                        self.marker_manager_handle_key(action)
                        return
                if top + 2 <= y < top + 2 + menu['rows']:
                    items = self._marker_items()
                    index = menu['scroll'] + y - top - 2
                    if index < len(items):
                        menu['index'], menu['selected'] = index, items[index]
                        if state & curses.BUTTON1_DOUBLE_CLICKED:
                            self.marker_manager_handle_key(curses.KEY_ENTER)
                return
            else:
                return

        items = self._marker_items()
        marker = menu['selected']
        if key in (27, ord('q'), curses.KEY_F12):
            self._marker_menu = None
        elif key in (10, 13, curses.KEY_ENTER) and marker is not None:
            self.cursor_time = marker.time
            set_active_cursor_visible(self)
            self._ensure_cursor_visible()
            self._marker_menu = None
        elif key == ord('r') and marker is not None:
            label = self.prompt_input('Marker label: ', initial=marker.label, cancel_value=None)
            if label is not None:
                rename_marker(self, marker, label)
        elif key in (curses.KEY_DC, ord('d')) and marker is not None:
            delete_markers(self, [marker])
        elif key == ord('M'):
            delete_markers(self, self.markers)
        elif key == ord('m'):
            added = self.place_marker()
            if added is not None:
                menu['selected'] = added
        elif key == ord('u'):
            self.undo()
        elif key == 18:
            self.redo()
        elif items and key in (curses.KEY_UP, curses.KEY_DOWN, ord('j'), ord('k'),
                              curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            step = max(1, menu.get('rows', 1)) if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            index = menu['index'] + (-step if key in (curses.KEY_UP, ord('k'), curses.KEY_PPAGE) else step)
            if key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(items) - 1
            menu['index'] = max(0, min(len(items) - 1, index))
            menu['selected'] = items[menu['index']]

    def draw_marker_manager(self):
        menu = self._marker_menu
        menu['buttons'] = []
        if self.width < 12 or self.height < 6:
            menu['rect'] = None
            return
        items = self._marker_items()
        width = min(84, self.width - 2)
        choices = ([(key, label) for key, label, _ in _CONTROLS] if width >= 50 else
                   [('Enter', 'Go'), ('r', 'Name'), ('Del', 'Del'), ('m', 'Add'),
                    ('M', 'All'), ('u', 'Undo'), ('^R', 'Redo'), ('Esc', 'Close')])
        controls = menu_pages(choices, width - 4, gap=2)
        height = min(self.height - 2, max(1, len(items)) + 3 + len(controls))
        rows = max(0, height - 3 - len(controls))
        left, top = (self.width - width) // 2, (self.height - height) // 2
        menu['rect'], menu['rows'] = (left, top, width, height), rows
        menu['scroll'] = max(0, min(menu['scroll'], menu['index']))
        menu['scroll'] = max(menu['scroll'], menu['index'] - max(1, rows) + 1)
        normal = curses.color_pair(self.C_HELP)
        selected = curses.color_pair(self.C_SELECT) | curses.A_BOLD

        def put(row, text, attr=normal):
            if not 0 <= row < height:
                return
            text = elide(text, width - 4)
            try:
                self.stdscr.addstr(top + row, left, '│ ' + text + ' ' * (width - 3 - text_width(text)) + '│', attr)
            except curses.error:
                pass

        for row in range(height):
            put(row, '')
        title = f'Markers · {menu["index"] + 1 if items else 0}/{len(items)}'
        try:
            label = elide(' ' + title + ' ', width - 2)
            self.stdscr.addstr(top, left, '┌' + label + '─' * (width - 2 - text_width(label)) + '┐', normal | curses.A_BOLD)
            self.stdscr.addstr(top + height - 1, left, '└' + '─' * (width - 2) + '┘', normal)
        except curses.error:
            pass
        if rows < 1:
            put(1, 'Enlarge terminal', curses.A_DIM)
            put(2, 'Esc Close', curses.A_DIM)
            return
        visible = items[menu['scroll']:menu['scroll'] + rows]
        times = [self.format_ruler_time(marker.time) for marker in visible]
        time_width = min(max(4, max(map(text_width, times), default=4)), max(4, (width - 6) // 2))
        put(1, 'Time'.ljust(time_width) + '  Label', normal | curses.A_BOLD)
        for offset, (marker, time) in enumerate(zip(visible, times), 2):
            time = elide(time, time_width)
            text = time + ' ' * (time_width - text_width(time)) + '  ' + (marker.label or '(unlabeled)')
            put(offset, text, selected if marker is menu['selected'] else normal)
        if not items:
            put(2, 'No markers · m Add', curses.A_DIM)
        actions = {key: action for key, _, action in _CONTROLS}
        actions['^R'] = 18
        for offset, page in enumerate(controls, 2 + rows):
            col = left + 2
            for key, label in page:
                text = elide(key + ' ' + label, max(0, left + width - 2 - col))
                try:
                    self.stdscr.addstr(top + offset, col, text, normal | curses.A_BOLD)
                except curses.error:
                    pass
                menu['buttons'].append((top + offset, col, col + text_width(text), actions[key]))
                col += text_width(text) + 2
