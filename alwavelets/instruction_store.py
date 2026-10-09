"""Indexed instruction fields and disk-resident selections of record IDs."""
from contextlib import contextmanager
import marshal
import operator
from pathlib import Path
import re
import sqlite3
import threading

from .results import PagedResults
from .storage import SequenceView
from .streaming import check_cancel


def number_key(value):
    """Lexically ordered arbitrary-width integers, including unsigned addresses."""
    if value is None:
        return None
    value = operator.index(value)
    magnitude = abs(value)
    raw = magnitude.to_bytes(max(1, (magnitude.bit_length() + 7) // 8), 'big')
    if value < 0:
        return b'\x7f' + (0xffffffff - len(raw)).to_bytes(4, 'big') + bytes(255 - b for b in raw)
    return b'\x80' + len(raw).to_bytes(4, 'big') + raw


def key_number(raw):
    if raw is None:
        return None
    return (-int.from_bytes(bytes(255 - b for b in raw[5:]), 'big') if raw[0] == 127
            else int.from_bytes(raw[5:], 'big'))


class InstructionResults(PagedResults):
    def __init__(self, item_type=dict, owner=None):
        super().__init__(item_type, owner)
        for name, kind in [('pc', 'BLOB'), ('hart', 'BLOB'), ('thread', 'BLOB'), ('mnemonic', 'TEXT'),
                           ('rd', 'BLOB'), ('mem', 'BLOB'), ('csr', 'BLOB'), ('is_store', 'INTEGER'),
                           ('is_csr', 'INTEGER'), ('stall', 'INTEGER')]:
            self._db.execute('ALTER TABLE results ADD COLUMN ' + name + ' ' + kind)
        self._configure_queries()

    def _configure_queries(self):
        self._db.create_function('matches', 2, lambda pattern, text: bool(re.search(pattern, text or '', re.I)))
        self._selection_id = 0
        self.cache_hit = False

    @classmethod
    def open_cached(cls, path, identity, owner=None):
        """Open an application-owned, completed index; selections stay temporary."""
        import json
        result = cls.__new__(cls)
        result.path = str(path)
        result.item_type, result.fields = dict, None
        result._lock, result._closed = threading.RLock(), threading.Event()
        result._temporary, result._owner = None, None
        result._db = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro&immutable=1', check_same_thread=False)
        try:
            result._db.execute('PRAGMA cache_size=-1024')
            result._db.execute('PRAGMA temp_store=FILE')
            meta = dict(result._db.execute('SELECT key,value FROM analysis_meta'))
            if json.loads(meta['identity']) != identity or meta['complete'] != '1':
                raise ValueError('Stale instruction index')
            result._count, result._max_duration = int(meta['count']), 0
            if result._count < 0:
                raise ValueError('Invalid instruction count')
            indexes = {row[1] for row in result._db.execute('PRAGMA index_list(results)')}
            if not {'result_time', *("instruction_" + name for name in ('pc', 'hart', 'thread', 'mnemonic', 'rd', 'reg', 'mem', 'csr'))} <= indexes:
                raise ValueError('Incomplete instruction indexes')
            last = result._db.execute('SELECT seq FROM results ORDER BY seq DESC LIMIT 1').fetchone()
            if (last[0] + 1 if last else 0) != result._count:
                raise ValueError('Incomplete instruction records')
            result._configure_queries()
            if result._count:
                for entry in (result[0], result[-1]):
                    if not isinstance(entry, dict) or not isinstance(entry['decoded'], dict):
                        raise ValueError('Invalid instruction record')
            result.cache_hit = True
            if owner is not None:
                if not hasattr(owner, '_result_stores'):
                    owner._result_stores = set()
                owner._result_stores.add(result)
                result._owner = owner
            return result
        except BaseException:
            result.close()
            raise

    def _append(self, item):
        decoded = item.get('decoded', {})
        t = item['time']
        self._db.execute('INSERT INTO results VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         (self._count, t, t, marshal.dumps(item), number_key(item.get('pc')),
                          number_key(item.get('hart_id', 0)), number_key(item.get('thread_id', 0)),
                          decoded.get('mnemonic', ''), number_key(item.get('rd_addr')),
                          number_key(item.get('mem_addr')), number_key(item.get('csr_addr')),
                          bool(decoded.get('is_store')), bool(decoded.get('is_csr')), bool(item.get('stall'))))
        self._count += 1
        return t, t

    @contextmanager
    def sql_operation(self, cancel=None):
        """SQLite sorts/index builds also observe cancellation, not just Python loops."""
        with self._lock:
            check_cancel(cancel)
            if self._closed.is_set():
                raise ValueError('Instruction results are closed')
            self._db.set_progress_handler(lambda: int(cancel is not None and cancel.is_set()), 4096)
            try:
                yield self._db
            except sqlite3.OperationalError:
                check_cancel(cancel)
                raise
            finally:
                self._db.set_progress_handler(None, 0)

    def _finish(self, cancel, progress):
        # Build indexes once, after bulk ingestion. Sorts spill through SQLite.
        if progress:
            progress(0, 0)
        with self.sql_operation(cancel) as db:
            for name, columns in [('pc', 'pc,seq'), ('hart', 'hart,start,seq'),
                                  ('thread', 'thread,start,seq'), ('mnemonic', 'mnemonic COLLATE NOCASE,seq'),
                                  ('rd', 'hart,rd,start,seq'), ('reg', 'rd,pc,seq'),
                                  ('mem', 'mem,seq'), ('csr', 'csr,seq')]:
                db.execute('CREATE INDEX instruction_' + name + ' ON results(' + columns + ')')

    @staticmethod
    def condition(filters):
        clauses, args = [], []
        names = {'pc': 'pc', 'hart_id': 'hart', 'thread_id': 'thread', 'rd_addr': 'rd',
                 'mem_addr': 'mem', 'csr_addr': 'csr', 'time': 'start', 'mnemonic': 'mnemonic',
                 'is_store': 'is_store', 'is_csr': 'is_csr', 'stall': 'stall'}
        for field, value in filters.items():
            if field == 'mnemonic_pattern':
                re.compile(value, re.I)  # expose useful errors outside SQLite's callback
                clauses.append('matches(?,mnemonic)')
                args.append(value)
                continue
            op = '='
            if field.endswith('_lo') or field.endswith('_hi'):
                op = '>=' if field.endswith('_lo') else '<='
                field = field[:-3]
                if value is None:
                    continue
            column = names[field]
            if column in ('pc', 'hart', 'thread', 'rd', 'mem', 'csr'):
                value = number_key(value)
            if value is None:
                clauses.append(column + ' IS NULL')
            else:
                clauses.append(column + (' COLLATE NOCASE' if field == 'mnemonic' else '') + op + '?')
                args.append(value)
        return ' AND '.join(clauses) or '1', args

    def rows(self, columns='data', *, filters=None, order='seq', cancel=None, progress=None):
        # columns/order are internal SQL fragments, never user query strings.
        where, args = self.condition(filters or {})
        with self.sql_operation(cancel) as db:
            cursor = db.execute('SELECT ' + columns + ' FROM results WHERE ' + where + ' ORDER BY ' + order, args)
        try:
            count = 0
            while True:
                with self.sql_operation(cancel):
                    row = cursor.fetchone()
                if row is None:
                    break
                count += 1
                if progress and count % 4096 == 0:
                    progress(count, len(self))
                yield row
            check_cancel(cancel)
        finally:
            with self._lock:
                if not self._closed.is_set():
                    cursor.close()

    def iter_instructions(self, *, cancel=None, progress=None, **filters):
        for row in self.rows(filters=filters, cancel=cancel, progress=progress):
            yield self._restore(row[0])

    def select(self, predicate=None, *, cancel=None, progress=None, **filters):
        """Retain row IDs on disk, preserving sequence and O(1) ordinal access."""
        with self.sql_operation(cancel) as db:
            self._selection_id += 1
            name = 'selection_%d' % self._selection_id
            db.execute('CREATE TEMP TABLE ' + name + '(ordinal INTEGER PRIMARY KEY, source INTEGER)')
        try:
            if progress:
                progress(0, 0)
            if predicate is None:
                where, args = self.condition(filters)
                with self.sql_operation(cancel) as db:
                    db.execute('INSERT INTO ' + name + '(source) SELECT seq FROM results WHERE ' + where + ' ORDER BY seq', args)
                    count = db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                    db.commit()
            else:
                count = 0
                for seq, data in self.rows('seq,data', filters=filters, cancel=cancel, progress=progress):
                    if predicate(self._restore(data)):
                        with self.sql_operation(cancel) as db:
                            db.execute('INSERT INTO ' + name + '(source) VALUES(?)', (seq,))
                        count += 1
                with self.sql_operation(cancel) as db:
                    db.commit()
            check_cancel(cancel)
            if progress:
                progress(len(self), len(self))
            check_cancel(cancel)
            return InstructionSelection(self, name, count)
        except BaseException:
            with self._lock:
                self._db.rollback()
                self._db.execute('DROP TABLE IF EXISTS ' + name)
            raise

    def latest(self, cancel=None, **filters):
        rows = self.rows(filters=filters, order='start DESC,seq DESC LIMIT 1', cancel=cancel)
        try:
            row = next(rows, None)
            return None if row is None else self._restore(row[0])
        finally:
            rows.close()


class InstructionSelection:
    """A read-only, paged selection; closing it leaves its source intact."""
    def __init__(self, source, table, count):
        self.source, self.table, self.count = source, table, count
        self.closed = False

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self, range(*index.indices(len(self))))
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return next(self.iter_range(index, index + 1))

    def iter_range(self, start, end):
        if self.closed or self.source._closed.is_set():
            raise ValueError('Instruction selection is closed')
        yield from self.source._rows('SELECT r.data FROM ' + self.table + ' s JOIN results r ON r.seq=s.source '
                                    'WHERE s.ordinal>? AND s.ordinal<=? ORDER BY s.ordinal', (start, end))

    def __iter__(self):
        return self.iter_range(0, len(self))

    def read_page(self, start, size=100, cancel=None):
        if not 0 <= size <= 256 or start < 0:
            raise ValueError('Invalid result page')
        with self.source.sql_operation(cancel) as db:
            if self.closed:
                raise ValueError('Instruction selection is closed')
            cursor = db.execute('SELECT r.data FROM ' + self.table + ' s JOIN results r ON r.seq=s.source '
                                'WHERE s.ordinal>? AND s.ordinal<=? ORDER BY s.ordinal', (start, start + size))
            try:
                rows = cursor.fetchall()
            finally:
                cursor.close()
        check_cancel(cancel)
        return [self.source._restore(row[0]) for row in rows]

    def close(self):
        with self.source._lock:
            if not self.closed and not self.source._closed.is_set():
                self.source._db.execute('DROP TABLE IF EXISTS ' + self.table)
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
