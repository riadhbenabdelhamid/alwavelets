"""Paths longer than a terminal row must survive prompt input exactly."""
import curses

from alwavelets.ui_prompt import cell_width, read_prompt


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)
        self.frames = []

    def getmaxyx(self):
        return 24, 40

    def addstr(self, row, col, text, attribute):
        assert sum(cell_width(c) for c in text) <= 39
        self.frames.append(text)

    def move(self, row, col):
        assert row == 23 and 0 <= col < 40

    def refresh(self):
        pass

    def get_wch(self):
        return next(self.keys)


def test_long_path_and_scrolling(monkeypatch):
    monkeypatch.setattr(curses, 'noecho', lambda: None)
    monkeypatch.setattr(curses, 'curs_set', lambda _: None)
    path = '/a long directory/' + 'abcdef/' * 50 + 'all matches.csv'
    screen = Screen(list(path) + ['\n'])
    assert read_prompt(screen, 'Export: ') == path
    assert screen.frames[-1].rstrip().endswith('all matches.csv')


def test_prompt_editing_and_cancel(monkeypatch):
    monkeypatch.setattr(curses, 'noecho', lambda: None)
    monkeypatch.setattr(curses, 'curs_set', lambda _: None)
    keys = list('abcd') + [curses.KEY_LEFT, curses.KEY_BACKSPACE, 'X', curses.KEY_HOME, curses.KEY_DC, curses.KEY_END, '\n']
    assert read_prompt(Screen(keys), '> ') == 'bXd'
    assert read_prompt(Screen(list('do not execute') + ['\x1b']), '> ') == ''
    assert read_prompt(Screen(list('old value') + ['\x15'] + list('new value') + ['\x17', '\n']), '> ') == 'new'


def test_wide_unicode_path_and_cursor_movement(monkeypatch):
    monkeypatch.setattr(curses, 'noecho', lambda: None)
    monkeypatch.setattr(curses, 'curs_set', lambda _: None)
    path = '/測定/' * 30 + 'signal-e\u0301.csv'
    keys = list(path) + [curses.KEY_HOME, curses.KEY_END, '\n']
    assert read_prompt(Screen(keys), 'Export: ') == path


def test_prefilled_settings_can_be_accepted_replaced_or_cancelled(monkeypatch):
    monkeypatch.setattr(curses, 'noecho', lambda: None)
    monkeypatch.setattr(curses, 'curs_set', lambda _: None)
    assert read_prompt(Screen(['\n']), 'Fixed: ', initial='7 8 signed') == '7 8 signed'
    assert read_prompt(Screen(['\x1b']), 'Fixed: ', initial='7 8 signed') == ''
    keys = ['\x15'] + list('0 15 unsigned') + ['\n']
    assert read_prompt(Screen(keys), 'Fixed: ', initial='7 8 signed') == '0 15 unsigned'


def test_cancel_sentinel_distinguishes_empty_label_from_escape(monkeypatch):
    monkeypatch.setattr(curses, 'noecho', lambda: None)
    monkeypatch.setattr(curses, 'curs_set', lambda _: None)
    assert read_prompt(Screen(['\x1b']), 'Label: ', initial='old', cancel_value=None) is None
    assert read_prompt(Screen(['\x15', '\n']), 'Label: ', initial='old', cancel_value=None) == ''
    assert read_prompt(Screen(['\n']), 'Label: ', cancel_value=None) == ''
