"""Native FST (Fast Signal Trace) reader, pure Python.

FST is GTKWave's binary trace format, written by Verilator, GHDL, Icarus
(``-fst``), cocotb and ``vcd2fst``. This module reads it directly — the
header, the (gzip / LZ4 / LZ4-double) hierarchy, the geometry block, every
value-change block (frame, per-signal change chains, time table) and the
blackout ($dumpoff/$dumpon) block — into the same VCDFile/VCDSignal model the
VCD parser produces, so everything downstream is format-agnostic.

Chains and hierarchies are compressed with zlib, LZ4 or FastLZ. zlib is in
the standard library; LZ4 block and FastLZ (levels 1 and 2) are small formats
and are decoded here in pure Python, with the optional ``lz4`` package used
as a fast path when it happens to be installed. Nothing here needs it.

Layout notes (from fstapi.c, the reference implementation):

* Every block is ``u8 type | u64 section_length | payload``; the length
  counts everything after the type byte.
* A value-change block (types 1, 5, 8) holds, in order: ``beg_time``,
  ``end_time``, ``mem_required`` (u64 each); the *frame* (varint uclen,
  clen, maxhandle, then the values of every signal at ``beg_time``); a
  varint ``vc_maxhandle`` and one pack-type byte (``'Z'`` zlib, ``'4'`` LZ4,
  ``'F'`` FastLZ); the per-signal change chains; the *chain table*; a u64
  with the chain table's length; the *time table* (varint deltas); and a
  footer of three u64: time table uclen, clen and item count. The footer
  is read first and everything else located backwards from it.
* Within a chain each change is ``varint | value``. For 1-bit signals the
  varint's low bit says 2-state (value in bit 1, time delta from bit 2) or
  4-state (value index in bits 1..3 of ``"xzhuwl-?"``, time delta from
  bit 4). For wider signals the time delta is ``varint >> 1`` and the low
  bit chooses packed bits (ceil(width/8) bytes, MSB first) or one
  character per bit. Reals are 8 raw bytes in the writer's endianness.
  Time deltas accumulate per chain into the block's time table.
"""
import os
import struct
import zlib

from .core import VCDSignal, AliasSignal, split_reference

# ── small decompressors ─────────────────────────────────────────────

def lz4_block_decompress(src, dst_size):
    """Decode one LZ4 *block* (no frame header) of known decompressed size."""
    try:
        import lz4.block as _lz4
        return _lz4.decompress(bytes(src), uncompressed_size=dst_size)
    except ImportError:
        pass
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n:
            break                      # the final sequence is literals only
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = token & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        if off == 0 or start < 0:
            raise ValueError("lz4: bad match offset")
        while ml > 0:                  # chunked so overlapping copies repeat
            chunk = out[start:start + min(ml, off)]
            out += chunk
            start += len(chunk)
            ml -= len(chunk)
    if len(out) != dst_size:
        raise ValueError("lz4: size mismatch (%d != %d)" % (len(out), dst_size))
    return bytes(out)


