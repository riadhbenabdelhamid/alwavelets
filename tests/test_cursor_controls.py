"""A/B selection and visibility stay independent throughout the viewer."""
import curses

import pytest

from alwavelets.ui_chrome import wave_origin
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


@pytest.mark.parametrize('a_on,b_on', [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize('active', ['a', 'b'])
def test_toggle_changes_only_selected_cursor_and_preserves_times(viewer, a_on, b_on, active):
    v = viewer
    v.cursor_on, v.cursor_b_on, v.active_cursor = a_on, b_on, active
    v.cursor_a, v.cursor_b = 25, 75
    for expected in ((not a_on, b_on) if active == 'a' else (a_on, not b_on), (a_on, b_on)):
        v.keymap[ord('c')]()
        assert (v.cursor_on, v.cursor_b_on) == expected
        assert (v.cursor_a, v.cursor_b, v.active_cursor) == (25, 75, active)


@pytest.mark.parametrize('initial', [(False, False), (True, False), (False, True), (True, True)])
def test_hide_both_sequence_then_show_either_independently(viewer, initial):
    v = viewer
    v.cursor_on, v.cursor_b_on = initial
    v.cursor_a, v.cursor_b = 25, 75
    for key, expected in [('1', (True, initial[1])), ('c', (False, initial[1])),
                          ('2', (False, True)), ('c', (False, False))]:
        v.keymap[ord(key)]()
        assert (v.cursor_on, v.cursor_b_on) == expected
    assert (v.cursor_a, v.cursor_b) == (25, 75)
    v.keymap[ord('2')]()
    assert (v.active_cursor, v.cursor_on, v.cursor_b_on) == ('b', False, True)
    v.keymap[ord('c')]()
    v.keymap[ord('1')]()
    assert (v.active_cursor, v.cursor_on, v.cursor_b_on) == ('a', True, False)


@pytest.mark.parametrize('active', ['a', 'b'])
def test_single_visible_cursor_moves_and_anchors_zoom(viewer, active):
    v = viewer
    v.cursor_on, v.cursor_b_on = active == 'a', active == 'b'
    v.active_cursor = active
    v.cursor_a, v.cursor_b = 25, 75
    before = v.cursor_time
    other = v.cursor_b if active == 'a' else v.cursor_a
    v.keymap[curses.KEY_RIGHT]()
    assert v.cursor_time > before
    v.keymap[curses.KEY_LEFT]()
    assert v.cursor_time == before
    assert (v.cursor_b if active == 'a' else v.cursor_a) == other
    column = v.time_to_col(v.cursor_time)
    v.zoom(0.5)
    assert abs(v.time_to_col(v.cursor_time) - column) <= 1


@pytest.mark.parametrize('active', ['a', 'b'])
def test_hidden_selected_cursor_scrolls_without_moving_either_cursor(viewer, active):
    v = viewer
    v.cursor_on, v.cursor_b_on = active != 'a', active != 'b'
    v.active_cursor = active
    v.cursor_a, v.cursor_b = 25, 75
    v.time_start, v.time_end = 100, 200
    v.keymap[curses.KEY_RIGHT]()
    assert v.time_start > 100
    v.keymap[curses.KEY_LEFT]()
    assert (v.time_start, v.time_end) == (100, 200)
    assert (v.cursor_a, v.cursor_b) == (25, 75)


@pytest.mark.parametrize('active', ['a', 'b'])
def test_single_cursor_draws_values_and_readout_without_delta(viewer, active, monkeypatch):
    v = viewer
    v.stdscr = Screen(120)
    v.panel_mode = False
    v.show_module_sep = False
    v.cursor_on, v.cursor_b_on = active == 'a', active == 'b'
    v.active_cursor = active
    v.cursor_a, v.cursor_b = 25, 75
    v.update_layout()
    read_at = []
    def value(*args):
        read_at.append(v.cursor_time)
        return '1' if active == 'b' else '0'
    monkeypatch.setattr(v, 'format_signal_value', value)
    v.draw_ruler()
    v.draw_signals()
    v.draw_status_bar()
    left = wave_origin(v)
    assert v.stdscr.rows[v.ruler_row][left + v.time_to_col(v.cursor_time)] == active.upper()
    assert v.stdscr.rows[v.wave_top][left + v.time_to_col(v.cursor_time)] == '┃'
    hidden_time = v.cursor_b if active == 'a' else v.cursor_a
    assert v.stdscr.rows[v.wave_top][left + v.time_to_col(hidden_time)] != '┃'
    assert read_at == [v.cursor_time]
    footer = v.stdscr.line(v.height - 1)
    assert ' ' + active.upper() + ' ' in footer
    assert 'Δ ' not in footer and v.cursor_delta is None


def test_measurements_require_both_visible_and_inspector_labels_hidden_cursor(viewer, monkeypatch):
    v = viewer
    v.stdscr = Screen(120)
    v.show_inspector = True
    v.cursor_a, v.cursor_b = 25, 75
    v.cursor_on, v.cursor_b_on = False, True
    v.update_layout()
    monkeypatch.setattr(v, 'inspector_details', lambda *args: {'a': None, 'b': '1', 'transitions': 1})
    v.draw_inspector()
    lines = '\n'.join(v.stdscr.line(row) for row in range(v.height))
    assert 'A off' in lines and 'B 75ns = 1' in lines
    assert 'Enable both cursors' in lines and 'Δ A–B:' not in lines
    before = v.time_start, v.time_end
    v.zoom_to_selection()
    assert (v.time_start, v.time_end) == before
    assert v.tcl.execute('measure') == ''
    v._kb_cursor_a()
    assert v.cursor_delta == 50 and 'Delta: 50' in v.tcl.execute('measure')
    v.zoom_to_selection()
    assert (v.time_start, v.time_end) != before


def test_expression_search_shows_selected_b_without_enabling_a(viewer):
    v = viewer
    v.active_cursor = 'b'
    v.cursor_b = 0
    v.cursor_on = v.cursor_b_on = False
    v.execute_command('when clk == 1')
    assert (v.cursor_on, v.cursor_b_on) == (False, True)
    assert v.cursor_b == 50


@pytest.mark.parametrize('active', ['a', 'b'])
def test_cursor_command_shows_selected_cursor_independently(viewer, active):
    viewer.cursor_on = viewer.cursor_b_on = False
    viewer.execute_command('cursor ' + active)
    assert viewer.active_cursor == active
    assert (viewer.cursor_on, viewer.cursor_b_on) == (active == 'a', active == 'b')


@pytest.mark.parametrize('a_on,b_on,active', [(False, False, 'b'), (False, True, 'b'), (True, False, 'a')])
def test_session_restores_visibility_selection_and_positions(viewer, tmp_path, monkeypatch, a_on, b_on, active):
    v = viewer
    monkeypatch.setattr(v, 'apply_theme', lambda: None)
    v.vcd._filename = str(tmp_path / 'cursors.vcd')
    v.cursor_a, v.cursor_b = 25, 75
    v.cursor_on, v.cursor_b_on, v.active_cursor = a_on, b_on, active
    v.save_session()
    v.cursor_a, v.cursor_b = 0, 0
    v.cursor_on = v.cursor_b_on = True
    v.active_cursor = 'a'
    assert v.restore_session()
    assert (v.cursor_a, v.cursor_b, v.cursor_on, v.cursor_b_on, v.active_cursor) == (25, 75, a_on, b_on, active)
