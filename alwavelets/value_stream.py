"""Shared VCD value/directive state for full, chunked and live readers."""

_LOGIC = {48: '0', 49: '1', 120: 'x', 88: 'x', 122: 'z', 90: 'z'}

class ValueState:
    """Incremental line decoder. A None identifier denotes a dump blackout.

    Keeping comment and dump state separate from storage lets a worker or a
    live tail resume at a byte boundary without changing the trace semantics.
    """
    __slots__ = ('time', 'dump', 'comment', 'max_time', 'monotonic')

    def __init__(self, time=0, dump=True, comment=False, max_time=0):
        self.time = time
        self.dump = dump
        self.comment = comment
        self.max_time = max_time
        self.monotonic = True

    def snapshot(self):
        return (self.time, self.dump, self.comment, self.max_time)

    def feed(self, raw):
        if self.comment:
            if b'$end' in raw:
                self.comment = False
            return None
        if raw and raw[0] <= 32:
            raw = raw.lstrip()
        if not raw:
            return None
        c = raw[0]
        if c == 35:
            try:
                time = int(raw[1:])
                if time < self.time:
                    self.monotonic = False
                self.time = time
                if time > self.max_time:
                    self.max_time = time
            except ValueError:
                pass
        elif c == 36:
            if raw.startswith(b'$comment'):
                self.comment = b'$end' not in raw
            elif raw.startswith(b'$dumpoff'):
                self.dump = False
                return None, 'x'
            elif raw.startswith(b'$dumpon'):
                self.dump = True
        elif self.dump:
            if c in _LOGIC:
                return raw[1:].strip(), _LOGIC[c]
            if c in (98, 66, 114, 82):
                parts = raw[1:].split(None, 1)
                if len(parts) == 2:
                    return parts[1].strip(), parts[0].decode('ascii', 'ignore')
                # Retain the historical empty-vector fallback.
                if raw[1:2] in (b' ', b'\t'):
                    return raw[1:].strip(), ''
        return None


def states_at_offsets(filename, start, offsets):
    """Capture directive/time state before each requested line boundary.

    Value payloads need not be decoded during this inexpensive preflight.
    """
    state = ValueState()
    result = {}
    with open(filename, 'rb') as f:
        f.seek(start)
        pos = start
        for offset in sorted(offsets):
            while pos < offset:
                raw = f.readline()
                if not raw:
                    break
                pos += len(raw)
                if state.comment or raw.lstrip()[:1] in (b'$', b'#'):
                    state.feed(raw)
            result[offset] = state.snapshot()
    return result
