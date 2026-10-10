"""Paged scope metadata must preserve order, navigation, and compact view state."""
import json
import random
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from alwavelets import cache, scope_store, storage
from alwavelets.hierarchy import ScopeIndex, TreeRows, scope_signal_ids, walk_scopes
from alwavelets.parsers import VCDFile
from alwavelets.fst import FSTReader
from alwavelets.scope_store import PagedTreeRows, ScopeFolds, ScopeHierarchy, fold_state, restore_folds
from test_display_order import viewer
from test_index_resume import interrupt_after_checkpoint
from tools.stress_vcd import create_vcd
from tools.stress_fst import create_fst


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.delenv('ALWAVELETS_CACHE_DIR', raising=False)
    monkeypatch.setattr(cache, 'DIRECTORY', None)


def trace_file(path, scopes):
    with path.open('w') as output:
        for i, scope in enumerate(scopes):
            parts = scope.split('.') if scope else ()
            for name in parts:
                output.write(f'$scope module {name} $end\n')
            output.write(f'$var wire 1 s{i} value{i} $end\n')
            output.write('$upscope $end\n' * len(parts))
        output.write('$enddefinitions $end\n#0\n0s0\n#2\n1s0\n')
    return path


def normalized(rows):
    return [(kind, node.full_path if kind == 'scope' else node.full_name, depth, opened, extra)
            for kind, node, depth, opened, extra in rows]


def test_random_expansions_and_subtree_counts_match_eager_tree(tmp_path, monkeypatch):
    rng = random.Random(921)
    scopes = ['', 'top', 'top.z', 'top.a', 'other']
    scopes += ['top.u%d.child%d' % (rng.randrange(15), rng.randrange(5)) for _ in range(300)]
    scopes += ['', 'top.a', 'top']
    path = trace_file(tmp_path / 'tree.vcd', scopes)
    eager = VCDFile.parse(path)
    monkeypatch.setattr(ScopeHierarchy, 'cache_limit', 3)
    with storage.open_trace(path) as trace:
        hierarchy = trace.scopes.hierarchy
        expected = [(node.full_path, list(node.signal_ids), node.total_signals()) for node in walk_scopes(eager.scopes)]
        assert [(node.full_path, list(node.signal_ids), node.total_signals()) for node in walk_scopes(trace.scopes)] == expected
        assert len(hierarchy.cache) <= 3
        held = hierarchy['top']
        list(walk_scopes(trace.scopes))
        assert held is hierarchy['top'] is trace.scopes.children['top']
        for _ in range(35):
            expanded = {path for path, _, _ in expected if rng.randrange(2)}
            actual = TreeRows(ScopeIndex(trace.scopes), expanded, trace.signals.__getitem__, lambda sid: True)
            reference = TreeRows(ScopeIndex(eager.scopes), expanded, eager.signals.__getitem__, lambda sid: True)
            assert isinstance(actual, PagedTreeRows)
            assert normalized(actual) == normalized(reference)
            assert normalized(actual[-3:]) == normalized(reference[-3:])
            with pytest.raises(IndexError):
                actual[len(actual)]


@pytest.mark.parametrize('rows,byte_limit', [(2, 256 * 1024), (256, 1024), (256, 256 * 1024)])
def test_batched_metadata_matches_eager_tree_across_eviction(tmp_path, monkeypatch, rows, byte_limit):
    rng = random.Random(783)
    # A descendant can precede its parent's direct signals; revisiting branches
    # after cache eviction must retain first-seen sibling and depth-first order.
    scopes = ['top.z.deep.last', 'top.a', 'top.z', '', 'top', 'other']
    scopes += [f'top.u{rng.randrange(40)}.child{rng.randrange(8)}.leaf' for _ in range(700)]
    scopes += [f'top.u{i}' for i in reversed(range(40))] + ['top.z', '', 'top.a']
    path = trace_file(tmp_path / 'batched.vcd', scopes)
    eager = VCDFile.parse(path)
    monkeypatch.setattr(ScopeHierarchy, 'cache_limit', 3)
    monkeypatch.setattr(scope_store, '_BATCH_ROWS', rows)
    monkeypatch.setattr(scope_store, '_BATCH_BYTES', byte_limit)
    monkeypatch.setattr(scope_store, '_CHILD_PAGES', 1)
    ordinals = {sid: i for i, sid in enumerate(eager.signal_order)}
    for cached in (False, True):
        with storage.open_trace(path, memory_mib=0.1) as trace:
            actual = list(walk_scopes(trace.scopes))
            expected = list(walk_scopes(eager.scopes))
            assert [node.full_path for node in actual] == [node.full_path for node in expected]
            for pre, (node, reference) in enumerate(zip(actual, expected)):
                ids = list(scope_signal_ids(reference))
                seq = [ordinals[sid] for sid in ids]
                assert list(node.signal_ids) == reference.signal_ids
                assert list(node.children) == list(reference.children)
                assert (node.pre, node.stop, node.total) == (pre, pre + len(list(walk_scopes(reference))), len(ids))
                assert (node.lo, node.hi, bool(node.ordered)) == (min(seq), max(seq), seq == list(range(min(seq), max(seq) + 1)))
                assert list(scope_signal_ids(node)) == ids
                hierarchy = trace.scopes.hierarchy
                assert [i for part in hierarchy.ranges(node) for i in part] == seq
                assert sorted(i for part in hierarchy.ranges(node, ordered=False) for i in part) == sorted(seq)


