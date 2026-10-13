"""Searchable viewer actions and contextual hints, using the live keymap."""
import curses
from dataclasses import dataclass

from .finder import fuzzy_match
from .ui_chrome import elide, text_width


ACTION_INFO = {
    'view_back': ('Previous view', 'Restore the previous time range; keep cursor positions'),
    'view_forward': ('Next view', 'Return to the next time range after going back'),
    'toggle_inspector': ('Signal inspector', 'Show or hide focused signal details and cursor values'),
    'toggle_overview': ('Timeline overview', 'Show or hide the whole-trace position strip; click to navigate'),
    'open_action_palette': ('Actions', 'Search commands and keyboard actions'),
    'open_signal_menu': ('Signal actions', 'Open a menu beside the focused signal; formatting applies to the selection'),
    '_kb_help': ('Help', 'Show quick keyboard help'),
    '_kb_show_manual': ('Manual', 'Open the full reference manual'),
    '_kb_cycle_workmode': ('Workmode', 'Cycle Classic, Minimal, and Calm presentation'),
    '_kb_tab': ('Switch pane', 'Move focus between hierarchy and waveforms; fold groups when browsing is hidden'),
    '_kb_toggle_panel': ('Hierarchy', 'Show or hide the hierarchy browser'),
    'open_finder': ('Find signals', 'Search signal names, add signals, and jump to a row'),
    '_kb_zoom_in': ('Zoom in', 'Keep the active cursor in place while showing more detail'),
    '_kb_zoom_out': ('Zoom out', 'Keep the active cursor in place while showing more time'),
    'zoom_fit_all': ('Fit all', 'Show the entire trace time range'),
    'zoom_to_selection': ('Fit cursors A–B', 'Zoom to the interval between both cursors'),
    '_kb_next_edge': ('Next edge', 'Move to the next transition on the focused waveform'),
    '_kb_prev_edge': ('Previous edge', 'Move to the previous transition on the focused waveform'),
    'snap_cursor_to_edge': ('Snap to edge', 'Snap the cursor to the nearest waveform transition'),
    '_kb_cursor_a': ('Cursor A', 'Show and select cursor A'),
    '_kb_cursor_b': ('Cursor B', 'Show and select cursor B'),
    '_kb_toggle_cursor': ('Toggle selected cursor', 'Show or hide the selected cursor; keep the other unchanged'),
    'expand_bus': ('Expand bus bits', 'Expand or collapse individual bit lanes of the focused bus'),
    'expand_float_fields': ('Expand format components', 'Expand fixed-point or float into its fields and decoded value'),
    'cycle_signal_radix': ('Signal radix', 'Change the number format of selected waveforms, or the focused waveform'),
    '_kb_radix_cycle': ('Global radix', 'Change the number format for all waveforms'),
    '_kb_signal_color': ('Signal color', 'Change the color of selected waveforms, or the focused waveform'),
    '_kb_signal_height': ('Signal height', 'Change the height of selected analog waveforms, or the focused waveform'),
    '_kb_label_size': ('Value labels', 'Cycle hidden, compact, and full bus labels'),
    '_kb_line_thickness': ('Wave style', 'Cycle thin, ultra-thin, and half-cell resolution'),
    '_kb_toggle_grid': ('Grid', 'Cycle off, vertical, and both grid directions'),
    '_kb_toggle_highlight': ('Highlight signal', 'Toggle the focused waveform selection'),
    '_kb_visual_mode': ('Select range', 'Select a contiguous range of waveform rows'),
    '_kb_backspace_remove': ('Remove selection', 'Remove selected waveforms; undo with the Undo action'),
    'delete_signal': ('Remove signal', 'Remove the focused waveform from the view'),
    'undo': ('Undo', 'Undo the last supported signal or layout edit'),
    'redo': ('Redo', 'Replay the last undone signal or layout edit'),
    '_kb_move_signal_up': ('Move signal up', 'Reorder the focused waveform'),
    '_kb_move_signal_down': ('Move signal down', 'Reorder the focused waveform'),
    '_kb_command_line': ('Command prompt', 'Enter a viewer command'),
    '_kb_quit': ('Quit', 'Close the viewer'),
    '_kb_escape': ('Cancel', 'Cancel selection or return from hierarchy to waveforms'),
    '_kb_toggle_full_names': ('Signal names', 'Toggle full paths and short names'),
    '_kb_toggle_menu_bar': ('Shortcut guide', 'Show or hide contextual keyboard hints'),
    'do_export_menu': ('Export', 'Save the view, waveform data, or an arrangement'),
    'add_annotation_prompt': ('Add annotation', 'Label the current time or the interval between cursors'),
    'place_marker': ('Place marker', 'Place a marker at the active cursor'),
    '_kb_clear_markers': ('Clear markers', 'Remove all time markers'),
    'jump_next_marker': ('Next marker', 'Jump to the next marked time'),
    'jump_prev_marker': ('Previous marker', 'Jump to the previous marked time'),
    '_kb_collapse_all': ('Collapse all scopes', 'Fold every scope in the waveform view'),
    '_kb_expand_all': ('Expand all scopes', 'Unfold every scope in the waveform view'),
    '_collapse_focused_scope': ('Collapse focused scope', 'Fold the focused waveform scope'),
    '_expand_focused_scope': ('Expand focused scope', 'Unfold the focused waveform scope'),
    '_kb_cursor_left': ('Move left', 'Move the cursor left, or scroll time when the cursor is hidden'),
    '_kb_cursor_right': ('Move right', 'Move the cursor right, or scroll time when the cursor is hidden'),
    '_kb_signal_down': ('Next signal', 'Focus the next waveform row'),
    '_kb_signal_up': ('Previous signal', 'Focus the previous waveform row'),
    '_kb_page_down': ('Next waveform page', 'Scroll down one page of waveform rows'),
    '_kb_page_up': ('Previous waveform page', 'Scroll up one page of waveform rows'),
    '_kb_home': ('Start of trace', 'Jump to the beginning of the simulation'),
    '_kb_end': ('End of trace', 'Jump to the end of the simulation'),
    '_jump_next_module': ('Next module', 'Focus the next module in the waveform view'),
    '_jump_prev_module': ('Previous module', 'Focus the previous module in the waveform view'),
    '_vim_goto_last_in_module': ('Last signal in module', 'Focus the last waveform in the current module'),
    'cycle_signal_spacing': ('Row spacing', 'Cycle Compact / Blank gap / Divider between signals'),
    'cycle_theme': ('Cycle theme', 'Change the color palette while keeping the current workmode'),
    '_kb_toggle_analog': ('Analog display', 'Toggle analog rendering for the focused waveform'),
    '_kb_toggle_indent': ('Indent signal names', 'Indent waveform labels by scope depth'),
    '_kb_toggle_time_units': ('Time units', 'Toggle raw ticks and physical time units'),
    '_kb_toggle_values': ('Values at cursor', 'Show or hide values beside signal names'),
    '_kb_name_width_dec': ('Narrower signal labels', 'Reduce the signal-name column by two cells'),
    '_kb_name_width_inc': ('Wider signal labels', 'Widen the signal-name column by two cells'),
    '_kb_menu_next': ('Next shortcut page', 'Show the next page of contextual shortcuts'),
    '_kb_menu_prev': ('Previous shortcut page', 'Show the previous page of contextual shortcuts'),
    '_kb_reg_panel': ('Register panel', 'Show or hide decoded register values'),
    '_kb_show_colors': ('Color guide', 'Explain the waveform colors'),
    '_kb_show_tree': ('Full-screen signal browser', 'Browse scopes and choose signals to display'),
    'do_edge_to_edge': ('Measure between edges', 'Measure time between waveform transitions'),
    'do_find_value': ('Find value', 'Search for a value on the focused waveform'),
    'do_pattern_search': ('Search pattern', 'Find a waveform pattern'),
    'next_search_result': ('Next search result', 'Jump to the next waveform search match'),
    'do_load_translations': ('Value translations', 'Load names for encoded waveform values'),
    'do_protocol_decode_menu': ('Decode protocol', 'Choose a protocol decoder and its signals'),
    'do_tcl_execute': ('Tcl prompt', 'Enter a Tcl command or script'),
    '_trace_query_prompt': ('Instruction query', 'Query decoded instruction execution'),
    'export_svg': ('Export SVG', 'Save the current waveform view as a vector image'),
    'toggle_live_reload': ('Watch trace file', 'Toggle reloading a VCD file when it changes'),
}

