"""Virtual hierarchy rows, bounded redraws, selection and reload behavior."""
import curses
import random

import pytest

from alwavelets.core import AliasSignal
from alwavelets.hierarchy import ScopeIndex, TreeRows, scope_all_selected, scope_signal_ids
from alwavelets.parsers import ScopeNode, VCDFile, build_scope_tree
from tools.stress_tree import make_viewer


@pytest.fixture(autouse=True)
def headless_colors(monkeypatch):
    monkeypatch.setattr(curses, 'color_pair', lambda number: 0)


def reference_rows(viewer):
    rows = []
    def descend(node, depth):
        for child in node.children.values():
            expanded = child.full_path in viewer.tree_expanded
            rows.append(('scope', child, depth, expanded, child.total_signals()))
            if expanded:
                rows.extend(('signal', viewer.get_signal(sid), depth + 1,
                             sid in viewer._display_order_set, sid) for sid in child.signal_ids)
                descend(child, depth + 1)
    descend(viewer.scope_tree, 0)
    return rows


def test_virtual_rows_match_flattened_order_and_live_selection():
    viewer = make_viewer(300, 20)
    rng = random.Random(104)
    for _ in range(20):
        viewer.tree_expanded = {'top'} | {'top.block%d' % i for i in range(20) if rng.randrange(2)}
        viewer._rebuild_tree()
        expected = reference_rows(viewer)
        assert list(viewer._tree_flat) == expected
        assert viewer._tree_flat[-1] == expected[-1]
        assert viewer._tree_flat[3:19:2] == expected[3:19:2]
        assert viewer._tree_flat[::-1] == expected[::-1]
        with pytest.raises(IndexError):
            viewer._tree_flat[len(expected)]
        with pytest.raises(IndexError):
            viewer._tree_flat[-len(expected) - 1]
    viewer.tree_expanded = {'top', 'top.block19'}
    viewer._rebuild_tree()
    rows = viewer._tree_flat
    viewer._display_order_set.discard('299')
    assert rows[-1][4] == '299' and not rows[-1][3]
    viewer._display_order_set.add('299')
    assert rows[-1][3]


class IndexedIds:
    def __init__(self, size):
        self.size = size
        self.accesses = []

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        assert isinstance(index, int) and 0 <= index < self.size
        self.accesses.append(index)
        return str(index)

    def __iter__(self):
        raise AssertionError('should index visible signal ids, not scan them')


def test_million_signal_expand_uses_no_signal_objects_or_id_scan():
    root, top = ScopeNode(''), ScopeNode('top', 'top')
    root.children['top'] = top
    top.signal_ids = IndexedIds(1000000)
    looked_up = []
    def resolve(sid):
        looked_up.append(sid)
        return sid
    index = ScopeIndex(root)
    rows = TreeRows(index, {'top'}, resolve, lambda sid: sid == '999999')
    assert index.counts[top] == 1000000 and len(rows) == 1000001
    assert len(rows.runs) == 1 and not looked_up and not top.signal_ids.accesses
    assert rows[-1] == ('signal', '999999', 1, True, '999999')
    assert len(rows[500000:500020]) == 20
    assert len(looked_up) == len(top.signal_ids.accesses) == 21


def test_scope_cache_survives_expansion_and_cursor_is_clamped(monkeypatch):
    viewer = make_viewer(100, 2)
    viewer._rebuild_tree()
    index = viewer._scope_index
    top = viewer.scope_tree.children['top']
    assert viewer._tree_scope_all_checked(top)
    cached = dict(viewer._scope_check_cache)
    def fail(*args):
        raise AssertionError('expansion must reuse counts and display membership')
    monkeypatch.setattr(viewer, '_sync_display_set', fail)
    monkeypatch.setattr(ScopeNode, 'total_signals', fail)
    viewer.tree_expanded = {'top', 'top.block0', 'top.block1'}
    viewer._rebuild_tree()
    assert viewer._scope_index is index and viewer._scope_check_cache == cached
    viewer.tree_cursor = viewer.tree_scroll = len(viewer._tree_flat) - 1
    viewer.tree_expanded.clear()
    viewer._rebuild_tree()
    assert viewer.tree_cursor == viewer.tree_scroll == 0


def test_split_panel_only_reads_visible_scope_signals():
    viewer = make_viewer(1000)
    viewer._rebuild_tree()
    top = viewer.scope_tree.children['top']
    top.signal_ids = IndexedIds(1000000)
    viewer.vcd.signal_order = IndexedIds(1000000)
    viewer._scope_check_cache['top'] = True
    class NoMembershipScan(list):
        def __contains__(self, value):
            raise AssertionError('display membership must use the set')
    viewer.display_order = NoMembershipScan(viewer.display_order)
    resolved, original = [], viewer.get_signal
    def get_signal(sid):
        resolved.append(sid)
        return original(sid)
    viewer.get_signal = get_signal
    viewer.draw_panel_tree()
    assert 0 < len(resolved) < viewer.height
    assert not viewer.vcd.signal_order.accesses
    assert top.signal_ids.accesses == list(range(len(resolved)))
    assert any('signal0' in text for text in viewer.stdscr.lines.values())