def test_scope_build_reads_pages_instead_of_each_sibling(tmp_path, monkeypatch):
    count = 3000
    source = trace_file(tmp_path / 'paged-build.vcd', (f'top.u{i}' for i in range(count)))
    selects = []
    build = ScopeHierarchy._build

    def counted(self):
        def record(sql):
            if sql.startswith('SELECT') and 'scope_nodes' in sql:
                selects.append(sql)
        self.store.db.set_trace_callback(record)
        try:
            build(self)
        finally:
            self.store.db.set_trace_callback(None)

    monkeypatch.setattr(ScopeHierarchy, '_build', counted)
    with storage.open_trace(source) as trace:
        assert trace.scopes.total == count
        assert trace.scopes.hierarchy[f'top.u{count - 1}'].lo == count - 1
    assert 0 < len(selects) < count // 10


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_wide_tree_expansion_end_jump_and_selection_do_not_walk_siblings(tmp_path, monkeypatch, kind):
    count = 12000
    path = tmp_path / ('wide.' + kind)
    if kind == 'vcd':
        trace_file(path, (f'top.u{i}' for i in range(count)))
    else:
        create_fst(path, signals=count, scope_per_signal=True)
    with storage.open_trace(path) as trace:
        last = trace.signal_order[-1]
        hierarchy = trace.scopes.hierarchy
        v = viewer(trace, monkeypatch)

        def no_walk(*args, **kwargs):
            raise AssertionError('this operation must not enumerate descendant scopes')

        monkeypatch.setattr(hierarchy, 'walk', no_walk)
        top = hierarchy['top']
        v.tree_expanded = {'top'}
        v._rebuild_tree()
        assert len(v._tree_flat) == count + 1 and len(v._tree_flat.runs) == 2
        assert v._tree_flat[-1][1].full_path == f'top.u{count - 1}'
        v.tree_expanded.add(f'top.u{count - 1}')
        v._rebuild_tree()
        assert v._tree_flat[-1][4] == last
        assert len(v._tree_flat.runs) == 3
        v._tree_remove_scope_signals(top)
        assert not v.display_order and not v._tree_scope_all_checked(top)
        v._tree_add_scope_signals(top)
        assert v.display_order.parts == [range(count)] and v._tree_scope_all_checked(top)
        v.collapse_all_modules()
        assert isinstance(v.collapsed_scopes, ScopeFolds) and len(v.collapsed_scopes) == count
        assert not v.display_order
        state = v._session_state()
        assert len(json.dumps(state)) < 2500
        assert not v.collapsed_scopes.extra and not v.collapsed_scopes.unfolded
        v.save_session()
        v.expand_all_modules()
        assert v.display_order.parts == [range(count)]
        assert v.restore_session() and not v.display_order
        assert f'top.u{count - 1}' in v.collapsed_scopes
        v.collapsed_scopes.discard(f'top.u{count - 1}')
        v._rebuild_display_order()
        assert v.display_order == [last]
        assert v.collapsed_scopes.expand('top')
        v._rebuild_display_order()
        assert v.display_order.parts == [range(count)] and not v.collapsed_scopes
        assert len(hierarchy.cache) <= hierarchy.cache_limit


