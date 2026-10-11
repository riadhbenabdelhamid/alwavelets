"""Disk-backed declarations, order and scope membership for wide traces."""
from collections import OrderedDict
from collections.abc import Mapping, MutableMapping, Sequence
import hashlib
from itertools import islice
import json
from json.encoder import encode_basestring_ascii
import operator
import weakref

from .core import AliasSignal


class CatalogAlias(AliasSignal):
    __slots__ = ('__weakref__',)

    def value_at(self, time):
        return self.master.value_at(time)


class SignalOrder(Sequence):
    """Indexed declaration order, optionally restricted to one direct scope."""
    def __init__(self, catalog, scope=None, count=None):
        self.catalog, self.scope = catalog, scope
        self.count = count

    def __len__(self):
        return len(self.catalog) if self.count is None else self.count

    def __getitem__(self, index):
        if isinstance(index, slice):
            from .storage import SequenceView
            return SequenceView(self, range(*index.indices(len(self))))
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        store = self.catalog.store
        with store.lock:
            if self.scope is None:
                row = store.db.execute('SELECT sid FROM signals WHERE seq=?', (index,)).fetchone()
            else:
                row = store.db.execute('SELECT sid FROM signals WHERE scope=? AND local_seq=?',
                                       (self.scope, index)).fetchone()
        return row[0]

    def __iter__(self):
        if self.scope is None:
            yield from (row[0] for row in self.catalog.rows('sid'))
            return
        # Match the scope index's ordering. Ordering by global seq here would
        # make SQLite sort the remaining scope again for every fetched batch.
        store = self.catalog.store
        for start in range(0, len(self), 256):
            with store.lock:
                rows = store.db.execute('SELECT sid FROM signals WHERE scope=? AND local_seq>=? '
                                        'AND local_seq<? ORDER BY local_seq',
                                        (self.scope, start, start + 256)).fetchall()
            yield from (row[0] for row in rows)

    def __eq__(self, other):
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))


