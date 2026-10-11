"""Bounded declarations and ranking, preserving identity and eager semantics."""
import curses
import gc
import sqlite3
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from alwavelets.catalog import SignalCatalog, SignalOrder, CatalogField
from alwavelets.finder import FinderMixin, fuzzy_match
from alwavelets.finder_search import FinderCandidates, FinderSearch, RankedMatches
from alwavelets.hierarchy import ScopeIndex, TreeRows, walk_scopes
from alwavelets.parsers import VCDFile
from alwavelets.storage import open_trace, use_disk_storage, index_path


def wide_vcd(path, count=200, events=True):
    with path.open('w') as f:
        f.write('$scope module top $end\n')
        for i in range(count):
            f.write('$var wire 1 s%d signal%d $end\n' % (i, i))
        f.write('$upscope $end\n$enddefinitions $end\n')
        if events:
            for t in (4, 0, 3, 3, 9):
                f.write('#%d\n' % t)
                for i in range(count):
                    f.write('%ds%d\n' % ((t + i) % 2, i))
    return path


def viewer(trace):
    v = SimpleNamespace(vcd=trace, height=30, messages=[])
    v._show_message = v.messages.append
    for name in ('open_finder', 'close_finder', '_finder_refilter', 'poll_finder', 'finder_handle_key'):
        setattr(v, name, getattr(FinderMixin, name).__get__(v))
    return v


@pytest.mark.parametrize('fst', [False, True])
def test_cold_and_cached_catalog_do_not_enumerate_signal_objects(tmp_path, monkeypatch, fst):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(SignalCatalog, 'cache_limit', 8)
    path = tmp_path / ('wide.fst' if fst else 'wide.vcd')
    if fst:
        from tools.stress_fst import create_fst
        create_fst(path, signals=2500)
    else:
        wide_vcd(path, 2500, events=False)
    def forbidden(self):
        raise AssertionError('opening a catalog must not enumerate signal objects')
    monkeypatch.setattr(SignalCatalog, '__iter__', forbidden)
    for cached in (False, True):
        with open_trace(path, memory_mib=0.1) as trace:
            catalog = trace.signals
            assert isinstance(catalog, SignalCatalog) and len(catalog) == 2500
            assert isinstance(trace.signal_order, SignalOrder)
            assert len(catalog.cache) <= 8
            if cached:
                assert not catalog.cache and trace._store.page_reads == 0
            index = ScopeIndex(trace.scopes)
            rows = TreeRows(index, {'top'}, catalog.__getitem__, lambda sid: False)
            assert len(rows) == 2501 and rows[-1][1].name == 'signal2499'
            assert rows[-1][4] == ('2500' if fst else 's2499')
            assert isinstance(trace.scopes.children['top'].signal_ids, SignalOrder)
            assert trace.signal_order[-2:] == list(trace.signal_order)[-2:]


def test_alias_identity_ranges_and_scope_order_survive_eviction_and_reopen(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(SignalCatalog, 'cache_limit', 1)
    path = tmp_path / 'aliases.vcd'
    path.write_text('$var wire 1 r root $end\n$scope module z $end\n'
                    '$var wire 8 ! bus [0:7] $end\n$upscope $end\n'
                    '$scope module a $end\n$var wire 8 ! alias [10:3] $end\n$upscope $end\n'
                    '$scope module z $end\n$var wire 1 x flag [9] $end\n'
                    '$var wire 1 x flag [9] $end\n$upscope $end\n$enddefinitions $end\n'
                    '#0\nb00000101 !\n0x\n1r\n#2\nb10101010 !\n')
    eager = VCDFile.parse(path)
    for _ in range(2):
        with open_trace(path) as trace:
            assert trace.signal_order == eager.signal_order
            assert [(n.full_path, list(n.signal_ids)) for n in walk_scopes(trace.scopes)] == [
                (n.full_path, list(n.signal_ids)) for n in walk_scopes(eager.scopes)]
            assert trace._store.total_changes == sum(s.num_changes() for s in eager.signals.values())
            assert trace.load_changes == eager.load_changes
            master = trace.signals['!']
            alias = trace.signals['! #2']
            trace.signals['x']
            assert alias.master is master is trace.signals['!']
            for key in trace.signal_order:
                assert trace.signals[key].identity() == eager.signals[key].identity()
                assert trace.signals[key].changes == eager.signals[key].changes
            assert alias.bit_label(0) == 3


def test_ingestion_state_evicts_and_repairs_disordered_values(tmp_path, monkeypatch):
    original = CatalogField.__init__
    fields = []
    def tiny(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.limit = 2
        fields.append(self)
    monkeypatch.setattr(CatalogField, '__init__', tiny)
    path = wide_vcd(tmp_path / 'wide.vcd', 70)
    eager = VCDFile.parse(path)
    with open_trace(path, use_cache=False, memory_mib=0.1) as trace:
        for sid in trace.signal_order:
            assert trace.signals[sid].changes == eager.signals[sid].changes
        assert all(len(field.cache) <= 2 for field in fields)
        assert trace.load_changes == 350


def test_auto_storage_accounts_for_header_metadata(tmp_path, monkeypatch):
    from alwavelets import storage
    path = wide_vcd(tmp_path / 'wide.vcd', 100, events=False)
    monkeypatch.setattr(storage, 'AUTO_THRESHOLD', 20000)
    assert path.stat().st_size < 20000
    assert use_disk_storage(path)
    assert not use_disk_storage(path, 'memory')


def test_header_cancellation_cleans_index(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    cancel = threading.Event()
    original = SignalCatalog.declare
    def stop(self, *args, **kwargs):
        if len(self) == 10:
            cancel.set()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SignalCatalog, 'declare', stop)
    path = wide_vcd(tmp_path / 'wide.vcd', events=False)
    with pytest.raises(ValueError, match='cancelled'):
        open_trace(path, cancel=cancel)
    assert not Path(index_path(path)).exists()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))


