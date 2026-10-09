"""Exact batched FST ingestion and the lifetime of prepared input resources."""
import array
import gzip
from pathlib import Path
import struct
import threading
import zlib

import pytest

from alwavelets.core import VCDSignal
from alwavelets.fst import FSTReader
from alwavelets.fst_stream import Buffer, CHUNK, Cursor, Records
from alwavelets.storage import PAGE_BYTES, PAGE_EVENTS, PageWriter, index_path, open_trace
from tools.stress_fst import create_fst, varint

FIXTURES = Path(__file__).parent / 'fixtures'
U64 = struct.Struct('>Q')


def write_trace(path, width, events, real=False):
    """Two VC blocks, an alias, an initial frame, and repeated time deltas."""
    create_fst(path, events=2)
    template = path.read_bytes()
    header = bytearray(template[:1 + U64.unpack_from(template, 1)[0]])
    header[17:25] = U64.pack(events[-1][0])
    header[49:57], header[65:73] = U64.pack(2), U64.pack(2)

    def section(kind, body):
        return bytes([kind]) + U64.pack(len(body) + 8) + body

    def compressed(data):
        result = zlib.compress(data)
        return data if len(result) == len(data) else result

    def vc(changes, first):
        begin = 0 if first else changes[0][0]
        ticks = sorted({begin, *(t for t, _ in changes)})
        indices = {t: i for i, t in enumerate(ticks)}
        chain, previous = bytearray(), 0
        for i, (t, value) in enumerate(changes):
            delta = indices[t] - previous
            previous = indices[t]
            if real:
                chain.extend(varint(delta << 1) + struct.pack('<d', float(value)))
            elif width == 1:
                code = ((delta << 2) | (int(value) << 1)) if value in '01' else ((delta << 4) | ('xz'.index(value) << 1) | 1)
                chain.extend(varint(code))
            elif i % 2 or 'x' in value or 'z' in value:
                chain.extend(varint((delta << 1) | 1) + value.encode())
            else:
                nbytes = (width + 7) // 8
                chain.extend(varint(delta << 1) + (int(value, 2) << (nbytes * 8 - width)).to_bytes(nbytes, 'big'))
        frame = struct.pack('<d', -2.5) if real else b'0' * width
        packed_frame = compressed(frame)
        raw_times = b''.join(varint(t - (ticks[i - 1] if i else 0)) for i, t in enumerate(ticks))
        packed_times = compressed(raw_times)
        body = (U64.pack(begin) + U64.pack(ticks[-1]) + U64.pack(len(chain)) +
                varint(len(frame)) + varint(len(packed_frame)) + b'\x01' + packed_frame +
                b'\x01Z' + varint(len(chain)) + zlib.compress(chain) + b'\x03' + U64.pack(1) +
                packed_times + U64.pack(len(raw_times)) + U64.pack(len(packed_times)) + U64.pack(len(ticks)))
        return section(8, body)

    tag = bytes([3 if real else 16, 0])
    hierarchy = b'\xfe\x00top\0\0' + tag + b'value\0' + varint(width) + b'\0'
    hierarchy += tag + b'alias\0' + varint(width) + b'\x01\xff'
    path.write_bytes(header + vc(events[:5000], True) + vc(events[5000:], False) +
                     section(4, U64.pack(len(hierarchy)) + zlib.compress(hierarchy)))


