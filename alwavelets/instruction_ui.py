"""Instruction query workers return bounded UI updates, never touch curses."""
import heapq

from .results import close_results
from .streaming import check_cancel


class QueryOutcome:
    def __init__(self):
        self.matches = None
        self.message = ''
        self.cursor = None
        self.markers = []
        self.replace_markers = False
        self.heatmap = None

    def close(self):
        close_results(self.matches)


def run_query(trace, query, start, end, cursor, width, *, cancel=None, progress=None):
    from .decoders import RV32Decoder, CSR_NAMES
    out = QueryOutcome()
    text = query.strip()
    parts = text.split()
    verb = parts[0].lower() if parts else ''
    filters, predicate = {}, None
    hart = None
    try:
        check_cancel(cancel)
        if progress:
            progress(0, 0)
        if verb in ('mn', 'mnemonic'):
            filters['mnemonic_pattern'] = text.split(None, 1)[1]
        elif verb == 'mn=':
            filters['mnemonic'] = text.split(None, 1)[1]
        elif verb == 'pc':
            filters = {'pc_lo': int(parts[1], 0), 'pc_hi': int(parts[2], 0) if len(parts) > 2 else int(parts[1], 0)}
        elif verb == 'time':
            filters = {'time_lo': int(parts[1], 0), 'time_hi': int(parts[2], 0)}
        elif verb in ('thread', 'hart'):
            hart = int(parts[1], 0)
            filters = {'hart_id': hart}
            if len(parts) > 2:
                filters['time_lo'] = int(parts[2], 0)
            if len(parts) > 3:
                filters['time_hi'] = int(parts[3], 0)
        elif verb == 'threadid':
            filters = {'thread_id': int(parts[1], 0)}
        elif verb == 'store':
            filters = {'is_store': True, 'mem_addr_lo': int(parts[1], 0) if len(parts) > 1 else 0,
                       'mem_addr_hi': int(parts[2], 0) if len(parts) > 2 else 0xFFFFFFFF}
        elif verb == 'reg':
            reg = int(parts[1]) if parts[1].isdigit() else RV32Decoder.REG_NAMES.index(parts[1])
            filters = {'rd_addr': reg, 'pc_lo': 0, 'pc_hi': 0xFFFFFFFF}
        elif verb == 'csr':
            address = next((addr for addr, name in CSR_NAMES.items() if name == parts[1]), None)
            filters = {'is_csr': True, 'csr_addr': int(parts[1], 0) if address is None else address}
        elif verb in ('stall', 'stalled'):
            filters = {'stall': True}
        elif verb == 'lambda':
            predicate = trace.compile_query(text)
        elif verb == 'summary':
            out.message = trace.summary(cancel=cancel, progress=progress)
            return out
        elif verb == 'ipc':
            ipcs = trace.all_hart_ipc(start, end, cancel=cancel, progress=progress)
            out.message = 'IPC: ' + ' '.join(f'H{h}:{v:.3f}' for h, v in sorted(ipcs.items())[:8])
            return out
        elif verb == 'fairness':
            stats = trace.fairness_analysis(cancel=cancel, progress=progress)
            top, count = [], 0
            for e in trace.iter_starvation_events(cancel=cancel, progress=progress, stats=stats):
                count += 1
                heapq.heappush(top, (-e['time'], -count, e))
                if len(top) > 50:
                    heapq.heappop(top)
            if count:
                out.markers = [(e['time'], f"H{e['hart_id']}:starved×{e['ratio']:.1f}")
                               for _, _, e in sorted(top, reverse=True)]
                out.message = f'Fairness: {count} starvation events ({len(top)} markers)'
            else:
                out.message = ' '.join(f"H{h}:{'⚠' if s['starved'] else '✓'}avg={s['avg_gap']:.0f} max={s['max_gap']}"
                                       for h, s in sorted(stats.items())[:6])
            return out
        elif verb == 'diverge':
            a, b = int(parts[1], 0), int(parts[2], 0)
            result = trace.thread_divergence(a, b, int(parts[3], 0) if len(parts) > 3 else 0,
                                             cancel=cancel, progress=progress)
            out.cursor = None if result['identical'] else result['diverge_time']
            out.message = f"H{a} vs H{b}: {result['reason']}"
            return out
        elif verb == 'contention':
            result = trace.contention_overview(int(parts[1], 0) if len(parts) > 1 else 0,
                                               int(parts[2], 0) if len(parts) > 2 else 0xFFFFFFFF,
                                               cancel=cancel, progress=progress)
            out.message = 'No contention found'
            if result['items']:
                first = result['items'][0]
                hazards = '+'.join(k.upper() for k in ('waw', 'raw', 'war') if first['has_' + k]) or 'shared'
                out.message = f"{result['count']} contended addresses; @0x{first['addr']:x}: {first['n_harts']} harts ({hazards}), {first['access_count']} accesses"
                out.cursor = first['accesses'][0]['time']
            return out
        elif verb == 'heatmap':
            out.heatmap = trace.scheduling_heatmap(start, end, width, cancel=cancel, progress=progress)
            out.message = (f"Heatmap: {len(out.heatmap['harts'])} harts, {len(out.heatmap['heatmap'])} buckets"
                           if out.heatmap else 'No instructions')
            return out
        elif verb == 'regs':
            hart = int(parts[1], 0)
            regs = trace.register_snapshot(hart, cursor, cancel=cancel)
            out.message = ' '.join('%s=%s' % (RV32Decoder.REG_NAMES[i], f'{regs[i]:08x}' if regs[i] is not None else '--------') for i in range(4))
            return out
        elif verb == 'harts':
            out.message = f'Active harts: {trace.get_active_harts(cancel=cancel)}'
            return out
        else:
            raise ValueError('Commands: mn <regex>, mn= <name>, pc <lo> [hi], time <lo> <hi>, hart <id>, threadid <id>, store, reg, csr, stall, summary, ipc, fairness, diverge, contention, heatmap, regs, harts, lambda e: ...')

        out.matches = trace.select(predicate, cancel=cancel, progress=progress, **filters)
        n = len(out.matches)
        if not n:
            out.message = 'No matches'
            return out
        first = out.matches[0]
        out.cursor = first['time']
        if n == 1:
            out.message = f"t={first['time']} {first['decoded']['asm']}"
        else:
            out.replace_markers = True
            out.markers = [(e['time'], e['decoded']['asm'][:20]) for e in out.matches[:100]]
            out.message = f'{n} matches ({len(out.markers)} markers). Use ;results to browse all.'
        if hart is not None:
            ipc = trace.hart_ipc(hart, filters.get('time_lo', 0), filters.get('time_hi'), cancel=cancel, progress=progress)
            out.message = f'Hart {hart}: {n} instrs, IPC={ipc:.3f} ({len(out.markers)} markers)'
        check_cancel(cancel)
        return out
    except BaseException:
        out.close()
        raise
