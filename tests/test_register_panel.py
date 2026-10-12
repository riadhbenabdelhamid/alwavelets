"""Register values remain readable, reachable, and isolated from waveforms."""
import curses
import re
import threading
from types import SimpleNamespace

import pytest

from alwavelets.core import VCDSignal
from alwavelets.instruction_store import InstructionResults
from alwavelets.ui_chrome import text_width, wave_origin, wave_right
from alwavelets.ui_registers import REGISTER_POSITIONS
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture


class PanelScreen(Screen):
    def __init__(self, width, height):
        self.calls = []
        super().__init__(width)
        self.height = height
        self.erase()

    def addstr(self, row, col, text, *attrs):
        self.calls.append((row, col, text, attrs))
        super().addstr(row, col, text, *attrs)

    addch = addstr


@pytest.fixture
def panel(viewer):
    v = viewer
    v.show_reg_panel = True
    v.register_panel_position = 'overlay'
    regs = [0] + [0xdead0000 + i for i in range(1, 32)]
    regs[7] = None
    current = {'pc': 0x80000108, 'decoded': {'asm': 'addi a0, a0, 1'}}
    v.instr_trace = SimpleNamespace(built=True, entries=[], regs=regs, current=current,
                                   register_panel_state=lambda t: (3, regs, {10}, current))
    return v


@pytest.mark.parametrize('width,height', [(24, 12), (40, 12), (60, 18), (80, 24), (120, 30), (160, 40)])
@pytest.mark.parametrize('mode', ['classic', 'calm', 'minimal'])
def test_all_registers_are_complete_and_reachable_without_overwriting_footer(panel, width, height, mode):
    v = panel
    v.workmode = mode
    v.show_menu_bar = mode != 'minimal'
    v.stdscr = PanelScreen(width, height)
    v.update_layout()
    seen = set()
    for page in range(32):
        v.stdscr.calls.clear()
        for row in v.stdscr.rows:
            row[:] = ['~'] * width
        v.draw_register_panel()
        layout = v._register_panel_layout
        left, top, pw, ph = (layout[k] for k in ('left', 'top', 'width', 'height'))
        assert layout['fits']
        for row, col, label, _ in v.stdscr.calls:
            match = re.fullmatch(r'x(\d+)\s+\S+\s+([0-9a-f-]+)', label)
            if match:
                index = int(match[1])
                expected = '--------' if index == 7 else f'{v.instr_trace.regs[index]:08x}'
                assert match[2] == expected
                assert label in v.stdscr.line(row)  # Neither neighbor nor footer overwrote it.
                assert left < col and col + len(label) < left + pw
                seen.add(index)
        for row in range(height):
            if top <= row < top + ph:
                assert '~' not in v.stdscr.rows[row][left:left + pw]
                assert all(c == '~' for c in v.stdscr.rows[row][:left] + v.stdscr.rows[row][left + pw:])
            else:
                assert set(v.stdscr.rows[row]) == {'~'}
        assert 'PC=0x80000108' in v.stdscr.line(top + ph - 4)
        assert 'addi a0, a0, 1' in v.stdscr.line(top + ph - 3)
        v.register_panel_handle_key(curses.KEY_NPAGE)
        if v._register_panel_page == page:
            break
    assert seen == set(range(32))
    assert v.register_panel_handle_key(curses.KEY_PPAGE)


def test_register_panel_avoids_inspector_and_keeps_wide_values(panel):
    v = panel
    v.instr_trace.regs[31] = 0x123456789abcdef0
    v.stdscr = PanelScreen(120, 30)
    v.show_inspector = True
    v.update_layout()
    found = False
    for _ in range(32):
        v.draw_register_panel()
        found |= any('123456789abcdef0' in text for _, _, text, _ in v.stdscr.calls)
        v.page_register_panel(1)
    assert found
    assert all(row < v.wave_bottom for row, _, _, _ in v.stdscr.calls)


