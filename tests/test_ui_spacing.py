"""Spacing keeps neighboring traces, analog heights, and diff lanes separate."""
import curses
import json

import pytest

from alwavelets.core import VCDSignal
from alwavelets.ui_chrome import signal_origin, wave_origin
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


@pytest.fixture
def pair(viewer, monkeypatch):
    monkeypatch.setattr(viewer, 'apply_theme', lambda: None)
    for sid, name, value in [('!', 'low', '0'), ('b', 'high', '1')]:
        sig = VCDSignal(name, sid, 1, scope='top')
        sig.add_change(0, value)
        viewer.vcd.signals[sid] = sig
    viewer.vcd.signal_order = ['!', 'b']
    viewer.display_order = ['!', 'b']
    viewer._sync_display_set()
    viewer._rebuild_view_rows()
    viewer.panel_mode = False
    viewer.cursor_on = False
    viewer.show_grid = 0
    viewer.show_module_sep = False
    return viewer


@pytest.mark.parametrize('style', [1, 2])
@pytest.mark.parametrize('mode,gap', [('compact', 0), ('blank', 1), ('divider', 1)])
def test_modes_separate_low_from_high_without_waveform_shaped_gaps(pair, style, mode, gap):
    v = pair
    v.line_thickness = style
    v.execute_command('spacing ' + mode)
    v.stdscr.erase()
    v.draw_signals()
    first, second = v.wave_top, v.wave_top + 1 + gap
    low, high = ('▁', '▔') if style == 1 else ('▄', '▀')
    left, right = wave_origin(v), v.width - 1
    assert set(v.stdscr.line(first)[left:right]) == {low}
    assert set(v.stdscr.line(second)[left:right]) == {high}
    assert v._signal_hit_rows == {first: 0, second: 1}
    if gap:
        between = ''.join(v.stdscr.rows[first + 1][signal_origin(v):right])
        assert set(between) == ({'┈'} if mode == 'divider' else {' '})


def test_keyboard_and_command_cycle_and_reject_invalid_choice(pair):
    v = pair
    assert (v.signal_spacing, v.signal_divider) == (0, False)
    for expected, label in [((1, False), 'Blank gap'), ((1, True), 'Divider'), ((0, False), 'Compact')]:
        v.keymap[ord('|')]()
        assert (v.signal_spacing, v.signal_divider) == expected
        assert v._message == 'Row spacing: ' + label
    v.execute_command('spacing')
    assert (v.signal_spacing, v.signal_divider) == (1, False)
    v.execute_command('spacing invalid')
    assert (v.signal_spacing, v.signal_divider) == (1, False)
    assert 'compact | blank | divider' in v._message


@pytest.mark.parametrize('height', [1, 3])
@pytest.mark.parametrize('diff', [False, True])
@pytest.mark.parametrize('mode', ['blank', 'divider'])
def test_gap_follows_all_analog_and_comparison_rows(pair, height, diff, mode):
    v = pair
    analog = VCDSignal('analog', '!', 1, signal_type='real', scope='top')
    analog.add_change(0, '0.25')
    analog.add_change(40, '0.75')
    v.vcd.signals['!'] = analog
    v.sig_height['!'] = height
    if diff:
        v.diff_vcd = v.vcd
        v.diff_sig_map['!'] = analog
        v.diff_ranges['!'] = [(0, 10)]
    v.set_signal_spacing(mode)
    v.stdscr.erase()
    v.draw_signals()
    gap_row = v.wave_top + height + int(diff)
    expected = '┈' if mode == 'divider' else ' '
    assert set(v.stdscr.rows[gap_row][signal_origin(v):v.width - 1]) == {expected}
    assert gap_row not in v._signal_hit_rows
    assert v._signal_hit_rows[gap_row + 1] == 1
    assert all(v._signal_hit_rows[row] == 0 for row in range(v.wave_top, gap_row))
    assert all('┈' not in v.stdscr.line(row) for row in range(v.wave_top, gap_row))


def test_mouse_gap_keeps_focus_and_second_signal_is_clickable(pair, monkeypatch):
    v = pair
    v.set_signal_spacing('divider')
    v.draw_signals()
    for offset, expected in [(1, 0), (2, 1)]:
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, wave_origin(v) + 5,
                                                     v.wave_top + offset, 0, curses.BUTTON1_CLICKED))
        v.handle_mouse()
        assert v.focus_idx == expected


@pytest.mark.parametrize('mode', ['compact', 'blank', 'divider'])
def test_named_spacing_survives_session_restore(pair, tmp_path, mode):
    v = pair
    v.vcd._filename = str(tmp_path / 'spacing.vcd')
    v.set_signal_spacing(mode)
    before = v.signal_spacing, v.signal_divider
    v.save_session()
    v.set_signal_spacing('compact' if mode != 'compact' else 'divider')
    assert v.restore_session()
    assert (v.signal_spacing, v.signal_divider) == before


@pytest.mark.parametrize('legacy,gap,divider', [(0, 0, False), (0.25, 1, True), (0.5, 1, True),
                                              (1, 1, False), (2, 2, False), (3, 3, False)])
def test_old_sessions_keep_their_gap_without_block_separators(pair, tmp_path, legacy, gap, divider):
    v = pair
    v.vcd._filename = str(tmp_path / 'legacy.vcd')
    state = v._session_state()
    state['signal_spacing'] = legacy
    del state['signal_divider']
    with open(v.session_path(v.vcd._filename), 'w') as out:
        json.dump(state, out)
    assert v.restore_session()
    v.stdscr.erase()
    v.draw_signals()
    assert v._signal_hit_rows[v.wave_top + 1 + gap] == 1
    for offset in range(gap):
        row = v.stdscr.rows[v.wave_top + 1 + offset][signal_origin(v):v.width - 1]
        assert set(row) == ({'┈'} if divider and offset == 0 else {' '})


@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
@pytest.mark.parametrize('width,height', [(24, 8), (40, 12), (120, 24)])
def test_dividers_fit_resized_workmodes_and_leave_footer_clear(pair, mode, width, height):
    v = pair
    v.workmode = mode
    v.stdscr = Screen(width)
    v.stdscr.height = height
    v.stdscr.erase()
    v.set_signal_spacing('divider')
    v.show_inspector = True
    v.update_layout()
    v.draw_header()
    v.draw_ruler()
    v.draw_signals()
    assert all('┈' not in v.stdscr.line(row) for row in range(v.wave_bottom, height))
    v.draw_status_bar()


def test_horizontal_grid_stays_outside_tall_waveform(pair):
    v = pair
    v.analog_signals.add('!')
    v.sig_height['!'] = 3
    v.set_signal_spacing('blank')
    v.show_grid = 2
    v.draw_signals()
    assert all('┈' not in v.stdscr.line(row) for row in range(v.wave_top, v.wave_top + 3))
    assert '┈' in v.stdscr.line(v.wave_top + 3)
