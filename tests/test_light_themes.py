"""Light palettes stay readable on bright terminals across workmode changes."""
import curses

import pytest

from alwavelets.ui import Alwavelets, LIGHT_THEMES
from test_ui_chrome import viewer as _viewer_fixture
from test_workmodes import APPLY_CONFIG, colors as _colors_fixture

viewer = _viewer_fixture
colors = _colors_fixture


def indexed_rgb(index):
    if index >= 232:
        return (8 + 10 * (index - 232),) * 3
    cube = (0, 95, 135, 175, 215, 255)
    offset = index - 16
    return cube[offset // 36], cube[offset // 6 % 6], cube[offset % 6]


def luminance(rgb):
    values = [channel / 255 for channel in rgb]
    linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in values]
    return sum(weight * value for weight, value in zip((0.2126, 0.7152, 0.0722), linear))


@pytest.mark.parametrize('theme', sorted(LIGHT_THEMES))
@pytest.mark.parametrize('background', [(255, 255, 255), (238, 238, 238), (220, 220, 220)])
def test_light_palette_pairs_have_strong_contrast_on_bright_backgrounds(theme, background):
    for role, pair in Alwavelets.THEMES[theme].items():
        if not isinstance(pair, tuple):
            continue
        foreground, back = pair
        # Default text on a light terminal is black; -1 backgrounds are inherited.
        fg = luminance((0, 0, 0) if foreground == -1 else indexed_rgb(foreground))
        bg = luminance(background if back == -1 else indexed_rgb(back))
        contrast = (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)
        assert contrast >= 4.5, (theme, role, background, contrast)


def test_mode_cycle_preserves_light_family_until_a_dark_theme_is_chosen(viewer, colors):
    viewer.execute_command('theme light')
    viewer.cursor_a, viewer.cursor_b = 20, 60
    order = viewer.display_order
    for mode in ('minimal', 'calm', 'classic'):
        viewer.keymap[curses.KEY_F3]()
        assert viewer.workmode == mode
        assert viewer.current_theme == mode + '-light'
        assert viewer.display_order is order
        assert (viewer.cursor_a, viewer.cursor_b) == (20, 60)
    viewer.execute_command('theme dark')
    viewer.keymap[curses.KEY_F3]()
    assert viewer.current_theme == viewer.workmode == 'minimal'


@pytest.mark.parametrize('theme', sorted(LIGHT_THEMES))
def test_light_themes_are_selectable_and_define_every_role(viewer, colors, theme):
    mode = viewer.workmode
    viewer.execute_command('theme ' + theme)
    assert viewer.current_theme == theme and viewer.workmode == mode
    assert len(colors) == viewer.C_DIFF_ED
    assert theme in viewer.THEME_ORDER
    if theme == 'minimal-light':
        grays = {-1, 16, 231, *range(232, 256)}
        assert all(fg in grays and bg in grays for fg, bg in colors.values())


@pytest.mark.parametrize('theme', sorted(LIGHT_THEMES))
def test_basic_color_fallback_avoids_light_text_on_default_background(viewer, colors, monkeypatch, theme):
    monkeypatch.setattr(curses, 'COLORS', 8)
    viewer.apply_theme(theme)
    assert len(colors) == viewer.C_DIFF_ED
    assert all(-1 <= fg < 8 and -1 <= bg < 8 for fg, bg in colors.values())
    assert all(fg not in (curses.COLOR_WHITE, curses.COLOR_YELLOW) for fg, bg in colors.values() if bg == -1)
    assert colors[viewer.C_SCOPE] == (curses.COLOR_BLACK, -1)
    if theme == 'minimal-light':
        assert all(fg in (-1, curses.COLOR_BLACK, curses.COLOR_WHITE)
                   and bg in (-1, curses.COLOR_BLACK, curses.COLOR_WHITE)
                   for fg, bg in colors.values())
        assert colors[viewer.C_CURSOR] == (curses.COLOR_WHITE, curses.COLOR_BLACK)
    else:
        assert colors[viewer.C_HELP] == (curses.COLOR_BLACK, curses.COLOR_WHITE)


def test_light_config_and_session_restore_keep_family_on_next_mode_switch(viewer, colors, monkeypatch, tmp_path):
    from alwavelets import config
    monkeypatch.setattr(config, 'load_config', lambda: {'workmode': 'calm', 'theme': 'calm-light'})
    APPLY_CONFIG(viewer)
    assert viewer.workmode == 'calm' and viewer.current_theme == 'calm-light'
    viewer.keymap[curses.KEY_F3]()
    assert viewer.current_theme == 'classic-light'
    viewer.vcd._filename = str(tmp_path / 'light.vcd')
    viewer.save_session()
    viewer.apply_theme('dark')
    viewer.apply_workmode('minimal')
    assert viewer.restore_session()
    assert viewer.workmode == 'classic' and viewer.current_theme == 'classic-light'
    viewer.keymap[curses.KEY_F3]()
    assert viewer.current_theme == 'minimal-light'
