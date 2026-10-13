"""Search marker metadata while keeping edits, selection, and paging coherent."""
import curses

import pytest

from alwavelets.core import Marker
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


def filter_list(viewer, monkeypatch, query):
    calls = []
    with monkeypatch.context() as patch:
        patch.setattr(viewer, 'prompt_input', lambda *args, **kwargs: calls.append(kwargs) or query)
        viewer.marker_manager_handle_key(ord('/'))
    return calls


def draw(viewer):
    viewer.stdscr.erase()
    viewer.draw_marker_manager()
    return '\n'.join(viewer.stdscr.line(row) for row in range(viewer.height))


@pytest.mark.parametrize('query,times', [
    ('rEsEt', [10, 30]), ('release 10 ns', [10]), ('120ns', [120]),
    ('120', [120]), ('120 NS', [120]), ('STRASSE ÜBER', [450]),
    ('unlabeled', [230]), ('absent', []),
])
def test_filter_matches_labels_displayed_time_and_ticks_without_editing(viewer, monkeypatch, query, times):
    original = [Marker(30, 'RESET asserted'), Marker(10, 'Reset release'),
                Marker(120, 'sample complete'), Marker(230, ''), Marker(450, 'Straße über')]
    viewer.markers = original[:]
    viewer.cursor_time = 30
    viewer._redo_stack = [('sentinel', lambda: None)]
    viewer.open_marker_manager()
    # Filtering must use marker metadata, without reading the waveform.
    monkeypatch.setattr(viewer, 'get_signal', lambda *a: pytest.fail('Search read a waveform'))
    calls = filter_list(viewer, monkeypatch, query)
    assert calls == [{'initial': '', 'cancel_value': None}]
    assert [m.time for m in viewer._marker_items()] == times
    screen = draw(viewer)
    assert f'Markers · {len(times)}/5 matches' in screen
    assert 'Filter: ' + query in screen
    assert 'Ctrl-U Reset filter' in screen
    assert viewer.markers == original and viewer.cursor_time == 30
    assert not viewer._undo_stack and len(viewer._redo_stack) == 1


def test_filter_preserves_matching_selection_and_reset_cancel_and_reopen(viewer, monkeypatch):
    first, selected, last = Marker(10, 'reset'), Marker(20, 'reset release'), Marker(30, 'data')
    viewer.markers = [first, selected, last]
    viewer.cursor_time = 20
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, 'reset')
    assert viewer._marker_menu['selected'] is selected
    calls = filter_list(viewer, monkeypatch, None)
    assert calls == [{'initial': 'reset', 'cancel_value': None}]
    assert viewer._marker_menu['query'] == 'reset'
    viewer._remote_depth = 1
    viewer.marker_manager_handle_key(ord('/'))
    assert viewer._marker_menu['query'] == 'reset'
    viewer._remote_depth = 0
    filter_list(viewer, monkeypatch, 'data')
    assert viewer._marker_menu['selected'] is last
    viewer.marker_manager_handle_key(21)
    assert viewer._marker_menu['query'] == ''
    assert viewer._marker_items() == [first, selected, last]
    assert viewer._marker_menu['selected'] is last
    filter_list(viewer, monkeypatch, 'reset')
    filter_list(viewer, monkeypatch, '   ')
    assert viewer._marker_menu['query'] == ''
    filter_list(viewer, monkeypatch, 'data')
    viewer.marker_manager_handle_key(27)
    viewer.open_marker_manager()
    assert viewer._marker_menu['query'] == ''


def test_filtered_paging_and_jump_report_position_in_full_list(viewer, monkeypatch):
    viewer.markers = [Marker(t, 'match' if t % 2 == 0 else 'other') for t in range(100)]
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, 'match')
    draw(viewer)
    rows = viewer._marker_menu['rows']
    viewer.marker_manager_handle_key(curses.KEY_NPAGE)
    assert viewer._marker_menu['selected'].time == rows * 2
    viewer.marker_manager_handle_key(curses.KEY_END)
    assert '98ns' in draw(viewer)
    assert viewer._marker_menu['selected'].time == 98
    viewer.marker_manager_handle_key(curses.KEY_UP)
    viewer.marker_manager_handle_key(10)
    assert viewer.cursor_time == 96 and viewer._marker_menu is None
    assert viewer._message == 'Marker 97/100 · match · 96ns'


def test_no_matches_are_inert_and_reset_keeps_list_open(viewer, monkeypatch):
    original = [Marker(10, 'first')]
    viewer.markers = original[:]
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, 'missing')
    assert 'No matches' in draw(viewer)
    for key in (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_ENTER, curses.KEY_DC, ord('r')):
        viewer.marker_manager_handle_key(key)
    assert viewer.markers == original and not viewer._undo_stack
    assert viewer._marker_menu['selected'] is None
    viewer.marker_manager_handle_key(21)
    assert viewer._marker_menu is not None
    assert viewer._marker_items() == original
    assert 'No matches' not in draw(viewer)


