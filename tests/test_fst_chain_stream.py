"""Forward decompression must preserve boundaries, late validation and resume."""
import io
from pathlib import Path
import struct
import threading
import tracemalloc
import zlib

import pytest

from alwavelets import fst_stream, storage
from alwavelets.fst import FSTReader
from alwavelets.fst_stream import CHUNK, ChainStream, Cursor, FileSlice
from alwavelets.streaming import OperationCancelled
from test_fst_resume import interrupt
from test_fst_stream import fastlz_literals, lz4_literal
from tools.stress_fst import create_fst, varint

U64 = struct.Struct('>Q')


def encode(raw, codec):
    return {'raw': lambda data: data, 'zlib': zlib.compress,
            'lz4': lz4_literal, 'fastlz': fastlz_literals}[codec](raw)


@pytest.mark.parametrize('codec', ['raw', 'zlib', 'lz4', 'fastlz'])
def test_forward_stream_handles_partial_reads_wide_values_and_resume_without_spilling(monkeypatch, codec):
    raw = bytes(range(251)) * 9000
    packed = encode(raw, codec)

    def no_spill(*args, **kwargs):
        raise AssertionError('Value chains must not create a decompression spill file')

    monkeypatch.setattr(fst_stream.tempfile, 'TemporaryFile', no_spill)
    tracemalloc.start()
    try:
        with ChainStream(FileSlice(io.BytesIO(packed), 0, len(packed)), codec, len(raw)) as chain:
            cursor = Cursor(chain)
            for size in (1, CHUNK - 2, 9, CHUNK * 3 + 17, 3, 1025):
                pos = cursor.pos
                assert cursor.read(size) == raw[pos:pos + size]
            assert not chain.finished
            # A fresh resumed cursor skips output without retaining the prefix.
            cursor = Cursor(chain)
            cursor.pos = len(raw) - 2000
            assert cursor.read(2000) == raw[-2000:]
            assert len(chain.buffer) < CHUNK * 3
            chain.finish()
            assert chain.finished
            assert tracemalloc.get_traced_memory()[1] < 2 * 1024 * 1024
        assert chain.chunks.gi_frame is None and not chain.buffer
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize('codec', ['raw', 'zlib', 'lz4', 'fastlz'])
@pytest.mark.parametrize('finish', [False, True])
def test_cancel_during_stream_consumption_or_tail_validation(codec, finish):
    raw = b'a' * (CHUNK * 8)
    cancel = threading.Event()
    with ChainStream(encode(raw, codec), codec, len(raw), cancel=cancel) as chain:
        assert chain[:CHUNK] == raw[:CHUNK]
        cancel.set()
        with pytest.raises(OperationCancelled):
            if finish:
                chain.finish()
            else:
                chain[CHUNK * 6:CHUNK * 7]
    assert chain.chunks.gi_frame is None and not chain.buffer


@pytest.mark.parametrize('damage', ['raw-short', 'raw-long', 'zlib-eof', 'zlib-checksum',
                                   'zlib-trailing', 'lz4-distance', 'fastlz-eof'])
def test_finish_checks_corrupt_compressed_tails_after_readable_prefix(damage):
    raw = b'a' * (CHUNK * 4)
    codec = damage.split('-')[0]
    packed, expected = encode(raw, codec), len(raw)
    if damage == 'raw-short':
        expected += 1
    elif damage == 'raw-long':
        packed += b'!'
    elif damage == 'zlib-eof':
        packed = packed[:-1]
    elif damage == 'zlib-checksum':
        packed = packed[:-1] + bytes([packed[-1] ^ 255])
    elif damage == 'zlib-trailing':
        packed += b'extra'
    elif damage == 'lz4-distance':
        packed += b'\0\0'
    else:
        packed += b'\xe0'
    with ChainStream(packed, codec, expected) as chain:
        assert chain[:CHUNK] == raw[:CHUNK]
        with pytest.raises((ValueError, zlib.error)):
            chain.finish()
    assert chain.chunks.gi_frame is None


