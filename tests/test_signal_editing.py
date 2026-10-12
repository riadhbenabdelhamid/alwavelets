"""Signal menus and reversible edits exercise the real viewer and keymap."""
import copy
import curses

import pytest

from alwavelets.core import Radix, VCDSignal
from alwavelets.ui_editing import apply_format
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


@pytest.fixture
def signals(viewer, monkeypatch):
    monkeypatch.setattr(viewer, 'apply_theme', lambda *a: None)
    for sid in ('b', 'c', 'd'):
        viewer.vcd.signals[sid] = VCDSignal(sid, sid, 8, scope='top')
        viewer.vcd.signal_order.append(sid)
        viewer.display_order.append(sid)
    viewer._sync_display_set()
    viewer._rebuild_view_rows()
    return viewer


def state(v):
    return (list(v.display_order), copy.deepcopy(v.groups), dict(v.sig_radix),
            dict(v.sig_color), dict(v.sig_height), set(v.analog_signals))


def choose(v, action, value=None):
    items = v.signal_menu_items()
    v._signal_menu['index'] = next(i for i, (_, a, val) in enumerate(items)
                                  if a == action and val == value)
    v.signal_menu_handle_key(curses.KEY_ENTER)


def test_mixed_edits_roundtrip_undo_redo_repeatedly(signals):
    v = signals
    states = [state(v)]
    actions = [lambda: v.move_signal(1), v.cycle_signal_color, v.cycle_signal_radix,
               v.cycle_signal_height, v._kb_toggle_analog, v.delete_signal,
               lambda: v._cmd_group('pair top.b top.d'), lambda: v._cmd_ungroup('pair'),
               lambda: v.remove_signals('top.c'), lambda: v.add_signals('top.c')]
    for action in actions:
        action()
        states.append(state(v))
    assert len(v._undo_stack) == len(actions)
    for _ in range(2):
        for expected in reversed(states[:-1]):
            v.undo()
            assert state(v) == expected
        for expected in states[1:]:
            v.redo()
            assert state(v) == expected


@pytest.mark.parametrize('prop,key', [('radix', 'R'), ('color', 'v'), ('height', 'H')])
def test_selection_format_restores_mixed_values_and_explicit_defaults(signals, prop, key):
    v = signals
    v.selected_signals = {'!', 'b', 'c', 'stale'}
    mapping = getattr(v, 'sig_' + prop)
    mapping.update({'b': Radix.BIN if prop == 'radix' else 1,
                    'c': Radix.HEX if prop == 'radix' else 3})
    before = dict(mapping)
    v.keymap[ord(key)]()
    after = dict(mapping)
    assert set(mapping) == {'!', 'b', 'c'}
    assert len(set(mapping.values())) == 1
    assert '3 signal(s)' in v._message
    assert len(v._undo_stack) == 1
    v.keymap[ord('u')]()
    assert mapping == before
    v._focus_on('d')
    v.selected_signals = {'d'}
    v.keymap[18]()
    assert mapping == after  # Replay original targets, not the new selection.
    assert 'd' not in mapping


def test_unselected_focus_does_not_get_formatted(signals):
    v = signals
    v.selected_signals = {'b', 'c'}
    v.sig_color['b'] = 3
    v.cycle_signal_color()
    assert v.sig_color == {'b': 4, 'c': 4}
    v.selected_signals = {'stale'}
    v.cycle_signal_color()
    assert v.sig_color == {'b': 4, 'c': 4}


def test_redo_branch_and_noop_behavior(signals):
    v = signals
    v.cycle_signal_color()
    v.cycle_signal_height()
    v.undo()
    v.undo()
    assert len(v._redo_stack) == 2
    # A redundant explicit default must not discard forward history.
    apply_format(v, 'color', None)
    assert len(v._redo_stack) == 2
    v.zoom(0.5)
    assert len(v._redo_stack) == 2
    v.cycle_signal_radix()
    assert not v._redo_stack
    v.execute_command('redo')
    assert v._message == 'Nothing to redo'
    v.execute_command('undo')
    v.execute_command('redo')
    assert v.sig_radix


def test_new_hierarchy_edit_and_session_restore_clear_redo(signals, tmp_path):
    v = signals
    v.cycle_signal_color()
    v.undo()
    v._tree_remove_scope_signals(v.scope_tree.children['top'])
    assert not v._redo_stack
    v.vcd._filename = str(tmp_path / 'trace.vcd')
    v.save_session()
    v._undo_stack = [('sentinel', lambda: None)]
    v._redo_stack = [('sentinel', lambda: None)]
    assert v.restore_session()
    assert not v._undo_stack and not v._redo_stack


