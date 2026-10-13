"""Cursor visibility and bounded view history for interactive navigation."""


def active_cursor_visible(viewer):
    return viewer.cursor_b_on if getattr(viewer, 'active_cursor', 'a') == 'b' else viewer.cursor_on


def set_active_cursor_visible(viewer, visible=True):
    attribute = 'cursor_b_on' if getattr(viewer, 'active_cursor', 'a') == 'b' else 'cursor_on'
    setattr(viewer, attribute, visible)


def push_view_history(viewer):
    current = (viewer.time_start, viewer.time_end)
    history = getattr(viewer, 'zoom_history', [])
    if not history or history[-1] != current:
        history.append(current)
        del history[:-getattr(viewer, 'zoom_history_max', 50)]
    viewer.zoom_history = history
    viewer.zoom_forward = []


def set_time_range(viewer, start, end):
    """Record a completed navigation, leaving invalid or unchanged views alone."""
    start, end = int(start), int(end)
    if start < 0 or end <= start:
        raise ValueError('Time range must satisfy 0 <= start < end')
    if (start, end) == (viewer.time_start, viewer.time_end):
        return False
    push_view_history(viewer)
    viewer.time_start, viewer.time_end = start, end
    viewer._dirty = True
    return True


class ViewHistoryMixin:
    def view_back(self):
        self._step_view_history('zoom_history', 'zoom_forward', 'earlier')

    def view_forward(self):
        self._step_view_history('zoom_forward', 'zoom_history', 'later')

    def _step_view_history(self, source, destination, direction):
        history = getattr(self, source, [])
        if not history:
            self._show_message(f'No {direction} view')
            return
        other = getattr(self, destination, [])
        other.append((self.time_start, self.time_end))
        del other[:-getattr(self, 'zoom_history_max', 50)]
        setattr(self, destination, other)
        self.time_start, self.time_end = history.pop()
        # A live-follow tick must not immediately replace the restored range.
        self.follow_on = False
        self._message = None
        self._dirty = True