@pytest.mark.parametrize('width,height', [(12, 8), (24, 8), (80, 6), (5, 4)])
def test_tiny_terminals_show_guidance_instead_of_truncated_values(panel, width, height):
    v = panel
    v.stdscr = PanelScreen(width, height)
    v.update_layout()
    v.draw_register_panel()
    assert not v._register_panel_layout['fits']
    assert not any(re.match(r'x\d', text) for _, _, text, _ in v.stdscr.calls)
    v.register_panel_handle_key(27)
    assert not v.show_reg_panel


def test_resize_clamps_page_and_clears_old_register_text(panel):
    v = panel
    v.stdscr = PanelScreen(40, 12)
    v.update_layout()
    v.draw_register_panel()
    v.page_register_panel(100)
    assert v._register_panel_page > 0
    v.stdscr = PanelScreen(140, 30)
    v.update_layout()
    v.draw_register_panel()
    assert v._register_panel_page == 0
    v.instr_trace.current['decoded']['asm'] = 'nop'
    v.draw_register_panel()
    text = '\n'.join(v.stdscr.line(row) for row in range(v.height))
    assert 'nop' in text and 'addi' not in text


def test_panel_mouse_blocks_hidden_waveforms_and_pages_with_wheel(panel, monkeypatch):
    v = panel
    v.stdscr = PanelScreen(40, 12)
    v.update_layout()
    v.draw_register_panel()
    layout = v._register_panel_layout
    x, y = layout['left'] + 5, layout['top'] + 2
    original = v.cursor_time, v.focus_idx, v.time_start, v.time_end
    for button in (curses.BUTTON1_PRESSED, curses.BUTTON1_RELEASED, curses.BUTTON3_CLICKED):
        v._drag_anchor = v._drag_cur = 20
        monkeypatch.setattr(curses, 'getmouse', lambda: (0, x, y, 0, button))
        v.handle_mouse()
        assert (v.cursor_time, v.focus_idx, v.time_start, v.time_end) == original
        assert v._drag_anchor is None and v._drag_cur is None
    assert v.register_panel_mouse(x, y, 1 << 21)
    assert v._register_panel_page == 1
    assert v.register_panel_mouse(x, y, curses.BUTTON4_PRESSED)
    assert v._register_panel_page == 0
    assert not v.register_panel_mouse(0, 0, curses.BUTTON1_CLICKED)
    assert not v.register_panel_handle_key(curses.KEY_RIGHT)  # Cursor navigation remains available.


def test_keymap_paging_and_escape_preserve_waveform_selection(panel):
    v = panel
    v.stdscr = PanelScreen(40, 12)
    v.update_layout()
    v.draw_register_panel()
    v.selected_signals = {'!'}
    before = v.sig_offset, v.focus_idx
    v.keymap[curses.KEY_NPAGE]()
    assert v._register_panel_page == 1 and (v.sig_offset, v.focus_idx) == before
    v.keymap[curses.KEY_PPAGE]()
    assert v._register_panel_page == 0
    v._pending_g = True
    v.keymap[27]()
    assert not v.show_reg_panel and not v._pending_g
    assert v.selected_signals == {'!'}


@pytest.mark.parametrize('building', [False, True])
def test_unbuilt_trace_explains_missing_data(panel, building):
    v = panel
    v.stdscr = PanelScreen(80, 24)
    v.update_layout()
    v.instr_trace.built = False
    v._analysis_job = SimpleNamespace() if building else None
    v.draw_register_panel()
    text = '\n'.join(v.stdscr.line(row) for row in range(v.height))
    assert ('Waiting for instruction trace' if building else 'No instruction trace') in text
    assert 'dead0001' not in text and 'p/Esc close' in text


def test_loading_is_explicit_and_closing_cancels_register_reads(panel, monkeypatch):
    v = panel
    v.stdscr = PanelScreen(80, 24)
    v.update_layout()
    ready = threading.Event()
    release = threading.Event()
    original = v.instr_trace.register_panel_state
    cancels = []

    def delayed(cursor, *, cancel):
        cancels.append(cancel)
        ready.set()
        assert release.wait(5)
        return original(cursor)

    with InstructionResults.collect([], dict) as entries:
        v.instr_trace.entries = entries
        monkeypatch.setattr(v.instr_trace, 'register_panel_state', delayed)
        try:
            v.draw_register_panel()
            assert ready.wait(5)
            text = '\n'.join(v.stdscr.line(row) for row in range(v.height))
            assert 'Reading registers' in text and 'Hart 0' not in text and '00000000' not in text
            v._kb_reg_panel()
            assert not v.show_reg_panel and cancels[0].is_set()
        finally:
            release.set()
            v._queries.close()