@pytest.mark.parametrize('panel', [False, True])
def test_toggle_individual_and_nested_scope_keeps_checkmarks_current(panel):
    viewer = make_viewer(12, 2)
    viewer.tree_expanded = {'top', 'top.block0', 'top.block1'}
    viewer._rebuild_tree()
    tree = viewer._tree_flat
    handle = viewer.panel_handle_tree_key if panel else viewer.tree_handle_key
    top = viewer.scope_tree.children['top']
    assert viewer._tree_scope_all_checked(top)
    viewer.tree_cursor = len(tree) - 1
    handle(ord(' '))
    assert '11' not in viewer.display_order and '11' not in viewer._display_order_set
    assert viewer._tree_flat is tree and not tree[-1][3]
    assert not viewer._tree_scope_all_checked(top)
    handle(ord(' '))
    assert tree[-1][3] and viewer._tree_scope_all_checked(top)
    viewer.tree_cursor = 1  # top.block0
    handle(ord(' '))
    assert viewer.display_order == [str(i) for i in range(6, 12)]
    assert not viewer._tree_scope_all_checked(top)
    handle(ord(' '))
    assert len(viewer.display_order) == len(viewer._display_order_set) == 12
    assert viewer._tree_scope_all_checked(top)


def test_removal_and_undo_invalidate_scope_checkmarks():
    viewer = make_viewer(20)
    viewer._rebuild_tree()
    top = viewer.scope_tree.children['top']
    assert viewer._tree_scope_all_checked(top)
    viewer.remove_signals('top.signal19')
    assert not viewer._tree_scope_all_checked(top)
    viewer.undo()
    assert viewer._tree_scope_all_checked(top)
    viewer._rebuild_view_rows()
    viewer.delete_signal()
    assert not viewer._tree_scope_all_checked(top)
    viewer.undo()
    assert viewer._tree_scope_all_checked(top)
    viewer.selected_signals = {'18', '19'}
    viewer._kb_backspace_remove()
    assert not viewer._tree_scope_all_checked(top)
    viewer.undo()
    assert viewer._tree_scope_all_checked(top)


@pytest.mark.parametrize('panel', [False, True])
def test_page_and_end_navigation_keep_large_tree_accessible(panel):
    viewer = make_viewer(1000, 2)
    viewer.tree_expanded = {'top', 'top.block0', 'top.block1'}
    viewer._rebuild_tree()
    handle = viewer.panel_handle_tree_key if panel else viewer.tree_handle_key
    assert handle(curses.KEY_END)
    assert viewer.tree_cursor == len(viewer._tree_flat) - 1
    if panel:
        assert viewer.panel_selected_scope == 'top.block1'
    assert viewer._tree_flat[viewer.tree_cursor][4] == '999'
    handle(curses.KEY_PPAGE)
    assert 0 < viewer.tree_cursor < len(viewer._tree_flat) - 1
    handle(curses.KEY_HOME)
    assert viewer.tree_cursor == 0
    if panel:
        assert viewer.panel_selected_scope == 'top'
    handle(curses.KEY_NPAGE)
    assert viewer.tree_cursor > 0


def test_root_level_signals_and_aliases_remain_accessible():
    viewer = make_viewer(2)
    viewer.vcd.signals['0'].scope = ''
    alias = AliasSignal('alias', '1', 1, 'wire', 'top', viewer.vcd.signals['1'])
    viewer.vcd.signals['alias'] = alias
    viewer.vcd.signal_order.append('alias')
    viewer.scope_tree = build_scope_tree(viewer.vcd)
    viewer.tree_expanded = {'top'}
    viewer._rebuild_tree()
    assert viewer._tree_flat[0][4] == '0'
    assert viewer._tree_flat[-1][1] is alias
    assert viewer._scope_index.counts[viewer.scope_tree] == 3
    viewer.tree_cursor = len(viewer._tree_flat) - 1
    viewer.tree_handle_key(ord(' '))
    assert 'alias' in viewer.display_order and '1' in viewer.display_order
    viewer.panel_selected_scope = ''
    viewer.draw_panel_tree()
    assert any('signal0' in line for line in viewer.stdscr.lines.values())


def test_deep_scopes_do_not_recurse_and_cached_empty_scopes_are_checked():
    root = ScopeNode('')
    parent = root
    opened = set()
    for depth in range(1500):
        path = parent.full_path + '.m' if parent.full_path else 'm'
        child = ScopeNode('m', path)
        parent.children['m'] = child
        opened.add(path)
        parent = child
    parent.signal_ids = ['last']
    index = ScopeIndex(root)
    rows = TreeRows(index, opened, lambda sid: sid, lambda sid: True)
    assert len(rows) == 1501 and rows[-1][4] == 'last'
    assert rows[-1][2] == 1500
    assert index.counts[root] == 1 and list(scope_signal_ids(root)) == ['last']
    assert scope_all_selected(root, {'last'}, {})
    assert not scope_all_selected(root, set(), {})
    empty = ScopeNode('empty', 'empty')
    assert scope_all_selected(empty, set(), {})


