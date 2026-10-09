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
import sys
import threading
from collections import OrderedDict

from .core import VCDSignal, AliasSignal, clear_pack_memos
from .fst import FSTReader  # noqa: F401  (re-exported; the reader lives in fst.py)

# First byte of a scalar value-change line -> normalised value byte for the
# packed bytearray store ('X'->'x', 'Z'->'z'). Lets the hot parse loop append a
# byte directly (no chr().lower()/intern per change). See VCDSignal._scalar.
_BITBYTE = {48: 48, 49: 49, 120: 120, 88: 120, 122: 122, 90: 122}


class ConverterError(RuntimeError):
    """An external format converter (fst2vcd, lxt2vcd, …) is missing or
    failed. Raised instead of exiting so library users (the MCP server, a
    REPL) get an exception; the CLI turns it into an error message."""


def _declare_var(vcd, parts, scope):
    """Register one `$var` line. A repeated identifier code is an alias (one
    net seen from several scopes — Icarus does this), so it becomes an
    AliasSignal sharing the first declaration's storage instead of being
    silently dropped. Alias keys contain a space, which an id never does."""
    sig_id, size, stype, name = parts[3], _safe_int(parts[2]), parts[1], parts[4]
    master = vcd.signals.get(sig_id)
    if master is None:
        vcd.signals[sig_id] = VCDSignal(name, sig_id, size, stype, scope)
        vcd.signal_order.append(sig_id)
    elif master.full_name != (f"{scope}.{name}" if scope else name):
        key = f"{sig_id} #{len(vcd.signal_order)}"
        vcd.signals[key] = AliasSignal(name, sig_id, size, stype, scope, master)
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


def _fst_via_converter(filename, progress_cb=None):
    """Convert FST to VCD using fst2vcd, then parse the VCD."""
    converter = shutil.which('fst2vcd')
    if not converter:
        raise ConverterError(
            "'fst2vcd' not found. Install GTKWave tools for FST support "
            "(Ubuntu/Debian: sudo apt install gtkwave), or convert first: "
            "fst2vcd input.fst > output.vcd")

    import tempfile
    _fd, vcd_file = tempfile.mkstemp(suffix='.vcd', prefix='alwavelets_fst_')
    os.close(_fd)
    print(f"  [fst2vcd converter: {os.path.basename(filename)} → VCD]")
    try:
        result = subprocess.run(
            [converter, filename],
            capture_output=True, timeout=300
        )
        if result.returncode != 0:
            err_msg = result.stderr.decode('utf-8', errors='replace').strip()
            raise ConverterError(f"fst2vcd failed: {err_msg}")

        # fst2vcd writes VCD to stdout
        vcd_data = result.stdout
        if not vcd_data or len(vcd_data) < 10:
            raise ConverterError("fst2vcd produced empty output")

        with open(vcd_file, 'wb') as f:
            f.write(vcd_data)

        print(f"  [Converted: {len(vcd_data)/1024:.0f} KB VCD, parsing...]")
        vcd = VCDFile.parse(vcd_file, progress_cb)
        vcd._filename = filename  # Keep original FST filename for sidecar files
        return vcd
    except subprocess.TimeoutExpired:
        raise ConverterError("fst2vcd timed out (>5 min)")
    except ConverterError:
        raise
    except Exception as e:
        raise ConverterError(f"converting FST: {e}")
    finally:
        try:
            os.unlink(vcd_file)
        except OSError:
            pass


def parse_file(filename, progress_cb=None, use_cache=True, jobs=None):
    """Universal file parser with a transparent persistent parse cache.

    On a cache hit (source size+mtime unchanged) the fully-parsed VCDFile is
    deserialized in milliseconds; otherwise the file is parsed normally and the
    result cached for next time. Caching is keyed on the original path, so
    reopening even skips FST/legacy conversion. `jobs` controls parallel parsing
    of large VCDs (None = auto, 1 = sequential)."""
    if use_cache:
        from . import cache
        cached = cache.load_cache(filename)
        if cached is not None:
            return cached
    vcd = _parse_file_uncached(filename, progress_cb, jobs)
    if use_cache and vcd is not None and getattr(vcd, 'signal_order', None):
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


