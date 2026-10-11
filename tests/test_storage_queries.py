"""Page-local reads preserve published bounds, delta order and cache limits."""
import array
import bisect
from fractions import Fraction
import random

import pytest

from alwavelets import storage
from alwavelets.catalog import CatalogAlias
from alwavelets.core import TimeScaledSignal, VCDSignal
from alwavelets.parsers import VCDFile
from test_writer_metadata import writer_for


@pytest.mark.parametrize('kind,width', [('wire', 1), ('wire', 7), ('wire', 1024), ('real', 64)])
@pytest.mark.parametrize('inline', [False, True])
def test_queries_match_eager_across_duplicates_gaps_and_integer_limits(tmp_path, monkeypatch, kind, width, inline):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 8 if inline else 0)
    trace = VCDFile()
    expected = VCDSignal('value', '!', width, kind)
    trace.signals['!'], trace.signal_order = expected, ['!']
    store = storage.TraceStore(tmp_path / 'query.sqlite')
    writer = storage.PageWriter(store, trace)
    times = [-(1 << 63), -(1 << 63), -7, 5, 5, 5, 5, 5, 9, 30, 30, (1 << 63) - 1]
    values = [repr(i / 4) if kind == 'real' else
              ('xz'[i % 2] * width if i % 4 == 0 else format(i % (1 << width), f'0{width}b'))
              for i in range(len(times))]
    try:
        for start in range(0, len(times), 3):
            batch = values[start:start + 3]
            writer.add_batch('!', array.array('q', times[start:start + 3]),
                             bytearray(''.join(batch), 'ascii') if width == 1 else batch)
        writer.publish(True)
        for time, value in zip(times, values):
            expected.add_change(time, value)
        got = trace.signals['!']
        alias = CatalogAlias('alias', '!', width, kind, '', got)
        scaled, eager_scaled = TimeScaledSignal(got, Fraction(1, 3)), TimeScaledSignal(expected, Fraction(1, 3))
        queries = times + [-10**30, 10**30, -8, -6, 4, 6, 10, 29, 31, Fraction(9, 2), Fraction(11, 2)]
        random.Random(7).shuffle(queries)
        for cold in (True, False):
            for time in queries:
                if cold:
                    store.pages.clear()
                    store.cache_bytes = 0
                assert got.value_at(time) == alias.value_at(time) == expected.value_at(time)
                assert store.bisect('!', time) == bisect.bisect_left(times, time)
                assert store.bisect('!', time, True) == bisect.bisect_right(times, time)
                assert scaled.value_at(Fraction(time, 3)) == eager_scaled.value_at(Fraction(time, 3))
        assert got.value_at(5) == values[7]
        assert list(got.iter_changes(-7, 30)) == list(expected.iter_changes(-7, 30))
    finally:
        trace.close()


def test_nearby_time_and_ordinal_reads_need_no_sql_after_warming(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1)
    try:
        writer.add_batch('0', array.array('q', range(100)), [format(i, '016b') for i in range(100)])
        writer.publish(True)
        signal, store = trace.signals['0'], trace._store
        assert signal.value_at(20) == format(20, '016b')
        statements = []
        store.db.set_trace_callback(statements.append)
        for time in range(21, 80):
            assert signal.value_at(time) == format(time, '016b')
            assert store.bisect('0', time) == time
            assert store.bisect('0', time, True) == time + 1
            assert signal._times[time] == time
            assert signal.value_str(time) == format(time, '016b')
        assert not statements
    finally:
        trace.close()


def test_gap_read_does_not_decompress_following_page(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1)
    try:
        for start in (0, 100):
            writer.add_batch('0', array.array('q', [start, start + 1]), ['0' * 16, '1' * 16])
        writer.publish(True)
        signal, store = trace.signals['0'], trace._store
        assert signal.value_at(50) == '1' * 16
        assert store.page_reads == 1
        assert [page._times[0] for page, _ in store.pages.values()] == [0]
    finally:
        trace.close()


@pytest.mark.parametrize('inline', [False, True])
def test_hints_follow_publication_and_hide_unpublished_same_tick_events(tmp_path, monkeypatch, inline):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 8 if inline else 0)
    trace, writer, _ = writer_for(tmp_path, count=1)
    try:
        writer.add_batch('0', array.array('q', [1, 5]), ['0' * 16, '1' * 16])
        writer.publish(True)
        signal, store = trace.signals['0'], trace._store
        assert signal.value_at(5) == '1' * 16
        writer.add_batch('0', array.array('q', [5, 9]), ['x' * 16, 'z' * 16])
        writer._drain_pages()  # Physical data exists, but the readable count is still two.
        for time in (5, 9, 100):
            assert signal.value_at(time) == '1' * 16
            assert store.bisect('0', time, True) == 2
        assert signal.num_changes() == 2
        writer.publish(True)
        assert signal.value_at(5) == 'x' * 16
        assert signal.value_at(100) == 'z' * 16
        assert store.bisect('0', 5) == 1 and store.bisect('0', 5, True) == 3
        # Reordering reuses page ids: old locations must not survive it.
        writer.add_batch('0', array.array('q', [0]), ['0' * 15 + '1'])
        writer.publish(True)
        assert signal.value_at(0) == 'x'
        writer.reorder()
        assert signal.value_at(0) == '0' * 15 + '1'
        assert signal.value_at(5) == 'x' * 16
    finally:
        trace.close()


def test_hint_count_is_bounded_and_eviction_does_not_retain_decoded_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'PAGE_HINTS', 3)
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=8, width=1024)
    store = trace._store
    store.memory_bytes = 64 * 1024
    try:
        for i in range(8):
            writer.add_batch(str(i), array.array('q', range(30)), [str(i % 2) * 1024] * 30)
        writer.publish(True)
        for _ in range(2):
            for i in range(8):
                signal = trace.signals[str(i)]
                assert signal.value_at(15) == str(i % 2) * 1024
                assert store.bisect(str(i), 15) == 15
                assert len(store.page_hints) <= 3
                assert store.cache_bytes <= store.memory_bytes
        assert store.page_reads >= 16
        # An oversized decoded page can still be queried, but cannot be retained
        # through its location hint after the main cache rejects it.
        store.memory_bytes = 1
        store.pages.clear()
        store.cache_bytes = 0
        signal = trace.signals['0']
        for _ in range(2):
            assert signal.value_at(15) == '0' * 1024
            assert '0' not in store.page_hints and not store.pages
    finally:
        trace.close()
    assert not store.page_hints
