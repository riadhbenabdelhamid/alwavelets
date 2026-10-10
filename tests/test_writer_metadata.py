"""Buffered metadata stays bounded and follows the pages it describes."""
import array
from collections import Counter

import pytest

from alwavelets import storage
from alwavelets.core import VCDSignal
from alwavelets.parsers import VCDFile


def writer_for(tmp_path, count=64, width=16):
    trace = VCDFile()
    expected = {}
    for i in range(count):
        key = str(i)
        expected[key] = trace.signals[key] = VCDSignal(key, key, width)
        trace.signal_order.append(key)
    store = storage.TraceStore(str(tmp_path / 'pages.sqlite'), 8 * 1024 * 1024)
    return trace, storage.PageWriter(store, trace), expected


@pytest.mark.parametrize('batches', [False, True])
def test_metadata_writes_scale_with_pages_not_buffered_events(tmp_path, batches):
    trace, writer, expected = writer_for(tmp_path)
    statements = Counter()
    writer.store.db.set_trace_callback(lambda sql: statements.update([sql.split(' SET ')[0]]))
    try:
        for tick in range(100):
            batch = {}
            for key, signal in expected.items():
                value = format(tick, '016b')
                signal.add_change(tick, value)
                batch[key] = [(tick, value)]
                if not batches:
                    writer.add(key, tick, value)
            if batches:
                writer.add_vcd_batch(batch)
        assert statements['UPDATE signals'] == 0
        assert trace.signals['0'].num_changes() == 0
        assert writer.buffer_bytes <= writer.buffer_limit
        writer.publish(True)
        # One paired timestamp/count write per signal plus readable counts.
        assert statements['UPDATE signals'] <= len(expected) + 1
        for key, signal in expected.items():
            assert trace.signals[key].changes == signal.changes
        assert writer.store.db.execute('SELECT written,last_time,n FROM signals WHERE sid="0"').fetchone() == (100, 99, 100)
        assert not writer.pending_positions and not writer.offsets
    finally:
        trace.close()


def test_prefetched_positions_survive_queue_drain_and_cache_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 1)
    monkeypatch.setattr(storage, 'WRITE_BATCH_PAGES', 3)
    trace, writer, expected = writer_for(tmp_path, count=5)
    writer.position_limit = 1
    try:
        for keys, tick in [('12', 0), ('03412', 1), ('43210', 1), ('01234', 2)]:
            batch = {}
            for key in keys:
                value = format(tick + int(key), '016b')
                batch[key] = [(tick, value)]
                expected[key].add_change(tick, value)
            writer.add_vcd_batch(batch)
            assert len(writer.positions) <= 1
            assert len(writer.pending_positions) <= len(writer.pending_pages) < 3
        writer.publish(True)
        for key, signal in expected.items():
            assert trace.signals[key].changes == signal.changes
            assert writer._position(key) == (signal.num_changes(), signal._times[-1])
    finally:
        trace.close()


def test_fst_initial_detection_includes_builders_and_queued_pages(tmp_path):
    trace, writer, expected = writer_for(tmp_path, count=1, width=4096)
    value = '1' * 4096
    try:
        assert not writer.has_values('0')
        writer.add('0', 1, value)
        assert writer.has_values('0')
        writer.flush('0')
        assert writer.has_values('0')
        assert writer.pending_positions['0'] == (1, 1)
        writer._initial_batch(0, {'0': '0' * 4096})
        writer.add_batch('0', array.array('q', [1, 2]), ['0' * 4096, value])
        assert writer._position('0') == (3, 2)
        writer.publish(True)
        writer.positions.clear()
        assert writer.has_values('0')
        assert trace.signals['0'].changes == [(1, value), (1, '0' * 4096), (2, value)]
    finally:
        trace.close()


def test_mixed_ingestion_keeps_positions_through_eviction_and_sorting(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 2)
    trace, writer, expected = writer_for(tmp_path, count=3)
    writer.position_limit = 1
    try:
        writer.add('0', 10, '1')
        writer.add_vcd_batch({'0': [(0, '0')]})  # active builder
        writer.add_vcd_batch({'0': [(-1, '1')]})  # queued page
        writer.publish(True)
        writer.add('1', 0, '1')
        writer.publish(True)  # evicts 0 from the read-through position cache
        writer.add_batch('0', array.array('q', [-2, -2]), ['0', '1'])
        assert writer.disordered
        writer.reorder()
        assert trace.signals['0'].changes == [(-2, '0' * 16), (-2, '0' * 15 + '1'),
                                               (-1, '0' * 15 + '1'), (0, '0' * 16), (10, '0' * 15 + '1')]
        assert writer.store.total_changes == 6
    finally:
        trace.close()


@pytest.mark.parametrize('state', ['builder', 'queued', 'stored'])
@pytest.mark.parametrize('method', ['single', 'vcd', 'fst'])
def test_backwards_timestamp_detection_before_and_after_page_writes(tmp_path, state, method):
    trace, writer, expected = writer_for(tmp_path, count=1)
    try:
        writer.add('0', 10, '1')
        if state in ('queued', 'stored'):
            writer.flush('0')
        if state == 'stored':
            writer.publish(True)
            writer.positions.clear()
        assert not writer.disordered
        if method == 'single':
            writer.add('0', 5, '0')
        elif method == 'vcd':
            writer.add_vcd_batch({'0': [(5, '0')]})
        else:
            writer.add_batch('0', array.array('q', [5]), ['0'])
        assert writer.disordered
        writer.reorder()
        assert trace.signals['0'].changes == [(5, '0' * 16), (10, '0' * 15 + '1')]
    finally:
        trace.close()
