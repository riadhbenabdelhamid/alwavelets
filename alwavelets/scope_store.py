"""Persistent scope metadata and random-access browser rows for paged traces."""
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Mapping, MutableSet, Sequence
import operator
import sqlite3
import weakref

from .streaming import check_cancel

VERSION = 1
_FIELDS = 'id,parent,child_seq,name,path,depth,n,children,total,pre,stop,lo,hi,ordered'
_BATCH_ROWS = 256
_BATCH_BYTES = 256 * 1024
_CHILD_PAGES = 8


class ScopeHierarchy(Mapping):
    """Path lookup with bounded nodes; held nodes keep their identity."""
    cache_limit = 1024

    def __init__(self, catalog):
        self.catalog, self.store = catalog, catalog.store
        self.cache = OrderedDict()
        self.live = weakref.WeakValueDictionary()
        self._ensure()
        self.root = self.by_id(0)

    def _ensure(self):
        db = self.store.db
        with self.store.lock:
            expected = (VERSION, len(self.catalog))
            present = db.execute("SELECT 1 FROM sqlite_master WHERE name='scope_meta'").fetchone()
            if present and db.execute('SELECT version,signals FROM scope_meta').fetchone() == expected:
                return
            # Take the write lock before reading the version again, so competing
            # legacy readers cannot deadlock while upgrading a read transaction.
            # Declaration ingestion already owns a transaction; retain its work.
            owned = not db.in_transaction
            db.execute('BEGIN IMMEDIATE' if owned else 'SAVEPOINT scope_upgrade')
            try:
                db.execute('CREATE TABLE IF NOT EXISTS scope_meta(version INTEGER,signals INTEGER)')
                if db.execute('SELECT version,signals FROM scope_meta').fetchone() != expected:
                    # Index sorts and bulk SQL must also remain cancellable.
                    cancel = self.store.cancel
                    db.set_progress_handler(lambda: int(cancel is not None and cancel.is_set()), 4096)
                    try:
                        self._build()
                    except sqlite3.OperationalError:
                        check_cancel(cancel)
                        raise
                    finally:
                        db.set_progress_handler(None, 0)
                    check_cancel(cancel)
                if owned:
                    db.commit()
                else:
                    db.execute('RELEASE scope_upgrade')
            except BaseException:
                if db.in_transaction:
                    if owned:
                        db.rollback()
                    else:
                        db.execute('ROLLBACK TO scope_upgrade')
                        db.execute('RELEASE scope_upgrade')
                raise

    def _build(self):
        from .loading import report_stage
        progress = self.store.stage_cb
        report_stage(progress, 'Preparing scopes', 0, unit='scopes')
        db = self.store.db
        db.execute('DROP TABLE IF EXISTS scope_nodes')
        db.execute('''CREATE TABLE scope_nodes(id INTEGER PRIMARY KEY,parent INTEGER,child_seq INTEGER,
                   name TEXT,path TEXT UNIQUE,depth INTEGER,n INTEGER DEFAULT 0,children INTEGER DEFAULT 0,
                   total INTEGER DEFAULT 0,pre INTEGER,stop INTEGER,lo INTEGER,hi INTEGER,ordered INTEGER)''')
        db.execute('INSERT INTO scope_nodes(id,parent,child_seq,name,path,depth) VALUES(0,NULL,0,"","",0)')
        created_count = self._build_nodes(progress)
        db.execute('CREATE UNIQUE INDEX scope_children ON scope_nodes(parent,child_seq)')
        self._count_nodes(progress, created_count)
        db.execute('CREATE UNIQUE INDEX scope_preorder ON scope_nodes(pre)')
        db.execute('CREATE INDEX scope_depth ON scope_nodes(depth)')
        db.execute('DELETE FROM scope_meta')
        db.execute('INSERT INTO scope_meta VALUES(?,?)', (VERSION, len(self.catalog)))

    def _build_nodes(self, progress):
        """Resolve ancestors together and write each batch in declaration order."""
        from .loading import report_stage
        db = self.store.db
        # Cached records hold only id, depth and the next child ordinal. Every
        # flush persists those ordinals before any records can be evicted.
        known = OrderedDict({'': [0, 0, 0]})
        pending, pending_bytes, created_count = {}, 0, 1

        def flush():
            nonlocal pending_bytes, created_count
            if not pending:
                return
            check_cancel(self.store.cancel)
            needed = dict.fromkeys(pending)
            needed.update((row[0], None) for row in pending.values() if row[0] is not None)
            nodes = {path: known[path] for path in needed if path in known}
            missing = [path for path in needed if path not in nodes]
            if missing:
                nodes.update((path, [node_id, depth, children]) for node_id, path, depth, children in
                    db.execute('SELECT id,path,depth,children FROM scope_nodes WHERE path IN (' +
                               ','.join('?' for _ in missing) + ')', missing))
            first_new = created_count
            inserts, direct, parents = [], [], {}
            for path, (parent_path, name, count, lo, hi) in pending.items():
                node = nodes.get(path)
                if node is None:
                    parent = nodes[parent_path]
                    node = nodes[path] = [created_count, parent[1] + 1, 0]
                    created_count += 1
                    inserts.append((node[0], parent[0], parent[2], name, path, node[1], count or 0, lo, hi))
                    parent[2] += 1
                    parents[parent[0]] = parent
                elif count is not None:
                    direct.append((count, lo, hi, node[0]))
            db.executemany('INSERT INTO scope_nodes(id,parent,child_seq,name,path,depth,n,lo,hi,children) '
                           'VALUES(?,?,?,?,?,?,?,?,?,?)', (row + (nodes[row[4]][2],) for row in inserts))
            db.executemany('UPDATE scope_nodes SET children=? WHERE id=?',
                           ((node[2], node_id) for node_id, node in parents.items() if node_id < first_new))
            db.executemany('UPDATE scope_nodes SET n=?,lo=?,hi=? WHERE id=?', direct)
            for path, node in nodes.items():
                known[path] = node
                known.move_to_end(path)
                if len(known) > self.cache_limit:
                    known.popitem(last=False)
            pending.clear()
            pending_bytes = 0
            report_stage(progress, 'Preparing scopes', created_count, unit='scopes')

        def stage(path, parent, name, count=None, lo=None, hi=None):
            nonlocal pending_bytes
            if path not in pending:
                pending_bytes += 256 + 4 * (len(path) + len(parent or '') + len(name))
            pending[path] = parent, name, count, lo, hi
            if len(pending) >= _BATCH_ROWS or pending_bytes >= _BATCH_BYTES:
                flush()

        cursor = db.execute('''SELECT path,n,
            (SELECT seq FROM signals WHERE scope=scopes.path AND local_seq=0),
            (SELECT seq FROM signals WHERE scope=scopes.path AND local_seq=scopes.n-1)
            FROM scopes ORDER BY rowid''')
        for path, count, lo, hi in cursor:
            check_cancel(self.store.cancel)
            parent, parts = path, []
            while parent and parent not in known and parent not in pending:
                if len(parts) % _BATCH_ROWS == 0:
                    check_cancel(self.store.cancel)
                parent, _, name = parent.rpartition('.')
                parts.append(name)
            if not parts:
                parent, _, name = path.rpartition('.')
                stage(path, parent if path else None, name, count, lo, hi)
            for name in reversed(parts):
                prefix = parent + '.' + name if parent else name
                if prefix == path:
                    stage(prefix, parent, name, count, lo, hi)
                else:
                    stage(prefix, parent, name)
                parent = prefix
        flush()
        return created_count

    def _count_nodes(self, progress, created_count):
        from .loading import report_stage
        db = self.store.db
        # Iterative postorder computes counts and ordinal bounds with storage
        # proportional to depth. A fixed cache of child pages avoids retaining
        # a page per ancestor in deep trees, or querying each sibling separately.
        pages, updates = OrderedDict(), []
        serial = 0
        report_stage(progress, 'Counting scope signals', 0, created_count, 'scopes')

        def frame(row):
            nonlocal serial
            node_id, n, children, lo, hi = row
            pre = serial
            serial += 1
            return [node_id, 0, children, n, lo, hi, not n or hi - lo + 1 == n, pre]

        def flush():
            db.executemany('UPDATE scope_nodes SET total=?,pre=?,stop=?,lo=?,hi=?,ordered=? WHERE id=?', updates)
            updates.clear()
            report_stage(progress, 'Counting scope signals', serial, created_count, 'scopes')

        stack = [frame(db.execute('SELECT id,n,children,lo,hi FROM scope_nodes WHERE id=0').fetchone())]
        while stack:
            check_cancel(self.store.cancel)
            item = stack[-1]
            node_id, child, children, total, lo, hi, ordered, pre = item
            if child < children:
                item[1] += 1
                start = child // _BATCH_ROWS * _BATCH_ROWS
                key = node_id, start
                page = pages.get(key)
                if page is None:
                    # Start at the unvisited child: previously completed nodes
                    # now contain subtree lo/hi, rather than direct-signal bounds.
                    page = pages[key] = (child, db.execute(
                        'SELECT id,n,children,lo,hi FROM scope_nodes WHERE parent=? '
                        'AND child_seq>=? AND child_seq<? ORDER BY child_seq',
                        (node_id, child, start + _BATCH_ROWS)).fetchall())
                    if len(pages) > _CHILD_PAGES:
                        pages.popitem(last=False)
                pages.move_to_end(key)
                stack.append(frame(page[1][child - page[0]]))
                continue
            updates.append((total, pre, serial, lo, hi, ordered, node_id))
            stack.pop()
            if len(updates) >= _BATCH_ROWS:
                flush()
            if stack:
                parent = stack[-1]
                if total:
                    parent[6] = parent[6] and ordered and (not parent[3] or parent[5] + 1 == lo)
                    parent[4] = min(parent[4], lo) if parent[3] else lo
                    parent[5] = max(parent[5], hi) if parent[3] else hi
                    parent[3] += total
        if updates:
            flush()

    def _node(self, row):
        node_id = row[0]
        node = self.live.get(node_id)
        if node is None:
            node = PagedScope(self, row)
            self.live[node_id] = node
        self.cache[node_id] = node
        self.cache.move_to_end(node_id)
        while len(self.cache) > self.cache_limit:
            self.cache.popitem(last=False)
        return node

    def by_id(self, node_id):
        with self.store.lock:
            node = self.live.get(node_id)
            if node is not None:
                return node
            row = self.store.db.execute('SELECT ' + _FIELDS + ' FROM scope_nodes WHERE id=?', (node_id,)).fetchone()
            if row is None:
                raise KeyError(node_id)
            return self._node(row)

    def __getitem__(self, path):
        with self.store.lock:
            row = self.store.db.execute('SELECT ' + _FIELDS + ' FROM scope_nodes WHERE path=?', (path,)).fetchone()
            if row is None:
                raise KeyError(path)
            return self._node(row)

    def __len__(self):
        return self.root.stop

    def __iter__(self):
        return (node.full_path for node in self.walk(self.root))

    def walk(self, root):
        for start in range(root.pre, root.stop, 256):
            with self.store.lock:
                rows = self.store.db.execute('SELECT ' + _FIELDS + ' FROM scope_nodes WHERE pre>=? AND pre<? ORDER BY pre',
                                             (start, min(start + 256, root.stop))).fetchall()
                nodes = [self._node(row) for row in rows]
            yield from nodes

    def child_at(self, parent, index):
        with self.store.lock:
            row = self.store.db.execute('SELECT ' + _FIELDS + ' FROM scope_nodes WHERE parent=? AND child_seq=?',
                                       (parent, index)).fetchone()
            if row is None:
                raise IndexError(index)
            return self._node(row)

    def ranges(self, root, *, ordered=True):
        """Subtree selection; contiguous subtrees need no descendant traversal."""
        if not root.total:
            return
        if root.hi - root.lo + 1 == root.total and (not ordered or root.ordered):
            yield range(root.lo, root.hi + 1)
            return
        pending = None
        for node in self.walk(root):
            for part in self.catalog.scope_ranges(node.full_path):
                if pending is not None and pending.stop == part.start:
                    pending = range(pending.start, part.stop)
                else:
                    if pending is not None:
                        yield pending
                    pending = part
        if pending is not None:
            yield pending


