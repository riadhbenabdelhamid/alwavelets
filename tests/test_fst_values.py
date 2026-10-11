"""Packed FST buses retain bit order, rare logic states and reader behavior."""
import array
import io
import json
import random
import sqlite3

import pytest

from alwavelets import storage
from alwavelets.core import Radix
from alwavelets.exporting import write_signal_csv
from alwavelets.fst import FSTReader
from alwavelets.fst_values import LogicBatch
from alwavelets.rendering import draw_bus_culled
from test_fst_batches import write_trace
from test_fst_resume import assert_matches, interrupt
from test_writer_metadata import writer_for


def binary_bytes(value, width):
    # Deliberately set unused low bits; these are not part of the signal.
    padding = (-width) % 8
    return ((value << padding) | ((1 << padding) - 1)).to_bytes((width + 7) // 8, 'big')


@pytest.mark.parametrize('width', [2, 3, 4, 5, 7, 8, 9, 15, 16, 31, 63, 64, 65, 127, 256, 257, 1024, 4097, 20000])
def test_binary_and_four_state_values_pack_without_losing_bit_order(width):
    rng = random.Random(width)
    numbers = [0, 1, 1 << (width - 1), (1 << width) - 1] + [rng.getrandbits(width) for _ in range(16)]
    batch, expected = LogicBatch(width), []
    for number in numbers:
        value = format(number, f'0{width}b')
        batch.append_binary(binary_bytes(number, width))
        batch.append_text(value.encode())
        expected.extend([value, value])
    for offset in range(4):
        value = ''.join('01xz'[(i + offset) % 4] for i in range(width))
        batch.append_text(value.encode())
        expected.append(value)
    assert list(batch) == expected
    assert batch.strings is None and len(batch.data) == len(expected) * ((width + 3) // 4)
    assert (batch.minimum, batch.maximum) == (0, (1 << width) - 1)


@pytest.mark.parametrize('width', [7, 65, 256, 1025])
def test_rare_states_and_uppercase_preserve_text_when_a_batch_falls_back(width):
    batch = LogicBatch(width)
    batch.append_binary(binary_bytes(17, width))
    batch.append_text(b'x' * width)
    expected = [format(17, f'0{width}b'), 'x' * width]
    for char in 'XZhluw-?':
        value = (char + '01' * width)[:width]
        batch.append_text(value.encode())
        expected.append(value)
    batch.append_binary(binary_bytes(31, width))
    expected.append(format(31, f'0{width}b'))
    assert batch.strings == list(batch) == expected and not batch.data
    # Preserve the existing numeric parser's treatment of a leading minus.
    assert (batch.minimum, batch.maximum) == (-int(('01' * width)[:width - 1], 2), 31)


def bus_events(width, unusual=False):
    rng = random.Random(width)
    events = []
    for i in range(11000):
        if unusual and i % 41 == 1 and i % 2:
            # The fixture writes odd-index values using FST's text encoding.
            value = 'XZhluw-?'[(i // 41) % 8] * width
        elif i % 17 == 0:
            value = 'xz'[i % 2] * width
        else:
            value = format(rng.getrandbits(width), f'0{width}b')
        events.append((3 + i // 3, value))
    return events


@pytest.mark.parametrize('width', [65, 256, 257, 1024])
@pytest.mark.parametrize('unusual', [False, True])
def test_wide_pages_resume_reopen_render_and_export_exactly(tmp_path, monkeypatch, width, unusual):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'bus.fst'
    write_trace(source, width, bus_events(width, unusual))
    with FSTReader(source).parse() as eager:
        saved = interrupt(source, monkeypatch, lambda state, writer:
                          state['phase'] == 'values' and state['block'] == 2 and state['position'] > 0)
        for cached in (False, True):
            with storage.open_trace(source, memory_mib=0.1) as trace:
                if not cached:
                    assert trace._resume_offset == saved['checkpoint']['offset']
                assert_matches(trace, eager)
                actual, expected = trace.signals['1'], eager.signals['1']
                assert list(trace.signals.values())[1].master is actual
                for start, end in ((0, 10), (600, 900), (1669, 1670), (3000, 3000)):
                    assert draw_bus_culled(actual, start, end, 61, Radix.HEX) == draw_bus_culled(expected, start, end, 61, Radix.HEX)
                    assert actual.numeric_range_in_window(start, end) == expected.numeric_range_in_window(start, end)
                outputs = [io.StringIO(), io.StringIO()]
                assert write_signal_csv(actual, outputs[0]) == write_signal_csv(expected, outputs[1])
                assert outputs[0].getvalue() == outputs[1].getvalue()
                if not unusual:
                    page, _ = trace._store.page_at('1', 100)
                    assert isinstance(page._values, bytearray)
                    assert page._stride == (width + 3) // 4
                assert trace._store.peak_cache_bytes <= trace._store.memory_bytes


@pytest.mark.parametrize('partial', [False, True])
def test_version7_text_pages_remain_readable_and_resumable(tmp_path, monkeypatch, partial):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'legacy.fst'
    write_trace(source, 1024, bus_events(1024))
    original = FSTReader._chain_batches

    def text_batches(self, *args):
        for timestamps, values in original(self, *args):
            yield timestamps, list(values)

    with monkeypatch.context() as patch:
        patch.setattr(storage, 'FST_VERSION', 7)
        patch.setattr(FSTReader, '_chain_batches', text_batches)
        if partial:
            saved = interrupt(source, patch, lambda state, writer:
                              state['phase'] == 'values' and state['block'] == 2 and state['position'] > 0)
            assert saved['version'] == 7
        else:
            with storage.open_trace(source):
                pass
    visits = []

    def track(self, *args):
        visits.append(1)
        yield from original(self, *args)

    monkeypatch.setattr(FSTReader, '_chain_batches', track)
    with storage.open_trace(source, memory_mib=0.1) as trace, FSTReader(source).parse() as eager:
        assert_matches(trace, eager)
        if partial:
            assert trace._resume_offset == saved['checkpoint']['offset'] and visits
        else:
            assert not visits
    with sqlite3.connect(storage.index_path(source)) as db:
        meta = json.loads(db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()[0])
        assert meta['version'] == (8 if partial else 7)


def test_single_very_wide_value_and_extrema_survive_packed_storage(tmp_path):
    width = 300001  # Larger than a page even at two bits per logic digit.
    trace, writer, _ = writer_for(tmp_path, count=1, width=width)
    number = (1 << (width - 1)) + 19
    batch = LogicBatch(width)
    batch.append_binary(binary_bytes(number, width))
    try:
        writer.add_batch('0', array.array('q', [0]), batch)
        writer.publish(True)
        signal = trace.signals['0']
        assert signal.value_at(0) == format(number, f'0{width}b')
        assert trace._store.numeric_range(signal, 0, 0) == (number, number)
        assert writer.pending_bytes == 0
    finally:
        trace.close()
