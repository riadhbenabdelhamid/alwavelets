"""Marker list interaction, reversible edits, and terminal layout."""
import curses

import pytest

from alwavelets.core import Marker
from alwavelets.marker_editing import add_marker, delete_markers, rename_marker
from alwavelets.ui_chrome import wave_origin
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


def rendered(viewer):
    viewer.stdscr.erase()
    viewer.draw_marker_manager()
    return '\n'.join(viewer.stdscr.line(row) for row in range(viewer.height))


def test_entry_points_and_action_palette(viewer):
    viewer.keymap[curses.KEY_F12]()
    assert viewer._marker_menu is not None
    viewer.marker_manager_handle_key(curses.KEY_F12)
    assert viewer._marker_menu is None
    viewer.execute_command('markers')
    assert viewer._marker_menu is not None
    viewer.marker_manager_handle_key(27)
    viewer.open_action_palette()
    for char in 'manage markers':
        viewer.action_palette_handle_key(ord(char))
    viewer.action_palette_handle_key(10)
    assert not viewer.show_actions
    assert viewer._marker_menu is not None


def test_sorted_list_preserves_duplicate_identity_and_capture_order(viewer, monkeypatch):
    late, first, second = Marker(90, 'late'), Marker(10, 'same'), Marker(10, 'same')
    viewer.markers = [late, first, second]
    viewer.cursor_time = 10
    viewer.open_marker_manager()
    assert viewer._marker_items() == [first, second, late]
    assert viewer.markers == [late, first, second]
    viewer.marker_manager_handle_key(curses.KEY_DOWN)
    monkeypatch.setattr(viewer, 'prompt_input', lambda *a, **kw: 'renamed')
    viewer.marker_manager_handle_key(ord('r'))
    assert first.label == 'same' and second.label == 'renamed'
    viewer.marker_manager_handle_key(curses.KEY_DC)
    assert viewer.markers == [late, first]
    viewer.marker_manager_handle_key(ord('u'))
    assert viewer.markers == [late, first, second]
    viewer.marker_manager_handle_key(ord('u'))
    assert second.label == 'same'
    viewer.marker_manager_handle_key(18)
    viewer.marker_manager_handle_key(18)
    assert viewer.markers == [late, first]


def test_jump_reveals_only_active_cursor_and_recenters(viewer):
    viewer.cursor_a, viewer.cursor_b, viewer.active_cursor = 20, 30, 'b'
    viewer.cursor_on = viewer.cursor_b_on = False
    viewer.time_start, viewer.time_end = 0, 40
    viewer.markers = [Marker(90, 'target')]
    viewer.open_marker_manager()
    viewer.marker_manager_handle_key(10)
    assert viewer._marker_menu is None
    assert viewer.cursor_a == 20 and viewer.cursor_b == 90
    assert not viewer.cursor_on and viewer.cursor_b_on
    assert 0 <= viewer.time_to_col(90) < viewer.wave_width - 1
    assert viewer._message == 'Marker 1/1 · target · 90ns'


@pytest.mark.parametrize('active', ['a', 'b'])
@pytest.mark.parametrize('other_visible', [False, True])
@pytest.mark.parametrize('key,start,target', [('.', 10, 60), ('>', 10, 60), (',', 90, 60), ('<', 90, 60)])
def test_shortcut_jump_reveals_active_cursor_and_preserves_other_state(viewer, active, other_visible, key, start, target):
    viewer.markers = [Marker(90, 'last'), Marker(10, 'first'), Marker(60, 'middle')]
    viewer.active_cursor = active
    viewer.cursor_a = viewer.cursor_b = 25
    viewer.cursor_time = start
    viewer.cursor_on = other_visible if active == 'b' else False
    viewer.cursor_b_on = other_visible if active == 'a' else False
    viewer.time_start, viewer.time_end = 0, 40
    viewer.selected_signals = {'!'}
    viewer._redo_stack = [('sentinel', lambda: None)]
    before = viewer.focus_idx, viewer.tree_cursor, list(viewer.markers)
    viewer.keymap[ord(key)]()
    assert viewer.cursor_time == target
    assert (viewer.cursor_on, viewer.cursor_b_on) == ((True, other_visible) if active == 'a' else (other_visible, True))
    assert (viewer.cursor_b if active == 'a' else viewer.cursor_a) == 25
    assert 0 <= viewer.time_to_col(target) < viewer.wave_width - 1
    assert viewer._message == 'Marker 2/3 · middle · 60ns'
    assert (viewer.focus_idx, viewer.tree_cursor, viewer.markers) == before
    assert viewer.selected_signals == {'!'} and len(viewer._redo_stack) == 1
    assert not viewer._undo_stack