def chain_fixture(path, codec, events=CHUNK * 3, bad_tail=False, unused_tail=False):
    """One long scalar chain; optional bad compression after an intact prefix."""
    create_fst(path, events=2)
    template = path.read_bytes()
    vc = 1 + U64.unpack_from(template, 1)[0]
    header = bytearray(template[:vc])
    header[17:25] = U64.pack(events - 1)
    hierarchy = template[vc + 1 + U64.unpack_from(template, vc + 1)[0]:]
    chain = b'\0' + (b'\x06\x04' * (events // 2))[:events - 1]
    if unused_tail:
        # The next event lies beyond the time table. Even when event decoding
        # stops there, the remaining compressed tail must still be validated.
        chain += varint(events << 2) + b'\0' * CHUNK
    packed = encode(chain, codec)
    if bad_tail:
        if codec == 'raw':
            # A declared chain longer than its source is a truncated block.
            unpacked = len(chain) + 1
            codec = 'zlib'
            packed = zlib.compress(chain)
        else:
            unpacked = len(chain)
            packed = packed[:-1] if codec == 'zlib' else packed + (b'\0\0' if codec == 'lz4' else b'\xe0')
    else:
        unpacked = 0 if codec == 'raw' else len(chain)
    raw_times = b'\0' + b'\1' * (events - 1)
    times = zlib.compress(raw_times)
    body = (U64.pack(0) + U64.pack(events - 1) + U64.pack(len(chain)) + b'\1\1\1' + b'0' +
            b'\1' + {'raw': b'Z', 'zlib': b'Z', 'lz4': b'4', 'fastlz': b'F'}[codec] +
            varint(unpacked) + packed + b'\3' + U64.pack(1) + times +
            struct.pack('>QQQ', len(raw_times), len(times), events))
    path.write_bytes(header + b'\x08' + U64.pack(len(body) + 8) + body + hierarchy)


@pytest.mark.parametrize('codec', ['raw', 'zlib', 'lz4', 'fastlz'])
def test_background_values_publish_before_chain_decompression_finishes(tmp_path, monkeypatch, codec):
    source = tmp_path / 'progressive.fst'
    chain_fixture(source, codec)
    entered, release = threading.Event(), threading.Event()
    chains = []
    decode, publish = FSTReader._decode_chain, storage.PageWriter.publish

    def track(self, handle, chain, times, begin):
        assert isinstance(chain, ChainStream) and not chain.buffer
        chains.append(chain)
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
        assert 0 < len(chains[0].buffer) < len(chains[0])
        assert not chains[0].finished
        assert trace.signals['1'].value_at(1) == '1'
        trace._store.cancel.set()
        release.set()
        trace._store.thread.join(5)
        assert not trace.is_loading and 'cancelled' in trace._load_error.lower()
        assert chains[0].chunks.gi_frame is None and not chains[0].buffer
    finally:
        release.set()
        trace.close()


@pytest.mark.parametrize('codec', ['raw', 'zlib', 'lz4', 'fastlz'])
def test_resume_uses_event_byte_offset_not_prefetched_decompression_position(tmp_path, monkeypatch, codec):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'resume.fst'
    events = CHUNK * 3
    chain_fixture(source, codec, events)
    saved = interrupt(source, monkeypatch, lambda state, writer:
                      state['phase'] == 'values' and state['position'] > CHUNK)
    assert saved['checkpoint']['state']['position'] < events
    with storage.open_trace(source, memory_mib=0.1) as trace:
        assert trace._resume_offset == saved['checkpoint']['offset']
        assert trace.load_changes == events
        assert list(trace.signals['1'].iter_changes()) == [(i, str(i % 2)) for i in range(events)]


@pytest.mark.parametrize('codec', ['zlib', 'lz4', 'fastlz'])
@pytest.mark.parametrize('unused_tail', [False, True])
def test_late_chain_corruption_cannot_be_skipped_by_resume(tmp_path, monkeypatch, codec, unused_tail):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(storage, 'CHECKPOINT_BYTES', 0)
    source = tmp_path / 'broken.fst'
    chain_fixture(source, codec, bad_tail=True, unused_tail=unused_tail)
    for _ in range(2):
        with pytest.raises(ValueError):
            storage.open_trace(source, memory_mib=0.1)
        assert not Path(storage.index_path(source)).exists()


def test_resume_at_end_of_chain_still_checks_checksum(tmp_path, monkeypatch):
    monkeypatch.setenv('ALWAVELETS_CACHE_DIR', str(tmp_path / 'cache'))
    source = tmp_path / 'end.fst'
    chain_fixture(source, 'zlib', bad_tail=True)
    saved = interrupt(source, monkeypatch, lambda state, writer:
                      state['phase'] == 'values' and state['position'] == CHUNK * 3)
    assert saved['checkpoint']['state']['position'] == CHUNK * 3
    with pytest.raises(ValueError, match='Truncated'):
        storage.open_trace(source, memory_mib=0.1)
    assert not Path(storage.index_path(source)).exists()
