"""Inline histories, bounded bulk writes, progressive publication and parity."""
import array
from fractions import Fraction
import itertools
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from alwavelets.catalog import SignalCatalog
from alwavelets.core import VCDSignal, time_bisect_left, time_bisect_right
from alwavelets.parsers import VCDFile
from alwavelets.storage import (PageWriter, SHORT_BYTES, TraceStore, VERSION,
                                index_path, open_trace)
from tools.stress_fst import create_fst


def writer_for(tmp_path, size=1, kind='wire'):
    trace = VCDFile()
    trace.signals['!'] = VCDSignal('value', '!', size, kind)
    trace.signal_order = ['!']
    store = TraceStore(str(tmp_path / 'pages.sqlite'), memory_bytes=65536)
    return trace, PageWriter(store, trace)


@pytest.mark.parametrize('size,kind,values,inline_count', [
    (1, 'wire', ['0', '1', 'x', 'z'] * 4, 8),
    (8, 'wire', ['0', 'x', '00001010', 'z'] * 4, 8),
    (8, 'wire', ['00000010', 'malformed', '', 'zzzzzzzz'] * 4, 8),
    (64, 'real', ['0.5', '-2.75', '1e100', 'nan'] * 4, 8),
    (256, 'wire', ['1' * 256, '0' * 256] * 8, 1),
    (1024, 'wire', ['1' * 1024, '0' * 1024] * 8, 0),
])
def test_inline_growth_publication_and_regular_page_transition(tmp_path, size, kind, values, inline_count):
    trace, writer = writer_for(tmp_path, size, kind)
    expected = VCDSignal('value', '!', size, kind)
    held = trace.signals['!']
    try:
        for i, value in enumerate(values):
            t = i // 3  # duplicate timestamps cross the inline/page boundary
            writer.add('!', t, value)
            writer.flush('!')
            assert held.num_changes() == i  # only published ordinals are visible
            assert held.changes == expected.changes
            expected.add_change(t, value)
            writer.publish(True)
            assert held.changes == expected.changes
            for timestamp in (-1, 0, 1, Fraction(3, 2), t, 100, 10 ** 30):
                assert time_bisect_left(held._times, timestamp) == time_bisect_left(expected._times, timestamp)
                assert time_bisect_right(held._times, timestamp) == time_bisect_right(expected._times, timestamp)
                assert held.value_at(timestamp) == expected.value_at(timestamp)
            for start, stop in ((0, t), (1, 2), (2, 2)):
                assert held.numeric_range_in_window(start, stop) == expected.numeric_range_in_window(start, stop)
        row = writer.store.db.execute('SELECT short_n,length(short_data) FROM signals WHERE sid="!"').fetchone()
        assert row[0] == inline_count
        assert row[1] is None or row[1] <= SHORT_BYTES
        assert writer.store.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0] > 0
        assert writer.store.peak_cache_bytes <= writer.store.memory_bytes
    finally:
        trace.close()


