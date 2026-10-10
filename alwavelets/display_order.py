"""Editable display order backed by catalog ranges instead of signal-id copies.

Memory follows the number of edits/runs. Arbitrarily fragmented selections can
still require many runs; an untouched catalog, or a contiguous scope, needs one.
"""
from bisect import bisect_right
from collections.abc import MutableSequence, MutableSet
import operator


def merged_ranges(ranges):
    out = []
    for part in sorted(ranges, key=lambda r: r.start):
        if not part:
            continue
        if out and part.start <= out[-1].stop:
            out[-1] = range(out[-1].start, max(out[-1].stop, part.stop))
        else:
            out.append(part)
    return out


def subtract(part, masks, ends):
    start = part.start
    for i in range(bisect_right(ends, start), len(masks)):
        mask = masks[i]
        if mask.start >= part.stop:
            break
        if start < mask.start:
            yield range(start, mask.start)
        start = max(start, mask.stop)
        if start >= part.stop:
            return
    if start < part.stop:
        yield range(start, part.stop)


class DisplayOrder(MutableSequence):
    """List-compatible runs of declaration ordinals and explicit virtual rows."""
    def __init__(self, catalog, parts=()):
        self.catalog = catalog
        self._set_parts(parts)

    @classmethod
    def from_order(cls, catalog, order):
        if isinstance(order, cls) and order.catalog is catalog:
            return order.copy()
        if order is catalog.order:
            return cls(catalog, [range(len(catalog))])
        return cls(catalog, (range(n, n + 1) if n is not None else (item,)
                             for item, n in catalog.ordinal_items(order)))

    def _item_part(self, item):
        ordinal = self.catalog.ordinal(item) if isinstance(item, str) else None
        return range(ordinal, ordinal + 1) if ordinal is not None else (item,)

    def _set_parts(self, parts):
        compact = []
        for part in parts:
            if not part:
                continue
            if compact and isinstance(part, range) and isinstance(compact[-1], range) and compact[-1].stop == part.start:
                compact[-1] = range(compact[-1].start, part.stop)
            elif compact and isinstance(part, tuple) and isinstance(compact[-1], tuple) and len(compact[-1]) + len(part) <= 256:
                compact[-1] += part
            else:
                compact.append(part)
        self.parts = compact
        self.starts, self.size = [], 0
        for part in compact:
            self.starts.append(self.size)
            self.size += len(part)
        self._selected = None

    def copy(self):
        return type(self)(self.catalog, self.parts)

    def __len__(self):
        return self.size

    def _parts_between(self, start, stop):
        if start >= stop:
            return
        first = max(0, bisect_right(self.starts, start) - 1)
        for i in range(first, len(self.parts)):
            base, part = self.starts[i], self.parts[i]
            if base >= stop:
                break
            yield part[max(0, start - base):min(len(part), stop - base)]

    def _position(self, index):
        index = operator.index(index)
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return index

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            return list(self.iter_range(start, stop))
        index = self._position(index)
        piece = bisect_right(self.starts, index) - 1
        part = self.parts[piece]
        value = part[index - self.starts[piece]]
        return self.catalog.order[value] if isinstance(part, range) else value

    def iter_range(self, start, stop):
        for part in self._parts_between(start, stop):
            if isinstance(part, range):
                yield from (row[0] for row in self.catalog.rows('sid', 'seq>=? AND seq<?', (part.start, part.stop)))
            else:
                yield from part

    def __iter__(self):
        return self.iter_range(0, len(self))

    def names(self, extras):
        for part in self.parts:
            if isinstance(part, range):
                yield from (row[0].lower() for row in self.catalog.rows('full_name', 'seq>=? AND seq<?', (part.start, part.stop)))
            else:
                for sid in part:
                    sig = extras.get(sid)
                    yield sig.full_name.lower() if sig is not None else ''

    def index(self, item, start=0, stop=None):
        start, stop, _ = slice(start, stop).indices(len(self))
        ordinal = self.catalog.ordinal(item) if isinstance(item, str) else None
        for base, part in zip(self.starts, self.parts):
            if base >= stop:
                break
            if isinstance(part, range):
                if ordinal is not None and ordinal in part:
                    found = base + ordinal - part.start
                    if start <= found < stop:
                        return found
            else:
                try:
                    return base + part.index(item, max(0, start - base), max(0, stop - base))
                except ValueError:
                    pass
        raise ValueError('%r is not in display order' % (item,))

    def __contains__(self, item):
        try:
            self.index(item)
            return True
        except ValueError:
            return False

    def __eq__(self, other):
        if isinstance(other, DisplayOrder) and other.catalog is self.catalog:
            return self.parts == other.parts
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def splice(self, start, stop, parts=()):
        before = list(self._parts_between(0, start))
        after = list(self._parts_between(stop, len(self)))
        self._set_parts(before + list(parts) + after)

    def insert(self, index, item):
        index = operator.index(index)
        index = max(0, len(self) + index) if index < 0 else min(index, len(self))
        self.splice(index, index, [self._item_part(item)])

    def __delitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step == 1:
                self.splice(start, max(start, stop))
            else:
                for i in sorted(range(start, stop, step), reverse=True):
                    del self[i]
        else:
            index = self._position(index)
            self.splice(index, index + 1)

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step == 1:
                parts = value.parts if isinstance(value, DisplayOrder) and value.catalog is self.catalog else [self._item_part(v) for v in value]
                self.splice(start, max(start, stop), parts)
            else:
                indices, values = range(start, stop, step), list(value)
                if len(indices) != len(values):
                    raise ValueError('extended slice size mismatch')
                for i, v in zip(indices, values):
                    self[i] = v
        else:
            index = self._position(index)
            self.splice(index, index + 1, [self._item_part(value)])

    def selected_ranges(self):
        if self._selected is None:
            self._selected = merged_ranges(p for p in self.parts if isinstance(p, range))
        return self._selected

    def remove_ranges(self, ranges):
        masks = merged_ranges(ranges)
        ends = [p.stop for p in masks]
        self._set_parts(piece for part in self.parts for piece in
                        (subtract(part, masks, ends) if isinstance(part, range) else [part]))

    def add_ranges(self, ranges):
        # Caller supplies disjoint ranges in the desired append order.
        masks = self.selected_ranges()
        ends = [p.stop for p in masks]
        self._set_parts(self.parts + [piece for part in ranges for piece in subtract(part, masks, ends)])

    def contains_ranges(self, ranges):
        masks = self.selected_ranges()
        if masks == [range(len(self.catalog))]:
            return True
        ends = [p.stop for p in masks]
        return all(next(subtract(part, masks, ends), None) is None for part in ranges)

    def remove_ids(self, ids):
        ordinals, extras = [], set()
        for item, ordinal in self.catalog.ordinal_items(ids):
            if ordinal is None:
                extras.add(item)
            else:
                ordinals.append(ordinal)
        masks = []
        for ordinal in sorted(set(ordinals)):
            if masks and masks[-1].stop == ordinal:
                masks[-1] = range(masks[-1].start, ordinal + 1)
            else:
                masks.append(range(ordinal, ordinal + 1))
        self.remove_ranges(masks)
        if extras:
            self._set_parts(tuple(item for item in part if item not in extras) if isinstance(part, tuple) else part
                            for part in self.parts)