def test_navigation_reports_chronological_position_wrap_and_unlabeled_markers(viewer):
    viewer.markers = [Marker(90, 'last'), Marker(10, ''), Marker(60, 'middle')]
    viewer.cursor_time = 60
    for key, message in [('.', 'Marker 3/3 · last · 90ns'),
                         ('.', 'Marker 1/3 · (unlabeled) · 10ns · wrapped'),
                         (',', 'Marker 3/3 · last · 90ns · wrapped'),
                         (',', 'Marker 2/3 · middle · 60ns')]:
        viewer.keymap[ord(key)]()
        assert viewer._message == message
    viewer.markers = [Marker(60, 'only')]
    viewer.jump_next_marker()
    assert viewer._message == 'Marker 1/1 · only · 60ns · wrapped'


def test_duplicate_times_use_list_order_and_shortcuts_keep_moving_in_time(viewer):
    viewer.markers = [Marker(10, 'first'), Marker(10, 'second'), Marker(60, 'later')]
    viewer.open_marker_manager()
    viewer.marker_manager_handle_key(curses.KEY_DOWN)
    viewer.marker_manager_handle_key(10)
    assert viewer._message == 'Marker 2/3 · second · 10ns'
    viewer.jump_next_marker()
    assert viewer._message == 'Marker 3/3 · later · 60ns'
    viewer.jump_prev_marker()
    assert viewer._message == 'Marker 2/3 · second · 10ns'


def test_empty_marker_navigation_explains_how_to_add_without_changing_cursor(viewer):
    viewer.cursor_on = viewer.cursor_b_on = False
    before = viewer.cursor_time, viewer.time_start, viewer.time_end
    for key in (',', '.'):
        viewer.keymap[ord(key)]()
        assert viewer._message == 'No markers · m adds one at the cursor'
        assert not viewer.cursor_on and not viewer.cursor_b_on
        assert (viewer.cursor_time, viewer.time_start, viewer.time_end) == before


@pytest.mark.parametrize('width', [24, 40, 80])
def test_long_unicode_jump_feedback_fits_and_survives_resize(viewer, width):
    viewer.stdscr = Screen(width)
    viewer.update_layout()
    viewer.markers = [Marker(10, '測定 e\u0301 ' * 100 + '\nnext line')]
    viewer.cursor_time = 10
    viewer.jump_next_marker()
    assert '\n' not in viewer._message
    if width >= 40:
        assert '10ns' in viewer._message and 'wrapped' in viewer._message
    viewer.draw_status_bar()
    assert 'Marker 1/1' in viewer.stdscr.line(viewer.height - 1)
    viewer.stdscr = Screen(20)
    viewer.update_layout()
    viewer.draw_status_bar()  # Cell-aware clipping also handles a later resize.


@pytest.mark.parametrize('active', ['a', 'b'])
def test_marker_line_does_not_cover_cursor_after_jump(viewer, active):
    viewer.panel_mode = False
    viewer.show_module_sep = False
    viewer.update_layout()
    viewer.active_cursor = active
    viewer.cursor_on = viewer.cursor_b_on = False
    viewer.markers = [Marker(50, 'edge')]
    viewer.jump_next_marker()
    viewer.draw_ruler()
    viewer.draw_signals()
    col = wave_origin(viewer) + viewer.time_to_col(50)
    assert viewer.stdscr.rows[viewer.ruler_row][col] == active.upper()
    assert viewer.stdscr.rows[viewer.wave_top][col] == '┃'


@pytest.mark.parametrize('width', [24, 40, 80])
def test_popup_explains_selection_and_jump_closure(viewer, width):
    viewer.stdscr = Screen(width)
    viewer.update_layout()
    viewer.markers = [Marker(10, 'first')]
    viewer.open_marker_manager()
    text = rendered(viewer)
    assert '↑↓ Select' in text
    assert ('Enter Jump and close' if width >= 52 else 'Enter Jump/close') in text


def test_rename_cancel_clear_and_noop_preserve_redo(viewer, monkeypatch):
    marker = add_marker(viewer, 10, 'old')
    rename_marker(viewer, marker, 'new')
    viewer.undo()
    viewer.open_marker_manager()
    prompts = []
    answers = iter([None, 'old', ''])

    def prompt(label, initial='', cancel_value=''):
        prompts.append((initial, cancel_value))
        return next(answers)

    monkeypatch.setattr(viewer, 'prompt_input', prompt)
    for _ in range(2):
        viewer.marker_manager_handle_key(ord('r'))
        assert marker.label == 'old' and len(viewer._redo_stack) == 1
    viewer.marker_manager_handle_key(ord('r'))
    assert prompts == [('old', None)] * 3
    assert marker.label == '' and not viewer._redo_stack
    viewer.undo()
    assert marker.label == 'old'
    viewer.redo()
    assert marker.label == ''