@pytest.mark.parametrize('width,real', [(1, False), (7, False), (64, False), (257, False), (64, True)])
def test_batches_preserve_frames_aliases_deltas_and_extrema(tmp_path, monkeypatch, width, real):
    events = []
    for i in range(11000):
        value = str((i % 311) / 8) if real else ('xz'[i % 2] * width if i % 19 == 0 else format(i % (1 << width), f'0{width}b'))
        events.append((3 + i // 3, value))
    path = tmp_path / 'batches.fst'
    write_trace(path, width, events, real)
    expected = VCDSignal('value', '1', width, 'real' if real else 'wire', 'top')
    expected.add_change(0, '-2.5' if real else '0' * width)
    for t, value in events:
        expected.add_change(t, value)
    batches = []
    original = PageWriter.add_batch
    def track(self, key, timestamps, values):
        batches.append(len(timestamps))
        assert len(timestamps) <= PAGE_EVENTS
        assert len(timestamps) * (96 if real else width + 64) <= PAGE_BYTES or width == 1
        return original(self, key, timestamps, values)
    monkeypatch.setattr(PageWriter, 'add_batch', track)
    with open_trace(path, use_cache=False, memory_mib=0.1) as trace:
        assert len(trace.signals) == 2
        got, alias = trace.signals.values()
        assert got.changes == expected.changes == alias.changes
        assert alias.master is got
        for start, end in [(0, events[-1][0]), (1000, 3000), (1669, 1670), (3000, 3000)]:
            assert got.numeric_range_in_window(start, end) == expected.numeric_range_in_window(start, end)
        assert trace.load_changes == len(events) + 1
    assert len(batches) > 2


@pytest.mark.parametrize('variant', ['Z', 'c'])
@pytest.mark.parametrize('background', [False, True])
def test_metadata_and_wrapper_are_read_once(monkeypatch, variant, background):
    import alwavelets.parsers as parsers
    counts = {'hierarchy': 0, 'wrapper': 0, 'scopes': 0}
    for owner, method, key in [(FSTReader, '_parse_hierarchy', 'hierarchy'),
                               (FSTReader, '_unwrap', 'wrapper'),
                               (parsers, 'build_scope_tree', 'scopes')]:
        original = getattr(owner, method)
        def counted(*args, original=original, key=key, **kwargs):
            counts[key] += 1
            return original(*args, **kwargs)
        monkeypatch.setattr(owner, method, counted)
    with open_trace(FIXTURES / f'fst_ref_{variant}.fst', background=background, use_cache=False) as trace:
        if background:
            trace._store.thread.join(5)
        assert not trace.is_loading and not trace._load_error
        assert len(trace.signal_order) == 7
    assert counts == {'hierarchy': 1, 'wrapper': int(variant == 'c'), 'scopes': 1}


def test_background_batches_publish_before_completion_and_cancel_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'deep.fst'
    create_fst(path, events=100000)
    entered, release = threading.Event(), threading.Event()
    readers = []
    original_values, original_publish = FSTReader.read_values, PageWriter.publish
    def read(self, *args):
        readers.append(self)
        return original_values(self, *args)
    def publish(self, force=False):
        original_publish(self, True)
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
    monkeypatch.setattr(FSTReader, 'read_values', read)
    monkeypatch.setattr(PageWriter, 'publish', publish)
    trace = open_trace(path, background=True, memory_mib=0.1)
    try:
        assert entered.wait(5) and trace.is_loading
        signal = trace.signals['1']
        assert 0 < signal.num_changes() < 100000
        assert signal.value_at(1) == '1'
        assert readers[0]._file and not readers[0]._file.closed
        trace._store.cancel.set()
        release.set()
        trace._store.thread.join(5)
        assert not trace.is_loading and 'cancelled' in trace._load_error.lower()
        assert readers[0]._file is None and readers[0]._handles == []
    finally:
        release.set()
        trace.close()
    assert not Path(index_path(path)).exists()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))


@pytest.mark.parametrize('failure', ['writer', 'thread'])
def test_prepared_wrapper_closes_on_startup_failure(tmp_path, monkeypatch, failure):
    import alwavelets.fst_stream as stream
    import alwavelets.storage as storage
    path = tmp_path / 'wide.fst'
    create_fst(path, signals=30000)
    raw = path.read_bytes()
    wrapped = b'\xfe' + U64.pack(len(raw) + 16) + U64.pack(len(raw)) + gzip.compress(raw)
    path.write_bytes(wrapped)
    opened, readers, indexes, inputs = [], [], [], []
    temporary, prepare, initialize = stream.tempfile.TemporaryFile, FSTReader._prepare, storage.TraceStore.__init__
    def track_file(*args, **kwargs):
        result = temporary(*args, **kwargs)
        opened.append(result)
        return result
    def track_reader(self, *args):
        readers.append(self)
        result = prepare(self, *args)
        inputs.append(self._file)
        return result
    def track_index(self, *args):
        initialize(self, *args)
        indexes.append(self)
    def fail(*args):
        raise RuntimeError('injected startup failure')
    monkeypatch.setattr(stream.tempfile, 'TemporaryFile', track_file)
    monkeypatch.setattr(FSTReader, '_prepare', track_reader)
    monkeypatch.setattr(storage.TraceStore, '__init__', track_index)
    if failure == 'writer':
        monkeypatch.setattr(storage.PageWriter, '__init__', fail)
    else:
        monkeypatch.setattr(threading.Thread, 'start', fail)
    with pytest.raises(RuntimeError, match='injected startup failure'):
        open_trace(path, background=True, use_cache=False, memory_mib=0.1)
    assert opened and all(f.closed for f in opened)
    assert inputs and all(f.closed for f in inputs)
    assert readers and all(r._file is None and r._handles == [] for r in readers)
    assert indexes and all(s.closed and not Path(s.path).exists() for s in indexes)


def test_cursor_varints_cross_chunks_and_uint64_batches():
    values = [0, 127, 128, 300, (1 << 63) - 1, (1 << 64) - 1]
    raw = b'\0' * (CHUNK - 1) + b''.join(varint(v) for v in values[2:])
    with Buffer(limit=0) as source, Records(limit=0) as records:
        source.write(raw)
        cursor = Cursor(source)
        for _ in range(CHUNK - 1):
            assert cursor.varint() == 0
        assert [cursor.varint() for _ in values[2:]] == values[2:]
        records.extend(array.array('Q', values))
        assert list(records) == values


def test_input_close_failure_does_not_publish_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'deep.fst'
    create_fst(path, events=10000)
    original = FSTReader._prepare
    def fail():
        raise OSError('injected input close failure')
    def prepare(self, *args):
        result = original(self, *args)
        self._resources.callback(fail)
        return result
    monkeypatch.setattr(FSTReader, '_prepare', prepare)
    with pytest.raises(ValueError, match='injected input close failure'):
        open_trace(path)
    assert not Path(index_path(path)).exists()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))


@pytest.mark.parametrize('raw', [b'\x80', b'\xff' * 10, b'\xff' * 9 + b'\x02'])
def test_cursor_rejects_truncated_and_overflowing_varints(raw):
    with pytest.raises(ValueError):
        Cursor(raw).varint()
