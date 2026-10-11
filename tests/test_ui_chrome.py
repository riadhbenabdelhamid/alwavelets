"""Terminal resizing must keep shortcuts and status readouts usable."""
import curses
from types import SimpleNamespace

import pytest

from alwavelets.core import VCDSignal
from alwavelets.parsers import VCDFile, build_scope_tree
from alwavelets.ui import Alwavelets
from alwavelets.ui_chrome import text_width
from alwavelets.ui_prompt import cell_width


class Screen:
    def __init__(self, width):
        self.width, self.height = width, 24
        self.erase()

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows = [[' '] * self.width for _ in range(self.height)]

    def addstr(self, row, col, text, *args):
        assert 0 <= row < self.height
        assert 0 <= col <= self.width
        assert col + text_width(text) <= self.width
        for char in text:
            size = cell_width(char)
            if size:
                self.rows[row][col] = char
                for extra in range(1, size):
                    self.rows[row][col + extra] = ''
            col += size

    addch = addstr

    def line(self, row):
        return ''.join(self.rows[row]).rstrip()


@pytest.fixture
def viewer(monkeypatch):
    monkeypatch.setattr(curses, 'color_pair', lambda n: 0)
    for method in ('_init_colors', '_init_mouse', '_apply_config'):
        monkeypatch.setattr(Alwavelets, method, lambda self: None)
    trace = VCDFile()
    trace._filename = 'simulation.vcd'
    sig = VCDSignal('clk', '!', 1, scope='top')
    sig.add_change(0, '0')
    sig.add_change(50, '1')
    trace.signals = {'!': sig}
    trace.signal_order = ['!']
    trace.max_time = 100
    trace.scopes = build_scope_tree(trace)
    return Alwavelets(Screen(80), trace)


@pytest.mark.parametrize('width', [40, 80, 120])
def test_every_shortcut_is_reachable_on_rendered_pages(viewer, monkeypatch, width):
    from alwavelets import ui_drawing
    original, captured = ui_drawing.menu_pages, []

    def record(items, room):
        pages = original(items, room)
        captured[:] = pages
        return pages

    monkeypatch.setattr(ui_drawing, 'menu_pages', record)
    viewer.stdscr = Screen(width)
    viewer.update_layout()
    viewer.draw_header()
    for page, entries in enumerate(captured):
        viewer.menu_page = page
        viewer.draw_header()
        line = viewer.stdscr.line(viewer.menu_row)
        for key, desc in entries:
            assert key in line
            # Long entries may elide their description but remain reachable.
            if text_width(key + ' ' + desc) <= width - 15:
                assert key + ' ' + desc in line
    viewer.menu_page = -1
    viewer.draw_header()
    assert viewer.menu_page == len(captured) - 1


@pytest.mark.parametrize('width', [24, 40, 80])
def test_resized_chrome_and_tree_stay_within_their_panes(viewer, width):
    viewer.stdscr = Screen(width)
    viewer.vcd._filename = '測定' * 30 + '.vcd'
    viewer.vcd.signals['!'].name = '信号' * 30
    viewer.tree_expanded.add('top')
    viewer._rebuild_tree()
    for menu in (True, False):
        viewer.show_menu_bar = menu
        viewer.update_layout()
        viewer.stdscr.erase()
        viewer.draw_header()
        viewer.draw_ruler()
        before = [row[viewer.name_width + 1:] for row in viewer.stdscr.rows]
        viewer.draw_panel_tree()
        after = [row[viewer.name_width + 1:] for row in viewer.stdscr.rows]
        assert before == after  # A narrow sidebar must not cover the waveform.
        assert viewer.wave_top == (3 if menu else 2)
        assert '…' in viewer.stdscr.line(0)


def test_narrow_footer_preserves_activity_and_complete_measurements(viewer):
    viewer.stdscr = Screen(40)
    viewer.update_layout()
    viewer.cursor_b_on = True
    viewer.draw_status_bar()
    line = viewer.stdscr.line(viewer.height - 1)
    assert not line.endswith('│')
    assert line.count('(') == line.count(')')
    viewer.vcd._load_error = 'Trace could not be read'
    viewer.draw_status_bar()
    assert 'Trace could not be' in viewer.stdscr.line(viewer.height - 1)
    viewer.control = SimpleNamespace(clients=1, last_activity=0)
    viewer.draw_status_bar()
    assert 'agent:1' in viewer.stdscr.line(viewer.height - 1)


def test_empty_trace_footer_does_not_show_an_inverted_row_range(viewer):
    viewer.display_order = []
    viewer.view_rows = []
    viewer.draw_status_bar()
    assert 'Rows 0–0/0' in viewer.stdscr.line(viewer.height - 1)
