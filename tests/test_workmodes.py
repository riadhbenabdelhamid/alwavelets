"""Presentation presets must round-trip without changing trace state."""
import curses
import json

import pytest

from alwavelets.ui import Alwavelets
from test_ui_chrome import Screen, viewer as _viewer_fixture

viewer = _viewer_fixture
APPLY_CONFIG = Alwavelets._apply_config


@pytest.fixture
def colors(monkeypatch):
    pairs = {}
    monkeypatch.setattr(curses, 'COLORS', 256, raising=False)
    monkeypatch.setattr(curses, 'init_pair', lambda i, fg, bg: pairs.__setitem__(i, (fg, bg)))
    return pairs


def test_cycle_and_commands_preserve_trace_state(viewer, colors):
    viewer.cursor_a, viewer.cursor_b = 30, 70
    viewer.time_start, viewer.time_end = 20, 80
    viewer.sig_height = {'!': 3}
    viewer.sig_color = {'!': viewer.C_MARKER}
    viewer.selected_signals = {'!'}
    order = viewer.display_order
    for name in ('classic', 'minimal', 'calm'):
        viewer.keymap[curses.KEY_F3]()
        assert viewer.workmode == viewer.current_theme == name
        assert viewer.show_menu_bar == (name != 'minimal')
        assert viewer.show_module_sep == (name == 'calm')
        assert viewer.display_order is order
        assert (viewer.cursor_a, viewer.cursor_b, viewer.time_start, viewer.time_end) == (30, 70, 20, 80)
        assert viewer.sig_height == {'!': 3} and viewer.sig_color == {'!': viewer.C_MARKER}
        assert viewer.selected_signals == {'!'}
    viewer.execute_command('workmode MINIMAL')
    assert viewer.workmode == 'minimal'
    viewer.execute_command('workmode')
    assert 'minimal' in viewer._message
    viewer.execute_command('workmode unknown')
    assert viewer.workmode == 'minimal' and 'Workmodes:' in viewer._message
    assert 'workmode' in viewer.command_verbs()


def test_mode_change_keeps_bottom_focused_row_visible(viewer, colors):
    from alwavelets.core import VCDSignal
    for i in range(40):
        sid = f's{i}'
        viewer.vcd.signals[sid] = VCDSignal(sid, sid, 1)
        viewer.display_order.append(sid)
    viewer._rebuild_view_rows()
    viewer.apply_workmode('minimal', notify=False)
    viewer.sig_offset = 10
    viewer.focus_idx = viewer.visible_signals - 1
    focused = viewer.focused_signal_id()
    viewer.apply_workmode('calm', notify=False)
    assert viewer.focus_idx < viewer.visible_signals
    assert viewer.focused_signal_id() == focused


@pytest.mark.parametrize('mode', ['calm', 'classic', 'minimal'])
@pytest.mark.parametrize('width', [24, 40, 80, 120])
def test_modes_render_on_narrow_and_wide_terminals(viewer, colors, mode, width):
    viewer.stdscr = Screen(width)
    viewer.apply_workmode(mode, notify=False)
    viewer.draw_header()
    viewer.draw_ruler()
    viewer.draw_panel_tree()
    viewer.draw_status_bar()
    assert mode.title() in viewer.stdscr.line(0)
    assert viewer.wave_top == (2 if mode == 'minimal' else 3)
    if width == 120:
        title = viewer.stdscr.line(0)
        assert ('Chg:' in title) == (mode == 'classic')
        assert ('changes' in title) == (mode == 'calm')
    # Minimal's hidden guide can still be brought back with F2.
    if mode == 'minimal':
        viewer.keymap[curses.KEY_F2]()
        viewer.update_layout()
        viewer.draw_header()
        assert 'Help' in viewer.stdscr.line(viewer.menu_row)


