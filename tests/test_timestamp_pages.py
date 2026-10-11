"""Timestamp prefixes share the page budget and upgrade only for value reads."""
import array
import bisect
from fractions import Fraction
import zlib

import pytest

from alwavelets import storage
from alwavelets.core import VCDSignal
from alwavelets.fst_values import LogicBatch
from alwavelets.parsers import VCDFile
from test_writer_metadata import writer_for


@pytest.mark.parametrize('encoding,width', [('scalar', 1), ('packed', 1025), ('text', 1025), ('real', 64)])
def test_time_reads_skip_values_and_upgrade_the_same_cache_entry(tmp_path, monkeypatch, encoding, width):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace = VCDFile()
    trace.signals['0'] = VCDSignal('value', '0', width, 'real' if encoding == 'real' else 'wire')
    trace.signal_order = ['0']
    store = storage.TraceStore(tmp_path / 'times.sqlite')
    writer = storage.PageWriter(store, trace)
    times = [-(1 << 63), -10, 5, 5, 5, 17, 100, (1 << 63) - 1]
    text = ([repr(i / 8) for i in range(len(times))] if encoding == 'real' else
            ['01xz'[i % 4] * width for i in range(len(times))])
    values = text
    if encoding == 'scalar':
        values = bytearray(''.join(text), 'ascii')
    elif encoding == 'packed':
        values = LogicBatch(width)
        for value in text:
            values.append_text(value.encode())
    try:
        writer.add_batch('0', array.array('q', times), values)
        writer.publish(True)
        signal = trace.signals['0']
        def no_value_decode(*args):
            raise AssertionError('Timestamp lookup must not decode waveform values')
        with monkeypatch.context() as patch:
            patch.setattr(storage, '_decode_page', no_value_decode)
            for t in [-(10**30), -10, 5, Fraction(11, 2), 19, 10**30]:
                assert signal._times.bisect(t) == bisect.bisect_left(times, t)
                assert signal._times.bisect(t, True) == bisect.bisect_right(times, t)
            assert list(signal._times) == times
            assert signal._times[3] == 5
        pid, (partial, old_size) = next(iter(store.pages.items()))
        assert partial._values is None and store.timestamp_page_reads == 1
        assert signal.value_at(5) == text[4]
        full, size = store.pages[pid]
        assert full is not partial and full._values is not None
        assert partial._values is None  # Readers holding a prefix keep an immutable snapshot.
        assert list(full._times) == list(partial._times) == times
        assert len(store.pages) == 1 and store.cache_bytes == size > old_size
        reads = store.page_reads
        assert list(signal._times) == times and signal.value_str(6) == text[6]
        assert store.page_reads == reads  # A full page also satisfies time-only reads.
        assert list(signal.iter_changes(-(1 << 63))) == list(zip(times, text))
        store.pages.clear()
        store.cache_bytes = 0
        prefixes = store.timestamp_page_reads
        assert signal._times.bisect_values(Fraction(11, 2)) == 5
        assert signal.value_at(5) == text[4]
        assert store.page_reads == reads + 1 and store.timestamp_page_reads == prefixes
    finally:
        trace.close()


def test_prefix_input_is_chunked_and_truncated_or_corrupt_headers_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    times = [i * 123456789101112 for i in range(100)]
    try:
        writer.add_batch('0', array.array('q', times), ['0' * 1024] * len(times))
        writer.publish(True)
        store, signal = trace._store, trace.signals['0']
        monkeypatch.setattr(storage, 'PAGE_BYTES', 13)  # Force prefix reads across many input chunks.
        statements = []
        store.db.set_trace_callback(statements.append)
        assert list(signal._times) == times
        reads = [sql for sql in statements if sql.startswith('SELECT substr(data,')]
        assert len(reads) > 1 and all(',13)' in sql for sql in reads)
        assert not any(sql.startswith('SELECT data FROM') for sql in statements)
        store.db.set_trace_callback(None)
        for raw in (b'', b'\x02' + array.array('q', times[:-1]).tobytes(),
                    b'\x03' + array.array('q', times).tobytes()):
            store.pages.clear()
            store.cache_bytes = 0
            store.db.execute('UPDATE pages SET data=?', (zlib.compress(raw),))
            with pytest.raises(ValueError, match='Truncated waveform timestamps|Invalid waveform page kind'):
                signal._times[0]
            assert not store.pages and not store.cache_bytes
    finally:
        trace.close()


