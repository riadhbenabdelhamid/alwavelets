"""Disk-backed waveform pages with bounded decoding and persistent indexes.

All declarations remain available. Signal selection is a query option, never
a requirement for opening a trace. SQLite stores independently compressed
pages; sequence views preserve the existing read-only signal API.
"""
import array
import bisect
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import ExitStack
import heapq
import json
import math
import os
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import zlib

from .core import VCDSignal, numeric_value, clear_pack_memos
from .value_stream import ValueState

VERSION = 6  # inline short histories and batched declarations/initial values
FST_VERSION = 7  # correct one-based dynamic chain alias targets
PAGE_BYTES = 64 * 1024
PAGE_EVENTS = 4096
SHORT_EVENTS = 8
SHORT_BYTES = 512
VCD_BATCH_KEYS = 512
VCD_BATCH_BYTES = 256 * 1024
WRITE_BATCH_BYTES = 256 * 1024
WRITE_BATCH_PAGES = 512
PUBLISH_INTERVAL = 0.25
PARTIAL_INTERVAL = 1.0
CHECKPOINT_INTERVAL = 5.0
CHECKPOINT_BYTES = 64 * 1024 * 1024
CHECKPOINT_VERSION = 1
FST_CHECKPOINT_VERSION = 1
_SCALAR_BYTES = {'0': 48, '1': 49, 'x': 120, 'X': 120, 'z': 122, 'Z': 122}
AUTO_THRESHOLD = 50 * 1024 * 1024


class Cancelled(ValueError):
    pass


def _encode_number(value):
    # Very wide logic vectors can exceed Python's decimal conversion limit.
    return json.dumps({'hex': hex(value)}) if isinstance(value, int) else json.dumps(value)


def _decode_number(value):
    value = json.loads(value)
    return int(value['hex'], 16) if isinstance(value, dict) else value


class SequenceView(Sequence):
    """Slices stay views, including for legacy consumers of _times/_values."""
    __slots__ = ('source', 'indices', 'transform')
    def __init__(self, source, indices=None, transform=None):
        self.source = source
        self.indices = range(len(source)) if indices is None else indices
        self.transform = transform

    def __len__(self):
        return len(self.indices)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self.source, self.indices[index], self.transform)
        value = self.source[self.indices[index]]
        return self.transform(value) if self.transform else value

    def __eq__(self, other):
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def __iter__(self):
        if self.indices.step == 1 and hasattr(self.source, 'iter_range'):
            values = self.source.iter_range(self.indices.start, self.indices.stop)
        else:
            values = (self.source[i] for i in self.indices)
        return map(self.transform, values) if self.transform else iter(values)


class PageSequence(Sequence):
    __slots__ = ('signal', 'values')
    typecode = 'q'

    def __init__(self, signal, values=False):
        self.signal, self.values = signal, values

    def __len__(self):
        return self.signal.num_changes()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self, range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        page, lo = self.signal.store.page_at(self.signal.key, index)
        index -= lo
        if not self.values:
            return page._times[index]
        return page._values[index] if self.signal._scalar else page.value_str(index)

    def bisect(self, timestamp, right=False):
        return self.signal.store.bisect(self.signal.key, timestamp, right)

    def iter_range(self, start, end):
        while start < end:
            page, lo = self.signal.store.page_at(self.signal.key, start)
            stop = min(end - lo, len(page._times))
            for i in range(start - lo, stop):
                yield (page._values[i] if self.signal._scalar else page.value_str(i)) if self.values else page._times[i]
            start = lo + stop

    def __iter__(self):
        return self.iter_range(0, len(self))


class StoredSignal(VCDSignal):
    __slots__ = ('store', 'key', '_time_view', '_value_view', '__weakref__')

    def __init__(self, metadata, key, store):
        # The metadata object owns no event data. Do not call the mutable
        # base constructor: these arrays are read-only page-backed views.
        for name in ('name', 'full_name', 'identifier', 'size', 'reference',
                     'declared_range', 'signal_type', 'scope', 'analog_mode',
                     'is_dumped', '_parent_sig', '_bit_idx', '_scalar'):
            setattr(self, name, getattr(metadata, name))
        self._packed, self._stride, self._memo = False, 0, None
        self.key, self.store = key, store
        self._time_view = self._value_view = None

    @property
    def _times(self):
        if self._time_view is None:
            self._time_view = PageSequence(self)
        return self._time_view

    @property
    def _values(self):
        if self._value_view is None:
            self._value_view = PageSequence(self, True)
        return self._value_view

    def num_changes(self):
        return self.store.counts.get(self.key, 0)

    def changes_in_window(self, start, end):
        lo = max(0, self._times.bisect(start) - 1)
        hi = self._times.bisect(end, True)
        return self._times[lo:hi], SequenceView(self._values, range(lo, hi),
                                               chr if self._scalar else None)

    def iter_changes(self, start=0, end=None):
        lo = self._times.bisect(start)
        hi = len(self._times) if end is None else self._times.bisect(end, True)
        values = self._values.iter_range(lo, hi)
        return zip(self._times.iter_range(lo, hi), map(chr, values) if self._scalar else values)

    def numeric_range_in_window(self, start, end):
        low, high = self.store.numeric_range(self, start, end)
        if low is None:
            return 0, 1
        return (low - 1, high + 1) if low == high else (low, high)

    def add_change(self, time, value):
        raise TypeError('disk-backed signals are read-only')


def _signature(path):
    st = os.stat(path)
    return [st.st_size, st.st_mtime_ns, sys.byteorder]


def _source_version(source):
    from .fst import FSTReader
    return FST_VERSION if FSTReader.is_fst(source) else VERSION


def index_path(path):
    from .cache import cache_path
    return cache_path(path) + '.sqlite'


def partial_index_path(path):
    return index_path(path) + '.partial'


def use_disk_storage(source, mode='auto', *, cancel=None):
    from .streaming import check_cancel
    check_cancel(cancel)
    if mode not in ('auto', 'memory', 'disk'):
        raise ValueError('storage must be auto, memory, or disk')
    if mode == 'memory':
        return False
    from .fst import FSTReader
    with open(source, 'rb') as f:
        supported = f.read(1) == b'$' or FSTReader.is_fst(source)
    if not supported and os.path.splitext(source)[1].lower() in ('.lxt', '.lxt2', '.ghw', '.vzt'):
        return False  # dispatcher converts first, then applies storage policy
    if mode == 'disk' and not supported:
        raise ValueError('Disk storage requires VCD or FST input')
    if not supported:
        return False
    if mode == 'disk' or os.path.getsize(source) > AUTO_THRESHOLD:
        return True
    if FSTReader.is_fst(source):
        return FSTReader.prefers_disk(source, AUTO_THRESHOLD, cancel=cancel)
    # A short waveform can still have an enormous declaration header. Stop as
    # soon as metadata alone would exceed the automatic storage threshold.
    declarations = 0
    with open(source, 'rb') as f:
        for line in f:
            check_cancel(cancel)
            line = line.lstrip()
            if line.startswith(b'$var '):
                declarations += 1
                if declarations * 640 > AUTO_THRESHOLD:
                    return True
            elif line.startswith(b'$enddefinitions'):
                break
    return False


