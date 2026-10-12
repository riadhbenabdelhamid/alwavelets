"""Navigation recovery, optional details, and honest empty/loading states."""
import curses
import threading

import pytest

from alwavelets.core import Marker, VCDSignal
from alwavelets.ui import Alwavelets
from alwavelets.ui_chrome import wave_bottom
from alwavelets.ui_navigation import set_time_range
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture
APPLY_CONFIG = Alwavelets._apply_config


def paint(viewer):
    viewer.stdscr.erase()
    viewer.prepare_inspector()
    viewer.draw_header()
    viewer.draw_overview()
    viewer.draw_ruler()
    viewer.draw_signals()
    viewer.draw_panel_tree()
    viewer.draw_inspector()
    viewer.draw_status_bar()


def settle(viewer):
    queries = viewer._queries
    for future in list(queries.pending.values()):
        future.result(timeout=3)
    queries.poll()


def test_history_covers_zoom_pan_goto_and_search_jumps(viewer):
    original = viewer.time_start, viewer.time_end
    viewer.zoom(0.5)
    zoomed = viewer.time_start, viewer.time_end
    viewer.cursor_on = False
    viewer.scroll_time(1)
    panned = viewer.time_start, viewer.time_end
    viewer.execute_command('goto 200')
    jumped = viewer.time_start, viewer.time_end
    viewer.vcd.signals['!'].add_change(300, '0')
    viewer.jump_next_edge()
    assert viewer.time_start <= 300 <= viewer.time_end
    viewer.cursor_a = 301
    for expected in (jumped, panned, zoomed, original):
        viewer.view_back()
        assert (viewer.time_start, viewer.time_end) == expected
        assert viewer.cursor_a == 301  # Range history does not move cursors.
    for expected in (zoomed, panned, jumped):
        viewer.view_forward()
        assert (viewer.time_start, viewer.time_end) == expected


def test_history_branches_noops_limit_and_live_follow(viewer):
    for i in range(60):
        set_time_range(viewer, i + 1, i + 101)
    assert len(viewer.zoom_history) == 50
    viewer.follow_on = True
    viewer.keymap[curses.KEY_F8]()
    assert not viewer.follow_on
    forward = list(viewer.zoom_forward)
    set_time_range(viewer, viewer.time_start, viewer.time_end)
    assert viewer.zoom_forward == forward
    viewer.execute_command('zoom 99 10')
    assert viewer.zoom_forward == forward  # Invalid input is atomic.
    before = viewer.time_start, viewer.time_end
    viewer.execute_command('zoom 400 500')
    assert not viewer.zoom_forward
    viewer.execute_command('back')
    assert (viewer.time_start, viewer.time_end) == before
    viewer.execute_command('forward')
    assert (viewer.time_start, viewer.time_end) == (400, 500)


def test_fit_and_tcl_share_view_history_and_large_integer_times(viewer):
    offset = (1 << 60) + 19
    set_time_range(viewer, offset, offset + 100)
    viewer.tcl.execute(f'zoom {offset + 10} {offset + 40}')
    viewer.zoom_fit_all()
    viewer.view_back()
    assert (viewer.time_start, viewer.time_end) == (offset + 10, offset + 40)
    viewer.view_back()
    assert (viewer.time_start, viewer.time_end) == (offset, offset + 100)
    assert viewer.time_end - viewer.time_start == 100


@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
@pytest.mark.parametrize('width,height', [(24, 12), (40, 24), (80, 24), (120, 30)])
def test_optional_panels_keep_drawing_and_focus_inside_layout(viewer, monkeypatch, mode, width, height):
    monkeypatch.setattr(viewer, 'apply_theme', lambda: None)
    viewer.stdscr = Screen(width)
    viewer.stdscr.height = height
    viewer.apply_workmode(mode, notify=False)
    for i in range(35):
        viewer.display_order.append('!')
    viewer.focus_idx = viewer.visible_signals - 1
    focused = viewer.sig_offset + viewer.focus_idx
    try:
        for panel in (viewer.toggle_inspector, viewer.toggle_overview):
            panel()
            assert viewer.sig_offset + viewer.focus_idx == focused
            assert 0 <= viewer.focus_idx < viewer.visible_signals
        paint(viewer)
        assert viewer.overview_row < viewer.ruler_row
        assert 'Signal inspector' in viewer.stdscr.line(wave_bottom(viewer))
        assert max(viewer._signal_hit_rows) < wave_bottom(viewer)
        rows = [row[:] for row in viewer.stdscr.rows[wave_bottom(viewer):]]
        viewer.draw_signals()
        viewer.draw_panel_tree()
        assert rows == viewer.stdscr.rows[wave_bottom(viewer):]
    finally:
        if hasattr(viewer, '_queries'):
            viewer._queries.close()


