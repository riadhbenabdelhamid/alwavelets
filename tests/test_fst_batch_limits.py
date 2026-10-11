"""Packed page capacity and safe checkpoint boundaries at text fallback."""
import array
import sys
import threading
import zlib

import pytest

from alwavelets import storage
from alwavelets.fst import FSTReader
from alwavelets.fst_stream import ChainStream, Records
from alwavelets.fst_values import LogicBatch
from alwavelets.streaming import OperationCancelled
from test_fst_batches import write_trace
from test_fst_resume import assert_matches, interrupt
from test_fst_values import binary_bytes, bus_events
from tools.stress_fst import varint


def reader_for(width):
    reader = FSTReader('unused')
    reader._cancel, reader._geom, reader._checkpoint = None, [], object()
    reader._chain_state = [0, 0]
    reader._handles = [None, {'size': width, 'real': False}]
    return reader


def assert_bounded(timestamps, values):
    assert 0 < len(values) == len(timestamps) <= storage.PAGE_EVENTS
    # Count the logical packed payload, or text objects and list slots, plus
    # timestamps. Containers/allocation slack add a bounded amount on top.
    size = len(timestamps) * timestamps.itemsize
    size += (len(values.data) if values.strings is None else
             sum(sys.getsizeof(value) + 8 for value in values.strings))
    assert size <= storage.PAGE_BYTES or len(values) == 1


@pytest.mark.parametrize('width', [2, 7, 65, 257, 1024, 20000, 300001])
def test_packed_pages_fill_by_storage_size_and_preserve_four_states(width):
    count = min(storage.PAGE_EVENTS, max(1, storage.PAGE_BYTES // ((width + 3) // 4 + 16)))
    total = count * 2 + 1
    raw, expected = bytearray(), []
    for i in range(total):
        delta = int(i != 0)
        if i % 17 == 0:
            value = ('x' if i % 34 == 0 else 'z') * width
            raw.extend(varint((delta << 1) | 1) + value.encode())
        else:
            number = i % (1 << width)
            value = format(number, f'0{width}b')
            raw.extend(varint(delta << 1) + binary_bytes(number, width))
        expected.append((i, value))
    reader, sizes, actual = reader_for(width), [], []
    with Records(limit=0) as times, ChainStream(zlib.compress(raw), 'zlib', len(raw)) as chain:
        times.extend(array.array('Q', range(total)))
        for timestamps, values in reader._chain_batches(1, chain, times):
            assert_bounded(timestamps, values)
            assert values.strings is None
            sizes.append(len(values))
            actual.extend(zip(timestamps, values))
        chain.finish()
    assert actual == expected
    assert sizes == [count, count, 1]
    if width == 1024:
        assert count >= 4 * (storage.PAGE_BYTES // (width + 64))


@pytest.mark.parametrize('width', [7, 257, 1024, 20000, 300001])
@pytest.mark.parametrize('rare', [b'h', b'\xff'])
def test_text_fallback_is_bounded_and_can_resume_at_every_batch(width, rare):
    capacity = max(1, storage.PAGE_BYTES // ((width + 3) // 4 + 16))
    prefix = max(1, min(239, capacity - 1))
    text_capacity = max(1, storage.PAGE_BYTES // (2 * width + 96))
    raw, expected, offsets, indices = bytearray(), [], [0], []
    index = 0
    for i in range(prefix + text_capacity * 2 + 3):
        # A multi-byte delta precisely at the representation boundary, then
        # same-tick events, exposes lookahead being saved too early.
        delta = 17000 if i == prefix else int(i % 3 == 0)
        index += delta
        if i >= prefix and i % 2 == prefix % 2:
            value = rare * width
            raw.extend(varint((delta << 1) | 1) + value)
            value = value.decode('ascii', 'replace')
        else:
            number = i % (1 << width)
            raw.extend(varint(delta << 1) + binary_bytes(number, width))
            value = format(number, f'0{width}b')
        expected.append((index * 11, value))
        offsets.append(len(raw))
        indices.append(index)
    packed = zlib.compress(raw)
    reader, actual, first = reader_for(width), [], True
    with Records(limit=0) as times:
        times.extend(array.array('Q', (i * 11 for i in range(index + 1))))
        uninterrupted = reader_for(width)
        uninterrupted._checkpoint = None
        with ChainStream(packed, 'zlib', len(raw)) as chain:
            decoded = []
            for timestamps, values in uninterrupted._chain_batches(1, chain, times):
                assert_bounded(timestamps, values)
                decoded.extend(zip(timestamps, values))
            assert decoded == expected
            chain.finish()
        while len(actual) < len(expected):
            # Reopen the forward-only chain at every saved boundary, including
            # one before an already-prefetched rare event; no rewind is needed.
            with ChainStream(packed, 'zlib', len(raw)) as chain:
                batches = reader._chain_batches(1, chain, times)
                timestamps, values = next(batches)
                assert_bounded(timestamps, values)
                if first:
                    assert values.strings is None and len(values) == prefix
                    first = False
                actual.extend(zip(timestamps, values))
                assert reader._chain_state == [offsets[len(actual)], indices[len(actual) - 1]]
                batches.close()
    assert actual == expected


def test_disallowed_fallback_does_not_expand_or_change_extrema():
    batch = LogicBatch(1024)
    batch.append_binary(binary_bytes(37, 1024))
    original = bytes(batch.data)
    assert not batch.append_text(b'h' * 1024, allow_fallback=False)
    assert batch.strings is None and batch.data == original
    assert (batch.minimum, batch.maximum) == (37, 37)


def test_cancel_after_packed_prefix_does_not_commit_prefetched_text():
    reader = reader_for(1024)
    reader._cancel = threading.Event()
    raw = b'\0' + binary_bytes(37, 1024) + b'\1' + b'h' * 1024
    with Records(limit=0) as times, ChainStream(raw, 'raw', len(raw)) as chain:
        times.append(0)
        batches = reader._chain_batches(1, chain, times)
        timestamps, values = next(batches)
        assert list(zip(timestamps, values)) == [(0, format(37, '01024b'))]
        assert reader._chain_state == [129, 0]
        reader._cancel.set()
        with pytest.raises(OperationCancelled):
            next(batches)
        assert reader._chain_state == [129, 0]
        assert batches.gi_frame is None


@pytest.mark.parametrize('rare_index', [1, 59, 239, 479])
def test_saved_prefix_before_rare_value_reopens_without_skipping_it(tmp_path, monkeypatch, rare_index):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'fallback.fst'
    events = bus_events(1024)
    events[rare_index] = (events[rare_index][0], 'h' * 1024)
    write_trace(source, 1024, events)
    saved = interrupt(source, monkeypatch, lambda state, writer:
                      state['phase'] == 'values' and writer.changes == rare_index + 1)
    assert saved['changes'] == rare_index + 1  # Includes the initial frame.
    with FSTReader(source).parse() as eager:
        for _ in range(2):
            with storage.open_trace(source, memory_mib=0.1) as trace:
                assert_matches(trace, eager)
                assert trace.signals['1']._values[rare_index + 1] == 'h' * 1024
                assert trace._store.peak_cache_bytes <= trace._store.memory_bytes