class PagedScope:
    __slots__ = ('hierarchy', 'id', 'parent', 'child_seq', 'name', 'full_path', 'depth', 'n', 'child_count',
                 'total', 'pre', 'stop', 'lo', 'hi', 'ordered', 'collapsed', '__weakref__')

    def __init__(self, hierarchy, row):
        self.hierarchy = hierarchy
        (self.id, self.parent, self.child_seq, self.name, self.full_path, self.depth, self.n, self.child_count,
         self.total, self.pre, self.stop, self.lo, self.hi, self.ordered) = row
        self.collapsed = False

    @property
    def signal_ids(self):
        from .catalog import SignalOrder
        return SignalOrder(self.hierarchy.catalog, self.full_path, self.n)

    @property
    def children(self):
        return ScopeChildren(self)

    def total_signals(self):
        return self.total


class ScopeChildren(Mapping):
    def __init__(self, node):
        self.node = node

    def __len__(self):
        return self.node.child_count

    def __getitem__(self, name):
        parent = self.node
        path = parent.full_path + '.' + name if parent.full_path else name
        node = parent.hierarchy[path]
        if node.parent != parent.id:
            raise KeyError(name)
        return node

    def __iter__(self):
        return (node.name for node in self.values())

    def values(self):
        hierarchy = self.node.hierarchy
        for start in range(0, len(self), 256):
            with hierarchy.store.lock:
                rows = hierarchy.store.db.execute('SELECT ' + _FIELDS + ' FROM scope_nodes '
                    'WHERE parent=? AND child_seq>=? AND child_seq<? ORDER BY child_seq',
                    (self.node.id, start, start + 256)).fetchall()
                nodes = [hierarchy._node(row) for row in rows]
            yield from nodes


