"""Exercise the palette and pane layout through real viewer actions."""
import curses

import pytest

from alwavelets.core import VCDSignal
from alwavelets.ui_actions import contextual_menu, shortcut_for
from alwavelets.ui_chrome import signal_origin, wave_origin
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


def query(viewer, text):
    viewer.action_palette_handle_key(21)
    for ch in text:
        viewer.action_palette_handle_key(ord(ch))
    return [viewer._palette_actions[i] for i in viewer.action_matches]


def test_palette_search_executes_existing_actions_and_handles_cancel(viewer):
    viewer.keymap[curses.KEY_F4]()
    assert viewer.show_actions
    viewer.cursor_a, viewer.cursor_b, viewer.cursor_b_on = 20, 80, True
    assert query(viewer, 'fit cursors')[0].handler == 'zoom_to_selection'
    viewer.action_palette_handle_key(10)
    assert not viewer.show_actions
    assert (viewer.time_start, viewer.time_end) == (17, 83)
    viewer.keymap[16]()  # Ctrl-P
    query(viewer, 'fit all')
    viewer.action_palette_handle_key(27)
    assert (viewer.time_start, viewer.time_end) == (17, 83)
    assert not viewer.show_actions


@pytest.mark.parametrize('answer', [None, '', '30ns'])
def test_command_search_prioritizes_exact_verb_and_prompts_for_arguments(viewer, monkeypatch, answer):
    prompts = []
    monkeypatch.setattr(viewer, 'prompt_input', lambda prompt: prompts.append(prompt) or answer)
    viewer.execute_command('actions')
    assert query(viewer, 'goto')[0].command == 'goto'
    before = viewer.cursor_time
    viewer.action_palette_handle_key(curses.KEY_ENTER)
    assert prompts == ['goto <time>: ']
    assert viewer.cursor_time == (30 if answer else before)
    assert not viewer.show_actions


def test_hints_and_palette_use_live_key_bindings(viewer):
    viewer._apply_keymap_overrides({'zoom_in': '?', 'help': '+'})
    assert shortcut_for(viewer, '_kb_zoom_in') == '?/='
    assert shortcut_for(viewer, '_kb_help') == '+'
    assert ('+', 'Help') in contextual_menu(viewer)
    assert ('?', 'Zoom in') in contextual_menu(viewer)
    viewer.open_action_palette()
    assert query(viewer, 'zoom in')[0].handler == '_kb_zoom_in'
    viewer.draw_action_palette()
    assert '?/=' in viewer.stdscr.line(3)
    assert viewer.stdscr.line(viewer.height - 2).strip().startswith('Keep the active cursor')
    assert query(viewer, 'F3')[0].handler == '_kb_cycle_workmode'


@pytest.mark.parametrize('width', [24, 40, 80, 120])
def test_palette_navigation_and_empty_search_survive_resize(viewer, width):
    viewer.open_action_palette()
    viewer.action_palette_handle_key(curses.KEY_END)
    viewer.stdscr = Screen(width)
    viewer.stdscr.height = 10
    viewer.stdscr.erase()
    viewer.update_layout()
    viewer.draw_action_palette()
    assert viewer.action_selected == len(viewer.action_matches) - 1
    assert viewer.action_scroll > 0
    query(viewer, 'no_such_action_987654321')
    viewer.action_palette_handle_key(curses.KEY_DOWN)
    viewer.action_palette_handle_key(10)
    viewer.draw_action_palette()
    assert viewer.show_actions
    assert 'No matching actions' in viewer.stdscr.line(3)
    viewer.action_palette_handle_key(21)
    viewer.action_palette_handle_key(curses.KEY_HOME)
    viewer.draw_action_palette()
    assert viewer.action_selected == viewer.action_scroll == 0


def test_palette_does_not_enumerate_trace_metadata(viewer, monkeypatch):
    class NoEnumeration(dict):
        def __iter__(self):
            pytest.fail('Opening the palette scanned the signal catalog')

        def values(self):
            pytest.fail('Opening the palette scanned the signal catalog')

        def items(self):
            pytest.fail('Opening the palette scanned the signal catalog')

    monkeypatch.setattr(viewer.vcd, 'signals', NoEnumeration(viewer.vcd.signals))
    viewer.open_action_palette()
    assert query(viewer, 'export')
    viewer.draw_action_palette()


