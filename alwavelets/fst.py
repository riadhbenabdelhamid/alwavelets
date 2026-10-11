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
import array
from contextlib import ExitStack, contextmanager
import io
import os
import struct
import zlib

from .core import VCDSignal, AliasSignal, split_reference
from .fst_stream import CHUNK, Cursor, FileSlice, Records, decompress
from .streaming import check_cancel

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


def checkpoint_state(phase='values', block=0, handle=1, position=0, time_index=0):
    """Next unread work; position is a byte offset in the decompressed chain."""
    return dict(phase=phase, block=block, handle=handle, position=position, time_index=time_index)


def valid_checkpoint(state):
    return (isinstance(state, dict) and state.keys() == checkpoint_state().keys() and
            state['phase'] in ('values', 'frame', 'blackouts', 'done') and
            all(type(state[k]) is int and state[k] >= 0 for k in ('block', 'position', 'time_index')) and
            type(state['handle']) is int and state['handle'] >= 1 and
            (state['phase'] == 'values' or state['position'] == state['time_index'] == 0))


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
        self._catalog = None
        self._handles = []           # handle -> {name,size,type,scope,hid,real}
        self._max_handle = 0
        self._geom = []              # handle -> bit length (0 = real)
        self._real_fmt = '<d'
        self._frame0 = None          # (beg_time, {handle: value}) of the first VC block
        self._resume_vcd = self._resume = self._checkpoint = self._chain_state = None

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
        while pos < n and shift < 70:
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                if result >= 1 << 64:
                    raise ValueError('FST varint overflow')
                return result, pos
            shift += 7
        raise ValueError('Truncated or oversized FST varint')

    @staticmethod
    def _read_svint(data, pos):
        """FST signed varint: LEB128 whose last byte's 0x40 bit sign-extends
        (fstGetSVarint64) — not zigzag."""
        result = 0
        shift = 0
        n = len(data)
        b = 0
        while pos < n and shift < 70:
            b = data[pos]
            pos += 1
            result |= (b & 0x7f) << shift
            shift += 7
            if not (b & 0x80):
                break
        else:
            raise ValueError('Truncated or oversized signed FST varint')
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
        while shift < 70:
            b = f.read(1)
            if not b:
                raise ValueError('Truncated FST varint')
            b = b[0]
            result |= (b & 0x7f) << shift
            if not (b & 0x80):
                if result >= 1 << 64:
                    raise ValueError('FST varint overflow')
                return result
            shift += 7
        raise ValueError('Oversized FST varint')

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

    @classmethod
    def prefers_disk(cls, filename, threshold, *, cancel=None):
        """Cheap, conservative expansion estimate; never decompress the waveform."""
        try:
            size = os.path.getsize(filename)
            with open(filename, 'rb') as f:
                magic = f.read(1)
                # Looking through a gzip wrapper would require a full scan.
                if magic == bytes([cls.BL_ZWRAPPER]):
                    return True
                f.seek(49)
                estimate = cls._read_u64(f) * 640  # declared signal metadata
                position = 0
                while position < size:
                    check_cancel(cancel)
                    f.seek(position)
                    kind = f.read(1)[0]
                    length = cls._read_u64(f)
                    end = position + length + 1
                    if length < 8 or end > size:
                        return True
                    if kind in (cls.BL_VCDATA, cls.BL_VCDATA_DYN_ALIAS, cls.BL_VCDATA_DYN_ALIAS2):
                        f.seek(position + 25)
                        # Unlike the header's fixed writer budget, this field
                        # measures actual uncompressed value-chain bytes.
                        estimate += cls._read_u64(f) * 48
                        estimate += cls._fst_varint_file(f)  # full initial frame
                        f.seek(end - 8)
                        estimate += cls._read_u64(f) * 8     # timestamp table
                    elif kind in (cls.BL_HIER, cls.BL_HIER_LZ4, cls.BL_HIER_LZ4DUO, cls.BL_GEOM):
                        estimate += cls._read_u64(f)
                    if estimate > threshold:
                        return True
                    position = end
                return False
        except (OSError, ValueError, IndexError, struct.error):
            check_cancel(cancel)
            return True  # Let the bounded parser report the malformed input.

    # ── top level ────────────────────────────────────────────────────

    def parse(self, progress_cb=None, writer_factory=None, metadata_only=False, *, cancel=None, memory_mib=8, stage_cb=None):
        """Read with bounded decompression; eager output still retains all events."""
        with self.prepare(progress_cb, cancel=cancel, memory_mib=memory_mib, stage_cb=stage_cb,
                          compact=metadata_only or writer_factory is not None) as vcd:
            if not metadata_only:
                writer = writer_factory(vcd) if writer_factory else None
                self.read_values(vcd, writer, progress_cb)
            return vcd

    @contextmanager
    def prepare(self, progress_cb=None, *, cancel=None, memory_mib=8, compact=True, catalog=None, resume=None, stage_cb=None):
        """Keep metadata and input open until value loading finishes or is cancelled.

        The context may be handed to a background worker after preparation;
        only that worker may read values, and it must close the context.
        """
        if memory_mib <= 0:
            raise ValueError('FST memory budget must be positive')
        self._catalog = catalog
        self._resume_vcd = resume
        self._cancel = cancel
        self._stage_cb = stage_cb
        self._limit = max(CHUNK, min(1024 * 1024, int(memory_mib * 1024 * 1024) // 16))
        self._frame0, self._geom = None, []
        with ExitStack() as resources:
            self._resources = resources
            try:
                yield self._prepare(progress_cb, compact)
            finally:
                # Drop references to closed spill files and partial eager data.
                self._frame0, self._geom, self._blocks = None, [], []
                self._times = self._vals = None
                self._handles, self._writer, self._file = [], None, None
                self._catalog = None
                self._resume_vcd = self._resume = self._checkpoint = self._chain_state = None

    def _check(self):
        check_cancel(self._cancel)

    def _stage(self, name, done=None, total=None, unit='bytes'):
        from .loading import report_stage
        report_stage(getattr(self, '_stage_cb', None), name, done, total, unit)

    def _records(self, fmt='<Q', resources=None):
        table = Records(fmt, self._limit, self._cancel)
        return (resources or self._resources).enter_context(table)

    def _decompress(self, data, codec, size, **kwargs):
        return decompress(data, codec, size, limit=self._limit, cancel=self._cancel, **kwargs)

    def _prepare(self, progress_cb, compact):
        from .parsers import VCDFile, build_scope_tree
        self._check()
        vcd = self._resume_vcd if self._resume_vcd is not None else VCDFile()
        if self._catalog is not None and self._resume_vcd is None:
            from .catalog import attach_catalog
            attach_catalog(self._catalog.store, vcd, self._catalog)
        vcd._filename = self.filename
        from .analysis_cache import stamp_source
        stamp_source(vcd, self.filename)
        f = self._resources.enter_context(open(self.filename, 'rb'))
        while True:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            f.seek(0)
            first = f.read(1)
            if not first:
                self._file, self._file_size = f, file_size
                vcd.scopes = build_scope_tree(vcd)
                return vcd
            if first[0] == self.BL_ZWRAPPER:
                f = self._unwrap(f)
                continue
            if first[0] != self.BL_HDR:
                raise ValueError("Not an FST file (first byte: 0x%02x)" % first[0])
            self._parse_header(f, vcd)
            if progress_cb:
                progress_cb(f.tell(), file_size)

            # Block map
            self._stage('Reading FST blocks', 0, file_size)
            self._blocks = self._records('<BQQ')
            f.seek(0)
            while True:
                self._check()
                pos = f.tell()
                tb = f.read(1)
                if not tb:
                    break
                sec_len = self._read_u64(f)
                if sec_len < 8 or pos + 1 + sec_len > file_size:
                    raise ValueError('Truncated or invalid FST section')
                self._blocks.append((tb[0], pos, sec_len))
                f.seek(pos + 1 + sec_len)
                if len(self._blocks) % 256 == 0:
                    self._stage('Reading FST blocks', f.tell(), file_size)

            # Hierarchy (the last one wins if a writer emitted several)
            hierarchy = None
            for block in self._blocks:
                if block[0] in (self.BL_HIER, self.BL_HIER_LZ4, self.BL_HIER_LZ4DUO):
                    hierarchy = block
            if hierarchy is None:
                raise ValueError("FST file has no hierarchy block")
            if self._resume_vcd is not None:
                # The durable declaration catalog is authoritative. Rebuild only
                # the bounded handle geometry, without inserting declarations.
                self._handles = _HandleTable(self._records('<QB'))
                self._stage('Restoring signal widths')
                self._handles.records.extend((width, kind in ('real', 'realtime', 'shortreal'))
                                             for width, kind in self._catalog.rows('size,kind', 'master=sid'))
            else:
                btype, bpos, bslen = hierarchy
                f.seek(bpos + 9)
                if compact and self._catalog is None:
                    vcd.signals = {}
                with self._read_hier_block(f, btype, bslen - 8) as hierarchy:
                    self._parse_hierarchy(hierarchy, vcd, compact)
            if progress_cb:
                progress_cb(f.tell(), file_size)

            # Geometry: bit width per handle (0 = real)
            self._stage('Reading signal widths')
            for btype, bpos, bslen in self._blocks:
                if btype == self.BL_GEOM:
                    f.seek(bpos + 9)
                    self._parse_geom(f, bslen - 8)
                    break

            self._file, self._file_size = f, file_size
            if self._resume_vcd is None:
                vcd.scopes = build_scope_tree(vcd, cancel=self._cancel, stage_cb=self._stage_cb)
            return vcd

    def read_values(self, vcd, writer=None, progress_cb=None, *, checkpoint=None, resume=None):
        """Consume a prepared reader without rebuilding declarations or hierarchy."""
        from .parsers import _compact_times
        self._check()
        self._stage('Indexing values' if writer is not None else 'Reading values')
        self._writer = writer
        self._checkpoint = checkpoint
        self._resume = resume or checkpoint_state()
        state = self._resume
        if (not valid_checkpoint(state) or state['block'] > len(self._blocks) or
                state['handle'] > len(self._handles)):
            raise ValueError('Invalid FST checkpoint position')
        if self._cancel is None and writer is not None:
            self._cancel = getattr(getattr(writer, 'store', None), 'cancel', None)
        f, file_size = self._file, self._file_size
        self._times, self._vals = {}, {}
        value_types = (self.BL_VCDATA, self.BL_VCDATA_DYN_ALIAS, self.BL_VCDATA_DYN_ALIAS2)
        if (state['phase'] == 'values' and (state['handle'] != 1 or state['position'] or state['time_index']) and
                (state['block'] == len(self._blocks) or self._blocks[state['block']][0] not in value_types)):
            raise ValueError('Invalid FST checkpoint block')
        # The first frame is still needed when resuming in a later block.
        for btype, bpos, bslen in self._blocks:
            if btype in value_types and state['phase'] in ('values', 'frame'):
                f.seek(bpos + 9)
                self._vc_header(f, bpos, bslen, vcd)
                break
        for ordinal, (btype, bpos, bslen) in enumerate(self._blocks):
            self._check()
            if state['phase'] != 'values' or ordinal < state['block'] or btype not in value_types:
                continue
            self._block_ordinal, self._block_range = ordinal, (bpos, bslen)
            f.seek(bpos + 9)
            self._parse_vc_block(f, btype, bpos, bslen, vcd)
            self._save(checkpoint_state(block=ordinal + 1), bpos + bslen + 1)
            if progress_cb:
                progress_cb(min(file_size, bpos + bslen), file_size)
        if self._writer is not None and state['phase'] in ('values', 'frame'):
            beg0, fvals = self._frame0 if self._frame0 else (0, {})
            if checkpoint is None:
                self._writer.add_initial_values(beg0, ((str(h), value) for h, value in fvals.items()))
            else:
                start = state['handle'] if state['phase'] == 'frame' else 1
                stop = fvals.count + 1 if self._frame0 else 1
                self._save(checkpoint_state('frame', handle=start), file_size, force=True)
                for first in range(start, stop, 512):
                    end = min(stop, first + 512)
                    values = ((str(h), fvals.get(h)) for h in range(first, end))
                    self._writer.add_initial_values(beg0, ((h, value) for h, value in values if value is not None))
                    self._save(checkpoint_state('frame', handle=end), file_size)
                self._save(checkpoint_state('blackouts'), file_size, force=True)

        # Insert blackout changes after initial values, preserving same-tick
        # changes already present in the waveform.
        for ordinal, (btype, bpos, bslen) in enumerate(self._blocks):
            if (btype == self.BL_BLACKOUT and state['phase'] != 'done' and
                    (state['phase'] != 'blackouts' or ordinal >= state['block'])):
                f.seek(bpos + 9)
                with self._decompress(FileSlice(f, bpos + 9, bslen - 8), 'raw', bslen - 8) as blackouts:
                    self._apply_blackouts(blackouts)
                self._save(checkpoint_state('blackouts', block=ordinal + 1), file_size)
        self._save(checkpoint_state('done'), file_size, force=True)
        if self._writer is None:
            self._store(vcd)
            _compact_times(vcd)
        return vcd

    def _save(self, state, done, *, work=0, force=False):
        if self._checkpoint is not None:
            self._checkpoint(state, min(done, self._file_size), self._file_size, work, force)

    def _unwrap(self, f):
        """A '-c' file: gzip of a whole FST after a 17-byte wrapper."""
        f.seek(9)
        expected = self._read_u64(f)
        f.seek(0, os.SEEK_END)
        compressed_size = f.tell() - 17
        unpacked = self._resources.enter_context(self._decompress(
            FileSlice(f, 17, compressed_size), 'zlib', expected, wbits=15 + 32,
            progress_cb=lambda done, total: self._stage('Unpacking FST', done, total)))
        return unpacked.file if unpacked.file is not None else self._resources.enter_context(io.BytesIO(unpacked[:]))

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
        vcd.max_time = max(vcd.max_time, end_time) if self._resume_vcd is not None else end_time

    # ── hierarchy ────────────────────────────────────────────────────

    def _read_hier_block(self, f, btype, data_len):
        uncomp_len = self._read_u64(f)
        progress = lambda done, total: self._stage('Unpacking declarations', done, total)
        if btype == self.BL_HIER:
            data = FileSlice(f, f.tell(), data_len - 8)
            for wbits in (15 + 32, 0, -15):
                try:
                    return self._decompress(data, 'zlib', uncomp_len, wbits=wbits, progress_cb=progress)
                except zlib.error:
                    continue
            raise ValueError("FST hierarchy: zlib decompression failed")
        if btype == self.BL_HIER_LZ4:
            return self._decompress(FileSlice(f, f.tell(), data_len - 8), 'lz4', uncomp_len, progress_cb=progress)
        mid_len = self._read_u64(f)
        with self._decompress(FileSlice(f, f.tell(), data_len - 16), 'lz4', mid_len, progress_cb=progress) as middle:
            return self._decompress(middle, 'lz4', uncomp_len, progress_cb=progress)

    def _parse_hierarchy(self, data, vcd, metadata_only=False):
        data = data.data if data.file is None else data
        pos, n = 0, len(data)
        self._stage('Reading declarations', 0, n)
        reported = 0
        scope_stack = []
        next_handle = 0
        self._handles = _HandleTable(self._records('<QB')) if self._catalog is not None else [None]
        handle_rows = []
        while pos < n:
            self._check()
            if pos - reported >= 65536:
                self._stage('Reading declarations', pos, n)
                reported = pos
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
                    if alias > next_handle:
                        raise ValueError('Invalid FST hierarchy alias')
                    handle = alias
                scope = ".".join(scope_stack)
                type_name = _VAR_TYPE_NAMES.get(var_type, 'wire')
                is_real = var_type in _REAL_TYPES
                # Preserve the declaration range with the same identity and
                # display conventions as the VCD parser.
                disp, declared_range = split_reference(name)
                size = 64 if is_real else length         # reals: 64, as fst2vcd declares them
                hid = str(handle)
                if self._catalog is not None:
                    self._catalog.declare(disp, hid, size, type_name, scope, declared_range, deduplicate=False)
                elif hid not in vcd.signals:
                    vcd.signals[hid] = VCDSignal(disp, hid, size, type_name, scope, declared_range,
                                                metadata_only=metadata_only)
                    vcd.signal_order.append(hid)
                else:
                    key = "%s #%d" % (hid, len(vcd.signal_order))
                    vcd.signals[key] = AliasSignal(disp, hid, size, type_name,
                                                   scope, vcd.signals[hid], declared_range)
                    vcd.signal_order.append(key)
                if isinstance(self._handles, _HandleTable):
                    if alias == 0:
                        handle_rows.append((length, is_real))
                        if len(handle_rows) >= 1024:
                            self._handles.records.extend(handle_rows)
                            handle_rows.clear()
                else:
                    while len(self._handles) <= handle:
                        self._handles.append(None)
                    if self._handles[handle] is None:
                        self._handles[handle] = {'hid': hid, 'size': length, 'real': is_real}
            else:
                pos += 1
        if handle_rows:
            self._handles.records.extend(handle_rows)
        self._max_handle = max(self._max_handle, next_handle)

    def _parse_geom(self, f, data_len):
        uncomp_len = self._read_u64(f)
        count = self._read_u64(f)
        codec = 'raw' if data_len - 16 == uncomp_len else 'zlib'
        geom = self._records()
        geom.append(0)
        with self._decompress(FileSlice(f, f.tell(), data_len - 16), codec, uncomp_len) as data:
            pos = 0
            for _ in range(count):
                self._check()
                val, pos = self._read_varint(data, pos)
                geom.append(0 if val in (0, 0xFFFFFFFF) else val)
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

    def _parse_vc_block(self, f, btype, bpos, bslen, vcd):
        with ExitStack() as resources:
            self._read_vc_block(f, btype, bpos, bslen, vcd, resources)

    def _vc_header(self, f, bpos, bslen, vcd):
        block_end = bpos + 1 + bslen
        beg_time = self._read_u64(f)
        end_time = self._read_u64(f)
        self._read_u64(f)  # writer memory estimate; also used by the auto policy
        vcd.max_time = max(vcd.max_time, end_time)
        frame_uclen = self._fst_varint_file(f)
        frame_clen = self._fst_varint_file(f)
        frame_maxhandle = self._fst_varint_file(f)
        frame_start = f.tell()
        if frame_start + frame_clen > block_end:
            raise ValueError('Invalid FST frame range')
        if self._frame0 is None:
            frame = self._resources.enter_context(self._decompress(
                FileSlice(f, frame_start, frame_clen), 'raw' if frame_clen == frame_uclen else 'zlib', frame_uclen))
            values = _FrameValues(self, frame, min(frame_maxhandle, len(self._handles) - 1))
            self._frame0 = (beg_time, values)
        f.seek(frame_start + frame_clen)
        return beg_time

    def _read_vc_block(self, f, btype, bpos, bslen, vcd, resources):
        block_end = bpos + 1 + bslen
        beg_time = self._vc_header(f, bpos, bslen, vcd)
        vc_maxhandle = self._fst_varint_file(f)
        vc_start = f.tell()
        packtype = f.read(1)[0]
        if packtype not in (0x34, 0x46, 0x5a):
            raise ValueError('Unknown FST chain compression')
        f.seek(block_end - 24)
        tsec_uclen, tsec_clen, tsec_nitems = struct.unpack('>QQQ', f.read(24))
        tsec_start = block_end - 24 - tsec_clen
        if tsec_start < vc_start + 8:
            raise ValueError('Invalid FST time table range')
        times = self._records(resources=resources)
        with self._decompress(FileSlice(f, tsec_start, tsec_clen),
                              'raw' if tsec_clen == tsec_uclen else 'zlib', tsec_uclen) as data:
            cursor, tv = Cursor(data, self._cancel), 0
            batch = array.array('Q')
            for _ in range(tsec_nitems):
                tv += cursor.varint()
                batch.append(tv)
                if len(batch) == CHUNK // 8:
                    times.extend(batch)
                    batch = array.array('Q')
            times.extend(batch)
        f.seek(tsec_start - 8)
        chain_clen = self._read_u64(f)
        chain_pos = tsec_start - 8 - chain_clen
        if chain_pos < vc_start:
            raise ValueError('Invalid FST chain table range')
        with self._decompress(FileSlice(f, chain_pos, chain_clen), 'raw', chain_clen) as table:
            offsets, lengths = self._decode_chain_table(btype, table, chain_pos - vc_start, resources)
        codec = {0x34: 'lz4', 0x46: 'fastlz', 0x5a: 'zlib'}[packtype]
        last_handle = min(vc_maxhandle, len(offsets) - 1)
        state = self._resume
        resuming = state['phase'] == 'values' and self._block_ordinal == state['block']
        first_handle = state['handle'] if resuming else 1
        if first_handle > last_handle + 1:
            raise ValueError('Invalid FST checkpoint handle')
        self._block_handles = max(1, last_handle)
        for h in range(first_handle, last_handle + 1):
            self._check()
            off, size = offsets[h], lengths[h]
            if not off or not size:
                if resuming and h == first_handle and state['position']:
                    raise ValueError('Invalid FST checkpoint chain')
                if h % 512 == 0:
                    self._save(checkpoint_state(block=self._block_ordinal, handle=h + 1),
                               bpos + bslen * h // self._block_handles)
                continue
            if size < 0 or off < 1 or vc_start + off + size > chain_pos:
                raise ValueError('Invalid FST chain range')
            f.seek(vc_start + off)
            unpacked_size = self._fst_varint_file(f)
            start = f.tell()
            size -= start - (vc_start + off)
            data = FileSlice(f, start, size)
            with self._decompress(data, codec if unpacked_size else 'raw', unpacked_size or size) as chain:
                cursor, tidx = Cursor(chain, self._cancel), 0
                if resuming and h == first_handle:
                    cursor.pos, tidx = state['position'], state['time_index']
                self._chain_state = [cursor, tidx]
                if cursor.pos > len(chain) or (cursor.pos and tidx >= len(times)):
                    raise ValueError('Invalid FST checkpoint chain position')
                self._decode_chain(h, chain, times, beg_time)
            self._save(checkpoint_state(block=self._block_ordinal, handle=h + 1),
                       bpos + bslen * h // self._block_handles)

    def _decode_chain_table(self, btype, chainbuf, end_offset, resources=None):
        """Return (offsets, lengths) indexed by handle (index 0 unused)."""
        n = len(chainbuf)
        offsets = self._records('<q', resources)
        lengths = self._records('<q', resources)
        offsets.append(0)
        lengths.append(0)
        pnt = 0
        pval = 0
        pidx = 0
        prev_alias = 0
        if btype == self.BL_VCDATA_DYN_ALIAS2:
            while pnt < n:
                self._check()
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
                    if len(offsets) + (val >> 1) > len(self._handles):
                        raise ValueError('FST chain table exceeds declared handles')
                    for _ in range(val >> 1):
                        offsets.append(0)
                        lengths.append(0)
        else:
            while pnt < n:
                self._check()
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
                    if len(offsets) + (val >> 1) > len(self._handles):
                        raise ValueError('FST chain table exceeds declared handles')
                    for _ in range(val >> 1):
                        offsets.append(0)
                        lengths.append(0)
        if len(offsets) > 1 and pidx:
            lengths[pidx] = end_offset - offsets[pidx]
        # Alias magnitudes are one-based handles. Unlike libfst's zero-based
        # arrays, our tables already reserve slot zero, so no subtraction here.
        for i in range(1, len(offsets)):
            self._check()
            if lengths[i] < 0 and offsets[i] == 0:
                src = -lengths[i]
                if 0 < src < i:
                    offsets[i] = offsets[src]
                    lengths[i] = lengths[src]
                else:
                    lengths[i] = 0
        return offsets, lengths

    def _decode_chain(self, h, mu, times, beg_time):
        writer = getattr(self, '_writer', None)
        if writer is not None:
            info = self._handles[h] if h < len(self._handles) else None
            if info is None:
                return
            key = info['hid']
            previous = self._chain_state[0].pos
            for timestamps, values in self._chain_batches(h, mu, times):
                self._check()
                if self._frame0 and not writer.has_values(key):
                    beg0, fvals = self._frame0
                    initial = fvals.get(h)
                    if timestamps[0] > beg0 and initial is not None:
                        writer.add(key, beg0, initial)
                writer.add_batch(key, timestamps, values)
                writer.publish()
                if self._checkpoint is not None:
                    cursor, tidx = self._chain_state
                    position = cursor.pos
                    bpos, bslen = self._block_range
                    done = bpos + bslen * ((h - 1) * len(mu) + position) // (self._block_handles * max(1, len(mu)))
                    self._save(checkpoint_state(block=self._block_ordinal, handle=h, position=position, time_index=tidx),
                               done, work=position - previous)
                    previous = position
            writer.publish()
        else:
            tl = self._times.setdefault(h, [])
            vl = self._vals.setdefault(h, [])
            for t, value in self._chain_events(h, mu, times):
                self._check()
                tl.append(t)
                vl.append(value)

    def _chain_batches(self, h, mu, times):
        """Decode bounded pages; scalar values stay bytes throughout ingestion."""
        from .storage import PAGE_BYTES, PAGE_EVENTS
        width, is_real = self._width(h)
        scalar = width == 1 and not is_real
        count = min(PAGE_EVENTS, max(1, PAGE_BYTES // (17 if scalar else (96 if is_real else width + 64))))
        state = self._chain_state if self._checkpoint is not None else None
        cursor, tidx = state if state is not None else (Cursor(mu, self._cancel), 0)
        rv, read = cursor.varint, cursor.read
        end, ntimes = len(mu), len(times)
        page, page_start, page_end = (), 0, 0
        nbytes = (width + 7) >> 3
        packed_format = '0%db' % (nbytes * 8)
        real = struct.Struct(self._real_fmt).unpack
        while cursor.pos < end:
            self._check()
            timestamps = array.array('q')
            values = bytearray() if scalar else []
            for _ in range(count):
                if cursor.pos >= end:
                    break
                vli = rv()
                if scalar:
                    if vli & 1:
                        next_tidx = tidx + (vli >> 4)
                        value = b'xzxxxxxx'[(vli >> 1) & 7]
                    else:
                        next_tidx = tidx + (vli >> 2)
                        value = 48 + ((vli >> 1) & 1)
                else:
                    next_tidx = tidx + (vli >> 1)
                    if is_real:
                        value = repr(real(read(8))[0])
                    elif vli & 1:
                        value = read(width).decode('ascii', 'replace')
                    else:
                        value = format(int.from_bytes(read(nbytes), 'big'), packed_format)[:width]
                if next_tidx >= ntimes:
                    end = cursor.pos
                    break
                tidx = next_tidx
                if not page_start <= tidx < page_end:
                    page_start = tidx
                    page = times.uint64_page(tidx)
                    page_end = tidx + len(page)
                timestamps.append(page[tidx - page_start])
                values.append(value)
            if timestamps:
                # Publish the cursor only at a complete batch boundary. The
                # checkpoint records the next byte and last event's time index,
                # including repeated timestamps and multi-byte time deltas.
                if state is not None:
                    state[1] = tidx
                self._check()
                yield timestamps, values

    def _chain_events(self, h, mu, times):
        """Decode text events for the eager reader, including FST logic states."""
        cursor, tidx = Cursor(mu, self._cancel), 0
        width, is_real = self._width(h)
        ntimes = len(times)
        rv = cursor.varint
        if width == 1 and not is_real:
            while cursor:
                if cursor.pos % 4096 == 0:
                    self._check()
                vli = rv()
                if vli & 1:
                    tidx += vli >> 4
                    val = _RCV[(vli >> 1) & 7]
                else:
                    tidx += vli >> 2
                    val = '1' if (vli >> 1) & 1 else '0'
                if tidx >= ntimes:
                    break
                yield times[tidx], val
            return
        nbytes = (width + 7) >> 3
        fmt = self._real_fmt
        while cursor:
            self._check()
            vli = rv()
            tidx += vli >> 1
            if is_real:
                raw = cursor.read(8)
                val = repr(struct.unpack(fmt, raw)[0])
            elif vli & 1:                              # one char per bit
                raw = cursor.read(width)
                val = raw.decode('ascii', 'replace')
            else:                                      # packed bits, MSB first
                raw = cursor.read(nbytes)
                val = format(int.from_bytes(raw, 'big'), '0%db' % (nbytes * 8))[:width]
            if tidx >= ntimes:
                break
            yield times[tidx], val

    # ── blackouts ────────────────────────────────────────────────────

    def _apply_blackouts(self, data):
        count, pos = self._read_varint(data, 0)
        t = 0
        import bisect
        writer = getattr(self, '_writer', None)
        if writer is not None:
            writer.publish(True)
        for _ in range(count):
            self._check()
            if pos >= len(data):
                raise ValueError('Truncated FST blackout table')
            active = data[pos]
            delta, pos = self._read_varint(data, pos + 1)
            t += delta
            if active:
                continue
            for h in range(1, len(self._handles)):
                self._check()
                info = self._handles[h]
                if info is None:
                    continue
                if writer is not None:
                    sig = writer.vcd.signals[info['hid']]
                    index = sig._times.bisect(t)
                    if index >= sig.num_changes() or sig._times[index] != t:
                        writer.add(info['hid'], t, 'x')
                else:
                    tl = self._times.setdefault(h, [])
                    vl = self._vals.setdefault(h, [])
                    index = bisect.bisect_right(tl, t)
                    if index and tl[index - 1] == t:
                        continue
                    tl.insert(index, t)
                    vl.insert(index, 'x')

    # ── store into VCDSignal ─────────────────────────────────────────

    def _store(self, vcd):
        import array
        beg0, fvals = self._frame0 if self._frame0 else (0, {})
        for h in range(1, len(self._handles)):
            self._check()
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


class _FrameValues:
    """Initial values decoded one at a time from a retained, spillable frame."""
    def __init__(self, reader, data, count):
        self.reader, self.data, self.count = reader, data, count
        self.offsets = reader._records()
        position = 0
        self.offsets.append(0)
        for h in range(1, count + 1):
            self.offsets.append(position)
            width, real = reader._width(h)
            position += 8 if real else width
        if position > len(data):
            raise ValueError('Truncated FST initial frame')

    def get(self, handle, default=None):
        if not 1 <= handle <= self.count:
            return default
        width, real = self.reader._width(handle)
        start = self.offsets[handle]
        raw = self.data[start:start + (8 if real else width)]
        if real:
            return repr(struct.unpack(self.reader._real_fmt, raw)[0])
        value = raw.decode('ascii', 'replace')
        return value if value.strip('xX') else default

    def items(self):
        for h in range(1, self.count + 1):
            self.reader._check()
            value = self.get(h)
            if value is not None:
                yield h, value


class _HandleTable:
    """Compact, spillable FST handle geometry; identifiers derive from indexes."""
    def __init__(self, records):
        self.records = records

    def __len__(self):
        return len(self.records) + 1

    def __getitem__(self, handle):
        if handle == 0:
            return None
        size, real = self.records[handle - 1]
        return {'hid': str(handle), 'size': size, 'real': bool(real)}