def test_full_upgrade_checks_checksum_after_readable_timestamp_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        writer.add_batch('0', array.array('q', range(100)), ['1' * 1024] * 100)
        writer.publish(True)
        store, signal = trace._store, trace.signals['0']
        original = store.db.execute('SELECT data FROM pages').fetchone()[0]
        damaged = original[:-1] + bytes([original[-1] ^ 255])
        store.db.execute('UPDATE pages SET data=?', (damaged,))
        assert signal._times.bisect(50) == 50
        with pytest.raises(zlib.error):
            signal.value_at(50)
        assert all(page._values is None for page, _ in store.pages.values())
        store.db.execute('UPDATE pages SET data=?', (original,))
        assert signal.value_at(50) == '1' * 1024
    finally:
        trace.close()


def test_prefix_reads_cancel_between_input_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        writer.add_batch('0', array.array('q', range(100)), ['0' * 1024] * 100)
        writer.publish(True)
        store, signal = trace._store, trace.signals['0']
        monkeypatch.setattr(storage, 'PAGE_BYTES', 8)
        original = zlib.decompressobj
        class CancelPrefix:
            def __init__(self):
                self.decoder = original()
            def decompress(self, raw, limit):
                value = self.decoder.decompress(raw, limit)
                store.cancel.set()
                return value
            @property
            def eof(self):
                return self.decoder.eof
        monkeypatch.setattr(zlib, 'decompressobj', CancelPrefix)
        with pytest.raises(storage.Cancelled, match='Read cancelled'):
            signal._times[0]
        assert not store.pages and store.page_reads == 0
    finally:
        trace.close()


def test_prefixes_and_upgrades_share_one_lru_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=4000)
    store, signal = trace._store, trace.signals['0']
    store.memory_bytes = 64 * 1024
    try:
        for start in range(0, 128, 32):
            batch = LogicBatch(4000)
            for _ in range(32):
                batch.append_text(b'1' * 4000)
            writer.add_batch('0', array.array('q', range(start, start + 32)), batch)
        writer.publish(True)
        assert list(signal._times) == list(range(128))
        assert len(store.pages) == 4 and all(page._values is None for page, _ in store.pages.values())
        for index in (0, 32, 64, 96, 0):
            assert signal.value_at(index) == '1' * 4000
            assert store.cache_bytes == sum(size for _, size in store.pages.values()) <= store.memory_bytes
        assert len(store.pages) < 4 and store.peak_cache_bytes <= store.memory_bytes
    finally:
        trace.close()


def test_oversized_values_leave_only_small_timestamp_pages_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    width = 2000001
    trace, writer, _ = writer_for(tmp_path, count=1, width=width)
    store, signal = trace._store, trace.signals['0']
    store.memory_bytes = 64 * 1024
    try:
        batch = LogicBatch(width)
        batch.append_text(b'z' * width)
        writer.add_batch('0', array.array('q', [11]), batch)
        writer.publish(True)
        assert signal._times[0] == 11 and store.cache_bytes < 1024
        assert signal.value_at(11) == 'z' * width
        assert not store.pages and store.cache_bytes == 0
        assert signal._times[0] == 11 and store.cache_bytes < 1024
    finally:
        trace.close()


def test_prefix_hints_follow_publication_and_reordering(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        store, signal = trace._store, trace.signals['0']
        writer.add_batch('0', array.array('q', [1, 5]), ['0' * 1024, '1' * 1024])
        writer.publish(True)
        assert signal._times.bisect(5) == 1
        writer.add_batch('0', array.array('q', [5, 9]), ['x' * 1024, 'z' * 1024])
        writer._drain_pages()
        assert signal._times.bisect(9, True) == 2
        writer.publish(True)
        assert signal._times.bisect(5, True) == 3
        assert signal.value_at(5) == 'x' * 1024
        store.pages.clear()
        store.cache_bytes = 0
        assert signal._times[0] == 1
        writer.add_batch('0', array.array('q', [0]), ['0' * 1024])
        writer.reorder()
        assert list(signal._times) == [0, 1, 5, 5, 9]
        assert signal.value_at(5) == 'x' * 1024
    finally:
        trace.close()