# Commands with arguments show their syntax and ask before executing. The
# remaining commands already run directly or present their own input prompt.
COMMAND_INPUT = {
    'zoom': ('Set time range', '<start> <end>', '170ns 290ns'),
    'goto': ('Go to time', '<time>', '220ns'),
    'find': ('Jump to signal', '<name>', 'clk'),
    'add': ('Add signals', '<pattern> ...', 'top.clk top.data*'),
    'remove': ('Remove signals', '<pattern> ...', 'top.data*'),
    'when': ('Find expression', '<expression>', 'valid && ready'),
    'derive': ('Create derived signal', '<name> = <expression>', 'busy = valid && ready'),
    'group': ('Group signals', '<name>', 'Bus'),
    'ungroup': ('Ungroup signals', '<name>', 'Bus'),
    'loadgtkw': ('Load GTKWave layout', '<path>', 'trace.gtkw'),
    'savegtkw': ('Save GTKWave layout', '<path>', 'trace.gtkw'),
    'radix': ('Set global radix', '<radix>', 'hex'),
    'fixed': ('Configure custom fixed-point', '<integer bits> <fraction bits> [signed|unsigned]', '7 8 signed'),
    'float': ('Configure custom float', '<exponent bits> <mantissa bits> [bias|auto] [ieee|finite|nan]', '5 10 auto ieee'),
    'theme': ('Choose theme', '<name or path>', 'phosphor'),
    'workmode': ('Choose workmode', '<calm|classic|minimal>', 'classic'),
    'decode': ('Load value translations', '<path>', 'opcodes.txt'),
    'eval': ('Evaluate Tcl', '<script>', 'set time 100'),
}
COMMAND_INFO = {
    'spacing': ('Row spacing', 'Cycle Compact / Blank gap / Divider (|)'),
    'signal-menu': ('Signal actions', 'Open the signal menu (F10)'),
    'expand-fields': ('Expand format components', 'Expand or collapse fixed-point/float fields (F)'),
    'undo': ('Undo', 'Undo a signal or layout edit (u)'),
    'redo': ('Redo', 'Replay an undone signal or layout edit (Ctrl-R)'),
    'session': ('Save session', 'Save the current layout and appearance for this trace'),
    'stats': ('Signal statistics', 'Measure the focused waveform in the current time range'),
    'cancel': ('Cancel analysis', 'Stop the current analysis job'),
    'follow': ('Follow live trace', 'Toggle following appended waveform data'),
    'glitches': ('Find glitches', 'Search for short pulses and unknown values'),
    'bookmarks': ('List bookmarks', 'Show saved positions in this trace'),
}


