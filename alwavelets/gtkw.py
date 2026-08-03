"""GTKWave save-file (.gtkw / .sav) interop.

Read and write the de-facto-standard GTKWave save format so saved views move
between Alwavelets and GTKWave. We round-trip the parts that matter for a view:
the dumpfile path, the ordered signal list, and each signal's radix (via the
GTKWave per-trace flag bytes). Other GTKWave keys are passed over on read and
omitted on write — GTKWave tolerates a minimal file.
"""
from .core import Radix

# GTKWave TraceFlagsType radix bits (analyzer.h). We read/write the common ones.
_TR_HEX = 0x00000002
_TR_DEC = 0x00000008
_TR_BIN = 0x00000010
_TR_SIGNED = 0x00000800

_RADIX_TO_FLAG = {
    Radix.HEX: _TR_HEX,
    Radix.BIN: _TR_BIN,
    Radix.DEC: _TR_DEC | _TR_SIGNED,
    Radix.UNSIGNED: _TR_DEC,
}


def _flag_to_radix(flags):
    if flags & _TR_BIN:
        return Radix.BIN
    if flags & _TR_HEX:
        return Radix.HEX
    if flags & _TR_DEC:
        return Radix.DEC if (flags & _TR_SIGNED) else Radix.UNSIGNED
    return None


# ── read ─────────────────────────────────────────────────────────────

def load_gtkw(path):
    """Parse a .gtkw file. Returns a dict:
        {'dumpfile': str|None, 'signals': [(name, radix|None), ...],
         'timestart': int|None}
    Signal names keep their hierarchical path; a trailing bus range like
    '[7:0]' is left intact for the caller to resolve.
    """
    out = {'dumpfile': None, 'signals': [], 'timestart': None}
    cur_flags = 0
    with open(path, 'r', errors='replace') as f:
        for line in f:
            s = line.rstrip('\n')
            t = s.strip()
            if not t or t.startswith('[*]') or t.startswith('*'):
                continue
            if t.startswith('@'):                       # per-trace flag word (hex)
                try:
                    cur_flags = int(t[1:], 16)
                except ValueError:
                    cur_flags = 0
                continue
            if t.startswith('[dumpfile]'):
                out['dumpfile'] = _bracket_value(t)
                continue
            if t.startswith('[timestart]'):
                v = _bracket_value(t)
                try:
                    out['timestart'] = int(v)
                except (TypeError, ValueError):
                    pass
                continue
            if t.startswith('['):                       # any other [key] — ignore
                continue
            if t.startswith('#') or t.startswith('('):   # group/comment forms
                continue
            out['signals'].append((t, _flag_to_radix(cur_flags)))
    return out


def _bracket_value(line):
    """Extract the value after a [key], unquoting a "..." if present."""
    rest = line.split(']', 1)[1].strip() if ']' in line else ''
    if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
        return rest[1:-1]
    return rest or None


# ── write ────────────────────────────────────────────────────────────

def save_gtkw(path, dumpfile, signals, time_start=None, time_end=None):
    """Write a minimal GTKWave save file.

    `signals` is an iterable of (full_name, radix, size) — `size > 1` signals
    get a [n-1:0] range suffix like GTKWave; flags carry the radix.
    """
    lines = ["[*] Alwavelets save file", '[dumpfile] "%s"' % (dumpfile or "")]
    if time_start is not None and time_end is not None:
        lines.append("[timestart] %d" % time_start)
    last_flag = None
    for name, radix, size in signals:
        flag = _RADIX_TO_FLAG.get(radix, _TR_HEX)
        if flag != last_flag:
            lines.append("@%x" % flag)
            last_flag = flag
        if size and size > 1:
            lines.append("%s[%d:0]" % (name, size - 1))
        else:
            lines.append(name)
    with open(path, 'w') as f:
        f.write("\n".join(lines) + "\n")
    return path


def base_name(sig_name):
    """Strip a trailing GTKWave bus range '[hi:lo]'/'[i]' for resolution."""
    if sig_name.endswith(']') and '[' in sig_name:
        return sig_name[:sig_name.rindex('[')]
    return sig_name