def test_deep_hierarchy_uses_iterative_build_counts_and_rows(tmp_path):
    depth = 1800
    path = tmp_path / 'deep.vcd'
    path.write_text('$scope module m $end\n' * depth + '$var wire 1 ! last $end\n' +
                    '$upscope $end\n' * depth + '$enddefinitions $end\n#0\n1!\n')
    for cached in (False, True):
        with storage.open_trace(path, memory_mib=0.1) as trace:
            index = ScopeIndex(trace.scopes)
            assert trace.scopes.total_signals() == index.counts[trace.scopes] == 1
            assert len(index.nodes) == depth + 1
            expanded = {'.'.join(['m'] * i) for i in range(1, depth + 1)}
            rows = TreeRows(index, expanded, trace.signals.__getitem__, lambda sid: True)
            assert len(rows) == depth + 1 and rows[-1][4] == '!'
            assert rows[-1][2] == depth
            assert list(scope_signal_ids(trace.scopes)) == ['!']
            assert len(trace.scopes.hierarchy.cache) <= ScopeHierarchy.cache_limit


def drop_scope_metadata(path):
    with sqlite3.connect(path) as db:
        db.execute('DROP TABLE scope_nodes')
        db.execute('DROP TABLE scope_meta')
        return db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0]


@pytest.mark.parametrize('kind', ['vcd', 'fst'])
def test_legacy_completed_index_upgrades_without_reparsing(tmp_path, monkeypatch, kind):
    source = tmp_path / ('old.' + kind)
    if kind == 'vcd':
        trace_file(source, ['top.a', 'top.b', 'top.a'])
    else:
        create_fst(source, signals=3, scope_per_signal=True)
    with storage.open_trace(source) as trace:
        first = trace.signal_order[0]
        changes = trace.signals[first].changes
    original = drop_scope_metadata(storage.index_path(source))

    def no_parse(*args, **kwargs):
        raise AssertionError('scope upgrade must retain existing waveform pages')

    monkeypatch.setattr(VCDFile, 'parse_header', no_parse)
    monkeypatch.setattr(FSTReader, '_parse_hierarchy', no_parse)
    monkeypatch.setattr(storage.PageWriter, '__init__', no_parse)
    with storage.open_trace(source) as trace:
        assert trace.scopes.hierarchy['top'].total_signals() == 3
        assert trace.signals[first].changes == changes
        assert trace._store.db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0] == original
    with storage.open_trace(source) as trace:
        assert len(trace.scopes.hierarchy.cache) == 1


def test_legacy_partial_index_upgrades_and_keeps_resume_position(tmp_path, monkeypatch):
    source = tmp_path / 'partial.vcd'
    create_vcd(source, 16, 6000, 1, True)
    saved = interrupt_after_checkpoint(source, monkeypatch)
    drop_scope_metadata(storage.partial_index_path(source))

    def no_parse(*args, **kwargs):
        raise AssertionError('partial upgrade must not restart the source')

    monkeypatch.setattr(VCDFile, 'parse_header', no_parse)
    with storage.open_trace(source) as trace:
        assert trace._resume_offset == saved['checkpoint']['offset']
        assert trace.scopes.total_signals() == len(trace.signals)


def test_concurrent_legacy_readers_share_one_upgrade(tmp_path, monkeypatch):
    source = trace_file(tmp_path / 'concurrent.vcd', [f'top.u{i}' for i in range(600)])
    with storage.open_trace(source):
        pass
    drop_scope_metadata(storage.index_path(source))
    entered, second = threading.Event(), threading.Event()
    build, ensure = ScopeHierarchy._build, ScopeHierarchy._ensure
    builds = []

    def blocked_build(self):
        builds.append(1)
        entered.set()
        assert second.wait(5)
        build(self)

    def synchronized_ensure(self):
        if entered.is_set():
            second.set()
        ensure(self)

    def no_parse(*args, **kwargs):
        raise AssertionError('competing upgrades must not start waveform indexing')

    def read():
        with storage.open_trace(source) as trace:
            return trace.scopes.total_signals()

    monkeypatch.setattr(ScopeHierarchy, '_build', blocked_build)
    monkeypatch.setattr(ScopeHierarchy, '_ensure', synchronized_ensure)
    monkeypatch.setattr(VCDFile, 'parse_header', no_parse)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read)
        assert entered.wait(5)
        other = pool.submit(read)
        assert first.result(timeout=10) == other.result(timeout=10) == 600
    assert builds == [1]


def test_reload_rebinds_compact_folds_to_new_scope_metadata(tmp_path, monkeypatch):
    source = trace_file(tmp_path / 'reload.vcd', ['top', 'top.a', 'top.b'])
    trace = storage.open_trace(source)
    v = viewer(trace, monkeypatch)
    v.live_file = str(source)
    v.collapse_all_modules()
    v.collapsed_scopes.discard('top.a')
    v._rebuild_display_order()
    trace_file(source, ['top', 'top.a', 'top.b', 'top.c'])
    try:
        assert v._reload_preserving_view()
        assert v.collapsed_scopes.hierarchy is v.vcd.scopes.hierarchy
        assert set(v.collapsed_scopes) == {'top.b', 'top.c'}
        assert v.display_order == ['s0', 's1']
    finally:
        v.vcd.close()


