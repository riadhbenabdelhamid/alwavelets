"""Cancellable fuzzy ranking with bounded candidates and disk-backed results."""
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
import os
import sqlite3
import tempfile
import threading

from .storage import SequenceView
from .streaming import check_cancel


class FinderCandidates(Sequence):
    def __init__(self, vcd, order=None, extras=None):
        self.vcd = vcd
        self.extras = extras or {}
        self.order = vcd.signal_order if order is None else order

    def __len__(self):
        return len(self.order)

    @staticmethod
    def candidate(sid, name, kind, size):
        return sid, name, name.lower(), '~' if kind in ('real', 'realtime') else ('═' if size > 1 else '─'), size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self, range(*index.indices(len(self))))
        from .catalog import SignalCatalog
        if isinstance(self.vcd.signals, SignalCatalog):
            catalog = self.vcd.signals
            with catalog.store.lock:
                if self.order is catalog.order:
                    if index < 0:
                        index += len(self)
                    if not 0 <= index < len(self):
                        raise IndexError(index)
                    row = catalog.store.db.execute('SELECT sid,full_name,kind,size FROM signals WHERE seq=?', (index,)).fetchone()
                else:
                    row = catalog.store.db.execute('SELECT sid,full_name,kind,size FROM signals WHERE sid=?',
                                                   (self.order[index],)).fetchone()
            if row is not None:
                return self.candidate(*row)
            sid = self.order[index]
            sig = self.extras.get(sid)
        else:
            sid = self.order[index]
            sig = self.vcd.signals.get(sid) or self.extras.get(sid)
        if sig is None:
            return self.candidate(sid, '', 'wire', 1)
        return self.candidate(sid, sig.full_name, sig.signal_type, sig.size)

    def names(self):
        """Stream names directly from the catalog, without filling its object cache."""
        from .catalog import SignalCatalog
        catalog = self.vcd.signals
        if isinstance(catalog, SignalCatalog) and self.order is catalog.order:
            yield from (row[0].lower() for row in catalog.rows('full_name'))
        elif isinstance(catalog, SignalCatalog):
            for start in range(0, len(self.order), 256):
                keys = list(self.order[start:start + 256])
                with catalog.store.lock:
                    rows = catalog.store.db.execute('SELECT sid,full_name FROM signals WHERE sid IN (' +
                                                     ','.join('?' for _ in keys) + ')', keys).fetchall()
                names = dict(rows)
                for key in keys:
                    extra = self.extras.get(key)
                    yield names.get(key, extra.full_name if extra is not None else '').lower()
        else:
            yield from (self[i][2] for i in range(len(self)))


class RankedMatches(Sequence):
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='alwavelets-finder-')
        self.path = os.path.join(self.temporary.name, 'matches.sqlite')
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.execute('PRAGMA cache_size=-1024')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('CREATE TABLE scores(candidate INTEGER PRIMARY KEY,score INTEGER)')
        self.db.execute('CREATE TABLE ranked(candidate INTEGER)')
        self.count = 0
        self.owner = None

    @classmethod
    def search(cls, candidates, query, cancel, declaration_order=False):
        from .finder import fuzzy_match
        results = cls()
        try:
            results.db.set_progress_handler(lambda: int(cancel.is_set()), 1000)
            for index, name in enumerate(candidates.names()):
                if index % 256 == 0:
                    check_cancel(cancel)
                score = fuzzy_match(query, name)
                if score is not None:
                    results.db.execute('INSERT INTO scores VALUES(?,?)', (index, -score))
                    results.count += 1
            check_cancel(cancel)
            # SQLite spills sorting to disk; rowids make every result page a
            # direct lookup, including End in a million-result match set.
            ordering = 'candidate' if declaration_order else 'score,candidate'
            results.db.execute('INSERT INTO ranked SELECT candidate FROM scores ORDER BY ' + ordering)
            results.db.execute('DROP TABLE scores')
            results.db.commit()
            check_cancel(cancel)
            results.db.set_progress_handler(None, 0)
            return results
        except BaseException:
            results.close()
            raise

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return SequenceView(self, range(*index.indices(len(self))))
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self.db.execute('SELECT candidate FROM ranked WHERE rowid=?', (index + 1,)).fetchone()[0]

    def __iter__(self):
        for row in self.db.execute('SELECT candidate FROM ranked ORDER BY rowid'):
            yield row[0]

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
            self.temporary.cleanup()
            if self.owner is not None:
                self.owner._result_stores.discard(self)
                self.owner = None


class FinderSearch:
    """Only the latest request is claimable; obsolete results clean themselves."""
    def __init__(self, owner):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='wavelet-finder')
        self.future = self.cancel = None
        self.owner = owner
        if not hasattr(owner, '_finder_workers'):
            owner._finder_workers = set()
        owner._finder_workers.add(self)

    @staticmethod
    def _discard(future):
        if not future.cancelled() and future.exception() is None:
            future.result().close()

    def invalidate(self):
        if self.future is not None:
            self.cancel.set()
            self.future.cancel()
            self.future.add_done_callback(self._discard)
            self.future = self.cancel = None

    def request(self, candidates, query, *, declaration_order=False):
        self.invalidate()
        self.cancel = threading.Event()
        if declaration_order:
            self.future = self.pool.submit(RankedMatches.search, candidates, query, self.cancel, True)
        else:
            self.future = self.pool.submit(RankedMatches.search, candidates, query, self.cancel)

    def poll(self):
        if self.future is None or not self.future.done():
            return None
        future = self.future
        self.future = self.cancel = None
        result = future.result()
        result.owner = self.owner
        if not hasattr(self.owner, '_result_stores'):
            self.owner._result_stores = set()
        self.owner._result_stores.add(result)
        return result

    def close(self):
        self.invalidate()
        self.pool.shutdown(wait=True)
        self.owner._finder_workers.discard(self)