def test_hints_follow_scope_signal_and_waveform_focus(viewer):
    viewer.panel_focus = 0
    assert ('Space', 'Add/remove scope') in contextual_menu(viewer)
    viewer.panel_handle_tree_key(curses.KEY_RIGHT)
    viewer.panel_handle_tree_key(curses.KEY_DOWN)
    assert ('Space', 'Add/remove signal') in contextual_menu(viewer)
    viewer._kb_tab()
    assert ('Space', 'Add/remove signal') not in contextual_menu(viewer)
    assert not any(label == 'Expand bus bits' for _, label in contextual_menu(viewer))
    viewer.vcd.signals['!'].size = 8
    assert ('x', 'Expand bus bits') in contextual_menu(viewer)
    viewer.selected_signals = {'!'}
    assert ('BS', 'Remove 1 selected') in contextual_menu(viewer)
    assert ('u', 'Undo') in contextual_menu(viewer)


@pytest.mark.parametrize('width', [24, 40, 80, 96, 120, 180])
def test_hierarchy_never_replaces_waveform_labels_except_explicit_overlay(viewer, width):
    viewer.stdscr = Screen(width)
    viewer.update_layout()
    viewer.draw_ruler()
    viewer.draw_signals()
    left = signal_origin(viewer)
    before = [row[left:] for row in viewer.stdscr.rows]
    viewer.draw_panel_tree()
    assert before == [row[left:] for row in viewer.stdscr.rows]
    assert 'clk' in ''.join(viewer.stdscr.rows[viewer.wave_top][left:wave_origin(viewer)])
    assert '▸ Signals' in viewer.stdscr.line(viewer.ruler_row) or width == 24
    viewer._kb_tab()
    viewer.draw_panel_tree()
    assert '▸ Hierarchy' in viewer.stdscr.line(viewer.ruler_row)
    if width >= 96:
        assert left > 0
        assert before == [row[left:] for row in viewer.stdscr.rows]
    else:
        assert left == 0
        assert before != viewer.stdscr.rows
    viewer._kb_escape()
    assert viewer.panel_focus == 1
    viewer.stdscr.erase()
    viewer.draw_ruler()
    viewer.draw_signals()
    viewer.draw_panel_tree()
    assert before == [row[left:] for row in viewer.stdscr.rows]


@pytest.mark.parametrize('width', [24, 40, 80, 120])
def test_long_unicode_names_and_values_cannot_overwrite_waveform(viewer, width):
    viewer.stdscr = Screen(width)
    viewer.update_layout()
    viewer.indent_mode = True
    sig = viewer.vcd.signals['!']
    sig.name, sig.scope, sig.size = '信号' * 40, 'top.cpu.alu.deep', 64
    viewer.cursor_on = False
    viewer.draw_signals()
    origin = wave_origin(viewer)
    waveform = viewer.stdscr.rows[viewer.wave_top][origin:]
    viewer.cursor_on = True
    viewer.cursor_a = -1000  # Show the value column, without drawing a cursor.
    viewer.format_signal_value = lambda *args: '界' * 40
    viewer.draw_signals()
    assert waveform == viewer.stdscr.rows[viewer.wave_top][origin:]
    assert viewer.stdscr.rows[viewer.wave_top][origin - 1] == '│'


def test_mouse_focus_and_time_follow_panes_and_actual_signal_rows(viewer, monkeypatch):
    viewer.stdscr = Screen(120)
    sig = VCDSignal('analog', '#', 8, scope='top')
    sig.add_change(0, '0')
    viewer.vcd.signals['#'] = sig
    viewer.display_order = ['#', '!']
    viewer.analog_signals = {'#'}
    viewer.sig_height['#'] = 3
    viewer.update_layout()
    viewer.draw_signals()

    def click(x, y, button=curses.BUTTON1_CLICKED):
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x, y, 0, button))
        viewer.handle_mouse()

    click(1, viewer.wave_top)
    assert viewer.panel_focus == 0
    previous_time = viewer.cursor_time
    click(signal_origin(viewer) + 2, viewer.wave_top + 3)
    assert viewer.panel_focus == 1
    assert viewer.focused_signal_id() == '!'
    assert viewer.cursor_time == previous_time
    click(wave_origin(viewer) + 10, viewer.wave_top + 1)
    assert viewer.focused_signal_id() == '#'
    assert viewer.cursor_time == viewer.col_to_time(10)
    viewer.stdscr = Screen(80)
    viewer.update_layout()
    viewer._kb_tab()
    previous_time, previous_range = viewer.cursor_time, (viewer.time_start, viewer.time_end)
    click(60, viewer.wave_top)
    click(60, viewer.wave_top, curses.BUTTON4_PRESSED)
    assert viewer.cursor_time == previous_time
    assert (viewer.time_start, viewer.time_end) == previous_range


