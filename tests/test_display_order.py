"""Compact selections retain list behavior, scope order and session identities."""
import curses
import json
import random
import threading

import pytest

from alwavelets.display_order import DisplayOrder, DisplayMembership, member_positions
from alwavelets.hierarchy import scope_signal_ids, walk_scopes
from alwavelets.parsers import VCDFile
from alwavelets.persistence import remap_view_state
from alwavelets.storage import open_trace
from alwavelets.ui import Alwavelets
from tools.stress_tree import Screen


def make_trace(path, count=40, prefix='s', changed=False):
    with path.open('w') as f:
        f.write('$scope module top $end\n')
        for i in range(count):
            # Repeated visits deliberately interleave direct-scope ordinals.
            f.write('$scope module unit%d $end\n' % (i % 3))
            f.write('$var wire 8 %s%d value%d $end\n' % (prefix, i, i + int(changed)))
            f.write('$upscope $end\n')
        f.write('$upscope $end\n$enddefinitions $end\n#0\nb00000001 %s0\n' % prefix)
    return path


@pytest.fixture
def trace(tmp_path):
    with open_trace(make_trace(tmp_path / 'view.vcd'), use_cache=False) as vcd:
        yield vcd


def viewer(trace, monkeypatch):
    for method in ('_init_colors', '_init_mouse', '_apply_config'):
        monkeypatch.setattr(Alwavelets, method, lambda self: None)
    monkeypatch.setattr(curses, 'color_pair', lambda n: 0)
    monkeypatch.setattr(Alwavelets, 'apply_theme', lambda self: None)
    screen = Screen()
    screen.getmaxyx = lambda: (40, 120)
    return Alwavelets(screen, trace)


def test_random_edits_slices_duplicates_and_snapshots_match_list(trace):
    order = DisplayOrder.from_order(trace.signals, trace.signal_order)
    expected = list(trace.signal_order)
    rng = random.Random(1729)
    for _ in range(500):
        snapshot, old = order.copy(), expected.copy()
        op = rng.randrange(6)
        item = rng.choice(['s%d' % i for i in range(40)] + ['virtual'])
        index = rng.randrange(-len(expected) - 3, len(expected) + 4)
        if op == 0:
            order.insert(index, item)
            expected.insert(index, item)
        elif op == 1 and expected:
            index %= len(expected)
            assert order.pop(index) == expected.pop(index)
        elif op == 2 and expected:
            index %= len(expected)
            order[index] = expected[index] = item
        elif op == 3:
            stop = rng.randrange(len(expected) + 1)
            order[index:stop] = [item, 's1']
            expected[index:stop] = [item, 's1']
        elif op == 4:
            del order[::3]
            del expected[::3]
        else:
            order.append(item)
            expected.append(item)
        assert list(order) == expected and order == expected
        assert list(snapshot) == old
        assert order[::-2] == expected[::-2]
        for sid in ('s0', 's1', 'virtual', 'missing'):
            assert (sid in order) == (sid in expected)
            if sid in expected:
                assert order.index(sid) == expected.index(sid)
        assert set(DisplayMembership(order)) == set(expected)


def test_interleaved_scopes_toggle_without_changing_append_order(trace, monkeypatch):
    v = viewer(trace, monkeypatch)
    expected = list(trace.signal_order)
    nodes = list(walk_scopes(trace.scopes))
    for node in nodes + list(reversed(nodes)):
        drop = set(scope_signal_ids(node))
        v._tree_remove_scope_signals(node)
        expected = [s for s in expected if s not in drop]
        assert v.display_order == expected
        assert v._tree_scope_all_checked(node) == (not drop)
        v._tree_add_scope_signals(node)
        expected.extend(s for s in scope_signal_ids(node) if s not in expected)
        assert v.display_order == expected
        assert v._tree_scope_all_checked(node)
    v.collapsed_scopes = {'top.unit1'}
    v._rebuild_display_order()
    assert v.display_order == [s for s in trace.signal_order if trace.signals[s].scope != 'top.unit1']


def test_groups_bus_expansion_reorder_delete_and_undo(trace, monkeypatch):
    v = viewer(trace, monkeypatch)
    original = list(v.display_order)
    v.sig_offset, v.focus_idx = 35, 0
    v.move_signal(-1)
    assert v.display_order[34:36] == ['s35', 's34']
    v.undo()
    assert v.display_order == original
    v._focus_on('s35')
    v.delete_signal()
    assert 's35' not in v._display_order_set
    v.undo()
    assert v.display_order == original
    v.expand_bus('s35')
    assert len(v.display_order) == 48
    assert v.expanded_buses['s35'][0] in v._display_order_set
    v.collapse_bus('s35')
    assert v.display_order == original
    restore = v._undo_groups_snapshot()
    v.create_group('inner', ['s2', 's35'], parent='outer')
    assert v.display_order[:5] == ['s0', 's1', 's2', 's35', 's3']
    assert isinstance(v.view_rows, DisplayOrder)
    v.toggle_group_collapse('outer')
    assert 's35' not in v.view_rows and ('\x01hdr', 'outer') in v.view_rows
    v._focus_on('s35')
    assert v.focused_signal_id() == 's35'
    restore()
    assert v.display_order == original and not v.groups
    v.selected_signals = {'s2', 's35'}
    v._kb_backspace_remove()
    assert 's2' not in v.display_order and 's35' not in v.display_order
    v.undo()
    assert v.display_order == original
    assert len(v.display_order.parts) == 1