def _definition(sig):
    return [sig.reference, sig.identifier, sig.size, sig.signal_type,
            sig.scope, sig.declared_range]


def _signal(definition, *, metadata_only=False):
    name, identifier, size, kind, scope, bounds = definition
    return VCDSignal(name, identifier, size, kind, scope, tuple(bounds) if bounds is not None else None,
                     metadata_only=metadata_only)


def _encode_page(sig):
    kind = 1 if sig._scalar else (2 if type(sig._values) is bytearray else 0)
    values = bytes(sig._values) if kind else json.dumps(sig._values, separators=(',', ':')).encode()
    return bytes([kind]) + sig._times.tobytes() + values


def _decode_page(definition, data, count):
    sig = _signal(definition)
    sig._times = array.array('q')
    sig._times.frombytes(data[1:1 + count * 8])
    raw = data[1 + count * 8:]
    if data[0] in (1, 2):
        sig._values = bytearray(raw)
    else:
        sig._packed = False
        sig._values = json.loads(raw)
    return sig


class TraceStore:
    """A cache budget covers decoded pages; metadata and SQLite are separate."""
    def __init__(self, path, memory_bytes=256 * 1024 * 1024):
        self.path = path
        self.memory_bytes = max(64 * 1024, int(memory_bytes))
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('PRAGMA cache_size=-2048')
        self.db.execute('PRAGMA temp_store=FILE')
        self.pages = OrderedDict()
        self.cache_bytes = self.peak_cache_bytes = self.page_reads = 0
        self.counts, self.definitions = {}, {}
        self.generation = 0
        self.total_changes = 0
        self.cancel = threading.Event()
        self.stage_cb = None
        self.thread = None
        self.temporary = None
        self.index_lease = None
        self.cache_manager = None
        self.preserve_checkpoint = False
        self.closed = False

    def bisect(self, key, timestamp, right=False):
        with self.lock:
            n = self.counts.get(key, 0)
            # SQLite binds integers, not Fraction (used by cross-timescale
            # comparisons). Round the lookup bound without losing precision.
            bound = math.floor(timestamp) if right else math.ceil(timestamp)
            if bound > (1 << 63) - 1:
                return n
            if bound < -(1 << 63):
                return 0
            short = self.short_page(key)
            if short is not None:
                count = min(n, len(short._times))
                index = (bisect.bisect_right if right else bisect.bisect_left)(short._times, timestamp, 0, count)
                if index < count or count == n:
                    return index
            op = '>' if right else '>='
            row = self.db.execute(f'SELECT lo FROM pages WHERE sid=? AND t1{op}? '
                                  'AND lo<? ORDER BY t1,lo LIMIT 1', (key, bound, n)).fetchone()
            if row is None:
                return n
            page, lo = self.page_at(key, row[0])
            return lo + (bisect.bisect_right if right else bisect.bisect_left)(page._times, timestamp)

    def page_at(self, key, index):
        with self.lock:
            # A lookup in the compact SQL index avoids keeping all page
            # offsets in Python for files with millions of blocks.
            row = self.db.execute('SELECT id,lo,n FROM pages WHERE sid=? AND lo<=? '
                                  'ORDER BY lo DESC LIMIT 1', (key, index)).fetchone()
            if row is None:
                short = self.short_page(key)
                if short is not None and 0 <= index < len(short._times):
                    return short, 0
                raise IndexError(index)
            if index >= row[1] + row[2]:
                raise IndexError(index)
            pid, lo, count = row
            cached = self.pages.get(pid)
            if cached is not None:
                self.pages.move_to_end(pid)
                return cached[0], lo
            blob = self.db.execute('SELECT data FROM pages WHERE id=?', (pid,)).fetchone()[0]
            data = zlib.decompress(blob)
            sig = _decode_page(self.definitions[key], data, count)
            self.cache_page(pid, sig, data[0])
            return sig, lo

    def cache_page(self, pid, sig, kind):
        size = sys.getsizeof(sig._times) + sys.getsizeof(sig._values) + 256
        if kind == 0:
            size += sum(sys.getsizeof(v) for v in sig._values)
        while self.pages and self.cache_bytes + size > self.memory_bytes:
            _, (_, old_size) = self.pages.popitem(last=False)
            self.cache_bytes -= old_size
        if size <= self.memory_bytes:
            self.pages[pid] = (sig, size)
            self.cache_bytes += size
            self.peak_cache_bytes = max(self.peak_cache_bytes, self.cache_bytes)
        self.page_reads += 1

    def short_page(self, key):
        """The inline prefix shares the same bounded cache as regular pages."""
        pid = ('short', key)
        cached = self.pages.get(pid)
        if cached is not None:
            self.pages.move_to_end(pid)
            return cached[0]
        row = self.db.execute('SELECT short_n,short_data FROM signals WHERE sid=?', (key,)).fetchone()
        if row is None or not row[0]:
            return None
        count, data = row
        sig = _decode_page(self.definitions[key], data, count)
        self.cache_page(pid, sig, data[0])
        return sig

    def forget_short(self, key):
        cached = self.pages.pop(('short', key), None)
        if cached is not None:
            self.cache_bytes -= cached[1]

    def numeric_range(self, sig, start, end):
        # Match changes_in_window, including the preceding baseline event.
        index = max(0, self.bisect(sig.key, start) - 1)
        stop = self.bisect(sig.key, end, True)
        low = high = None
        with self.lock:
            while index < stop:
                if self.cancel.is_set():
                    raise Cancelled('Read cancelled')
                row = self.db.execute('SELECT n,vmin,vmax FROM summaries WHERE sid=? '
                                      'AND lo=? AND n<=? ORDER BY level DESC LIMIT 1',
                                      (sig.key, index, stop - index)).fetchone()
                if row is not None:
                    n, vmin, vmax = row
                    candidates = (_decode_number(vmin), _decode_number(vmax))
                    index += n
                else:
                    page, lo = self.page_at(sig.key, index)
                    end_index = min(stop, lo + len(page._times))
                    candidates = (numeric_value(page.value_str(i), sig.signal_type)
                                  for i in range(index - lo, end_index - lo))
                    index = end_index
                for value in candidates:
                    if value is not None:
                        low = value if low is None else min(low, value)
                        high = value if high is None else max(high, value)
        return low, high

    def iter_events(self, signal_ids, start=0, end=None, cancel=None, progress=None):
        """Merge referenced signals lazily; memory is bounded by their heads."""
        def stream(sid):
            sig = self.vcd.signals[sid]
            for ordinal, (t, value) in enumerate(sig.iter_changes(start, end)):
                yield t, sid, ordinal, value
        streams = [stream(sid) for sid in signal_ids]
        count = 0
        for event in heapq.merge(*streams):
            if self.cancel.is_set() or (cancel is not None and cancel.is_set()):
                raise Cancelled('Analysis cancelled')
            count += 1
            if progress is not None and count % 4096 == 0:
                progress(event[0], end if end is not None else self.vcd.max_time)
            yield event[0], event[1], event[3]

    def close(self):
        self.cancel.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join()
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.db.close()
            self.pages.clear()
            self.cache_bytes = 0
            if self.temporary:
                self.temporary.cleanup()
            if self.index_lease:
                self.index_lease.close()
                self.index_lease = None
            if self.cache_manager:
                self.cache_manager.trim(exclude=(self.path,) if self.preserve_checkpoint else ())


