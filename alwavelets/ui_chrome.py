"""Small, cell-aware layout helpers for terminal bars."""
from .ui_prompt import cell_width, clip_cells


def text_width(text):
    return sum(cell_width(char) for char in text)


def elide(text, width):
    """Fit a label without splitting a wide character; mark omitted text."""
    if width <= 0:
        return ''
    clipped, _ = clip_cells(text, width)
    if clipped == text:
        return text
    return clip_cells(text, width - 1)[0] + '…'


def menu_pages(items, width, gap=3):
    """Pack whole shortcuts into pages; an oversized item gets its own page."""
    pages, page, used = [], [], 0
    for key, description in items:
        size = text_width(key) + 1 + text_width(description)
        needed = size + (gap if page else 0)
        if page and used + needed > width:
            pages.append(page)
            page, used = [], 0
        used += size + (gap if page else 0)
        page.append((key, description))
    if page:
        pages.append(page)
    return pages or [[]]