def test_compact_sessions_remap_handles_and_reject_changed_layout(trace, tmp_path, monkeypatch):
    v = viewer(trace, monkeypatch)
    v._focus_on('s35')
    v.move_signal(-1)
    v._tree_remove_scope_signals(trace.scopes.children['top'].children['unit1'])
    v.sig_color = {'s2': 5}
    v.create_group('g', ['s2', 's35'])
    state = json.loads(json.dumps(v._session_state()))
    assert state['version'] == 3 and 'display_order' not in state
    assert len(state['signal_identities']) == 2
    expected = [s.replace('s', 'new', 1) for s in v.display_order]
    path = make_trace(tmp_path / 'new.vcd', prefix='new')
    with open_trace(path, use_cache=False) as other:
        restored, missing = remap_view_state(other, state)
        assert not missing
        assert isinstance(restored['display_order'], DisplayOrder)
        assert restored['display_order'] == expected
        assert restored['sig_color'] == {'new2': 5}
    eager = VCDFile.parse(path)
    restored, missing = remap_view_state(eager, state)
    assert not missing and restored['display_order'] == expected
    from alwavelets.snapshot import load_arrangement
    arrangement = tmp_path / 'compact.json'
    arrangement.write_text(json.dumps(state))
    names, _ = load_arrangement(eager, arrangement)
    assert names == [eager.signals[s].full_name for s in expected]
    with open_trace(make_trace(tmp_path / 'changed.vcd', changed=True), use_cache=False) as changed:
        with pytest.raises(ValueError, match='different declaration layout'):
            remap_view_state(changed, state)
    state['display_ranges']['parts'] = [[0, 1000000]]
    with pytest.raises(ValueError, match='invalid saved display range'):
        remap_view_state(trace, state)


@pytest.mark.parametrize('empty', [False, True])
def test_session_and_arrangement_roundtrip_avoid_catalog_enumeration(trace, monkeypatch, tmp_path, empty):
    v = viewer(trace, monkeypatch)
    if empty:
        v.display_order = []
    def forbidden(*args, **kwargs):
        raise AssertionError('whole-catalog enumeration')
    monkeypatch.setattr(trace.signals, 'rows', forbidden)
    state = v._session_state()
    assert len(json.dumps(state)) < 1500
    v.save_session()
    v.display_order = ['s0']
    assert v.restore_session()
    assert len(v.display_order) == (0 if empty else 40)
    path = tmp_path / 'arrangement.json'
    v.prompt_input = lambda *args: str(path)
    v.save_arrangement()
    v.display_order = ['s1']
    v.load_arrangement()
    assert len(v.display_order) == (0 if empty else 40)


def test_optional_filtered_order_and_nonexistent_focus(trace, monkeypatch):
    trace.signal_order = ['s4', 's1']
    v = viewer(trace, monkeypatch)
    assert v.display_order == ['s4', 's1']
    assert trace.signals['s39'].name == 'value39'
    assert member_positions(v.display_order, ['missing', 's1']) == [(1, 's1')]


def test_catalog_fingerprint_survives_reopen_and_older_index(tmp_path, monkeypatch):
    import sqlite3
    from alwavelets.storage import index_path
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = make_trace(tmp_path / 'cached.vcd')
    with open_trace(path) as trace:
        digest = trace.signals.layout_signature
    with open_trace(path) as trace:
        assert trace.signals.layout_signature == digest
    with sqlite3.connect(index_path(path)) as db:
        db.execute('DELETE FROM meta WHERE key="catalog_layout"')
    with open_trace(path) as trace:
        assert trace.signals.layout_signature == digest


def test_bulk_removal_and_import_keep_contiguous_runs_compact(trace):
    order = DisplayOrder.from_order(trace.signals, list(trace.signal_order))
    assert len(order.parts) == 1
    order.remove_ids(('s%d' % i for i in range(30)))
    assert order == ['s%d' % i for i in range(30, 40)]
    assert len(order.parts) == 1


def test_finder_snapshot_is_immutable_and_stale_results_are_discarded(tmp_path, monkeypatch):
    from alwavelets.finder_search import RankedMatches
    entered, release = threading.Event(), threading.Event()
    original = RankedMatches.search
    observed = []
    def slow(candidates, query, cancel, declaration_order=False):
        snapshot = candidates.order
        assert isinstance(snapshot, DisplayOrder)
        first = snapshot[0]
        entered.set()
        assert release.wait(5)
        assert snapshot[0] == first and len(snapshot) == 5000
        result = original(candidates, query, cancel, declaration_order)
        observed.append(result)
        return result
    monkeypatch.setattr(RankedMatches, 'search', slow)
    with open_trace(make_trace(tmp_path / 'large.vcd', count=5000), use_cache=False) as trace:
        v = viewer(trace, monkeypatch)
        try:
            v.open_finder()
            v.finder_query = 'value'
            v._finder_commit()
            assert entered.wait(5)
            v.display_order.pop(0)
            release.set()
            v._finder_next_worker.future.result(timeout=5)
            assert v.poll_finder_navigation()
            assert not v.search_results
            assert observed[0].db is None
        finally:
            release.set()
            v.close_finder()
            v.close_finder_navigation()
