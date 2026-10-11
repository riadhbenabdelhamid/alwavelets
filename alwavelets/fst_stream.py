"""Bounded FST block storage, numeric tables and streaming decompression."""
from collections.abc import Sequence
import array
import struct
import sys
import tempfile
import zlib

from .streaming import check_cancel

CHUNK = 64 * 1024
HISTORY = 128 * 1024  # Covers LZ4 and both FastLZ back-reference windows.


class Buffer:
    """Bytes-like random access with a small read cache and automatic disk spill."""
    def __init__(self, limit=1024 * 1024, cancel=None):
        self.limit, self.cancel = max(0, int(limit)), cancel
        self.data = bytearray()
        self.file = None
        self.size = 0
        self.position = 0
        self.page, self.page_start = b'', 0

    def _seek(self, position):
        if self.position != position:
            self.file.seek(position)
            self.position = position

    def __len__(self):
        return self.size

    def write(self, data):
        check_cancel(self.cancel)
        if self.file is None and self.size + len(data) > self.limit:
            self.file = tempfile.TemporaryFile(prefix='alwavelets-fst-')
            self.file.write(self.data)
            self.position = self.size
            self.data = None
        if self.file is None:
            self.data.extend(data)
        else:
            self._seek(self.size)
            self.file.write(data)
            self.position += len(data)
        self.size += len(data)
        self.page = b''

    def replace(self, offset, data):
        check_cancel(self.cancel)
        if offset < 0 or offset + len(data) > self.size:
            raise IndexError(offset)
        if self.file is None:
            self.data[offset:offset + len(data)] = data
        else:
            self._seek(offset)
            self.file.write(data)
            self.position += len(data)
        self.page = b''

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.size)
            if step != 1:
                raise ValueError('FST block slices must be contiguous')
            if stop <= start:
                return b''
            return self.read(start, stop - start)
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        if self.file is None:
            return self.data[index]
        if not self.page_start <= index < self.page_start + len(self.page):
            check_cancel(self.cancel)
            self.page_start = index // CHUNK * CHUNK
            self._seek(self.page_start)
            self.page = self.file.read(CHUNK)
            self.position += len(self.page)
        return self.page[index - self.page_start]

    def read(self, start, size):
        check_cancel(self.cancel)
        if self.file is None:
            return bytes(self.data[start:start + size])
        if self.page_start <= start and start + size <= self.page_start + len(self.page):
            return self.page[start - self.page_start:start - self.page_start + size]
        if size <= CHUNK:
            self.page_start = start
            self._seek(start)
            self.page = self.file.read(CHUNK)
            self.position += len(self.page)
            return self.page[:size]
        self._seek(start)
        data = self.file.read(size)  # Only a single declaration/value can request this.
        self.position += len(data)
        return data

    def find(self, needle, start=0):
        if self.file is None:
            return self.data.find(needle, start)
        start = max(0, self.size + start) if start < 0 else start
        length = len(needle)
        if not length:
            return start if start <= self.size else -1
        while start + length <= self.size:
            check_cancel(self.cancel)
            end = self.page_start + len(self.page)
            if self.page_start <= start and start + length <= end:
                found = self.page.find(needle, start - self.page_start)
                if found >= 0:
                    return self.page_start + found
                if end == self.size:
                    return -1
                # Keep possible matches across the page boundary. In particular,
                # NUL searches consume each cached page before reading another.
                start = end - length + 1
            self.page_start = start
            self._seek(start)
            # Large needles also need lookahead, so an unsuccessful search
            # advances by more than one byte before refilling the page.
            self.page = self.file.read(max(CHUNK, length * 2))
            self.position += len(self.page)
        return -1

    def close(self):
        if self.file is not None:
            self.file.close()
        self.data, self.page = bytearray(), b''

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Records(Sequence):
    """Fixed-size numeric records; large time/offset tables stay on disk."""
    def __init__(self, fmt='<Q', limit=1024 * 1024, cancel=None):
        self.record = struct.Struct(fmt)
        self.buffer = Buffer(limit, cancel)

    def __len__(self):
        return len(self.buffer) // self.record.size

    def append(self, value):
        self.buffer.write(self.record.pack(*value if isinstance(value, tuple) else (value,)))

    def extend(self, values):
        """Pack an iterable in bounded batches, with a fast path for uint64 arrays."""
        if self.record.format == '<Q' and isinstance(values, array.array) and values.typecode == 'Q':
            if sys.byteorder != 'little':
                values = array.array('Q', values)
                values.byteswap()
            self.buffer.write(values.tobytes())
            return
        data = bytearray()
        for value in values:
            check_cancel(self.buffer.cancel)
            data.extend(self.record.pack(*value if isinstance(value, tuple) else (value,)))
            if len(data) >= CHUNK:
                self.buffer.write(data)
                data.clear()
        if data:
            self.buffer.write(data)

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        offset = index * self.record.size
        if self.buffer.file is None:
            row = self.record.unpack_from(self.buffer.data, offset)
        elif self.buffer.page_start <= offset and offset + self.record.size <= self.buffer.page_start + len(self.buffer.page):
            row = self.record.unpack_from(self.buffer.page, offset - self.buffer.page_start)
        else:
            row = self.record.unpack(self.buffer.read(offset, self.record.size))
        return row[0] if len(row) == 1 else row

    def __setitem__(self, index, value):
        if index < 0:
            index += len(self)
        self.buffer.replace(index * self.record.size, self.record.pack(*value if isinstance(value, tuple) else (value,)))

    def uint64_page(self, start):
        """Copy at most one page of timestamps into a native numeric array.

        Decoders can reuse this bounded snapshot without unpacking a record
        and checking the spill buffer for every event.
        """
        if self.record.format != '<Q':
            raise ValueError('Expected an unsigned 64-bit table')
        if not 0 <= start < len(self):
            raise IndexError(start)
        values = array.array('Q')
        values.frombytes(self.buffer.read(start * 8, min(CHUNK, (len(self) - start) * 8)))
        if sys.byteorder != 'little':
            values.byteswap()
        return values

    def close(self):
        self.buffer.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FileSlice:
    """A compressed range in an open file, without reading the whole range."""
    def __init__(self, file, start, size):
        if start < 0 or size < 0:
            raise ValueError('Invalid FST block range')
        self.file, self.start, self.size = file, start, size

    def __len__(self):
        return self.size

    def __getitem__(self, key):
        start, end, step = key.indices(self.size)
        if step != 1:
            raise ValueError('Non-contiguous FST read')
        self.file.seek(self.start + start)
        data = self.file.read(max(0, end - start))
        if len(data) != max(0, end - start):
            raise ValueError('Truncated FST block')
        return data


