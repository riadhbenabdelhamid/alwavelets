#!/usr/bin/env python3
"""Compare cold VCD indexing, first readable values and RSS in fresh processes.

Wide traces have one scalar value per declaration. Mixed traces repeatedly
interleave scalar, bus and real events through nested scopes, with aliases.
Generation, cached reopening and rendering are excluded from cold timings.
"""
import argparse
import cProfile
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = str(Path(__file__).resolve().parents[1])


def value_at(signal, tick, mixed):
    kind = signal % 4 if mixed else 0
    if kind == 0:
        return '01xz'[tick % 4]
    if kind == 2:
        return str(tick / 4 - signal)
    width = 16 if kind == 1 else 128
    if tick % 17 == 0:
        return ('x' if tick % 34 == 0 else 'z') * width
    return format((tick + signal) % (1 << width), '0%db' % width)


def create_vcd(path, signals, events, depth, mixed):
    with path.open('w') as output:
        for level in range(depth):
            output.write('$scope module level%d $end\n' % level)
        for i in range(signals):
            kind = i % 4 if mixed else 0
            width = (1, 16, 64, 128)[kind]
            output.write('$var %s %d s%d signal%d $end\n' % (
                'real' if kind == 2 else 'wire', width, i, i))
        output.write('$var wire 1 s0 alias $end\n')
        output.write('$upscope $end\n' * depth + '$enddefinitions $end\n')
        for start in range(0, events, signals):
            tick = start // signals
            output.write('#%d\n' % tick)
            for i in range(min(signals, events - start)):
                value = value_at(i, tick, mixed)
                kind = i % 4 if mixed else 0
                output.write(('%ss%d\n' % (value, i)) if kind == 0 else
                             ('%s%s s%d\n' % ('r' if kind == 2 else 'b', value, i)))


def worker(path, signals, events, mixed, memory_mib, repo, profile=None):
    sys.path.insert(0, repo)
    from alwavelets.storage import open_trace
    from tools.stress_workflows import peak_rss
    profiler = cProfile.Profile() if profile else None
    # Profile synchronous ingestion; ordinary runs measure progressive loading.
    if profiler:
        profiler.enable()
    cpu_before = time.process_time()
    before = time.perf_counter()
    trace = open_trace(path, memory_mib=memory_mib, background=not profiler, use_cache=False)
    metadata_s = time.perf_counter() - before
    metadata_cpu_s = time.process_time() - cpu_before
    first_s = None
    try:
        first = trace.signals['s0']
        while trace.is_loading:
            if first_s is None and first.num_changes():
                assert first.value_at(0) == '0'
                first_s = time.perf_counter() - before
            trace._store.thread.join(0.01)
        trace.require_loaded()
        elapsed = time.perf_counter() - before
        cpu_s = time.process_time() - cpu_before
        if profiler:
            profiler.disable()
            profiler.dump_stats(profile)
        assert len(trace.signals) == signals + 1
        assert trace.load_changes == events
        assert trace._store.total_changes == events + (events + signals - 1) // signals
        for ordinal in sorted({0, 1, 2, 3, signals // 2, signals - 1}):
            if ordinal >= signals:
                continue
            signal = trace.signals['s%d' % ordinal]
            count = max(0, (events - ordinal + signals - 1) // signals)
            assert signal.num_changes() == count
            for tick in sorted({0, count // 2, count - 1}) if count else ():
                assert signal.value_at(tick) == value_at(ordinal, tick, mixed), (ordinal, tick)
        index = Path(trace._store.path)
        result = {'signals': signals, 'events': events, 'input_bytes': Path(path).stat().st_size,
                  'metadata_s': metadata_s, 'first_waveform_s': first_s or elapsed,
                  'metadata_cpu_s': metadata_cpu_s, 'cpu_s': cpu_s,
                  'load_s': elapsed, 'peak_rss_mib': peak_rss(), 'index_bytes': index.stat().st_size}
    finally:
        trace.close()
    assert not index.exists()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', type=int, default=1000000)
    parser.add_argument('--events', type=int, default=10000000)
    parser.add_argument('--active-signals', type=int, default=128)
    parser.add_argument('--depth', type=int, default=8)
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--cases', nargs='+', choices=['wide', 'mixed'], default=['wide', 'mixed'])
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--profile', help='Write synchronous worker cProfile data')
    parser.add_argument('--worker', nargs=4, metavar=('PATH', 'SIGNALS', 'EVENTS', 'MIXED'))
    args = parser.parse_args()
    if min(args.signals, args.events, args.active_signals, args.depth, args.memory_mib) <= 0:
        parser.error('counts, depth and memory must be positive')
    if args.worker:
        path, signals, events, mixed = args.worker
        print(json.dumps(worker(path, int(signals), int(events), bool(int(mixed)),
                                args.memory_mib, args.repo, args.profile)))
        return
    results = {}
    with tempfile.TemporaryDirectory(prefix='alwavelets-vcd-stress-') as directory:
        env = dict(os.environ, XDG_CACHE_HOME=os.path.join(directory, 'cache'))
        for case in args.cases:
            mixed = case == 'mixed'
            signals = args.active_signals if mixed else args.signals
            events = args.events if mixed else signals
            path = Path(directory) / (case + '.vcd')
            create_vcd(path, signals, events, args.depth if mixed else 1, mixed)
            print('Checking %s VCD (%d signals, %d events)…' % (case, signals, events),
                  file=sys.stderr, flush=True)
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--repo', args.repo,
                                  '--memory-mib', str(args.memory_mib), '--worker', str(path),
                                  str(signals), str(events), str(int(mixed))] +
                                 (['--profile', args.profile + '.' + case] if args.profile else []),
                                 env=env, check=True, stdout=subprocess.PIPE, text=True)
            results[case] = json.loads(run.stdout)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