def test_inspector_uses_only_focused_signal_and_refreshes_cursor_values(viewer, monkeypatch):
    sig = viewer.vcd.signals['!']
    class NoScan(dict):
        def values(self):
            pytest.fail('Inspector scanned all signals')
        def __iter__(self):
            pytest.fail('Inspector scanned all signals')
    monkeypatch.setattr(viewer.vcd, 'signals', NoScan(viewer.vcd.signals))
    viewer.show_inspector = True
    viewer.cursor_a, viewer.cursor_b, viewer.cursor_b_on = 25, 75, True
    viewer.update_layout()
    try:
        assert viewer.inspector_details('!', sig) is None
        settle(viewer)
        assert viewer.inspector_details('!', sig) == {'a': '0', 'b': '1', 'transitions': 2}
        viewer.draw_inspector()
        text = '\n'.join(viewer.stdscr.line(r) for r in range(wave_bottom(viewer), viewer.height - 1))
        assert 'top.clk' in text and '1 bit' in text
        assert 'A 25ns = 0' in text and 'B 75ns = 1' in text
        assert 'Δ A–B: 50ns' in text
        viewer.cursor_a = 80
        assert viewer.inspector_details('!', sig) is None
        settle(viewer)
        assert viewer.inspector_details('!', sig)['a'] == '1'
        viewer.toggle_inspector()
        assert not any(token[0] == 'inspector' for token in viewer._queries.pending)
    finally:
        viewer._queries.close()


def test_inspector_does_not_load_missing_lazy_ranges_or_report_partial_measurements(viewer, monkeypatch):
    viewer.show_inspector = True
    viewer.vcd._lazy_mode = True
    viewer.vcd._loaded_ranges = [(0, 40)]
    viewer.cursor_a, viewer.cursor_b, viewer.cursor_b_on = 20, 75, True
    monkeypatch.setattr(viewer.vcd, 'ensure_loaded', lambda *args: pytest.fail('Inspector expanded lazy loading'))
    sig = viewer.vcd.signals['!']
    try:
        viewer.inspector_details('!', sig)
        settle(viewer)
        result = viewer.inspector_details('!', sig)
        assert result == {'a': '0', 'b': None, 'transitions': None}
        viewer.vcd._loading = True
        assert 'Loading' in viewer.inspector_details('!', sig)['status']
    finally:
        viewer._queries.close()


def test_changed_focus_cancels_pending_inspector_read(viewer, monkeypatch):
    from alwavelets.streaming import check_read_cancel
    entered, release = threading.Event(), threading.Event()
    sig = viewer.vcd.signals['!']
    original = VCDSignal.value_at
    def delayed(signal, tick):
        entered.set()
        assert release.wait(3)
        check_read_cancel()
        return original(signal, tick)
    monkeypatch.setattr(VCDSignal, 'value_at', delayed)
    viewer.show_inspector = True
    try:
        viewer.inspector_details('!', sig)
        assert entered.wait(3)
        cancellation = next(iter(viewer._queries.cancellations.values()))
        viewer.cursor_a = 99
        viewer.prepare_inspector()
        assert cancellation.is_set()
        release.set()
        viewer._queries.close()
        assert not viewer._queries.ready
    finally:
        release.set()
        viewer._queries.close()


def test_overview_is_metadata_only_and_clicks_preserve_span_and_history(viewer, monkeypatch):
    class NoReads:
        def __getattribute__(self, name):
            if name == '__class__':
                return object.__getattribute__(self, name)
            pytest.fail('Overview touched waveform data')
    monkeypatch.setattr(viewer.vcd, 'signals', NoReads())
    viewer.show_overview = True
    viewer.cursor_on = viewer.cursor_b_on = False
    viewer.update_layout()
    viewer.time_start, viewer.time_end = 20, 40
    viewer.markers = [Marker(50, 'half')]
    viewer.draw_overview()
    assert '│' in viewer.stdscr.line(viewer.overview_row)
    assert '[' in viewer.stdscr.line(viewer.overview_row)
    viewer.overview_click(viewer.wave_width - 2)
    assert (viewer.time_start, viewer.time_end) == (80, 100)
    viewer.view_back()
    assert (viewer.time_start, viewer.time_end) == (20, 40)
    viewer.vcd._extent_known = False
    viewer.overview_click(0)
    assert (viewer.time_start, viewer.time_end) == (20, 40)


def test_mouse_overview_and_inspector_do_not_place_waveform_cursors(viewer, monkeypatch):
    viewer.toggle_inspector()
    viewer.toggle_overview()
    viewer.time_start, viewer.time_end = 20, 40
    viewer.cursor_a = 30
    def click(row):
        monkeypatch.setattr(curses, 'getmouse', lambda: (
            0, viewer.width - 2, row, 0, curses.BUTTON1_CLICKED))
        viewer.handle_mouse()
    click(viewer.overview_row)
    assert (viewer.time_start, viewer.time_end) == (80, 100)
    assert viewer.cursor_a == 30
    click(wave_bottom(viewer) + 1)
    assert viewer.cursor_a == 30


