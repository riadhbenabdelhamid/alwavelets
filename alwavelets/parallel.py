"""Parallel VCD value-change parsing across processes (opt-in, large files).

After the header is parsed, the value-change region is split into byte ranges
aligned to '#timestamp' line boundaries; each worker process parses its range
independently and returns compact per-signal arrays (array('q').tobytes() +
packed bytes / a joined blob), which the parent concatenates in chunk order.

This only speeds up the *first* full parse of a large file (the parse cache
makes re-opens instant regardless). It is gated to fork-capable platforms and
sizable VCDs, and every entry point falls back to the sequential parser on any
problem, so correctness never depends on it.

Workers receive the time, comment and dump state at their byte boundary, so
splits inside dump blackouts or comments preserve sequential semantics.
"""
import array
import os

from .parsers import VCDFile, _BITBYTE, _compact_times
from .value_stream import ValueState, states_at_offsets


def _split_points(filename, offset, size, n):
    """Return up to n (start, end) byte ranges over [offset, size), each
    starting at the beginning of a '#timestamp' line (the first range starts at
    offset). Fewer ranges are returned if good split points aren't found."""
    if n <= 1 or size - offset < 2:
        return [(offset, size)]
    bounds = [offset]
    step = (size - offset) // n
    with open(filename, 'rb') as f:
        for i in range(1, n):
            target = offset + i * step
            if target <= bounds[-1]:
                continue
            f.seek(target)
            f.readline()                       # discard the partial line
            while True:
                p = f.tell()
                line = f.readline()
                if not line:
                    break
                if line[:1] == b'#':
                    if p > bounds[-1]:
                        bounds.append(p)
                    break
    bounds.append(size)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1) if bounds[i] < bounds[i + 1]]


def _worker(args):
    """Parse [start, end) of `filename`; return (max_time, {id: (tbytes, vblob, scalar)})."""
    filename, start, end, meta_items = args[:4]
    state = ValueState(*args[4]) if len(args) > 4 else ValueState()
    meta = dict(meta_items)
    result = {}
    with open(filename, 'rb') as f:
        f.seek(start)
        pos = start
        for raw in f:
            if pos >= end:
                break
            pos += len(raw)
            event = state.feed(raw)
            if event is None:
                continue
            bid, value = event
            keys = meta if bid is None else (bid,)
            for key in keys:
                if key not in meta:
                    continue
                ts, vs = result.setdefault(key, ([], []))
                ts.append(state.time)
                vs.append(_BITBYTE.get(ord(value[-1:]) if value else 120, 120)
                          if meta[key] else value)
    mx = state.max_time
    out = {}
    for bid, (ts, vs) in result.items():
        tb = array.array('q', ts).tobytes()
        if meta.get(bid):                       # scalar -> packed bytes
            out[bid.decode('ascii', 'ignore')] = (tb, bytes(vs), True)
        else:                                   # bus -> newline-joined blob
            out[bid.decode('ascii', 'ignore')] = (tb, "\n".join(vs).encode('utf-8', 'replace'), False)
    return mx, out


def _fill_parallel(vcd, filename, offset, jobs):
    """Parse the value region with `jobs` workers and fill vcd's signals.
    Returns True on success, False to signal a fallback to sequential parsing."""
    import multiprocessing as mp
    import sys
    size = os.path.getsize(filename)
    n = min(jobs, mp.cpu_count() or jobs) if jobs else min(mp.cpu_count() or 1, 8)
    ranges = _split_points(filename, offset, size, n)
    if len(ranges) < 2:
        return False
    meta = [(k.encode('ascii'), s._scalar)
            for k, s in vcd.signals.items() if s.owns_storage]
    states = states_at_offsets(filename, offset, [a for a, _ in ranges])
    tasks = [(filename, a, b, meta, states[a]) for (a, b) in ranges]
    ctx = mp.get_context('fork')
    with ctx.Pool(len(ranges)) as pool:
        parts = pool.map(_worker, tasks)       # ordered by chunk

    pieces = {}
    mx = 0
    for cmx, part in parts:
        if cmx > mx:
            mx = cmx
        for sid, triple in part.items():
            pieces.setdefault(sid, []).append(triple)

    intern = sys.intern
    signals = vcd.signals
    for sid, plist in pieces.items():
        sig = signals.get(sid)
        if sig is None:
            continue
        times = array.array('q')
        if plist[0][2]:                         # scalar
            vals = bytearray()
            for tb, vb, _ in plist:
                times.frombytes(tb); vals += vb
        else:                                   # bus / real
            strs = []
            for tb, vb, _ in plist:
                times.frombytes(tb)
                if vb:
                    strs.extend(vb.decode('utf-8', 'replace').split("\n"))
            if sig._packed:                     # encode once; no interning needed
                vals = sig._encode_values(strs)
                if vals is None:                # unpackable value: string fallback
                    sig._packed = False
                    vals = [intern(s) for s in strs]
            else:
                vals = [intern(s) for s in strs]
        sig._times = times
        sig._values = vals
    vcd.max_time = mx
    vcd._extent_known = True
    vcd._parsed_offset = size
    for sig in vcd.signals.values():
        if sig.owns_storage and any(sig._times[i] < sig._times[i - 1]
                                   for i in range(1, len(sig._times))):
            sig._sort_by_time()
    _compact_times(vcd)
    return True


def parse_parallel_file(filename, jobs=None):
    """Header-parse `filename`, then fill values in parallel. Returns a fully
    loaded VCDFile, or None to fall back to the sequential parser."""
    vcd, offset = VCDFile.parse_header(filename)
    if not vcd.signal_order:
        return None
    return vcd if _fill_parallel(vcd, filename, offset, jobs) else None


def viable(filename, jobs=None, min_size=16 * 1024 * 1024):
    """True if parallel parsing is worth attempting for this file.

    Opt-in only: returns False unless `jobs` >= 2 is given explicitly. Measured
    speedup is modest and depends on core count + file size (it can even be
    slower with few workers because the parsed data must be merged serially), so
    parallelism is never enabled by default — no regression for the common case.
    """
    if not jobs or jobs < 2 or not hasattr(os, 'fork'):
        return False
    try:
        if os.path.getsize(filename) < min_size:
            return False
        with open(filename, 'rb') as f:
            if f.read(1)[:1] != b'$':           # VCD only (FST is binary)
                return False
    except OSError:
        return False
    import multiprocessing as mp
    return (mp.cpu_count() or 1) > 1
