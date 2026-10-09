"""Persistence: JSON-backed stores for ISA signal tags, waveform
annotations, and bookmarks (saved as sidecar files next to a trace)."""
import json
import os
import re


def _write_json_atomic(path, data):
    """Serialise first, write a temp file beside `path`, then rename it over.
    A failure (unserialisable value, disk full, crash mid-write) leaves the
    previous sidecar untouched instead of a truncated one."""
    text = json.dumps(data, indent=2)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, 'w') as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def signal_identities(vcd, ids=None):
    ids = vcd.signals if ids is None else ids
    return {sid: vcd.signals[sid].identity() for sid in ids if sid in vcd.signals}


def view_reference_ids(viewer):
    ids = set(viewer.display_order)
    for field in ('analog_signals', 'selected_signals', 'sig_radix', 'sig_color', 'sig_height'):
        ids.update(getattr(viewer, field, ()))
    for group in viewer.groups:
        ids.update(group['members'])
    return ids


def remap_view_state(vcd, data):
    """Resolve v2 view references by declaration, retaining v1 compatibility.

    An ambiguous or missing declaration is omitted, never rebound to whatever
    happens to have its old VCD identifier. Returns (state, missing count).
    """
    if not isinstance(data, dict):
        raise ValueError('view state must be an object')
    if data.get('version', 1) == 1:
        return data, 0
    if data.get('version') != 2 or not isinstance(data.get('signal_identities'), dict):
        raise ValueError('unsupported view state version')
    by_identity = {}
    for sid, identity in signal_identities(vcd).items():
        key = json.dumps(identity, sort_keys=True)
        by_identity.setdefault(key, []).append(sid)
    mapping = {}
    for old, identity in data['signal_identities'].items():
        matches = by_identity.get(json.dumps(identity, sort_keys=True), [])
        if len(matches) == 1:
            mapping[old] = matches[0]
    out = dict(data)
    referenced = set()

    def remap(items):
        referenced.update(items)
        return [mapping[sid] for sid in items if sid in mapping]

    for field in ('display_order', 'analog_signals', 'selected_signals'):
        if field in data:
            out[field] = remap(data[field])
    for field in ('sig_radix', 'sig_color', 'sig_height'):
        if field in data:
            referenced.update(data[field])
            out[field] = {mapping[sid]: value for sid, value in data[field].items() if sid in mapping}
    out['groups'] = [dict(g, members=remap(g.get('members', []))) for g in data.get('groups', [])]
    return out, len(referenced - mapping.keys())


def apply_session_times(viewer, data):
    """Apply time state only once the trace's final extent is available."""
    if getattr(viewer.vcd, 'is_loading', False) or not getattr(viewer.vcd, '_extent_known', True):
        viewer._pending_session_times = {k: data[k] for k in ('view', 'cursors', 'markers') if k in data}
        return False
    from .core import Marker
    mx = max(0, viewer.vcd.max_time)

    def clamp(t):
        return max(0, min(int(t), mx))

    viewer.markers = [Marker(clamp(m['time']), m.get('label', '')) for m in data.get('markers', [])]
    view = data.get('view')
    if isinstance(view, list) and len(view) == 2 and view[1] > view[0]:
        ts, te = map(clamp, view)
        viewer.time_start, viewer.time_end = (ts, te) if te > ts else (0, max(1, mx))
    cur = data.get('cursors')
    if isinstance(cur, list) and len(cur) == 3:
        viewer.cursor_a, viewer.cursor_b = clamp(cur[0]), clamp(cur[1])
        viewer.cursor_b_on = bool(cur[2])
    viewer._pending_session_times = None
    return True


# ══════════════════════════════════════════════════════════════
#  STAGE 1: SIGNAL TAG SYSTEM
# ══════════════════════════════════════════════════════════════