class DisplayMembership(MutableSet):
    """A live membership view; UI list and set updates remain idempotent."""
    def __init__(self, order):
        self.order = order

    def __len__(self):
        return sum(len(p) for p in self.order.selected_ranges()) + len({v for p in self.order.parts if isinstance(p, tuple) for v in p})

    def __iter__(self):
        for part in self.order.selected_ranges():
            yield from (row[0] for row in self.order.catalog.rows('sid', 'seq>=? AND seq<?', (part.start, part.stop)))
        yield from {v for p in self.order.parts if isinstance(p, tuple) for v in p}

    def __contains__(self, item):
        return item in self.order

    def add(self, item):
        if item not in self.order:
            self.order.append(item)

    def discard(self, item):
        self.order.remove_ids([item])

    def difference_update(self, items):
        self.order.remove_ids(items)


def copy_order(order):
    return order.copy() if isinstance(order, DisplayOrder) else list(order)


def remove_display_ids(order, ids):
    if isinstance(order, DisplayOrder):
        order.remove_ids(ids)
    else:
        gone = set(ids)
        order[:] = [sid for sid in order if sid not in gone]


def member_positions(order, members):
    positions = []
    for sid in members:
        start = 0
        while True:
            try:
                index = order.index(sid, start)
            except ValueError:
                break
            positions.append((index, sid))
            start = index + 1
    return sorted(positions)


def grouped_rows(order, member_group, groups, chain_for):
    """Overlay only group members/headers; untouched catalog runs stay shared."""
    parts, headed, cursor = [], set(), 0
    for index, sid in member_positions(order, member_group):
        parts.extend(order._parts_between(cursor, index))
        chain = chain_for(member_group[sid])
        for i in range(len(chain) - 1, -1, -1):
            if any(groups[a]['collapsed'] for a in chain[i + 1:]):
                break
            name = chain[i]
            if name not in headed:
                headed.add(name)
                parts.append((('\x01hdr', name),))
        if not any(groups[a]['collapsed'] for a in chain):
            parts.extend(order._parts_between(index, index + 1))
        cursor = index + 1
    parts.extend(order._parts_between(cursor, len(order)))
    return DisplayOrder(order.catalog, parts)