def _storage_error(exc, store=None):
    """Restore the durable readable prefix after SQLite rolls back a full disk."""
    from .index_cache import disk_full
    if not disk_full(exc):
        return str(exc)
    saved = False
    format_name = 'waveform'
    if store is not None and not store.closed:
        store.preserve_checkpoint = store.cache_manager is not None and str(store.path).endswith('.partial')
        with store.lock:
            try:
                store.db.rollback()
                row = store.db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()
                meta = json.loads(row[0]) if row else {}
                saved = bool(meta.get('checkpoint')) and store.temporary is None
                if saved:
                    format_name = meta['checkpoint'].get('format', 'vcd').upper()
                    store.preserve_checkpoint = True
                    store.pages.clear()
                    store.cache_bytes = 0
                    store.counts.cache.clear()
                    store.total_changes = meta['total_changes']
                    store.vcd._load_changes = meta['changes']
                    store.vcd.max_time = meta['max_time']
                    store.vcd._load_progress = min(99, meta['checkpoint']['offset'] * 100 // max(1, meta['signature'][0]))
                    store.generation += 1
            except (sqlite3.Error, ValueError, KeyError, AttributeError):
                pass
    location = os.path.dirname(store.path) if store is not None else 'the index directory'
    message = f'Not enough disk space for waveform indexing in {location}. '
    if saved:
        return message + f'The last {format_name} checkpoint is saved; free space and reopen to resume, or choose another --cache-dir.'
    return message + 'Free space or choose another --cache-dir and reopen.'


class PageWriter:
    def __init__(self, store, vcd, *, checkpointed=False, resume=None):
        self.store, self.vcd = store, vcd
        self.checkpointed = checkpointed
        self.builders = OrderedDict()
        self.offsets = {}
        self.pending_pages = []
        self.pending_positions = {}
        self.pending_bytes = 0
        self.positions = OrderedDict()
        self.position_limit = 1024
        self.summary_groups = OrderedDict()
        self.summary_group_limit = 4096
        self.sizes = {}
        self.buffer_bytes = 0
        self.buffer_limit = min(32 * 1024 * 1024, max(PAGE_BYTES, store.memory_bytes // 4))
        self.disordered = False
        self.changes = 0
        self.last_publish = time.monotonic()
        self.last_partial = None
        if resume is None:
            self._initialize(vcd)
        else:
            self.changes = resume['changes']
            self.disordered = resume['checkpoint']['disordered']
            self.last_partial = self.last_publish if self.changes else None

    def _position(self, key):
        """End ordinal and last timestamp of stored or queued pages.

        Active builders carry their own offsets and timestamps. This bounded
        cache is read-through only; page drains persist both fields together.
        """
        pending = self.pending_positions.get(key)
        if pending is not None:
            return pending
        cached = self.positions.get(key)
        if cached is not None:
            self.positions.move_to_end(key)
            return cached
        with self.store.lock:
            row = self.store.db.execute('SELECT written,last_time FROM signals WHERE sid=?', (key,)).fetchone()
        position = (row[0] or 0, row[1]) if row else (0, None)
        self._remember_position(key, position)
        return position

    def _remember_position(self, key, position):
        self.positions[key] = position
        self.positions.move_to_end(key)
        while len(self.positions) > self.position_limit:
            self.positions.popitem(last=False)

    def has_values(self, key):
        return key in self.builders or bool(self._position(key)[0])

    def _initialize(self, vcd):
        from .parsers import build_scope_tree
        from .catalog import SignalCatalog, attach_catalog
        store = self.store
        store.vcd = vcd
        db = store.db
        db.executescript('''
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE pages(id INTEGER PRIMARY KEY,sid TEXT,lo INTEGER,n INTEGER,
                               t0 INTEGER,t1 INTEGER,vmin TEXT,vmax TEXT,data BLOB);
            CREATE TABLE summaries(sid TEXT,lo INTEGER,n INTEGER,level INTEGER,vmin TEXT,vmax TEXT,
                                   PRIMARY KEY(sid,lo,level));
            CREATE INDEX page_lookup ON pages(sid,lo);
            CREATE INDEX page_time ON pages(sid,t0,t1);
            CREATE INDEX page_end ON pages(sid,t1,lo);
        ''')
        db.execute('CREATE ' + ('' if self.checkpointed else 'TEMP ') +
                   'TABLE pending_summaries(sid TEXT,level INTEGER,data TEXT, PRIMARY KEY(sid,level))')
        if not isinstance(vcd.signals, SignalCatalog):
            catalog = SignalCatalog(store, create=True)
            for sid in vcd.signal_order:
                sig = vcd.signals[sid]
                catalog.declare(sig.reference, sig.identifier, sig.size, sig.signal_type,
                                sig.scope, sig.declared_range, deduplicate=False)
            attach_catalog(store, vcd, catalog)
            vcd.scopes = None
        vcd.signals.flush_declarations()
        db.execute('INSERT INTO meta VALUES(?,?)', ('catalog_layout', vcd.signals.layout_signature))
        db.commit()
        vcd._store = store
        vcd._lazy_mode = False
        if vcd.scopes is None:
            vcd.scopes = build_scope_tree(vcd)

    def add(self, key, timestamp, value):
        if self.store.cancel.is_set():
            raise Cancelled('Indexing cancelled')
        sig = self.builders.get(key)
        if sig is None:
            metadata = self.vcd.signals.get(key)
            if metadata is None or not metadata.owns_storage:
                return
            written, previous = self._position(key)
            sig = self.builders[key] = _signal(_definition(metadata))
            sig._memo = None
            self.offsets[key] = written
            self.sizes[key] = 256
            self.buffer_bytes += 256
        else:
            previous = sig._times[-1]
        if previous is not None and timestamp < previous:
            self.disordered = True
        before = len(sig._values)
        if sig._scalar:
            value = value[-1:].lower()
            sig._values.append(ord(value) if value in ('0', '1', 'x', 'z') else 120)
        else:
            sig._append_value(value, intern=False)
        sig._times.append(timestamp)
        cost = 16 + (len(sig._values) - before if type(sig._values) is bytearray else sys.getsizeof(value) + 8)
        self.sizes[key] += cost
        self.buffer_bytes += cost
        self.changes += 1
        if len(sig._times) >= PAGE_EVENTS or self.sizes[key] >= PAGE_BYTES:
            self.flush(key)
        while self.builders and self.buffer_bytes > self.buffer_limit:
            self.flush(next(iter(self.builders)))

    def flush(self, key):
        sig = self.builders.pop(key)
        self.buffer_bytes -= self.sizes.pop(key)
        self._write_page(key, sig, self.offsets.pop(key))

    def add_vcd_batch(self, batch):
        """Append bounded, per-handle event lists in original delta order.

        Resolve cold handles with one query. Short scalar histories can be
        updated together without constructing signal objects; longer histories
        retain builders across batches so input batches don't fragment pages.
        """
        store = self.store
        if store.cancel.is_set():
            raise Cancelled('Indexing cancelled')
        missing = {key for key in batch if key not in self.builders}
        with store.lock:
            rows = dict((row[0], row[1:]) for row in store.db.execute(
                'SELECT sid,definition,written,last_time,short_n,short_data,size,kind FROM signals '
                'WHERE master=sid AND sid IN (' + ','.join('?' for _ in missing) + ')', tuple(missing))) if missing else {}
        # Snapshot queued positions along with the query. Processing an earlier
        # handle can drain the queue and evict its read-through cache entry.
        for key, row in rows.items():
            pending = self.pending_positions.get(key)
            if pending is not None:
                rows[key] = row[:1] + pending + row[3:]
        updates = []
        for key, events in batch.items():
            if store.cancel.is_set():
                raise Cancelled('Indexing cancelled')
            if not events:
                continue
            sig = self.builders.get(key)
            if sig is None:
                row = rows.get(key)
                if row is None and key not in missing:
                    # An earlier handle in this batch can evict this builder.
                    with store.lock:
                        row = store.db.execute('SELECT definition,written,last_time,short_n,short_data,size,kind '
                                               'FROM signals WHERE sid=?', (key,)).fetchone()
                    pending = self.pending_positions.get(key)
                    if row is not None and pending is not None:
                        row = row[:1] + pending + row[3:]
                if row is None:
                    continue
                definition, written, previous, short_n, short_data, width, kind = row
                written = written or 0
            else:
                previous = sig._times[-1]
            for timestamp, _ in events:
                if previous is not None and timestamp < previous:
                    self.disordered = True
                previous = timestamp
            if sig is None and width == 1 and kind not in ('real', 'realtime') and written == short_n and written + len(events) <= SHORT_EVENTS:
                times = array.array('q', (event[0] for event in events)).tobytes()
                values = bytes(_SCALAR_BYTES.get(value[-1:], 120) for _, value in events)
                raw = (b'\x01' + short_data[1:1 + written * 8] + times + short_data[1 + written * 8:] + values
                       if written else b'\x01' + times + values)
                updates.append((written + len(events), raw, written + len(events), previous, key))
                self.changes += len(events)
                continue
            if sig is None:
                sig = _signal(json.loads(definition))
                sig._memo = None
            if key not in self.builders:
                self.builders[key] = sig
                self.offsets[key] = written
                self.sizes[key] = 256
                self.buffer_bytes += 256
            size, added = self.sizes[key], 0
            definition = _definition(sig)
            # Account once per piece, retaining the same page limits. Buffer
            # eviction happens at each page boundary and at the batch's end.
            for timestamp, value in events:
                if sig is None:
                    sig = self.builders[key] = _signal(definition)
                    sig._memo = None
                    self.offsets[key] = written
                    self.sizes[key] = size = 256
                    self.buffer_bytes += 256
                before = len(sig._values)
                if sig._scalar:
                    sig._values.append(_SCALAR_BYTES.get(value[-1:], 120))
                else:
                    sig._append_value(value, intern=False)
                sig._times.append(timestamp)
                cost = 16 + (len(sig._values) - before if type(sig._values) is bytearray else sys.getsizeof(value) + 8)
                size += cost
                added += cost
                if len(sig._times) >= PAGE_EVENTS or size >= PAGE_BYTES:
                    self.sizes[key] = size
                    self.buffer_bytes += added
                    written = self.offsets[key] + len(sig._times)
                    self.flush(key)
                    sig, added = None, 0
                    while self.builders and self.buffer_bytes > self.buffer_limit:
                        self.flush(next(iter(self.builders)))
            if sig is not None:
                self.sizes[key] = size
                self.buffer_bytes += added
            self.changes += len(events)
            while self.builders and self.buffer_bytes > self.buffer_limit:
                self.flush(next(iter(self.builders)))
        with store.lock:
            store.db.executemany('UPDATE signals SET short_n=?,short_data=?,written=?,last_time=? WHERE sid=?', updates)
            for _, _, _, _, key in updates:
                self.positions.pop(key, None)
                store.forget_short(key)

    def add_batch(self, key, timestamps, values):
        """Write one bounded FST batch, preserving every event and delta order.

        The decoder limits batches to PAGE_EVENTS and PAGE_BYTES (except an
        individual wide value), and supplies ordered timestamps and normalized
        scalar bytes. Arrays belong to the batch and are not mutated afterward.
        """
        if self.store.cancel.is_set():
            raise Cancelled('Indexing cancelled')
        if len(timestamps) != len(values) or len(timestamps) > PAGE_EVENTS:
            raise ValueError('Invalid waveform batch size')
        metadata = self.vcd.signals.get(key)
        if not timestamps or metadata is None or not metadata.owns_storage:
            return
        if key in self.builders:
            self.flush(key)
        written, previous = self._position(key)
        if previous is not None and timestamps[0] < previous:
            self.disordered = True
        sig = _signal(_definition(metadata))
        sig._memo = None
        sig._times = timestamps
        if sig._scalar:
            sig._values = values
        else:
            packed = sig._encode_values(values) if sig._packed else None
            sig._packed = packed is not None
            sig._values = packed if sig._packed else values
        self._write_page(key, sig, written)
        self.changes += len(timestamps)

    def add_initial_values(self, timestamp, values):
        """Ingest an FST frame in bounded batches, skipping handles with chains.

        Scalar initials need neither signal objects nor a separate compressed
        page. Wider initials use the identical page encoding as ordinary writes.
        """
        self.publish(True)  # make chain-written state available to bulk lookups
        batch, size = {}, 0
        for key, value in values:
            if self.store.cancel.is_set():
                raise Cancelled('Indexing cancelled')
            if key not in batch:
                batch[key] = value
                size += 128 + 4 * (len(key) + len(value))
            if len(batch) >= 512 or size >= PAGE_BYTES:
                self._initial_batch(timestamp, batch)
                batch, size = {}, 0
                self.publish()
        if batch:
            self._initial_batch(timestamp, batch)
        self.publish()

    def _initial_batch(self, timestamp, batch):
        store = self.store
        with store.lock:
            rows = store.db.execute('SELECT sid,size,kind,definition FROM signals WHERE master=sid '
                                    'AND written IS NULL AND last_time IS NULL AND sid IN (' +
                                    ','.join('?' for _ in batch) + ')', tuple(batch)).fetchall()
        updates, large = [], []
        for key, width, kind, definition in rows:
            if store.cancel.is_set():
                raise Cancelled('Indexing cancelled')
            # A repeated wide initial may still be buffered from a prior batch.
            if key in self.builders or key in self.pending_positions:
                continue
            value = batch[key]
            if width == 1 and kind not in ('real', 'realtime'):
                scalar = value[-1:].lower()
                raw = b'\x01' + struct.pack('=q', timestamp) + bytes([ord(scalar) if scalar in ('0', '1', 'x', 'z') else 120])
            else:
                sig = _signal(json.loads(definition))
                sig._memo = None
                sig._times.append(timestamp)
                sig._append_value(value, intern=False)
                raw = _encode_page(sig)
            if len(raw) <= SHORT_BYTES:
                updates.append((raw, timestamp, key))
            else:
                large.append((key, value))
        with store.lock:
            store.db.executemany('UPDATE signals SET short_n=1,short_data=?,written=1,last_time=? WHERE sid=?', updates)
            for _, _, key in updates:
                self.positions.pop(key, None)
                store.forget_short(key)
        self.changes += len(updates)
        for key, value in large:
            self.add(key, timestamp, value)

    def _write_page(self, key, sig, lo=None):
        """Queue immutable pages with bounded memory; ordinals include the queue."""
        if lo is None:
            lo = self._position(key)[0]
        raw = _encode_page(sig)
        self.pending_pages.append((key, sig, lo, raw))
        # Include both the encoded payload and the retained signal arrays.
        size = len(raw) + sys.getsizeof(sig._times) + sys.getsizeof(sig._values) + 256
        if type(sig._values) is list:
            size += sum(sys.getsizeof(value) for value in sig._values)
        if key not in self.pending_positions:
            size += 192  # dictionary entry, position tuple and its integers
        self.pending_bytes += size
        self.pending_positions[key] = (lo + len(sig._times), sig._times[-1])
        if self.pending_bytes >= WRITE_BATCH_BYTES or len(self.pending_pages) >= WRITE_BATCH_PAGES:
            self._drain_pages()

    def _drain_pages(self):
        if not self.pending_pages:
            return
        store = self.store
        pages, summaries, updates = [], [], {}
        keys = list(dict.fromkeys(key for key, sig, lo, raw in self.pending_pages
                                 if lo and lo + len(sig._times) <= SHORT_EVENTS and len(raw) <= SHORT_BYTES))
        with store.lock:
            shorts = {key: (n, raw) for key, n, raw in store.db.execute(
                'SELECT sid,short_n,short_data FROM signals WHERE sid IN (' + ','.join('?' for _ in keys) + ')', keys)} if keys else {}
        for key, sig, lo, page_data in self.pending_pages:
            if store.cancel.is_set():
                raise Cancelled('Indexing cancelled')
            raw, n = page_data, len(sig._times)
            if lo + n <= SHORT_EVENTS and len(raw) <= SHORT_BYTES:
                if lo:
                    old_n, old_raw = shorts[key]
                    if old_n == lo:
                        old = _decode_page(_definition(sig), old_raw, lo)
                        merged = _signal(_definition(sig))
                        merged._memo = None
                        merged._times = old._times + sig._times
                        if merged._scalar:
                            merged._values = old._values + sig._values
                        else:
                            for part in (old, sig):
                                for i in range(len(part._times)):
                                    merged._append_value(part.value_str(i), intern=False)
                        raw = _encode_page(merged)
                    else:
                        raw = None
                if raw is not None and len(raw) <= SHORT_BYTES:
                    shorts[key] = updates[key] = (lo + n, raw)
                    continue
            # An inline prefix stays in place when a signal grows. Every later
            # page starts at its reserved ordinal, including same-tick events.
            minimum = maximum = None
            if sig._scalar:
                zero, one = 48 in sig._values, 49 in sig._values
                minimum = 0 if zero else (1 if one else None)
                maximum = 1 if one else (0 if zero else None)
            else:
                for i in range(n):
                    value = numeric_value(sig.value_str(i), sig.signal_type)
                    if value is not None:
                        minimum = value if minimum is None else min(minimum, value)
                        maximum = value if maximum is None else max(maximum, value)
            pages.append((key, lo, n, min(sig._times), max(sig._times),
                          _encode_number(minimum), _encode_number(maximum), zlib.compress(page_data, 1)))
            summaries.append((key, lo, n, 0, minimum, maximum))
        with store.lock:
            store.db.executemany('UPDATE signals SET short_n=?,short_data=?,written=?,last_time=? WHERE sid=?',
                                 ((n, raw, *self.pending_positions[key], key) for key, (n, raw) in updates.items()))
            for key in updates:
                store.forget_short(key)
            store.db.executemany('INSERT INTO pages(sid,lo,n,t0,t1,vmin,vmax,data) VALUES(?,?,?,?,?,?,?,?)', pages)
            # Fetch spilled leaf groups together. Removing them now means a
            # later group spill/reload in this batch sees the new state.
            keys = list(dict.fromkeys(row[0] for row in summaries if (row[0], 0) not in self.summary_groups))
            pending = dict(store.db.execute('SELECT sid,data FROM pending_summaries WHERE level=0 AND sid IN (' +
                                           ','.join('?' for _ in keys) + ')', keys)) if keys else {}
            store.db.executemany('DELETE FROM pending_summaries WHERE sid=? AND level=0', ((key,) for key in pending))
            pending = {key: pending.get(key) for key in keys}
            rows = []
            for summary in summaries:
                self.add_summary(*summary, rows=rows, pending=pending)
            store.db.executemany('INSERT INTO summaries VALUES(?,?,?,?,?,?)', rows)
            store.db.executemany('UPDATE signals SET written=?,last_time=? WHERE sid=?',
                                 ((n, timestamp, key) for key, (n, timestamp) in self.pending_positions.items()
                                  if key not in updates))
        for key, position in self.pending_positions.items():
            self._remember_position(key, position)
        self.pending_pages.clear()
        self.pending_positions.clear()
        self.pending_bytes = 0

    def add_summary(self, key, lo, n, level, minimum, maximum, *, rows, pending):
        rows.append((key, lo, n, level, _encode_number(minimum), _encode_number(maximum)))
        group_key = key, level
        group = self.summary_groups.get(group_key)
        if group is None:
            if level == 0 and key in pending:
                data = pending.pop(key)
            else:
                row = self.store.db.execute('SELECT data FROM pending_summaries WHERE sid=? AND level=?',
                                            group_key).fetchone()
                data = row[0] if row else None
                if row is not None:
                    self.store.db.execute('DELETE FROM pending_summaries WHERE sid=? AND level=?', group_key)
            if data is not None:
                group = json.loads(data)
                group[2], group[3] = _decode_number(group[2]), _decode_number(group[3])
            else:
                group = [lo, 0, None, None, 0]
            self.summary_groups[group_key] = group
        self.summary_groups.move_to_end(group_key)
        group[1] += n
        if minimum is not None:
            group[2] = minimum if group[2] is None else min(group[2], minimum)
            group[3] = maximum if group[3] is None else max(group[3], maximum)
        group[4] += 1
        if group[4] == 16:
            del self.summary_groups[group_key]
            self.add_summary(key, group[0], group[1], level + 1, group[2], group[3], rows=rows, pending=pending)
        while len(self.summary_groups) > self.summary_group_limit:
            (sid, level), group = self.summary_groups.popitem(last=False)
            data = [group[0], group[1], _encode_number(group[2]), _encode_number(group[3]), group[4]]
            self.store.db.execute('INSERT OR REPLACE INTO pending_summaries VALUES(?,?,?)',
                                  (sid, level, json.dumps(data)))

    def publish(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_publish < PUBLISH_INTERVAL:
            return
        # Publish the first partial histories promptly. Later updates expose
        # completed/evicted pages every quarter second; partial pages get up to
        # a second to accumulate events. A forced publish always drains all.
        if force or self.last_partial is None or now - self.last_partial >= PARTIAL_INTERVAL:
            for key in list(self.builders):
                self.flush(key)
            # Empty frame boundaries must not delay the first actual values.
            if self.changes:
                self.last_partial = now
        self._drain_pages()
        with self.store.lock:
            if not self.disordered:
                delta = self.store.db.execute('SELECT COALESCE(SUM((written-n)*aliases),0) FROM signals '
                                              'WHERE written IS NOT NULL AND n!=written').fetchone()[0]
                self.store.total_changes += delta
                self.store.db.execute('UPDATE signals SET n=written WHERE written IS NOT NULL AND n!=written')
                self.store.counts.cache.clear()
            # Progressive readers share this connection. Durable commits happen
            # only at decoder boundaries with matching VCD/FST parser state.
            if not self.checkpointed:
                self.store.db.commit()
            self.store.generation += 1
        self.vcd._load_changes = self.changes
        self.last_publish = time.monotonic()

    def reorder(self):
        """Externally sort out-of-order VCD events and appended FST blackouts."""
        self.publish(True)
        db = self.store.db
        if self.checkpointed and not db.in_transaction:
            # A disordered prefix need not update any readable counts. Keep
            # the sort's DDL in the transaction even when publish did no DML.
            db.execute('BEGIN')
        db.execute('CREATE TABLE reordered(sid TEXT,t INTEGER,ordinal INTEGER,value TEXT)')
        for key, n in self.vcd.signals.rows('sid,written', 'written IS NOT NULL'):
            index = 0
            while index < n:
                page, lo = self.store.page_at(key, index)
                db.executemany('INSERT INTO reordered VALUES(?,?,?,?)',
                               ((key, t, lo + i, page.value_str(i)) for i, t in enumerate(page._times)))
                index = lo + len(page._times)
        if not self.checkpointed:
            db.commit()
        db.execute('DELETE FROM pages')
        db.execute('UPDATE signals SET short_n=0,short_data=NULL WHERE short_n!=0')
        db.execute('DELETE FROM summaries')
        self.summary_groups.clear()
        db.execute('DELETE FROM pending_summaries')
        self.store.pages.clear()
        self.store.cache_bytes = 0
        self.positions.clear()
        db.execute('UPDATE signals SET written=NULL,last_time=NULL')
        self.disordered = False
        self.changes = 0
        cursor = db.execute('SELECT sid,t,value FROM reordered ORDER BY sid,t,ordinal')
        for key, t, value in cursor:
            self.add(key, t, value)
        self.publish(True)
        db.execute('DROP TABLE reordered')

    def _metadata(self, source, signature, complete):
        return {'version': _source_version(source), 'signature': signature, 'complete': complete,
                'timescale': self.vcd.timescale, 'timescale_number': self.vcd.timescale_number,
                'timescale_unit': self.vcd.timescale_unit, 'date': self.vcd.date,
                'simulator_version': self.vcd.version, 'max_time': self.vcd.max_time,
                'min_time': self.vcd.min_time, 'total_changes': self.store.total_changes,
                'changes': self.changes}

    def checkpoint(self, source, signature, source_state, offset, state):
        """Commit a fully drained prefix and its exact next-line decoder state."""
        checkpoint = {'version': CHECKPOINT_VERSION, 'source': source_state, 'offset': offset,
                      'state': state.snapshot(), 'monotonic': state.monotonic, 'disordered': self.disordered}
        self._commit_checkpoint(source, signature, source_state, checkpoint)

    def checkpoint_fst(self, source, signature, source_state, offset, state):
        """Commit pages and an FST decoder position in the same transaction."""
        checkpoint = {'version': FST_CHECKPOINT_VERSION, 'format': 'fst', 'source': source_state,
                      'offset': offset, 'state': state, 'disordered': self.disordered}
        self._commit_checkpoint(source, signature, source_state, checkpoint)

    def _commit_checkpoint(self, source, signature, source_state, checkpoint):
        from .analysis_cache import source_signature
        if self.store.cancel.is_set():
            raise Cancelled('Indexing cancelled')
        self.publish(True)
        if source_signature(source) != source_state:
            raise ValueError('Trace changed during indexing; reopen it after the writer finishes')
        meta = self._metadata(source, signature, False)
        meta['checkpoint'] = checkpoint
        with self.store.lock:
            self.store.db.executemany('INSERT OR REPLACE INTO pending_summaries VALUES(?,?,?)',
                                     ((sid, level, json.dumps([g[0], g[1], _encode_number(g[2]),
                                                              _encode_number(g[3]), g[4]]))
                                      for (sid, level), g in self.summary_groups.items()))
            self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('trace', json.dumps(meta)))
            self.store.db.commit()
            self.summary_groups.clear()
        if self.store.cache_manager:
            self.store.cache_manager.trim()

    def finish(self, source, signature, *, source_state=None):
        if self.store.cancel.is_set():
            raise Cancelled('Indexing cancelled')
        self.publish(True)
        if self.disordered:
            with self.store.lock:
                self.reorder()
        from .analysis_cache import source_signature
        if (_signature(source) != signature or
                (source_state is not None and source_signature(source) != source_state)):
            raise ValueError('Trace changed during indexing; reopen it after the writer finishes')
        meta = self._metadata(source, signature, True)
        with self.store.lock:
            self.store.db.execute('DROP TABLE pending_summaries')
            self.store.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('trace', json.dumps(meta)))
            self.store.db.commit()
        self.vcd._extent_known = True
        self.vcd._load_progress = 100
        clear_pack_memos()


def create_derived_store(parent, signal):
    """A derived trace shares its parent's lifetime but is never persisted."""
    from .parsers import VCDFile
    temporary = tempfile.TemporaryDirectory(prefix='alwavelets-derived-')
    store = TraceStore(os.path.join(temporary.name, 'trace.sqlite'),
                       min(parent._store.memory_bytes, 8 * 1024 * 1024))
    store.temporary = temporary
    trace = VCDFile()
    trace.signals[signal.identifier] = signal
    trace.signal_order = [signal.identifier]
    writer = PageWriter(store, trace)
    if not hasattr(parent, '_derived_stores'):
        parent._derived_stores = []
    parent._derived_stores.append(store)
    return trace.signals[signal.identifier], writer


def _restore(store, source, *, allow_partial=False):
    from .parsers import VCDFile, build_scope_tree
    from .analysis_cache import source_signature
    source_state = source_signature(source)
    row = store.db.execute('SELECT value FROM meta WHERE key="trace"').fetchone()
    if row is None:
        return None
    meta = json.loads(row[0])
    if meta['version'] != _source_version(source) or meta['signature'] != _signature(source):
        return None
    if not meta['complete']:
        if not allow_partial:
            return None
        checkpoint = meta['checkpoint']
        state = checkpoint['state']
        if (checkpoint['source'] != source_state
                or type(checkpoint['offset']) is not int or not 0 <= checkpoint['offset'] <= source_state[3]
                or type(checkpoint['disordered']) is not bool):
            return None
        from .fst import FSTReader, valid_checkpoint
        if checkpoint.get('format', 'vcd') == 'fst':
            if checkpoint['version'] != FST_CHECKPOINT_VERSION or not valid_checkpoint(state) or not FSTReader.is_fst(source):
                return None
        elif checkpoint.get('format', 'vcd') == 'vcd':
            if (checkpoint['version'] != CHECKPOINT_VERSION or not isinstance(state, list) or len(state) != 4
                    or type(state[0]) is not int or type(state[3]) is not int
                    or type(state[1]) is not bool or type(state[2]) is not bool
                    or type(checkpoint['monotonic']) is not bool or FSTReader.is_fst(source)):
                return None
        else:
            return None
        store.db.execute('SELECT sid,level,data FROM pending_summaries LIMIT 0')
    vcd = VCDFile()
    from .catalog import SignalCatalog, attach_catalog
    attach_catalog(store, vcd, SignalCatalog(store))
    vcd._filename = source
    vcd._analysis_source = source_state
    for field in ('timescale', 'timescale_number', 'timescale_unit', 'date', 'max_time', 'min_time'):
        setattr(vcd, field, meta[field])
    vcd.version = meta['simulator_version']
    store.total_changes = meta['total_changes']
    vcd._load_changes = meta['changes']
    vcd.scopes = build_scope_tree(vcd)
    if not meta['complete']:
        vcd._checkpoint = meta
        vcd._resume_offset = checkpoint['offset']
        vcd._load_progress = min(99, checkpoint['offset'] * 100 // max(1, source_state[3]))
        vcd._extent_known = False
    return vcd


def _open_partial(source, memory, manager, cancel=None, stage_cb=None):
    """Acquire or create a checkpoint, falling back if its cache is unavailable."""
    from .index_cache import discard_partial
    from .streaming import OperationCancelled
    path = partial_index_path(source)
    store = lease = vcd = None
    try:
        lease = manager.acquire(path)
        if os.path.exists(path):
            try:
                store = TraceStore(path, memory)
                store.stage_cb = stage_cb
                if cancel is not None:
                    store.cancel = cancel
                vcd = _restore(store, source, allow_partial=True)
            except OperationCancelled:
                raise
            except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
                vcd = None
            if vcd is None:
                if store is not None:
                    # Closing a rejected probe must not cancel the replacement
                    # load, which shares the caller's cancellation event.
                    store.cancel = threading.Event()
                    store.close()
                    store = None
                discard_partial(path)
        if store is None:
            store = TraceStore(path, memory)
            if cancel is not None:
                store.cancel = cancel
        store.index_lease = lease
        store.cache_manager = manager
        return store, vcd
    except (OSError, sqlite3.Error):
        if store is not None:
            store.cancel = threading.Event()
            store.close()
        if lease is not None:
            lease.close()
        return None, None
    except BaseException:
        if store is not None:
            store.close()
        if lease is not None:
            lease.close()
        raise


def open_trace(source, memory_mib=256, background=False, use_cache=True, progress_cb=None, cancel=None, stage_cb=None):
    """Open VCD/FST into paged storage; all declarations remain available.

    stage_cb(stage, done, total, unit) reports preparation stages and counts;
    unavailable counts are None. It runs on the thread doing that work.
    progress_cb retains its waveform-byte progress semantics.
    """
    from .parsers import VCDFile
    from . import cache
    from .index_cache import WaveformCache, disk_full
    manager = WaveformCache()
    use_cache = use_cache and cache.ENABLED and manager.quota != 0
    source = os.path.abspath(source)
    if memory_mib <= 0:
        raise ValueError('memory_mib must be positive')
    from .streaming import check_cancel, OperationCancelled
    from .loading import report_stage
    cancel = cancel if cancel is not None else threading.Event()
    check_cancel(cancel)
    report_stage(stage_cb, 'Checking saved index')
    check_cancel(cancel)
    memory = int(memory_mib * 1024 * 1024)
    target = index_path(source)
    if use_cache:
        manager.trim(exclude=(target, partial_index_path(source), cache.cache_path(source)))
    if use_cache and os.path.isfile(target):
        store = lease = None
        try:
            lease = manager.acquire(target, shared=True)
            store = TraceStore(target, memory)
            store.stage_cb = stage_cb
            store.cancel = cancel
            store.index_lease = lease
            vcd = _restore(store, source)
            if vcd is not None:
                store.cache_manager = manager
                manager.touch(target)
                return vcd
        except OperationCancelled:
            if store is not None:
                store.close()
            elif lease is not None:
                lease.close()
            raise
        except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            pass
        except BaseException:
            if store is not None:
                store.close()
            elif lease is not None:
                lease.close()
            raise
        if store is not None:
            store.cancel = threading.Event()
            store.close()
        elif lease is not None:
            lease.close()
    signature = _signature(source)
    from .analysis_cache import source_signature
    source_state = source_signature(source)
    from .fst import FSTReader
    is_fst = FSTReader.is_fst(source)
    preparation = ExitStack()
    store = temporary = lease = vcd = None
    resume = None
    state = ValueState()
    try:
        if use_cache:
            store, vcd = _open_partial(source, memory, manager, cancel, stage_cb)
            use_cache = store is not None
            if store is not None:
                lease, path = store.index_lease, store.path
        if vcd is not None and not hasattr(vcd, '_checkpoint'):
            # A crash can occur after the final commit but before rename.
            try:
                os.replace(path, target)
                store.path = target
            except OSError:
                pass
            manager.touch(store.path)
            manager.share(store.index_lease)
            store.cancel = cancel
            return vcd
        if not use_cache:
            directory = cache.configured_directory()
            try:
                if directory:
                    os.makedirs(directory, exist_ok=True)
                temporary = tempfile.TemporaryDirectory(prefix='alwavelets-index-', dir=directory)
            except OSError:
                temporary = tempfile.TemporaryDirectory(prefix='alwavelets-index-')
        if temporary is not None:
            path = os.path.join(temporary.name, 'trace.sqlite')
        if store is None:
            store = TraceStore(path, memory)
        store.index_lease = lease
        store.cache_manager = manager if use_cache else None
        store.cancel = cancel
        store.stage_cb = stage_cb
        store.temporary = temporary
        if vcd is not None:
            resume = vcd._checkpoint
            offset = resume['checkpoint']['offset']
            if is_fst:
                reader = FSTReader(source)
                preparation.enter_context(reader.prepare(cancel=cancel, memory_mib=memory_mib,
                                                         catalog=vcd.signals, resume=vcd, stage_cb=stage_cb))
            else:
                state = ValueState(*resume['checkpoint']['state'])
                state.monotonic = resume['checkpoint']['monotonic']
        else:
            from .catalog import SignalCatalog
            catalog = SignalCatalog(store, create=True)
            if is_fst:
                reader = FSTReader(source)
                vcd = preparation.enter_context(reader.prepare(cancel=cancel, memory_mib=memory_mib,
                                                               catalog=catalog, stage_cb=stage_cb))
                offset = 0
            else:
                vcd, offset = VCDFile.parse_header(source, metadata_only=True, catalog=catalog,
                                                  cancel=cancel, stage_cb=stage_cb)
        if not vcd.signal_order:
            raise ValueError('Trace has no signal declarations')
        writer = PageWriter(store, vcd, checkpointed=lease is not None, resume=resume)
        report_stage(stage_cb, 'Saving preparation')
        check_cancel(cancel)
        if writer.checkpointed and resume is None:
            if is_fst:
                from .fst import checkpoint_state
                writer.checkpoint_fst(source, signature, source_state, 0, checkpoint_state())
            else:
                writer.checkpoint(source, signature, source_state, offset, state)
        vcd._loading = True
        vcd._extent_known = False
    except BaseException as exc:
        message = _storage_error(exc, store) if disk_full(exc) else None
        preparation.close()
        if store is not None:
            store.close()
        elif temporary is not None:
            temporary.cleanup()
        if lease is not None:
            lease.close()
        if message is not None:
            raise ValueError(message) from exc
        raise

    def build():
        try:
            with preparation:
                report_stage(stage_cb, 'Indexing values')
                if is_fst:
                    checkpoint_time, checkpoint_work = time.monotonic(), 0

                    def progress(done, total):
                        vcd._load_progress = max(vcd._load_progress, min(99, done * 100 // max(1, total)))
                        if progress_cb:
                            progress_cb(min(signature[0], done * signature[0] // max(1, total)), signature[0])

                    def checkpoint_fst(state, done, total, work, force):
                        nonlocal checkpoint_time, checkpoint_work
                        checkpoint_work += work
                        progress(done, total)
                        if force or checkpoint_work >= CHECKPOINT_BYTES or time.monotonic() - checkpoint_time >= CHECKPOINT_INTERVAL:
                            position = min(signature[0], done * signature[0] // max(1, total))
                            writer.checkpoint_fst(source, signature, source_state, position, state)
                            checkpoint_time, checkpoint_work = time.monotonic(), 0

                    if resume is not None and progress_cb:
                        progress_cb(offset, signature[0])
                    reader.read_values(vcd, writer, progress,
                                       checkpoint=checkpoint_fst if writer.checkpointed else None,
                                       resume=resume['checkpoint']['state'] if resume else None)
                else:
                    ingest_vcd()
            writer.finish(source, signature, source_state=source_state if writer.checkpointed else None)
            if use_cache:
                try:
                    os.replace(path, target)
                    store.path = target
                except OSError:
                    pass  # the complete temporary index remains usable
                manager.touch(store.path)
                manager.share(store.index_lease)
                manager.trim()
        except Exception as exc:
            vcd._load_error = _storage_error(exc, store)
        finally:
            vcd._loading = False

    def ingest_vcd():
        pos = offset
        checkpoint_pos, checkpoint_time = pos, time.monotonic()
        batch, batch_bytes, batch_events = {}, 0, 0

        def append(key, timestamp, value):
            nonlocal batch, batch_bytes, batch_events
            events = batch.get(key)
            if events is None:
                events = batch[key] = []
                batch_bytes += 160 + 4 * len(key)
            events.append((timestamp, value))
            batch_bytes += 128 + 4 * len(value)
            batch_events += 1
            if len(batch) >= VCD_BATCH_KEYS or batch_events >= PAGE_EVENTS or batch_bytes >= VCD_BATCH_BYTES:
                writer.add_vcd_batch(batch)
                batch, batch_bytes, batch_events = {}, 0, 0
                vcd.max_time = state.max_time
                writer.publish()

        with open(source, 'rb', buffering=1 << 20) as f:
            f.seek(offset)
            if resume is not None and progress_cb:
                progress_cb(offset, signature[0])
            for raw in f:
                if store.cancel.is_set():
                    raise Cancelled('Indexing cancelled')
                pos += len(raw)
                event = state.feed(raw)
                if event is not None:
                    bid, value = event
                    if bid is None:
                        # A blackout applies once per storage owner. Traverse
                        # declarations in bounded batches, including hidden nets.
                        for key, in vcd.signals.rows('sid', 'master=sid'):
                            append(key, state.time, value)
                    else:
                        append(bid.decode('ascii', 'ignore'), state.time, value)
                if pos % (1 << 16) < len(raw):
                    vcd.max_time = state.max_time
                    vcd._load_progress = min(99, pos * 100 // max(1, signature[0]))
                    if batch:
                        writer.add_vcd_batch(batch)
                        batch, batch_bytes, batch_events = {}, 0, 0
                    writer.publish()
                    if writer.checkpointed and (pos - checkpoint_pos >= CHECKPOINT_BYTES or
                                               time.monotonic() - checkpoint_time >= CHECKPOINT_INTERVAL):
                        writer.checkpoint(source, signature, source_state, pos, state)
                        checkpoint_pos, checkpoint_time = pos, time.monotonic()
                    if progress_cb:
                        progress_cb(pos, signature[0])
        if batch:
            writer.add_vcd_batch(batch)
        vcd.max_time = state.max_time
        if writer.checkpointed:
            # Sorting an out-of-order trace may itself take time. Its input
            # remains recoverable if cancellation interrupts the final sort.
            writer.checkpoint(source, signature, source_state, pos, state)
    if background:
        store.thread = threading.Thread(target=build, daemon=True, name='wavelet-index')
        try:
            store.thread.start()
        except BaseException:
            store.thread = None
            preparation.close()
            store.close()
            raise
    else:
        build()
        if vcd._load_error:
            store.close()
            raise ValueError(vcd._load_error)
    return vcd
