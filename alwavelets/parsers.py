"""VCD and FST parsing: the streaming VCD parser (header / background /
lazy), the native pure-Python FST reader, the scope tree, and the
parse_file dispatcher. Depends on the stdlib and core.VCDSignal.
"""
import array
import bisect
import os
import re
import shutil
import subprocess
import threading
import tempfile
from collections import OrderedDict

from .core import VCDSignal, AliasSignal, clear_pack_memos, split_reference
from .value_stream import ValueState, states_at_offsets
from .fst import FSTReader  # noqa: F401  (re-exported; the reader lives in fst.py)

# First byte of a scalar value-change line -> normalised value byte for the
# packed bytearray store ('X'->'x', 'Z'->'z'). Lets the hot parse loop append a
# byte directly (no chr().lower()/intern per change). See VCDSignal._scalar.
_BITBYTE = {48: 48, 49: 49, 120: 120, 88: 120, 122: 122, 90: 122}


class ConverterError(RuntimeError):
    """An external format converter (fst2vcd, lxt2vcd, …) is missing or
    failed. Raised instead of exiting so library users (the MCP server, a
    REPL) get an exception; the CLI turns it into an error message."""


def _declare_var(vcd, parts, scope, metadata_only=False):
    """Register one `$var` line. A repeated identifier code is an alias (one
    net seen from several scopes — Icarus does this), so it becomes an
    AliasSignal sharing the first declaration's storage instead of being
    silently dropped. Alias keys contain a space, which an id never does."""
    sig_id, size, stype, name = parts[3], _safe_int(parts[2]), parts[1], parts[4]
    reference = ' '.join(parts[4:]).split('$end', 1)[0].strip()
    name, declared_range = split_reference(reference)
    declare = getattr(vcd.signals, 'declare', None)
    if declare is not None:
        declare(name, sig_id, size, stype, scope, declared_range)
        return
    master = vcd.signals.get(sig_id)
    if master is None:
        vcd.signals[sig_id] = VCDSignal(name, sig_id, size, stype, scope, declared_range,
                                      metadata_only=metadata_only)
        vcd.signal_order.append(sig_id)
    elif (master.reference, master.scope, master.declared_range) != (name, scope, declared_range):
        key = f"{sig_id} #{len(vcd.signal_order)}"
        vcd.signals[key] = AliasSignal(name, sig_id, size, stype, scope, master, declared_range)
        vcd.signal_order.append(key)