class SignalTagStore:
    """Maps semantic roles to signal paths for ISA-aware debugging."""

    # Standard tag roles for processor debug
    ROLES = [
        'isa.instr.fetch', 'isa.instr.decode', 'isa.instr.execute',
        'isa.instr.writeback', 'isa.pc.fetch', 'isa.pc.writeback',
        'isa.valid.fetch', 'isa.valid.writeback',
        'isa.rd.addr', 'isa.rd.data', 'isa.rd.wen',
        'isa.rs1.addr', 'isa.rs1.data', 'isa.rs2.addr', 'isa.rs2.data',
        'isa.mem.addr', 'isa.mem.wdata', 'isa.mem.rdata',
        'isa.mem.wen', 'isa.mem.ren',
        'isa.csr.addr', 'isa.csr.wdata', 'isa.csr.rdata', 'isa.csr.wen',
        'isa.pipeline.stall', 'isa.pipeline.flush',
        'isa.hart_id', 'isa.thread_id',
        'isa.pipeline.bubble',
        'clock', 'reset',
    ]

    def __init__(self):
        self.isa = 'rv32i'
        self.hart_id = 0
        self.thread_id = 0
        self.tags = {}             # role -> signal_full_name
        self.reverse = {}          # signal_full_name -> role
        self.hart_tags = {}        # (hart_id, role) -> signal_full_name

    def set_tag(self, role, signal_full_name, hart=0, thread=0):
        key = (hart, thread, role)
        self.tags[role] = signal_full_name
        self.reverse[signal_full_name] = role
        self.hart_tags[key] = signal_full_name

    def get_signal(self, role, hart=0, thread=0):
        key = (hart, thread, role)
        return self.hart_tags.get(key, self.tags.get(role))

    def get_role(self, signal_full_name):
        return self.reverse.get(signal_full_name)

    def save(self, path):
        data = {
            'isa': self.isa,
            'hart_id': self.hart_id,
            'tags': self.tags,
            'hart_tags': {f"{h}:{t}:{r}": v for (h, t, r), v in self.hart_tags.items()},
        }
        _write_json_atomic(path, data)

    @staticmethod
    def load(path):
        with open(path) as f:
            data = json.load(f)
        store = SignalTagStore()
        store.isa = data.get('isa', 'rv32i')
        store.hart_id = data.get('hart_id', 0)
        store.tags = data.get('tags', {})
        store.reverse = {v: k for k, v in store.tags.items()}
        for key_str, val in data.get('hart_tags', {}).items():
            parts = key_str.split(':')
            if len(parts) == 3:
                h, t, r = int(parts[0]), int(parts[1]), parts[2]
                store.hart_tags[(h, t, r)] = val
        return store

    def auto_detect_roles(self, vcd):
        """Heuristic: guess tags from signal names."""
        patterns = {
            'isa.instr.fetch':     re.compile(r'(instr|inst|ir|instruction).*?(if|fetch)', re.I),
            'isa.instr.writeback': re.compile(r'(instr|inst|ir).*?(wb|write_?back|retire)', re.I),
            'isa.instr.decode':    re.compile(r'(instr|inst|ir).*?(id|decode)', re.I),
            'isa.instr.execute':   re.compile(r'(instr|inst|ir).*?(ex|execute)', re.I),
            'isa.pc.fetch':        re.compile(r'\bpc\b.*?(if|fetch)', re.I),
            'isa.pc.writeback':    re.compile(r'\bpc\b.*?(wb|write_?back|retire)', re.I),
            'isa.valid.fetch':     re.compile(r'valid.*?(if|fetch)', re.I),
            'isa.valid.writeback': re.compile(r'valid.*?(wb|write_?back|retire)', re.I),
            'isa.rd.addr':         re.compile(r'(rd|dst|write).*?addr', re.I),
            'isa.rd.data':         re.compile(r'(rd|dst|write).*?data', re.I),
            'isa.rd.wen':          re.compile(r'(rd|reg|dst).*?(wen|write.*en)', re.I),
            'isa.mem.addr':        re.compile(r'mem.*?addr', re.I),
            'isa.mem.wdata':       re.compile(r'mem.*?w.*?data', re.I),
            'isa.mem.rdata':       re.compile(r'mem.*?r.*?data', re.I),
            'isa.pipeline.stall':  re.compile(r'stall', re.I),
            'isa.pipeline.flush':  re.compile(r'flush', re.I),
            'clock':               re.compile(r'^(clk|clock)$', re.I),
            'reset':               re.compile(r'^(rst|reset|rst_n|reset_n)$', re.I),
            'isa.hart_id':         re.compile(r'(hart.*id|active.*hart|hart_sel)', re.I),
            'isa.thread_id':       re.compile(r'(thread.*id|active.*thread|tid|thread_sel)', re.I),
        }
        for sid in vcd.signal_order:
            sig = vcd.signals.get(sid)
            if not sig:
                continue
            for role, pat in patterns.items():
                if role not in self.tags:
                    if pat.search(sig.full_name) or pat.search(sig.name):
                        if role.startswith('isa.instr') and sig.size < 16:
                            continue
                        if role.startswith('isa.pc') and sig.size < 16:
                            continue
                        self.set_tag(role, sig.full_name)
        return len(self.tags)


