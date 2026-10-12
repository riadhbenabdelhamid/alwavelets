"""Register values remain readable, reachable, and isolated from waveforms."""
import curses
import re
import threading
from types import SimpleNamespace

import pytest

from alwavelets.instruction_store import InstructionResults
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
