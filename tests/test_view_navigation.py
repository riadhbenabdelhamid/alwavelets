"""Zoom anchors, precise mouse times, and grids that preserve waveform data."""
import curses

import pytest

from alwavelets.core import VCDSignal
from alwavelets.ui_chrome import wave_origin
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


class AttributeScreen(Screen):
    def erase(self):
        super().erase()
        self.attrs = {}

    def addstr(self, row, col, text, attr=0):
        super().addstr(row, col, text, attr)
        for i in range(len(text)):
            self.attrs[row, col + i] = attr

    addch = addstr


@pytest.mark.parametrize('offset', [1000, (1 << 53) + 3])
@pytest.mark.parametrize('active', ['a', 'b'])
@pytest.mark.parametrize('factor', [0.5, 2])
def test_zoom_preserves_active_cursor_column(viewer, offset, active, factor):
    viewer.time_start, viewer.time_end, viewer.wave_width = offset, offset + 1000, 100
    viewer.cursor_a, viewer.cursor_b = offset + 200, offset + 800
    viewer.active_cursor, viewer.cursor_on, viewer.cursor_b_on = active, True, True
    column = viewer.time_to_col(viewer.cursor_time)
    viewer.zoom(factor)
    assert viewer.time_to_col(viewer.cursor_time) == column
    assert viewer.time_end - viewer.time_start == int(1000 * factor)
    assert (viewer.cursor_a, viewer.cursor_b) == (offset + 200, offset + 800)
    viewer.zoom(1 / factor)
    assert (viewer.time_start, viewer.time_end) == (offset, offset + 1000)


@pytest.mark.parametrize('cursor_on,cursor', [(False, 1200), (True, 100), (True, 9000)])
def test_zoom_uses_view_center_when_cursor_is_hidden_or_offscreen(viewer, cursor_on, cursor):
    viewer.time_start, viewer.time_end = 1000, 2000
    viewer.cursor_on, viewer.cursor_a = cursor_on, cursor
    viewer.zoom(0.5)
    assert (viewer.time_start, viewer.time_end) == (1250, 1750)


def test_zoom_clamps_at_zero_and_does_not_move_at_minimum_span(viewer):
    viewer.time_start, viewer.time_end, viewer.cursor_a = 0, 100, 80
    viewer.zoom(2)
    assert (viewer.time_start, viewer.time_end) == (0, 200)
    for span in (5, 10):
        viewer.time_start, viewer.time_end, viewer.cursor_a = 100, 100 + span, 103
        viewer.zoom(0.5)
        assert (viewer.time_start, viewer.time_end) == (100, 100 + span)


def test_zoom_preserves_integer_ticks_for_very_wide_windows(viewer):
    span = (1 << 54) + 123
    viewer.time_start, viewer.time_end, viewer.cursor_a = 1000, 1000 + span, 1000
    viewer.zoom(2)
    assert (viewer.time_start, viewer.time_end) == (1000, 1000 + 2 * span)


@pytest.mark.parametrize('offset', [0, (1 << 53) + 3, (1 << 63) - 1000])
def test_mouse_clicks_and_markers_keep_exact_ticks(viewer, monkeypatch, offset):
    viewer.time_start, viewer.time_end, viewer.wave_width = offset, offset + 43, 13
    ticks = [0, 3, 6, 9, 13, 16, 19, 23, 26, 29, 33, 36, 39]
    for col, delta in enumerate(ticks):
        for button in (curses.BUTTON1_CLICKED, curses.BUTTON3_CLICKED):
            monkeypatch.setattr(curses, 'getmouse', lambda: (
                0, wave_origin(viewer) + col, viewer.wave_top, 0, button))
            viewer.handle_mouse()
            timestamp = viewer.cursor_time if button == curses.BUTTON1_CLICKED else viewer.markers[-1].time
            assert timestamp == offset + delta
            assert viewer.time_to_col(timestamp) == col


