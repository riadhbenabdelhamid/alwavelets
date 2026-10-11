"""Shared viewport reads preserve value, clipping and publication semantics."""
import array
from fractions import Fraction
import threading
from types import SimpleNamespace

import pytest

from alwavelets import rendering, storage
from alwavelets.catalog import CatalogAlias
from alwavelets.core import Radix, TimeScaledSignal, VirtualBitSignal
from alwavelets.streaming import OperationCancelled, read_cancellation
from alwavelets.ui_drawing import DrawingMixin
from test_writer_metadata import writer_for


def clear_pages(store):
    store.pages.clear()
    store.page_hints.clear()
    store.cache_bytes = 0


@pytest.mark.parametrize('offset', [0, (1 << 53) + 3])
def test_batched_values_preserve_alias_bit_and_scaled_time_semantics(tmp_path, offset):
    trace, writer, expected = writer_for(tmp_path, count=1)
    eager = expected['0']
    try:
        times = [offset + i // 3 for i in range(90)]
        values = [format(i, '016b') if i % 5 else 'xz01'[i % 4] * 16 for i in range(90)]
        for start in range(0, 90, 9):
            writer.add_batch('0', array.array('q', times[start:start + 9]), values[start:start + 9])
        writer.publish(True)
        for t, value in zip(times, values):
            eager.add_change(t, value)
        signal = trace.signals['0']
        pairs = [(signal, eager), (CatalogAlias('alias', '0', 16, 'wire', '', signal), eager),
                 (VirtualBitSignal(signal, 3), VirtualBitSignal(eager, 3))]
        pairs.extend((TimeScaledSignal(disk, Fraction(2, 3)), TimeScaledSignal(memory, Fraction(2, 3)))
                     for disk, memory in pairs[:2])
        queries = [offset - 1, offset, offset + 5, offset + Fraction(11, 2), offset + 29, offset + 50]
        for disk, memory in pairs:
            for ticks in (queries, queries[::-1], queries * 2):
                expected_values = [memory.value_at(t) for t in ticks]
                assert disk.values_at(iter(ticks)) == expected_values
                assert memory.values_at(ticks) == expected_values
        # An event exactly at t belongs to values_at, but not a left boundary.
        assert signal.values_at([offset + 1]) == [values[5]]
        assert signal._times.sample_boundaries([offset + 1]) == [(3, values[2])]
        with pytest.raises(OperationCancelled), read_cancellation(cancelled()):
            signal.values_at(queries)  # Cache hits still observe cancellation.
    finally:
        trace.close()


def cancelled():
    event = threading.Event()
    event.set()
    return event


@pytest.mark.parametrize('subcell', [False, True])
def test_related_rows_reuse_raw_samples_after_decoded_pages_are_evicted(tmp_path, monkeypatch, subcell):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        for start in range(0, 1024, 16):
            writer.add_batch('0', array.array('q', range(start, start + 16)),
                             [format(i * 101, '01024b') for i in range(start, start + 16)])
        writer.publish(True)
        signal, store = trace.signals['0'], trace._store
        rendering.draw_bus_culled(signal, 0, 1023, 64, Radix.HEX)
        alias = CatalogAlias('alias', '0', 1024, 'wire', '', signal)
        draw = rendering.draw_single_bit_subcell if subcell else rendering.draw_single_bit_culled
        draw(VirtualBitSignal(alias, 0), 0, 1023, 64)
        clear_pages(store)
        reads, hits = store.page_reads, store.sample_cache_hits
        frame = draw(VirtualBitSignal(signal, 1), 0, 1023, 64)
        assert frame and store.sample_cache_hits - hits == (129 if subcell else 65)
        # Only the window endpoints/initial value may reread pages.
        assert store.page_reads - reads <= 3
        assert 0 < store.sample_cache_bytes <= min(storage.SAMPLE_CACHE_BYTES, store.memory_bytes // 8)
    finally:
        trace.close()


def test_point_batch_keeps_an_uncacheable_page_active_and_preserves_delta_order(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        times = [i // 2 for i in range(32)]
        values = [format(i, '01024b') for i in range(32)]
        writer.add_batch('0', array.array('q', times), values)
        writer.publish(True)
        signal, store = trace.signals['0'], trace._store
        store.memory_bytes = 1  # Neither page nor raw samples fit.
        queries = [0, 0, Fraction(1, 2), 1, 2, 3, 15, 16, 100]
        assert signal.values_at(queries) == [values[1], values[1], values[1], values[3], values[5],
                                            values[7], values[31], values[31], values[31]]
        assert store.page_reads == 1
        assert not store.pages and not store.samples
    finally:
        trace.close()


@pytest.mark.parametrize('width', [16, 65536])
def test_sample_cache_is_byte_bounded_and_does_not_retain_oversized_values(tmp_path, width):
    trace, writer, _ = writer_for(tmp_path, count=1, width=width)
    try:
        for start in range(0, 100, 10):
            writer.add_batch('0', array.array('q', range(start, start + 10)), ['1' * width] * 10)
        writer.publish(True)
        store = trace._store
        store.memory_bytes = 64 * 1024
        for start in range(10):
            samples = trace.signals['0']._times.sample_boundaries([i + start / 10 for i in range(100)], transform=len)
            assert all(value in (None, width) for _, value in samples)
            assert store.sample_cache_bytes <= store.memory_bytes // 8
            assert store.sample_cache_bytes == sum(entry[2] for entry in store.samples.values())
        if width > store.memory_bytes // 8:
            assert all(value is None for _, value, _ in store.samples.values())
        else:
            assert 0 < len(store.samples) < 100
    finally:
        trace.close()
    assert not store.samples and store.sample_cache_bytes == 0


def test_sample_cache_tracks_publication_reordering_and_store_identity(tmp_path):
    traces = []
    try:
        for number in range(2):
            directory = tmp_path / str(number)
            directory.mkdir()
            trace, writer, _ = writer_for(directory, count=1)
            traces.append(trace)
            writer.add_batch('0', array.array('q', [0, 5]), [str(number) * 16, 'x' * 16])
            writer.publish(True)
            signal = trace.signals['0']
            assert signal.values_at([0, 5, 9]) == [str(number) * 16, 'x' * 16, 'x' * 16]
            writer.add('0', 5, 'z' * 16)
            writer.flush('0')
            writer._drain_pages()  # Unpublished changes cannot leak through samples.
            assert signal.values_at([5, 9]) == ['x' * 16] * 2
            writer.publish(True)
            assert signal.values_at([5, 9]) == ['z' * 16] * 2
            writer.add('0', -1, '1' * 16)
            writer.reorder()
            assert signal.values_at([-1, 0, 5]) == ['1' * 16, str(number) * 16, 'z' * 16]
        for number, trace in enumerate(traces):
            assert trace.signals['0'].values_at([0]) == [str(number) * 16]
    finally:
        for trace in traces:
            trace.close()


@pytest.mark.parametrize('offset', [0, (1 << 53) + 3])
def test_bus_label_batch_preserves_exact_pixel_time_lookups(tmp_path, monkeypatch, offset):
    trace, writer, _ = writer_for(tmp_path, count=1)
    viewer = SimpleNamespace(time_start=offset, time_end=offset + 43, wave_width=13, line_thickness=1,
                             _row_cache_window=None, _row_cache={}, vcd=trace)
    calls = []
    original = storage.StoredSignal.values_at
    def batch(signal, timestamps):
        timestamps = list(timestamps)
        calls.append(timestamps)
        return original(signal, timestamps)
    try:
        writer.add_batch('0', array.array('q', (offset + i // 3 for i in range(150))),
                         [format(i, '016b') for i in range(150)])
        writer.publish(True)
        signal = trace.signals['0']
        expected = rendering.draw_bus_culled(signal, offset, offset + 43, 13, Radix.HEX)
        labels = {col: signal.value_at(int(offset + col / 13 * 43)) for col in expected[2]}
        monkeypatch.setattr(storage.StoredSignal, 'values_at', batch)
        DrawingMixin._render_row(viewer, 'bus', signal, '0', Radix.HEX)
        for future in list(viewer._queries.pending.values()):
            future.result(timeout=5)
        viewer._queries.poll()
        assert DrawingMixin._render_row(viewer, 'bus', signal, '0', Radix.HEX) == expected
        assert viewer._row_extras['bus', '0'] == (labels, None)
        assert len(calls) == 1 and len(calls[0]) == len(labels)
    finally:
        if hasattr(viewer, '_queries'):
            viewer._queries.close()
        trace.close()


@pytest.mark.parametrize('kind,extra,draw', [('analog', None, rendering.draw_analog_culled),
                                         ('abraille', 3, rendering.draw_analog_braille),
                                         ('amulti', 3, rendering.draw_analog_multi)])
def test_viewer_reuses_one_analog_range_for_waveform_and_limits(tmp_path, monkeypatch, kind, extra, draw):
    trace, writer, _ = writer_for(tmp_path, count=1)
    viewer = SimpleNamespace(time_start=0, time_end=100, wave_width=32, line_thickness=1,
                             _row_cache_window=None, _row_cache={}, vcd=trace)
    calls = []
    original = storage.TraceStore.numeric_range
    def count(store, *args):
        calls.append(args)
        return original(store, *args)
    try:
        writer.add_batch('0', array.array('q', range(100)), [format(i, '016b') for i in range(100)])
        writer.publish(True)
        signal = trace.signals['0']
        expected = draw(signal, 0, 100, 32, *(() if extra is None else (extra,)))
        monkeypatch.setattr(storage.TraceStore, 'numeric_range', count)
        DrawingMixin._render_row(viewer, kind, signal, '0', extra)
        for future in list(viewer._queries.pending.values()):
            future.result(timeout=5)
        viewer._queries.poll()
        assert DrawingMixin._render_row(viewer, kind, signal, '0', extra) == expected
        assert viewer._row_extras[kind, '0'][1] == (0, 99)
        assert len(calls) == 1
    finally:
        if hasattr(viewer, '_queries'):
            viewer._queries.close()
        trace.close()
