"""Bounded timestamp access, direct decoding and buffered decompressor output."""
import array
import struct
import threading

import pytest

from alwavelets.fst import FSTReader
from alwavelets.fst_stream import Buffer, CHUNK, Cursor, HISTORY, Records, decompress
from alwavelets.streaming import OperationCancelled
from tools.stress_fst import varint


@pytest.mark.parametrize('spill', [False, True])
def test_timestamp_pages_are_bounded_snapshots(spill):
    values = array.array('Q', ((1 << 63) + i for i in range(CHUNK)))
    with Records(limit=0 if spill else len(values) * 8) as table:
        table.extend(values)
        for start in (0, CHUNK // 8 - 1, CHUNK // 8, CHUNK - 3):
            page = table.uint64_page(start)
            assert page == values[start:start + CHUNK // 8]
            assert len(page) * page.itemsize <= CHUNK
            table[start] = 0
            assert page[0] == values[start]
            table[start] = values[start]
        for start in (-1, len(table)):
            with pytest.raises(IndexError):
                table.uint64_page(start)


@pytest.mark.parametrize('width,real', [(1, False), (7, False), (257, False), (64, True)])
def test_direct_batches_resume_across_timestamp_pages_and_zero_deltas(width, real):
    raw, offsets, indices, expected = bytearray(), [0], [], []
    previous = 0
    for i in range(11000):
        index = i // 3 + (20000 if i >= 6500 else 0)
        delta, previous = index - previous, index
        if real:
            value = repr(i / 8 - 100)
            raw.extend(varint(delta << 1) + struct.pack('>d', float(value)))
        elif width == 1:
            if i % 7 == 0:
                raw.extend(varint((delta << 4) | ((i % 8) << 1) | 1))
                value = 'xzxxxxxx'[i % 8]
            else:
                raw.extend(varint((delta << 2) | ((i % 2) << 1)))
                value = str(i % 2)
        elif i % 17 == 0:
            value = ('x' if i % 34 == 0 else 'z') * width
            raw.extend(varint((delta << 1) | 1) + value.encode())
        else:
            value = format(i % (1 << width), f'0{width}b')
            nbytes = (width + 7) // 8
            raw.extend(varint(delta << 1) + (int(value, 2) << (nbytes * 8 - width)).to_bytes(nbytes, 'big'))
        offsets.append(len(raw))
        indices.append(index)
        expected.append((index * 11 + (1 << 40), value))
    reader = FSTReader('unused')
    reader._cancel, reader._geom, reader._checkpoint = None, [], object()
    reader._real_fmt = '>d'
    reader._handles = [None, {'size': width, 'real': real}]
    with Buffer(limit=0) as chain, Records(limit=0) as times:
        chain.write(raw)
        times.extend(array.array('Q', (i * 11 + (1 << 40) for i in range(previous + 1))))
        reader._chain_state = [Cursor(chain), 0]
        batches = reader._chain_batches(1, chain, times)
        actual, consumed = [], 0
        for _ in range(2):
            timestamps, values = next(batches)
            consumed += len(timestamps)
            actual.extend(zip(timestamps, map(chr, values) if width == 1 else values))
            assert reader._chain_state[0].pos == offsets[consumed]
            assert reader._chain_state[1] == indices[consumed - 1]
        batches.close()
        # A fresh cursor has no buffered bytes: it must restart at the exact
        # next event, even when that event repeats the previous timestamp.
        cursor = Cursor(chain)
        cursor.pos = offsets[consumed]
        reader._chain_state = [cursor, indices[consumed - 1]]
        for timestamps, values in reader._chain_batches(1, chain, times):
            consumed += len(timestamps)
            actual.extend(zip(timestamps, map(chr, values) if width == 1 else values))
            assert cursor.pos == offsets[consumed]
            assert reader._chain_state[1] == indices[consumed - 1]
        assert actual == expected


@pytest.mark.parametrize('codec', ['lz4', 'fastlz'])
def test_many_short_compression_matches_write_bounded_chunks(monkeypatch, codec):
    # A long back-reference window with four-byte matches exercises history
    # trimming and pending output across multiple flushes, plus a final tail.
    count, distance = HISTORY, 4096
    if codec == 'lz4':
        extra = distance - 15
        raw = (b'\xf0' + b'\xff' * (extra // 255) + bytes([extra % 255]) + b'a' * distance +
               struct.pack('<H', distance) + (b'\0' + struct.pack('<H', distance)) * (count - 1) + b'\x50abcde')
    else:
        raw = b'\x1f' + b'a' * 32
        raw += (b'\x1f' + b'a' * 32) * (distance // 32 - 1)
        raw += bytes([0x40 | ((distance - 1) >> 8), (distance - 1) & 255]) * count + b'\x04abcde'
    expected = b'a' * (distance + count * 4) + b'abcde'
    writes, progress = [], []
    original = Buffer.write

    def write(self, data):
        assert 0 < len(data) <= CHUNK * 2
        writes.append(len(data))
        original(self, data)

    monkeypatch.setattr(Buffer, 'write', write)
    with decompress(raw, codec, len(expected), limit=0,
                    progress_cb=lambda done, total: progress.append((done, total))) as block:
        assert block[:] == expected
    assert sum(writes) == len(expected)
    assert len(writes) <= len(expected) // CHUNK + 1
    assert progress[0] == (0, len(expected)) and progress[-1] == (len(expected), len(expected))
    assert progress == sorted(progress)


def test_cancel_during_buffered_match_flush_closes_output(monkeypatch):
    cancel, files = threading.Event(), []
    original = Buffer.write

    def write(self, data):
        original(self, data)
        files.append(self.file)
        cancel.set()

    monkeypatch.setattr(Buffer, 'write', write)
    raw = b'\x10a\x01\x00' + b'\0\x01\x00' * CHUNK
    with pytest.raises(OperationCancelled):
        decompress(raw, 'lz4', 1 + 4 * (CHUNK + 1), limit=0, cancel=cancel)
    assert files and all(file.closed for file in files)