@pytest.mark.parametrize('query', ['signal', 's1', 'top 9', 'missing', 'signal199'])
@pytest.mark.parametrize('filtered', [False, True])
def test_paged_fuzzy_ranking_matches_eager_exactly(tmp_path, query, filtered):
    path = wide_vcd(tmp_path / 'wide.vcd', events=False)
    with open_trace(path, use_cache=False) as trace:
        if filtered:
            trace.signal_order = ['s199', 's100', 's19', 's1']
        candidates = FinderCandidates(trace)
        expected = []
        for i, name in enumerate(candidates.names()):
            score = fuzzy_match(query, name)
            if score is not None:
                expected.append((-score, i))
        expected = [i for _, i in sorted(expected)]
        matches = RankedMatches.search(candidates, query, threading.Event())
        try:
            assert list(matches) == expected
            assert list(matches[1:17]) == expected[1:17]
            if expected:
                assert matches[-1] == expected[-1]
            assert not trace.signals.cache
            # Optional filtering never removes other declarations.
            assert trace.signals['s0'].name == 'signal0'
        finally:
            matches.close()
        assert not Path(matches.path).exists()


def test_finder_open_is_lazy_and_pending_enter_does_not_select_stale_result(tmp_path, monkeypatch):
    with open_trace(wide_vcd(tmp_path / 'wide.vcd', events=False), use_cache=False) as trace:
        v = viewer(trace)
        v.open_finder()
        assert isinstance(v.finder_matches, range)
        assert not trace.signals.cache
        v.finder_handle_key(curses.KEY_END)
        assert v.finder_sel == 199
        entered, release = threading.Event(), threading.Event()
        original = RankedMatches.search
        def slow(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)
        monkeypatch.setattr(RankedMatches, 'search', slow)
        try:
            v.finder_query = 'signal1'
            v._finder_refilter()
            assert entered.wait(5)
            assert v.finder_handle_key(10) is True and v._finder_busy
            assert not v.finder_matches
            release.set()
            v._finder_worker.future.result(timeout=5)
            assert v.poll_finder() and len(v.finder_matches) > 1
            result_path = v.finder_matches.path
            v.finder_handle_key(21)
            assert not v._finder_busy and len(v.finder_matches) == 200
            assert not Path(result_path).exists()
            v.finder_handle_key(27)
            assert not trace._finder_workers
        finally:
            release.set()
            v.close_finder()


def test_superseded_results_are_discarded_and_trace_close_releases_claimed_results(tmp_path, monkeypatch):
    trace = open_trace(wide_vcd(tmp_path / 'wide.vcd', events=False), use_cache=False)
    worker = FinderSearch(trace)
    candidates = FinderCandidates(trace)
    entered, release = threading.Event(), threading.Event()
    old_paths = []
    original = RankedMatches.search
    def late(candidates, query, cancel):
        # Model a result completed just before cancellation is delivered.
        result = original(candidates, query, threading.Event())
        if query == 'signal':
            old_paths.append(result.path)
            entered.set()
            assert release.wait(5)
        return result
    monkeypatch.setattr(RankedMatches, 'search', late)
    try:
        worker.request(candidates, 'signal')
        assert entered.wait(5)
        worker.request(candidates, 'signal199')
        release.set()
        worker.future.result(timeout=5)
        result = worker.poll()
        assert len(result) == 1 and candidates[result[0]][0] == 's199'
        assert all(not Path(p).exists() for p in old_paths)
        path = result.path
        trace.close()
        assert not Path(path).exists() and not trace._finder_workers
    finally:
        release.set()
        worker.close()
        trace.close()


