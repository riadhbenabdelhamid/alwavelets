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


class Output:
    def __init__(self, buffer, expected, progress_cb=None):
        self.buffer, self.expected = buffer, expected
        self.progress_cb, self.reported = progress_cb, 0
        self.history = bytearray()
        self.size, self.pending = 0, 0

    def write(self, data):
        size = len(data)
        if self.expected is not None and self.size + size > self.expected:
            raise ValueError('FST decompressed size exceeds declaration')
        self.history.extend(data)
        self.size += size
        self.pending += size
        # LZ4/FastLZ often emit just a few bytes per literal or match. Reuse
        # their bounded history as the write buffer instead of spilling each
        # tiny piece separately. Never trim bytes that have not been flushed.
        if self.pending >= CHUNK:
            self.flush()
        if len(self.history) > HISTORY * 2:
            del self.history[:-HISTORY]

    def flush(self):
        if self.pending:
            self.buffer.write(self.history[-self.pending:])
            self.pending = 0
        if self.progress_cb is not None and self.size - self.reported >= CHUNK:
            self.reported = self.size
            self.progress_cb(self.reported, self.expected)

    def match(self, distance, size):
        if not 0 < distance <= min(len(self.history), HISTORY):
            raise ValueError('FST compressed block has invalid match distance')
        while size:
            count = min(size, CHUNK)
            start = len(self.history) - distance
            block = self.history[start:start + min(distance, count)]
            if count > distance:
                block = (block * ((count + distance - 1) // distance))[:count]
            self.write(block)
            size -= count


def decompress(data, codec, expected, *, limit=1024 * 1024, cancel=None, wbits=15, progress_cb=None):
    """Decode to a closeable buffer; working memory is independent of block size."""
    result = Buffer(limit, cancel)
    output = Output(result, expected, progress_cb)
    source = Cursor(data, cancel)
    try:
        check_cancel(cancel)
        if progress_cb is not None:
            progress_cb(0, expected)
        if codec == 'raw':
            while source:
                output.write(source.read(min(CHUNK, len(data) - source.pos)))
        elif codec == 'zlib':
            decoder = zlib.decompressobj(wbits)
            while source:
                chunk = source.read(min(CHUNK, len(data) - source.pos))
                while chunk:
                    output.write(decoder.decompress(chunk, CHUNK))
                    if decoder.unused_data:
                        raise ValueError('Trailing data in compressed FST block')
                    chunk = decoder.unconsumed_tail
            if not decoder.eof:
                raise ValueError('Truncated compressed FST block')
        elif codec == 'lz4':
            while source:
                token = source.byte()
                literal = token >> 4
                if literal == 15:
                    while True:
                        extra = source.byte()
                        literal += extra
                        if extra != 255:
                            break
                while literal:
                    count = min(literal, CHUNK)
                    output.write(source.read(count))
                    literal -= count
                if not source:
                    break
                distance = source.byte() | (source.byte() << 8)
                size = token & 15
                if size == 15:
                    while True:
                        extra = source.byte()
                        size += extra
                        if extra != 255:
                            break
                output.match(distance, size + 4)
        elif codec == 'fastlz':
            first = source.byte()
            level, control = (first >> 5) + 1, first & 31
            if level not in (1, 2):
                raise ValueError('Invalid FastLZ level')
            while True:
                if control < 32:
                    output.write(source.read(control + 1))
                else:
                    size, offset = (control >> 5) - 1, (control & 31) << 8
                    if size == 6:
                        while True:
                            extra = source.byte()
                            size += extra
                            if level == 1 or extra != 255:
                                break
                    code = source.byte()
                    distance = offset + code + 1
                    if level == 2 and code == 255 and offset == 31 << 8:
                        distance = (source.byte() << 8 | source.byte()) + 8192
                    output.match(distance, size + 3)
                if not source:
                    break
                control = source.byte()
        else:
            raise ValueError('Unknown FST compression: ' + codec)
        output.flush()
        if expected is not None and len(result) != expected:
            raise ValueError('FST decompressed size mismatch (%d != %d)' % (len(result), expected))
        check_cancel(cancel)
        if progress_cb is not None:
            progress_cb(len(result), expected)
        return result
    except BaseException:
        result.close()
        raise