def fastlz_decompress(src, dst_size):
    """Decode a FastLZ buffer (level 1 or 2, from the first byte's top bits)."""
    level = (src[0] >> 5) + 1
    if level not in (1, 2):
        raise ValueError("fastlz: unknown level %d" % level)
    out = bytearray()
    n = len(src)
    ip = 0
    ctrl = src[ip] & 31
    ip += 1
    loop = True
    while loop:
        if ctrl >= 32:                             # match
            ln = (ctrl >> 5) - 1
            ofs = (ctrl & 31) << 8
            if level == 1:
                if ln == 6:
                    ln += src[ip]
                    ip += 1
                code = src[ip]
                ip += 1
                dist = ofs + code + 1
            else:
                if ln == 6:
                    while True:
                        code = src[ip]
                        ip += 1
                        ln += code
                        if code != 255:
                            break
                code = src[ip]
                ip += 1
                dist = ofs + code + 1
                if code == 255 and ofs == (31 << 8):
                    ofs = (src[ip] << 8) | src[ip + 1]
                    ip += 2
                    dist = ofs + 8191 + 1
            ln += 3
            start = len(out) - dist
            if start < 0:
                raise ValueError("fastlz: bad match distance")
            while ln > 0:
                chunk = out[start:start + min(ln, dist)]
                out += chunk
                start += len(chunk)
                ln -= len(chunk)
            if ip < n:
                ctrl = src[ip]
                ip += 1
            else:
                loop = False
        else:                                      # literal run
            cnt = ctrl + 1
            out += src[ip:ip + cnt]
            ip += cnt
            loop = ip < n
            if loop:
                ctrl = src[ip]
                ip += 1
    if len(out) != dst_size:
        raise ValueError("fastlz: size mismatch (%d != %d)" % (len(out), dst_size))
    return bytes(out)


# ── the reader ───────────────────────────────────────────────────────

_RCV = "xzhuwl-?"          # 4-state value index -> VCD char (fstapi FST_RCV_STR)
_VAR_TYPE_NAMES = {
    0: 'event', 1: 'integer', 2: 'parameter', 3: 'real', 4: 'real',
    5: 'reg', 6: 'supply0', 7: 'supply1', 8: 'time', 9: 'tri',
    10: 'triand', 11: 'trior', 12: 'trireg', 13: 'tri0', 14: 'tri1',
    15: 'wand', 16: 'wire', 17: 'wor', 18: 'port', 19: 'sparray',
    20: 'real', 21: 'string', 22: 'bit', 23: 'logic', 24: 'int',
    25: 'shortint', 26: 'longint', 27: 'byte', 28: 'enum', 29: 'shortreal',
}
_REAL_TYPES = {3, 4, 20, 29}