def test_ungrouped_waveform_rows_do_not_scan_display_order():
    viewer = make_viewer(10)
    class NoIteration(list):
        def __iter__(self):
            raise AssertionError('ungrouped rows must not copy the full display order')
    viewer.display_order = NoIteration(viewer.display_order)
    assert viewer._rebuild_view_rows() is viewer.display_order
    assert viewer.view_rows[-1] == '9'


def test_replacing_hierarchy_drops_old_counts_nodes_and_checkmarks():
    viewer = make_viewer(20)
    viewer._rebuild_tree()
    old = viewer._scope_index
    assert viewer._tree_scope_all_checked(viewer.scope_tree.children['top'])
    updated = make_viewer(30, 3)
    viewer.vcd, viewer.scope_tree = updated.vcd, updated.scope_tree
    viewer.display_order = []
    viewer._sync_display_set()
    viewer.tree_expanded = {'top', 'top.block2'}
    viewer._rebuild_tree()
    assert viewer._scope_index is not old
    assert viewer._scope_index.counts[viewer.scope_tree] == 30
    assert viewer._tree_flat[-1][4] == '29' and not viewer._tree_flat[-1][3]
    assert not viewer._tree_scope_all_checked(viewer.scope_tree.children['top'])


def test_live_reload_uses_the_new_hierarchy(tmp_path):
    path = tmp_path / 'reload.vcd'
    def source(scope, name):
        return ('$scope module %s $end\n$var wire 1 ! %s $end\n'
                '$upscope $end\n$enddefinitions $end\n#0\n0!\n#10\n1!\n') % (scope, name)
    path.write_text(source('before', 'old'))
    viewer = make_viewer(1)
    viewer.vcd = VCDFile.parse(path)
    viewer.scope_tree = viewer.vcd.scopes
    viewer.panel_selected_scope = 'before'
    viewer._rebuild_tree()
    old = viewer._scope_index
    viewer.live_file = str(path)
    viewer.time_start, viewer.time_end = 0, 10
    viewer.cursor_a, viewer.cursor_b = 0, 5
    viewer.active_cursor, viewer.cursor_on, viewer.cursor_b_on = 'a', True, False
    viewer._row_cache, viewer._row_cache_window = {}, None
    viewer._clear_decode_results = lambda **kwargs: None
    path.write_text(source('after', 'new'))
    try:
        assert viewer._reload_preserving_view()
        assert viewer._scope_index is not old
        assert viewer.scope_tree is viewer.vcd.scopes
        assert 'before' not in viewer._scope_index.nodes
        assert 'after' in viewer._scope_index.nodes
        assert viewer.panel_selected_scope == 'after'
        viewer.tree_expanded = {'after'}
        viewer._rebuild_tree()
        assert viewer._tree_flat[-1][1].name == 'new'
    finally:
        viewer.vcd.close()


@pytest.mark.parametrize('unchecked', [False, True])
def test_optional_unchecked_start_keeps_browser_membership_consistent(monkeypatch, unchecked):
    from alwavelets import cli
    viewer = make_viewer(10)
    viewer.panel_mode = True
    viewer.tree_expanded = {'top'}
    viewer._rebuild_tree()
    assert viewer._tree_scope_all_checked(viewer.scope_tree.children['top'])
    viewer.run = lambda: None
    monkeypatch.setattr(cli, 'Alwavelets', lambda screen, trace: viewer)
    cli.main_curses(viewer.stdscr, viewer.vcd, start_collapsed=False,
                    load_unchecked=unchecked, use_session=False)
    assert len(viewer.vcd.signals) == 10
    assert len(viewer.display_order) == (0 if unchecked else 10)
    assert bool(viewer._display_order_set) is not unchecked
    assert viewer._tree_flat[-1][3] is not unchecked
    assert viewer._tree_scope_all_checked(viewer.scope_tree.children['top']) is not unchecked


def test_initial_display_filter_does_not_hide_other_declarations():
    viewer = make_viewer(20)
    viewer.vcd.signal_order = ['0']
    viewer.display_order = ['0']
    viewer._sync_display_set()
    viewer.tree_expanded = {'top'}
    viewer._rebuild_tree()
    viewer.draw_tree_browser()
    assert '1/20 signals in view' in viewer.stdscr.lines[viewer.height - 1, 0]
    assert len(viewer._tree_flat) == 21
    viewer.tree_handle_key(curses.KEY_END)
    viewer.tree_handle_key(ord(' '))
    assert viewer.display_order == ['0', '19']
    assert viewer._tree_flat[-1][3]