def test_add_cancel_unlabeled_and_remote_prompts(viewer, monkeypatch):
    viewer.open_marker_manager()
    with monkeypatch.context() as patch:
        patch.setattr(viewer, 'prompt_input', lambda *a, **kw: None)
        viewer.marker_manager_handle_key(ord('m'))
        assert not viewer.markers and not viewer._undo_stack
        patch.setattr(viewer, 'prompt_input', lambda *a, **kw: '')
        viewer.marker_manager_handle_key(ord('m'))
    marker = viewer.markers[0]
    assert marker.label == '' and marker.time == viewer.cursor_time
    assert viewer._marker_menu['selected'] is marker
    viewer._remote_depth = 1
    viewer.marker_manager_handle_key(ord('m'))
    viewer.marker_manager_handle_key(ord('r'))
    assert viewer.markers == [marker] and len(viewer._undo_stack) == 1
    viewer.undo()
    assert not viewer.markers
    viewer.redo()
    assert viewer.markers == [marker]


@pytest.mark.parametrize('in_menu', [False, True])
def test_clear_all_is_one_reversible_edit_and_empty_clear_preserves_redo(viewer, in_menu):
    original = [Marker(time, str(time)) for time in range(500)]
    viewer.markers = original[:]
    viewer.open_marker_manager()
    clear = (lambda: viewer.marker_manager_handle_key(ord('M'))) if in_menu else viewer.keymap[ord('M')]
    clear()
    assert not viewer.markers and len(viewer._undo_stack) == 1
    for _ in range(3):
        viewer.undo()
        assert viewer.markers == original
        viewer.redo()
        assert not viewer.markers
    viewer.undo()
    viewer.undo()  # No older edit; the redo entry must remain intact.
    assert len(viewer._redo_stack) == 1
    viewer.markers = []
    clear()
    assert len(viewer._redo_stack) == 1


@pytest.mark.parametrize('indices', [(1,), (0, 2), (0, 1, 2)])
def test_history_keeps_unrelated_markers_added_by_analysis(viewer, indices):
    original = [Marker(t, str(t)) for t in (30, 10, 20)]
    viewer.markers = original[:]
    removed = [original[i] for i in indices]
    delete_markers(viewer, removed)
    external = Marker(40, 'analysis')
    viewer.markers.append(external)
    for _ in range(3):
        viewer.undo()
        assert viewer.markers == original + [external]
        viewer.redo()
        assert viewer.markers == [m for m in original if m not in removed] + [external]


def test_marker_signal_and_script_edits_share_history(viewer):
    marker = add_marker(viewer, 20, 'first')
    viewer.cycle_signal_color()
    colors = dict(viewer.sig_color)
    viewer.tcl.execute('marker 40 scripted')
    scripted = viewer.markers[-1]
    for _ in range(2):
        viewer.undo()
        assert viewer.markers == [marker] and viewer.sig_color == colors
        viewer.undo()
        assert not viewer.sig_color
        viewer.undo()
        assert not viewer.markers
        viewer.redo()
        viewer.redo()
        viewer.redo()
        assert viewer.markers == [marker, scripted] and viewer.sig_color == colors


def test_paging_deletion_and_external_list_replacement(viewer):
    viewer.markers = [Marker(t, 'marker ' + str(t)) for t in range(500)]
    viewer.open_marker_manager()
    rendered(viewer)
    rows = viewer._marker_menu['rows']
    viewer.marker_manager_handle_key(curses.KEY_NPAGE)
    assert viewer._marker_menu['index'] == rows
    viewer.marker_manager_handle_key(curses.KEY_END)
    assert 'marker 499' in rendered(viewer)
    viewer.marker_manager_handle_key(curses.KEY_DC)
    assert '499/499' in rendered(viewer)
    assert viewer._marker_menu['selected'].time == 498
    viewer.marker_manager_handle_key(curses.KEY_HOME)
    rendered(viewer)
    assert viewer._marker_menu['scroll'] == 0
    viewer.markers = [Marker(50, 'new analysis')]
    assert 'new analysis' in rendered(viewer)
    assert viewer._marker_menu['selected'] is viewer.markers[0]
    viewer.marker_manager_handle_key(ord('M'))
    assert 'No markers' in rendered(viewer)
    viewer.marker_manager_handle_key(10)
    viewer.marker_manager_handle_key(curses.KEY_UP)
    assert viewer._marker_menu is not None