def test_empty_view_distinguishes_no_selection_folds_loading_and_no_declarations(viewer):
    viewer.display_order = []
    viewer.collapsed_scopes = {'top'}  # An explicit empty selection remains intentional.
    viewer.draw_signals()
    assert 'No signals selected' in viewer.stdscr.line(viewer.wave_top + 1)
    viewer._rebuild_display_order()
    viewer.draw_signals()
    assert 'scopes folded' in viewer.stdscr.line(viewer.wave_top + 1)
    viewer.vcd._loading = True
    viewer.draw_signals()
    assert 'Loading trace' in viewer.stdscr.line(viewer.wave_top + 1)
    viewer.vcd._loading = False
    viewer.vcd.signals = {}
    viewer.draw_signals()
    assert 'No signals in this trace' in viewer.stdscr.line(viewer.wave_top + 1)


def test_optional_panels_round_trip_through_session_and_config(viewer, monkeypatch, tmp_path):
    from alwavelets import config
    monkeypatch.setattr(viewer, 'apply_theme', lambda: None)
    viewer.vcd._filename = str(tmp_path / 'wave.vcd')
    viewer.execute_command('inspect')
    viewer.execute_command('overview')
    viewer.save_session()
    viewer.show_inspector = viewer.show_overview = False
    assert viewer.restore_session()
    assert viewer.show_inspector and viewer.show_overview
    monkeypatch.setattr(config, 'load_config', lambda: {'inspector': False, 'overview': False})
    # The shared fixture disables config at construction; use the original method.
    APPLY_CONFIG(viewer)
    assert not viewer.show_inspector and not viewer.show_overview


def test_disk_inspector_and_pending_waveform_status(viewer, tmp_path):
    from test_writer_metadata import writer_for
    trace, writer, _ = writer_for(tmp_path, count=2, width=8)
    writer.add('0', 0, '00000001')
    writer.add('0', 50, '00000010')
    writer.publish(True)
    trace.max_time = 100
    disk = Alwavelets(Screen(120), trace)
    disk.show_inspector = True
    disk.cursor_a, disk.cursor_b, disk.cursor_b_on = 20, 75, True
    disk.update_layout()
    try:
        disk.draw_signals()
        assert 'Loading…' in disk.stdscr.line(disk.wave_top)
        disk.draw_inspector()
        settle(disk)
        sig = disk.get_signal('0')
        assert disk.inspector_details('0', sig) == {'a': '00000001', 'b': '00000010', 'transitions': 2}
        disk.stdscr.erase()
        disk.draw_signals()
        disk.draw_inspector()
        assert 'Loading…' not in disk.stdscr.line(disk.wave_top)
        assert 'No recorded values' in disk.stdscr.line(disk.wave_top + 1)
        text = '\n'.join(disk.stdscr.line(r) for r in range(wave_bottom(disk), disk.height - 1))
        assert 'A 20ns = 01' in text and 'B 75ns = 02' in text
    finally:
        disk._queries.close()
        trace.close()


def test_inspector_error_is_not_left_as_a_loading_placeholder(viewer, monkeypatch):
    viewer.show_inspector = True
    sig = viewer.vcd.signals['!']
    def broken(*args):
        raise OSError('page read failed')
    monkeypatch.setattr(VCDSignal, 'value_at', broken)
    try:
        viewer.inspector_details('!', sig)
        for future in viewer._queries.pending.values():
            with pytest.raises(OSError):
                future.result(timeout=3)
        viewer._queries.poll()
        assert 'Read failed' in viewer.inspector_details('!', sig)['status']
    finally:
        viewer._queries.close()


def test_empty_finder_explains_how_to_recover(viewer):
    viewer.open_finder()
    try:
        for ch in 'nomatch987':
            viewer.finder_handle_key(ord(ch))
        viewer.draw_finder()
        assert 'No matching signals' in viewer.stdscr.line(3)
        assert 'Ctrl-U Clear search' in viewer.stdscr.line(4)
        viewer.finder_handle_key(21)
        viewer.draw_finder()
        assert 'clk' in viewer.stdscr.line(3)
    finally:
        viewer.close_finder()


def test_results_overlay_remains_above_optional_inspector(viewer, monkeypatch):
    viewer.toggle_inspector()
    viewer.show_instruction_results = True
    monkeypatch.setattr(viewer, 'poll_instruction_results', lambda: None)
    monkeypatch.setattr(viewer, 'draw_instruction_results', lambda: viewer.stdscr.addstr(
        viewer.height - 2, 0, 'Result navigation: Enter / Esc'))
    monkeypatch.setattr(viewer, 'instruction_results_handle_key', lambda key: 'QUIT')
    for name in ('curs_set', 'halfdelay'):
        monkeypatch.setattr(curses, name, lambda *args: None)
    for name in ('keypad', 'refresh'):
        monkeypatch.setattr(viewer.stdscr, name, lambda *args: None, raising=False)
    monkeypatch.setattr(viewer.stdscr, 'getch', lambda: ord('q'), raising=False)
    monkeypatch.setattr(viewer, '_cleanup_mouse', lambda: None)
    viewer.run()
    assert 'Result navigation: Enter / Esc' in viewer.stdscr.line(viewer.height - 2)