def test_filtered_edits_refresh_matches_and_undo_keeps_hidden_markers(viewer, monkeypatch):
    kept, hidden, last = Marker(10, 'reset'), Marker(20, 'data'), Marker(30, 'reset')
    viewer.markers = [kept, hidden, last]
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, 'reset')
    viewer.marker_manager_handle_key(curses.KEY_DC)
    assert viewer.markers == [hidden, last]
    assert '1/2 matches' in draw(viewer)
    viewer.marker_manager_handle_key(ord('u'))
    assert viewer.markers == [kept, hidden, last]
    assert '2/3 matches' in draw(viewer)
    # Renaming a selected match can hide it; undo makes it match again.
    with monkeypatch.context() as patch:
        patch.setattr(viewer, 'prompt_input', lambda *a, **kw: 'renamed')
        viewer.marker_manager_handle_key(ord('r'))
    assert '1/3 matches' in draw(viewer)
    viewer.marker_manager_handle_key(ord('u'))
    assert '2/3 matches' in draw(viewer)
    viewer.markers.append(Marker(40, 'reset from analysis'))
    assert '3/4 matches' in draw(viewer)
    viewer.markers = [Marker(50, 'new analysis')]
    assert '0/1 matches' in draw(viewer)


def test_filtered_clear_all_remains_explicit_and_undoable(viewer, monkeypatch):
    original = [Marker(10, 'reset'), Marker(20, 'data')]
    viewer.markers = original[:]
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, 'reset')
    assert 'M Clear all 2' in draw(viewer)
    viewer.marker_manager_handle_key(ord('M'))
    assert viewer.markers == []
    assert '0/0 matches' in draw(viewer)
    viewer.marker_manager_handle_key(ord('u'))
    assert viewer.markers == original
    assert '1/2 matches' in draw(viewer)


def test_mouse_search_reset_and_filtered_row_jump(viewer, monkeypatch):
    viewer.markers = [Marker(10, 'hidden'), Marker(20, 'match'), Marker(30, 'match')]
    viewer.open_marker_manager()

    def click(x, y, state=curses.BUTTON1_CLICKED):
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x, y, 0, state))
        viewer.marker_manager_handle_key(curses.KEY_MOUSE)

    draw(viewer)
    row, x0, _, _ = next(b for b in viewer._marker_menu['buttons'] if b[3] == ord('/'))
    with monkeypatch.context() as patch:
        patch.setattr(viewer, 'prompt_input', lambda *a, **kw: 'match')
        click(x0, row)
    draw(viewer)
    row, x0, _, _ = next(b for b in viewer._marker_menu['buttons'] if b[3] == 21)
    click(x0, row)
    assert viewer._marker_menu['query'] == ''
    filter_list(viewer, monkeypatch, 'match')
    draw(viewer)
    menu = viewer._marker_menu
    left, top, _, _ = menu['rect']
    click(left + 2, top + menu['table_top'] + 1, curses.BUTTON1_DOUBLE_CLICKED)
    assert viewer.cursor_time == 30 and viewer._marker_menu is None
    assert viewer._message == 'Marker 3/3 · match · 30ns'


@pytest.mark.parametrize('size', [(12, 6), (24, 12), (24, 24), (40, 20), (80, 24)])
def test_filtered_layout_survives_long_unicode_query_and_resize(viewer, monkeypatch, size):
    viewer.markers = [Marker(10, '測定 ' * 100)]
    viewer.open_marker_manager()
    filter_list(viewer, monkeypatch, '測定 ' * 100)
    viewer.stdscr = Screen(size[0])
    viewer.stdscr.height = size[1]
    viewer.stdscr.erase()
    viewer.update_layout()
    text = draw(viewer)
    if size[0] >= 24 and size[1] >= 20:
        assert 'Filter:' in text
        assert '1/1' in text
        assert len(viewer._marker_menu['buttons']) == 10
    viewer.marker_manager_handle_key(21)
    draw(viewer)
    viewer.marker_manager_handle_key(27)
    assert viewer._marker_menu is None


def test_run_loop_filters_markers_without_opening_signal_finder(viewer, monkeypatch):
    viewer.markers = [Marker(10, 'hidden'), Marker(20, 'pick'), Marker(60, 'pick')]
    frames = []
    for name in ('curs_set', 'halfdelay'):
        monkeypatch.setattr(curses, name, lambda *a: None)
    monkeypatch.setattr(viewer.stdscr, 'keypad', lambda *a: None, raising=False)
    monkeypatch.setattr(viewer.stdscr, 'refresh', lambda: frames.append(
        '\n'.join(viewer.stdscr.line(i) for i in range(viewer.height))), raising=False)
    keys = iter([curses.KEY_F12, ord('/'), curses.KEY_DOWN, 10, ord('q')])
    monkeypatch.setattr(viewer.stdscr, 'getch', lambda: next(keys), raising=False)
    monkeypatch.setattr(viewer, 'prompt_input', lambda *a, **kw: 'pick')
    monkeypatch.setattr(viewer, '_cleanup_mouse', lambda: None)
    viewer.run()
    assert any('2/3 matches' in frame and 'Filter: pick' in frame for frame in frames)
    assert viewer.cursor_time == 60 and viewer._marker_menu is None
    assert not viewer.show_finder and list(viewer.display_order) == ['!']
