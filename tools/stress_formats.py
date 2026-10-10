#!/usr/bin/env python3
"""Compare identical long histories in VCD and converted FST, in fresh processes.

Requires GTKWave's vcd2fst only to generate benchmark inputs. Timings exclude
generation, conversion and full waveform verification. Each worker opens all
signals using the same disk budget; no selection filter is applied.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def create_vcd(path, signals, ticks, mixed):
    from tools.stress_vcd import value_at
    with path.open('w') as output:
        output.write('$timescale 1ns $end\n$scope module top $end\n')
        for i in range(signals):
            kind = i % 4 if mixed else 0
            width = (1, 16, 64, 128)[kind]
            bounds = f' [{width - 1}:0]' if kind in (1, 3) else ''
            output.write(f'$var {"real" if kind == 2 else "wire"} {width} s{i} signal{i}{bounds} $end\n')
        output.write('$var wire 1 s0 alias $end\n$upscope $end\n$enddefinitions $end\n')
        for tick in range(ticks):
            output.write(f'#{tick}\n')
            for i in range(signals):
                kind = i % 4 if mixed else 0
                value = value_at(i, tick, mixed)
                output.write(f'{value}s{i}\n' if kind == 0 else
                             f'{"r" if kind == 2 else "b"}{value} s{i}\n')


def verify(trace, signals, ticks, mixed):
    """Check every event, including aliases, and return a cross-format digest."""
    from tools.stress_vcd import value_at
    assert len(trace.signals) == signals + 1 and trace.max_time == ticks - 1
    digest = hashlib.sha256()

    def record(value):
        digest.update(json.dumps(value, separators=(',', ':')).encode() + b'\n')

    record(trace.timescale)
    for ordinal, sig in enumerate(trace.signals.values()):
        index = 0 if ordinal == signals else ordinal
        assert sig.name == ('alias' if ordinal == signals else f'signal{index}')
        assert sig.num_changes() == ticks, (sig.full_name, sig.num_changes(), ticks)
        kind = index % 4 if mixed else 0
        assert sig.size == (1, 16, 64, 128)[kind]
        assert sig.signal_type == ('real' if kind == 2 else 'wire')
        record([sig.scope, sig.name, sig.size, sig.signal_type, sig.declared_range])
        count = 0
        for tick, (timestamp, value) in enumerate(sig.iter_changes()):
            expected = value_at(index, tick, mixed)
            assert timestamp == tick
            if kind == 2:
                assert float(value) == float(expected)
                value = repr(float(value))
            else:
                assert value == expected
            record([timestamp, value])
            count += 1
        assert count == ticks
    return digest.hexdigest()


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.storage import open_trace
    from tools.stress_workflows import peak_rss
    stages = {}
    before, cpu_before = time.perf_counter(), time.process_time()

    def stage(name, *unused):
        stages.setdefault(name, time.perf_counter() - before)

    with open_trace(args.source, memory_mib=args.memory_mib, background=True, stage_cb=stage) as trace:
        metadata_s = time.perf_counter() - before
        first = trace.signals[trace.signal_order[0]]
        first_s = None
        while trace.is_loading:
            if first_s is None and first.num_changes():
                assert first.value_at(0) == '0'
                first_s = time.perf_counter() - before
            if time.perf_counter() - before > args.timeout:
                raise TimeoutError('Waveform indexing exceeded the time limit')
            trace._store.thread.join(0.005)
        trace.require_loaded()
        elapsed = time.perf_counter() - before
        result = dict(metadata_s=metadata_s, first_waveform_s=first_s or elapsed,
                      load_s=elapsed, cpu_s=time.process_time() - cpu_before, peak_rss_mib=peak_rss(),
                      input_bytes=args.source.stat().st_size, index_bytes=Path(trace._store.path).stat().st_size,
                      stage_started_s=stages)
        result['waveform_sha256'] = verify(trace, args.signals, args.ticks, args.mixed)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--signals', type=int, default=128)
    parser.add_argument('--ticks', type=int, default=10000)
    parser.add_argument('--mixed', action='store_true', help='Include scalar, 16/128-bit bus and real signals')
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--codec', choices=('4', 'F', 'Z'), default='4', help='vcd2fst value compression')
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path, help='Also benchmark an older checkout with the same source files')
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if min(args.signals, args.ticks, args.memory_mib, args.timeout) <= 0:
        parser.error('counts, memory and timeout must be positive')
    if args.source:
        print(json.dumps(worker(args)))
        return
    converter = shutil.which('vcd2fst')
    if converter is None:
        parser.error('vcd2fst is required to generate matching benchmark inputs')
    sys.path.insert(0, str(ROOT))
    result = dict(signals=args.signals, ticks=args.ticks, mixed=args.mixed, codec=args.codec,
                  memory_mib=args.memory_mib, repositories={})
    signature = None
    with tempfile.TemporaryDirectory(prefix='alwavelets-formats-') as directory:
        vcd, fst = (Path(directory) / ('trace.' + kind) for kind in ('vcd', 'fst'))
        create_vcd(vcd, args.signals, args.ticks, args.mixed)
        subprocess.run([converter, '-' + args.codec, str(vcd), str(fst)], check=True, capture_output=True, timeout=args.timeout)
        repositories = [('baseline', args.baseline)] if args.baseline else []
        repositories.append(('current', args.repo))
        for label, repo in repositories:
            entry = result['repositories'][label] = dict(repo=str(repo), formats={})
            for source in (vcd, fst):
                kind = source.suffix[1:]
                samples = entry['formats'][kind] = {}
                env = dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / f'cache-{label}-{kind}'))
                for phase in ('cold', 'cached'):
                    command = [sys.executable, str(Path(__file__).resolve()), '--source', str(source),
                               '--repo', str(repo), '--signals', str(args.signals), '--ticks', str(args.ticks),
                               '--memory-mib', str(args.memory_mib), '--timeout', str(args.timeout)]
                    if args.mixed:
                        command.append('--mixed')
                    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=args.timeout * 2)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr or completed.stdout)
                    sample = samples[phase] = json.loads(completed.stdout)
                    if signature is None:
                        signature = sample['waveform_sha256']
                    assert sample['waveform_sha256'] == signature, 'VCD/FST waveform mismatch'
                    print(json.dumps(dict(repository=label, format=kind, phase=phase, **sample)), flush=True)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