def test_search_cancellation_cleans_files(tmp_path, monkeypatch):
    opened = []
    original = RankedMatches.__init__
    def track(self):
        original(self)
        opened.append(self)
    monkeypatch.setattr(RankedMatches, '__init__', track)
    cancel = threading.Event()
    class Candidates:
        def names(self):
            for i in range(2000):
                if i == 800:
                    cancel.set()
                yield 'signal%d' % i
    with pytest.raises((ValueError, sqlite3.OperationalError)):
        RankedMatches.search(Candidates(), 'signal', cancel)
    assert opened and all(not Path(r.path).exists() for r in opened)


def test_catalog_cache_does_not_keep_scanned_signals_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(SignalCatalog, 'cache_limit', 8)
    with open_trace(wide_vcd(tmp_path / 'wide.vcd', events=False), use_cache=False) as trace:
        for sid in trace.signal_order:
            trace.signals[sid]
        gc.collect()
        assert len(trace.signals.cache) == len(trace.signals.live) == 8


@pytest.mark.parametrize('change_selection', [False, True])
def test_committing_large_finder_seeds_next_match_asynchronously(change_selection):
    from tools.stress_tree import make_viewer
    v = make_viewer(5000)
    focused = []
    v._focus_on = focused.append
    v.search_results = []
    try:
        v.open_finder()
        v.finder_query = 'signal4999'
        v._finder_refilter()
        v._finder_worker.future.result(timeout=5)
        v.poll_finder()
        assert v.finder_handle_key(10) is False
        assert v._finder_next_worker is not None and not v.search_results
        v.next_search_result()
        if change_selection:
            v.display_order.pop()
        v._finder_next_worker.future.result(timeout=5)
        assert v.poll_finder()
        assert focused == ([] if change_selection else ['4999'])
        assert not v._finder_next_worker
    finally:
        v.close_finder()
        v.close_finder_navigation()
        v.vcd.close()


def test_sort_cancellation_interrupts_sql_and_cleans_results(monkeypatch):
    cancel = threading.Event()
    paths = []
    original_init, connect = RankedMatches.__init__, sqlite3.connect
    def track(self):
        original_init(self)
        paths.append(self.path)
    class InterruptSort(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith('INSERT INTO ranked'):
                cancel.set()
            return super().execute(sql, parameters)
    monkeypatch.setattr(RankedMatches, '__init__', track)
    monkeypatch.setattr(sqlite3, 'connect', lambda *a, **kw: connect(*a, **kw, factory=InterruptSort))
    candidates = SimpleNamespace(names=lambda: ('signal%d' % i for i in range(5000)))
    with pytest.raises(sqlite3.OperationalError, match='interrupted'):
        RankedMatches.search(candidates, 'signal', cancel)
    assert paths and all(not Path(path).exists() for path in paths)


def test_navigation_candidates_include_virtual_signals(tmp_path):
    from alwavelets.core import VirtualBitSignal
    path = tmp_path / 'bus.vcd'
    path.write_text('$var wire 8 ! bus $end\n$enddefinitions $end\n#0\nb0 !\n')
    with open_trace(path, use_cache=False) as trace:
        bit = VirtualBitSignal(trace.signals['!'], 2)
        candidates = FinderCandidates(trace, ['!', bit.identifier], {bit.identifier: bit})
        assert list(candidates.names()) == ['bus', bit.full_name.lower()]
        assert candidates[1][1] == bit.full_name
        results = RankedMatches.search(candidates, bit.full_name.lower(), threading.Event(), True)
        try:
            assert list(results) == [1]
        finally:
            results.close()


def test_header_uses_published_count_without_loading_declarations(tmp_path, monkeypatch):
    from alwavelets.ui_drawing import DrawingMixin
    from tools.stress_tree import Screen
    with open_trace(wide_vcd(tmp_path / 'wide.vcd'), use_cache=False) as trace:
        assert trace._store.total_changes == 1000
        def forbidden(self):
            raise AssertionError('header must not scan the catalog')
        monkeypatch.setattr(SignalCatalog, '__iter__', forbidden)
        monkeypatch.setattr(curses, 'color_pair', lambda number: 0)
        screen = Screen()
        v = SimpleNamespace(vcd=trace, width=160, display_order=trace.signal_order,
                            C_HEADER=0, stdscr=screen, show_menu_bar=False)
        DrawingMixin.draw_header(v)
        assert any('Chg:1,000' in text for (row, _), text in screen.lines.items() if row == 0)


def test_scope_iteration_uses_index_without_sorting_the_entire_scope(tmp_path):
    from itertools import islice
    with open_trace(wide_vcd(tmp_path / 'wide.vcd', 3000, events=False), use_cache=False) as trace:
        calls = [0]
        def progress():
            calls[0] += 1
            return calls[0] > 10
        trace._store.db.set_progress_handler(progress, 1000)
        try:
            ids = trace.scopes.children['top'].signal_ids
            assert list(islice(ids, 10)) == ['s%d' % i for i in range(10)]
        finally:
            trace._store.db.set_progress_handler(None, 0)
