#!/usr/bin/env python3
"""Reproducible parser/view benchmarks; stdlib only, one fresh process per sample.

Run from any directory. Generation, imports and fingerprints are outside the
timed region. OS page caches are intentionally left alone; "cold seek" means
the requested window has not been loaded by Alwavelets yet.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import time

SCENARIOS = ('scalar', 'bus', 'real', 'mixed', 'sparse')
METRICS = ('load_s', 'first_view_s', 'cold_seek_s', 'pan_s', 'parent_peak_rss_mib')
ROOT = Path(__file__).resolve().parents[1]


def identifier(i):
    out = ''
    while True:
        out += chr(33 + i % 94)
        i //= 94
        if not i:
            return out


def generate_trace(path, scenario, size_bytes, signals=32, seed=141):
    """Write deterministic complete timestamp groups up to the requested size."""
    rng = random.Random(seed)
    types = [('wire', 1), ('wire', 32), ('real', 64)]
    if scenario == 'mixed':
        declarations = [types[i % 3] for i in range(signals)]
    else:
        declarations = [types[{'scalar': 0, 'bus': 1, 'real': 2, 'sparse': 0}[scenario]]] * signals
    ids = [identifier(i) for i in range(signals)]
    header = ['$timescale 1ns $end', '$scope module bench $end']
    for i, ((kind, width), sid) in enumerate(zip(declarations, ids)):
        header.append('$var %s %d %s s%d $end' % (kind, width, sid, i))
    header += ['$upscope $end', '$enddefinitions $end']
    changes, tick = 0, 0
    with open(path, 'wb', buffering=1 << 20) as f:
        f.write(('\n'.join(header) + '\n').encode('ascii'))
        while f.tell() < size_bytes or tick < 2:
            lines = ['#%d' % tick]
            for i, ((kind, width), sid) in enumerate(zip(declarations, ids)):
                if scenario == 'sparse' and tick and i and tick % (4096 * i):
                    continue
                if kind == 'real':
                    value = 'r%.6f %s' % (rng.uniform(-100, 100), sid)
                elif width == 1:
                    value = '%d%s' % ((tick + i) % 2, sid)
                else:
                    value = 'b%s %s' % (format(rng.getrandbits(width), '032b'), sid)
                lines.append(value)
                changes += 1
            f.write(('\n'.join(lines) + '\n').encode('ascii'))
            tick += 1
    return {'name': scenario, 'changes': changes, 'signals': signals,
            'max_time': tick - 1}


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def run_sample(path, mode, jobs, memory_mib=32):
    from alwavelets.parsers import VCDFile
    from alwavelets.snapshot import render_text
    from alwavelets.parallel import parse_parallel_file

    started = time.perf_counter()
    if mode == 'disk':
        from alwavelets.storage import open_trace
        vcd = open_trace(path, use_cache=False, memory_mib=memory_mib)
    elif mode == 'lazy':
        vcd, offset = VCDFile.parse_header(path)
        vcd._build_time_index(offset)
        if vcd._load_error:
            raise RuntimeError(vcd._load_error)
    elif mode == 'parallel':
        vcd = parse_parallel_file(path, jobs)
        if vcd is None:
            raise RuntimeError('parallel parsing unavailable; refusing to time a silent fallback')
    else:
        vcd = VCDFile.parse(path)      # bypass persistent parse cache
    if not vcd.signal_order:
        raise ValueError('benchmark input must be a VCD with signal declarations')
    loaded = time.perf_counter()
    names = [vcd.signals[sid].full_name for sid in vcd.signal_order[:16]]
    span = max(1, vcd.max_time // 1000)

    def view(start):
        return render_text(vcd, names, start, min(vcd.max_time, start + span), width=100)

    snapshots = [view(0)]
    first_view = time.perf_counter()
    before = time.perf_counter()
    snapshots.append(view(vcd.max_time * 9 // 10))
    cold_seek = time.perf_counter() - before
    pans = []
    for fraction in (0.4, 0.1, 0.7):
        before = time.perf_counter()
        snapshots.append(view(int(vcd.max_time * fraction)))
        pans.append(time.perf_counter() - before)
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss /= (1024 * 1024 if sys.platform == 'darwin' else 1024)
    except ImportError:
        rss = None
    result = {'load_s': loaded - started, 'first_view_s': first_view - started,
            'cold_seek_s': cold_seek, 'pan_s': statistics.median(pans),
            'parent_peak_rss_mib': rss,
            'max_time': vcd.max_time, 'signals': len(vcd.signal_order),
            'stored_changes': sum(s.num_changes() for s in vcd.signals.values() if s.owns_storage),
            'view_digest': hashlib.sha256('\n'.join(snapshots).encode()).hexdigest()}
    if mode == 'disk':
        result['peak_page_cache_mib'] = vcd._store.peak_cache_bytes / 1024 / 1024
        result['index_mib'] = os.path.getsize(vcd._store.path) / 1024 / 1024
        result['page_reads'] = vcd._store.page_reads
        vcd.close()
    return result


def code_version(repo):
    def git(*args):
        try:
            p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True)
        except OSError:
            return None
        return p.stdout.strip() if p.returncode == 0 else None
    return {'commit': git('rev-parse', 'HEAD'), 'dirty': bool(git('status', '--porcelain'))}


def summarize(samples):
    return {key: statistics.median(s[key] for s in samples) if samples[0][key] is not None else None
            for key in METRICS}


def print_report(report, baseline=None):
    print('scenario  mode       load(s)  first(s)   seek(ms)    pan(ms)  main RSS(MiB)')
    prior = {(r['scenario'], r['mode']): r for r in (baseline or {}).get('results', [])}
    old_inputs = {t['name']: t['sha256'] for t in (baseline or {}).get('traces', [])}
    inputs = {t['name']: t['sha256'] for t in report['traces']}
    for row in report['results']:
        m = row['median']
        rss = '-' if m['parent_peak_rss_mib'] is None else '%.1f' % m['parent_peak_rss_mib']
        print('%-9s %-8s %9.3f %9.3f %10.2f %10.2f %14s' % (
            row['scenario'], row['mode'], m['load_s'], m['first_view_s'],
            m['cold_seek_s'] * 1000, m['pan_s'] * 1000, rss))
        old = prior.get((row['scenario'], row['mode']))
        if old and old_inputs.get(row['scenario']) == inputs[row['scenario']]:
            if old['samples'][0]['view_digest'] != row['samples'][0]['view_digest']:
                raise ValueError('rendered values differ from baseline: ' + row['scenario'])
            delta = [(key, (m[key] / old['median'][key] - 1) * 100) for key in METRICS
                     if m[key] is not None and old['median'].get(key)]
            print('  baseline delta: ' + ', '.join('%s %+.1f%%' % item for item in delta))
        elif old:
            print('  baseline comparison skipped: trace fingerprints differ')


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=ROOT, help='checkout to import (baseline comparisons)')
    parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS, default=list(SCENARIOS))
    parser.add_argument('--modes', nargs='+', choices=('full', 'lazy', 'parallel', 'disk'), default=['full', 'lazy'])
    parser.add_argument('--size-mib', type=positive_int, default=8, help='approximate generated size per scenario')
    parser.add_argument('--memory-mib', type=positive_int, default=32, help='disk page-cache budget')
    parser.add_argument('--signals', type=positive_int, default=32)
    parser.add_argument('--seed', type=int, default=141)
    parser.add_argument('--repeat', type=positive_int, default=3)
    parser.add_argument('--jobs', type=positive_int, default=4, help='workers for explicit parallel mode')
    parser.add_argument('--input', type=Path, help='benchmark an existing VCD instead of generated scenarios')
    parser.add_argument('--output', type=Path, help='write metadata, raw samples and medians as JSON')
    parser.add_argument('--baseline', type=Path, help='compare medians and rendered values with an earlier JSON report')
    parser.add_argument('--timeout', type=positive_int, default=600, help='timeout per sample in seconds')
    parser.add_argument('--worker', nargs=2, metavar=('PATH', 'MODE'), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if 'parallel' in args.modes and args.jobs < 2:
        parser.error('parallel mode needs --jobs >= 2')
    args.repo = args.repo.resolve()
    if not (args.repo / 'alwavelets' / 'parsers.py').is_file():
        parser.error('--repo must contain the alwavelets package')
    if args.worker:
        sys.path.insert(0, str(args.repo))
        print(json.dumps(run_sample(*args.worker, args.jobs, args.memory_mib)))
        return
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    report = {'schema_version': 1, 'environment': {
        'python': sys.version, 'platform': platform.platform(), 'cpu_count': os.cpu_count(),
        'repo': str(args.repo), **code_version(args.repo)},
        'settings': {'seed': args.seed, 'size_mib': args.size_mib, 'signals': args.signals,
                     'repeat': args.repeat, 'jobs': args.jobs, 'memory_mib': args.memory_mib, 'pythonhashseed': '0'},
        'traces': [], 'results': []}
    with tempfile.TemporaryDirectory(prefix='alwavelets-bench-') as td:
        traces = []
        if args.input:
            path = args.input.resolve()
            traces.append((path, {'name': path.name}))
        else:
            for scenario in args.scenarios:
                path = Path(td) / (scenario + '.vcd')
                meta = generate_trace(path, scenario, args.size_mib * 1024 * 1024,
                                      args.signals, args.seed)
                traces.append((path, meta))
        for path, meta in traces:
            meta.update(bytes=path.stat().st_size, sha256=file_hash(path))
            report['traces'].append(meta)
            expected_digest = None
            for mode in args.modes:
                samples = []
                for i in range(args.repeat):
                    print('%s / %s / %d of %d' % (meta['name'], mode, i + 1, args.repeat),
                          file=sys.stderr, flush=True)
                    command = [sys.executable, str(Path(__file__).resolve()), '--repo', str(args.repo),
                               '--jobs', str(args.jobs), '--memory-mib', str(args.memory_mib), '--worker', str(path), mode]
                    env = dict(os.environ, PYTHONHASHSEED='0')
                    result = subprocess.run(command, capture_output=True, text=True, timeout=args.timeout, env=env)
                    if result.returncode:
                        raise RuntimeError('benchmark worker failed:\n' + result.stderr)
                    if result.stderr:
                        print(result.stderr, file=sys.stderr, end='')
                    sample = json.loads(result.stdout)
                    if expected_digest is None:
                        expected_digest = sample['view_digest']
                    if sample['view_digest'] != expected_digest:
                        raise ValueError('rendered values differ between samples/modes: ' + meta['name'])
                    if mode != 'lazy' and 'changes' in meta and sample['stored_changes'] != meta['changes']:
                        raise ValueError('parsed change count differs from generated trace: ' + meta['name'])
                    samples.append(sample)
                report['results'].append({'scenario': meta['name'], 'mode': mode,
                                          'samples': samples, 'median': summarize(samples)})
    print_report(report, baseline)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