@pytest.mark.parametrize('partial', [False, True])
def test_cancelled_upgrade_rolls_back_and_can_retry(tmp_path, monkeypatch, partial):
    source = trace_file(tmp_path / 'upgrade.vcd', [f'top.u{i}' for i in range(600)])
    if partial:
        with source.open('a') as output:
            output.write(''.join(f'#{t}\n{t % 2}s0\n' for t in range(3, 10000)))
        saved = interrupt_after_checkpoint(source, monkeypatch)
        target = storage.partial_index_path(source)
    else:
        with storage.open_trace(source):
            pass
        target = storage.index_path(source)
    original = drop_scope_metadata(target)
    cancel = threading.Event()
    check = scope_store.check_cancel
    calls = []

    def stop(event):
        calls.append(1)
        if len(calls) == 50:
            cancel.set()
        check(event)

    with monkeypatch.context() as patch:
        patch.setattr(scope_store, 'check_cancel', stop)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(source, cancel=cancel)
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0] == original
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name="scope_nodes"').fetchone()
    with storage.open_trace(source) as trace:
        assert trace.scopes.total_signals() == 600
        if partial:
            assert trace._resume_offset == saved['checkpoint']['offset']


@pytest.mark.parametrize('index', ['scope_children', 'scope_preorder', 'scope_depth'])
@pytest.mark.parametrize('partial', [False, True])
def test_cancelled_scope_index_sort_preserves_upgrade_and_checkpoint(tmp_path, monkeypatch, index, partial):
    source = trace_file(tmp_path / 'sort.vcd', (f'top.u{i}' for i in range(600)))
    if partial:
        with source.open('a') as output:
            output.write(''.join(f'#{t}\n{t % 2}s0\n' for t in range(3, 10000)))
        saved = interrupt_after_checkpoint(source, monkeypatch)
        target = storage.partial_index_path(source)
    else:
        with storage.open_trace(source):
            pass
        target = storage.index_path(source)
    original = drop_scope_metadata(target)
    cancel, statements = threading.Event(), []
    build = ScopeHierarchy._build

    def interrupted(self):
        def stop(sql):
            if sql.startswith('CREATE') and index in sql:
                statements.append(sql)
                cancel.set()
        self.store.db.set_trace_callback(stop)
        try:
            build(self)
        finally:
            self.store.db.set_trace_callback(None)

    with monkeypatch.context() as patch:
        patch.setattr(ScopeHierarchy, '_build', interrupted)
        with pytest.raises(ValueError, match='cancelled'):
            storage.open_trace(source, cancel=cancel)
    assert len(statements) == 1
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0] == original
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name="scope_nodes"').fetchone()
    with storage.open_trace(source) as trace:
        assert trace.scopes.total == 600
        if partial:
            assert trace._resume_offset == saved['checkpoint']['offset']


def test_fold_edits_and_state_match_explicit_sets(tmp_path):
    scopes = ['', 'top', 'top.a', 'top.a.nested', 'top.b', 'other.a', 'other.b', 'other.b.nested']
    source = trace_file(tmp_path / 'folds.vcd', scopes)
    eager = VCDFile.parse(source)
    with storage.open_trace(source) as trace:
        folds = ScopeFolds(trace.scopes.hierarchy)
        expected = {'top.a', 'top.b', 'other.a', 'other.b'}
        for action, path in [('discard', 'top.a'), ('add', 'top.a'), ('expand', 'other'),
                             ('add', 'other.b.nested'), ('add', 'other.a'), ('expand', 'top'),
                             ('discard', 'missing'), ('add', 'missing'), ('clear', '')]:
            if action == 'expand':
                expected = {p for p in expected if p != path and not p.startswith(path + '.')}
                folds.expand(path)
            elif action == 'clear':
                folds.clear()
                expected.clear()
            else:
                getattr(folds, action)(path)
                getattr(expected, action)(path)
            assert set(folds) == expected and len(folds) == len(expected)
            state = fold_state(folds)
            assert set(restore_folds(trace.scopes, state)) == expected
            assert restore_folds(eager.scopes, state) == expected
            visible = [sid for sid, sig in eager.signals.items() if not any(
                sig.scope == p or sig.scope.startswith(p + '.') for p in expected)]
            assert folds.order() == visible