class Cursor:
    def __init__(self, data, cancel=None):
        self.data, self.cancel = data, cancel
        self.pos, self.chunk, self.index = 0, b'', 0

    def __bool__(self):
        return self.pos < len(self.data)

    def read(self, size):
        check_cancel(self.cancel)
        if self.pos + size > len(self.data):
            raise ValueError('Truncated compressed FST block')
        if self.index + size > len(self.chunk):
            self.chunk = self.data[self.pos:self.pos + max(CHUNK, size)]
            self.index = 0
        data = self.chunk[self.index:self.index + size]
        self.index += size
        self.pos += size
        return data

    def byte(self):
        if self.index >= len(self.chunk):
            check_cancel(self.cancel)
            if not self:
                raise ValueError('Truncated compressed FST block')
            self.chunk = self.data[self.pos:self.pos + CHUNK]
            self.index = 0
        value = self.chunk[self.index]
        self.index += 1
        self.pos += 1
        return value

    def varint(self):
        """Read a LEB128 integer, with a fast path for single-byte deltas."""
        if self.index >= len(self.chunk):
            first = self.byte()
        else:
            first = self.chunk[self.index]
            self.index += 1
            self.pos += 1
        if first < 128:
            return first
        value, shift = first & 127, 7
        while shift < 70:
            byte = self.byte()
            value |= (byte & 127) << shift
            if byte < 128:
                if value >= 1 << 64:
                    raise ValueError('FST varint overflow')
                return value
            shift += 7
        raise ValueError('Oversized FST varint')


