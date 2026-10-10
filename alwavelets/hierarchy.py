"""Scope indexes and virtual tree rows, without one UI object per signal."""
from bisect import bisect_right
from collections.abc import Sequence


def walk_scopes(root):
    """Visit scopes in declaration order without Python recursion."""
    if hasattr(root, 'hierarchy'):
        yield from root.hierarchy.walk(root)
        return
    yield root
    stack = [iter(root.children.values())]
    while stack:
        node = next(stack[-1], None)
        if node is None:
            stack.pop()
        else:
            yield node
            stack.append(iter(node.children.values()))


def scope_signal_ids(root):
    for node in walk_scopes(root):
        yield from node.signal_ids


class ScopeIndex:
    """Counts and scope lookup, paged when the trace uses a disk catalog.

    The declaration tree is immutable for a loaded trace. A reload supplies a
    new root and creates a new index; selection and expansion reuse this one.
    """
    def __init__(self, root):
        self.root = root
        if hasattr(root, 'hierarchy'):
            from .scope_store import ScopeCounts
            self.nodes, self.counts = root.hierarchy, ScopeCounts(root.hierarchy)
            return
        self.nodes = {node.full_path: node for node in walk_scopes(root)}
        self.counts = {}
        for node in reversed(list(self.nodes.values())):
            self.counts[node] = len(node.signal_ids) + sum(self.counts[child] for child in node.children.values())


class TreeRows(Sequence):
    """Random-access rows backed by scope runs and existing signal-id lists."""
    def __new__(cls, scopes, expanded, get_signal, is_selected):
        if hasattr(scopes.root, 'hierarchy'):
            from .scope_store import PagedTreeRows
            return PagedTreeRows(scopes, expanded, get_signal, is_selected)
        return super().__new__(cls)

    def __init__(self, scopes, expanded, get_signal, is_selected):
        self.scopes = scopes
        self.get_signal, self.is_selected = get_signal, is_selected
        self.starts, self.runs = [], []
        self.size = 0
        root = scopes.root
        if root.signal_ids:
            self._append(root, 0, True, False)
        stack = [(iter(root.children.values()), 0)]
        while stack:
            children, depth = stack[-1]
            node = next(children, None)
            if node is None:
                stack.pop()
                continue
            opened = node.full_path in expanded
            self._append(node, depth, opened, True)
            if opened:
                stack.append((iter(node.children.values()), depth + 1))

    def _append(self, node, depth, opened, header):
        self.starts.append(self.size)
        self.runs.append((node, depth, opened, header))
        self.size += int(header) + (len(node.signal_ids) if opened else 0)

    def __len__(self):
        return self.size

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.size))]
        if index < 0:
            index += self.size
        if not 0 <= index < self.size:
            raise IndexError(index)
        run = bisect_right(self.starts, index) - 1
        node, depth, opened, header = self.runs[run]
        offset = index - self.starts[run]
        if header and offset == 0:
            return 'scope', node, depth, opened, self.scopes.counts[node]
        sid = node.signal_ids[offset - int(header)]
        return 'signal', self.get_signal(sid), depth + int(header), self.is_selected(sid), sid


def scope_all_selected(root, selected, cache):
    """Cache subtree checkmarks across redraws and expansion changes."""
    stack = [(root, None)]
    while stack:
        node, children = stack[-1]
        path = node.full_path
        if path in cache:
            stack.pop()
            if not cache[path] and stack:
                cache[stack[-1][0].full_path] = False
            continue
        if children is None:
            if not all(sid in selected for sid in node.signal_ids):
                cache[path] = False
                continue
            children = iter(node.children.values())
            stack[-1] = node, children
        child = next(children, None)
        if child is None:
            cache[path] = True
        else:
            stack.append((child, None))
    return cache[root.full_path]


def scope_ranges(root, catalog, *, ordered=True):
    if hasattr(root, 'hierarchy'):
        yield from root.hierarchy.ranges(root, ordered=ordered)
    else:
        for node in walk_scopes(root):
            yield from catalog.scope_ranges(node.full_path)