def key_label(key):
    labels = {9: 'Tab', 10: 'Enter', 13: 'Enter', 27: 'Esc', 32: 'Space', 127: 'BS',
              curses.KEY_ENTER: 'Enter', curses.KEY_BACKSPACE: 'BS',
              curses.KEY_UP: '↑', curses.KEY_DOWN: '↓', curses.KEY_LEFT: '←', curses.KEY_RIGHT: '→',
              curses.KEY_HOME: 'Home', curses.KEY_END: 'End',
              curses.KEY_PPAGE: 'PgUp', curses.KEY_NPAGE: 'PgDn',
              curses.KEY_SLEFT: 'S←', curses.KEY_SRIGHT: 'S→', curses.KEY_SR: 'S↑', curses.KEY_SF: 'S↓'}
    if key in labels:
        return labels[key]
    if curses.KEY_F1 <= key <= curses.KEY_F12:
        return f'F{key - curses.KEY_F1 + 1}'
    if 0 < key < 27:
        return 'Ctrl-' + chr(64 + key)
    return chr(key) if 32 <= key < 256 else str(key)


def shortcut_for(viewer, handler, limit=2):
    labels = []
    for key, function in viewer.keymap.items():
        if getattr(function, '__name__', '') == handler:
            label = key_label(key)
            if label not in labels:
                labels.append(label)
    return '/'.join(labels[:limit])