class FSTReader:
    """Pure-Python FST reader producing a VCDFile. See the module docstring."""

    BL_HDR = 0
    BL_VCDATA = 1
    BL_BLACKOUT = 2
    BL_GEOM = 3
    BL_HIER = 4
    BL_VCDATA_DYN_ALIAS = 5
    BL_HIER_LZ4 = 6
    BL_HIER_LZ4DUO = 7
    BL_VCDATA_DYN_ALIAS2 = 8
    BL_ZWRAPPER = 254
    BL_SKIP = 255

    HT_ATTRBEGIN = 0xFC
    HT_ATTREND = 0xFD
    HT_SCOPE = 0xFE
    HT_UPSCOPE = 0xFF
    HT_VAR_MAX = 0xFB

    _TS_MAP = {
        0: (1, 's'), -1: (100, 'ms'), -2: (10, 'ms'), -3: (1, 'ms'),
        -4: (100, 'us'), -5: (10, 'us'), -6: (1, 'us'),
        -7: (100, 'ns'), -8: (10, 'ns'), -9: (1, 'ns'),
        -10: (100, 'ps'), -11: (10, 'ps'), -12: (1, 'ps'),
        -13: (100, 'fs'), -14: (10, 'fs'), -15: (1, 'fs'),
    }
    _VAR_TYPES = _VAR_TYPE_NAMES

    def __init__(self, filename):
        self.filename = filename
        self._blocks = []            # [(type, offset, section_length)]
        self._handles = []           # handle -> {name,size,type,scope,hid,real}
        self._max_handle = 0
        self._geom = []              # handle -> bit length (0 = real)
        self._real_fmt = '<d'
        self._frame0 = None          # (beg_time, {handle: value}) of the first VC block

    # ── primitive readers ────────────────────────────────────────────

    @staticmethod
    def _read_u64(f):
        return struct.unpack('>Q', f.read(8))[0]

    @staticmethod
    def _read_varint(data, pos):
        """LEB128 unsigned varint from a bytes-like; returns (value, new_pos)."""
        result = 0
        shift = 0
        n = len(data)
        while pos < n:
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    @staticmethod
    def _read_svint(data, pos):
        """FST signed varint: LEB128 whose last byte's 0x40 bit sign-extends
        (fstGetSVarint64) — not zigzag."""
        result = 0
        shift = 0
        n = len(data)
        b = 0
        while pos < n:
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80):
                break
        if b & 0x40:
            result -= 1 << shift
        return result, pos

    @staticmethod
    def _read_cstring(data, pos):
        end = data.find(b'\x00', pos)
        if end < 0:
            end = len(data)
        return bytes(data[pos:end]).decode('utf-8', errors='replace'), end + 1

    @staticmethod
    def _fst_varint_file(f):
        result = 0
        shift = 0
        while True:
            b = f.read(1)
            if not b:
                break
            b = b[0]
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result

    @staticmethod
    def is_fst(filename):
        """FST files start with a header block (0) or a gzip wrapper (254);
        VCD starts with '$'."""
        try:
            with open(filename, 'rb') as f:
                magic = f.read(1)
            return bool(magic) and magic[0] in (0, 254)
        except OSError:
            return False

    # ── top level ────────────────────────────────────────────────────

    def parse(self, progress_cb=None):
        from .parsers import VCDFile, build_scope_tree, _compact_times
        vcd = VCDFile()
        vcd._filename = self.filename
        file_size = os.path.getsize(self.filename)

        with open(self.filename, 'rb') as f:
            first = f.read(1)
            if not first:
                return vcd
            if first[0] == self.BL_ZWRAPPER:
                return self._parse_zwrapped(f, progress_cb)
            if first[0] != self.BL_HDR:
                raise ValueError("Not an FST file (first byte: 0x%02x)" % first[0])
            self._parse_header(f, vcd)
            if progress_cb:
                progress_cb(f.tell(), file_size)

            # Block map
            self._blocks = []
            f.seek(0)
            while True:
                pos = f.tell()
                tb = f.read(1)
                if not tb:
                    break
                sec_len = self._read_u64(f)
                if sec_len < 8:
                    break
                self._blocks.append((tb[0], pos, sec_len))
                f.seek(pos + 1 + sec_len)

            # Hierarchy (the last one wins if a writer emitted several)
            hier = [b for b in self._blocks
                    if b[0] in (self.BL_HIER, self.BL_HIER_LZ4, self.BL_HIER_LZ4DUO)]
            if not hier:
                raise ValueError("FST file has no hierarchy block")
            btype, bpos, bslen = hier[-1]
            f.seek(bpos + 9)
            self._parse_hierarchy(self._read_hier_block(f, btype, bslen - 8), vcd)
            if progress_cb:
                progress_cb(f.tell(), file_size)

            # Geometry: bit width per handle (0 = real)
            for btype, bpos, bslen in self._blocks:
                if btype == self.BL_GEOM:
                    f.seek(bpos + 9)
                    self._parse_geom(f, bslen - 8)
                    break

            # Value changes, collected per handle as parallel lists
            self._times = {}
            self._vals = {}
            vc_blocks = [(p, s) for t, p, s in self._blocks
                         if t in (self.BL_VCDATA, self.BL_VCDATA_DYN_ALIAS,
                                  self.BL_VCDATA_DYN_ALIAS2)]
            for bi, (bpos, bslen) in enumerate(vc_blocks):
                btype = next(t for t, p, s in self._blocks if p == bpos)
                f.seek(bpos + 9)
                self._parse_vc_block(f, btype, bpos, bslen, vcd)
                if progress_cb:
                    progress_cb(min(file_size, bpos + bslen), file_size)

            # Blackouts ($dumpoff regions): every signal goes 'x' there, as the
            # VCD parser does for $dumpoff.
            for btype, bpos, bslen in self._blocks:
                if btype == self.BL_BLACKOUT:
                    f.seek(bpos + 9)
                    self._apply_blackouts(f.read(bslen - 8))

        self._store(vcd)
        _compact_times(vcd)
        vcd.scopes = build_scope_tree(vcd)
        return vcd

    def _parse_zwrapped(self, f, progress_cb):
        """A '-c' file: gzip of a whole FST after a 17-byte wrapper."""
        import tempfile
        f.seek(0)
        raw = f.read()
        data = zlib.decompress(raw[17:], 15 + 32)
        tmp = tempfile.NamedTemporaryFile(suffix='.fst', delete=False)
        try:
            tmp.write(data)
            tmp.close()
            vcd = FSTReader(tmp.name).parse(progress_cb)
            vcd._filename = self.filename
            return vcd
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ── header ───────────────────────────────────────────────────────

    def _parse_header(self, f, vcd):
        self._read_u64(f)                      # section length
        start_time = self._read_u64(f)
        end_time = self._read_u64(f)
        endian_test = f.read(8)
        self._real_fmt = '<d' if endian_test == struct.pack('<d', 2.7182818284590452354) else '>d'
        self._read_u64(f)                      # memory used by writer
        self._read_u64(f)                      # scope count
        self._read_u64(f)                      # var count
        self._max_handle = self._read_u64(f)
        self._read_u64(f)                      # value-change section count
        ts_byte = struct.unpack('b', f.read(1))[0]
        ts_num, ts_unit = self._TS_MAP.get(ts_byte, (1, 'ns'))
        vcd.timescale = "%d%s" % (ts_num, ts_unit)
        vcd.timescale_number = ts_num
        vcd.timescale_unit = ts_unit
        vcd.version = f.read(128).split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        vcd.date = f.read(119).split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
        f.read(1)                              # file type (verilog/vhdl/…)
        f.read(8)                              # timezero (display offset)
        vcd.min_time = start_time
        vcd.max_time = end_time

    # ── hierarchy ────────────────────────────────────────────────────

    def _read_hier_block(self, f, btype, data_len):
        uncomp_len = self._read_u64(f)
        if btype == self.BL_HIER:
            comp = f.read(data_len - 8)
            for wbits in (15 + 32, 0, -15):
                try:
                    return zlib.decompress(comp, wbits) if wbits else zlib.decompress(comp)
                except zlib.error:
                    continue
            raise ValueError("FST hierarchy: zlib decompression failed")
        if btype == self.BL_HIER_LZ4:
            return lz4_block_decompress(f.read(data_len - 8), uncomp_len)
        # LZ4DUO: lz4(lz4(data)); the second length is the middle stage
        mid_len = self._read_u64(f)
        mid = lz4_block_decompress(f.read(data_len - 16), mid_len)
        return lz4_block_decompress(mid, uncomp_len)

    def _parse_hierarchy(self, data, vcd):
        pos, n = 0, len(data)
        scope_stack = []
        next_handle = 0
        self._handles = [None]
        while pos < n:
            tag = data[pos]
            if tag == self.HT_SCOPE:
                pos += 2                                   # tag + scope type
                name, pos = self._read_cstring(data, pos)
                _component, pos = self._read_cstring(data, pos)
                scope_stack.append(name)
            elif tag == self.HT_UPSCOPE:
                pos += 1
                if scope_stack:
                    scope_stack.pop()
            elif tag == self.HT_ATTRBEGIN:
                pos += 3                                   # tag + type + subtype
                _name, pos = self._read_cstring(data, pos)
                _arg, pos = self._read_varint(data, pos)
            elif tag == self.HT_ATTREND:
                pos += 1
            elif tag <= self.HT_VAR_MAX:
                var_type = tag
                pos += 2                                   # tag + direction
                name, pos = self._read_cstring(data, pos)
                length, pos = self._read_varint(data, pos)
                alias, pos = self._read_varint(data, pos)
                if alias == 0:
                    next_handle += 1
                    handle = next_handle
                else:
                    handle = alias
                scope = ".".join(scope_stack)
                type_name = _VAR_TYPE_NAMES.get(var_type, 'wire')
                is_real = var_type in _REAL_TYPES
                # Preserve the declaration range with the same identity and
                # display conventions as the VCD parser.
                disp, declared_range = split_reference(name)
                size = 64 if is_real else length         # reals: 64, as fst2vcd declares them
                hid = str(handle)
                if hid not in vcd.signals:
                    vcd.signals[hid] = VCDSignal(disp, hid, size, type_name, scope, declared_range)
                    vcd.signal_order.append(hid)
                else:
                    key = "%s #%d" % (hid, len(vcd.signal_order))
                    vcd.signals[key] = AliasSignal(disp, hid, size, type_name,
                                                   scope, vcd.signals[hid], declared_range)
                    vcd.signal_order.append(key)
                while len(self._handles) <= handle:
                    self._handles.append(None)
                if self._handles[handle] is None:
                    self._handles[handle] = {'hid': hid, 'size': length, 'real': is_real}
            else:
                pos += 1
        self._max_handle = max(self._max_handle, next_handle)

    def _parse_geom(self, f, data_len):
        uncomp_len = self._read_u64(f)
        count = self._read_u64(f)
        comp = f.read(data_len - 16)
        data = comp if len(comp) == uncomp_len else zlib.decompress(comp)
        geom = [0]
        pos = 0
        for _ in range(count):
            val, pos = self._read_varint(data, pos)
            geom.append(0 if val == 0 else (0 if val == 0xFFFFFFFF else val))
        self._geom = geom

    def _width(self, handle):
        """(bit width, is_real) for a handle, geometry first, hierarchy second."""
        h = self._handles[handle] if handle < len(self._handles) else None
        if handle < len(self._geom):
            g = self._geom[handle]
            if g == 0:
                return 8, True
            return g, bool(h and h['real'])
        if h is None:
            return 1, False
        return (8, True) if h['real'] else (h['size'], False)

    # ── value-change blocks ──────────────────────────────────────────

    def _unpack_chain(self, packtype, chunk):
        uclen, skip = self._read_varint(chunk, 0)
        body = chunk[skip:]
        if uclen == 0:
            return body
        if packtype == 0x34:                   # '4'
            return lz4_block_decompress(body, uclen)
        if packtype == 0x46:                   # 'F'
            return fastlz_decompress(body, uclen)
        return zlib.decompress(body)

    def _parse_vc_block(self, f, btype, bpos, bslen, vcd):
        block_end = bpos + 1 + bslen
        beg_time = self._read_u64(f)
        end_time = self._read_u64(f)
        self._read_u64(f)                              # mem_required
        if end_time > vcd.max_time:
            vcd.max_time = end_time

        # Frame: every signal's value at beg_time
        frame_uclen = self._fst_varint_file(f)
        frame_clen = self._fst_varint_file(f)
        frame_maxhandle = self._fst_varint_file(f)
        frame_comp = f.read(frame_clen)
        frame = frame_comp if frame_clen == frame_uclen else zlib.decompress(frame_comp)

        vc_maxhandle = self._fst_varint_file(f)
        vc_start = f.tell()                            # chain offsets count from here
        packtype = f.read(1)[0]

        # Footer: time table, then (backwards) the chain table
        f.seek(block_end - 24)
        tsec_uclen, tsec_clen, tsec_nitems = struct.unpack('>QQQ', f.read(24))
        tsec_start = block_end - 24 - tsec_clen
        f.seek(tsec_start)
        tcomp = f.read(tsec_clen)
        tdata = tcomp if tsec_clen == tsec_uclen else zlib.decompress(tcomp)
        times = []
        pos = 0
        tv = 0
        for _ in range(tsec_nitems):
            d, pos = self._read_varint(tdata, pos)
            tv += d
            times.append(tv)

        f.seek(tsec_start - 8)
        chain_clen = self._read_u64(f)
        chain_pos = tsec_start - 8 - chain_clen
        f.seek(chain_pos)
        chainbuf = f.read(chain_clen)
        offsets, lengths = self._decode_chain_table(btype, chainbuf, chain_pos - vc_start)

        f.seek(vc_start)
        vcdata = f.read(chain_pos - vc_start)

        # The frame is each signal's value when the block began. For every
        # block but the first that is the last value already stored; for the
        # first block it carries values a writer emitted before any
        # timestamp (a VCD's $dumpvars before '#0'), which _store applies as
        # the initial value of signals that have no change at beg_time.
        if self._frame0 is None:
            fvals = {}
            fpos = 0
            for h in range(1, min(frame_maxhandle, len(self._handles) - 1) + 1):
                width, is_real = self._width(h)
                if is_real:
                    raw = frame[fpos:fpos + 8]
                    fpos += 8
                    if len(raw) == 8:
                        fvals[h] = repr(struct.unpack(self._real_fmt, raw)[0])
                else:
                    raw = frame[fpos:fpos + width]
                    fpos += width
                    if len(raw) == width:
                        v = raw.decode('ascii', 'replace')
                        if v.strip('xX'):                  # all-x is the default anyway
                            fvals[h] = v
            self._frame0 = (beg_time, fvals)

        # Chains
        for h in range(1, min(vc_maxhandle, len(offsets) - 1) + 1):
            off = offsets[h]
            if not off:
                continue
            ln = lengths[h]
            chunk = vcdata[off:off + ln] if ln > 0 else b''
            if not chunk:
                continue
            try:
                mu = self._unpack_chain(packtype, chunk)
            except Exception as e:
                raise ValueError("FST chain for handle %d: %s" % (h, e))
            self._decode_chain(h, mu, times, beg_time)

    def _decode_chain_table(self, btype, chainbuf, end_offset):
        """Return (offsets, lengths) indexed by handle (index 0 unused)."""
        n = len(chainbuf)
        offsets = [0]
        lengths = [0]
        pnt = 0
        pval = 0
        pidx = 0
        prev_alias = 0
        if btype == self.BL_VCDATA_DYN_ALIAS2:
            while pnt < n:
                if chainbuf[pnt] & 1:
                    shval, pnt = self._read_svint(chainbuf, pnt)
                    shval >>= 1
                    if shval > 0:
                        pval += shval
                        offsets.append(pval)
                        lengths.append(0)
                        if len(offsets) > 2:
                            lengths[pidx] = pval - offsets[pidx]
                        pidx = len(offsets) - 1
                    elif shval < 0:
                        offsets.append(0)
                        lengths.append(shval)              # negative: alias
                        prev_alias = shval
                    else:
                        offsets.append(0)
                        lengths.append(prev_alias)
                else:
                    val, pnt = self._read_varint(chainbuf, pnt)
                    for _ in range(val >> 1):
                        offsets.append(0)
                        lengths.append(0)
        else:
            while pnt < n:
                val, pnt = self._read_varint(chainbuf, pnt)
                if val == 0:
                    val, pnt = self._read_varint(chainbuf, pnt)
                    offsets.append(0)
                    lengths.append(-val)                   # alias of handle val
                elif val & 1:
                    pval += val >> 1
                    offsets.append(pval)
                    lengths.append(0)
                    if len(offsets) > 2:
                        lengths[pidx] = pval - offsets[pidx]
                    pidx = len(offsets) - 1
                else:
                    for _ in range(val >> 1):
                        offsets.append(0)
                        lengths.append(0)
        if len(offsets) > 1 and pidx:
            lengths[pidx] = end_offset - offsets[pidx]
        # Resolve aliases: a negative length -L names handle index L-1.
        for i in range(1, len(offsets)):
            if lengths[i] < 0 and offsets[i] == 0:
                src = -lengths[i] - 1
                if 0 < src < i:
                    offsets[i] = offsets[src]
                    lengths[i] = lengths[src]
                else:
                    lengths[i] = 0
        return offsets, lengths

    def _decode_chain(self, h, mu, times, beg_time):
        width, is_real = self._width(h)
        tl = self._times.get(h)
        if tl is None:
            tl = self._times[h] = []
            vl = self._vals[h] = []
        else:
            vl = self._vals[h]
        n = len(mu)
        pos = 0
        tidx = 0
        ntimes = len(times)
        rv = _read_varint = self._read_varint
        if width == 1 and not is_real:
            while pos < n:
                vli, pos = rv(mu, pos)
                if vli & 1:
                    tidx += vli >> 4
                    val = _RCV[(vli >> 1) & 7]
                else:
                    tidx += vli >> 2
                    val = '1' if (vli >> 1) & 1 else '0'
                if tidx >= ntimes:
                    break
                tl.append(times[tidx])              # same-time changes are kept,
                vl.append(val)                      # as the VCD parser keeps them
            return
        nbytes = (width + 7) >> 3
        fmt = self._real_fmt
        while pos < n:
            vli, pos = rv(mu, pos)
            tidx += vli >> 1
            if is_real:
                raw = mu[pos:pos + 8]
                pos += 8
                if len(raw) < 8:
                    break
                val = repr(struct.unpack(fmt, raw)[0])
            elif vli & 1:                              # one char per bit
                raw = mu[pos:pos + width]
                pos += width
                if len(raw) < width:
                    break
                val = raw.decode('ascii', 'replace')
            else:                                      # packed bits, MSB first
                raw = mu[pos:pos + nbytes]
                pos += nbytes
                if len(raw) < nbytes:
                    break
                val = format(int.from_bytes(raw, 'big'), '0%db' % (nbytes * 8))[:width]
            if tidx >= ntimes:
                break
            tl.append(times[tidx])
            vl.append(val)

    # ── blackouts ────────────────────────────────────────────────────

    def _apply_blackouts(self, data):
        count, pos = self._read_varint(data, 0)
        t = 0
        events = []
        for _ in range(count):
            if pos >= len(data):
                break
            active = data[pos]
            pos += 1
            d, pos = self._read_varint(data, pos)
            t += d
            events.append((t, bool(active)))
        import bisect
        for t, active in events:
            if active:
                continue                        # values resume via the chains
            for h in range(1, len(self._handles)):
                if self._handles[h] is None:
                    continue
                tl = self._times.setdefault(h, [])
                vl = self._vals.setdefault(h, [])
                i = bisect.bisect_right(tl, t)
                if i and tl[i - 1] == t:
                    continue                    # a real change at t wins
                tl.insert(i, t)
                vl.insert(i, 'x')

    # ── store into VCDSignal ─────────────────────────────────────────

    def _store(self, vcd):
        import array
        beg0, fvals = self._frame0 if self._frame0 else (0, {})
        for h in range(1, len(self._handles)):
            info = self._handles[h]
            if info is None:
                continue
            sig = vcd.signals.get(info['hid'])
            if sig is None:
                continue
            tl = self._times.get(h) or []
            vl = self._vals.get(h) or []
            fv = fvals.get(h)
            if fv is not None and (not tl or tl[0] > beg0):
                tl.insert(0, beg0)
                vl.insert(0, fv)
            if not tl:
                continue
            sig._times = array.array('q', tl)
            if sig._scalar:
                from .core import _S2B
                sig._values = bytearray(_S2B.get(v, 120) for v in vl)
            elif sig._packed:
                enc = sig._encode_values(vl)
                if enc is None:
                    sig._packed = False
                    sig._values = [v for v in vl]
                else:
                    sig._values = enc
            else:
                sig._values = list(vl)
        self._times = self._vals = None
