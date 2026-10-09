"""Persistent on-disk parse cache.

Serializes a fully-parsed VCDFile to a compact binary sidecar in the user cache
dir, so re-opening an unchanged trace is near-instant instead of re-parsing.

Pure stdlib (struct + zlib). The cache is validated against the source file's
size + mtime + the host byte order, so a stale, foreign, or wrong-endian cache
is silently ignored and never trusted blindly. All operations are best-effort:
any failure falls back to a normal parse.
"""
import array
import hashlib
import os
import struct
import sys
import zlib

from .core import VCDSignal, AliasSignal
from .parsers import VCDFile, build_scope_tree

_MAGIC = b'WLT1'
_VERSION = 6      # declared signal ranges; invalidate old parser semantics
_BO = 0 if sys.byteorder == 'little' else 1

# Module-level switch so `--no-cache` can disable load + save in one place.
ENABLED = True


def _cache_dir():
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'alwavelets')


def cache_path(source):
    h = hashlib.sha1(os.path.abspath(source).encode('utf-8', 'replace')).hexdigest()[:16]
    return os.path.join(_cache_dir(), f"{os.path.basename(source)}.{h}.wlc")


# ── tiny serialization helpers ───────────────────────────────────────

def _wstr(buf, s):
    b = (s or "").encode('utf-8', 'replace')
    buf += struct.pack('<I', len(b))
    buf += b


def _rstr(mv, pos):
    (n,) = struct.unpack_from('<I', mv, pos)
    pos += 4
    return bytes(mv[pos:pos + n]).decode('utf-8', 'replace'), pos + n


# ── save / load ──────────────────────────────────────────────────────

def save_cache(vcd, source):
    """Write `vcd` to its cache file. Returns True on success, False otherwise."""
    if not ENABLED:
        return False
    try:
        st = os.stat(source)
        if (getattr(vcd, '_store', None) is not None or getattr(vcd, '_lazy_mode', False) or not vcd._extent_known
                or getattr(vcd, '_parsed_offset', st.st_size) != st.st_size):
            return False
        body = bytearray()
        _wstr(body, vcd.timescale)
        body += struct.pack('<i', vcd.timescale_number)
        _wstr(body, vcd.timescale_unit)
        _wstr(body, vcd.date)
        _wstr(body, vcd.version)
        body += struct.pack('<qq', vcd.max_time, vcd.min_time)
        body += struct.pack('<I', len(vcd.signal_order))
        for sid in vcd.signal_order:
            _wstr(body, sid)
        body += struct.pack('<I', len(vcd.signals))
        master_keys = {}
        for key, sig in vcd.signals.items():
            _wstr(body, key)
            _wstr(body, sig.reference)
            _wstr(body, ','.join(map(str, sig.declared_range)) if sig.declared_range else '')
            _wstr(body, sig.identifier)
            _wstr(body, sig.signal_type)
            _wstr(body, sig.scope)
            # Storage kind: 1 = scalar bytearray, 2 = packed bus bytearray,
            # 0 = string list (reals, very wide, or de-packed signals),
            # 3 = alias (names the master's key; masters precede aliases).
            if not sig.owns_storage:
                body += struct.pack('<IBIc', sig.size, 3, 0, b'q')
                _wstr(body, master_keys[id(sig.master)])
                continue
            master_keys[id(sig)] = key
            n = len(sig._times)
            if sig._scalar:
                kind = 1
            elif type(sig._values) is bytearray:
                kind = 2
            else:
                kind = 0
            body += struct.pack('<IBIc', sig.size, kind, n,
                                sig._times.typecode.encode('ascii'))
            body += sig._times.tobytes()
            if kind == 1:
                body += bytes(sig._values)
            elif kind == 2:                      # raw packed bytes (n * stride)
                body += bytes(sig._values)
            else:
                blob = "\n".join(sig._values).encode('utf-8', 'replace')
                body += struct.pack('<I', len(blob))
                body += blob
        comp = zlib.compress(bytes(body), 6)
        header = _MAGIC + struct.pack('<IBQQ', _VERSION, _BO, st.st_size, st.st_mtime_ns)
        os.makedirs(_cache_dir(), exist_ok=True)
        path = cache_path(source)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, 'wb') as f:
            f.write(header)
            f.write(comp)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


