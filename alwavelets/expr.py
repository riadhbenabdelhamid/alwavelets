"""Cross-signal expression search.

Evaluate a boolean expression over several signals at a point in time and find
the next time it holds — e.g.

    state == 3 && valid && !stall
    addr >= 0x40 && addr < 0x80
    req && !ack

Grammar (precedence low -> high): ||  &&  (== != < <= > >=)  unary(! not -)
primary. Primaries are numeric literals (0x.., 0b.., 12, 3.14), quoted string
literals ('x', "idle"), or signal references (name or hierarchical path like
top.alu.state). A tiny hand-written tokenizer + recursive-descent parser — no
eval(), so an expression can never execute arbitrary code.

Evaluation only happens at times where a referenced signal changes, so a search
touches O(transitions in window) points, not every tick.
"""
from .core import time_bisect_right
import heapq
import itertools
import fnmatch
import operator
import re

from .core import VCDSignal, ensure_trace_loaded, numeric_value

_TOKEN = re.compile(r"""\s*(?:
      (?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+\.\d+|\d+)
    | (?P<str>'[^']*'|"[^"]*")
    | (?P<op>==|!=|<=|>=|<<|>>|&&|\|\||[<>!()+*/%&|^~\-])
    | (?P<ident>[A-Za-z_][\w.$/]*(?:\[[^\]\s]+\][\w.$/]*)*)
    )""", re.VERBOSE)

_KEYWORD_OPS = {'and': '&&', 'or': '||', 'not': '!'}
_CMP = {'==', '!=', '<', '<=', '>', '>='}
_ARITH = {
    'add': operator.add, 'sub': operator.sub, 'mul': operator.mul,
    'div': operator.truediv, 'mod': operator.mod,
    'shl': lambda a, b: int(a) << int(b), 'shr': lambda a, b: int(a) >> int(b),
    'band': lambda a, b: int(a) & int(b), 'bor': lambda a, b: int(a) | int(b),
    'bxor': lambda a, b: int(a) ^ int(b),
}


class ExprError(ValueError):
    pass


# ── tokenizer ────────────────────────────────────────────────────────