class SignalCatalog(Mapping):
    """Immutable metadata with a bounded strong cache and live-object identity.

    Weak references keep a held signal (including an alias's master) identical
    across cache eviction, without retaining every visited declaration.
    """
    cache_limit = 1024

    def __init__(self, store, create=False):
        self.store = store
        self.cache = OrderedDict()
        self.live = weakref.WeakValueDictionary()
        self.pending = []
        self.pending_bytes = 0
        self._ordinals = OrderedDict()
        self._scope_ranges = OrderedDict()
        self._layout_hash = hashlib.sha256() if create else None
        self._layout_signature = None
        self._scope_hierarchy = None
        if create:
            store.db.executescript('''
                CREATE TABLE signals(seq INTEGER PRIMARY KEY,sid TEXT UNIQUE,definition TEXT,
                    master TEXT,n INTEGER DEFAULT 0,scope TEXT,local_seq INTEGER,
                    full_name TEXT,kind TEXT,size INTEGER,written INTEGER,last_time INTEGER,aliases INTEGER DEFAULT 1,
                    short_n INTEGER DEFAULT 0,short_data BLOB);
                CREATE UNIQUE INDEX scope_order ON signals(scope,local_seq);
                CREATE INDEX unpublished_counts ON signals(written) WHERE written IS NOT NULL AND n!=written;
                CREATE TABLE scopes(path TEXT PRIMARY KEY,n INTEGER);
            ''')
        self.count = store.db.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        self.order = SignalOrder(self)

    def declare(self, name, identifier, size, kind, scope, bounds, *, deduplicate=True):
        from .streaming import check_cancel
        check_cancel(self.store.cancel)
        self.pending.append((name, identifier, size, kind, scope, bounds, deduplicate))
        self.pending_bytes += 256 + 4 * (len(name) + len(identifier) + len(kind) + len(scope))
        if len(self.pending) >= 512 or self.pending_bytes >= 256 * 1024:
            self.flush_declarations()

    def flush_declarations(self):
        """Resolve duplicates and aliases against a bounded batch and the DB."""
        if not self.pending:
            return
        from .core import signal_display_name
        from .streaming import check_cancel
        with self.store.lock:
            check_cancel(self.store.cancel)
            db = self.store.db
            keys = list(dict.fromkeys(row[1] for row in self.pending))
            scopes = list(dict.fromkeys(row[4] for row in self.pending))
            masters = dict(db.execute('SELECT sid,definition FROM signals WHERE sid IN (' +
                                      ','.join('?' for _ in keys) + ')', keys))
            counts = dict(db.execute('SELECT path,n FROM scopes WHERE path IN (' +
                                     ','.join('?' for _ in scopes) + ')', scopes))
            rows, aliases, touched, tails = [], {}, {}, {}
            for name, identifier, size, kind, scope, bounds, deduplicate in self.pending:
                check_cancel(self.store.cancel)
                definition = [name, identifier, size, kind, scope, list(bounds) if bounds is not None else None]
                old = masters.get(identifier)
                if old is not None and deduplicate:
                    previous = json.loads(old)
                    if (previous[0], previous[4], previous[5]) == (name, scope, definition[5]):
                        continue
                key = identifier if old is None else '%s #%d' % (identifier, self.count)
                # Reuse the common type/scope JSON within this bounded batch.
                # These strings exactly match the original json.dumps format,
                # including escaping, so existing session hashes stay valid.
                tail_key = size, kind, scope, tuple(bounds) if bounds is not None else None
                tail = tails.get(tail_key)
                if tail is None:
                    tail = tails[tail_key] = json.dumps([size, kind, scope, definition[5]])[1:]
                prefix = '[' + encode_basestring_ascii(name) + ', '
                encoded = prefix + encode_basestring_ascii(identifier) + ', ' + tail
                self._layout_hash.update((prefix + tail + '\n').encode())
                full = signal_display_name(name, size, bounds)
                if scope:
                    full = scope + '.' + full
                local = counts.get(scope, 0)
                rows.append((self.count, key, encoded, identifier, scope, local, full, kind, size))
                counts[scope] = local + 1
                touched[scope] = local + 1
                if old is None:
                    masters[identifier] = encoded
                else:
                    aliases[identifier] = aliases.get(identifier, 0) + 1
                self.count += 1
            db.executemany('INSERT INTO signals(seq,sid,definition,master,scope,local_seq,full_name,kind,size) '
                           'VALUES(?,?,?,?,?,?,?,?,?)', rows)
            db.executemany('INSERT INTO scopes VALUES(?,?) ON CONFLICT(path) DO UPDATE SET n=excluded.n', touched.items())
            db.executemany('UPDATE signals SET aliases=aliases+? WHERE sid=?',
                           ((n, key) for key, n in aliases.items()))
            self.pending.clear()
            self.pending_bytes = 0
            self._ordinals.clear()
            self._scope_ranges.clear()
            self._scope_hierarchy = None

    @property
    def layout_signature(self):
        """Declaration identity/order, independent of VCD/FST handle spelling."""
        self.flush_declarations()
        if self._layout_hash is not None:
            return self._layout_hash.hexdigest()
        if self._layout_signature is None:
            with self.store.lock:
                row = self.store.db.execute('SELECT value FROM meta WHERE key="catalog_layout"').fetchone()
            if row is not None:
                self._layout_signature = row[0]
            else:
                # Older indexes remain usable; only their first session save
                # needs this bounded scan, without constructing signal objects.
                digest = hashlib.sha256()
                for definition, in self.rows('definition'):
                    name, _, size, kind, scope, bounds = json.loads(definition)
                    digest.update(json.dumps([name, size, kind, scope, bounds]).encode() + b'\n')
                self._layout_signature = digest.hexdigest()
        return self._layout_signature

    def ordinal(self, sid):
        with self.store.lock:
            if sid in self._ordinals:
                self._ordinals.move_to_end(sid)
                return self._ordinals[sid]
            row = self.store.db.execute('SELECT seq FROM signals WHERE sid=?', (sid,)).fetchone()
            value = row[0] if row else None
            self._ordinals[sid] = value
            if len(self._ordinals) > 1024:
                self._ordinals.popitem(last=False)
            return value

    def ordinal_items(self, items):
        """Resolve an explicit selection in bounded batches, in input order."""
        items = iter(items)
        while True:
            batch = list(islice(items, 512))
            if not batch:
                return
            keys = [item for item in batch if isinstance(item, str)]
            with self.store.lock:
                found = dict(self.store.db.execute('SELECT sid,seq FROM signals WHERE sid IN (' +
                                                   ','.join('?' for _ in keys) + ')', keys)) if keys else {}
            for item in batch:
                yield item, found.get(item)

    def scope_ranges(self, path):
        """Direct scope membership as ordinal runs, preserving declaration order."""
        cached = self._scope_ranges.get(path)
        if cached is not None:
            self._scope_ranges.move_to_end(path)
            return cached
        with self.store.lock:
            db = self.store.db
            row = db.execute('SELECT n FROM scopes WHERE path=?', (path,)).fetchone()
            count = row[0] if row else 0
            if not count:
                return []
            lo = db.execute('SELECT seq FROM signals WHERE scope=? AND local_seq=0', (path,)).fetchone()[0]
            hi = db.execute('SELECT seq FROM signals WHERE scope=? AND local_seq=?', (path, count - 1)).fetchone()[0]
        if hi - lo + 1 == count:
            runs = [range(lo, hi + 1)]
        else:
            runs = []
            for start in range(0, count, 256):
                with self.store.lock:
                    rows = self.store.db.execute('SELECT seq FROM signals WHERE scope=? AND local_seq>=? '
                                                 'AND local_seq<? ORDER BY local_seq', (path, start, start + 256)).fetchall()
                for seq, in rows:
                    if runs and runs[-1].stop == seq:
                        runs[-1] = range(runs[-1].start, seq + 1)
                    else:
                        runs.append(range(seq, seq + 1))
        self._scope_ranges[path] = runs
        if len(self._scope_ranges) > 128:
            self._scope_ranges.popitem(last=False)
        return runs

    def __len__(self):
        self.flush_declarations()
        return self.count

    def __iter__(self):
        return iter(self.order)

    def __contains__(self, key):
        self.flush_declarations()
        with self.store.lock:
            return self.store.db.execute('SELECT 1 FROM signals WHERE sid=?', (key,)).fetchone() is not None

    def __getitem__(self, key):
        from .storage import StoredSignal, _signal
        self.flush_declarations()
        with self.store.lock:
            sig = self.live.get(key)
            if sig is None:
                row = self.store.db.execute('SELECT definition,master FROM signals WHERE sid=?', (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                definition, master = json.loads(row[0]), row[1]
                if key == master:
                    sig = StoredSignal(_signal(definition, metadata_only=True), key, self.store)
                else:
                    name, identifier, size, kind, scope, bounds = definition
                    sig = CatalogAlias(name, identifier, size, kind, scope, self[master],
                                       tuple(bounds) if bounds is not None else None)
                self.live[key] = sig
            self.cache[key] = sig
            self.cache.move_to_end(key)
            while len(self.cache) > self.cache_limit:
                self.cache.popitem(last=False)
            return sig

    def rows(self, columns, where='', args=()):
        """Bounded keyset traversal; no cursor spans a writer's publication."""
        self.flush_declarations()
        last = -1
        clause = ' AND (' + where + ')' if where else ''
        while True:
            with self.store.lock:
                if self.store.closed:
                    return
                rows = self.store.db.execute('SELECT seq,' + columns + ' FROM signals WHERE seq>?' +
                                             clause + ' ORDER BY seq LIMIT 256', (last,) + args).fetchall()
            if not rows:
                return
            last = rows[-1][0]
            for row in rows:
                yield row[1:]

    def scope_tree(self):
        from .scope_store import ScopeHierarchy
        self.flush_declarations()
        if self._scope_hierarchy is None:
            self._scope_hierarchy = ScopeHierarchy(self)
        return self._scope_hierarchy.root

    def visible_ids(self, collapsed):
        # Scope checks run on short row batches, without creating signal objects.
        for sid, scope in self.rows('sid,scope'):
            parts = scope.split('.') if scope else ()
            if not any('.'.join(parts[:i]) in collapsed for i in range(1, len(parts) + 1)):
                yield sid


class CatalogDefinitions(Mapping):
    def __init__(self, catalog):
        self.catalog = catalog

    def __getitem__(self, key):
        store = self.catalog.store
        with store.lock:
            row = store.db.execute('SELECT definition FROM signals WHERE sid=? AND master=sid', (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __iter__(self):
        return (row[0] for row in self.catalog.rows('sid', 'master=sid'))

    def __len__(self):
        with self.catalog.store.lock:
            return self.catalog.store.db.execute('SELECT COUNT(*) FROM signals WHERE master=sid').fetchone()[0]


class CatalogField(MutableMapping):
    """Bounded write-back state for counts and timestamps during ingestion."""
    def __init__(self, catalog, column, writable=False):
        assert column in ('n', 'written', 'last_time')
        self.catalog, self.column, self.writable = catalog, column, writable
        self.cache, self.dirty = OrderedDict(), set()
        self.limit = 1024

    def __getitem__(self, key):
        with self.catalog.store.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                value = self.cache[key]
            else:
                row = self.catalog.store.db.execute('SELECT ' + self.column + ' FROM signals WHERE sid=?', (key,)).fetchone()
                value = row[0] if row else None
                self._cache(key, value)
            if value is None:
                raise KeyError(key)
            return value

    def _cache(self, key, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        while len(self.cache) > self.limit:
            # Drain dirty entries together before evicting one. Wide traces
            # otherwise issue a separate UPDATE for almost every event.
            if next(iter(self.cache)) in self.dirty:
                self.flush()
            self.cache.popitem(last=False)

    def __setitem__(self, key, value):
        if not self.writable:
            raise TypeError('read-only field')
        with self.catalog.store.lock:
            self._cache(key, value)
            self.dirty.add(key)

    def __delitem__(self, key):
        self[key] = None

    def flush(self):
        with self.catalog.store.lock:
            self.catalog.store.db.executemany('UPDATE signals SET ' + self.column + '=? WHERE sid=?',
                                              ((self.cache[key], key) for key in self.dirty))
            self.dirty.clear()

    def __iter__(self):
        self.flush()
        return (row[0] for row in self.catalog.rows('sid', self.column + ' IS NOT NULL'))

    def __len__(self):
        self.flush()
        with self.catalog.store.lock:
            return self.catalog.store.db.execute('SELECT COUNT(*) FROM signals WHERE ' + self.column + ' IS NOT NULL').fetchone()[0]

    def clear(self):
        if not self.writable:
            raise TypeError('read-only field')
        with self.catalog.store.lock:
            self.cache.clear()
            self.dirty.clear()
            self.catalog.store.db.execute('UPDATE signals SET ' + self.column + '=NULL')


def attach_catalog(store, vcd, catalog):
    vcd.signals, vcd.signal_order = catalog, catalog.order
    store.definitions = CatalogDefinitions(catalog)
    store.counts = CatalogField(catalog, 'n')
    store.vcd, vcd._store = vcd, store
