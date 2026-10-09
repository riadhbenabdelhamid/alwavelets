"""Streaming instruction queries and aggregates, shared by eager and paged traces."""
from collections import Counter
import heapq
import itertools
import re

from .instruction_store import InstructionResults, key_number
from .results import PagedResults
from .streaming import check_cancel


class InstructionAnalysis:
    def iter_matching(self, predicate=None, *, cancel=None, progress=None, **filters):
        """Yield matching entries in capture order; never retain the result set."""
        check_cancel(cancel)
        if isinstance(self.entries, InstructionResults):
            entries = self.entries.iter_instructions(cancel=cancel, progress=progress, **filters)
        else:
            pattern = re.compile(filters['mnemonic_pattern'], re.I) if 'mnemonic_pattern' in filters else None
            def matches(entry):
                for name, expected in filters.items():
                    if name == 'mnemonic_pattern':
                        if not pattern.search(entry['decoded']['mnemonic']):
                            return False
                        continue
                    op = '='
                    if name.endswith('_lo') or name.endswith('_hi'):
                        op, name = name[-2:], name[:-3]
                        if expected is None:
                            continue
                    value = (entry.get('decoded', {}).get(name) if name in ('mnemonic', 'is_store', 'is_csr')
                             else entry.get(name, 0 if name in ('hart_id', 'thread_id') else None))
                    if op == '=' and (value.lower() != expected.lower() if name == 'mnemonic' else value != expected):
                        return False
                    if op != '=' and (value is None or (value < expected if op == 'lo' else value > expected)):
                        return False
                return True
            def source():
                for i, e in enumerate(self.entries):
                    if i % 4096 == 0:
                        check_cancel(cancel)
                        if progress:
                            progress(i, len(self.entries))
                    if matches(e):
                        yield e
            entries = source()
        try:
            for i, entry in enumerate(entries):
                if i % 4096 == 0:
                    check_cancel(cancel)
                    if progress:
                        progress(i, len(self.entries))
                if predicate is None or predicate(entry):
                    yield entry
            check_cancel(cancel)
        finally:
            close = getattr(entries, 'close', None)
            if close:
                close()

    def select(self, predicate=None, *, cancel=None, progress=None, **filters):
        """Return a closeable paged selection, including for arbitrary predicates."""
        if isinstance(self.entries, InstructionResults):
            return self.entries.select(predicate, cancel=cancel, progress=progress, **filters)
        return PagedResults.collect(self.iter_matching(predicate, cancel=cancel, progress=progress, **filters),
                                    dict, cancel=cancel)

    def query(self, filter_fn):
        return list(self.iter_matching(filter_fn))

    # User-authored lambdas have the same authority as Tcl scripts. This is a
    # convenience namespace, not a security sandbox or an SQL expression parser.
    _QUERY_NAMESPACE = {'__builtins__': {}, 'len': len, 'int': int, 'str': str, 'abs': abs,
                        'min': min, 'max': max, 'any': any, 'all': all, 'sum': sum, 'range': range,
                        'sorted': sorted, 'hex': hex, 'bin': bin, 'oct': oct, 'ord': ord, 'bool': bool,
                        'round': round, 'float': float}

    def compile_query(self, expression):
        predicate = eval(expression, dict(self._QUERY_NAMESPACE), {})
        if not callable(predicate):
            raise ValueError('Expression must be callable (use lambda e: ...)')
        return predicate

    def query_str(self, expression):
        try:
            return self.query(self.compile_query(expression)), None
        except Exception as exc:
            return [], str(exc)

    # Existing list-returning APIs stay explicit compatibility conveniences.
    def find_mnemonic(self, pattern):
        return list(self.iter_matching(mnemonic_pattern=pattern))

    def find_pc_range(self, pc_lo, pc_hi):
        return list(self.iter_matching(pc_lo=pc_lo, pc_hi=pc_hi))

    def find_reg_writes(self, reg_num, pc_lo=0, pc_hi=0xFFFFFFFF):
        return list(self.iter_matching(rd_addr=reg_num, pc_lo=pc_lo, pc_hi=pc_hi))

    def find_stores(self, addr_lo=0, addr_hi=0xFFFFFFFF):
        return list(self.iter_matching(is_store=True, mem_addr_lo=addr_lo, mem_addr_hi=addr_hi))

    def find_csr_changes(self, csr_addr):
        return list(self.iter_matching(is_csr=True, csr_addr=csr_addr))

    def find_stalled(self):
        return list(self.iter_matching(stall=True))

    def extract_thread_trace(self, hart_id):
        return list(self.iter_matching(hart_id=hart_id))

    def extract_thread_trace_range(self, hart_id, t_start=0, t_end=None):
        return list(self.iter_matching(hart_id=hart_id, time_lo=t_start, time_hi=t_end))

    def _times_and_harts(self, cancel=None, progress=None, **filters):
        if isinstance(self.entries, InstructionResults):
            for t, hart in self.entries.rows('start,hart', filters=filters, cancel=cancel, progress=progress):
                yield t, key_number(hart)
        else:
            for e in self.iter_matching(cancel=cancel, progress=progress, **filters):
                yield e['time'], e.get('hart_id', 0)

    def get_active_harts(self, *, cancel=None):
        if isinstance(self.entries, InstructionResults):
            with self.entries.sql_operation(cancel) as db:
                return [key_number(r[0]) for r in db.execute('SELECT DISTINCT hart FROM results ORDER BY hart')]
        return sorted({h for _, h in self._times_and_harts(cancel)})

    def get_hart_count(self):
        return len(self.get_active_harts())

    def _hart_stats(self, cancel=None, progress=None, **filters):
        stats = {}
        for t, h in self._times_and_harts(cancel, progress, **filters):
            state = stats.get(h)
            if state is None:
                stats[h] = [1, t, t, None, 0]
            else:
                gap = t - state[2]
                if state[3] is None or gap > state[3]:
                    state[3], state[4] = gap, state[2]
                state[0] += 1
                state[2] = t
        return stats

    def hart_ipc(self, hart_id, t_start=0, t_end=None, *, cancel=None, progress=None):
        # Preserve the historical count/time-span metric (time is in trace ticks).
        state = self._hart_stats(cancel, progress, hart_id=hart_id, time_lo=t_start, time_hi=t_end).get(hart_id)
        return self._ipc(state)

    @staticmethod
    def _ipc(state):
        return state[0] / (state[2] - state[1]) if state and state[0] >= 2 and state[2] > state[1] else 0.0

    def all_hart_ipc(self, t_start=0, t_end=None, *, cancel=None, progress=None):
        stats = self._hart_stats(cancel, progress, time_lo=t_start, time_hi=t_end)
        return {h: self._ipc(stats.get(h)) for h in self.get_active_harts(cancel=cancel)}

    def fairness_analysis(self, *, cancel=None, progress=None):
        results = {}
        for h, (n, first, last, gap, when) in self._hart_stats(cancel, progress).items():
            avg = (last - first) / (n - 1) if n > 1 else 0
            results[h] = {'count': n, 'avg_gap': avg, 'max_gap': gap if gap is not None else 0,
                          'max_gap_time': when, 'starved': avg > 0 and gap is not None and gap > 3 * avg}
        return results

    def iter_starvation_events(self, threshold_multiplier=3.0, *, cancel=None, progress=None, stats=None):
        """Stream gap completions; callers needing start-time order sort on disk."""
        stats = self.fairness_analysis(cancel=cancel, progress=progress) if stats is None else stats
        last = {}
        for t, h in self._times_and_harts(cancel, progress):
            previous = last.get(h)
            last[h] = t
            state = stats[h]
            avg = state['avg_gap']
            if previous is not None and state['count'] >= 3 and t - previous > threshold_multiplier * avg:
                yield {'hart_id': h, 'time': previous, 'gap': t - previous,
                       'avg_gap': avg, 'ratio': (t - previous) / avg if avg > 0 else 0}

    def find_starvation_events(self, threshold_multiplier=3.0):
        return sorted(self.iter_starvation_events(threshold_multiplier), key=lambda e: e['time'])

    def thread_divergence(self, hart_a, hart_b, after_time=0, *, cancel=None, progress=None):
        a = self.iter_matching(hart_id=hart_a, time_lo=after_time, cancel=cancel, progress=progress)
        b = self.iter_matching(hart_id=hart_b, time_lo=after_time, cancel=cancel)
        try:
            compared = 0
            pairs = zip(a, b)
            for i, (ea, eb) in enumerate(pairs):
                compared += 1
                da, db = ea['decoded'], eb['decoded']
                if da['mnemonic'] != db['mnemonic'] or ea['pc'] != eb['pc']:
                    reason = (f"Mnemonic: {da['asm']} vs {db['asm']}" if da['mnemonic'] != db['mnemonic']
                              else f"PC: 0x{ea['pc']:x} vs 0x{eb['pc']:x}")
                    details = []
                    for j, (x, y) in enumerate(itertools.chain([(ea, eb)], itertools.islice(pairs, 4)), i):
                        details.append({'index': j, 'a_time': x['time'], 'a_pc': x['pc'], 'a_asm': x['decoded']['asm'],
                                        'b_time': y['time'], 'b_pc': y['pc'], 'b_asm': y['decoded']['asm'],
                                        'match': x['pc'] == y['pc'] and x['decoded']['mnemonic'] == y['decoded']['mnemonic']})
                    return {'identical': False, 'diverge_time': ea['time'], 'diverge_index': i,
                            'hart_a_entry': ea, 'hart_b_entry': eb, 'reason': reason, 'details': details}
            if not compared:
                return {'identical': False, 'reason': 'Empty trace(s)', 'diverge_time': after_time, 'details': []}
            return {'identical': True, 'compared': compared, 'reason': f'Identical for {compared} instructions', 'details': []}
        finally:
            a.close()
            b.close()

    def scheduling_heatmap(self, t_start, t_end, n_buckets=80, *, cancel=None, progress=None):
        if not self.entries:
            return []
        if n_buckets < 1:
            raise ValueError('n_buckets must be positive')
        size = max(1, (t_end - t_start) // n_buckets)
        harts = self.get_active_harts(cancel=cancel)
        indices = {h: i for i, h in enumerate(harts)}
        heatmap = [[0] * len(harts) for _ in range(n_buckets)]
        for t, h in self._times_and_harts(cancel, progress, time_lo=t_start, time_hi=t_end):
            heatmap[min(n_buckets - 1, (t - t_start) // size)][indices[h]] += 1
        return {'heatmap': heatmap, 'harts': harts, 'bucket_size': size, 't_start': t_start, 't_end': t_end}

    def register_snapshot(self, hart_id, time, n_regs=32, *, cancel=None):
        regs = [None] * n_regs
        if not regs:
            return regs
        regs[0] = 0
        if isinstance(self.entries, InstructionResults):
            for reg in range(1, n_regs):
                e = self.entries.latest(cancel, hart_id=hart_id, rd_addr=reg, time_hi=time)
                if e is not None:
                    regs[reg] = e.get('rd_data')
        else:
            for e in self.iter_matching(hart_id=hart_id, time_hi=time, cancel=cancel):
                if e.get('rd_addr') is not None and 0 < e['rd_addr'] < n_regs:
                    regs[e['rd_addr']] = e['rd_data']
        return regs

    def register_panel_state(self, time, *, cancel=None):
        if isinstance(self.entries, InstructionResults):
            current = self.entries.latest(cancel, time_hi=time)
        else:
            current = None
            for e in self.iter_matching(time_hi=time, cancel=cancel):
                current = e
        hart = current.get('hart_id', 0) if current else 0
        regs = self.register_snapshot(hart, time, cancel=cancel)
        recent = {e.get('rd_addr') for e in self.iter_matching(hart_id=hart, time_lo=time - 99, time_hi=time, cancel=cancel)}
        return hart, regs, recent, current

    def summary(self, *, cancel=None, progress=None):
        if not self.entries:
            return 'No trace built'
        if isinstance(self.entries, InstructionResults):
            with self.entries.sql_operation(cancel) as db:
                top = list(db.execute('SELECT mnemonic,COUNT(*) n FROM results GROUP BY mnemonic '
                                      'ORDER BY n DESC,MIN(seq) LIMIT 5'))
        else:
            counts = Counter(e['decoded']['mnemonic'] for e in self.iter_matching(cancel=cancel, progress=progress))
            top = counts.most_common(5)
        return (f"{len(self.entries)} instructions [{self.entries[0]['time']}..{self.entries[-1]['time']}] "
                f"Top: {', '.join(f'{m}:{c}' for m, c in top)}")

    def contention_overview(self, lo=0, hi=0xFFFFFFFF, *, cancel=None, progress=None):
        """Bounded UI summary; the compatibility API still returns access lists."""
        if not isinstance(self.entries, InstructionResults):
            items = self.shared_resource_contention(lo, hi)
            return {'count': len(items), 'items': [dict(c, accesses=c['accesses'][:1], access_count=len(c['accesses'])) for c in items[:3]]}
        rows = self.entries.rows('seq,data', filters={'mem_addr_lo': lo, 'mem_addr_hi': hi}, order='mem,start,seq',
                                 cancel=cancel, progress=progress)
        top, count = [], 0
        entries = ((row[0], self.entries._restore(row[1])) for row in rows)
        try:
            for addr, group in itertools.groupby(entries, key=lambda pair: pair[1]['mem_addr']):
                harts, writers, readers = set(), {}, {}
                first = previous = None
                waw = raw = war = False
                n = 0
                first_seq = None
                for seq, e in group:
                    h, t, write = e.get('hart_id', 0), e['time'], e['decoded']['is_store']
                    first = e if first is None else first
                    first_seq = seq if first_seq is None else min(first_seq, seq)
                    n += 1
                    harts.add(h)
                    if write:
                        waw |= previous is not None and previous[1] and previous[0] != h
                        war |= any(rh != h and rt < t for rh, rt in readers.items())
                        writers[h] = t
                    else:
                        raw |= any(wh != h and wt < t for wh, wt in writers.items())
                        readers[h] = t
                    previous = h, write
                if len(harts) > 1:
                    count += 1
                    item = {'addr': addr, 'n_harts': len(harts), 'harts': sorted(harts), 'access_count': n,
                            'accesses': [{'time': first['time']}], 'has_waw': waw, 'has_raw': raw, 'has_war': war}
                    # Larger hart counts win; capture order breaks ties.
                    heapq.heappush(top, (len(harts), -first_seq, -count, item))
                    if len(top) > 3:
                        heapq.heappop(top)
        finally:
            rows.close()
        return {'count': count, 'items': [t[-1] for t in sorted(top, reverse=True)]}
