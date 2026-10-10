"""Publication latency and bulk page writes preserve the readable event prefix."""
import array
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from alwavelets import storage
from alwavelets.core import VCDSignal
from alwavelets.parsers import VCDFile
from tests.test_ingestion import writer_for
from tools.stress_vcd import create_vcd


def test_first_publication_is_prompt_and_later_partial_pages_can_accumulate(tmp_path, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(storage, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    trace, writer = writer_for(tmp_path)
    expected = []
    try:
        writer.publish(True)  # FST can publish an empty initial frame boundary.

        def append(start, count):
            for i in range(start, start + count):
                value = str(i % 2)
                writer.add('!', i // 2, value)
                expected.append((i // 2, value))

        append(0, 32)
        clock.now = 0.25
        writer.publish()
        assert trace.signals['!'].changes == expected
        assert not writer.builders
        append(32, 32)
        clock.now = 0.50
        writer.publish()
        assert trace.signals['!'].changes == expected[:32]
        assert writer.builders['!'].num_changes() == 32
        # A completed page from another input batch is still published on the
        # ordinary quarter-second cadence while the partial page is retained.
        clock.now = 0.75
        writer.flush('!')
        assert trace.signals['!'].changes == expected[:32]
        writer.publish()
        assert trace.signals['!'].changes == expected
        append(64, 32)
        clock.now = 1.0
        writer.publish()
        assert trace.signals['!'].changes == expected[:64]
        clock.now = 1.25
        writer.publish()
        assert trace.signals['!'].changes == expected
        assert not writer.builders and not writer.pending_pages
        append(96, 1)
        writer.publish(True)
        assert trace.signals['!'].changes == expected
        assert writer.store.total_changes == len(expected)
    finally:
        trace.close()


def test_queued_pages_keep_delta_order_inline_prefix_and_spilled_summary_levels(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'PAGE_EVENTS', 2)
    monkeypatch.setattr(storage, 'WRITE_BATCH_PAGES', 64)
    trace = VCDFile()
    expected = {}
    for i, (size, kind) in enumerate([(1, 'wire'), (16, 'wire'), (64, 'real'), (128, 'wire')]):
        key = str(i)
        expected[key] = trace.signals[key] = VCDSignal(key, key, size, kind)
        trace.signal_order.append(key)
    store = storage.TraceStore(str(tmp_path / 'pages.sqlite'), 65536)
    writer = storage.PageWriter(store, trace)
    writer.summary_group_limit = 1
    writer.position_limit = 2
    try:
        for event in range(610):
            timestamp = event // 3
            for key, signal in expected.items():
                value = str(event % 2) if key == '0' else (str(event / 4 - 100) if key == '2' else
                                                         format(event % 31, '0%db' % signal.size))
                signal.add_change(timestamp, value)
                writer.add(key, timestamp, value)
                assert len(writer.pending_pages) < storage.WRITE_BATCH_PAGES
                assert writer.pending_bytes < storage.WRITE_BATCH_BYTES
                assert len(writer.offsets) == len(writer.builders)
            if event == 10:
                writer.publish(True)
                for key, signal in expected.items():
                    assert trace.signals[key].changes == signal.changes
        writer.publish(True)
        assert store.db.execute('SELECT MAX(level) FROM summaries').fetchone()[0] >= 2
        for key, signal in expected.items():
            actual = trace.signals[key]
            assert actual.changes == signal.changes
            for start, stop in ((0, 200), (4, 29), (33, 33), (201, 1000)):
                assert actual.numeric_range_in_window(start, stop) == signal.numeric_range_in_window(start, stop)
                assert actual.value_at(start) == signal.value_at(start)
        assert store.total_changes == 610 * 4
        assert not writer.pending_pages and not writer.offsets
    finally:
        trace.close()


def test_fst_batches_reserve_ordinals_until_queued_pages_are_published(tmp_path):
    trace, writer = writer_for(tmp_path)
    try:
        writer.add_batch('!', array.array('q', [0, 0]), bytearray(b'01'))
        writer.add_batch('!', array.array('q', [0, 1]), bytearray(b'xz'))
        assert trace.signals['!'].num_changes() == 0
        writer.publish(True)
        assert trace.signals['!'].changes == [(0, '0'), (0, '1'), (0, 'x'), (1, 'z')]
        assert trace.signals['!'].value_at(0) == 'x'
    finally:
        trace.close()


@pytest.mark.parametrize('cancel', [False, True])
def test_background_queue_cancellation_and_complete_index_reopen(tmp_path, monkeypatch, cancel):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'trace.vcd'
    create_vcd(path, 128, 16000, 4, True)
    entered, release = threading.Event(), threading.Event()
    original = storage.PageWriter._drain_pages

    def pause(self):
        if self.pending_pages and not entered.is_set():
            entered.set()
            assert release.wait(5)
        original(self)

    monkeypatch.setattr(storage.PageWriter, '_drain_pages', pause)
    trace = storage.open_trace(path, background=True, memory_mib=0.1)
    try:
        assert entered.wait(5)
        assert trace.is_loading and not trace._load_error
        # Reserved writer ordinals must not expose pages still in the queue.
        assert trace.signals['s1'].num_changes() == 0
        if cancel:
            trace._store.cancel.set()
        release.set()
        trace._store.thread.join(10)
        assert not trace.is_loading
        if cancel:
            assert 'cancelled' in trace._load_error.lower()
            assert not Path(storage.index_path(path)).exists()
        else:
            trace.require_loaded()
            expected = trace.signals['s1'].changes
            with storage.open_trace(path) as reopened:
                assert reopened.signals['s1'].changes == expected
                assert reopened._store.total_changes == trace._store.total_changes
    finally:
        release.set()
        trace.close()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))