_HDR = struct.calcsize('<IBQQ') + 4   # magic + version + bo + size + mtime


def load_cache(source):
    """Return a fully-loaded VCDFile from a valid cache, or None."""
    if not ENABLED:
        return None
    try:
        st = os.stat(source)
        with open(cache_path(source), 'rb') as f:
            head = f.read(_HDR)
            if len(head) < _HDR or head[:4] != _MAGIC:
                return None
            ver, bo, size, mtime_ns = struct.unpack_from('<IBQQ', head, 4)
            if (ver != _VERSION or bo != _BO
                    or size != st.st_size or mtime_ns != st.st_mtime_ns):
                return None
            body = zlib.decompress(f.read())
    except Exception:
        return None

    try:
        mv = memoryview(body)
        pos = 0
        vcd = VCDFile()
        vcd._filename = source
        vcd._parsed_offset = st.st_size
        vcd.timescale, pos = _rstr(mv, pos)
        (vcd.timescale_number,) = struct.unpack_from('<i', mv, pos); pos += 4
        vcd.timescale_unit, pos = _rstr(mv, pos)
        vcd.date, pos = _rstr(mv, pos)
        vcd.version, pos = _rstr(mv, pos)
        vcd.max_time, vcd.min_time = struct.unpack_from('<qq', mv, pos); pos += 16
        (n_order,) = struct.unpack_from('<I', mv, pos); pos += 4
        order = []
        for _ in range(n_order):
            s, pos = _rstr(mv, pos)
            order.append(s)
        vcd.signal_order = order
        (n_sig,) = struct.unpack_from('<I', mv, pos); pos += 4
        _intern = sys.intern
        for _ in range(n_sig):
            key, pos = _rstr(mv, pos)
            name, pos = _rstr(mv, pos)
            range_text, pos = _rstr(mv, pos)
            declared_range = tuple(map(int, range_text.split(','))) if range_text else None
            ident, pos = _rstr(mv, pos)
            stype, pos = _rstr(mv, pos)
            scope, pos = _rstr(mv, pos)
            size, kind, n, tc = struct.unpack_from('<IBIc', mv, pos)
            pos += struct.calcsize('<IBIc')
            if kind == 3:                        # alias of an earlier signal
                mkey, pos = _rstr(mv, pos)
                vcd.signals[key] = AliasSignal(name, ident, size, stype, scope,
                                               vcd.signals[mkey], declared_range)
                continue
            sig = VCDSignal(name, ident, size, stype, scope, declared_range)
            t = array.array(tc.decode('ascii'))
            t.frombytes(mv[pos:pos + n * t.itemsize]); pos += n * t.itemsize
            sig._times = t
            if kind == 1:                        # scalar bytearray
                sig._values = bytearray(mv[pos:pos + n]); pos += n
            elif kind == 2:                      # packed bus bytearray
                if not sig._packed:              # corrupt: kind disagrees with type
                    return None
                nb = n * sig._stride
                sig._values = bytearray(mv[pos:pos + nb]); pos += nb
            else:                                # string list
                (blen,) = struct.unpack_from('<I', mv, pos); pos += 4
                blob = bytes(mv[pos:pos + blen]).decode('utf-8', 'replace'); pos += blen
                sig._values = [_intern(s) for s in blob.split("\n")] if n else []
                sig._packed = False              # e.g. a de-packed signal
            vcd.signals[key] = sig
        vcd.scopes = build_scope_tree(vcd)
        return vcd
    except Exception:
        return None