@pytest.mark.parametrize('button,factor', [(curses.BUTTON4_PRESSED, 0.5), (1 << 21, 2)])
def test_mouse_wheel_keeps_time_under_pointer(viewer, monkeypatch, button, factor):
    offset = (1 << 53) + 3
    viewer.time_start, viewer.time_end, viewer.wave_width = offset, offset + 1000, 100
    viewer.cursor_a = offset + 800
    monkeypatch.setattr(curses, 'getmouse', lambda: (
        0, wave_origin(viewer) + 20, viewer.wave_top, 0, button))
    viewer.handle_mouse()
    assert viewer.col_to_time(20) == offset + 200
    assert viewer.cursor_a == offset + 800
    assert viewer.time_end - viewer.time_start == int(1000 * factor)


def _screen(viewer, monkeypatch, width=120, mode='classic'):
    monkeypatch.setattr(viewer, 'apply_theme', lambda: None)
    viewer.apply_workmode(mode, notify=False)
    viewer.stdscr = AttributeScreen(width)
    viewer.cursor_on = viewer.cursor_b_on = False
    viewer.update_layout()


@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
@pytest.mark.parametrize('width', [40, 80, 120])
def test_grid_uses_ruler_columns_after_pan_zoom_and_resize(viewer, monkeypatch, mode, width):
    _screen(viewer, monkeypatch, width, mode)
    sig = VCDSignal('steady', '!', 1)
    sig.add_change(0, '0')
    viewer.vcd.signals['!'] = sig
    viewer.time_start, viewer.time_end = 17, 317
    viewer.show_grid = 1
    for factor in (1, 0.5, 2):
        viewer.zoom(factor)
        viewer.stdscr.erase()
        viewer.draw_ruler()
        viewer.draw_signals()
        origin = wave_origin(viewer)
        tick_cols = {col for col in range(origin, viewer.width - 1)
                     if viewer.stdscr.rows[viewer.ruler_row][col] == '┬'}
        grid_cols = {col for col in range(origin, viewer.width - 1)
                     if viewer.stdscr.attrs[viewer.wave_top, col] & curses.A_DIM}
        assert tick_cols
        assert grid_cols == tick_cols


@pytest.mark.parametrize('kind', ['bit', 'subcell', 'bus', 'analog', 'braille'])
def test_grid_preserves_waveform_glyphs_labels_and_cached_rows(viewer, monkeypatch, kind):
    _screen(viewer, monkeypatch)
    bus = kind == 'bus'
    analog = kind in ('analog', 'braille')
    sig = VCDSignal('value', '!', 8 if bus or analog else 1)
    for tick, value in [(0, '0'), (20, '1'), (40, 'x'), (60, 'z'), (80, '0')]:
        sig.add_change(tick, value * sig.size)
    viewer.vcd.signals['!'] = sig
    viewer.time_start, viewer.time_end = 0, 100
    viewer.line_thickness = 2 if kind in ('subcell', 'braille') else 1
    if analog:
        viewer.analog_signals = {'!'}
        viewer.sig_height['!'] = 3
    viewer.show_grid = 0
    viewer.draw_ruler()
    viewer.draw_signals()
    rows = range(viewer.wave_top, viewer.wave_top + (3 if analog else 1))
    origin = wave_origin(viewer)
    before = {(row, col): viewer.stdscr.rows[row][col]
              for row in rows for col in range(origin, viewer.width - 1)}
    attrs = dict(viewer.stdscr.attrs)
    cached = dict(viewer._row_cache)
    viewer.show_grid = 1
    viewer.stdscr.erase()
    viewer.draw_ruler()
    viewer.draw_signals()
    for (row, col), ch in before.items():
        after = viewer.stdscr.rows[row][col]
        if ch != ' ':
            assert after == ch  # Includes edges, x/z states and bus labels.
        elif after != ' ':
            assert after == '┊'
            assert viewer.stdscr.rows[viewer.ruler_row][col] == '┬'
        if ch in ('│', '▓', '░', '╳', '▚', '▞'):
            assert viewer.stdscr.attrs[row, col] == attrs[row, col]
    assert viewer._row_cache == cached
    viewer.show_grid = 0
    viewer.stdscr.erase()
    viewer.draw_signals()
    assert all(viewer.stdscr.rows[row][col] == ch for (row, col), ch in before.items())