def contextual_menu(viewer):
    def item(handler, label=None):
        return shortcut_for(viewer, handler, limit=1), label or ACTION_INFO[handler][0]

    items = [item('open_action_palette'), item('_kb_help')]
    if viewer.panel_mode:
        items.append(item('_kb_tab', 'Waveforms' if viewer.panel_focus == 0 else 'Hierarchy'))
    if viewer.panel_mode and viewer.panel_focus == 0:
        kind = viewer._tree_flat[viewer.tree_cursor][0] if viewer.tree_cursor < len(viewer._tree_flat) else 'scope'
        items += [('↑↓', 'Browse'), ('←→', 'Fold scope'), ('Space', 'Add/remove ' + kind)]
        if kind == 'scope':
            items.append(('Enter', 'Open scope'))
    else:
        if viewer.selected_signals:
            items += [item('_kb_backspace_remove', f'Remove {len(viewer.selected_signals)} selected'), item('undo')]
        if getattr(viewer, '_redo_stack', []):
            items.append(item('redo'))
        if viewer.selected_signals or viewer.focused_signal_id() is not None:
            items.append(item('open_signal_menu'))
        sid = viewer.focused_signal_id()
        sig = viewer.get_signal(sid) if sid else None
        if sig is not None:
            if sig.size > 1:
                items.append(item('expand_bus'))
            items += [item('cycle_signal_radix'), item('_kb_next_edge'), item('_kb_prev_edge')]
        elif not viewer.panel_mode:
            items.append(item('_kb_tab', 'Fold group'))
        items += [item('_kb_zoom_in'), item('_kb_zoom_out'), item('zoom_fit_all')]
        if getattr(viewer, 'zoom_history', []):
            items.append(item('view_back', 'Back'))
        if getattr(viewer, 'zoom_forward', []):
            items.append(item('view_forward', 'Forward'))
        if viewer.cursor_on and viewer.cursor_b_on:
            items.append(item('zoom_to_selection'))
    items += [item('open_finder'), item('toggle_inspector'), item('toggle_overview'), item('_kb_cycle_workmode'), item('_kb_toggle_panel'), item('_kb_toggle_menu_bar')]
    return [(key, label) for key, label in items if key]


@dataclass(frozen=True)
class Action:
    title: str
    description: str
    handler: str = ''
    command: str = ''
    arguments: str = ''


