"""LZ token boundaries, long extensions and history reuse must stay exact."""
import io
import random
import struct
import threading

import pytest

from alwavelets import fst_stream
from alwavelets.fst import fastlz_decompress, lz4_block_decompress
from alwavelets.fst_stream import CHUNK, HISTORY, FileSlice, decompress, iter_decompress
from alwavelets.streaming import OperationCancelled
from test_fst_stream import fastlz_literals


def extension(size):
    return b'\xff' * (size // 255) + bytes([size % 255])


def lz4_sequence(literal, distance=None, size=0):
    match = size - 4 if distance is not None else 0
    raw = bytes([(min(len(literal), 15) << 4) | min(match, 15)])
    if len(literal) >= 15:
        raw += extension(len(literal) - 15)
    raw += literal
    if distance is not None:
        raw += struct.pack('<H', distance)
        if match >= 15:
            raw += extension(match - 15)
    return raw


def fastlz_match(distance, size, level):
    offset = distance - 1 if distance < 8192 or level == 1 else 8191
    raw = bytes([(min(size - 2, 7) << 5) | (offset >> 8)])
    if size >= 9:
        raw += bytes([size - 9]) if level == 1 else extension(size - 9)
    raw += bytes([offset & 255])
    if level == 2 and distance >= 8192:
        raw += struct.pack('>H', distance - 8192)
    return raw


def mixed_tokens(codec, level=1):
    rng = random.Random(71)
    # Enough seed data to exercise the largest distances and trim history.
    expected = bytearray(rng.getrandbits(8) for _ in range(HISTORY + 33))
    encoded = bytearray()
    literal = bytes(expected) if codec == 'lz4' else b''
    if codec == 'fastlz':
        encoded.extend(fastlz_literals(expected, level))
    largest = 65535 if codec == 'lz4' else (8192 if level == 1 else 73727)
    sizes = (4, 5, 18, 19, 20, 254, 255, 256, 4097) if codec == 'lz4' else (
        (3, 4, 8, 9, 10, 263, 264) if level == 1 else (3, 4, 8, 9, 10, 263, 264, 265, 4097))
    for i in range(4096):
        distance = rng.choice((1, 2, 3, 31, 255, 256, 4096, largest - 1, largest))
        size = rng.choice(sizes)
        if codec == 'lz4':
            encoded.extend(lz4_sequence(literal, distance, size))
        else:
            encoded.extend(fastlz_match(distance, size, level))
        # A byte-at-a-time oracle also covers overlapping back-references.
        for _ in range(size):
            expected.append(expected[-distance])
        literal = bytes(rng.getrandbits(8) for _ in range(i % 33))
        expected.extend(literal)
        if codec == 'fastlz' and literal:
            encoded.extend(fastlz_literals(literal))
    if codec == 'lz4':
        expected.extend(b'final-literals')
        encoded.extend(lz4_sequence(literal + b'final-literals'))
    return bytes(encoded), bytes(expected)


@pytest.mark.parametrize('codec,level', [('lz4', 1), ('fastlz', 1), ('fastlz', 2)])
@pytest.mark.parametrize('chunk_size', [17, 4093, CHUNK])
def test_tokens_and_maximum_distances_cross_refills_and_trimmed_history(monkeypatch, codec, level, chunk_size):
    packed, expected = mixed_tokens(codec, level)
    reference = lz4_block_decompress if codec == 'lz4' else fastlz_decompress
    assert reference(packed, len(expected)) == expected
    monkeypatch.setattr(fst_stream, 'CHUNK', chunk_size)
    reads = []

    class BoundedSource(FileSlice):
        def __getitem__(self, key):
            assert 0 < key.stop - key.start <= chunk_size
            reads.append((key.start, key.stop))
            return super().__getitem__(key)

    source = BoundedSource(io.BytesIO(packed), 0, len(packed))
    chunks = list(iter_decompress(source, codec, len(expected)))
    assert b''.join(chunks) == expected
    assert all(0 < len(chunk) < chunk_size * 2 for chunk in chunks)
    assert len(reads) > 1 and [start for start, _ in reads] == sorted(start for start, _ in reads)
    assert len(expected) > HISTORY * 2


@pytest.mark.parametrize('codec,level', [('lz4', 1), ('fastlz', 1), ('fastlz', 2)])
def test_every_truncated_token_prefix_fails_cleanly(monkeypatch, codec, level):
    literal = bytes(range(17))
    if codec == 'lz4':
        packed = lz4_sequence(literal, 17, 274) + lz4_sequence(b'final-literals')
        expected = 17 + 274 + 14
    else:
        packed = fastlz_literals(literal, level) + fastlz_match(17, 264, level) + fastlz_literals(b'final-literals')
        expected = 17 + 264 + 14
    monkeypatch.setattr(fst_stream, 'CHUNK', 7)
    for cut in range(len(packed)):
        with pytest.raises(ValueError):
            list(iter_decompress(packed[:cut], codec, expected))
    for wrong in (expected - 1, expected + 1):
        with pytest.raises(ValueError):
            list(iter_decompress(packed, codec, wrong))
    assert len(b''.join(iter_decompress(packed, codec, None))) == expected


@pytest.mark.parametrize('codec', ['lz4', 'fastlz'])
def test_long_overlapping_match_yields_bounded_output(codec):
    size = CHUNK * 20 + 7
    packed = (lz4_sequence(b'ab', 2, size) + lz4_sequence(b'finish') if codec == 'lz4' else
              fastlz_literals(b'ab', 2) + fastlz_match(2, size, 2) + fastlz_literals(b'finish'))
    expected = b'ab' + (b'ab' * ((size + 1) // 2))[:size] + b'finish'
    with decompress(FileSlice(io.BytesIO(packed), 0, len(packed)), codec, len(expected), limit=0) as decoded:
        assert decoded[:] == expected and decoded.file is not None
    assert decoded.file.closed


@pytest.mark.parametrize('codec,part', [('lz4', 'literal'), ('lz4', 'match'), ('fastlz', 'match')])
def test_cancel_while_reading_length_extensions_before_output(codec, part):
    # An extension longer than one input window must observe cancellation
    # without waiting to produce the declared multi-megabyte literal/match.
    prefix = b'\xf0' if part == 'literal' else (b'\x1fa\x01\0' if codec == 'lz4' else b'\x20a\xe0')
    packed = prefix + b'\xff' * (CHUNK * 2) + b'\0'
    cancel, reads = threading.Event(), []

    class CancellingSource(FileSlice):
        def __getitem__(self, key):
            reads.append(key)
            cancel.set()
            return super().__getitem__(key)

    source = CancellingSource(io.BytesIO(packed), 0, len(packed))
    with pytest.raises(OperationCancelled):
        decompress(source, codec, None, limit=0, cancel=cancel)
    assert len(reads) == 1


@pytest.mark.parametrize('codec', ['lz4', 'fastlz'])
def test_close_and_cancel_after_yield_release_history_without_reading_more(codec):
    packed, expected = mixed_tokens(codec, 2)
    cancel = threading.Event()

    class TrackedSource(FileSlice):
        reads = 0

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

    source = TrackedSource(io.BytesIO(packed), 0, len(packed))
    chunks = iter_decompress(source, codec, len(expected), cancel=cancel)
    assert next(chunks) == expected[:CHUNK]
    reads = source.reads
    cancel.set()
    with pytest.raises(OperationCancelled):
        next(chunks)
    assert chunks.gi_frame is None and source.reads == reads
    chunks = iter_decompress(packed, codec, len(expected))
    next(chunks)
    chunks.close()
    assert chunks.gi_frame is None