# ══════════════════════════════════════════════════════════════
#  FEATURE 1: WAVEFORM ANNOTATIONS
# ══════════════════════════════════════════════════════════════

class WaveformAnnotation:
    """A persistent note attached to a time range and optional signal."""
    __slots__ = ('time_start', 'time_end', 'signal', 'text', 'category', 'color')

    def __init__(self, time_start, time_end=None, signal=None, text="",
                 category="note", color=None):
        self.time_start = time_start
        self.time_end = time_end if time_end is not None else time_start
        self.signal = signal
        self.text = text
        self.category = category
        self.color = color

    def to_dict(self):
        return {
            'time_start': self.time_start, 'time_end': self.time_end,
            'signal': self.signal, 'text': self.text,
            'category': self.category, 'color': self.color,
        }

    @staticmethod
    def from_dict(d):
        return WaveformAnnotation(
            d.get('time_start', 0), d.get('time_end'),
            d.get('signal'), d.get('text', ''),
            d.get('category', 'note'), d.get('color'),
        )


class AnnotationStore:
    CATEGORY_MARKERS = {
        'note': ('N', 45), 'bug': ('B', 196), 'todo': ('T', 226),
        'warning': ('W', 208), 'info': ('I', 51),
    }

    def __init__(self):
        self.annotations = []
        self.path = None

    def add(self, ann): self.annotations.append(ann)

    def remove(self, ann):
        if ann in self.annotations:
            self.annotations.remove(ann)

    def at_time(self, t, tolerance=0):
        return [a for a in self.annotations
                if a.time_start - tolerance <= t <= a.time_end + tolerance]

    def on_signal(self, signal_name):
        return [a for a in self.annotations if a.signal == signal_name]

    def in_range(self, t_start, t_end):
        return [a for a in self.annotations
                if not (a.time_end < t_start or a.time_start > t_end)]

    def save(self, path=None):
        path = path or self.path
        if not path: return False
        try:
            data = {'version': 1, 'annotations': [a.to_dict() for a in self.annotations]}
            _write_json_atomic(path, data)
            self.path = path
            return True
        except Exception:
            return False

    def load(self, path):
        try:
            with open(path) as f:
                data = json.load(f)
            self.annotations = [WaveformAnnotation.from_dict(d) for d in data.get('annotations', [])]
            self.path = path
            return len(self.annotations)
        except Exception:
            return 0

    def auto_load(self, vcd_path):
        base = os.path.splitext(vcd_path)[0]
        for c in [base + '.alwavelets_notes.json', base + '.annotations.json',
                  os.path.join(os.path.dirname(vcd_path), '.alwavelets_notes.json'),
                  base + '.wavelet_notes.json']:      # pre-rename sidecar
            if os.path.isfile(c):
                n = self.load(c)
                if n > 0:
                    # Loaded from a shared/legacy file; later saves still go
                    # to this trace's own sidecar, never into the shared one.
                    self.path = self.default_path(vcd_path)
                    return c, n
        return None, 0

    def default_path(self, vcd_path):
        return os.path.splitext(vcd_path)[0] + '.alwavelets_notes.json'