def _tokenize(s):
    toks, pos, n = [], 0, len(s)
    while pos < n:
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            if s[pos:].strip() == "":
                break
            raise ExprError(f"unexpected character at: {s[pos:][:12]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)                    # the token, not group(0) (which has \s*)
        if kind == 'num':
            toks.append(('num', _parse_number(val)))
        elif kind == 'str':
            toks.append(('str', val[1:-1]))
        elif kind == 'op':
            toks.append(('op', val))
        else:
            low = val.lower()
            if low in _KEYWORD_OPS:
                toks.append(('op', _KEYWORD_OPS[low]))
            else:
                toks.append(('sig', val))
    toks.append(('end', None))
    return toks


def _parse_number(t):
    tl = t.lower()
    if tl.startswith('0x'):
        return int(tl, 16)
    if tl.startswith('0b'):
        return int(tl, 2)
    if '.' in tl:
        return float(tl)
    return int(tl)


# ── recursive-descent parser ─────────────────────────────────────────

class _Parser:
    def __init__(self, toks):
        self.toks = toks
        self.i = 0
        self.sigs = set()

    def _peek(self):
        return self.toks[self.i]

    def _eat(self, val=None):
        tok = self.toks[self.i]
        if val is not None and not (tok[0] == 'op' and tok[1] == val):
            raise ExprError(f"expected {val!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self._or()
        if self._peek()[0] != 'end':
            raise ExprError(f"trailing tokens near {self._peek()[1]!r}")
        return node

    def _or(self):
        node = self._and()
        while self._peek() == ('op', '||'):
            self.i += 1
            node = ('or', node, self._and())
        return node

    def _and(self):
        node = self._cmp()
        while self._peek() == ('op', '&&'):
            self.i += 1
            node = ('and', node, self._cmp())
        return node

    def _cmp(self):
        node = self._bor()
        t = self._peek()
        if t[0] == 'op' and t[1] in _CMP:
            self.i += 1
            return ('cmp', t[1], node, self._bor())
        return node

    def _binary(self, ops, lower):
        node = lower()
        while True:
            t = self._peek()
            if t[0] == 'op' and t[1] in ops:
                self.i += 1
                node = (ops[t[1]], node, lower())
            else:
                return node

    def _bor(self):
        return self._binary({'|': 'bor'}, self._bxor)

    def _bxor(self):
        return self._binary({'^': 'bxor'}, self._band)

    def _band(self):
        return self._binary({'&': 'band'}, self._shift)

    def _shift(self):
        return self._binary({'<<': 'shl', '>>': 'shr'}, self._add)

    def _add(self):
        return self._binary({'+': 'add', '-': 'sub'}, self._mul)

    def _mul(self):
        return self._binary({'*': 'mul', '/': 'div', '%': 'mod'}, self._unary)

    def _unary(self):
        t = self._peek()
        if t == ('op', '!'):
            self.i += 1
            return ('not', self._unary())
        if t == ('op', '-'):
            self.i += 1
            return ('neg', self._unary())
        if t == ('op', '~'):
            self.i += 1
            return ('bnot', self._unary())
        return self._primary()

    def _primary(self):
        t = self._peek()
        if t[0] == 'op' and t[1] == '(':
            self.i += 1
            node = self._or()
            self._eat(')')
            return node
        if t[0] in ('num', 'str'):
            self.i += 1
            return t
        if t[0] == 'sig':
            self.i += 1
            self.sigs.add(t[1])
            return t
        raise ExprError(f"unexpected token {t[1]!r}")


def parse(expr):
    """Return (ast, set_of_signal_names). Raises ExprError on a bad expression."""
    p = _Parser(_tokenize(expr))
    ast = p.parse()
    return ast, p.sigs


# ── evaluation ───────────────────────────────────────────────────────

def _as_number(v):
    """Coerce a value (number or VCD value string) to a number, or None for x/z."""
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, bool):
        return int(v)
    s = str(v)
    if not s:
        return None
    if all(c in '01' for c in s):
        return int(s, 2)
    sl = s.lower()
    try:
        if sl.startswith('0x'):
            return int(sl, 16)
        if sl.startswith('0b'):
            return int(sl, 2)
        return float(s)
    except ValueError:
        return None


def _truth(v):
    if isinstance(v, bool):
        return v
    n = _as_number(v)
    return n is not None and n != 0


def _compare(op, a, b):
    na, nb = _as_number(a), _as_number(b)
    if op in ('==', '!='):
        if na is not None and nb is not None:
            eq = (na == nb)
        else:                                  # x/z or symbolic -> string compare
            eq = (str(a).lower() == str(b).lower())
        return eq if op == '==' else not eq
    if na is None or nb is None:               # ordering on unknown -> false
        return False
    return {'<': na < nb, '<=': na <= nb, '>': na > nb, '>=': na >= nb}[op]


def _value(node, resolved, t):
    k = node[0]
    if k == 'num' or k == 'str':
        return node[1]
    if k == 'sig':
        sig = resolved[node[1]]
        value = sig.value_at(t)
        if sig.signal_type in ('real', 'realtime'):
            number = numeric_value(value, sig.signal_type)
            return value if number is None else number
        return value
    if k == 'cmp':
        return _compare(node[1], _value(node[2], resolved, t), _value(node[3], resolved, t))
    if k == 'and':
        return _truth(_value(node[1], resolved, t)) and _truth(_value(node[2], resolved, t))
    if k == 'or':
        return _truth(_value(node[1], resolved, t)) or _truth(_value(node[2], resolved, t))
    if k == 'not':
        return not _truth(_value(node[1], resolved, t))
    if k == 'neg':
        n = _as_number(_value(node[1], resolved, t))
        return None if n is None else -n
    if k == 'bnot':
        n = _as_number(_value(node[1], resolved, t))
        return None if n is None else ~int(n)
    if k in _ARITH:
        a = _as_number(_value(node[1], resolved, t))
        b = _as_number(_value(node[2], resolved, t))
        if a is None or b is None:           # arithmetic on x/z propagates unknown
            return None
        try:
            return _ARITH[k](a, b)
        except (ZeroDivisionError, ValueError):
            return None
    raise ExprError("bad node")


def evaluate(ast, resolved, t):
    """True iff the parsed expression holds at time t (resolved: name->VCDSignal)."""
    return _truth(_value(ast, resolved, t))


# ── signal resolution + search ───────────────────────────────────────

def resolve_signal(vcd, name):
    sigs = vcd.signals
    for s in sigs.values():
        if s.full_name == name:
            return s
    for s in sigs.values():
        if s.name == name:
            return s
    for s in sigs.values():
        if fnmatch.fnmatch(s.full_name, name) or fnmatch.fnmatch(s.name, name):
            return s
    return None


def _prepare(vcd, expr):
    ast, names = parse(expr)
    resolved = {}
    missing = []
    for nm in names:
        sig = resolve_signal(vcd, nm)
        if sig is None:
            missing.append(nm)
        else:
            resolved[nm] = sig
    if missing:
        raise ExprError("unknown signal(s): " + ", ".join(sorted(missing)))
    return ast, resolved


def _candidate_times(resolved, start, end):
    """Yield `start`, then every time in (start, end] where any referenced
    signal changes, ascending and deduplicated. A lazy k-way merge of the
    per-signal slices: a first-match search stops at the hit instead of
    first building a set of every change time in the trace."""
    if end < start:
        return
    yield start
    slices = []
    for s in resolved.values():
        ts = s._times
        lo = time_bisect_right(ts, start)
        hi = time_bisect_right(ts, end)
        if hi > lo:
            slices.append(_time_slice(ts, lo, hi))
    prev = start
    for t in heapq.merge(*slices):
        if t != prev:
            yield t
            prev = t


def _time_slice(times, lo, hi):
    if hasattr(times, 'iter_range'):
        yield from times.iter_range(lo, hi)
    else:
        for i in range(lo, hi):
            yield times[i]


class _CurrentValue:
    def __init__(self, sig, time):
        self.signal_type = sig.signal_type
        self.value = sig.value_at(time)

    def value_at(self, time):
        return self.value


def _stream_values(resolved, start, end, cancel=None, progress=None):
    """Evaluate only referenced signals, keeping one current value per input.

    Same-timestamp updates are applied together, preserving value_at's last
    delta-cycle semantics. Cancellation is checked even within long runs of
    repeated timestamps.
    """
    if end < start:
        return
    def check(t):
        if cancel is not None and cancel.is_set():
            raise ExprError('Analysis cancelled')
        if progress is not None:
            progress(t, end)
    check(start)
    state = {name: _CurrentValue(sig, start) for name, sig in resolved.items()}
    yield start, state
    def events(name, sig):
        for ordinal, (t, value) in enumerate(sig.iter_changes(start, end)):
            if ordinal % 4096 == 0:
                check(t)
            if t > start:
                yield t, name, ordinal, value
    merged = heapq.merge(*(events(name, sig) for name, sig in resolved.items()))
    count = 0
    for t, group in itertools.groupby(merged, key=lambda event: event[0]):
        for _, name, _, value in group:
            state[name].value = value
            count += 1
            if count % 4096 == 0:
                check(t)
        yield t, state
    check(end)


def _load_interval(vcd, start, end):
    try:
        ensure_trace_loaded(vcd, start, end)
    except ValueError as exc:
        raise ExprError(str(exc)) from exc


def find_expression(vcd, expr, start_time=0, end_time=None, *, cancel=None, progress=None):
    """Return the first time >= start_time where `expr` holds, or None."""
    ast, resolved = _prepare(vcd, expr)
    _load_interval(vcd, start_time, end_time)
    if end_time is None:
        end_time = vcd.max_time
    if start_time > end_time:
        return None
    for t, state in _stream_values(resolved, start_time, end_time, cancel, progress):
        if evaluate(ast, state, t):
            return t
    return None


def find_all_expression(vcd, expr, start_time=0, end_time=None, limit=1000, *, cancel=None, progress=None):
    """Return up to `limit` times where `expr` *becomes* true (rising matches)."""
    ast, resolved = _prepare(vcd, expr)
    _load_interval(vcd, start_time, end_time)
    if end_time is None:
        end_time = vcd.max_time
    if limit <= 0:
        return []
    hits, prev = [], False
    for t, state in _stream_values(resolved, start_time, end_time, cancel, progress):
        cur = evaluate(ast, state, t)
        if cur and not prev:
            hits.append(t)
            if len(hits) >= limit:
                break
        prev = cur
    return hits


def make_derived_signal(vcd, name, expr_str, size=None, scope="derived"):
    """Build a virtual VCDSignal whose value is `expr_str` evaluated over time.

    The expression may use arithmetic/bitwise/logical/comparison operators over
    existing signals, e.g.

        (a3 << 3) | (a2 << 2) | (a1 << 1) | a0   # reassemble a bus from bits
        addr + offset                            # a derived sum
        state == 3 && valid                      # a 1-bit predicate signal

    The result is sampled at every time a referenced signal changes; x/z inputs
    propagate to an x result. `size` (bit width) is inferred from the values if
    not given. Returns a VCDSignal not attached to `vcd`."""
    ast, resolved = _prepare(vcd, expr_str)
    _load_interval(vcd, 0, None)
    def results():
        for t, state in _stream_values(resolved, 0, vcd.max_time):
            value = _value(ast, state, t)
            number = _as_number(value)
            yield t, None if number is None else int(number)
    if size is None:
        # Two streaming passes trade some compute for bounded intermediate
        # memory; no list of every expression sample is retained.
        mx = max((abs(n) for _, n in results() if n is not None), default=0)
        size = max(1, mx.bit_length())
    mask = (1 << size) - 1
    sig = VCDSignal(name, "__derived__%s" % name, size, "wire", scope)
    writer = None
    if getattr(vcd, '_store', None) is not None:
        from .storage import create_derived_store
        sig, writer = create_derived_store(vcd, sig)
    prev = None
    try:
        for t, n in results():
            val = ("x" * size) if n is None else format(n & mask, "0%db" % size)
            if val != prev:
                if writer is None:
                    sig.add_change(t, val)
                else:
                    writer.add(sig.identifier, t, val)
                prev = val
        if writer is not None:
            writer.publish(True)
    except Exception:
        if writer is not None:
            writer.store.close()
        raise
    return sig