def _safe_int(s, default=1):
    """int() that tolerates malformed input (returns default on garbage)."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


_I32_MAX = 0x7FFFFFFF
_I32_OK = array.array('i').itemsize >= 4   # 'i' is 4 bytes on all common platforms


def _compact_times(vcd):
    """Downcast each signal's timestamp array from 64-bit to 32-bit when the
    whole trace fits in a signed 32-bit range. Halves timestamp memory with no
    lookup penalty (a flat array still bisects in O(log n)). No-op for traces
    whose max time needs 64 bits, or on exotic platforms where 'i' isn't 4 bytes.

    Called at the end of every parse path, so it is also where the bus-packing
    encode memos are released (see core.clear_pack_memos).
    """
    clear_pack_memos()
    if not _I32_OK or vcd.max_time > _I32_MAX:
        return
    for sig in vcd.signals.values():
        if not sig.owns_storage:
            continue
        t = sig._times
        if t.typecode != 'i' and len(t):
            sig._times = array.array('i', t)


def _convert_trace(filename, converter, progress_cb=None, *, timeout=120,
                   storage='auto', memory_mib=256, jobs=None):
    """Spool converter output to private files, never PIPE-sized RAM buffers."""
    try:
        with tempfile.TemporaryDirectory(prefix='alwavelets-convert-') as directory:
            converted = os.path.join(directory, 'trace.vcd')
            with open(converted, 'wb') as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run([converter, os.fspath(filename)], stdout=output,
                                        stderr=errors, timeout=timeout)
                if result.returncode:
                    errors.seek(0)
                    detail = errors.read(65537)
                    suffix = ' [stderr truncated]' if len(detail) > 65536 else ''
                    raise ConverterError('%s failed: %s%s' % (
                        os.path.basename(converter), detail[:65536].decode('utf-8', 'replace').strip(), suffix))
            if os.path.getsize(converted) < 10:
                raise ConverterError('%s produced empty output' % os.path.basename(converter))
            # A disk index is self-contained when parsing returns. It survives
            # deletion of the converted source until the returned trace closes.
            trace = parse_file(converted, progress_cb, use_cache=False, jobs=jobs,
                               storage=storage, memory_mib=memory_mib)
            trace._filename = os.fspath(filename)
            return trace
    except subprocess.TimeoutExpired as exc:
        raise ConverterError('%s timed out (>%ds)' % (os.path.basename(converter), timeout)) from exc
    except ConverterError:
        raise
    except Exception as exc:
        raise ConverterError('%s: %s' % (os.path.basename(converter), exc)) from exc


def _fst_via_converter(filename, progress_cb=None, storage='auto', memory_mib=256):
    """Convert FST to VCD using fst2vcd, then parse with the chosen storage."""
    converter = shutil.which('fst2vcd')
    if not converter:
        raise ConverterError(
            "'fst2vcd' not found. Install GTKWave tools for FST support "
            "(Ubuntu/Debian: sudo apt install gtkwave), or convert first: "
            "fst2vcd input.fst > output.vcd")
    return _convert_trace(filename, converter, progress_cb, timeout=300,
                          storage=storage, memory_mib=memory_mib)


def parse_file(filename, progress_cb=None, use_cache=True, jobs=None,
               storage='auto', memory_mib=256):
    """Universal reader with persistent caching and optional disk storage.

    Auto storage pages files above 50 MiB, wide headers, and large FST expansion
    to disk, keeping all signal declarations available. memory_mib bounds the decoded-page cache, not total
    process memory. Close the returned trace (or use a with statement) when done.
    storage='memory' preserves the eager reader and full parse-cache path.
    jobs opts into parallel eager VCD parsing (None or 1 = sequential).
    """
    from .storage import use_disk_storage, open_trace
    if use_disk_storage(filename, storage):
        return open_trace(filename, memory_mib=memory_mib, use_cache=use_cache,
                          progress_cb=progress_cb)
    # Converted size is unknown until the converter finishes. An old eager
    # cache must not bypass auto/disk storage for these formats.
    converted_format = os.path.splitext(filename)[1].lower() in ('.lxt', '.lxt2', '.ghw', '.vzt')
    eager_cache = use_cache and (storage == 'memory' or not converted_format)
    if eager_cache:
        from . import cache
        cached = cache.load_cache(filename)
        if cached is not None:
            return cached
    vcd = _parse_file_uncached(filename, progress_cb, jobs, storage, memory_mib)
    if eager_cache and vcd is not None and getattr(vcd, 'signal_order', None):
        from . import cache
        cache.save_cache(vcd, filename)
    return vcd


def _parse_vcd(filename, progress_cb, jobs):
    """Parse a VCD, using parallel workers for large files when worthwhile,
    falling back to the sequential parser on any problem."""
    try:
        from . import parallel
        if parallel.viable(filename, jobs):
            vcd = parallel.parse_parallel_file(filename, jobs)
            if vcd is not None:
                return vcd
    except Exception:
        pass
    return VCDFile.parse(filename, progress_cb)


def _parse_file_uncached(filename, progress_cb=None, jobs=None, storage='auto', memory_mib=256):
    """
    Universal file parser: auto-detects VCD vs FST by content magic bytes.
    Falls back to external converters for LXT/GHW/VZT.
    """
    ext = os.path.splitext(filename)[1].lower()

    # ── Detect by content first (extension can lie) ──
    try:
        with open(filename, 'rb') as f:
            first_byte = f.read(1)
        if first_byte and first_byte[0] == 0x24:  # '$' = VCD header
            return _parse_vcd(filename, progress_cb, jobs)
    except Exception:
        pass

    # ── Native FST (verified by magic bytes) ──
    if FSTReader.is_fst(filename):
        try:
            vcd = FSTReader(filename).parse(progress_cb)
            if vcd and vcd.signal_order:
                return vcd
            print("  [Native FST: 0 signals, trying fst2vcd converter...]")
        except Exception as e:
            print(f"  [Native FST failed: {e}, trying fst2vcd converter...]")

        # ── Fallback: use fst2vcd converter ──
        return _fst_via_converter(filename, progress_cb, storage, memory_mib)

    # ── Native VCD (fallback for unknown extensions) ──
    if ext in ('.vcd', '') or ext not in ('.lxt', '.lxt2', '.ghw', '.vzt'):
        return _parse_vcd(filename, progress_cb, jobs)

    # ── External converter for legacy formats ──
    converters = {'.lxt': 'lxt2vcd', '.lxt2': 'lxt2vcd', '.ghw': 'ghw2vcd', '.vzt': 'vzt2vcd'}
    converter = converters.get(ext)
    if not converter:
        return VCDFile.parse(filename, progress_cb)

    converter_path = shutil.which(converter)
    if not converter_path:
        raise ConverterError(f"'{converter}' not found (needed for {ext} files)")

    return _convert_trace(filename, converter_path, progress_cb, storage=storage,
                          memory_mib=memory_mib, jobs=jobs)


# ══════════════════════════════════════════════════════════════
#  SCOPE TREE
# ══════════════════════════════════════════════════════════════

class ScopeNode:
    """Tree node for scope hierarchy (used for folding/browsing)."""
    __slots__ = ('name', 'full_path', 'children', 'signal_ids', 'collapsed')
    def __init__(self, name, full_path=""):
        self.name = name
        self.full_path = full_path
        self.children = OrderedDict()
        self.signal_ids = []
        self.collapsed = False

    def total_signals(self):
        from .hierarchy import walk_scopes
        return sum(len(node.signal_ids) for node in walk_scopes(self))


def build_scope_tree(vcd):
    """Build a scope hierarchy tree from parsed VCD signals."""
    catalog_tree = getattr(vcd.signals, 'scope_tree', None)
    if catalog_tree is not None:
        return catalog_tree()
    root = ScopeNode("", "")
    for sig_id in vcd.signal_order:
        sig = vcd.signals[sig_id]
        parts = sig.scope.split('.') if sig.scope else []
        node = root
        path = ""
        for part in parts:
            path = f"{path}.{part}" if path else part
            if part not in node.children:
                node.children[part] = ScopeNode(part, path)
            node = node.children[part]
        node.signal_ids.append(sig_id)
    return root


# ══════════════════════════════════════════════════════════════
#  STREAMING VCD PARSER (robust, fast)
# ══════════════════════════════════════════════════════════════

class VCDFile:
    def __init__(self):
        self.timescale = "1ns"
        self.timescale_number = 1
        self.timescale_unit = "ns"
        self.date = ""
        self.version = ""
        self.signals = OrderedDict()
        self.signal_order = []
        self.max_time = 0
        self.min_time = 0
        self._filename = ""
        self._extent_known = True
        self.scopes = None  # ScopeNode tree, built after parse

        # ── Background loading state ──
        self._loading = False
        self._load_progress = 0
        self._load_thread = None
        self._load_lock = threading.Lock()
        self._load_error = None
        self._load_changes = 0
        self._resume_offset = 0

        # ── Lazy chunk loading state ──
        self._time_index = None
        self._values_offset = 0
        self._loaded_ranges = []   # coalesced (start, end) intervals already loaded (lazy mode)
        self._lazy_mode = False
        self._tail_offset = None   # live 'follow' tail position (None = not following)

    @property
    def is_loading(self):
        return self._loading

    @property
    def tick_fs(self):
        return self.timescale_number * {'fs': 1, 'ps': 1000, 'ns': 1000000,
                                       'us': 1000000000, 'ms': 1000000000000,
                                       's': 1000000000000000}[self.timescale_unit]

    @property
    def load_progress(self):
        return self._load_progress

    @property
    def load_changes(self):
        return self._load_changes

    def time_to_str(self, ticks):
        """Format ticks as human-readable time string using timescale."""
        units = {'fs':1e-15,'ps':1e-12,'ns':1e-9,'us':1e-6,'ms':1e-3,'s':1.0}
        base = units.get(self.timescale_unit, 1e-9) * self.timescale_number
        seconds = ticks * base
        if seconds == 0:
            return f"0{self.timescale_unit}"
        for unit, scale in [('s',1.0),('ms',1e-3),('us',1e-6),('ns',1e-9),('ps',1e-12),('fs',1e-15)]:
            if abs(seconds) >= scale * 0.999:
                val = round(seconds / scale, 3)
                if val == int(val):
                    return f"{int(val)}{unit}"
                s = f"{val:.3f}".rstrip('0').rstrip('.')
                return f"{s}{unit}"
        return f"{ticks}"

    # ══ Phase 1: Header-only parse (synchronous, <100ms) ══

    @staticmethod
    def parse_header(filename, *, metadata_only=False, catalog=None):
        """Parse VCD header only. Returns (vcd, values_offset)."""
        vcd = VCDFile()
        if catalog is not None:
            from .catalog import attach_catalog
            attach_catalog(catalog.store, vcd, catalog)
        elif metadata_only:
            vcd.signals = {}  # insertion order without linked-list overhead
        vcd._filename = filename
        from .analysis_cache import stamp_source
        stamp_source(vcd, filename)
        vcd._extent_known = False
        scope_stack = []
        current_scope = ""

        with open(filename, 'rb') as f:
            for raw_line in f:
                if len(raw_line) <= 1:
                    continue
                line = raw_line.decode('ascii', errors='ignore').rstrip('\n\r')
                if not line.strip():
                    continue
                line = line.strip()
                if not line or line[0] != '$':
                    continue
                if line.startswith('$timescale'):
                    ts = _extract_block(line, '$timescale', _TextWrapper(f))
                    vcd.timescale = ts
                    m = re.match(r'(\d+)\s*(s|ms|us|ns|ps|fs)', ts)
                    if m:
                        vcd.timescale_number = int(m.group(1))
                        vcd.timescale_unit = m.group(2)
                elif line.startswith('$scope'):
                    parts = line.split()
                    if len(parts) >= 3:
                        scope_stack.append(parts[2])
                        current_scope = ".".join(scope_stack)
                elif line.startswith('$upscope'):
                    if scope_stack:
                        scope_stack.pop()
                        current_scope = ".".join(scope_stack)
                elif line.startswith('$var'):
                    parts = line.split()
                    if len(parts) >= 5:
                        _declare_var(vcd, parts, current_scope, metadata_only)
                elif line.startswith('$enddefinitions'):
                    break
                elif line.startswith('$comment'):
                    _extract_block(line, '$comment', _TextWrapper(f))
                elif line.startswith('$date'):
                    vcd.date = _extract_block(line, '$date', _TextWrapper(f))
                elif line.startswith('$version'):
                    vcd.version = _extract_block(line, '$version', _TextWrapper(f))

            vcd._values_offset = f.tell()

        vcd.scopes = build_scope_tree(vcd)
        return vcd, vcd._values_offset

    # ══ Phase 2a: Background full value parse ══

    def _append_event(self, bid, value, time):
        if bid is None:
            for sig in self.signals.values():
                if sig.owns_storage:
                    sig.add_change(time, 'x')
            return sum(s.owns_storage for s in self.signals.values())
        sig = self._value_signals.get(bid)
        if sig is None:
            return 0
        sig.add_change(time, value[-1:].lower() if sig._scalar else value)
        return 1

    def _read_values(self, offset, progress_cb=None):
        self._value_signals = {k.encode('ascii'): s for k, s in self.signals.items()
                               if s.owns_storage}
        state = ValueState()
        feed = state.feed
        bget = self._value_signals.get
        logic = _BITBYTE
        active, current_time = True, 0
        size = os.path.getsize(self._filename)
        pos, reported, changes = offset, offset, 0
        with open(self._filename, 'rb', buffering=1 << 20) as f:
            f.seek(offset)
            for raw in f:
                pos += len(raw)
                # Scalar lines cannot alter directive state. Avoid creating
                # an intermediate event/string on this common storage path.
                c = raw[0]
                if active and c in logic:
                    sig = bget(raw[1:].strip())
                    if sig is not None:
                        if sig._scalar:
                            sig._values.append(logic[c])
                        else:
                            sig._append_value(chr(logic[c]))
                        sig._times.append(current_time)
                        changes += 1
                elif c == 35 and not state.comment:
                    # Timestamp-heavy sparse traces should not pay payload
                    # dispatch or recompute unchanged dump/comment state.
                    feed(raw)
                    current_time = state.time
                elif active and c in (98, 66, 114, 82):
                    # As with scalar lines, payloads cannot affect directive
                    # state. Split once and store directly; only decode text
                    # for signals that need it (b-form scalars do not).
                    parts = raw[1:].split(None, 1)
                    if len(parts) == 2:
                        payload, bid = parts[0], parts[1].strip()
                    elif raw[1:2] in (b' ', b'\t'):
                        payload, bid = b'', raw[1:].strip()
                    else:
                        continue
                    sig = bget(bid)
                    if sig is not None:
                        if sig._scalar:
                            sig._values.append(logic.get(payload[-1] if payload else 120, 120))
                        else:
                            sig._append_value(payload.decode('ascii', 'ignore'))
                        sig._times.append(current_time)
                        changes += 1
                else:
                    # Shared decoder owns all timestamp/directive semantics,
                    # including indented lines and multiline comments.
                    event = feed(raw)
                    active = state.dump and not state.comment
                    current_time = state.time
                    if event is not None:
                        changes += self._append_event(*event, current_time)
                if pos - reported >= 1 << 20:
                    self.max_time = state.max_time
                    reported = pos
                    self._load_progress = min(99, pos * 100 // max(1, size))
                    self._load_changes = changes
                    if progress_cb:
                        progress_cb(pos, size)
        if not state.monotonic:
            for sig in self.signals.values():
                if sig.owns_storage:
                    sig._sort_by_time()
        self._parsed_offset = pos
        self.max_time = state.max_time
        self._value_state = state
        self._extent_known = True
        self._load_changes = changes
        self._load_progress = 100
        _compact_times(self)
        if progress_cb:
            progress_cb(pos, size)

    def _parse_values_background(self, offset):
        try:
            self._read_values(offset)
            from . import cache
            cache.save_cache(self, self._filename)
        except Exception as e:
            self._load_error = str(e)
        finally:
            self._loading = False

    # ══ Phase 2b: Lazy time index build (for huge files) ══

    # Sparse timestamp boundaries keep the index small on multi-GB traces.
    # Cold windows may scan earlier blocks to recover held signal values.
    _INDEX_STRIDE = 64 * 1024

    def _build_time_index(self, offset):
        """Index timestamp boundaries and the directive state at each one.

        First-change times let a cold window find predecessor values without
        searching for signals that have never changed. No values are retained.
        """
        size = os.path.getsize(self._filename)
        state = ValueState()
        index = [(0, offset)]
        states = {offset: state.snapshot()}
        first = {}
        unseen = {k.encode('ascii') for k, s in self.signals.items() if s.owns_storage}
        pos = last_indexed = reported = offset
        feed = state.feed
        try:
            with open(self._filename, 'rb', buffering=1 << 20) as f:
                f.seek(offset)
                for raw in f:
                    line = raw.lstrip() if raw[0] <= 32 else raw
                    c = line[:1]
                    if state.comment:
                        feed(line)
                    elif c == b'#':
                        before = state.snapshot() if pos - last_indexed >= self._INDEX_STRIDE else None
                        feed(line)
                        if before is not None and state.time > before[0]:
                            index.append((state.time, pos))
                            states[pos] = before
                            last_indexed = pos
                        self.max_time = state.max_time
                    elif c == b'$':
                        # Value payloads are irrelevant to indexing once a
                        # signal's first change is known. Still consume every
                        # directive so comments and dump blackouts carry over.
                        event = feed(line)
                        if event is not None:
                            bid, _ = event
                            if bid is None:
                                for key in unseen:
                                    first[key] = state.time
                                unseen.clear()
                    elif unseen and state.dump:
                        # Undumped declarations may stay unseen forever.
                        # Extract identifiers without decoding bus/real values.
                        bid = None
                        if line and line[0] in _BITBYTE:
                            bid = line[1:].strip()
                        elif c in (b'b', b'B', b'r', b'R'):
                            parts = line[1:].split(None, 1)
                            if len(parts) == 2:
                                bid = parts[1].strip()
                            elif line[1:2] in (b' ', b'\t'):
                                bid = line[1:].strip()
                        if bid in unseen:
                            first[bid] = state.time
                            unseen.remove(bid)
                    pos += len(raw)
                    if pos - reported >= 1 << 20:
                        reported = pos
                        self._load_progress = min(99, pos * 100 // max(1, size))
            if not state.monotonic:
                # Bisect indexes require monotonic timestamps. The full
                # reader can repair malformed out-of-order traces safely.
                self._read_values(offset)
                return
            self._time_index = index
            self._ti_times = [t for t, _ in index]
            self._index_states = states
            self._first_changes = first
            self._lazy_mode = True
            self._loaded_ranges = []
            self._extent_known = True
            self._load_progress = 100
        except Exception as e:
            self._load_error = str(e)
        finally:
            self._loading = False

    def require_loaded(self, t_start=0, t_end=None):
        """Ensure an analysis interval is complete, or explain why it is not."""
        if self._load_error:
            raise ValueError("Trace loading failed: " + self._load_error)
        if self.is_loading or not self._extent_known:
            raise ValueError("Trace is still loading; retry the analysis when loading finishes")
        self.ensure_loaded(t_start, self.max_time if t_end is None else t_end)

    def close(self):
        """Cancel indexing and release disk pages and temporary storage."""
        for worker in tuple(getattr(self, '_finder_workers', ())):
            worker.close()
        for results in tuple(getattr(self, '_result_stores', ())):
            results.close()
        for derived in getattr(self, '_derived_stores', ()):
            derived.close()
        store = getattr(self, '_store', None)
        if store is not None:
            store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def ensure_loaded(self, t_start, t_end):
        """Ensure value changes in [t_start, t_end] are loaded into memory.

        Only the sub-ranges not already loaded are parsed, and the parsed data
        is merged into each signal's arrays in sorted order. This keeps the
        per-signal _times arrays monotonic regardless of navigation order, so
        the bisect-based readers stay correct even when the user jumps
        backwards in time.
        """
        if not self._lazy_mode or not self._time_index:
            return
        t_start = int(t_start)
        t_end = int(t_end)
        gaps = self._uncovered_ranges(t_start, t_end)
        if not gaps:
            return
        for gs, ge in gaps:
            start_off, end_off = self._chunk_offsets(gs, ge)
            chunk = self._parse_chunk(start_off, end_off, gs, ge)
            self._merge_chunk(chunk)
        self._mark_loaded(t_start, t_end)

    def _chunk_offsets(self, t_start, t_end):
        """Map a time window to a [start, end) byte range using the time
        index: from the entry before the first timestamp >= t_start to the
        entry after the last timestamp <= t_end (O(log n), two bisects)."""
        idx = self._time_index
        if not idx:
            return self._values_offset, os.path.getsize(self._filename)
        tt = getattr(self, '_ti_times', None)
        if tt is None or len(tt) != len(idx):
            tt = self._ti_times = [t for t, _ in idx]
        i = bisect.bisect_left(tt, t_start)
        start_off = idx[max(0, i - 1)][1]
        j = bisect.bisect_right(tt, t_end)
        end_off = idx[j][1] if j < len(idx) else os.path.getsize(self._filename)
        return start_off, end_off

    def _uncovered_ranges(self, t_start, t_end):
        """Sub-ranges of [t_start, t_end] not yet covered by _loaded_ranges.

        Boundaries are allowed to overlap loaded ranges by a point; the merge
        step dedups any duplicate timestamps, so coverage gaps are impossible.
        """
        gaps = [(t_start, t_end)]
        for ls, le in self._loaded_ranges:
            next_gaps = []
            for gs, ge in gaps:
                if le < gs or ls > ge:
                    next_gaps.append((gs, ge))          # disjoint
                    continue
                if gs < ls:
                    next_gaps.append((gs, ls))          # left remainder
                if ge > le:
                    next_gaps.append((le, ge))          # right remainder
            gaps = next_gaps
            if not gaps:
                break
        return gaps

    def _mark_loaded(self, t_start, t_end):
        """Record [t_start, t_end] as loaded, coalescing adjacent intervals."""
        ranges = sorted(self._loaded_ranges + [(t_start, t_end)])
        merged = [ranges[0]]
        for s, e in ranges[1:]:
            ms, me = merged[-1]
            if s <= me:
                merged[-1] = (ms, max(me, e))
            else:
                merged.append((s, e))
        self._loaded_ranges = merged

    def _merge_chunk(self, chunk):
        """Merge freshly parsed per-signal data into the signals' sorted arrays.

        `chunk` maps sig_id -> (times, values), each already sorted ascending.
        Entries whose timestamp already exists in the signal are skipped, so
        overlapping reloads never create duplicates.
        """
        for sig_id, (nt, nv) in chunk.items():
            if not nt:
                continue
            sig = self.signals.get(sig_id)
            if sig is None:
                continue
            ot, ov = sig._times, sig._values
            # Packed buses: encode the incoming strings once up front. If any
            # value can't pack, the signal permanently de-packs to strings.
            packed = (not sig._scalar) and type(ov) is bytearray
            env = None
            if packed:
                env = sig._encode_values(nv)
                if env is None:
                    sig._depack()
                    ov = sig._values
                    packed = False
            if not ot:
                # nv holds bytes for scalar signals, strings for buses.
                if sig._scalar:
                    sig._values = bytearray(nv)
                else:
                    sig._values = env if packed else list(nv)
                sig._times = array.array('q', nt)
                continue
            # Fast paths: new data entirely after / before the existing data.
            if nt[0] > ot[-1]:
                ov.extend(env if packed else nv)     # values before times
                ot.extend(nt)
                continue
            if nt[-1] < ot[0]:
                if sig._scalar:
                    new_values = bytearray(nv) + ov
                elif packed:
                    new_values = env + ov
                else:
                    new_values = list(nv) + ov
                new_times = array.array('q', nt)
                new_times.extend(ot)
                sig._values = new_values
                sig._times = new_times
                continue
            # General merge of two sorted sequences, deduped by timestamp.
            # For packed buses, merge in string space and re-encode at the end
            # (this path is rare: only overlapping reloads land here).
            if packed:
                ov = sig._decode_slice(ov, 0, len(ov) // sig._stride)
            mt = array.array('q')
            mv = bytearray() if sig._scalar else []
            i = j = 0
            li, lj = len(ot), len(nt)
            while i < li and j < lj:
                a, b = ot[i], nt[j]
                if a < b:
                    mt.append(a); mv.append(ov[i]); i += 1
                elif a > b:
                    mt.append(b); mv.append(nv[j]); j += 1
                else:                                   # same timestamp: keep existing
                    mt.append(a); mv.append(ov[i]); i += 1; j += 1
            while i < li:
                mt.append(ot[i]); mv.append(ov[i]); i += 1
            while j < lj:
                mt.append(nt[j]); mv.append(nv[j]); j += 1
            if packed:
                mv = sig._encode_values(mv)             # can't fail: inputs packed
            sig._values = mv
            sig._times = mt

    def _parse_chunk(self, start_offset, end_offset, t_start, t_end):
        """Load complete timestamp groups and the predecessor of each signal.

        Cold seeks walk earlier index blocks only for unresolved predecessors.
        A baseline retains the entire last timestamp group, so an overlapping
        load cannot erase delta-cycle events or change their order.
        """
        masters = {k.encode('ascii'): s for k, s in self.signals.items() if s.owns_storage}
        result = {}

        def read_range(start, end, wanted):
            state = ValueState(*self._index_states[start])
            found = {}
            with open(self._filename, 'rb', buffering=1 << 20) as f:
                f.seek(start)
                pos = start
                while pos < end:
                    raw = f.readline()
                    if not raw:
                        break
                    pos += len(raw)
                    event = state.feed(raw)
                    if event is None or state.time > t_end:
                        continue
                    bid, value = event
                    keys = wanted if bid is None else (bid,)
                    for key in keys:
                        if key not in wanted:
                            continue
                        ts, vs = found.setdefault(key, ([], []))
                        if state.time < t_start and ts and ts[-1] != state.time:
                            ts.clear()
                            vs.clear()
                        ts.append(state.time)
                        vs.append(_BITBYTE.get(ord(value[-1:]) if value else 120, 120)
                                  if masters[key]._scalar else value)
            return found

        result.update(read_range(start_offset, end_offset, masters))
        missing = {k for k, t in self._first_changes.items() if t < t_start
                   and (k not in result or result[k][0][0] >= t_start)}
        offsets = [off for _, off in self._time_index]
        block = bisect.bisect_left(offsets, start_offset)
        while missing and block > 0:
            older = read_range(offsets[block - 1], offsets[block], missing)
            for key, (ts, vs) in older.items():
                if key in result:
                    result[key] = (ts + result[key][0], vs + result[key][1])
                else:
                    result[key] = (ts, vs)
            missing.difference_update(older)
            block -= 1
        return {k.decode('ascii'): v for k, v in result.items()}

    # ══ Combined: choose strategy based on file size ══

    def start_background_load(self, lazy_threshold=50 * 1024 * 1024):
        """Start background loading. Files >lazy_threshold use lazy chunk mode."""
        self._loading = True
        self._load_progress = 0
        file_size = os.path.getsize(self._filename)
        if file_size > lazy_threshold:
            self._load_thread = threading.Thread(
                target=self._build_time_index, args=(self._values_offset,), daemon=True)
        else:
            self._load_thread = threading.Thread(
                target=self._parse_values_background, args=(self._values_offset,), daemon=True)
        self._load_thread.start()

    # ══ Live 'follow' mode: incrementally tail appended value changes ══

    def start_follow(self):
        """Resume after the bytes actually parsed, carrying directive state."""
        if getattr(self, '_store', None) is not None:
            raise ValueError('Follow requires --storage memory; disk indexes are immutable snapshots')
        size = os.path.getsize(self._filename)
        self._tail_offset = getattr(self, '_parsed_offset', size)
        state = getattr(self, '_value_state', None)
        if state is None:
            _, offset = self.parse_header(self._filename)
            state = ValueState(*states_at_offsets(self._filename, offset,
                                                  [self._tail_offset])[self._tail_offset])
        self._tail_state = ValueState(*state.snapshot())
        self._value_signals = {k.encode('ascii'): s for k, s in self.signals.items()
                               if s.owns_storage}
        self._tail_narrow = any(s.owns_storage and s._times.typecode == 'i'
                                for s in self.signals.values())

    def _widen_times(self):
        """Undo _compact_times: 32-bit timestamp arrays back to 64-bit."""
        for sig in self.signals.values():
            if sig.owns_storage and sig._times.typecode == 'i':
                sig._times = array.array('q', sig._times)   # atomic rebind
        self._tail_narrow = False

    def _resync_storage(self):
        """After an interrupted append (value in, timestamp not), trim any
        value store that ran ahead of its timestamps so readers stay aligned."""
        for sig in self.signals.values():
            if not sig.owns_storage:
                continue
            n, vals = len(sig._times), sig._values
            want = n * sig._stride if (sig._packed and type(vals) is bytearray) else n
            if len(vals) > want:
                del vals[want:]

    def tail(self):
        """Consume complete appended lines with the full reader's semantics."""
        if self._tail_offset is None:
            return 0
        with open(self._filename, 'rb') as f:
            f.seek(self._tail_offset)
            data = f.read()
        consumed = data.rfind(b'\n') + 1
        if not consumed:
            return 0
        changes = 0
        state = self._tail_state
        try:
            for raw in data[:consumed].splitlines():
                event = state.feed(raw)
                if self._tail_narrow and state.max_time > _I32_MAX:
                    self._widen_times()
                if event is not None:
                    changes += self._append_event(*event, state.time)
        except Exception:
            self._resync_storage()
        self._tail_offset += consumed
        self._parsed_offset = self._tail_offset
        self._value_state = state
        self.max_time = state.max_time
        return changes

    # ══ Original synchronous parse (unchanged, for compatibility) ══

    @staticmethod
    def parse(filename, progress_cb=None):
        vcd, offset = VCDFile.parse_header(filename)
        vcd._read_values(offset, progress_cb)
        return vcd


def _extract_block(first_line, keyword, fileobj):
    """Extract text from a $keyword ... $end block."""
    text = first_line.replace(keyword, '').replace('$end', '').strip()
    if '$end' in first_line:
        return text
    for cont in fileobj:
        if isinstance(cont, bytes):
            cont = cont.decode('ascii', errors='ignore')
        if '$end' in cont:
            text += " " + cont.replace('$end', '').strip()
            break
        text += " " + cont.strip()
    return text.strip()


class _TextWrapper:
    """Wrap binary file object to yield decoded strings for _extract_block."""
    __slots__ = ('_f',)
    def __init__(self, f): self._f = f
    def __iter__(self): return self
    def __next__(self):
        line = self._f.readline()
        if not line: raise StopIteration
        return line.decode('ascii', errors='ignore')