class ScopeCounts(Mapping):
    def __init__(self, hierarchy):
        self.hierarchy = hierarchy

    def __getitem__(self, node):
        return node.total

    def __len__(self):
        return len(self.hierarchy)

    def __iter__(self):
        return self.hierarchy.walk(self.hierarchy.root)


class PagedTreeRows(Sequence):
    """One run per explicit expansion; unopened siblings are indexed in SQLite."""
    def __init__(self, scopes, expanded, get_signal, is_selected):
        self.scopes = scopes
        self.get_signal, self.is_selected = get_signal, is_selected
        hierarchy = self.hierarchy = scopes.root.hierarchy
        self.opened = {0: (0, 0, 0, scopes.root.n, scopes.root.child_count, 0)}
        for path in expanded:
            node = hierarchy.get(path)
            if node is not None:
                self.opened[node.id] = (node.id, node.parent, node.child_seq, node.n, node.child_count, node.depth)
        contents, children = {}, {}
        self.runs = {}
        for node_id, parent, seq, n, count, depth in sorted(self.opened.values(), key=lambda r: -r[5]):
            parts = sorted(children.get(node_id, ()))
            starts, ends, ids = [], [], []
            extra = 0
            for child_seq, child_id in parts:
                starts.append(child_seq + extra)
                extra += contents[child_id]
                ends.append(extra)
                ids.append(child_id)
            self.runs[node_id] = starts, ends, ids
            contents[node_id] = n + count + extra
            if node_id and parent in self.opened:
                children.setdefault(parent, []).append((seq, node_id))
        self.size = contents[0]

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.size))]
        index = operator.index(index)
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        node_id = 0
        while True:
            _, _, _, n, _, depth = self.opened[node_id]
            if index < n:
                node = self.hierarchy.by_id(node_id)
                sid = node.signal_ids[index]
                return 'signal', self.get_signal(sid), depth, self.is_selected(sid), sid
            index -= n
            starts, ends, ids = self.runs[node_id]
            run = bisect_right(starts, index) - 1
            if run >= 0 and index <= starts[run] + ends[run] - (ends[run - 1] if run else 0):
                if index > starts[run]:
                    node_id, index = ids[run], index - starts[run] - 1
                    continue
                node = self.hierarchy.by_id(ids[run])
                return 'scope', node, depth, True, node.total
            child_seq = index - (ends[run] if run >= 0 else 0)
            node = self.hierarchy.child_at(node_id, child_seq)
            return 'scope', node, depth, False, node.total