class ActionPaletteMixin:
    def available_actions(self):
        actions, seen = [], set()
        for handler in self.keymap.values():
            name = getattr(handler, '__name__', '')
            if not name or name in seen or name in ('handle_mouse', '_kb_resize', 'open_action_palette'):
                continue
            seen.add(name)
            readable = name[4:] if name.startswith('_kb_') else name
            title, description = ACTION_INFO.get(name, (readable.strip('_').replace('_', ' ').capitalize(), ''))
            actions.append(Action(title, description, handler=name))
        aliases = {'q', 'quit', 'exit', 'rm', 'bm', 'gb', 'cex', 'man', 'glitch', 'match', 'measure', 'actions'}
        for command in self.command_verbs():
            if command in aliases:
                continue
            if command in COMMAND_INPUT:
                title, arguments, example = COMMAND_INPUT[command]
                description = f';{command} {arguments} · e.g. {example}'
            else:
                title, description = COMMAND_INFO.get(command, (command.replace('_', ' ').capitalize(), f'Run ;{command}'))
                arguments = ''
            actions.append(Action(title, description, command=command, arguments=arguments))
        return sorted(actions, key=lambda action: action.title.lower())

    def open_action_palette(self):
        self._palette_actions = self.available_actions()
        self.action_query = ''
        self.action_selected = self.action_scroll = 0
        self.show_actions = True
        self._filter_actions()

    def _filter_actions(self):
        terms = self.action_query.lower().split()
        matches = []
        for index, action in enumerate(self._palette_actions):
            shortcut = shortcut_for(self, action.handler) if action.handler else ''
            text = (action.title + ' ' + action.description + ' ' + action.command + ' ' + shortcut).lower()
            scores = [fuzzy_match(term, text) for term in terms]
            if all(score is not None for score in scores):
                exact_command = bool(terms) and self.action_query.strip().lower() == action.command
                matches.append((not exact_command, -sum(scores), index))
        self.action_matches = [index for _, _, index in sorted(matches)]
        self.action_selected = self.action_scroll = 0

    def action_palette_handle_key(self, key):
        if key == 27:
            self.show_actions = False
        elif key in (10, 13, curses.KEY_ENTER):
            if not self.action_matches:
                return
            action = self._palette_actions[self.action_matches[self.action_selected]]
            self.show_actions = False
            if action.handler:
                return getattr(self, action.handler)()
            args = ''
            if action.arguments:
                args = self.prompt_input(f'{action.command} {action.arguments}: ')
                if not args:
                    return
            return self.execute_command(action.command + (' ' + args if args else ''))
        elif key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END):
            step = max(1, self.height - 6) if key in (curses.KEY_PPAGE, curses.KEY_NPAGE) else 1
            index = self.action_selected + (-step if key in (curses.KEY_UP, curses.KEY_PPAGE) else step)
            if key == curses.KEY_HOME:
                index = 0
            elif key == curses.KEY_END:
                index = len(self.action_matches) - 1
            self.action_selected = max(0, min(len(self.action_matches) - 1, index))
        else:
            if key in (curses.KEY_BACKSPACE, 127, 8):
                self.action_query = self.action_query[:-1]
            elif key == 21:
                self.action_query = ''
            elif key == 23:
                self.action_query = self.action_query.rstrip().rsplit(' ', 1)[0] if ' ' in self.action_query.rstrip() else ''
            elif 32 <= key < 256:
                self.action_query += chr(key)
            else:
                return
            self._filter_actions()

    def draw_action_palette(self):
        self.stdscr.erase()
        h, w = self.height, self.width
        room = max(0, w - 1)
        def put(row, text, attr=0):
            if 0 <= row < h:
                text = elide(text, room)
                try:
                    self.stdscr.addstr(row, 0, text + ' ' * (room - text_width(text)), attr)
                except curses.error:
                    pass
        put(0, ' Actions · search commands and shortcuts', curses.color_pair(self.C_HEADER) | curses.A_BOLD)
        put(1, ' > ' + self.action_query + '▌', curses.A_BOLD)
        put(2, f' {len(self.action_matches)} actions', curses.A_DIM)
        visible = max(1, h - 6)
        self.action_scroll = max(0, min(self.action_scroll, self.action_selected))
        self.action_scroll = max(self.action_scroll, self.action_selected - visible + 1)
        if not self.action_matches:
            put(3, ' No matching actions. Ctrl-U clears search.', curses.A_DIM)
        for row, index in enumerate(self.action_matches[self.action_scroll:self.action_scroll + visible], 3):
            action = self._palette_actions[index]
            shortcut = shortcut_for(self, action.handler) if action.handler else ';' + action.command
            shortcut = elide(shortcut, max(0, room // 3))
            label_room = max(0, room - text_width(shortcut) - 2)
            label = elide(' ' + action.title, label_room)
            label += ' ' * (room - text_width(label) - text_width(shortcut)) + shortcut
            current = row - 3 + self.action_scroll == self.action_selected
            put(row, label, curses.color_pair(self.C_SELECT) | curses.A_BOLD if current else curses.A_NORMAL)
        if self.action_matches:
            action = self._palette_actions[self.action_matches[self.action_selected]]
            put(h - 2, ' ' + (action.description or action.title), curses.A_DIM)
        put(h - 1, ' ↑↓ Choose · Enter Run · Esc Back · Ctrl-U Clear', curses.color_pair(self.C_HEADER))