@pytest.mark.parametrize('command', ['radix bin', 'analog clk'])
def test_script_edits_discard_redo(signals, command):
    signals.cycle_signal_color()
    signals.undo()
    signals.tcl.execute(command)
    assert not signals._redo_stack


def test_group_adds_missing_rows_as_one_reversible_edit(signals):
    v = signals
    v.display_order = ['!']
    v._sync_display_set()
    v._rebuild_view_rows()
    before = state(v)
    v._cmd_group('pair top.b top.c')
    after = state(v)
    v.undo()
    assert state(v) == before
    v.redo()
    assert state(v) == after


def test_finder_add_is_redoable(signals):
    v = signals
    v.remove_signals('top.b')
    v.open_finder()
    try:
        v.finder_marked = {'b'}
        v._finder_commit()
        added = state(v)
        v.undo()
        assert 'b' not in v.display_order
        v.redo()
        assert state(v) == added
    finally:
        v.close_finder()


def test_popup_explicit_choice_and_default_are_one_edit(signals):
    v = signals
    v.selected_signals = {'!', 'b'}
    v.keymap[curses.KEY_F10]()
    assert v._signal_menu['title'] == '2 selected signals'
    assert not any(a == 'bus' for _, a, _ in v.signal_menu_items())
    choose(v, 'page', 'radix')
    choose(v, 'radix', Radix.UNSIGNED)
    assert v._signal_menu is None
    assert v.sig_radix == {'!': Radix.UNSIGNED, 'b': Radix.UNSIGNED}
    v.execute_command('signal-menu')
    choose(v, 'page', 'radix')
    assert any(label.startswith('✓') and value == Radix.UNSIGNED
               for label, _, value in v.signal_menu_items())
    choose(v, 'radix', None)
    assert not v.sig_radix
    v.undo()
    assert len(v.sig_radix) == 2


def test_popup_group_preserves_members_and_remove_is_reversible(signals, monkeypatch):
    v = signals
    v._cmd_group('existing top.b')
    v.selected_signals = {'c', 'd'}
    before = state(v)
    monkeypatch.setattr(v, 'prompt_input', lambda *a: 'existing')
    v.open_signal_menu()
    choose(v, 'group')
    assert v._group_by_name['existing']['members'] == {'b', 'c', 'd'}
    after = state(v)
    v.undo()
    assert state(v) == before
    v.redo()
    assert state(v) == after
    v.open_signal_menu()
    choose(v, 'remove')
    assert 'c' not in v.display_order and 'd' not in v.display_order
    v.undo()
    assert state(v) == after
    v.redo()
    assert 'c' not in v.display_order and 'd' not in v.display_order


def test_popup_mouse_targets_clicked_label_and_preserves_waveform_marker(signals, monkeypatch):
    from alwavelets.ui_chrome import signal_origin, wave_origin
    v = signals
    v.draw_signals()
    v.selected_signals = {'!', 'b'}

    def click(x, y):
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x, y, 0, curses.BUTTON3_CLICKED))
        v.handle_mouse()

    click(signal_origin(v), v.wave_top + 1)
    assert v._signal_menu['targets'] == ['!', 'b']
    v.signal_menu_handle_key(27)
    click(signal_origin(v), v.wave_top + 2)
    assert v._signal_menu['targets'] == ['c']
    assert v.focused_signal_id() == 'c'
    assert not v.selected_signals
    v.signal_menu_handle_key(27)
    click(wave_origin(v) + 2, v.wave_top + 2)
    assert v._signal_menu is None and len(v.markers) == 1


def test_menu_dismissal_never_moves_cursors_or_runs_waveform_keys(signals, monkeypatch):
    v = signals
    v.open_signal_menu()
    v.draw_signal_menu()
    original = state(v), v.cursor_time
    v.signal_menu_handle_key(ord('d'))
    assert (state(v), v.cursor_time) == original
    monkeypatch.setattr(curses, 'getmouse', lambda: (0, 79, 0, 0, curses.BUTTON1_CLICKED))
    v.signal_menu_handle_key(curses.KEY_MOUSE)
    assert v._signal_menu is None
    assert (state(v), v.cursor_time) == original


def test_menu_bus_and_keyboard_back(signals):
    v = signals
    v.open_signal_menu()
    assert not any(a == 'bus' for _, a, _ in v.signal_menu_items())
    v.signal_menu_handle_key(27)
    v._focus_on('b')
    v.open_signal_menu()
    choose(v, 'page', 'color')
    v.signal_menu_handle_key(27)
    assert v._signal_menu['page'] == '' and v._signal_menu['index'] == 1
    choose(v, 'bus')
    assert len(v.expanded_buses['b']) == 8
    v.open_signal_menu()
    assert any(label == 'Collapse bus' for label, _, _ in v.signal_menu_items())
    choose(v, 'bus')
    assert not v.expanded_buses