class ScopeFolds(MutableSet):
    """The default folded level plus explicit edits, without one string per scope."""
    def __init__(self, hierarchy, level=2, extra=(), unfolded=()):
        self.hierarchy, self.level = hierarchy, level
        self.extra, self.unfolded = set(extra), set(unfolded)
        self._count = None

    @staticmethod
    def _under(path, prefix):
        return not prefix or path == prefix or path.startswith(prefix + '.')

    def _base(self, path):
        if self.level is None:
            return False
        node = self.hierarchy.get(path)
        return node is not None and node.depth == self.level

    def _excluded(self, path):
        return any(self._under(path, prefix) for prefix in self.unfolded)

    def __contains__(self, path):
        return path in self.extra or (self._base(path) and not self._excluded(path))

    def __len__(self):
        if self._count is None:
            count = 0
            if self.level is not None:
                with self.hierarchy.store.lock:
                    db = self.hierarchy.store.db
                    count = db.execute('SELECT COUNT(*) FROM scope_nodes WHERE depth=?', (self.level,)).fetchone()[0]
                    for path in self.unfolded:
                        node = self.hierarchy.get(path)
                        if node is not None and not any(p != path and self._under(path, p) for p in self.unfolded):
                            count -= db.execute('SELECT COUNT(*) FROM scope_nodes WHERE depth=? AND pre>=? AND pre<?',
                                                (self.level, node.pre, node.stop)).fetchone()[0]
            self._count = count + sum(not self._base(p) or self._excluded(p) for p in self.extra)
        return self._count

    def paths_at_depth(self, depth, *, with_signals=False):
        last = -1
        while True:
            with self.hierarchy.store.lock:
                rows = self.hierarchy.store.db.execute('SELECT id,path FROM scope_nodes WHERE depth=? AND id>? ' +
                    ('AND n>0 ' if with_signals else '') + 'ORDER BY id LIMIT 256', (depth, last)).fetchall()
            if not rows:
                return
            last = rows[-1][0]
            yield from (row[1] for row in rows)

    def __iter__(self):
        if self.level is not None:
            yield from (path for path in self.paths_at_depth(self.level) if not self._excluded(path) and path not in self.extra)
        yield from self.extra

    def add(self, path):
        if not self._base(path) or self._excluded(path):
            self.extra.add(path)
        self._count = None

    def discard(self, path):
        self.extra.discard(path)
        if self._base(path):
            self.unfolded.add(path)
        self._count = None

    def clear(self):
        self.level = None
        self.extra.clear()
        self.unfolded.clear()
        self._count = 0

    def expand(self, path):
        before = len(self)
        self.extra = {p for p in self.extra if not self._under(p, path)}
        self.unfolded.add(path)
        self._count = None
        return len(self) != before

    def order(self):
        from .display_order import DisplayOrder, merged_ranges
        hierarchy, catalog = self.hierarchy, self.hierarchy.catalog
        if self.level is None:
            order = DisplayOrder(catalog, [range(len(catalog))])
        else:
            def visible():
                for depth in range(self.level):
                    for path in self.paths_at_depth(depth, with_signals=True):
                        yield from catalog.scope_ranges(path)
                for path in self.unfolded:
                    node = hierarchy.get(path)
                    if node is not None:
                        yield from hierarchy.ranges(node, ordered=False)
            order = DisplayOrder(catalog, merged_ranges(visible()))
        for path in self.extra:
            node = hierarchy.get(path)
            if node is not None:
                order.remove_ranges(hierarchy.ranges(node, ordered=False))
        return order


def fold_state(folds):
    if isinstance(folds, ScopeFolds):
        return dict(collapsed_scopes=sorted(folds.extra), collapsed_scope_level=folds.level,
                    unfolded_scopes=sorted(folds.unfolded))
    return dict(collapsed_scopes=sorted(folds))


def restore_folds(root, data):
    level = data.get('collapsed_scope_level')
    extra = data.get('collapsed_scopes', ())
    unfolded = data.get('unfolded_scopes', ())
    if level != 2 or root is None:
        return set(extra)
    if hasattr(root, 'hierarchy'):
        return ScopeFolds(root.hierarchy, level, extra, unfolded)
    from .hierarchy import walk_scopes
    return set(extra) | {node.full_path for node in walk_scopes(root)
                        if node.full_path.count('.') == level - 1 and
                        not any(ScopeFolds._under(node.full_path, p) for p in unfolded)}
