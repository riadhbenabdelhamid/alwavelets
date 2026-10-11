"""Viewport sampling preserves half-open boundaries with bounded page reads."""
import array
import bisect
from fractions import Fraction
import random

import pytest

from alwavelets import rendering, storage
from alwavelets.catalog import CatalogAlias
from alwavelets.core import Radix, TimeScaledSignal, VCDSignal, VirtualBitSignal
from alwavelets.fst_values import LogicBatch
from alwavelets.parsers import VCDFile
from test_writer_metadata import writer_for


@pytest.mark.parametrize('encoding,width', [('scalar', 1), ('text', 7), ('packed', 1025), ('real', 64)])
@pytest.mark.parametrize('inline', [False, True])
@pytest.mark.parametrize('right', [False, True])
def test_boundary_samples_match_bisection_and_preceding_values(tmp_path, monkeypatch, encoding, width, inline, right):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 8 if inline else 0)
    trace = VCDFile()
    trace.signals['0'] = VCDSignal('value', '0', width, 'real' if encoding == 'real' else 'wire')
    trace.signal_order = ['0']
    store = storage.TraceStore(tmp_path / 'samples.sqlite', 512)
    writer = storage.PageWriter(store, trace)
    times = [-(1 << 63), -7, 5, 5, 5, 5, 5, 5, 9, 30, 30, (1 << 63) - 1]
    values = [repr(i / 4) if encoding == 'real' else '01xz'[i % 4] * width for i in range(len(times))]
    try:
        for start in range(0, len(times), 3):
            batch = values[start:start + 3]
            if encoding == 'scalar':
                batch = bytearray(''.join(batch), 'ascii')
            elif encoding == 'packed':
                batch = LogicBatch(width)
                for value in values[start:start + 3]:
                    batch.append_text(value.encode())
            writer.add_batch('0', array.array('q', times[start:start + 3]), batch)
        writer.publish(True)
        signal = trace.signals['0']
        boundaries = sorted(times + [-10**30, 10**30, -8, -6, 4, 6, 10, 29, 31, Fraction(9, 2), Fraction(11, 2)])
        for begin, end in [(0, len(times)), (2, 9), (4, 6), (3, 3)]:
            view = signal._times[begin:end]
            for queries in (boundaries, boundaries[::-1]):
                want = []
                for time in queries:
                    index = (bisect.bisect_right if right else bisect.bisect_left)(times[begin:end], time)
                    want.append((index, values[begin + index - 1] if index else None))
                def sample(transform=None):
                    return (store.sample_boundaries('0', queries, begin, end, transform, right=True)
                            if right else view.sample_boundaries(queries, transform=transform))
                assert sample() == want
                assert sample(len) == [
                    (index, len(value) if value is not None else None) for index, value in want]
                assert store.cache_bytes <= store.memory_bytes
        # Nested contiguous views preserve relative ordinals.
        nested = storage.SequenceView(signal._times[2:10], range(1, 5))
        assert nested.sample_boundaries([5, 6, 100]) == [(0, None), (4, values[6]), (4, values[6])]
    finally:
        trace.close()


def test_grouped_samples_read_an_uncacheable_page_once_and_skip_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, _ = writer_for(tmp_path, count=1, width=1024)
    try:
        for start in (0, 100):
            writer.add_batch('0', array.array('q', range(start, start + 32)), ['1' * 1024] * 32)
        writer.publish(True)
        store, signal = trace._store, trace.signals['0']
        store.memory_bytes = 1
        queries = [i + 0.5 for i in range(32)] + [50, 99, 100]
        samples = signal._times.sample_boundaries(queries)
        assert samples == [(i + 1, '1' * 1024) for i in range(32)] + [(32, '1' * 1024)] * 3
        assert store.page_reads == 1 and store.timestamp_page_reads == 0
        assert not store.pages and not store.page_hints and not store.cache_bytes
    finally:
        trace.close()