@pytest.mark.parametrize('mode', ['calm', 'classic', 'minimal'])
def test_session_restores_mode_and_customized_appearance(viewer, colors, tmp_path, mode):
    viewer.vcd._filename = str(tmp_path / 'trace.vcd')
    viewer.apply_workmode(mode, notify=False)
    viewer.current_theme = 'light'
    viewer.show_menu_bar = not viewer.show_menu_bar
    viewer.show_module_sep = not viewer.show_module_sep
    viewer.signal_spacing = 0.5
    expected = (viewer.show_menu_bar, viewer.show_module_sep)
    viewer.save_session()
    saved = json.loads((tmp_path / 'trace.alwavelets_session.json').read_text())
    assert saved['workmode'] == mode
    viewer.apply_workmode('classic', notify=False)
    assert viewer.restore_session()
    assert viewer.workmode == mode and viewer.current_theme == 'light'
    assert (viewer.show_menu_bar, viewer.show_module_sep) == expected
    assert viewer.signal_spacing == 0.5


@pytest.mark.parametrize('saved_mode', [None, 'unknown', ['minimal']])
def test_old_or_invalid_mode_does_not_reset_existing_appearance(viewer, colors, tmp_path, saved_mode):
    viewer.vcd._filename = str(tmp_path / 'trace.vcd')
    viewer.apply_workmode('minimal', notify=False)
    saved = {'theme': 'phosphor'}
    if saved_mode is not None:
        saved['workmode'] = saved_mode
    (tmp_path / 'trace.alwavelets_session.json').write_text(json.dumps(saved))
    assert viewer.restore_session()
    assert viewer.workmode == 'minimal' and viewer.current_theme == 'phosphor'
    assert viewer.show_menu_bar is False


def test_config_overrides_mode_defaults_and_invalid_modes_are_ignored(viewer, colors, monkeypatch):
    from alwavelets import config
    monkeypatch.setattr(config, 'load_config', lambda: {'workmode': 'MINIMAL'})
    APPLY_CONFIG(viewer)
    assert viewer.workmode == viewer.current_theme == 'minimal'
    assert viewer.show_menu_bar is False
    monkeypatch.setattr(config, 'load_config', lambda: {
        'workmode': 'minimal', 'theme': 'light', 'menu_bar': True,
        'keys': {'cycle_workmode': '@'}})
    APPLY_CONFIG(viewer)
    assert viewer.current_theme == 'light' and viewer.show_menu_bar is True
    viewer.keymap[ord('@')]()
    assert viewer.workmode == 'calm'
    monkeypatch.setattr(config, 'load_config', lambda: {'workmode': []})
    APPLY_CONFIG(viewer)
    assert viewer.workmode == 'calm'


def test_minimal_palette_and_eight_color_fallback(viewer, colors, monkeypatch):
    viewer.apply_workmode('minimal', notify=False)
    grays = {-1, 16, 231, *range(232, 256)}
    assert all(fg in grays and bg in grays for fg, bg in colors.values())
    monkeypatch.setattr(curses, 'COLORS', 8)
    viewer.apply_workmode('minimal', notify=False)
    assert all(fg in (-1, curses.COLOR_BLACK, curses.COLOR_WHITE)
               and bg in (-1, curses.COLOR_BLACK, curses.COLOR_WHITE)
               for fg, bg in colors.values())
    viewer.apply_workmode('classic', notify=False)
    assert colors[viewer.C_HIGH][0] == curses.COLOR_GREEN
    viewer.apply_workmode('minimal', notify=False)
    assert colors[viewer.C_HIGH][0] == curses.COLOR_WHITE


def test_explicit_launch_mode_wins_over_saved_appearance(viewer, colors, monkeypatch):
    from alwavelets import cli
    monkeypatch.setattr(cli, 'Alwavelets', lambda screen, trace: viewer)
    monkeypatch.setattr(cli, '_auto_load_sidecars', lambda *args: None)
    monkeypatch.setattr(viewer, 'restore_session', lambda: viewer.apply_workmode('minimal', notify=False))
    monkeypatch.setattr(viewer, 'run', lambda: None)
    cli.main_curses(viewer.stdscr, viewer.vcd, start_collapsed=False, workmode='classic')
    assert viewer.workmode == viewer.current_theme == 'classic'
    assert viewer.show_menu_bar is True
