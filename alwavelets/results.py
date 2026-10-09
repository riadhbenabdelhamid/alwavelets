"""Temporary, indexed decoder results with bounded traversal and UI rendering."""
from collections.abc import Sequence
import marshal
import os
import operator
import sqlite3
import tempfile
import threading

from .storage import SequenceView
from .streaming import check_cancel


class PagedResults(Sequence):
    """Read-only snapshots of frames, transactions, or instruction dictionaries.

    The process-private database is never reopened from a user-supplied path.
    Only primitive field values are marshalled; each read returns a fresh object.
    SQLite's page cache is bounded, and Python retains one record at a time.
    """
    def __init__(self, item_type, owner=None):
        self.item_type = item_type
        self.fields = None if item_type is dict else item_type.__slots__
        self._temporary = tempfile.TemporaryDirectory(prefix='alwavelets-results-')
        self.path = os.path.join(self._temporary.name, 'results.sqlite')
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute('PRAGMA cache_size=-1024')
        self._db.execute('PRAGMA temp_store=FILE')
        self._db.execute('CREATE TABLE results(seq INTEGER PRIMARY KEY,start INTEGER,end INTEGER,data BLOB)')
        self._db.execute('CREATE INDEX result_time ON results(start,seq)')
        self._count = self._max_duration = 0
        self._owner = owner
        if owner is not None:
            if not hasattr(owner, '_result_stores'):
                owner._result_stores = set()
            owner._result_stores.add(self)

    @classmethod
    def collect(cls, items, item_type, *, owner=None, cancel=None, progress=None, end=None):
        results = cls(item_type, owner)
        iterator = iter(items)
        last_stop = 0
        try:
            check_cancel(cancel)
            for item in iterator:
                check_cancel(cancel)
                start = item['time'] if item_type is dict else item.t_start
                stop = start if item_type is dict else item.t_end
                last_stop = stop
                values = item if results.fields is None else tuple(getattr(item, f) for f in results.fields)
                results._db.execute('INSERT INTO results VALUES(?,?,?,?)',
                                    (results._count, start, stop, marshal.dumps(values)))
                results._count += 1
                results._max_duration = max(results._max_duration, stop - start)
                if results._count % 4096 == 0:
                    results._db.commit()
                    if progress is not None:
                        progress(start, end if end is not None else stop)
            results._db.commit()
            if progress is not None:
                total = last_stop if end is None else end
                progress(total, total)
            check_cancel(cancel)
            return results
        except BaseException:
            results.close()
            raise
        finally:
            close = getattr(iterator, 'close', None)
            if close is not None:
                close()

    def __len__(self):
        return self._count

    def _restore(self, data):
        values = marshal.loads(data)
        if self.fields is None:
            return values
        item = self.item_type.__new__(self.item_type)
        for name, value in zip(self.fields, values):
            setattr(item, name, value)
        return item

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self, range(*index.indices(len(self))))
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        with self._lock:
            if self._closed.is_set():
                raise ValueError('Decoded results are closed')
            row = self._db.execute('SELECT data FROM results WHERE seq=?', (index,)).fetchone()
        return self._restore(row[0])

    def _rows(self, query, args):
        with self._lock:
            if self._closed.is_set():
                return
            cursor = self._db.execute(query, args)
        try:
            while True:
                with self._lock:
                    if self._closed.is_set():
                        return
                    row = cursor.fetchone()
                if row is None:
                    return
                yield self._restore(row[0])
        finally:
            with self._lock:
                if not self._closed.is_set():
                    cursor.close()

    def iter_range(self, start, end):
        return self._rows('SELECT data FROM results WHERE seq>=? AND seq<? ORDER BY seq', (start, end))

    def __iter__(self):
        return self.iter_range(0, len(self))

    def iter_window(self, start, end):
        # A duration bound permits an indexed start-time lookup while retaining
        # overlapping transactions and exact same-tick ordering.
        if end < start:
            return iter(())
        low = max(-(1 << 63), start - self._max_duration)
        return self._rows('SELECT data FROM results WHERE start>=? AND start<=? AND end>=? '
                          'ORDER BY start,seq', (low, end, start))

    def overlay(self, start, end, width, cancel=None):
        """Return only the painted terminal cells, not a list of all frames."""
        if width <= 0 or end <= start:
            return []
        chars, painted = [' '] * width, bytearray(width)
        check_cancel(cancel)
        for i, frame in enumerate(self.iter_window(start, end)):
            if i % 1024 == 0:
                check_cancel(cancel)
            left = max(0, (frame.t_start - start) * width // (end - start))
            right = min(width, (frame.t_end - start) * width // (end - start))
            if right <= left:
                continue
            n = right - left
            chars[left:right] = frame.label[:n].center(n)
            painted[left:right] = b'\1' * n
        runs = []
        left = 0
        while left < width:
            if not painted[left]:
                left += 1
                continue
            right = left + 1
            while right < width and painted[right]:
                right += 1
            runs.append((left, ''.join(chars[left:right])))
            left = right
        return runs

    def close(self):
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._db.close()
            self._temporary.cleanup()
            if self._owner is not None:
                self._owner._result_stores.discard(self)
                self._owner = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def close_results(results):
    close = getattr(results, 'close', None)
    if close is not None:
        close()