@pytest.mark.parametrize('count', [1, 4, 8])
def test_short_history_has_no_page_or_summary_rows_and_reopens(tmp_path, monkeypatch, count):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'short.vcd'
    path.write_text('$var wire 1 ! a $end\n$var wire 1 ! alias $end\n$enddefinitions $end\n' +
                    ''.join('#%d\n%d!\n' % (i // 2, i % 2) for i in range(count)))
    eager = VCDFile.parse(path)
    for _ in range(2):
        with open_trace(path) as trace:
            assert trace.signals['!'].changes == eager.signals['!'].changes
            assert trace.signals['! #1'].changes == eager.signals['!'].changes
            assert trace._store.total_changes == count * 2
            assert trace.load_changes == count
            assert trace._store.db.execute('SELECT COUNT(*) FROM pages').fetchone()[0] == 0
            assert trace._store.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0] == 0


def test_inline_prefix_followed_by_large_batches_keeps_summaries(tmp_path):
    trace, writer = writer_for(tmp_path)
    try:
        writer.add('!', 0, '1')
        writer.publish(True)
        for offset in range(0, 65536, 4096):
            writer.add_batch('!', array.array('q', range(offset, offset + 4096)), bytearray(b'0' * 4096))
        writer.publish(True)
        signal = trace.signals['!']
        assert signal.num_changes() == 65537
        assert signal.changes[:2] == [(0, '1'), (0, '0')]
        assert signal.value_at(0) == '0'
        assert signal.numeric_range_in_window(0, 65535) == (0, 1)
        assert writer.store.db.execute('SELECT MAX(level) FROM summaries').fetchone()[0] >= 1
    finally:
        trace.close()


@pytest.mark.parametrize('kind,width,value', [('wire', 1, '1'), ('wire', 8, 'xz10'),
                                            ('real', 64, '-2.5'), ('wire', 4096, '1' * 4096)])
def test_bulk_initial_values_match_regular_ingestion(tmp_path, kind, width, value):
    trace, writer = writer_for(tmp_path, width, kind)
    expected = VCDSignal('value', '!', width, kind)
    try:
        initials = [('!', value), ('!', '0')]
        initials.extend(('missing%d' % i, value) for i in range(600))
        initials.append(('!', '0'))  # duplicate after an intervening batch
        writer.add_initial_values(2, initials)
        writer.publish(True)
        expected.add_change(2, value)
        assert trace.signals['!'].changes == expected.changes
        # A later initial-frame pass must never overwrite an earlier chain.
        writer.add_initial_values(0, [('!', '0')])
        writer.publish(True)
        assert trace.signals['!'].changes == expected.changes
        writer.add('!', 2, '0')
        writer.publish(True)
        expected.add_change(2, '0')
        assert trace.signals['!'].changes == expected.changes
    finally:
        trace.close()


def test_inline_history_is_reordered_stably_after_publication(tmp_path):
    trace, writer = writer_for(tmp_path)
    try:
        for timestamp, value in [(10, '1'), (0, '0'), (0, 'x'), (2, '1')]:
            writer.add('!', timestamp, value)
            writer.publish(True)
        assert writer.disordered
        writer.reorder()
        assert trace.signals['!'].changes == [(0, '0'), (0, 'x'), (2, '1'), (10, '1')]
        assert writer.store.total_changes == 4
    finally:
        trace.close()


def test_declaration_batches_resolve_aliases_duplicates_ranges_and_scope_order(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'declarations.vcd'
    declarations = ('$scope module z $end\n$var wire 8 ! bus [0:7] $end\n' +
                    '$var wire 8 ! bus [0:7] $end\n' * 3 +
                    '$var wire 8 ! alias [10:3] $end\n$upscope $end\n$scope module a $end\n' +
                    ''.join('$var wire 1 s%d value%d $end\n' % (i, i) for i in range(1200)) +
                    '$upscope $end\n$scope module z $end\n$var wire 8 ! later [4:11] $end\n'
                    '$var wire 8 ! bus [0:7] $end\n$upscope $end\n')
    path.write_text(declarations + '$enddefinitions $end\n#0\nb110 !\n')
    eager = VCDFile.parse(path)
    from alwavelets.hierarchy import walk_scopes
    original = SignalCatalog.flush_declarations
    batches = []
    def track(self):
        if self.pending:
            batches.append((len(self.pending), self.pending_bytes))
        return original(self)
    monkeypatch.setattr(SignalCatalog, 'flush_declarations', track)
    for _ in range(2):
        with open_trace(path) as trace:
            assert trace.signal_order == eager.signal_order
            assert [(s.full_path, list(s.signal_ids)) for s in walk_scopes(trace.scopes)] == [
                (s.full_path, list(s.signal_ids)) for s in walk_scopes(eager.scopes)]
            for key in trace.signal_order:
                assert trace.signals[key].identity() == eager.signals[key].identity()
            assert trace._store.total_changes == 3
    assert 2 < len(batches) < 10
    assert max(n for n, _ in batches) <= 512
    assert max(size for _, size in batches) <= 257 * 1024


@pytest.mark.parametrize('cancel', [False, True])
def test_wide_fst_publishes_initial_values_before_finishing_and_cleans_up(tmp_path, monkeypatch, cancel):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'wide.fst'
    create_fst(path, signals=30000)
    entered, release = threading.Event(), threading.Event()
    original = PageWriter._initial_batch
    original_publish = PageWriter.publish
    ticks = itertools.count()
    monkeypatch.setattr('alwavelets.storage.time', SimpleNamespace(monotonic=lambda: next(ticks) * 0.3))
    batches = []
    def track(self, timestamp, batch):
        batches.append(len(batch))
        original(self, timestamp, batch)
    def pause(self, force=False):
        original_publish(self, force)
        if self.store.total_changes and not entered.is_set():
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(PageWriter, '_initial_batch', track)
    monkeypatch.setattr(PageWriter, 'publish', pause)
    trace = open_trace(path, background=True, memory_mib=0.1)
    try:
        assert entered.wait(5)
        assert trace.is_loading and not trace._load_error
        assert trace.signals['1'].value_at(0) == '0'
        assert 0 < trace.load_changes < 30000
        assert trace.signals['30000'].num_changes() == 0
        if cancel:
            trace._store.cancel.set()
        release.set()
        trace._store.thread.join(10)
        assert not trace.is_loading
        if cancel:
            assert 'cancelled' in trace._load_error.lower()
            assert not Path(index_path(path)).exists()
        else:
            trace.require_loaded()
            assert trace.load_changes == trace._store.total_changes == 30000
            assert trace.signals['30000'].value_at(0) == '0'
        assert max(batches) <= 512
    finally:
        release.set()
        trace.close()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))


def test_old_index_version_is_rebuilt(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'small.vcd'
    path.write_text('$var wire 1 ! a $end\n$enddefinitions $end\n#0\n1!\n')
    with open_trace(path) as trace:
        assert trace.signals['!'].value_at(0) == '1'
    with sqlite3.connect(index_path(path)) as db:
        meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        meta['version'] -= 1
        db.execute('UPDATE meta SET value=? WHERE key="trace"', (json.dumps(meta),))
        db.commit()
    with open_trace(path) as trace:
        assert trace.signals['!'].value_at(0) == '1'
        assert trace.load_changes == 1
        meta = json.loads(trace._store.db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        assert meta['version'] == VERSION