class TimeTable(Sequence):
    """Expand delta timestamps on demand, retaining bounded pages on disk.

    The caller owns the decompressed delta buffer. This table owns only its
    numeric records; len() reports the declared count without decoding. Call
    finish() before accepting a block, including timestamps no chain used.
    """
    def __init__(self, data, count, *, limit=1024 * 1024, cancel=None):
        if not 0 <= count <= len(data):  # Every timestamp uses at least one byte.
            raise ValueError('Invalid FST timestamp count')
        self.count, self.cancel = count, cancel
        self.cursor = Cursor(data, cancel)
        self.records = Records(limit=limit, cancel=cancel)
        self.time = 0
        self.page, self.page_start = (), 0

    def __len__(self):
        return self.count

    def _ensure(self, stop):
        decoded = len(self.records)
        rv, timestamp = self.cursor.varint, self.time
        while decoded < stop:
            check_cancel(self.cancel)
            batch = array.array('Q')
            for _ in range(min(CHUNK // 8, self.count - decoded)):
                timestamp += rv()
                batch.append(timestamp)
            self.records.extend(batch)
            decoded += len(batch)
            self.time = timestamp

    def __getitem__(self, index):
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        if not self.page_start <= index < self.page_start + len(self.page):
            self.page = self.uint64_page(index)
            self.page_start = index
        return self.page[index - self.page_start]

    def uint64_page(self, start):
        if not 0 <= start < self.count:
            raise IndexError(start)
        self._ensure(min(self.count, start + CHUNK // 8))
        return self.records.uint64_page(start)

    def finish(self):
        self._ensure(self.count)

    def close(self):
        self.records.close()
        self.page = ()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Output:
    """Bounded compression history, emitting complete chunks to either reader."""
    def __init__(self, expected, cancel=None):
        self.expected, self.cancel = expected, cancel
        self.history = bytearray()
        self.size, self.pending = 0, 0

    def write(self, data):
        size = len(data)
        if self.expected is not None and self.size + size > self.expected:
            raise ValueError('FST decompressed size exceeds declaration')
        self.history.extend(data)
        self.size += size
        self.pending += size
        chunk = self.flush() if self.pending >= CHUNK else None
        if len(self.history) > HISTORY * 2:
            del self.history[:-HISTORY]
        return chunk

    def flush(self):
        check_cancel(self.cancel)
        if self.pending:
            chunk = self.history[-self.pending:]
            self.pending = 0
            return chunk
        return None


def _iter_lz(data, lz4, expected, cancel):
    """Decode LZ tokens with local cursors and bounded input/output windows.

    Tiny literals and matches stay in this loop; refills and yields perform
    cancellation checks. Long literals, matches and length extensions still
    advance in bounded chunks. Both codecs share the overlap-copy rules.
    """
    length, offset = len(data), 0

    def refill(consumed, need=1):
        nonlocal offset
        check_cancel(cancel)
        offset += consumed
        if offset + need > length:
            raise ValueError('Truncated compressed FST block')
        block = data[offset:offset + CHUNK]
        if len(block) < need:
            raise ValueError('Truncated compressed FST block')
        return block, len(block), 0

    block, end, pos = b'', 0, 0
    history = bytearray()
    history_size = pending = produced = level = 0
    if not lz4 and not length:
        raise ValueError('Truncated compressed FST block')
    while offset + pos < length:
        if pos == end:
            block, end, pos = refill(pos)
        control = block[pos]
        pos += 1
        if lz4:
            literal = control >> 4
            if literal == 15:
                while True:
                    if pos == end:
                        block, end, pos = refill(pos)
                    extra = block[pos]
                    pos += 1
                    literal += extra
                    if extra != 255:
                        break
        else:
            if not level:
                level = (control >> 5) + 1
                if level not in (1, 2):
                    raise ValueError('Invalid FastLZ level')
                control &= 31
            literal = control + 1 if control < 32 else 0
        produced += literal
        if expected is not None and produced > expected:
            raise ValueError('FST decompressed size exceeds declaration')
        while literal:
            count = literal if literal < CHUNK else CHUNK
            if pos + count > end:
                block, end, pos = refill(pos, count)
            history.extend(block[pos:pos + count])
            pos += count
            history_size += count
            pending += count
            literal -= count
            if pending >= CHUNK:
                check_cancel(cancel)
                yield history[-pending:]
                check_cancel(cancel)
                pending = 0
                if history_size > HISTORY * 2:
                    del history[:-HISTORY]
                    history_size = HISTORY
        if lz4:
            if offset + pos == length:  # The final LZ4 sequence is literals only.
                break
            if pos + 2 > end:
                block, end, pos = refill(pos, 2)
            distance = block[pos] | (block[pos + 1] << 8)
            pos += 2
            size = control & 15
            if size == 15:
                while True:
                    if pos == end:
                        block, end, pos = refill(pos)
                    extra = block[pos]
                    pos += 1
                    size += extra
                    if extra != 255:
                        break
            size += 4
        else:
            if control < 32:
                continue
            size, high = (control >> 5) - 1, (control & 31) << 8
            if size == 6:
                while True:
                    if pos == end:
                        block, end, pos = refill(pos)
                    extra = block[pos]
                    pos += 1
                    size += extra
                    if level == 1 or extra != 255:
                        break
            if pos == end:
                block, end, pos = refill(pos)
            code = block[pos]
            pos += 1
            distance = high + code + 1
            if level == 2 and code == 255 and high == 31 << 8:
                if pos + 2 > end:
                    block, end, pos = refill(pos, 2)
                distance = (block[pos] << 8 | block[pos + 1]) + 8192
                pos += 2
            size += 3
        if distance <= 0 or distance > history_size or distance > HISTORY:
            raise ValueError('FST compressed block has invalid match distance')
        produced += size
        if expected is not None and produced > expected:
            raise ValueError('FST decompressed size exceeds declaration')
        while size:
            count = size if size < CHUNK else CHUNK
            start = history_size - distance
            if count <= distance:
                history.extend(history[start:start + count])
            else:
                pattern = history[start:]
                history.extend((pattern * ((count + distance - 1) // distance))[:count])
            history_size += count
            pending += count
            size -= count
            if pending >= CHUNK:
                check_cancel(cancel)
                yield history[-pending:]
                check_cancel(cancel)
                pending = 0
                if history_size > HISTORY * 2:
                    del history[:-HISTORY]
                    history_size = HISTORY
    check_cancel(cancel)
    if pending:
        yield history[-pending:]
    if expected is not None and produced != expected:
        raise ValueError('FST decompressed size mismatch (%d != %d)' % (produced, expected))
    check_cancel(cancel)


def iter_decompress(data, codec, expected, *, cancel=None, wbits=15):
    """Yield bounded chunks; exhaust the iterator to validate the entire block."""
    check_cancel(cancel)
    if codec in ('lz4', 'fastlz'):
        yield from _iter_lz(data, codec == 'lz4', expected, cancel)
        return
    output, source = Output(expected, cancel), Cursor(data, cancel)
    if codec == 'raw':
        while source:
            chunk = output.write(source.read(min(CHUNK, len(data) - source.pos)))
            if chunk:
                yield chunk
    elif codec == 'zlib':
        decoder = zlib.decompressobj(wbits)
        while source:
            compressed = source.read(min(CHUNK, len(data) - source.pos))
            while compressed:
                chunk = output.write(decoder.decompress(compressed, CHUNK))
                if decoder.unused_data:
                    raise ValueError('Trailing data in compressed FST block')
                if chunk:
                    yield chunk
                compressed = decoder.unconsumed_tail
        if not decoder.eof:
            raise ValueError('Truncated compressed FST block')
    else:
        raise ValueError('Unknown FST compression: ' + codec)
    chunk = output.flush()
    if chunk:
        yield chunk
    if expected is not None and output.size != expected:
        raise ValueError('FST decompressed size mismatch (%d != %d)' % (output.size, expected))
    check_cancel(cancel)


class ChainStream:
    """Forward slices of a value chain without materializing its full history.

    Cursor may reread the unconsumed tail of its last chunk or skip forward to
    a checkpoint. Only that tail and bounded decompressor chunks are retained.
    A single unusually wide value may request a larger slice. finish() validates
    any unread tail; close() abandons it when loading is cancelled or fails.
    """
    def __init__(self, data, codec, expected, *, cancel=None):
        if expected < 0:
            raise ValueError('Invalid FST chain size')
        self.size, self.cancel = expected, cancel
        self.chunks = iter_decompress(data, codec, expected, cancel=cancel)
        self.buffer, self.start = bytearray(), 0
        self.finished = False

    def __len__(self):
        return self.size

    def _next(self):
        check_cancel(self.cancel)
        chunk = next(self.chunks, None)
        if chunk is None:
            self.finished = True
            return bytearray()
        return chunk

    def __getitem__(self, key):
        start, end, step = key.indices(self.size)
        if step != 1 or start < self.start:
            raise ValueError('FST chains require forward contiguous reads')
        if end <= start:
            return b''
        # Skip old output without accumulating the prefix on resume.
        while start > self.start + len(self.buffer) and not self.finished:
            self.start += len(self.buffer)
            self.buffer = self._next()
        del self.buffer[:start - self.start]
        self.start = start
        while len(self.buffer) < end - start and not self.finished:
            self.buffer.extend(self._next())
        if len(self.buffer) < end - start:
            raise ValueError('Truncated compressed FST chain')
        return bytes(self.buffer[:end - start])

    def finish(self):
        while not self.finished:
            self._next()
        self.buffer = bytearray()

    def close(self):
        self.chunks.close()
        self.buffer = bytearray()
        self.finished = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def decompress(data, codec, expected, *, limit=1024 * 1024, cancel=None, wbits=15, progress_cb=None):
    """Decode to a closeable buffer for callers that need random access."""
    result = Buffer(limit, cancel)
    chunks = iter_decompress(data, codec, expected, cancel=cancel, wbits=wbits)
    try:
        check_cancel(cancel)
        reported = 0
        if progress_cb is not None:
            progress_cb(0, expected)
        for chunk in chunks:
            result.write(chunk)
            if progress_cb is not None and len(result) - reported >= CHUNK:
                reported = len(result)
                progress_cb(reported, expected)
        if progress_cb is not None:
            progress_cb(len(result), expected)
        return result
    except BaseException:
        result.close()
        raise
    finally:
        chunks.close()