@pytest.mark.parametrize('inline', [False, True])
def test_boundary_samples_respect_publication_reordering_and_cancellation(tmp_path, monkeypatch, inline):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 8 if inline else 0)
    trace, writer, _ = writer_for(tmp_path, count=1)
    try:
        store, signal = trace._store, trace.signals['0']
        sample = signal._times.sample_boundaries
        assert sample([0, 10]) == [(0, None), (0, None)]
        writer.add_batch('0', array.array('q', [1, 5]), ['0' * 16, '1' * 16])
        writer.publish(True)
        writer.add_batch('0', array.array('q', [5, 9]), ['x' * 16, 'z' * 16])
        writer._drain_pages()
        assert sample([5, 6, 10]) == [(1, '0' * 16), (2, '1' * 16), (2, '1' * 16)]
        writer.publish(True)
        assert sample([5, 6, 10]) == [(1, '0' * 16), (3, 'x' * 16), (4, 'z' * 16)]
        writer.add_batch('0', array.array('q', [0]), ['z' * 16])
        writer.reorder()
        assert sample([1, 5, 6]) == [(1, 'z' * 16), (2, '0' * 16), (4, 'x' * 16)]
        calls = []
        def cancel(value):
            calls.append(value)
            store.cancel.set()
            return value
        with pytest.raises(storage.Cancelled, match='Read cancelled'):
            sample([1, 5, 6], transform=cancel)
        assert len(calls) == 1
        store.cancel.clear()
        with pytest.raises(storage.Cancelled, match='Read cancelled'):
            sample([1], transform=cancel)  # Cancellation during the final sample also wins.
    finally:
        trace.close()


@pytest.mark.parametrize('width', [1, 7, 1025])
@pytest.mark.parametrize('offset', [0, (1 << 53) + 3])
def test_viewport_rendering_matches_eager_with_aliases_bits_and_scaled_times(tmp_path, monkeypatch, width, offset):
    monkeypatch.setattr(storage, 'SHORT_EVENTS', 0)
    trace, writer, expected = writer_for(tmp_path, count=1, width=width)
    eager = expected['0']
    rng = random.Random(71)
    times = [offset + (i // 3) * 5 for i in range(210)]
    values = [rng.choice('01xz') * width if i % 5 == 0 else format(rng.getrandbits(width), f'0{width}b')
              for i in range(len(times))]
    try:
        for begin in range(0, len(times), 11):
            batch = values[begin:begin + 11]
            writer.add_batch('0', array.array('q', times[begin:begin + 11]),
                             bytearray(''.join(batch), 'ascii') if width == 1 else batch)
        writer.publish(True)
        for time, value in zip(times, values):
            eager.add_change(time, value)
        signal = trace.signals['0']
        pairs = [(signal, eager), (CatalogAlias('alias', '0', width, 'wire', '', signal), eager)]
        if width > 1:
            pairs.extend((VirtualBitSignal(signal, bit), VirtualBitSignal(eager, bit)) for bit in (0, width - 1))
        pairs.extend((TimeScaledSignal(disk, Fraction(2, 3)), TimeScaledSignal(memory, Fraction(2, 3)))
                     for disk, memory in pairs[:2])
        for disk, memory in pairs:
            draws = [(rendering.draw_bus_culled, (Radix.HEX,))] if disk.size > 1 else [
                (rendering.draw_single_bit_culled, ()), (rendering.draw_single_bit_subcell, ())]
            ratio = Fraction(2, 3) if isinstance(disk, TimeScaledSignal) else 1
            for begin, end in [(-5, 360), (10, 350), (11, 16), (0, 5), (100, 100), (355, 500)]:
                start, stop = (offset + begin) * ratio, (offset + end) * ratio
                for columns in (1, 7, 32):
                    for draw, args in draws:
                        assert draw(disk, start, stop, columns, *args) == draw(memory, start, stop, columns, *args)
    finally:
        trace.close()


@pytest.mark.parametrize('draw,columns', [(rendering.draw_single_bit_culled, 40),
                                       (rendering.draw_single_bit_subcell, 80)])
def test_expanded_eager_bits_sample_only_screen_boundaries(monkeypatch, draw, columns):
    bus = VCDSignal('bus', '!', 8)
    for tick in range(10000):
        bus.add_change(tick, format(tick % 256, '08b'))
    bit = VirtualBitSignal(bus, 0)
    reads = []
    original = VirtualBitSignal.value_str
    def sample(self, index):
        reads.append(index)
        return original(self, index)
    def no_window(*args):
        raise AssertionError('Dense bit lanes must not extract the full visible history')
    monkeypatch.setattr(VirtualBitSignal, 'value_str', sample)
    monkeypatch.setattr(VirtualBitSignal, 'changes_in_window', no_window)
    chars, _ = draw(bit, 0, 9999, 40)
    assert set(chars) == {rendering.CH_BUSY}
    assert len(reads) <= columns + 1