def test_mouse_selection_footer_wheel_and_outside_dismissal(viewer, monkeypatch):
    viewer.markers = [Marker(10, 'first'), Marker(70, 'second')]
    viewer.open_marker_manager()
    rendered(viewer)

    def click(x, y, state=curses.BUTTON1_CLICKED):
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x, y, 0, state))
        viewer.marker_manager_handle_key(curses.KEY_MOUSE)

    left, top, _, _ = viewer._marker_menu['rect']
    click(left + 2, top + 3)
    assert viewer._marker_menu['selected'].time == 70
    click(left + 2, top + 3, curses.BUTTON4_PRESSED)
    assert viewer._marker_menu['selected'].time == 10
    row, x0, _, _ = next(b for b in viewer._marker_menu['buttons'] if b[3] == curses.KEY_DC)
    click(x0, row)
    assert [m.time for m in viewer.markers] == [70]
    rendered(viewer)
    left, top, _, _ = viewer._marker_menu['rect']
    click(left + 2, top + 2, curses.BUTTON1_DOUBLE_CLICKED)
    assert viewer.cursor_time == 70 and viewer._marker_menu is None
    viewer.open_marker_manager()
    rendered(viewer)
    click(0, 0)
    assert viewer._marker_menu is None and viewer.cursor_time == 70


@pytest.mark.parametrize('mode', ['calm', 'classic', 'minimal'])
@pytest.mark.parametrize('size', [(8, 4), (12, 6), (24, 12), (40, 20), (80, 24), (160, 40)])
def test_draw_is_bounded_and_opaque_across_modes_and_sizes(viewer, mode, size):
    width, height = size
    viewer.workmode = mode
    viewer.stdscr = Screen(width)
    viewer.stdscr.height = height
    viewer.stdscr.erase()
    viewer.update_layout()
    viewer.markers = [Marker(10**30, '測定 e\u0301 ' * 100)]
    viewer.open_marker_manager()
    for row in range(height):
        viewer.stdscr.addstr(row, 0, '~' * width)
    viewer.draw_marker_manager()
    menu = viewer._marker_menu
    if menu['rect']:
        left, top, w, h = menu['rect']
        assert all('~' not in viewer.stdscr.rows[row][left:left + w] for row in range(top, top + h))
    if width >= 40:
        assert len(menu['buttons']) == 9
    viewer.marker_manager_handle_key(27)
    assert viewer._marker_menu is None


def test_edited_markers_persist_in_session(viewer, tmp_path, monkeypatch):
    monkeypatch.setattr(viewer, 'apply_theme', lambda: None)
    viewer.vcd._filename = str(tmp_path / 'markers.vcd')
    kept = add_marker(viewer, 12, 'old')
    removed = add_marker(viewer, 25, 'removed')
    rename_marker(viewer, kept, 'saved label')
    delete_markers(viewer, [removed])
    viewer.save_session()
    viewer.markers = []
    viewer.open_marker_manager()
    assert viewer.restore_session()
    assert [(m.time, m.label) for m in viewer.markers] == [(12, 'saved label')]
    assert viewer._marker_menu is None and not viewer._undo_stack


def test_run_loop_routes_marker_keys_before_waveform_actions(viewer, monkeypatch):
    viewer.markers = [Marker(10, 'first'), Marker(60, 'second')]
    viewer.focus_idx = 0
    frames = []
    for name in ('curs_set', 'halfdelay'):
        monkeypatch.setattr(curses, name, lambda *a: None)
    monkeypatch.setattr(viewer.stdscr, 'keypad', lambda *a: None, raising=False)
    monkeypatch.setattr(viewer.stdscr, 'refresh', lambda: frames.append(
        '\n'.join(viewer.stdscr.line(i) for i in range(viewer.height))), raising=False)
    keys = iter([curses.KEY_F12, ord('d'), ord('u'), 18, curses.KEY_RESIZE,
                 curses.KEY_ENTER, ord('q')])
    monkeypatch.setattr(viewer.stdscr, 'getch', lambda: next(keys), raising=False)
    monkeypatch.setattr(viewer, '_cleanup_mouse', lambda: None)
    viewer.run()
    assert any('Markers' in frame and 'Rename' in frame for frame in frames)
    assert list(viewer.display_order) == ['!']
    assert viewer.cursor_time == 60 and len(viewer.markers) == 1
