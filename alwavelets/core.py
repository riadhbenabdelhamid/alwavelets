"""Core data structures and value formatting.

The foundation the rest of the package builds on — signals, radixes,
translation maps, IEEE-754 decoding, and value formatting. Depends only
on the standard library.
"""
import array
import bisect
import json
import math
import re
import sys
from enum import Enum


# ══════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════

# ── packed scalar value storage ──────────────────────────────────────
# Single-bit signals store _values as a bytearray (1 byte/change) instead of a
# list of interned-string pointers (~8 bytes/change). _B2S maps a stored byte
# back to its interned logic string; _S2B maps a logic string to its byte.
_B2S = ['x'] * 256
for _b, _s in ((48, '0'), (49, '1'), (120, 'x'), (122, 'z'), (88, 'x'), (90, 'z')):
    _B2S[_b] = sys.intern(_s)
_S2B = {'0': 48, '1': 49, 'x': 120, 'z': 122, 'X': 120, 'Z': 122}

# ── packed bus value storage ─────────────────────────────────────────
# Multi-bit (2..64 wide, non-real) signals pack each value into a flat
# bytearray at (size+3)//4 bytes per change — 2 bits per logic char, base-4:
# 0→00 1→01 x→10 z→11 — instead of a list of interned-string pointers
# (8 B/change plus ~50-90 B per unique string). Encoding rides int(v, 4),
# which reads '0'/'1' directly; x/z go through the _T4 translation first.
# High-entropy wide buses (data/address) shrink ~4-16x; encode speed matches
# or beats sys.intern (see the memo below). Values that can't pack (chars
# outside 01xz, or wider than the declared size) permanently de-pack the
# signal back to string storage via _depack().
#
# Lock-free-read note: a signal can de-pack while the UI thread reads it, so
# readers never trust _packed — they snapshot _values once and decide how to
# decode from the snapshot's own type (bytearray = packed, list = strings).
_T4 = str.maketrans('xzXZ', '2323')
_B4Q = [''.join('01xz'[(b >> s) & 3] for s in (6, 4, 2, 0)) for b in range(256)]

# Encode memo, one dict per stride, shared by the signals of that stride.
# Keys are x/z-extension-normalized value strings, so they are stride-exact.
# Only signals ≤16 bits use it (bounded: ≤64k binary values per stride; the
# cap guards pathological x/z mixes). For wider buses a memo would just miss —
# and the plain int(v,4) encode already beats interning there.
_PACK_MEMOS = {}
_MEMO_CAP = 65536


def clear_pack_memos():
    """Release the encode memos once a parse is done.

    They only pay off while values are streaming in, but a filled stride-4
    memo is ~13 MB that would otherwise sit there for the process lifetime —
    material next to a packed trace's whole resident size. Cleared in place
    (not rebound) because live signals hold references, and a later append
    from follow/live-reload simply refills what it needs.
    """
    for memo in _PACK_MEMOS.values():
        memo.clear()


def numeric_value(value, signal_type="wire"):
    """Interpret a value using its declaration, never its digit spelling."""
    try:
        if signal_type in ('real', 'realtime'):
            n = float(value)
            return n if math.isfinite(n) else None
        return int(value, 2)
    except (ValueError, TypeError, OverflowError):
        return None


def time_bisect_left(times, timestamp):
    lookup = getattr(times, 'bisect', None)
    return lookup(timestamp) if lookup else bisect.bisect_left(times, timestamp)


def time_bisect_right(times, timestamp):
    lookup = getattr(times, 'bisect', None)
    return lookup(timestamp, True) if lookup else bisect.bisect_right(times, timestamp)


def ensure_trace_loaded(vcd, start=0, end=None):
    """Honor an optional storage-loading hook on a trace-like object.

    In-memory library callers need only supply signals and time metadata;
    VCDFile additionally guarantees complete coverage through this hook.
    """
    load = getattr(vcd, 'require_loaded', None)
    if load is not None:
        load(start, end)


def split_reference(reference):
    """Separate a whitespace-delimited VCD/FST reference and declared range."""
    m = re.fullmatch(r'(.*?)\s+\[\s*(-?\d+)\s*(?::\s*(-?\d+)\s*)?\]', reference)
    if not m:
        return reference, None
    return m[1], (int(m[2]), int(m[3] if m[3] is not None else m[2]))