def _parse_file_uncached(filename, progress_cb=None, jobs=None):
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
        return _fst_via_converter(filename, progress_cb)

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

    import tempfile
    _fd, vcd_file = tempfile.mkstemp(suffix='.vcd', prefix='vcd_viewer_')
    os.close(_fd)
    print(f"  Converting {ext} → VCD via {converter}...")
    try:
        result = subprocess.run([converter_path, filename], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise ConverterError(f"{converter} failed: {result.stderr.strip()}")
        with open(vcd_file, 'w') as f:
            f.write(result.stdout)
        return VCDFile.parse(vcd_file, progress_cb)
    except ConverterError:
        raise
    except Exception as e:
        raise ConverterError(f"{converter}: {e}")
    finally:
        try:
            os.unlink(vcd_file)
        except OSError:
            pass


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
        n = len(self.signal_ids)
        for child in self.children.values():
            n += child.total_signals()
        return n


def build_scope_tree(vcd):
    """Build a scope hierarchy tree from parsed VCD signals."""
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
        self.scopes = None  # ScopeNode tree, built after parse

        # ── Background loading state ──
        self._loading = False
        self._load_progress = 0
        self._load_thread = None
        self._load_lock = threading.Lock()
        self._load_error = None
        self._load_changes = 0

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
    def parse_header(filename):
        """Parse VCD header only. Returns (vcd, values_offset)."""
        vcd = VCDFile()
        vcd._filename = filename
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
                        _declare_var(vcd, parts, current_scope)
                elif line.startswith('$enddefinitions'):
                    break
                elif line.startswith('$date'):
                    vcd.date = _extract_block(line, '$date', _TextWrapper(f))
                elif line.startswith('$version'):
                    vcd.version = _extract_block(line, '$version', _TextWrapper(f))

            vcd._values_offset = f.tell()

        vcd.scopes = build_scope_tree(vcd)
        return vcd, vcd._values_offset

    # ══ Phase 2a: Background full value parse ══

    def _parse_values_background(self, offset):
        """Parse all value changes from offset. Runs in background thread.

        Hot path is lock-free: the reader never takes _load_lock, and VCDSignal
        appends values before times, so a concurrent bisect reader always sees a
        consistent prefix (see VCDSignal.add_change). It also keys signals by
        bytes (no per-change decode) and inlines the appends — together this is
        several times faster than the locked/decoded per-change path it replaces.
        """
        try:
            file_size = os.path.getsize(self._filename)
            signals = self.signals
            # Bytes-keyed view of the id->signal map so the hot loop never has to
            # decode the signal id of every value change.
            bget = {k.encode('ascii'): v for k, v in signals.items()}.get
            bitbyte = _BITBYTE
            current_time = 0
            dump_active = True
            bytes_read = offset
            changes = 0
            time_index = []
            ti_append = time_index.append

            with open(self._filename, 'rb', buffering=1 << 20) as f:
                f.seek(offset)
                for raw_line in f:
                    bytes_read += len(raw_line)
                    c0 = raw_line[0]

                    # ── scalar value change (overwhelmingly the common case) ──
                    if c0 == 48 or c0 == 49 or c0 == 120 or c0 == 88 or c0 == 122 or c0 == 90:
                        if dump_active:
                            sig = bget(raw_line[1:].rstrip())
                            if sig is not None:
                                sig._values.append(bitbyte[c0])  # bytearray; values before times
                                sig._times.append(current_time)
                                changes += 1
                        continue

                    # ── timestamp ──
                    if c0 == 35:  # b'#'
                        try:
                            current_time = int(raw_line[1:])
                            if current_time > self.max_time:
                                self.max_time = current_time
                            if not time_index or bytes_read - time_index[-1][1] > 100000:
                                ti_append((current_time, f.tell() - len(raw_line)))
                                if file_size > 0:
                                    self._load_progress = min(99, bytes_read * 100 // file_size)
                                    self._load_changes = changes
                        except ValueError:
                            pass
                        continue

                    # ── vector (bus) value change ──
                    if c0 == 98 or c0 == 66:  # b'b' / b'B'
                        if dump_active:
                            sp = raw_line.find(32, 1)
                            if sp > 0:
                                sig = bget(raw_line[sp + 1:].rstrip())
                                if sig is not None:
                                    if sig._scalar:
                                        # 'b1 !' for a 1-bit var (GHDL, xsim,
                                        # cocotb write one-element vectors so)
                                        sig._values.append(bitbyte.get(raw_line[sp - 1], 120))
                                    else:                  # vector -> packed/list store
                                        v = raw_line[1:sp].decode('ascii', 'ignore')
                                        sig._append_value(v)   # values before times (contract)
                                    sig._times.append(current_time)
                                    changes += 1
                        continue

                    # ── real value change ──
                    if c0 == 114 or c0 == 82:  # b'r' / b'R'
                        if dump_active:
                            sp = raw_line.find(32, 1)
                            if sp > 0:
                                sig = bget(raw_line[sp + 1:].rstrip())
                                if sig is not None and not sig._scalar:
                                    v = raw_line[1:sp].decode('ascii', 'ignore')
                                    sig._append_value(v)   # reals stay strings
                                    sig._times.append(current_time)
                                    changes += 1
                        continue

                    # ── directives ($dumpvars/off/on, $comment, $end) ──
                    if c0 == 36:  # b'$'
                        line = raw_line.decode('ascii', errors='ignore').strip()
                        if line.startswith('$dumpoff'):
                            dump_active = False
                            for sig in signals.values():
                                if sig.owns_storage:
                                    sig.add_change(current_time, 'x')  # type-aware append
                                    changes += 1
                        elif line.startswith('$dumpon'):
                            dump_active = True
                        elif line.startswith('$comment'):
                            if '$end' not in line:
                                for cont in f:
                                    bytes_read += len(cont)
                                    if b'$end' in cont:
                                        break
                        continue

            self._time_index = time_index

            # Repair out-of-order timestamps (e.g. $dumpoff/$dumpon regions).
            # Scan the WHOLE array — disorder can appear anywhere, not just in
            # the first 100 entries — and rebind _values before _times to keep
            # the lock-free-read contract (see VCDSignal.add_change).
            for sig in signals.values():
                if not sig.owns_storage:
                    continue
                t = sig._times
                if len(t) < 2:
                    continue
                prev = t[0]
                out_of_order = False
                for cur in t:
                    if cur < prev:
                        out_of_order = True
                        break
                    prev = cur
                if out_of_order:
                    sig._sort_by_time()   # storage-aware (scalar/packed/list)

            self._load_changes = changes
            self._load_progress = 100
            _compact_times(self)     # 64-bit -> 32-bit timestamps when they fit
            # Persist a parse cache so the next open of this file is instant.
            try:
                from . import cache
                cache.save_cache(self, self._filename)
            except Exception:
                pass
        except Exception as e:
            self._load_error = str(e)
        finally:
            self._loading = False

    # ══ Phase 2b: Lazy time index build (for huge files) ══

    # One index entry at most every this many bytes: a chunk request then
    # over-reads by at most this much on each side, while the index stays
    # tens of thousands of entries for a multi-GB trace instead of one tuple
    # per '#' line (which was ~100 bytes of Python objects per timestamp).
    _INDEX_STRIDE = 64 * 1024

    def _build_time_index(self, offset):
        """Fast scan: read only timestamps to build time->offset index."""
        file_size = os.path.getsize(self._filename)
        time_index = []
        stride = self._INDEX_STRIDE
        last_indexed = -stride
        n_lines = 0
        try:
            with open(self._filename, 'rb', buffering=1 << 20) as f:
                f.seek(offset)
                pos = offset
                for raw_line in f:
                    if raw_line[0:1] == b'#':
                        try:
                            t = int(raw_line[1:])
                            if t > self.max_time:
                                self.max_time = t
                            if pos - last_indexed >= stride:
                                time_index.append((t, pos))
                                last_indexed = pos
                        except ValueError:
                            pass
                        n_lines += 1
                        if n_lines % 10000 == 0 and file_size > 0:
                            self._load_progress = min(99, pos * 100 // file_size)
                    pos += len(raw_line)
            self._time_index = time_index
            self._ti_times = [t for t, _ in time_index]
            self._lazy_mode = True
            self._loaded_ranges = []
            self._load_progress = 100
        except Exception as e:
            self._load_error = str(e)
        finally:
            self._loading = False

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
        """Parse value changes in a file range, returning {sig_id: (times, values)}
        for changes within [t_start, t_end]. Does not mutate signals directly."""
        signals = self.signals
        # Bytes-keyed membership + result so the hot loop avoids decoding the id
        # of every change; the keys are decoded once at return (per signal).
        valid = {k.encode('ascii') for k in signals}
        scalar_ids = {k.encode('ascii') for k, s in signals.items() if s._scalar}
        bitbyte = _BITBYTE
        _intern = sys.intern
        result = {}
        rget = result.get
        current_time = 0
        in_range = False

        with open(self._filename, 'rb', buffering=1 << 20) as f:
            f.seek(start_offset)
            bytes_to_read = end_offset - start_offset
            bytes_read = 0
            for raw_line in f:
                bytes_read += len(raw_line)
                if bytes_read > bytes_to_read + 10000:
                    break
                c0 = raw_line[0]
                if c0 == 35:  # b'#'
                    try:
                        current_time = int(raw_line[1:])
                        in_range = t_start <= current_time <= t_end
                    except ValueError:
                        pass
                    continue
                if not in_range:
                    continue
                if c0 == 48 or c0 == 49 or c0 == 120 or c0 == 88 or c0 == 122 or c0 == 90:
                    bid = raw_line[1:].rstrip()
                    if bid in valid:
                        tv = rget(bid)
                        if tv is None:
                            tv = result[bid] = ([], [])
                        if not tv[0] or tv[0][-1] != current_time:
                            tv[0].append(current_time); tv[1].append(bitbyte[c0])
                elif c0 == 98 or c0 == 66:
                    sp = raw_line.find(32, 1)
                    if sp > 0:
                        bid = raw_line[sp + 1:].rstrip()
                        if bid in valid:
                            tv = rget(bid)
                            if tv is None:
                                tv = result[bid] = ([], [])
                            if not tv[0] or tv[0][-1] != current_time:
                                tv[0].append(current_time)
                                if scalar_ids is not None and bid in scalar_ids:
                                    tv[1].append(bitbyte.get(raw_line[sp - 1], 120))
                                else:
                                    tv[1].append(_intern(raw_line[1:sp].decode('ascii', 'ignore')))
                elif c0 == 114 or c0 == 82:
                    sp = raw_line.find(32, 1)
                    if sp > 0:
                        bid = raw_line[sp + 1:].rstrip()
                        if bid in valid:
                            tv = rget(bid)
                            if tv is None:
                                tv = result[bid] = ([], [])
                            if not tv[0] or tv[0][-1] != current_time:
                                tv[0].append(current_time)
                                tv[1].append(_intern(raw_line[1:sp].decode('ascii', 'ignore')))
        return {k.decode('ascii', 'ignore'): v for k, v in result.items()}

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
        """Arm incremental tailing from the current end of the file."""
        try:
            self._tail_offset = os.path.getsize(self._filename)
        except OSError:
            self._tail_offset = 0
        self._tail_time = self.max_time
        self._tail_dump = True
        self._tail_bget = {k.encode('ascii'): v for k, v in self.signals.items()}.get
        # _compact_times may have narrowed timestamps to 32 bits; a live sim
        # can run past that, so tail() widens them back the moment it has to.
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
        """Parse value changes appended since the last tail()/start_follow().

        Reads only the new bytes (up to the last complete line), appends changes
        to the existing signals (lock-free: values before times), advances
        max_time, and returns the number of new changes. New signals can't appear
        mid-stream in VCD, so only existing signals grow."""
        if getattr(self, '_tail_offset', None) is None:
            return 0
        try:
            size = os.path.getsize(self._filename)
        except OSError:
            return 0
        if size <= self._tail_offset:
            return 0
        with open(self._filename, 'rb') as f:
            f.seek(self._tail_offset)
            data = f.read(size - self._tail_offset)
        nl = data.rfind(b'\n')
        if nl < 0:
            return 0                       # no complete new line yet
        consumed = nl + 1
        bget = self._tail_bget
        bitbyte = _BITBYTE
        ct, dump, mx, changes = self._tail_time, self._tail_dump, self.max_time, 0
        narrow = getattr(self, '_tail_narrow', False)
        try:
            for raw_line in data[:consumed].split(b'\n'):
                if not raw_line:
                    continue
                c0 = raw_line[0]
                if c0 == 48 or c0 == 49 or c0 == 120 or c0 == 88 or c0 == 122 or c0 == 90:
                    if dump:
                        sig = bget(raw_line[1:].rstrip())
                        if sig is not None:
                            sig._values.append(bitbyte[c0]); sig._times.append(ct); changes += 1
                    continue
                if c0 == 35:
                    try:
                        ct = int(raw_line[1:])
                        if ct > mx:
                            mx = ct
                            if narrow and ct > _I32_MAX:
                                self._widen_times()
                                narrow = False
                    except ValueError:
                        pass
                    continue
                if c0 == 98 or c0 == 66 or c0 == 114 or c0 == 82:
                    if dump:
                        sp = raw_line.find(32, 1)
                        if sp > 0:
                            sig = bget(raw_line[sp + 1:].rstrip())
                            if sig is not None:
                                if sig._scalar:
                                    if c0 == 98 or c0 == 66:   # 'b1 !' on a 1-bit var
                                        sig._values.append(bitbyte.get(raw_line[sp - 1], 120))
                                        sig._times.append(ct); changes += 1
                                else:
                                    v = raw_line[1:sp].decode('ascii', 'ignore')
                                    sig._append_value(v)   # values before times
                                    sig._times.append(ct); changes += 1
                    continue
                if c0 == 36:
                    line = raw_line.decode('ascii', 'ignore').strip()
                    if line.startswith('$dumpoff'):
                        dump = False
                    elif line.startswith('$dumpon'):
                        dump = True
        except Exception:
            self._resync_storage()         # malformed tail: keep what aligned
        self._tail_offset += consumed
        self._tail_time, self._tail_dump, self.max_time = ct, dump, mx
        return changes

    # ══ Original synchronous parse (unchanged, for compatibility) ══

    @staticmethod
    def parse(filename, progress_cb=None):
        """Parse VCD file. Uses fast binary mode for value changes."""
        vcd = VCDFile()
        vcd._filename = filename
        file_size = os.path.getsize(filename)

        scope_stack = []
        current_scope = ""
        current_time = 0
        in_header = True
        dump_active = True
        bytes_read = 0
        last_progress = 0

        # Fast-path: pre-fetch signal dict for inner loop
        signals = vcd.signals
        bget = None                  # bytes-keyed id->signal map, built at $enddefinitions
        bitbyte = _BITBYTE

        # Use binary mode for faster I/O (avoid unicode decode overhead)
        with open(filename, 'rb', buffering=1 << 20) as f:
            for raw_line in f:
                bytes_read += len(raw_line)

                # Fast skip blank lines
                if len(raw_line) <= 1:
                    continue

                c0 = raw_line[0]

                # ── Value changes hot path (binary mode; lock-free, bytes-keyed,
                #    inlined appends — see _parse_values_background) ──
                if not in_header:
                    # Scalar: 0/1/x/z + identifier (the common case)
                    if c0 == 48 or c0 == 49 or c0 == 120 or c0 == 88 or c0 == 122 or c0 == 90:
                        if dump_active:
                            sig = bget(raw_line[1:].rstrip())
                            if sig is not None:
                                sig._values.append(bitbyte[c0])  # bytearray; values before times
                                sig._times.append(current_time)
                        continue

                    # Timestamp
                    if c0 == 35:  # b'#'
                        try:
                            current_time = int(raw_line[1:])
                            if current_time > vcd.max_time:
                                vcd.max_time = current_time
                        except ValueError:
                            pass
                        continue

                    # Vector: b/B<binary> <id>
                    if c0 == 98 or c0 == 66:  # b'b','B'
                        if dump_active:
                            sp = raw_line.find(32, 1)  # space
                            if sp > 0:
                                sig = bget(raw_line[sp + 1:].rstrip())
                                if sig is not None:
                                    if sig._scalar:        # 'b1 !' for a 1-bit var
                                        sig._values.append(bitbyte.get(raw_line[sp - 1], 120))
                                    else:                  # vector -> packed/list store
                                        v = raw_line[1:sp].decode('ascii', 'ignore')
                                        sig._append_value(v)   # values before times
                                    sig._times.append(current_time)
                        continue

                    # Real: r/R<float> <id>
                    if c0 == 114 or c0 == 82:  # b'r','R'
                        if dump_active:
                            sp = raw_line.find(32, 1)
                            if sp > 0:
                                sig = bget(raw_line[sp + 1:].rstrip())
                                if sig is not None and not sig._scalar:
                                    v = raw_line[1:sp].decode('ascii', 'ignore')
                                    sig._append_value(v)   # reals stay strings
                                    sig._times.append(current_time)
                        continue

                    if c0 == 36:  # b'$'
                        line = raw_line.decode('ascii', errors='ignore').strip()
                        if line.startswith('$dumpoff'):
                            dump_active = False
                            for sig in signals.values():
                                if sig.owns_storage:
                                    sig.add_change(current_time, 'x')  # type-aware append
                        elif line.startswith('$dumpon'):
                            dump_active = True
                        elif line.startswith('$comment'):
                            if '$end' not in line:
                                for cont in f:
                                    bytes_read += len(cont)
                                    if b'$end' in cont:
                                        break
                        continue

                    continue

                # ── Header (text mode — only runs once) ──
                # Whitespace handling
                if c0 in (32, 9, 10, 13):  # space, tab, newline, cr
                    line = raw_line.decode('ascii', errors='ignore').strip()
                    if not line:
                        continue
                    c0_str = line[0]
                else:
                    line = raw_line.decode('ascii', errors='ignore').rstrip('\n\r')
                    c0_str = chr(c0)

                # Progress reporting
                if progress_cb and file_size > 0:
                    pct = bytes_read * 100 // file_size
                    if pct >= last_progress + 5:
                        last_progress = pct
                        progress_cb(bytes_read, file_size)

                if c0_str != '$':
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
                        _declare_var(vcd, parts, current_scope)
                elif line.startswith('$enddefinitions'):
                    in_header = False
                    # Build the bytes-keyed id->signal map for the value hot loop.
                    bget = {k.encode('ascii'): v for k, v in signals.items()}.get
                    # Report progress at header end
                    if progress_cb and file_size > 0:
                        progress_cb(bytes_read, file_size)
                elif line.startswith('$date'):
                    vcd.date = _extract_block(line, '$date', _TextWrapper(f))
                elif line.startswith('$version'):
                    vcd.version = _extract_block(line, '$version', _TextWrapper(f))

        # ── Post-parse: robustness checks ──
        for sig in vcd.signals.values():
            if sig.owns_storage and len(sig._times) >= 2:
                is_sorted = True
                for i in range(1, min(len(sig._times), 100)):
                    if sig._times[i] < sig._times[i-1]:
                        is_sorted = False
                        break
                if not is_sorted:
                    sig._sort_by_time()   # storage-aware (scalar/packed/list)

        _compact_times(vcd)          # 64-bit -> 32-bit timestamps when they fit
        # Build scope tree
        vcd.scopes = build_scope_tree(vcd)

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
