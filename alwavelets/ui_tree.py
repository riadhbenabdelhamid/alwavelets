"""Scope-tree browser and split-panel UI for Alwavelets: the full-screen tree
browser and the side panel for selecting signals/scopes."""
import curses

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .hierarchy import scope_signal_ids, scope_all_selected, scope_ranges
from .display_order import DisplayOrder


class TreePanelMixin:
    def draw_tree_browser(self):
        """Draw full-screen scope tree browser overlay."""
        self.stdscr.erase()
        h, w = self.height, self.width

        # Title bar
        title = " ◆ Signal Browser — ↑↓ Navigate · → Expand · ← Collapse · Space Toggle · Enter Select Scope · B Close"
        try:
            self.stdscr.addstr(0, 0, title[:w-1].ljust(w-1), curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        except curses.error:
            pass

        # Tree entries
        vis = h - 2  # rows for tree content
        if self.tree_scroll > self.tree_cursor:
            self.tree_scroll = self.tree_cursor
        if self.tree_cursor >= self.tree_scroll + vis:
            self.tree_scroll = self.tree_cursor - vis + 1

        for i in range(vis):
            idx = self.tree_scroll + i
            row = 1 + i
            if idx >= len(self._tree_flat):
                break

            kind, item, depth, flag, extra = self._tree_flat[idx]
            is_cursor = (idx == self.tree_cursor)

            indent = "  " * depth
            if kind == 'scope':
                icon = "▼" if flag else "▶"
                scope_checked = self._tree_scope_all_checked(item)
                scope_mark = "✓" if scope_checked else " "
                label = f"{indent}{icon}{scope_mark}{item.name}/ [{extra}]"
                attr = curses.color_pair(self.C_MENUKEY) | curses.A_BOLD if is_cursor else curses.color_pair(self.C_SCOPE)
            else:
                check = "☑ " if flag else "☐ "
                sig = item
                type_ch = '~' if sig.analog_mode else ('═' if sig.size > 1 else '─')
                role = self.signal_tags.get_role(sig.full_name) if hasattr(self, 'signal_tags') else None
                role_str = f" «{role}»" if role else ""
                label = f"{indent}  {check}{type_ch} {sig.name} [{sig.size}]{role_str}"
                if flag:
                    attr = curses.color_pair(self.C_HIGH) if not is_cursor else curses.color_pair(self.C_SELECT) | curses.A_BOLD
                else:
                    attr = curses.A_NORMAL if not is_cursor else curses.color_pair(self.C_FOCUS) | curses.A_BOLD

            # Alt row bg
            if i % 2 == 1 and not is_cursor:
                attr |= curses.A_DIM

            try:
                line = label[:w-1].ljust(w-1)
                self.stdscr.addstr(row, 0, line, attr)
            except curses.error:
                pass

        # Status bar
        total_in_view = len(self.display_order)
        total_available = self._scope_index.counts[self.scope_tree]
        status = f" {total_in_view}/{total_available} signals in view │ {len(self._tree_flat)} tree entries"
        try:
            self.stdscr.addstr(h-1, 0, status[:w-1].ljust(w-1), curses.color_pair(self.C_HEADER))
        except curses.error:
            pass

    def tree_handle_key(self, key):
        """Handle key in tree browser mode. Returns True to stay in tree, False to exit."""
        if key == ord('B') or key == 27 or key == ord('q'):
            return False

        # Tag signal with ISA role
        if key == ord('t'):
            self._tag_signal_at_cursor()
            return True
        if key == ord('T'):
            self._load_tag_file()
            return True

        n = len(self._tree_flat)
        if n == 0:
            # If tree is empty, expand root
            for name in self.scope_tree.children:
                self.tree_expanded.add(self.scope_tree.children[name].full_path)
            self._rebuild_tree()
            return True

        if key == curses.KEY_DOWN or key == ord('j'):
            self.tree_cursor = min(self.tree_cursor + 1, n - 1)
        elif key == curses.KEY_UP or key == ord('k'):
            self.tree_cursor = max(self.tree_cursor - 1, 0)
        elif key == curses.KEY_NPAGE:
            self.tree_cursor = min(self.tree_cursor + self.height - 3, n - 1)
        elif key == curses.KEY_PPAGE:
            self.tree_cursor = max(self.tree_cursor - (self.height - 3), 0)
        elif key == curses.KEY_HOME:
            self.tree_cursor = 0
        elif key == curses.KEY_END:
            self.tree_cursor = n - 1

        elif key == curses.KEY_RIGHT or key == ord('l'):
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope' and item.full_path not in self.tree_expanded:
                    self.tree_expanded.add(item.full_path)
                    self._rebuild_tree()

        elif key == curses.KEY_LEFT or key == ord('h'):
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope' and item.full_path in self.tree_expanded:
                    self.tree_expanded.discard(item.full_path)
                    self._rebuild_tree()

        elif key == ord(' '):
            if self.tree_cursor < n:
                kind, item, _, flag, extra = self._tree_flat[self.tree_cursor]
                if kind == 'signal':
                    sid = extra
                    if sid in self._display_order_set:
                        self.display_order.remove(sid)
                        self._display_order_set.discard(sid)
                    else:
                        self.display_order.append(sid)
                        self._display_order_set.add(sid)
                elif kind == 'scope':
                    # Toggle all signals in scope (and nested)
                    if self._tree_scope_all_checked(item):
                        self._tree_remove_scope_signals(item)
                    else:
                        self._tree_add_scope_signals(item)
                self._invalidate_scope_cache()

        elif key == 10 or key == curses.KEY_ENTER:  # Enter
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope':
                    # Expand scope and select it
                    if item.full_path not in self.tree_expanded:
                        self.tree_expanded.add(item.full_path)
                    self.panel_selected_scope = item.full_path
                    self._rebuild_tree()

        return True

    def _tree_add_scope_signals(self, scope_node):
        """Add all signals in scope (and subscopes) to display."""
        if isinstance(self.display_order, DisplayOrder):
            order = self.display_order
            order.add_ranges(scope_ranges(scope_node, order.catalog))
            self._scope_check_cache = {}
            return
        self._sync_display_set()
        present = self._display_order_set
        for sid in self._scope_signal_ids(scope_node):
            if sid not in present:
                self.display_order.append(sid)
                present.add(sid)
        self._scope_check_cache = {}

    def _tree_remove_scope_signals(self, scope_node):
        """Remove all signals in scope (and subscopes) from display."""
        if isinstance(self.display_order, DisplayOrder):
            order = self.display_order
            order.remove_ranges(scope_ranges(scope_node, order.catalog, ordered=False))
            self._scope_check_cache = {}
            return
        drop = set(self._scope_signal_ids(scope_node))
        if drop:
            self.display_order = [s for s in self.display_order if s not in drop]
            self._sync_display_set()

    @staticmethod
    def _scope_signal_ids(scope_node):
        """Every signal id under a scope node, depth first (one pass, so the
        add/remove above are O(n) instead of a list scan per signal)."""
        return scope_signal_ids(scope_node)

    def _tree_scope_all_checked(self, scope_node):
        """Check if all signals in scope (and subscopes) are in display. Cached."""
        if isinstance(self.display_order, DisplayOrder):
            order = self.display_order
            return order.contains_ranges(scope_ranges(scope_node, order.catalog, ordered=False))
        return scope_all_selected(scope_node, self._display_order_set, self._scope_check_cache)

    # ── Bus Expansion ──

    def draw_panel_tree(self):
        """Draw the tree panel on the left side."""
        tw = self.panel_tree_width
        h = self.height
        has_focus = (self.panel_focus == 0)

        # Use cached tree (rebuilt on toggle, not on draw)

        # ── Tree panel header ──
        hdr = " Modules" if has_focus else " Modules"
        hdr_attr = curses.color_pair(self.C_HEADER) | (curses.A_BOLD if has_focus else curses.A_DIM)
        # Anchor the panel to the waveform view's rows so it sits below the
        # title/menu/separator header instead of covering the help bar.
        top = self.ruler_row          # panel header aligns with the ruler row
        try:
            self.stdscr.addstr(top, 0, hdr[:tw].ljust(tw), hdr_attr)
        except curses.error:
            pass

        # ── Tree entries (top portion: from wave_top to ~h//2) ──
        tree_rows = max(1, h // 2 - self.wave_top)
        if self.tree_scroll > self.tree_cursor:
            self.tree_scroll = self.tree_cursor
        if self.tree_cursor >= self.tree_scroll + tree_rows:
            self.tree_scroll = self.tree_cursor - tree_rows + 1

        for i in range(tree_rows):
            idx = self.tree_scroll + i
            row = self.wave_top + i
            if row >= h - 1:
                break
            if idx >= len(self._tree_flat):
                # Clear remaining rows
                try:
                    self.stdscr.addstr(row, 0, ' ' * tw, curses.A_NORMAL)
                except curses.error:
                    pass
                continue

            kind, item, depth, flag, extra = self._tree_flat[idx]
            is_cursor = (idx == self.tree_cursor) and has_focus

            indent = " " * min(depth * 2, tw - 10)
            if kind == 'scope':
                icon = "▼" if flag else "▶"
                scope_checked = self._tree_scope_all_checked(item)
                scope_mark = "✓" if scope_checked else " "
                label = f"{indent}{icon}{scope_mark}{item.name}/"
                is_selected_scope = (self.panel_selected_scope and 
                                    item.full_path == self.panel_selected_scope)
                if is_cursor:
                    attr = curses.color_pair(self.C_SELECT) | curses.A_BOLD
                elif is_selected_scope:
                    attr = curses.color_pair(self.C_MENUKEY) | curses.A_BOLD
                else:
                    attr = curses.color_pair(self.C_SCOPE)
            else:
                sig = item
                check = "✓" if flag else " "
                label = f"{indent}  {check} {sig.name}"
                if is_cursor:
                    attr = curses.color_pair(self.C_SELECT) | curses.A_BOLD
                elif flag:
                    attr = curses.color_pair(self.C_HIGH)
                else:
                    attr = curses.A_DIM

            try:
                line = label[:tw-1].ljust(tw-1)
                self.stdscr.addstr(row, 0, line, attr)
                self.stdscr.addch(row, tw-1, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

        # ── Separator between tree and signal list ──
        sep_row = self.wave_top + tree_rows
        if sep_row < h - 1:
            sep_label = f"─ Signals "
            try:
                sep_line = sep_label[:tw-1].ljust(tw-1, '─')
                self.stdscr.addstr(sep_row, 0, sep_line, curses.color_pair(self.C_HEADER))
                self.stdscr.addch(sep_row, tw-1, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

        # ── Signal list (bottom portion: signals in selected scope) ──
        sig_start_row = sep_row + 1
        sig_rows = max(0, h - 2 - sig_start_row)

        # Get signals for selected scope
        scope_node = self._scope_index.nodes.get(self.panel_selected_scope)
        scope_sids = scope_node.signal_ids if scope_node is not None else ()

        for i in range(sig_rows):
            row = sig_start_row + i
            if row >= h - 1:
                break
            if i < len(scope_sids):
                sid = scope_sids[i]
                sig = self.get_signal(sid)
                in_view = sid in self._display_order_set
                check = "✓" if in_view else " "
                type_ch = '═' if sig.size > 1 else '─'
                label = f"  {check} {type_ch} {sig.name} [{sig.size}]"
                attr = curses.color_pair(self.C_HIGH) if in_view else curses.A_DIM
            else:
                label = ""
                attr = curses.A_NORMAL

            try:
                line = label[:tw-1].ljust(tw-1)
                self.stdscr.addstr(row, 0, line, attr)
                self.stdscr.addch(row, tw-1, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

        # ── Vertical separator ──
        for row in range(self.ruler_row, h - 1):
            try:
                self.stdscr.addch(row, tw - 1, '│', curses.color_pair(self.C_MENUSEP))
            except curses.error:
                pass

    def panel_handle_tree_key(self, key):
        """Handle key input when tree panel has focus in split mode."""
        n = len(self._tree_flat)
        if n == 0:
            for name in self.scope_tree.children:
                self.tree_expanded.add(self.scope_tree.children[name].full_path)
            self._rebuild_tree()
            return True

        if key not in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k'),
                       curses.KEY_RIGHT, ord('l'), curses.KEY_LEFT, ord('h'),
                       curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END,
                       ord(' '), 10, curses.KEY_ENTER):
            return False

        if key == curses.KEY_DOWN or key == ord('j'):
            self.tree_cursor = min(self.tree_cursor + 1, n - 1)
            self._panel_update_selected_scope()
        elif key == curses.KEY_UP or key == ord('k'):
            self.tree_cursor = max(self.tree_cursor - 1, 0)
            self._panel_update_selected_scope()
        elif key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
            page = max(1, self.height // 2 - self.wave_top)
            step = page if key == curses.KEY_NPAGE else -page
            self.tree_cursor = max(0, min(self.tree_cursor + step, n - 1))
            self._panel_update_selected_scope()
        elif key in (curses.KEY_HOME, curses.KEY_END):
            self.tree_cursor = 0 if key == curses.KEY_HOME else n - 1
            self._panel_update_selected_scope()

        elif key == curses.KEY_RIGHT or key == ord('l'):
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope' and item.full_path not in self.tree_expanded:
                    self.tree_expanded.add(item.full_path)
                    self._rebuild_tree()

        elif key == curses.KEY_LEFT or key == ord('h'):
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope' and item.full_path in self.tree_expanded:
                    self.tree_expanded.discard(item.full_path)
                    self._rebuild_tree()

        elif key == ord(' '):
            # Toggle signal or entire scope in/out of display
            if self.tree_cursor < n:
                kind, item, _, flag, extra = self._tree_flat[self.tree_cursor]
                if kind == 'signal':
                    sid = extra
                    if sid in self._display_order_set:
                        self.display_order.remove(sid)
                        self._display_order_set.discard(sid)
                    else:
                        self.display_order.append(sid)
                        self._display_order_set.add(sid)
                elif kind == 'scope':
                    if self._tree_scope_all_checked(item):
                        self._tree_remove_scope_signals(item)
                    else:
                        self._tree_add_scope_signals(item)
                self._invalidate_scope_cache()

        elif key == 10 or key == curses.KEY_ENTER:
            # Expand scope and select it
            if self.tree_cursor < n:
                kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
                if kind == 'scope':
                    if item.full_path not in self.tree_expanded:
                        self.tree_expanded.add(item.full_path)
                    self.panel_selected_scope = item.full_path
                    self._rebuild_tree()
        return True

    def _panel_update_selected_scope(self):
        """Update selected scope based on tree cursor position."""
        if self.tree_cursor < len(self._tree_flat):
            kind, item, _, _, _ = self._tree_flat[self.tree_cursor]
            if kind == 'scope':
                self.panel_selected_scope = item.full_path
            elif kind == 'signal':
                sig = item
                self.panel_selected_scope = sig.scope
