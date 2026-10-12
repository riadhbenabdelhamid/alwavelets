"""Reversible display edits. History stores changed rows and properties only."""
from .display_order import DisplayOrder, member_positions, remove_display_ids


def remember_rows(viewer, sids):
    """Capture positions of affected rows, including rows currently absent.

    Restores use the following row as an anchor, so unrelated tree edits do
    not turn a saved position into a different signal. Catalog orders stay
    compact: only the affected IDs and their neighbours are resolved.
    """
    sids = set(sids)

    def capture():
        order = viewer.display_order
        positions = (member_positions(order, sids) if isinstance(order, DisplayOrder)
                     else [(i, sid) for i, sid in enumerate(order) if sid in sids])
        return [(i, sid, order[i + 1] if i + 1 < len(order) else None)
                for i, sid in positions]

    saved = capture()

    def restore():
        nonlocal saved
        current = capture()
        order = viewer.display_order
        remove_display_ids(order, sids)
        viewer._display_order_set.difference_update(sids)
        for index, sid, anchor in reversed(saved):
            at = order.index(anchor) if anchor in order else min(index, len(order))
            order.insert(at, sid)
            viewer._display_order_set.add(sid)
        saved = current
        return restore

    return restore


def remember_entries(mapping, keys):
    """Swap only these dictionary entries, preserving explicit defaults."""
    missing = object()
    saved = {key: mapping.get(key, missing) for key in keys}

    def restore():
        nonlocal saved
        current = {key: mapping.get(key, missing) for key in saved}
        for key, value in saved.items():
            if value is missing:
                mapping.pop(key, None)
            else:
                mapping[key] = value
        saved = current
        return restore

    return restore


def remember_members(members, keys):
    saved = {key: key in members for key in keys}

    def restore():
        nonlocal saved
        current = {key: key in members for key in saved}
        for key, present in saved.items():
            (members.add if present else members.discard)(key)
        saved = current
        return restore

    return restore


def format_targets(viewer):
    """Selection first; never scan the catalog or touch stale selections."""
    selected = getattr(viewer, 'selected_signals', set())
    if selected:
        return sorted(sid for sid in selected if sid in viewer._display_order_set)
    sid = viewer.focused_signal_id()
    return [sid] if sid is not None else []


def remove_rows(viewer, sids):
    sids = {sid for sid in sids if sid in viewer._display_order_set}
    if not sids:
        return
    viewer.push_undo(f'remove {len(sids)} signals', remember_rows(viewer, sids))
    remove_display_ids(viewer.display_order, sids)
    viewer._display_order_set.difference_update(sids)
    viewer._scope_check_cache = {}
    viewer.selected_signals.difference_update(sids)
    viewer._visual_mode = False
    viewer._rebuild_view_rows()
    viewer._needs_layout = viewer._dirty = True
    viewer._show_message(f'Removed {len(sids)} signals (u to undo)')


def apply_format(viewer, prop, value, targets=None):
    """Apply one explicit property to the selection as one history entry."""
    targets = format_targets(viewer) if targets is None else targets
    mapping = getattr(viewer, 'sig_' + prop)
    changed = [sid for sid in targets if sid in viewer._display_order_set and
               ((sid in mapping) if value is None else
                (sid not in mapping or mapping[sid] != value))]
    if not changed:
        return
    viewer.push_undo(f'{prop} change ({len(changed)} signals)', remember_entries(mapping, changed))
    for sid in changed:
        if value is None:
            mapping.pop(sid, None)
        else:
            mapping[sid] = value
    viewer._needs_layout = True
    viewer._dirty = True
    label = getattr(value, 'value', value) if value is not None else 'default'
    viewer._show_message(f'{prop.capitalize()}: {label} · {len(targets)} signal(s)')


def step_history(viewer, redo=False):
    source = getattr(viewer, '_redo_stack' if redo else '_undo_stack', [])
    if not source:
        viewer._show_message('Nothing to redo' if redo else 'Nothing to undo')
        return
    desc, action = source[-1]
    inverse = action()
    source.pop()
    if not hasattr(viewer, '_redo_stack'):
        viewer._redo_stack = []
    destination = viewer._undo_stack if redo else viewer._redo_stack
    if callable(inverse):
        destination.append((desc, inverse))
        del destination[:-100]
    else:
        # Old extension actions can still be undone, but cannot be replayed.
        destination.clear()
    viewer._sync_display_set()
    viewer._reindex_groups()
    viewer._rebuild_view_rows()
    viewer._needs_layout = viewer._dirty = True
    viewer._show_message(('Redid: ' if redo else 'Undid: ') + desc)
