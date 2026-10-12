"""A horizontally scrolling command/path prompt without input truncation."""
import curses
import unicodedata


def cell_width(char):
    return 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1)


def clip_cells(text, limit):
    used = 0
    for i, char in enumerate(text):
        size = cell_width(char)
        if used + size > limit:
            return text[:i], used
        used += size
    return text, used


def read_prompt(screen, label, attribute=0, initial=''):
    text, cursor, left = initial, len(initial), 0
    curses.noecho()
    curses.curs_set(1)
    try:
        while True:
            height, width = screen.getmaxyx()
            prefix, prefix_width = clip_cells(label, max(0, width - 2))
            room = max(1, width - prefix_width - 1)
            left = min(left, cursor)
            before = sum(cell_width(c) for c in text[left:cursor])
            while before >= room and left < cursor:
                before -= cell_width(text[left])
                left += 1
            visible, used = clip_cells(text[left:], room)
            try:
                screen.addstr(height - 1, 0, prefix + visible + ' ' * max(0, room - used), attribute)
                screen.move(height - 1, min(width - 1, prefix_width + before))
                screen.refresh()
            except curses.error:
                pass
            try:
                key = screen.get_wch()
            except curses.error:
                continue
            if key in ('\n', '\r', curses.KEY_ENTER):
                return text.strip()
            if key in ('\x1b', 27):
                return ''
            if key in ('\b', '\x7f', curses.KEY_BACKSPACE):
                if cursor:
                    text = text[:cursor - 1] + text[cursor:]
                    cursor -= 1
            elif key == curses.KEY_DC:
                text = text[:cursor] + text[cursor + 1:]
            elif key in (curses.KEY_LEFT, '\x02'):
                cursor = max(0, cursor - 1)
            elif key in (curses.KEY_RIGHT, '\x06'):
                cursor = min(len(text), cursor + 1)
            elif key in (curses.KEY_HOME, '\x01'):
                cursor = 0
            elif key in (curses.KEY_END, '\x05'):
                cursor = len(text)
            elif key == '\x15':
                text, cursor, left = '', 0, 0
            elif key == '\x17':
                start = text[:cursor].rstrip().rfind(' ') + 1
                text, cursor = text[:start] + text[cursor:], start
            elif isinstance(key, str) and key.isprintable():
                text = text[:cursor] + key + text[cursor:]
                cursor += len(key)
    finally:
        curses.curs_set(0)
