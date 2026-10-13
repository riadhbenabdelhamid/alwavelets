"""Reversible marker edits, usable by the UI and headless command engine."""
from itertools import islice

from .core import Marker


def _remember_markers(viewer, targets):
    """Swap presence of these marker objects; keep unrelated markers intact."""
    targets = set(targets)

    def capture():
        return [(i, marker) for i, marker in enumerate(viewer.markers) if marker in targets]

    saved = capture()

    def restore():
        nonlocal saved
        current = capture()
        remaining = iter([marker for marker in viewer.markers if marker not in targets])
        restored = []
        # Merge in capture order, avoiding repeated insertion for clear-all.
        for index, marker in saved:
            restored.extend(islice(remaining, max(0, index - len(restored))))
            restored.append(marker)
        restored.extend(remaining)
        viewer.markers[:] = restored
        saved = current
        return restore

    return restore


def add_marker(viewer, time, label=''):
    marker = Marker(time, label)
    # The scripting engine also supports small headless viewer adapters.
    if hasattr(viewer, 'push_undo'):
        viewer.push_undo('add marker', _remember_markers(viewer, [marker]))
    viewer.markers.append(marker)
    viewer._dirty = True
    return marker


def delete_markers(viewer, targets):
    targets = set(targets).intersection(viewer.markers)
    if not targets:
        return
    if hasattr(viewer, 'push_undo'):
        viewer.push_undo(f'delete {len(targets)} marker(s)', _remember_markers(viewer, targets))
    viewer.markers[:] = [marker for marker in viewer.markers if marker not in targets]
    viewer._dirty = True


def rename_marker(viewer, marker, label):
    if marker not in viewer.markers or label == marker.label:
        return
    saved = marker.label

    def restore():
        nonlocal saved
        marker.label, saved = saved, marker.label
        return restore

    viewer.push_undo('rename marker', restore)
    marker.label = label
    viewer._dirty = True
