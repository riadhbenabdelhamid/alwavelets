"""Small signal action popup, shared by mouse and keyboard users."""
import curses

from .core import Radix, IEEE754_FORMATS
from .custom_radix import CustomFixed, CustomFloat
from .ui_chrome import elide, signal_origin, text_width
from .ui_editing import apply_format, format_targets, remove_rows


class SignalMenuMixin:
    def open_signal_menu(self, x=None, y=None):
        targets = format_targets(self)
        if not targets:
            self._show_message('Focus or select a signal first')
            return
        sig = self.get_signal(targets[0]) if len(targets) == 1 else None
        title = sig.full_name if sig is not None else f'{len(targets)} selected signals'
        self._signal_menu = {
            'targets': targets, 'title': title, 'page': '', 'index': 0, 'scroll': 0,
            'root_index': 0, 'bus': bool(sig and sig.size > 1),
            'x': signal_origin(self) if x is None else x,
            'y': min((row for row, index in getattr(self, '_signal_hit_rows', {}).items()
                      if index == self.focus_idx), default=self.wave_top + self.focus_idx) if y is None else y,
        }
        self._drag_anchor = self._drag_cur = None
        self._pending_g = False
        self.panel_focus = 1
        self._dirty = True

    def signal_menu_items(self):
        menu = self._signal_menu
        page = menu['page']
        if not page:
            items = [('Number format  ▸', 'page', 'radix'),
                     ('Color          ▸', 'page', 'color'),
                     ('Height (analog) ▸', 'page', 'height')]
            if menu['bus']:
                expanded = menu['targets'][0] in self.expanded_buses
                items.append(('Collapse bus' if expanded else 'Expand bus', 'bus', None))
            if len(menu['targets']) == 1:
                sid = menu['targets'][0]
                sig = self.get_signal(sid)
                fmt = self.get_signal_radix(sid)
                if (sig is not None and sig.signal_type not in ('real', 'realtime')
                        and (isinstance(fmt, (CustomFixed, CustomFloat)) or fmt in IEEE754_FORMATS)):
                    items.append(('Collapse components' if sid in self.expanded_buses else
                                  'Expand components', 'components', None))
            items += [('Add to group…', 'group', None), ('Remove from view', 'remove', None)]
            return items
        if page == 'radix':
            choices = [('Default (global)', None), ('Binary', Radix.BIN),
                       ('Hexadecimal', Radix.HEX), ('Signed decimal', Radix.DEC),
                       ('Unsigned decimal', Radix.UNSIGNED), ('Float 16', Radix.FLOAT16),
                       ('BF16 (bfloat16)', Radix.BFLOAT16),
                       ('Float 32', Radix.FLOAT32), ('Float 64', Radix.FLOAT64),
                       ('Custom fixed: ' + self.custom_radixes['fixed'].description, Radix.CUSTOM_FIXED),
                       ('Custom float: ' + self.custom_radixes['float'].description, Radix.CUSTOM_FLOAT)]
        elif page == 'color':
            choices = [('Default', None)] + [(f'Color {n}', n) for n in self.USER_COLORS]
        else:
            choices = [('Default (1 row)', None)] + [(f'{n} row' + ('s' if n > 1 else ''), n)
                                                      for n in range(1, 5)]
        mapping = getattr(self, 'sig_' + page)
        values = {mapping.get(sid) for sid in menu['targets']}
        items = [('✓ ' + label if values == {value} else '  ' + label, page, value)
                 for label, value in choices]
        if page == 'radix':
            items += [('Configure custom fixed…', 'configure', 'fixed'),
                      ('Configure custom float…', 'configure', 'float')]
        return items

    def _run_signal_menu_item(self):
        menu = self._signal_menu
        _, action, value = self.signal_menu_items()[menu['index']]
        if action == 'page':
            menu['root_index'] = menu['index']
            menu.update(page=value, index=0, scroll=0)
            return
        if action == 'configure':
            self.configure_custom_radix(value)
            return
        self._signal_menu = None
        targets = [sid for sid in menu['targets'] if sid in self._display_order_set]
        if not targets:
            self._show_message('These signals are no longer displayed')
            return
        if action in ('radix', 'color', 'height'):
            apply_format(self, action, value, targets)
        elif action == 'remove':
            remove_rows(self, targets)
        elif action == 'bus':
            self.expand_bus(targets[0])
            self._sync_display_set()
            self._rebuild_view_rows()
            self._needs_layout = True
        elif action == 'components':
            self.expand_format_fields(targets[0])
        elif action == 'group':
            name = self.prompt_input('Add to group (name or parent/name): ')
            if not name or not name.strip():
                return
            name = name.strip()
            parent, _, leaf = name.rpartition('/')
            leaf = leaf.strip()
            if not leaf:
                self._show_message('Give a group name')
                return
            # This action adds members; the command also supports replacing
            # an entire group. Preserve existing members in this popup.
            members = set(targets) | self._group_by_name.get(leaf, {}).get('members', set())
            restore = self._undo_groups_snapshot()
            if self.create_group(leaf, members, parent.strip() or None):
                self.push_undo(f"add to group '{leaf}'", restore)
                self._needs_layout = True
                self._show_message(f'Added {len(targets)} signal(s) to {leaf}')
            else:
                self._show_message('Cannot nest a group inside itself')

    def signal_menu_handle_key(self, key):
        menu = self._signal_menu
        items = self.signal_menu_items()
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
            except curses.error:
                return
            rect = menu.get('rect')
            if rect is None:
                return
            left, top, width, height = rect
            inside = left <= x < left + width and top <= y < top + height
            click = state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED | curses.BUTTON3_CLICKED)
            if not inside:
                if click:
                    self._signal_menu = None
                return  # Never pass the dismissing click into the waveform.
            if state & curses.BUTTON4_PRESSED:
                key = curses.KEY_UP
            elif state & getattr(curses, 'BUTTON5_PRESSED', 1 << 21):
                key = curses.KEY_DOWN
            elif 0 < y - top < height - 2 and left < x < left + width - 1:
                index = menu['scroll'] + y - top - 1
                if index < len(items) and state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    menu['index'] = index
                    self._run_signal_menu_item()
                return
            else:
                return
        if key in (27, curses.KEY_LEFT):
            if menu['page']:
                menu.update(page='', index=menu['root_index'], scroll=0)
            else:
                self._signal_menu = None
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT):
            if key != curses.KEY_RIGHT or items[menu['index']][1] == 'page':
                self._run_signal_menu_item()
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_HOME, curses.KEY_END,
                     curses.KEY_PPAGE, curses.KEY_NPAGE):
            step = max(1, self.height - 3) if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            index = menu['index'] + (-step if key in (curses.KEY_UP, curses.KEY_PPAGE) else step)
            if key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(items) - 1
            menu['index'] = max(0, min(len(items) - 1, index))

    def draw_signal_menu(self):
        menu = self._signal_menu
        items = self.signal_menu_items()
        if self.height < 4 or self.width < 8:
            menu['rect'] = None
            return
        title = (menu['page'].capitalize() + ' · ' if menu['page'] else '') + menu['title']
        width = min(self.width, max(30, min(60, text_width(title) + 4),
                                    max(text_width(item[0]) for item in items) + 4))
        height = min(self.height, len(items) + 3)
        x = max(0, min(menu['x'], self.width - width))
        y = max(0, min(menu['y'], self.height - height))
        menu['rect'] = x, y, width, height
        visible = height - 3
        menu['scroll'] = max(0, min(menu['scroll'], menu['index']))
        menu['scroll'] = max(menu['scroll'], menu['index'] - visible + 1)
        normal = curses.color_pair(self.C_HELP)
        selected = curses.color_pair(self.C_SELECT) | curses.A_BOLD

        def put(row, label, attr=normal):
            label = elide(label, width - 2)
            try:
                self.stdscr.addstr(y + row, x, '│' + label + ' ' * (width - 2 - text_width(label)) + '│', attr)
            except curses.error:
                pass

        heading = elide(' ' + title + ' ', width - 2)
        try:
            self.stdscr.addstr(y, x, '┌' + heading + '─' * (width - 2 - text_width(heading)) + '┐', normal | curses.A_BOLD)
        except curses.error:
            pass
        for row, index in enumerate(range(menu['scroll'], min(len(items), menu['scroll'] + visible)), 1):
            put(row, ' ' + items[index][0], selected if index == menu['index'] else normal)
            if menu['page'] == 'color' and items[index][2] is not None and width >= 20:
                try:
                    self.stdscr.addstr(y + row, x + width - 5, '━━', curses.color_pair(items[index][2]) | curses.A_BOLD)
                except curses.error:
                    pass
        footer = ' ↑↓ Choose · Enter · Esc Back' if width >= 32 else ' ↑↓ Enter Esc' if width >= 16 else ' Esc'
        put(height - 2, footer, normal | curses.A_DIM)
        try:
            self.stdscr.addstr(y + height - 1, x, '└' + '─' * (width - 2) + '┘', normal)
        except curses.error:
            pass
