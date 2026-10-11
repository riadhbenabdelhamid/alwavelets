"""Incremental timestamp expansion must preserve progressive and resumed reads."""
import array
from itertools import accumulate
from pathlib import Path
import struct
import threading
import tracemalloc
import zlib

import pytest

from alwavelets import storage
from alwavelets.fst import FSTReader
from alwavelets.fst_stream import Buffer, CHUNK, TimeTable
from alwavelets.streaming import OperationCancelled
from test_fst_resume import interrupt
from tools.stress_fst import create_fst, varint

PAGE = CHUNK // 8
U64 = struct.Struct('>Q')


@pytest.mark.parametrize('spill', [False, True])
def test_time_pages_expand_once_and_support_sparse_backward_reads(spill):
    deltas = [0, 1, 127, 128, 65537, 0] * (PAGE // 2)
    expected = array.array('Q', accumulate(deltas))
    raw = b''.join(varint(delta) for delta in deltas)
    with Buffer(limit=0 if spill else len(raw)) as data:
        data.write(raw)
        with TimeTable(data, len(deltas), limit=0 if spill else len(deltas) * 8) as times:
            assert len(times) == len(deltas) and len(times.records) == times.cursor.pos == 0
            assert times.uint64_page(0) == expected[:PAGE]
            assert len(times.records) == PAGE
            # Skip ahead, then revisit a different signal's earlier timestamps.
            for start in (2 * PAGE + 17, PAGE - 1, 5, len(deltas) - 3):
                assert times.uint64_page(start) == expected[start:start + PAGE]
            position = times.cursor.pos
            assert list(times) == list(expected)
            assert times[-1] == expected[-1]
            times.finish()
            assert times.cursor.pos == position == len(raw)
            assert len(times.records) == len(deltas)
            assert (times.records.buffer.file is not None) == spill
            for start in (-1, len(times)):
                with pytest.raises(IndexError):
                    times.uint64_page(start)
            with pytest.raises(IndexError):
                _ = times[-len(times) - 1]
        if spill:
            assert times.records.buffer.file.closed and not data.file.closed


def test_delta_varints_cross_input_pages_and_preserve_uint64_limits():
    deltas = [0] * (CHUNK - 1) + [128, (1 << 64) - 129]
    raw = b''.join(varint(delta) for delta in deltas)
    with TimeTable(raw, len(deltas), limit=0) as times:
        assert times[0] == 0 and len(times.records) == PAGE
        assert times[-2] == 128 and times[-1] == (1 << 64) - 1
        assert times.cursor.pos == len(raw)


@pytest.mark.parametrize('tail,error', [(b'\x80', ValueError), (b'\xff' * 11, ValueError),
                                        (varint((1 << 64) - 1), OverflowError)])
def test_unused_timestamp_tail_is_still_validated(tail, error):
    raw = b'\x01' * PAGE + tail
    with TimeTable(raw, PAGE + 1, limit=0) as times:
        assert times[0] == 1 and len(times.records) == PAGE
        with pytest.raises(error):
            times.finish()
    assert times.records.buffer.file.closed


def test_empty_and_impossible_timestamp_counts():
    with TimeTable(b'', 0) as times:
        assert len(times) == 0
        times.finish()
        with pytest.raises(IndexError):
            _ = times[0]
    for count in (-1, 2):
        with pytest.raises(ValueError, match='timestamp count'):
            TimeTable(b'\0', count)


@pytest.mark.parametrize('finish', [False, True])
def test_cancel_large_time_jump_or_finish_closes_spilled_records(monkeypatch, finish):
    cancel = threading.Event()
    with TimeTable(b'\x01' * (PAGE * 10), PAGE * 10, limit=0, cancel=cancel) as times:
        original = times.records.buffer.write

        def write(data):
            original(data)
            cancel.set()

        monkeypatch.setattr(times.records.buffer, 'write', write)
        with pytest.raises(OperationCancelled):
            if finish:
                times.finish()
            else:
                times.uint64_page(PAGE * 9)
        assert len(times.records) == PAGE and times.cursor.pos == PAGE
    assert times.records.buffer.file.closed


def test_incremental_expansion_memory_is_bounded():
    raw = b'\x01' * 1000000
    tracemalloc.start()
    try:
        with TimeTable(raw, len(raw), limit=CHUNK) as times:
            assert times[0] == 1
            assert times[-1] == len(raw)
            assert times[0] == 1
            assert times.records.buffer.file is not None
            assert tracemalloc.get_traced_memory()[1] < 1024 * 1024
    finally:
        tracemalloc.stop()


def replace_time_deltas(source, raw, count):
    """Replace the stress fixture's first block time table, keeping its chains."""
    trace = source.read_bytes()
    block = 1 + U64.unpack_from(trace, 1)[0]
    end = block + 1 + U64.unpack_from(trace, block + 1)[0]
    start = end - 24 - U64.unpack_from(trace, end - 16)[0]
    packed = zlib.compress(raw)
    if len(packed) == len(raw):
        packed = raw
    prefix = bytearray(trace[:start])
    block_end = start + len(packed) + 24
    prefix[block + 1:block + 9] = U64.pack(block_end - block - 1)
    source.write_bytes(prefix + packed + struct.pack('>QQQ', len(raw), len(packed), count) + trace[end:])


@pytest.mark.parametrize('disk', [False, True])
def test_malformed_unused_tail_never_completes_a_trace(tmp_path, monkeypatch, disk):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(storage, 'CHECKPOINT_BYTES', 0)
    source = tmp_path / 'bad-tail.fst'
    create_fst(source, events=2)
    replace_time_deltas(source, b'\0' + b'\1' * (PAGE * 3 - 2) + b'\x80', PAGE * 3)
    for _ in range(2):  # A saved prefix must still validate the tail on resume.
        with pytest.raises(ValueError, match='Truncated'):
            if disk:
                storage.open_trace(source, memory_mib=0.1)
            else:
                FSTReader(source).parse(memory_mib=0.1)
        assert not Path(storage.index_path(source)).exists()


def test_first_waveform_is_readable_before_time_expansion_finishes(tmp_path, monkeypatch):
    source = tmp_path / 'deep.fst'
    create_fst(source, events=200000)
    entered, release = threading.Event(), threading.Event()
    tables = []
    decode, publish = FSTReader._decode_chain, storage.PageWriter.publish

    def track(self, handle, chain, times, begin):
        tables.append(times)
        assert len(times.records) == 0
        return decode(self, handle, chain, times, begin)

    def pause(self, force=False):
        publish(self, True)
        if self.changes and not entered.is_set():
            entered.set()
            assert release.wait(5)

    monkeypatch.setattr(FSTReader, '_decode_chain', track)
    monkeypatch.setattr(storage.PageWriter, 'publish', pause)
    trace = storage.open_trace(source, background=True, use_cache=False, memory_mib=0.1)
    try:
        assert entered.wait(5) and trace.is_loading
        times = tables[0]
        assert len(times.records) == PAGE < len(times)
        signal = trace.signals['1']
        assert 0 < signal.num_changes() < len(times)
        assert signal.value_at(0) == '0' and signal.value_at(1) == '1'
        trace._store.cancel.set()
        release.set()
        trace._store.thread.join(5)
        assert not trace.is_loading and 'cancelled' in trace._load_error.lower()
        assert times.cursor.data.file.closed
        assert not times.records.buffer.data
    finally:
        release.set()
        trace.close()


def test_resume_reconstructs_only_the_time_prefix_needed_for_its_first_batch(tmp_path, monkeypatch):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'resume.fst'
    events = PAGE * 10
    create_fst(source, events=events)
    saved = interrupt(source, monkeypatch, lambda state, writer:
                      state['phase'] == 'values' and state['position'] > PAGE * 2)
    state = saved['checkpoint']['state']
    assert 0 < state['time_index'] < events // 2
    pages = []
    original = TimeTable.uint64_page

    def page(self, start):
        result = original(self, start)
        pages.append((start, len(self.records)))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(TimeTable, 'uint64_page', page)
        with storage.open_trace(source, memory_mib=0.1) as trace:
            assert trace._resume_offset == saved['checkpoint']['offset']
            assert trace.load_changes == events
            assert list(trace.signals['1'].iter_changes()) == [(i, str(i % 2)) for i in range(events)]
    first, expanded = pages[0]
    assert first == state['time_index'] + 1
    assert first < expanded <= first + PAGE * 2 < events
    assert not Path(storage.partial_index_path(source)).exists()
    with storage.open_trace(source) as cached:
        assert cached.signals['1'].num_changes() == events
        assert cached.signals['1'].value_at(events - 1) == '1'