def test_manual_label_width_survives_browser_toggle_and_wide_resize(viewer):
    viewer.name_width, viewer._name_width_manual = 26, True
    viewer.stdscr = Screen(120)
    viewer.update_layout()
    for width in (180, 120):
        viewer.stdscr = Screen(width)
        for _ in range(2):
            viewer._kb_toggle_panel()
            viewer.update_layout()
            assert viewer.name_width == 26
            assert viewer._name_width_manual


def test_palette_runs_in_event_loop_with_empty_hierarchy_and_resize(viewer, monkeypatch):
    viewer._tree_flat = []
    viewer.panel_focus = 0
    for name in ('curs_set', 'halfdelay'):
        monkeypatch.setattr(curses, name, lambda *args: None)
    for name in ('keypad', 'refresh'):
        monkeypatch.setattr(viewer.stdscr, name, lambda *args: None, raising=False)
    monkeypatch.setattr(viewer, '_cleanup_mouse', lambda: None)
    keys = iter([curses.KEY_F4, curses.KEY_RESIZE, *map(ord, 'quit'), 10])
    monkeypatch.setattr(viewer.stdscr, 'getch', lambda: next(keys), raising=False)
    viewer.run()
    assert not viewer.show_actions
    assert query(viewer, 'quit')[0].handler == '_kb_quit'


@pytest.mark.parametrize('menu', [False, True])
def test_time_overlays_follow_waveform_origin_with_sidebar(viewer, menu):
    from alwavelets.core import Marker
    from alwavelets.persistence import AnnotationStore, WaveformAnnotation

    viewer.stdscr = Screen(120)
    viewer.show_menu_bar = menu
    viewer.update_layout()
    viewer.cursor_a, viewer.cursor_b, viewer.cursor_b_on = 25, 75, True
    viewer.markers = [Marker(50, 'middle')]
    viewer.draw_ruler()
    viewer.draw_signals()
    origin = wave_origin(viewer)
    row = viewer.stdscr.rows[viewer.wave_top]
    assert row[origin + viewer.time_to_col(25)] == '┃'
    assert row[origin + viewer.time_to_col(75)] == '┃'
    assert row[origin + viewer.time_to_col(50)] == '▎'
    viewer.annotations.add(WaveformAnnotation(40, text='check', category='note'))
    viewer.show_annotations = True
    viewer.draw_annotation_overlays()
    assert viewer.stdscr.rows[viewer.ruler_row][origin + viewer.time_to_col(40)] == AnnotationStore.CATEGORY_MARKERS['note'][0]
    labels = [line[signal_origin(viewer):origin] for line in viewer.stdscr.rows]
    viewer.draw_panel_tree()
    assert labels == [line[signal_origin(viewer):origin] for line in viewer.stdscr.rows]


def test_group_headers_and_scope_separators_remain_visible_next_to_browser(viewer):
    viewer.stdscr = Screen(120)
    viewer.update_layout()
    assert viewer.create_group('Clock', ['!'])
    viewer.draw_signals()
    viewer.draw_panel_tree()
    assert 'Clock' in ''.join(viewer.stdscr.rows[viewer.wave_top][signal_origin(viewer):])
    before = [row[:signal_origin(viewer)] for row in viewer.stdscr.rows]
    viewer._draw_scope_separator(viewer.wave_top + 3, 'cpu')
    assert before == [row[:signal_origin(viewer)] for row in viewer.stdscr.rows]
    assert 'cpu' in ''.join(viewer.stdscr.rows[viewer.wave_top + 3][signal_origin(viewer):])