# ══════════════════════════════════════════════════════════════
#  FEATURE 6: BOOKMARKS
# ══════════════════════════════════════════════════════════════

class WaveformBookmark:
    __slots__ = ('name', 'time_start', 'time_end', 'cursor_a', 'cursor_b',
                 'cursor_on', 'active_cursor', 'signals', 'notes', 'timestamp')

    def __init__(self, name, time_start, time_end, cursor_a=0, cursor_b=0,
                 cursor_on=False, active_cursor='a', signals=None, notes=""):
        self.name = name
        self.time_start = time_start
        self.time_end = time_end
        self.cursor_a = cursor_a
        self.cursor_b = cursor_b
        self.cursor_on = cursor_on
        self.active_cursor = active_cursor
        self.signals = signals or []
        self.notes = notes
        import time as _time
        self.timestamp = _time.time()

    def to_dict(self):
        return {
            'name': self.name, 'time_start': self.time_start, 'time_end': self.time_end,
            'cursor_a': self.cursor_a, 'cursor_b': self.cursor_b,
            'cursor_on': self.cursor_on, 'active_cursor': self.active_cursor,
            'signals': self.signals, 'notes': self.notes, 'timestamp': self.timestamp,
        }

    @staticmethod
    def from_dict(d):
        b = WaveformBookmark(
            d.get('name', 'bookmark'), d.get('time_start', 0), d.get('time_end', 0),
            d.get('cursor_a', 0), d.get('cursor_b', 0),
            d.get('cursor_on', False), d.get('active_cursor', 'a'),
            d.get('signals', []), d.get('notes', ''),
        )
        b.timestamp = d.get('timestamp', b.timestamp)
        return b


class BookmarkStore:
    def __init__(self):
        self.bookmarks = {}
        self.path = None

    def add(self, bm): self.bookmarks[bm.name] = bm
    def get(self, name): return self.bookmarks.get(name)
    def remove(self, name):
        if name in self.bookmarks:
            del self.bookmarks[name]
            return True
        return False
    def list_names(self): return sorted(self.bookmarks.keys())

    def save(self, path=None):
        path = path or self.path
        if not path: return False
        try:
            data = {'version': 1, 'bookmarks': [b.to_dict() for b in self.bookmarks.values()]}
            _write_json_atomic(path, data)
            self.path = path
            return True
        except Exception:
            return False

    def load(self, path):
        try:
            with open(path) as f:
                data = json.load(f)
            self.bookmarks = {}
            for d in data.get('bookmarks', []):
                b = WaveformBookmark.from_dict(d)
                self.bookmarks[b.name] = b
            self.path = path
            return len(self.bookmarks)
        except Exception:
            return 0

    def auto_load(self, vcd_path):
        base = os.path.splitext(vcd_path)[0]
        for c in [base + '.alwavelets_bookmarks.json',
                  os.path.join(os.path.dirname(vcd_path), '.alwavelets_bookmarks.json'),
                  base + '.wavelet_bookmarks.json']:  # pre-rename sidecar
            if os.path.isfile(c):
                n = self.load(c)
                if n > 0:
                    self.path = self.default_path(vcd_path)   # see annotations
                    return c, n
        return None, 0

    def default_path(self, vcd_path):
        return os.path.splitext(vcd_path)[0] + '.alwavelets_bookmarks.json'




# ══════════════════════════════════════════════════════════════
#  EXTENSIVE HELP / MANUAL SYSTEM
