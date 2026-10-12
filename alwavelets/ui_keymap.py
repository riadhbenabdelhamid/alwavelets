"""Keybinding action handlers and the keymap table for Alwavelets. Each _kb_*
is a no-arg handler; _build_keymap maps keycodes to them (see Alwavelets.run)."""
import curses

from .core import *  # noqa: F401,F403
from .rendering import *  # noqa: F401,F403
from .rendering import _update_wave_chars  # noqa: F401
from .decoders import *  # noqa: F401,F403
from .parsers import *  # noqa: F401,F403
from .analysis import *  # noqa: F401,F403
from .persistence import *  # noqa: F401,F403
from .tcl import TclEngine  # noqa: F401
from .manual import AlwaveletsManual  # noqa: F401



class KeymapMixin:
    def _kb_quit(self):
        return 'QUIT'

    def _kb_escape(self):
        """Esc cancels — a pending g- prefix, visual mode, then the
        selection. It never quits: terminals deliver a bare Esc for unknown
        Alt/function-key sequences, and users press it to back out."""
        if self._pending_g:
            self._pending_g = False
            self._show_message("g- cancelled")
        elif getattr(self, 'panel_mode', False) and self.panel_focus == 0:
            self.panel_focus = 1
            self.menu_page = 0
        elif self._visual_mode:
            self._kb_visual_mode()
        elif self.selected_signals:
            self.selected_signals = set()
            self._show_message("Selection cleared")

    def _kb_help(self):
        if self.show_help:
            self.show_help = False
            self.show_manual = True
        else:
            self.show_help = True

    def _kb_show_manual(self):
        self.show_manual = True

    def _kb_toggle_menu_bar(self):
        self.show_menu_bar = not self.show_menu_bar
        self._needs_layout = True
        self._show_message("Menu bar " + ("shown" if self.show_menu_bar else "hidden"))

    def _kb_cycle_workmode(self):
        modes = list(self.WORKMODES)
        current = getattr(self, 'workmode', 'classic')
        index = modes.index(current) if current in modes else -1
        self.apply_workmode(modes[(index + 1) % len(modes)])

    def _kb_show_colors(self):
        self.show_colors = True

    def _kb_cursor_right(self):
        if self.cursor_on:
            self.move_cursor(1)
        else:
            self.scroll_time(1)

    def _kb_cursor_left(self):
        if self.cursor_on:
            self.move_cursor(-1)
        else:
            self.scroll_time(-1)

    def _kb_signal_down(self):
        nrows = len(self.view_rows)
        if self.focus_idx < self.visible_signals - 1 and \
           self.sig_offset + self.focus_idx < nrows - 1:
            self.focus_idx += 1
        elif self.sig_offset + self.visible_signals < nrows:
            self.sig_offset += 1
        if self._visual_mode:
            self._update_visual_selection()

    def _kb_signal_up(self):
        if self.focus_idx > 0:
            self.focus_idx -= 1
        elif self.sig_offset > 0:
            self.sig_offset -= 1
        if self._visual_mode:
            self._update_visual_selection()

    def _kb_zoom_in(self):
        self.zoom(0.5)

    def _kb_zoom_out(self):
        self.zoom(2.0)

    def _kb_home(self):
        span = self.time_end - self.time_start
        self.time_start = 0
        self.time_end = span
        self.cursor_a = 0

    def _kb_end(self):
        span = self.time_end - self.time_start
        self.time_end = self.vcd.max_time
        self.time_start = max(0, self.time_end - span)
        self.cursor_a = self.vcd.max_time

    def _kb_page_up(self):
        self.sig_offset = max(0, self.sig_offset - self.visible_signals)
        self.focus_idx = 0

    def _kb_page_down(self):
        self.sig_offset = min(
            max(0, len(self.view_rows) - self.visible_signals),
            self.sig_offset + self.visible_signals
        )
        self.focus_idx = 0

    def _kb_toggle_cursor(self):
        self.cursor_on = not self.cursor_on

    def _kb_cursor_a(self):
        self.active_cursor = 'a'
        self.cursor_on = True

    def _kb_cursor_b(self):
        self.active_cursor = 'b'
        self.cursor_b_on = True
        self.cursor_on = True

    def _kb_next_edge(self):
        self.jump_next_edge(any_visible=False)

    def _kb_prev_edge(self):
        self.jump_prev_edge(any_visible=False)

    def _kb_clear_markers(self):
        self.markers.clear()

    def _kb_toggle_full_names(self):
        self.show_full_names = not self.show_full_names

    def _kb_toggle_indent(self):
        self.indent_mode = not self.indent_mode
        self._show_message("Indent mode: " + ("on" if self.indent_mode else "off"))

    def _kb_toggle_panel(self):
        # No status message: the panel appearing/disappearing already makes the
        # state obvious, and it keeps the status row stable during quick toggles.
        self.panel_mode = not self.panel_mode
        self.panel_focus = 0 if self.panel_mode else 1
        self.menu_page = 0
        if self.panel_mode and not self._tree_flat:
            self._rebuild_tree()
        self._needs_layout = True

    def _kb_tab(self):
        if self.panel_mode:
            self.panel_focus = 1 - self.panel_focus
            self.menu_page = 0
            focus_name = "Tree" if self.panel_focus == 0 else "Waveform"
            self._show_message(f"Focus: {focus_name}")
        else:
            # Tab folds/unfolds a group header only. On a normal signal it does
            # nothing (folding its whole scope here would surprise-hide signals);
            # use Shift-←/→ or { } to fold scopes. No status message: the ▼/▶
            # arrow already shows the group's state.
            tok = self.focused_token()
            if self.is_group_header(tok):
                self.toggle_group_collapse(tok[1])

    def _kb_collapse_all(self):
        self.collapse_all_modules()
        self._show_message("All modules collapsed")

    def _kb_expand_all(self):
        self.expand_all_modules()
        self._show_message("All modules expanded")

    def _kb_toggle_time_units(self):
        self.show_time_units = not self.show_time_units

    def _kb_menu_next(self):
        self.menu_page += 1  # next page (wraps in draw_header)

    def _kb_menu_prev(self):
        self.menu_page -= 1  # prev page

    def _kb_toggle_grid(self):
        self.show_grid = (self.show_grid + 1) % 3
        grid_labels = {0: "Grid off", 1: "Grid: vertical", 2: "Grid: both"}
        self._show_message(grid_labels[self.show_grid])

    def _kb_line_thickness(self):
        self.line_thickness = (self.line_thickness + 1) % 3  # ultra-thin → thin → sub-cell
        # Buses/analog still use the WAVE_CHARS glyphs; in sub-cell mode keep them thin.
        _update_wave_chars(1 if self.line_thickness == 2 else self.line_thickness)
        width_names = {0: "Ultra-thin", 1: "Thin", 2: "Hi-res (sub-cell)"}
        self._show_message(f"Wave style: {width_names[self.line_thickness]}")

    def _kb_name_width_dec(self):
        self.name_width = max(8, self.name_width - 2)
        self._name_width_manual = True
        self.update_layout()

    def _kb_name_width_inc(self):
        self.name_width = min(self.width // 2, self.name_width + 2)
        self._name_width_manual = True
        self.update_layout()

    def _kb_radix_cycle(self):
        radixes = list(Radix)
        self.radix = radixes[(radixes.index(self.radix) + 1) % len(radixes)]

    def _kb_toggle_analog(self):
        sid = self.focused_signal_id()
        if sid:
            def _undo(sid=sid, was=(sid in self.analog_signals)):
                (self.analog_signals.add if was else self.analog_signals.discard)(sid)
            self.push_undo("analog toggle", _undo)
            if sid in self.analog_signals:
                self.analog_signals.discard(sid)
            else:
                self.analog_signals.add(sid)

    def _kb_toggle_values(self):
        self.show_values = not self.show_values

    def _kb_visual_mode(self):
        if self._visual_mode:
            self._visual_mode = False
            self._show_message("-- VISUAL --  off")
        else:
            self._visual_mode = True
            self._visual_anchor = self.sig_offset + self.focus_idx
            self.selected_signals.clear()
            sid = self.focused_signal_id()
            if sid:
                self.selected_signals.add(sid)
            self._show_message("-- VISUAL --")

    def _kb_toggle_highlight(self):
        sid = self.focused_signal_id()
        if sid:
            if sid in self.selected_signals:
                self.selected_signals.discard(sid)
            else:
                self.selected_signals.add(sid)

    def _kb_backspace_remove(self):
        if self.selected_signals:
            from .display_order import DisplayOrder, member_positions
            removed = (member_positions(self.display_order, self.selected_signals)
                       if isinstance(self.display_order, DisplayOrder) else
                       [(i, sid) for i, sid in enumerate(self.display_order) if sid in self.selected_signals])
            for i, _ in reversed(removed):
                self.display_order.pop(i)
            # Keep the finder's membership set in sync, or `/` will refuse to
            # re-add these signals (it treats them as already displayed).
            self._display_order_set.difference_update(s for _, s in removed)
            self._scope_check_cache = {}

            def _undo(removed=removed):
                for i, sid in removed:     # ascending: indices stay valid
                    if sid in self._display_order_set:
                        continue
                    self.display_order.insert(min(i, len(self.display_order)), sid)
                    self._display_order_set.add(sid)
            self.push_undo(f"remove {len(removed)} signals", _undo)
            self.selected_signals.clear()
            self._visual_mode = False
            self._show_message(f"Removed {len(removed)} signals (u to undo)")
        else:
            self.delete_signal()

    def _kb_label_size(self):
        self.value_label_size = (self.value_label_size + 1) % 3
        labels = {0: "Labels: hidden", 1: "Labels: compact", 2: "Labels: full"}
        self._show_message(labels[self.value_label_size])

    def _kb_signal_color(self):
        self.cycle_signal_color()
        c = self.sig_color.get(self.focused_signal_id(), 0)
        self._show_message(f"Color: {c}" if c else "Color: default")

    def _kb_signal_height(self):
        self.cycle_signal_height()
        h = self.sig_height.get(self.focused_signal_id(), 1)
        self._show_message(f"Height: {h}")

    def _kb_show_tree(self):
        self.show_tree = True

    def _kb_reg_panel(self):
        self.show_reg_panel = not self.show_reg_panel
        if self.show_reg_panel:
            if not self.instr_trace.built:
                self._build_trace()
            self._show_message("Register panel ON")
        else:
            self._show_message("Register panel OFF")

    def _kb_command_line(self):
        result = self.do_command_line()
        if result == 'QUIT':
            return 'QUIT'

    def _kb_move_signal_up(self):
        self.move_signal(-1)

    def _kb_move_signal_down(self):
        self.move_signal(1)

    def _kb_resize(self):
        self._needs_layout = True

    def _build_keymap(self):
        """Return the waveform-mode keycode → handler table.

        Single source of truth for key dispatch. Handlers take no arguments and
        may return 'QUIT' to exit. Modal keys (overlays and the vim 'g' prefix)
        are handled in run() before this table is consulted. Remap by editing
        the returned dict (e.g. self.keymap[ord('Z')] = self.zoom_to_selection).
        """
        K = curses
        return {
            ord('q'): self._kb_quit, 27: self._kb_escape,
            ord('?'): self._kb_help,
            K.KEY_F1: self._kb_show_manual,
            K.KEY_F2: self._kb_toggle_menu_bar,
            K.KEY_F3: self._kb_cycle_workmode,
            K.KEY_F4: self.open_action_palette,
            16: self.open_action_palette,  # Ctrl-P
            ord('C'): self._kb_show_colors,
            K.KEY_RIGHT: self._kb_cursor_right, ord('l'): self._kb_cursor_right,
            K.KEY_LEFT: self._kb_cursor_left, ord('h'): self._kb_cursor_left,
            K.KEY_DOWN: self._kb_signal_down, ord('j'): self._kb_signal_down,
            K.KEY_UP: self._kb_signal_up, ord('k'): self._kb_signal_up,
            K.KEY_SR: self._jump_prev_module,
            K.KEY_SF: self._jump_next_module,
            K.KEY_SLEFT: self._collapse_focused_scope,
            K.KEY_SRIGHT: self._expand_focused_scope,
            ord('K'): self._kb_move_signal_up,
            ord('J'): self._kb_move_signal_down,
            ord('+'): self._kb_zoom_in, ord('='): self._kb_zoom_in,
            ord('-'): self._kb_zoom_out,
            ord('Z'): self.zoom_fit_all,
            ord('W'): self.zoom_to_selection,
            K.KEY_HOME: self._kb_home,
            K.KEY_END: self._kb_end,
            K.KEY_PPAGE: self._kb_page_up,
            K.KEY_NPAGE: self._kb_page_down,
            ord('c'): self._kb_toggle_cursor,
            ord('1'): self._kb_cursor_a,
            ord('2'): self._kb_cursor_b,
            ord(']'): self._kb_next_edge,
            ord('['): self._kb_prev_edge,
            ord('m'): self.place_marker,
            ord('M'): self._kb_clear_markers,
            ord('<'): self.jump_prev_marker, ord(','): self.jump_prev_marker,
            ord('.'): self.jump_next_marker, ord('>'): self.jump_next_marker,
            10: self._kb_toggle_full_names, K.KEY_ENTER: self._kb_toggle_full_names,
            ord('i'): self._kb_toggle_indent,
            ord('S'): self.snap_cursor_to_edge,
            ord('s'): self._kb_toggle_panel,
            ord('\t'): self._kb_tab,
            ord('{'): self._kb_collapse_all,
            ord('}'): self._kb_expand_all,
            ord('P'): self.do_pattern_search,
            ord('E'): self.do_export_menu,
            ord('T'): self._kb_toggle_time_units,
            ord('0'): self._kb_menu_next,
            ord('9'): self._kb_menu_prev,
            ord('\\'): self.cycle_theme,
            ord('Q'): self._kb_toggle_grid,
            ord('w'): self._kb_line_thickness,
            ord('('): self._kb_name_width_dec,
            ord(')'): self._kb_name_width_inc,
            ord('|'): self.cycle_signal_spacing,
            ord('G'): self._vim_goto_last_in_module,
            ord('r'): self._kb_radix_cycle,
            ord('a'): self._kb_toggle_analog,
            ord('t'): self._kb_toggle_values,
            ord('V'): self._kb_visual_mode,
            ord(' '): self._kb_toggle_highlight,
            K.KEY_BACKSPACE: self._kb_backspace_remove,
            127: self._kb_backspace_remove, 8: self._kb_backspace_remove,
            ord('d'): self.delete_signal,
            ord('u'): self.undo,
            ord('/'): self.open_finder,
            ord('n'): self.next_search_result,
            ord('f'): self.do_find_value,
            ord('B'): self._kb_show_tree,
            ord('x'): self.expand_bus,
            ord('F'): self.expand_float_fields,
            ord('R'): self.cycle_signal_radix,
            ord('L'): self._kb_label_size,
            ord('v'): self._kb_signal_color,
            ord('H'): self._kb_signal_height,
            ord('D'): self.do_load_translations,
            ord('I'): self.do_protocol_decode_menu,
            ord('e'): self.do_edge_to_edge,
            ord(';'): self._kb_command_line,
            ord('A'): self.add_annotation_prompt,
            ord('p'): self._kb_reg_panel,
            ord('`'): self._trace_query_prompt,
            ord(':'): self.do_tcl_execute,
            ord('X'): self.export_svg,
            K.KEY_F5: self.toggle_live_reload,
            K.KEY_MOUSE: self.handle_mouse,
            K.KEY_RESIZE: self._kb_resize,
        }