class VCDSignal:
    """
    Signal with C-level array for timestamps and interned value strings.
    Uses __slots__ for minimal per-instance overhead.
    """
    __slots__ = ('name', 'full_name', 'identifier', 'size',
                 'reference', 'declared_range',
                 'signal_type', 'scope', '_times', '_values', '_scalar',
                 '_packed', '_stride', '_memo',
                 'analog_mode', 'is_dumped', '_parent_sig', '_bit_idx')
    # False on AliasSignal: storage loops (sort/compact/dumpoff) skip aliases.
    owns_storage = True

    def __init__(self, name, identifier, size=1, signal_type="wire", scope="",
                 declared_range=None, *, metadata_only=False):
        self.reference = name
        self.declared_range = declared_range
        # Keep familiar names for conventional buses; disambiguate slices
        # and separately declared bits in every name-based consumer.
        if declared_range is not None and (size == 1 or declared_range != (size - 1, 0)):
            hi, lo = declared_range
            name += '[%d]' % hi if hi == lo else '[%d:%d]' % (hi, lo)
        self.name = name
        self.full_name = f"{scope}.{name}" if scope else name
        self.identifier = identifier
        self.size = size
        self.signal_type = signal_type
        self.scope = scope
        self._times = () if metadata_only else array.array('q')
        # Single-bit signals pack values into a bytearray (1 byte/change);
        # 2..64-bit buses pack into a flat bytearray at _stride bytes/change
        # (see "packed bus value storage" above); reals and very wide buses
        # keep a list of interned value strings.
        self._scalar = (size == 1 and signal_type not in ('real', 'realtime'))
        self._packed = (1 < size <= 64 and signal_type not in ('real', 'realtime'))
        if self._packed:
            self._stride = (size + 3) // 4
            self._memo = (_PACK_MEMOS.setdefault(self._stride, {})
                          if size <= 16 else None)
            self._values = bytearray()
        else:
            self._stride = 0
            self._memo = None
            self._values = bytearray() if self._scalar else []
        self.analog_mode = (signal_type in ('real', 'realtime'))
        self.is_dumped = True             # tracks $dumpoff/$dumpon state
        self._parent_sig = None           # set for expanded bus bits
        self._bit_idx = None              # bit index for expanded bus bits
        if metadata_only:
            self._values = ()

    def bit_label(self, offset):
        if self.declared_range is None:
            return offset
        hi, lo = self.declared_range
        return lo + offset if hi >= lo else lo - offset

    def identity(self):
        return {'name': (self.scope + '.' if self.scope else '') + self.reference,
                'size': self.size, 'type': self.signal_type,
                'range': list(self.declared_range) if self.declared_range is not None else None}

    @property
    def changes(self):
        vals = self._values
        if self._scalar:
            return [(t, _B2S[v]) for t, v in zip(self._times, vals)]
        if type(vals) is bytearray:            # packed bus (see decode note)
            n = min(len(self._times), len(vals) // self._stride)
            return list(zip(self._times, self._decode_slice(vals, 0, n)))
        return list(zip(self._times, vals))

    def add_change(self, time, value):
        # Lock-free-read contract: the background parser may append here while
        # the UI thread reads via bisect() on _times. Appending the value
        # BEFORE the timestamp guarantees that, under CPython's GIL, any index
        # a reader derives from _times already exists in _values (len(_values)
        # is always >= len(_times) during the transient). This avoids a
        # per-read lock on the rendering hot path. See VCDFile._load_lock.
        if self._scalar:
            self._values.append(_S2B.get(value, 120))
        else:
            self._append_value(value)
        self._times.append(time)

    def _append_value(self, v, intern=True):
        """Append one non-scalar value: encode into the packed store, or
        intern into the string list. Callers append the timestamp AFTER this
        (the add_change lock-free contract). Parser hot loops call this
        directly to skip add_change's scalar branch."""
        if self._packed:
            if not v:
                # A malformed/truncated dump can emit an empty payload ("b !").
                # The packed form can't represent it, so de-pack to string
                # storage and keep it verbatim — the pre-packing parser
                # interned it happily, and raising here would abort the open.
                self._depack()
            else:
                # VCD left-extends a short x/z value with that char (a short
                # 0/1 value zero-extends, which int() already does). Normalize
                # before the memo so keys are exact for every size sharing
                # this stride.
                if len(v) < self.size and (v[0] == 'x' or v[0] == 'z'
                                           or v[0] == 'X' or v[0] == 'Z'):
                    v = v[0] * (self.size - len(v)) + v
                memo = self._memo
                if memo is not None:
                    b = memo.get(v)
                    if b is not None:
                        self._values.extend(b)
                        return
                try:
                    try:
                        b = int(v, 4).to_bytes(self._stride, 'big')
                    except ValueError:
                        b = int(v.translate(_T4), 4).to_bytes(self._stride, 'big')
                except (ValueError, OverflowError):
                    self._depack()             # falls through to string store
                else:
                    if memo is not None and len(memo) < _MEMO_CAP:
                        memo[v] = b
                    self._values.extend(b)
                    return
        self._values.append(sys.intern(v) if intern and len(v) < 64 else v)

    def _encode_values(self, strs):
        """Bulk-encode an iterable of value strings into a packed bytearray,
        or None if any value can't be packed (caller should _depack)."""
        out = bytearray()
        st, size = self._stride, self.size
        try:
            for v in strs:
                if len(v) < size and v[0] in 'xzXZ':
                    v = v[0] * (size - len(v)) + v
                try:
                    out.extend(int(v, 4).to_bytes(st, 'big'))
                except ValueError:
                    out.extend(int(v.translate(_T4), 4).to_bytes(st, 'big'))
        except (ValueError, OverflowError, IndexError):
            return None
        return out

    def _depack(self):
        """Permanently fall back to string storage (a value arrived that the
        packed form can't hold). Builds the full string list first and swaps
        it in with one assignment, so concurrent readers — which key off the
        container type of their _values snapshot — stay consistent."""
        vals = self._values
        n = len(vals) // self._stride
        self._values = [sys.intern(s) for s in self._decode_slice(vals, 0, n)]
        self._packed = False

    def _decode1(self, vals, i):
        """Decode packed change `i` from the `vals` snapshot to a full-width
        logic string."""
        o = i * self._stride
        return ''.join(map(_B4Q.__getitem__, vals[o:o + self._stride]))[-self.size:]

    def _decode_slice(self, vals, lo, hi):
        """Decode packed changes [lo, hi) from the `vals` snapshot to a list
        of full-width logic strings (one join for the whole run)."""
        st = self._stride
        quads = ''.join(map(_B4Q.__getitem__, vals[lo * st:hi * st]))
        w = st * 4
        off = w - self.size
        return [quads[i * w + off:(i + 1) * w] for i in range(hi - lo)]

    def _sort_by_time(self):
        """Re-sort changes by timestamp (repairs out-of-order dumps). Rebinds
        _values before _times to keep the lock-free-read contract."""
        vals = self._values
        if self._scalar:
            pairs = sorted(zip(self._times, vals), key=lambda p: p[0])
            new_values = bytearray(p[1] for p in pairs)
        elif type(vals) is bytearray:          # packed bus: sort stride chunks
            st = self._stride
            chunks = [bytes(vals[i * st:(i + 1) * st])
                      for i in range(len(vals) // st)]
            pairs = sorted(zip(self._times, chunks), key=lambda p: p[0])
            new_values = bytearray(b''.join(p[1] for p in pairs))
        else:
            pairs = sorted(zip(self._times, vals), key=lambda p: p[0])
            new_values = [p[1] for p in pairs]
        self._values = new_values
        self._times = array.array(self._times.typecode, [p[0] for p in pairs])

    def num_changes(self):
        return len(self._times)

    def value_str(self, i):
        """String logic value at change index `i` (decodes packed storage)."""
        vals = self._values
        if self._scalar:
            return _B2S[vals[i]]
        if type(vals) is bytearray:
            return self._decode1(vals, i)
        return vals[i]

    def value_at(self, time):
        """O(log n) lookup via bisect."""
        if not self._times:
            return 'x'
        idx = time_bisect_right(self._times, time) - 1
        if idx < 0:
            return 'x'
        vals = self._values
        if self._scalar:
            return _B2S[vals[idx]]
        if type(vals) is bytearray:
            return self._decode1(vals, idx)
        return vals[idx]

    def changes_in_window(self, t_start, t_end):
        """O(log n) window extraction with one-before for initial value."""
        if not self._times:
            return [], []
        lo = time_bisect_left(self._times, t_start)
        if lo > 0:
            lo -= 1
        hi = time_bisect_right(self._times, t_end)
        vals = self._values
        if hasattr(vals, 'iter_range'):
            from .storage import SequenceView
            vals = SequenceView(vals, range(lo, hi), chr if self._scalar else None)
        elif self._scalar:
            vals = [_B2S[b] for b in vals[lo:hi]]
        elif type(vals) is bytearray:
            vals = self._decode_slice(vals, lo, hi)
        else:
            vals = vals[lo:hi]
        return self._times[lo:hi], vals

    def iter_changes(self, start=0, end=None):
        lo = time_bisect_left(self._times, start)
        hi = len(self._times) if end is None else time_bisect_right(self._times, end)
        for i in range(lo, hi):
            yield self._times[i], self.value_str(i)

    def next_edge_after(self, time):
        """Find the next transition time after `time`. Returns None if none."""
        if not self._times:
            return None
        idx = time_bisect_right(self._times, time)
        if idx < len(self._times):
            return self._times[idx]
        return None

    def prev_edge_before(self, time):
        """Find the previous transition time before `time`. Returns None if none."""
        if not self._times:
            return None
        idx = time_bisect_left(self._times, time) - 1
        if idx >= 0:
            return self._times[idx]
        return None

    def find_value(self, target, start_time=0):
        """Find the next time >= start_time where signal equals target. Returns None if not found."""
        if not self._times:
            return None
        idx = time_bisect_left(self._times, start_time)
        # Check current value at start_time
        cur_idx = time_bisect_right(self._times, start_time) - 1
        if cur_idx >= 0 and self._match_value(self.value_str(cur_idx), target):
            return start_time
        # Scan forward through changes
        for i in range(max(idx, 0), len(self._times)):
            if self._match_value(self.value_str(i), target):
                return self._times[i]
        return None

    @staticmethod
    def _match_value(val, target):
        target = target.strip().lower()
        val_lower = val.lower()
        if val_lower == target:
            return True
        # Try numeric comparison
        try:
            if val_lower.startswith('0x'):
                v = int(val_lower, 16)
            else:
                v = int(val_lower, 2)
            if target.startswith('0x'):
                t = int(target, 16)
            elif target.startswith('0b'):
                t = int(target, 2)
            else:
                t = int(target)
            return v == t
        except (ValueError, TypeError):
            return False

    def numeric_value_at(self, time):
        """Get numeric (float) value at time, for analog rendering.

        The declared type controls interpretation: real/realtime values use
        decimal notation; logic vectors use unsigned binary."""
        return numeric_value(self.value_at(time), self.signal_type)

    def numeric_range_in_window(self, t_start, t_end):
        """Get (min, max) numeric values in window for analog scaling.

        Values follow the declared signal type, matching numeric_value_at."""
        times, values = self.changes_in_window(t_start, t_end)
        mn = mx = None
        for raw in values:
            value = numeric_value(raw, self.signal_type)
            if value is not None:
                mn = value if mn is None else min(mn, value)
                mx = value if mx is None else max(mx, value)
        if mn is None:
            return (0, 1)
        if mn == mx:
            return (mn - 1, mx + 1)
        return (mn, mx)

    def detect_frequency(self):
        """Auto-detect clock period by measuring intervals between same-direction edges.
        Returns (period, frequency_hz) or (None, None) if not periodic."""
        if len(self._times) < 6:
            return None, None
        # For single-bit: find rising edges (0→1)
        if self.size == 1:
            edges = []
            vals = self._values
            one, zero = (49, 48) if self._scalar else ('1', '0')
            for i in range(1, min(len(self._times), 200)):
                if vals[i] == one and vals[i-1] == zero:
                    edges.append(self._times[i])
            if len(edges) < 3:
                return None, None
            # Compute deltas
            deltas = [edges[i+1] - edges[i] for i in range(len(edges)-1)]
            if not deltas:
                return None, None
            median = sorted(deltas)[len(deltas)//2]
            if median <= 0:
                return None, None
            # Check consistency (within 10%)
            consistent = sum(1 for d in deltas if abs(d - median) <= median * 0.1)
            if consistent < len(deltas) * 0.7:
                return None, None
            return median, None  # period in timescale units
        return None, None

    def transition_count_in_window(self, t_start, t_end):
        """Count number of transitions in window."""
        if not self._times:
            return 0
        lo = time_bisect_left(self._times, t_start)
        hi = time_bisect_right(self._times, t_end)
        return max(0, hi - lo)

    def find_pattern(self, pattern_val, other_sigs_conditions, start_time=0):
        """Find time where this signal matches pattern_val AND other conditions hold.
        other_sigs_conditions: list of (VCDSignal, target_value) pairs.
        Returns time or None."""
        for i in range(time_bisect_left(self._times, start_time), len(self._times)):
            t = self._times[i]
            if not self._match_value(self.value_str(i), pattern_val):
                continue
            all_match = True
            for other_sig, other_val in other_sigs_conditions:
                if not self._match_value(other_sig.value_at(t), other_val):
                    all_match = False
                    break
            if all_match:
                return t
        return None

    def bit_value_at(self, time, bit_idx):
        """Get single bit from bus at time. bit_idx 0 = LSB."""
        val = self.value_at(time)
        if val in ('x', 'z'):
            return val
        try:
            if bit_idx < len(val):
                return val[-(bit_idx + 1)]  # VCD binary is MSB-first
            return '0'
        except (IndexError, TypeError):
            return 'x'

    def make_expanded_bit(self, bit_idx):
        """Create a virtual 1-bit VCDSignal for bit_idx of this bus."""
        bit_sig = VCDSignal(
            f"{self.reference}[{self.bit_label(bit_idx)}]",
            f"{self.identifier}__bit{bit_idx}",
            size=1, signal_type="wire", scope=self.scope
        )
        bit_sig._parent_sig = self
        bit_sig._bit_idx = bit_idx
        # Build the bit's change list from parent (changes decodes packed
        # storage to logic strings)
        prev_bit = None
        for t, v in self.changes:
            if v in ('x', 'z'):
                bv = v
            elif bit_idx < len(v):
                bv = v[-(bit_idx + 1)]
            else:
                bv = '0'
            if bv != prev_bit:
                bit_sig.add_change(t, bv)
                prev_bit = bv
        return bit_sig


class AliasSignal(VCDSignal):
    """A second name for another signal's storage.

    VCD reuses one identifier code for a net that is visible from several
    scopes, and FST marks such variables as aliases of an earlier handle. The
    alias owns no storage: every read goes to the master, so de-packing,
    re-sorting and timestamp compaction happen once, on the master, and the
    two can never drift apart. Appends are keyed by identifier and only ever
    reach the master; storage-mutating loops skip aliases via owns_storage.
    """
    __slots__ = ('master',)
    owns_storage = False

    def __init__(self, name, identifier, size, signal_type, scope, master,
                 declared_range=None):
        self.master = master
        self.reference = name
        self.declared_range = declared_range
        if declared_range is not None and (size == 1 or declared_range != (size - 1, 0)):
            hi, lo = declared_range
            name += '[%d]' % hi if hi == lo else '[%d:%d]' % (hi, lo)
        self.name = name
        self.full_name = f"{scope}.{name}" if scope else name
        self.identifier = identifier
        self.size = size
        self.signal_type = signal_type
        self.scope = scope
        self.analog_mode = (signal_type in ('real', 'realtime'))
        self.is_dumped = True
        self._parent_sig = None
        self._bit_idx = None

    _times = property(lambda self: self.master._times)
    _values = property(lambda self: self.master._values)
    _scalar = property(lambda self: self.master._scalar)
    _packed = property(lambda self: self.master._packed)
    _stride = property(lambda self: self.master._stride)
    _memo = property(lambda self: self.master._memo)

    def changes_in_window(self, start, end):
        if hasattr(self.master, 'store'):
            return self.master.changes_in_window(start, end)
        return super().changes_in_window(start, end)

    def iter_changes(self, start=0, end=None):
        return self.master.iter_changes(start, end)

    def numeric_range_in_window(self, start, end):
        return self.master.numeric_range_in_window(start, end)


class Radix(Enum):
    BIN = "bin"
    HEX = "hex"
    DEC = "dec"
    UNSIGNED = "uns"
    FLOAT16 = "f16"
    FLOAT32 = "f32"
    FLOAT64 = "f64"


class TimeScaledSignal(AliasSignal):
    """Read-only signal view on another trace's time axis (exact ratios)."""
    __slots__ = ('_scaled_times',)

    def __init__(self, signal, ratio):
        super().__init__(signal.reference, signal.identifier, signal.size,
                         signal.signal_type, signal.scope, signal, signal.declared_range)
        from .storage import SequenceView
        self._scaled_times = SequenceView(signal._times, transform=lambda t: t * ratio)

    _times = property(lambda self: self._scaled_times)
    changes_in_window = VCDSignal.changes_in_window
    iter_changes = VCDSignal.iter_changes
    numeric_range_in_window = VCDSignal.numeric_range_in_window


class Marker:
    __slots__ = ('time', 'label')
    def __init__(self, time, label=""):
        self.time = time
        self.label = label


class VirtualBitSignal:
    """Pseudo-signal extracting bit N from a parent bus signal."""
    __slots__ = ('name', 'full_name', 'identifier', 'size', 'signal_type',
                 'scope', 'analog_mode', 'is_dumped', 'parent', 'bit_index')

    def __init__(self, parent, bit_index):
        self.parent = parent
        self.bit_index = bit_index
        self.name = f"{parent.reference}[{parent.bit_label(bit_index)}]"
        self.full_name = f"{parent.scope}.{self.name}" if parent.scope else self.name
        self.identifier = f"{parent.identifier}__bit{bit_index}"
        self.size = 1
        self.signal_type = "wire"
        self.scope = parent.scope
        self.analog_mode = False
        self.is_dumped = True

    @property
    def _times(self):
        return self.parent._times

    @property
    def _values(self):
        # NB: raw storage — for a packed parent this is the packed bytearray,
        # not strings. Use value_str/changes_in_window for decoded values.
        return self.parent._values

    def num_changes(self):
        return self.parent.num_changes()

    def value_at(self, time):
        val = self.parent.value_at(time)
        return self._extract_bit(val)

    def _extract_bit(self, val):
        if val in ('x', 'z'):
            return val
        if any(c in val for c in 'xXzZ'):
            idx = len(val) - 1 - self.bit_index
            if 0 <= idx < len(val):
                c = val[idx].lower()
                return c if c in '01xz' else 'x'
            return 'x'
        try:
            int_val = int(val, 2)
            return '1' if (int_val >> self.bit_index) & 1 else '0'
        except (ValueError, TypeError):
            return 'x'

    def changes_in_window(self, t_start, t_end):
        times, values = self.parent.changes_in_window(t_start, t_end)
        if hasattr(self.parent._times, 'bisect'):
            from .storage import SequenceView
            return times, SequenceView(values, transform=self._extract_bit)
        return times, [self._extract_bit(v) for v in values]

    def next_edge_after(self, time):
        """Find next transition in extracted bit."""
        if not self.parent._times:
            return None
        idx = time_bisect_right(self.parent._times, time)
        cur = self.value_at(time)
        for i in range(idx, len(self.parent._times)):
            nxt = self._extract_bit(self.parent.value_str(i))
            if nxt != cur:
                return self.parent._times[i]
            cur = nxt
        return None

    def iter_changes(self, start=0, end=None):
        for t, value in self.parent.iter_changes(start, end):
            yield t, self._extract_bit(value)

    def value_str(self, index):
        return self._extract_bit(self.parent.value_str(index))

    def prev_edge_before(self, time):
        if not self.parent._times:
            return None
        idx = time_bisect_left(self.parent._times, time) - 1
        if idx < 0:
            return None
        # The bit takes value_str(i) at _times[i], so the edge between change
        # i and i+1 is AT _times[i+1] — returning _times[i] here used to
        # report the previous edge one bus-change too early whenever the bit
        # held its value across changes.
        cur = self._extract_bit(self.parent.value_str(idx))
        for i in range(idx - 1, -1, -1):
            v = self._extract_bit(self.parent.value_str(i))
            if v != cur:
                return self.parent._times[i + 1]
            cur = v
        return None

    def transition_count_in_window(self, t_start, t_end):
        times, values = self.changes_in_window(t_start, t_end)
        if len(values) < 2:
            return 0
        count = 0
        for i in range(1, len(values)):
            if values[i] != values[i-1]:
                count += 1
        return count

    def numeric_value_at(self, time):
        v = self.value_at(time)
        if v == '1': return 1.0
        if v == '0': return 0.0
        return None

    def numeric_range_in_window(self, t_start, t_end):
        return (0, 1)

    def detect_frequency(self):
        return None, None

    def find_value(self, target, start_time=0):
        for i in range(time_bisect_left(self.parent._times, start_time), len(self.parent._times)):
            if self._extract_bit(self.parent.value_str(i)) == target:
                return self.parent._times[i]
        return None


class TranslationMap:
    """Maps signal values to human-readable names (enum/state decode)."""
    def __init__(self):
        self.maps = {}  # signal_name -> {binary_value: label}

    def add(self, signal_name, value_map):
        self.maps[signal_name] = value_map

    def translate(self, signal_name, value, size=1):
        """Translate a value if mapping exists. Returns label or None."""
        if signal_name not in self.maps:
            return None
        m = self.maps[signal_name]
        # Try direct match
        if value in m:
            return m[value]
        # Try as integer
        try:
            int_val = int(value, 2)
            for k, v in m.items():
                try:
                    if int(k) == int_val:
                        return v
                except ValueError:
                    pass
            # Try str(int_val)
            if str(int_val) in m:
                return m[str(int_val)]
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def load_file(filename):
        """Load translations from JSON file.
        Format: { "signal_name": { "0": "IDLE", "1": "RUN", "2": "STOP" }, ... }
        """
        tm = TranslationMap()
        with open(filename) as f:
            data = json.load(f)
        for sig_name, value_map in data.items():
            tm.add(sig_name, {str(k): str(v) for k, v in value_map.items()})
        return tm


# ══════════════════════════════════════════════════════════════
#  VALUE FORMATTING
# ══════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════
#  IEEE 754 FLOAT DECODE
# ══════════════════════════════════════════════════════════════

# Format specs: (total_bits, exp_bits, mantissa_bits, exp_bias)
IEEE754_FORMATS = {
    Radix.FLOAT16: (16, 5, 10, 15),
    Radix.FLOAT32: (32, 8, 23, 127),
    Radix.FLOAT64: (64, 11, 52, 1023),
}


def ieee754_decode(bits_int, fmt):
    """
    Decode IEEE 754 float from integer bit pattern.
    Returns (float_val, sign, exponent_raw, exponent_real, mantissa_raw, special)
    where special is None, 'inf', '-inf', 'nan', '+0', '-0', or 'denorm'.
    """
    total, exp_bits, man_bits, bias = IEEE754_FORMATS[fmt]

    sign = (bits_int >> (total - 1)) & 1
    exp_raw = (bits_int >> man_bits) & ((1 << exp_bits) - 1)
    man_raw = bits_int & ((1 << man_bits) - 1)

    exp_real = exp_raw - bias

    # Special cases
    if exp_raw == 0 and man_raw == 0:
        special = '-0' if sign else '+0'
        return (0.0, sign, exp_raw, exp_real, man_raw, special)
    elif exp_raw == 0:
        # Denormalized
        fval = (-1) ** sign * (man_raw / (1 << man_bits)) * (2 ** (1 - bias))
        return (fval, sign, exp_raw, exp_real, man_raw, 'denorm')
    elif exp_raw == (1 << exp_bits) - 1:
        if man_raw == 0:
            special = '-inf' if sign else 'inf'
            return (float('-inf') if sign else float('inf'), sign, exp_raw, exp_real, man_raw, special)
        else:
            return (float('nan'), sign, exp_raw, exp_real, man_raw, 'nan')

    # Normal number
    fval = (-1) ** sign * (1.0 + man_raw / (1 << man_bits)) * (2 ** exp_real)
    return (fval, sign, exp_raw, exp_real, man_raw, None)


def format_real(val):
    """Format a real-valued ('r' line) string compactly: six significant
    digits, scientific notation when needed. Never truncates the text, which
    used to turn '1.2345e-05' into '1.2345e-'."""
    try:
        return f"{float(val):.6g}"
    except (ValueError, OverflowError):
        return val


def format_float_value(val, fmt, size=1):
    """Format a binary value string as IEEE 754 float."""
    if any(c in val for c in 'xXzZ'):
        return val.upper()
    try:
        bits = int(val, 2)
    except ValueError:
        return format_real(val)

    fval, sign, exp_raw, exp_real, man_raw, special = ieee754_decode(bits, fmt)

    if special == 'nan':
        return 'NaN'
    elif special in ('inf', '-inf'):
        return special
    elif special in ('+0', '-0'):
        return special

    # Format the float compactly
    if abs(fval) == 0:
        return '0.0'
    elif abs(fval) >= 1e6 or abs(fval) < 1e-3:
        return f"{fval:.4e}"
    else:
        s = f"{fval:.6g}"
        return s


def format_float_decomposed(val, fmt, size=1):
    """Format showing float value + S|E|M decomposition."""
    if any(c in val for c in 'xXzZ'):
        return val.upper()
    try:
        bits = int(val, 2)
    except ValueError:
        return format_real(val)

    total, exp_bits, man_bits, bias = IEEE754_FORMATS[fmt]
    fval, sign, exp_raw, exp_real, man_raw, special = ieee754_decode(bits, fmt)

    # Float value part
    if special == 'nan':
        fstr = 'NaN'
    elif special in ('inf', '-inf'):
        fstr = special
    elif special in ('+0', '-0'):
        fstr = special
    elif abs(fval) >= 1e6 or (abs(fval) < 1e-3 and abs(fval) > 0):
        fstr = f"{fval:.3e}"
    else:
        fstr = f"{fval:.5g}"

    # Decomposition
    exp_hex = f"{exp_raw:0{(exp_bits+3)//4}X}"
    man_hex = f"{man_raw:0{(man_bits+3)//4}X}"
    sign_ch = '-' if sign else '+'

    return f"{fstr} [{sign_ch}|E:{exp_hex}|M:{man_hex}]"


def make_float_field_signals(parent_sig, fmt):
    """
    Create virtual signals for IEEE 754 field decomposition.
    Returns list of (field_name, VCDSignal) for sign, exponent, mantissa.
    """
    total, exp_bits, man_bits, bias = IEEE754_FORMATS[fmt]
    fmt_name = {Radix.FLOAT16: 'f16', Radix.FLOAT32: 'f32', Radix.FLOAT64: 'f64'}[fmt]
    results = []

    # Sign bit
    sign_sig = VCDSignal(
        f"{parent_sig.name}.sign", f"{parent_sig.identifier}__fsign",
        size=1, signal_type="wire", scope=parent_sig.scope
    )
    # Exponent field
    exp_sig = VCDSignal(
        f"{parent_sig.name}.exp", f"{parent_sig.identifier}__fexp",
        size=exp_bits, signal_type="wire", scope=parent_sig.scope
    )
    # Mantissa field
    man_sig = VCDSignal(
        f"{parent_sig.name}.mant", f"{parent_sig.identifier}__fmant",
        size=man_bits, signal_type="wire", scope=parent_sig.scope
    )
    # Decoded float value (as real)
    float_sig = VCDSignal(
        f"{parent_sig.name}.value", f"{parent_sig.identifier}__fval",
        size=1, signal_type="real", scope=parent_sig.scope
    )
    float_sig.analog_mode = True

    # Extract fields from parent's changes (decodes packed storage)
    for t, v in parent_sig.changes:
        if any(c in v for c in 'xXzZ'):
            sign_sig.add_change(t, 'x')
            exp_sig.add_change(t, 'x')
            man_sig.add_change(t, 'x')
            float_sig.add_change(t, 'x')
            continue
        try:
            bits = int(v, 2)
        except ValueError:
            continue

        s = (bits >> (total - 1)) & 1
        e = (bits >> man_bits) & ((1 << exp_bits) - 1)
        m = bits & ((1 << man_bits) - 1)

        sign_sig.add_change(t, str(s))
        exp_sig.add_change(t, format(e, f'0{exp_bits}b'))
        man_sig.add_change(t, format(m, f'0{man_bits}b'))

        fval, _, _, _, _, _ = ieee754_decode(bits, fmt)
        float_sig.add_change(t, f"{fval}")

    results = [
        ('sign', sign_sig),
        ('exp', exp_sig),
        ('mant', man_sig),
        ('value', float_sig),
    ]
    return results


def format_value(val, radix, size=1):
    if val in ('x', 'z', '0', '1') and size == 1:
        return val.upper()
    if any(c in val for c in 'xXzZ'):
        return val.upper()

    # IEEE 754 float formats
    if radix in (Radix.FLOAT16, Radix.FLOAT32, Radix.FLOAT64):
        return format_float_decomposed(val, radix, size)

    try:
        int_val = int(val, 2)
    except ValueError:
        return format_real(val)
    if radix == Radix.BIN:
        return val
    elif radix == Radix.HEX:
        return f"{int_val:0{(size+3)//4}X}"
    elif radix == Radix.DEC:
        # Two's complement: the sign bit is bit size-1 of the value, which is
        # only the string's first character when the string is full width.
        # String-stored values (wide or de-packed buses) can be shorter —
        # VCD zero-extends them — so '1' in an 8-bit bus is +1, not -255.
        if size > 1 and len(val) >= size and val[-size] == '1':
            int_val -= (1 << size)
        return str(int_val)
    elif radix == Radix.UNSIGNED:
        return str(int_val)
    return val


def format_typed_value(sig, val, radix):
    if getattr(sig, 'signal_type', 'wire') in ('real', 'realtime'):
        return format_real(val)
    return format_value(val, radix, sig.size)


def format_value_decoded(val, decode_map, radix, size=1):
    """Format value with decode map (enum decode). Falls back to format_value."""
    if decode_map:
        # Try raw value
        if val in decode_map:
            return decode_map[val]
        # Try hex/dec/int lookup
        try:
            int_val = int(val, 2)
            hex_key = f"0x{int_val:X}"
            dec_key = str(int_val)
            if hex_key in decode_map:
                return decode_map[hex_key]
            if dec_key in decode_map:
                return decode_map[dec_key]
            if str(int_val) in decode_map:
                return decode_map[str(int_val)]
        except (ValueError, TypeError):
            pass
    return format_value(val, radix, size)


def xml_escape(s):
    """Escape a string for safe inclusion in SVG/XML text content."""
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def pdf_escape(s):
    """Escape a string for a PDF literal string: backslash and parentheses."""
    return (str(s).replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)'))
