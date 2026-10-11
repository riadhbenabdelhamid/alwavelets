#!/usr/bin/env python3
"""Compare long bus/real FST chains, first values and decompression spill writes.

Requires vcd2fst only for generation. Workers use fresh processes and temporary
indexes, load every signal, and verify every event outside the timed region.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def value_at(tick, kind, width):
    if kind == 'real':
        return str(tick / 4 - 2)
    if tick % 17 == 0:
        return ('x' if tick % 34 == 0 else 'z') * width
    return format(tick % (1 << width), f'0{width}b')


def create_vcd(path, events, kind, width):
    with path.open('w') as output:
        output.write('$timescale 1ns $end\n$scope module top $end\n')
        for name in ('value', 'alias'):
            output.write(f'$var {"real" if kind == "real" else "wire"} {width} ! {name} $end\n')
        output.write('$upscope $end\n$enddefinitions $end\n')
        for tick in range(events):
            output.write(f'#{tick}\n{"r" if kind == "real" else "b"}{value_at(tick, kind, width)} !\n')


def worker(args):
    sys.path.insert(0, str(args.repo))
    from alwavelets.fst import FSTReader
    from alwavelets.fst_stream import Buffer
    from alwavelets.storage import open_trace
    from tools.stress_workflows import peak_rss
    # Count bytes passed to decompression/table spill-file writes, including
    # the resident prefix copied when a buffer first spills (not physical I/O).
    stats = dict(spill_write_bytes=0, max_chain_bytes=0, chains=0)
    write, decode = Buffer.write, FSTReader._decode_chain

    def counted_write(self, data):
        prefix = self.size if self.file is None else 0
        write(self, data)
        if self.file is not None:
            stats['spill_write_bytes'] += prefix + len(data)

    def counted_decode(self, handle, chain, times, begin):
        stats['chains'] += 1
        stats['max_chain_bytes'] = max(stats['max_chain_bytes'], len(chain))
        return decode(self, handle, chain, times, begin)

    Buffer.write, FSTReader._decode_chain = counted_write, counted_decode
    started, cpu = time.perf_counter(), time.process_time()
    with open_trace(args.source, memory_mib=args.memory_mib, use_cache=False, background=True) as trace:
        metadata_s, first_s = time.perf_counter() - started, None
        signal = trace.signals[trace.signal_order[0]]
        while trace.is_loading:
            if first_s is None and signal.num_changes():
                assert signal.value_at(0) == value_at(0, args.kind, args.width)
                first_s = time.perf_counter() - started
            if time.perf_counter() - started > args.timeout:
                raise TimeoutError('FST indexing exceeded the time limit')
            trace._store.thread.join(0.005)
        trace.require_loaded()
        elapsed = time.perf_counter() - started
        result = dict(metadata_s=metadata_s, first_waveform_s=first_s or elapsed,
                      load_s=elapsed, cpu_s=time.process_time() - cpu, peak_rss_mib=peak_rss(),
                      input_bytes=args.source.stat().st_size, index_bytes=Path(trace._store.path).stat().st_size,
                      **stats)
        assert len(trace.signals) == 2 and trace.max_time == args.events - 1
        digest = hashlib.sha256()
        for sig in trace.signals.values():
            assert sig.num_changes() == args.events and sig.size == args.width
            count = 0
            for tick, (timestamp, value) in enumerate(sig.iter_changes()):
                assert timestamp == tick and value == value_at(tick, args.kind, args.width)
                digest.update(struct.pack('<Q', timestamp) + value.encode() + b'\0')
                count += 1
            assert count == args.events
        result['waveform_sha256'] = digest.hexdigest()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=1000000)
    parser.add_argument('--kind', choices=('bus', 'real'), default='bus')
    parser.add_argument('--width', type=int, default=256)
    parser.add_argument('--codec', choices=('4', 'F', 'Z'), default='4')
    parser.add_argument('--memory-mib', type=float, default=8)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--repo', type=Path, default=ROOT)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.kind == 'real':
        args.width = 64
    if min(args.events, args.memory_mib, args.timeout) <= 0 or args.width < 2:
        parser.error('counts, memory and timeout must be positive; bus width must be at least two')
    if args.source:
        print(json.dumps(worker(args)))
        return
    converter = shutil.which('vcd2fst')
    if converter is None:
        parser.error('vcd2fst is required to generate benchmark inputs')
    result = dict(events=args.events, kind=args.kind, width=args.width, codec=args.codec,
                  memory_mib=args.memory_mib, repositories={})
    signature = None
    with tempfile.TemporaryDirectory(prefix='alwavelets-chains-') as directory:
        vcd, fst = (Path(directory) / ('trace.' + kind) for kind in ('vcd', 'fst'))
        create_vcd(vcd, args.events, args.kind, args.width)
        subprocess.run([converter, '-' + args.codec, str(vcd), str(fst)], check=True, capture_output=True, timeout=args.timeout)
        repositories = [('baseline', args.baseline)] if args.baseline else []
        for label, repo in repositories + [('current', args.repo)]:
            command = [sys.executable, str(Path(__file__).resolve()), '--source', str(fst),
                       '--repo', str(repo), '--events', str(args.events), '--kind', args.kind,
                       '--width', str(args.width), '--memory-mib', str(args.memory_mib), '--timeout', str(args.timeout)]
            env = dict(os.environ, ALWAVELETS_CACHE_DIR=str(Path(directory) / label))
            completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=args.timeout * 2)
            if completed.returncode:
                raise RuntimeError(completed.stderr or completed.stdout)
            sample = json.loads(completed.stdout)
            result['repositories'][label] = dict(repo=str(repo), **sample)
            if signature is None:
                signature = sample['waveform_sha256']
            assert sample['waveform_sha256'] == signature, 'Waveform mismatch'
            print(json.dumps(dict(repository=label, **sample)), flush=True)
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