def test_toggle_preserves_trace_setup_message_and_avoids_duplicate_jobs(panel, monkeypatch):
    v = panel
    v.show_reg_panel = False
    v.instr_trace.built = False
    calls = []

    def build():
        calls.append(True)
        v._show_message('No tagged signals')

    monkeypatch.setattr(v, '_build_trace', build)
    v._kb_reg_panel()
    assert v.show_reg_panel and v._message == 'No tagged signals' and calls == [True]
    v._kb_reg_panel()
    v._analysis_job = SimpleNamespace()
    v._kb_reg_panel()
    assert calls == [True] and 'Analysis running' in v._message


def test_registers_default_to_right_and_closing_restores_waveform_space(viewer):
    v = viewer
    v.instr_trace.built = True
    before = v.signal_left, v.wave_width, v.wave_top, v.wave_bottom
    assert v.register_panel_position == 'right'
    v._kb_reg_panel()
    assert v.wave_width < before[1]
    assert wave_right(v) == v._register_panel_layout['left']
    v._kb_reg_panel()
    assert (v.signal_left, v.wave_width, v.wave_top, v.wave_bottom) == before


@pytest.mark.parametrize('position', REGISTER_POSITIONS)
@pytest.mark.parametrize('width,height', [(40, 18), (80, 24), (160, 40)])
@pytest.mark.parametrize('details', [False, True])
def test_docks_reserve_space_for_all_waveform_layers(panel, position, width, height, details):
    v = panel
    v.stdscr = PanelScreen(width, height)
    v.show_inspector = v.show_overview = details
    v.show_grid = 2
    v.signal_spacing, v.signal_divider = 1, True
    analog = VCDSignal('analog', '!', 1, signal_type='real', scope='top')
    analog.add_change(0, '0.25')
    analog.add_change(40, '0.75')
    v.vcd.signals['!'] = analog
    v.sig_height['!'] = 3
    v.diff_vcd = v.vcd
    v.diff_sig_map['!'] = analog
    v.diff_ranges['!'] = [(0, 10)]
    v.create_group('Example signals', ['!'])
    v.tree_expanded.add('top')
    v._rebuild_tree()
    v.set_register_position(position)
    v.draw_register_panel()
    box = v._register_panel_layout
    left, top, pw, ph = (box[k] for k in ('left', 'top', 'width', 'height'))
    original = [row[:] for row in v.stdscr.rows]
    v.stdscr.calls.clear()
    v.draw_header()
    v.draw_overview()
    v.draw_ruler()
    v.draw_signals()
    v.draw_panel_tree()
    v.draw_inspector()
    v.draw_status_bar()
    if position != 'overlay':
        for row, col, text, _ in v.stdscr.calls:
            assert not (top <= row < top + ph and col < left + pw and col + text_width(text) > left), (row, col, text)
        for row in range(top, top + ph):
            assert v.stdscr.rows[row][left:left + pw] == original[row][left:left + pw]
    assert wave_origin(v) < wave_right(v)
    assert v.wave_top < v.wave_bottom
    if details:
        assert 'Signal inspector' in v.stdscr.line(v.inspector_top)


@pytest.mark.parametrize('position', REGISTER_POSITIONS)
def test_every_placement_has_reachable_values_and_commands(panel, position):
    v = panel
    v.stdscr = PanelScreen(80, 24)
    v.set_register_position(position)
    seen = set()
    for _ in range(32):
        v.stdscr.erase()
        v.stdscr.calls.clear()
        v.draw_register_panel()
        assert {button[3] for button in v._register_panel_buttons} == set(REGISTER_POSITIONS)
        box = v._register_panel_layout
        assert box['fits']
        for row, _, text, _ in v.stdscr.calls:
            match = re.fullmatch(r'x(\d+)\s+\S+\s+([0-9a-f-]+)', text)
            if match:
                seen.add(int(match[1]))
                assert text in v.stdscr.line(row)
        assert 'PC=0x80000108' in v.stdscr.line(box['top'] + box['height'] - 4)
        old_page = v._register_panel_page
        v.page_register_panel(1)
        if old_page == v._register_panel_page:
            break
    assert seen == set(range(32))


