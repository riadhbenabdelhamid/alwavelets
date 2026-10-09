#!/usr/bin/env python3
"""Index and query ten million synthetic retirements in a fresh process."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
BASE = 1 << 63


def entries(count):
    for i in range(count):
        mnemonic = ('add', 'lw', 'sw', 'nop')[(i // 8) % 4]
        yield {'time': timestamp(i), 'pc': BASE + ((i // 8) % 4096) * 4,
               'decoded': {'mnemonic': mnemonic, 'asm': mnemonic + ' x1,x2',
                           'is_store': mnemonic == 'sw', 'is_csr': False},
               'hart_id': i % 8, 'thread_id': i % 16, 'rd_addr': i % 31 + 1,
               'rd_data': i, 'mem_addr': i % 1024 * 8 if mnemonic in ('lw', 'sw') else None,
               'mem_wdata': i, 'csr_addr': None, 'stall': i % 17 == 0}


def timestamp(i):
    return i // 2 + (i // 10000) * 7


def worker(count, cache_roundtrip=False):
    from alwavelets.instruction_store import InstructionResults
    from alwavelets.decoders import InstructionTrace
    from alwavelets.parsers import VCDFile
    from tools.stress_workflows import peak_rss
    result = {'instructions': count, 'python': sys.version}
    def phase(name):
        print(name, file=sys.stderr, flush=True)
        return time.perf_counter()
    with VCDFile() as owner:
        before = phase('Building instruction fields and indexes…')
        trace = InstructionTrace()
        trace.entries = InstructionResults.collect(entries(count), dict, owner=owner)
        result['build_s'] = time.perf_counter() - before
        result['index_bytes'] = Path(trace.entries.path).stat().st_size
        cases = [('all', {'time_lo': 0}, count),
                 ('pc', {'pc_lo': BASE + 20, 'pc_hi': BASE + 20}, count // 32768 * 8 + max(0, min(8, count % 32768 - 40))),
                 ('hart', {'hart_id': 3}, (count + 4) // 8),
                 ('thread', {'thread_id': 7}, (count + 8) // 16),
                 ('mnemonic', {'mnemonic': 'nop'}, count // 32 * 8 + max(0, count % 32 - 24))]
        result['queries'] = {}
        for name, filters, expected in cases:
            before = phase('Querying ' + name + '…')
            with trace.select(**filters) as matches:
                assert len(matches) == expected, (name, len(matches), expected)
                if name == 'all':
                    assert matches[-1]['rd_data'] == count - 1
                    assert matches[count // 2]['rd_data'] == count // 2
                result['queries'][name] = {'s': time.perf_counter() - before, 'matches': len(matches)}
        before = phase('Computing per-hart statistics…')
        fairness = trace.fairness_analysis()
        assert sum(s['count'] for s in fairness.values()) == count
        for h, stats in fairness.items():
            last = (count - 1 - h) // 8 * 8 + h
            assert stats['count'] == (count + 7 - h) // 8
            expected = (timestamp(last) - timestamp(h)) / (stats['count'] - 1) if stats['count'] > 1 else 0
            assert stats['avg_gap'] == expected
        result['fairness_s'] = time.perf_counter() - before
        before = phase('Reconstructing registers near the middle…')
        state = trace.register_panel_state(timestamp(count // 2))
        assert state[1][0] == 0 and state[3]['time'] <= timestamp(count // 2)
        result['register_panel_s'] = time.perf_counter() - before
        if cache_roundtrip:
            from alwavelets.analysis_cache import InstructionCache, identity_for, stamp_source
            from alwavelets.decoders import RV32Decoder
            from alwavelets.persistence import SignalTagStore
            with tempfile.TemporaryDirectory(prefix='wavelet-instruction-cache-stress-') as directory:
                source = Path(directory) / 'synthetic.source'
                source.write_text('Synthetic instruction records: %d\n' % count)
                owner._filename = str(source)
                stamp_source(owner, source)
                identity = identity_for(owner, SignalTagStore(), RV32Decoder())
                manager = InstructionCache(Path(directory) / 'cache', quota=16 * 1024 ** 3)
                before = phase('Persisting completed instruction index…')
                assert manager.publish(trace.entries, identity)
                result['cache_write_s'] = time.perf_counter() - before
                before = phase('Reopening instruction index…')
                with manager.load(identity) as reopened:
                    assert len(reopened) == count and reopened.cache_hit
                    result['cache_reopen_s'] = time.perf_counter() - before
                    with reopened.select() as matches:
                        before = phase('Reading final result page…')
                        page = matches.read_page(count - 32, 32)
                        assert len(page) == 32 and page[-1]['rd_data'] == count - 1
                        result['last_page_s'] = time.perf_counter() - before
                assert manager.purge()[0] == 1
        result['peak_rss_mib'] = peak_rss()
        path = trace.entries.path
    assert not Path(path).exists()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records', type=int, default=10000000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--cache-roundtrip', action='store_true', help='Also validate persistent reuse and final-page access')
    args = parser.parse_args()
    if args.records < 32:
        parser.error('--records must be at least 32')
    if args.worker:
        print(json.dumps(worker(args.records, args.cache_roundtrip)))
        return
    command = [sys.executable, str(Path(__file__).resolve()), '--worker', '--records', str(args.records)]
    if args.cache_roundtrip:
        command.append('--cache-roundtrip')
    run = subprocess.run(command,
                         check=True, stdout=subprocess.PIPE, text=True)
    result = json.loads(run.stdout)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
