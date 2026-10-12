"""Named row spacing and geometry shared by layout, drawing, and input."""
import math


SPACING_MODES = {'compact': (0, False), 'blank': (1, False), 'divider': (1, True)}


def row_spacing(viewer):
    """Return whole gap rows and divider visibility, accepting old sessions.

    Legacy 0.25/0.5 settings already used a full row. Keep that space but
    replace the block glyphs with a dotted divider; retain old 2/3-row gaps.
    """
    try:
        value = float(getattr(viewer, 'signal_spacing', 0))
    except (ValueError, TypeError):
        value = 0
    if not math.isfinite(value) or value <= 0:
        return 0, False
    gap = max(1, min(3, int(value)))
    divider = bool(getattr(viewer, 'signal_divider', False)) or value < 1
    return gap, divider


def spacing_name(viewer):
    gap, divider = row_spacing(viewer)
    if not gap:
        return 'Compact'
    name = 'Divider' if divider else 'Blank gap'
    return name if gap == 1 else f'{name} ({gap} rows)'