@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
@pytest.mark.parametrize('size', [(120, 30), (40, 10), (24, 6), (8, 4)])
def test_menu_layout_handles_resize_and_unicode(signals, mode, size):
    v = signals
    v.vcd.signals['!'].name = '信号' * 25
    v.apply_workmode(mode)
    v.open_signal_menu(119, 29)
    v.stdscr = Screen(size[0])
    v.stdscr.height = size[1]
    v.stdscr.erase()
    v.update_layout()
    for page in ('', 'radix', 'color', 'height'):
        v._signal_menu.update(page=page, index=0, scroll=0)
        v.signal_menu_handle_key(curses.KEY_END)
        v.draw_signal_menu()
        assert v._signal_menu['index'] == len(v.signal_menu_items()) - 1
        x, y, width, height = v._signal_menu['rect']
        assert x + width <= size[0] and y + height <= size[1]


def test_menu_and_formatting_do_not_read_waveform_history(signals, monkeypatch):
    def fail(*a, **kw):
        pytest.fail('Editing read waveform history')
    monkeypatch.setattr(VCDSignal, 'value_at', fail)
    monkeypatch.setattr(VCDSignal, 'num_changes', fail)
    signals.selected_signals = {'!', 'b'}
    signals.open_signal_menu()
    choose(signals, 'page', 'height')
    choose(signals, 'height', 4)
    signals.undo()
    signals.redo()
    assert signals.sig_height == {'!': 4, 'b': 4}


def test_popup_mouse_choice_and_escape_from_submenu(signals, monkeypatch):
    v = signals
    v.open_signal_menu()
    v.draw_signal_menu()

    def click(index):
        x, y, _, _ = v._signal_menu['rect']
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x + 2, y + index + 1, 0, curses.BUTTON1_CLICKED))
        v.signal_menu_handle_key(curses.KEY_MOUSE)
        if v._signal_menu:
            v.draw_signal_menu()

    click(0)  # Number format
    assert v._signal_menu['page'] == 'radix'
    click(3)  # Signed decimal
    assert v.sig_radix == {'!': Radix.DEC}
    assert v._signal_menu is None


def test_run_loop_routes_modal_keys_and_draws_menu_last(signals, monkeypatch):
    v = signals
    frames = []
    for name in ('curs_set', 'halfdelay'):
        monkeypatch.setattr(curses, name, lambda *a: None)
    monkeypatch.setattr(v.stdscr, 'keypad', lambda *a: None, raising=False)
    monkeypatch.setattr(v.stdscr, 'refresh', lambda: frames.append(
        '\n'.join(v.stdscr.line(i) for i in range(v.height))), raising=False)
    # Choose Float 64 explicitly; configurable slots follow it in the menu.
    keys = iter([curses.KEY_F10, ord('d'), curses.KEY_RIGHT, curses.KEY_HOME,
                 *([curses.KEY_DOWN] * 8), curses.KEY_ENTER, ord('u'), 18, ord('q')])
    monkeypatch.setattr(v.stdscr, 'getch', lambda: next(keys), raising=False)
    monkeypatch.setattr(v, '_cleanup_mouse', lambda: None)
    v.run()
    assert any('Number format' in frame for frame in frames)
    assert any('Float 64' in frame for frame in frames)
    assert v.sig_radix == {'!': Radix.FLOAT64}
    assert list(v.display_order) == ['!', 'b', 'c', 'd']


def test_disk_catalog_edits_keep_compact_order_and_never_enumerate(tmp_path, monkeypatch):
    from alwavelets.display_order import DisplayOrder
    from alwavelets.storage import open_trace
    from test_display_order import make_trace, viewer as disk_viewer

    with open_trace(make_trace(tmp_path / 'large.vcd', count=10000), use_cache=False) as trace:
        v = disk_viewer(trace, monkeypatch)
        def fail(*a):
            pytest.fail('An edit enumerated the entire catalog order')
        monkeypatch.setattr(DisplayOrder, '__iter__', fail)
        v.selected_signals = {'s2', 's35', 's9999'}
        v.open_signal_menu()
        choose(v, 'page', 'color')
        choose(v, 'color', 3)
        v.undo()
        v.redo()
        assert v.sig_color == {'s2': 3, 's35': 3, 's9999': 3}
        v._kb_backspace_remove()
        assert len(v.display_order) == 9997
        v.undo()
        assert len(v.display_order) == 10000 and len(v.display_order.parts) == 1
        v.redo()
        assert len(v.display_order) == 9997
