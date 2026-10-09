"""Bounded FST blocks, cancellation, expansion policy and exact parser parity."""
import io
from pathlib import Path
import struct
import threading
import tracemalloc
import zlib

import pytest

from alwavelets.fst import FSTReader, lz4_block_decompress, fastlz_decompress
from alwavelets.fst_stream import Buffer, CHUNK, FileSlice, Records, decompress
from alwavelets.storage import open_trace, use_disk_storage
from alwavelets.streaming import OperationCancelled
from tools.stress_fst import create_fst

FIXTURES = Path(__file__).parent / 'fixtures'


def lz4_repeat(size):
    # One literal, a long overlapping match, then five final literals.
    extra = size - 6 - 4 - 15
    return b'\x1fa\x01\x00' + b'\xff' * (extra // 255) + bytes([extra % 255]) + b'\x50aaaaa'


def fastlz_literals(data, level=1):
    out = bytearray()
    for start in range(0, len(data), 32):
        chunk = data[start:start + 32]
        out.append(len(chunk) - 1 | (32 if not start and level == 2 else 0))
        out.extend(chunk)
    return bytes(out)


@pytest.mark.parametrize('codec', ['raw', 'zlib', 'lz4', 'fastlz'])
def test_large_block_spills_and_matches(codec):
    expected = b'a' * (2 * 1024 * 1024)
    data = {'raw': expected, 'zlib': zlib.compress(expected), 'lz4': lz4_repeat(len(expected)),
            'fastlz': fastlz_literals(expected)}[codec]
    tracemalloc.start()
    try:
        with decompress(FileSlice(io.BytesIO(data), 0, len(data)), codec, len(expected), limit=CHUNK) as block:
            assert block.file is not None
            for start in range(0, len(expected), CHUNK):
                assert block[start:start + CHUNK] == expected[start:start + CHUNK]
            assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
        assert block.file.closed
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('codec,data,expected', [
    ('lz4', b'\x44abcd\x04\x00', b'abcd' * 3),
    ('fastlz', b'\x03abcd\xc0\x03', b'abcd' * 3),
    ('fastlz', b'\x20a\xe0\xff\x00\x00', b'a' * 265),
])
def test_overlapping_matches_and_fastlz_level2(codec, data, expected):
    with decompress(data, codec, len(expected), limit=0) as block:
        assert block[:] == expected
    reference = lz4_block_decompress if codec == 'lz4' else fastlz_decompress
    assert reference(data, len(expected)) == expected


def test_fastlz_level2_far_distance():
    prefix = bytes(range(256)) * 36
    distance = len(prefix)
    data = fastlz_literals(prefix, 2) + b'\x3f\xff' + struct.pack('>H', distance - 8192)
    with decompress(data, 'fastlz', len(prefix) + 3, limit=64) as block:
        assert block[:] == prefix + prefix[:3]


@pytest.mark.parametrize('codec,data,size', [
    ('zlib', zlib.compress(b'hello')[:-2], 5), ('zlib', zlib.compress(b'hello'), 4),
    ('zlib', zlib.compress(b'hello') + b'trailing', 5),
    ('raw', b'abc', 4), ('lz4', b'\xf0', 20), ('lz4', b'\x00\x00\x00', 4),
    ('lz4', b'\x20a', 2), ('fastlz', b'\x03ab', 4), ('fastlz', b'\xe0', 0),
    ('fastlz', b'\x20a\x3f\xff', 4)
])
def test_corrupt_blocks_fail_and_close_spills(monkeypatch, codec, data, size):
    import alwavelets.fst_stream as streaming
    opened = []
    original = streaming.tempfile.TemporaryFile
    def track(*args, **kwargs):
        f = original(*args, **kwargs)
        opened.append(f)
        return f
    monkeypatch.setattr(streaming.tempfile, 'TemporaryFile', track)
    with pytest.raises((ValueError, zlib.error)):
        decompress(data, codec, size, limit=0)
    assert all(f.closed for f in opened)


@pytest.mark.parametrize('codec', ['zlib', 'lz4', 'fastlz'])
def test_cancel_during_decompression_closes_spill(monkeypatch, codec):
    import alwavelets.fst_stream as streaming
    expected = 2 * 1024 * 1024
    data = {'zlib': zlib.compress(b'a' * expected), 'lz4': lz4_repeat(expected),
            'fastlz': fastlz_literals(b'a' * expected)}[codec]
    cancel, spilled = threading.Event(), []
    original = Buffer.write
    def write(self, chunk):
        original(self, chunk)
        if len(self) > CHUNK * 2:
            spilled.append(self.file)
            cancel.set()
    monkeypatch.setattr(streaming.Buffer, 'write', write)
    with pytest.raises(OperationCancelled):
        decompress(data, codec, expected, limit=CHUNK, cancel=cancel)
    assert spilled and all(f.closed for f in spilled)


def test_spilled_numeric_tables_random_access_and_replacement():
    with Records('<q', limit=64) as table:
        for i in range(20000):
            table.append(i * 3)
        table[10000] = -999
        assert table[-1] == 59997 and table[10000] == -999 and table[10001] == 30003
        assert table.buffer.file is not None
    assert table.buffer.file.closed


@pytest.mark.parametrize('variant', ['4', 'F', 'Z', 'c'])
def test_forced_spill_matches_existing_fixture(variant, monkeypatch):
    import alwavelets.fst_stream as streaming
    original = Buffer.__init__
    def force(self, limit=0, cancel=None):
        original(self, 0, cancel)
    monkeypatch.setattr(streaming.Buffer, '__init__', force)
    from alwavelets.parsers import VCDFile
    with VCDFile.parse(str(FIXTURES / 'fst_ref.vcd')) as expected:
        with open_trace(FIXTURES / ('fst_ref_' + variant + '.fst'), memory_mib=1, use_cache=False) as actual:
            assert actual.max_time == expected.max_time
            for a, b in zip(actual.signals.values(), expected.signals.values()):
                assert a.full_name == b.full_name
                aa, bb = list(a.iter_changes()), list(b.iter_changes())
                if a.signal_type == 'real':
                    assert [t for t, _ in aa] == [t for t, _ in bb]
                    assert [float(v) if v not in ('x', 'z') else v for _, v in aa] == [float(v) if v not in ('x', 'z') else v for _, v in bb]
                else:
                    assert aa == bb


def test_small_compressed_deep_trace_chooses_disk(tmp_path):
    path = tmp_path / 'deep.fst'
    create_fst(path, events=1200000)
    assert path.stat().st_size < 1024 * 1024 and use_disk_storage(path)
    assert not use_disk_storage(path, 'memory')
    with open_trace(path, memory_mib=1, use_cache=False) as trace:
        signal = trace.signals[trace.signal_order[0]]
        assert signal.num_changes() == 1200000
        assert signal.value_at(1199999) == '1'


def test_small_native_stays_eager_and_wrapper_is_conservative():
    assert not use_disk_storage(FIXTURES / 'fst_ref_Z.fst')
    assert use_disk_storage(FIXTURES / 'fst_ref_c.fst')
    assert not use_disk_storage(FIXTURES / 'fst_ref_c.fst', 'memory')


def test_metadata_and_frame_spill_keep_all_declarations(tmp_path):
    path = tmp_path / 'wide.fst'
    create_fst(path, events=1, signals=4096, width=256)
    with open_trace(path, memory_mib=1, use_cache=False) as trace:
        assert len(trace.signal_order) == 4096
        for index in (0, 2048, 4095):
            signal = trace.signals[trace.signal_order[index]]
            assert signal.num_changes() == 1 and signal.value_at(0) == '0' * 256


def test_cancel_metadata_and_index_never_publishes_incomplete_cache(tmp_path, monkeypatch):
    from alwavelets.storage import index_path
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'deep.fst'
    create_fst(path, events=200000)
    cancel = threading.Event()
    original = FSTReader._decode_chain
    def stop(self, *args):
        cancel.set()
        original(self, *args)
    monkeypatch.setattr(FSTReader, '_decode_chain', stop)
    with pytest.raises(ValueError, match='cancelled'):
        open_trace(path, memory_mib=1, cancel=cancel)
    assert not Path(index_path(path)).exists()
    assert not list((tmp_path / 'cache').rglob('alwavelets-index-*'))
    with pytest.raises(OperationCancelled):
        FSTReader(path).parse(metadata_only=True, cancel=cancel)


@pytest.mark.parametrize('raw', [b'\x80', b'\xff' * 11])
def test_truncated_and_oversized_varints(raw):
    with pytest.raises(ValueError):
        FSTReader._read_varint(raw, 0)
    with pytest.raises(ValueError):
        FSTReader._read_svint(raw, 0)
    with pytest.raises(ValueError):
        FSTReader._fst_varint_file(io.BytesIO(raw))


def lz4_literal(data):
    extra = len(data) - 15
    return b'\xf0' + b'\xff' * (extra // 255) + bytes([extra % 255]) + data


@pytest.mark.parametrize('variant', ['gzip', 'raw-deflate', 'lz4', 'lz4-duo'])
def test_hierarchy_compression_variants_spill(variant):
    data = b'scope\0signal\0' * 10000
    reader = FSTReader('unused')
    reader._cancel, reader._limit = None, CHUNK
    if variant in ('gzip', 'raw-deflate'):
        compressor = zlib.compressobj(wbits=31 if variant == 'gzip' else -15)
        compressed = compressor.compress(data) + compressor.flush()
        body, kind = struct.pack('>Q', len(data)) + compressed, reader.BL_HIER
    elif variant == 'lz4':
        body, kind = struct.pack('>Q', len(data)) + lz4_literal(data), reader.BL_HIER_LZ4
    else:
        middle = lz4_literal(data)
        body = struct.pack('>QQ', len(data), len(middle)) + lz4_literal(middle)
        kind = reader.BL_HIER_LZ4DUO
    with reader._read_hier_block(io.BytesIO(body), kind, len(body)) as block:
        assert block.file is not None and block[:] == data


def test_same_tick_chain_changes_are_preserved():
    reader = FSTReader('unused')
    reader._cancel, reader._geom = None, []
    reader._handles = [None, {'size': 1, 'real': False}]
    with Buffer(limit=0) as chain:
        chain.write(b'\x00\x02\x00\x06')  # three deltas at t=0, then a rise at t=1
        assert list(reader._chain_events(1, chain, [0, 1])) == [(0, '0'), (0, '1'), (0, '0'), (1, '1')]


def test_background_index_can_cancel_before_chains(tmp_path, monkeypatch):
    import alwavelets.fst_stream as streaming
    path = tmp_path / 'cancel.fst'
    create_fst(path, events=200000)
    entered, release = threading.Event(), threading.Event()
    original = FSTReader._read_vc_block
    opened = []
    temporary = streaming.tempfile.TemporaryFile
    def track(*args, **kwargs):
        f = temporary(*args, **kwargs)
        opened.append(f)
        return f
    monkeypatch.setattr(streaming.tempfile, 'TemporaryFile', track)
    def paused(self, *args):
        entered.set()
        assert release.wait(5)
        self._check()
        return original(self, *args)
    monkeypatch.setattr(FSTReader, '_read_vc_block', paused)
    trace = open_trace(path, background=True, memory_mib=1, use_cache=False)
    try:
        assert entered.wait(5)
        trace._store.cancel.set()
        release.set()
        trace._store.thread.join(5)
        assert not trace._store.thread.is_alive() and not trace.is_loading
        assert 'cancelled' in trace._load_error.lower()
    finally:
        release.set()
        trace.close()
    assert all(f.closed for f in opened)