@pytest.mark.parametrize('active', ['a', 'b'])
@pytest.mark.parametrize('position', REGISTER_POSITIONS[:4])
def test_cursor_stays_visible_when_moving_right_through_docked_view(panel, position, active):
    v = panel
    v.stdscr = PanelScreen(120, 30)
    v.panel_mode = False
    v.show_module_sep = False
    v.cursor_on = v.cursor_b_on = True
    v.active_cursor = active
    v.set_register_position(position)
    # Cross the boundary repeatedly, not just once within the initial view.
    for _ in range(150):
        v.keymap[curses.KEY_RIGHT]()
        col = v.time_to_col(v.cursor_time)
        assert 0 <= col < v.wave_width - 1
        v.stdscr.erase()
        v.draw_ruler()
        v.draw_signals()
        v.draw_register_panel()
        assert v.stdscr.rows[v.wave_top][wave_origin(v) + col] == '┃'


def test_placement_shortcuts_and_clicks_preserve_times_and_selection(panel):
    v = panel
    v.stdscr = PanelScreen(120, 30)
    v.cursor_a, v.cursor_b, v.active_cursor = 20, 75, 'b'
    v.selected_signals = {'!'}
    before = v.time_start, v.time_end, v.cursor_a, v.cursor_b, list(v.zoom_history)
    for key, position in zip('34567', REGISTER_POSITIONS):
        assert v.register_panel_handle_key(ord(key))
        assert v.register_panel_position == position
        assert (v.time_start, v.time_end, v.cursor_a, v.cursor_b, v.zoom_history) == before
        assert v.selected_signals == {'!'} and v.active_cursor == 'b'
    for position in REGISTER_POSITIONS:
        v.draw_register_panel()
        row, x0, _, _ = next(button for button in v._register_panel_buttons if button[3] == position)
        assert v.register_panel_mouse(x0, row, curses.BUTTON1_CLICKED)
        assert v.register_panel_position == position
        assert (v.time_start, v.time_end, v.cursor_a, v.cursor_b, v.zoom_history) == before
    assert not v.register_panel_handle_key(ord('1'))  # Cursor A/B keys remain available.
    assert not v.register_panel_handle_key(ord('2'))
    assert not v.set_register_position('invalid')


@pytest.mark.parametrize('position', REGISTER_POSITIONS)
def test_placement_persists_in_session_and_resize_never_changes_it(panel, position, tmp_path, monkeypatch):
    v = panel
    monkeypatch.setattr(v, 'apply_theme', lambda: None)
    v.vcd._filename = str(tmp_path / 'registers.vcd')
    v.set_register_position(position)
    v.save_session()
    v.register_panel_position = 'invalid'
    assert v.restore_session()
    assert v.register_panel_position == position
    for width, height in ((12, 8), (24, 12), (80, 24), (160, 40)):
        v.stdscr = PanelScreen(width, height)
        v.update_layout()
        v.draw_register_panel()
        assert v.register_panel_position == position
        box = v._register_panel_layout
        assert 0 <= box['left'] <= box['left'] + box['width'] <= width
        assert 0 <= box['top'] <= box['top'] + box['height'] < height


@pytest.mark.parametrize('position', REGISTER_POSITIONS[:4])
def test_mouse_maps_times_inside_resized_waveform_pane(panel, position, monkeypatch):
    v = panel
    v.stdscr = PanelScreen(120, 30)
    v.set_register_position(position)
    v.draw_signals()
    col = v.wave_width // 2
    expected = v.col_to_time(col)
    monkeypatch.setattr(curses, 'getmouse', lambda: (
        0, wave_origin(v) + col, v.wave_top, 0, curses.BUTTON1_CLICKED))
    v.handle_mouse()
    assert v.cursor_time == expected
