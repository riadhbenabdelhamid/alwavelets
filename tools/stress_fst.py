#!/usr/bin/env python3
"""Generate deep/wide compressed FSTs and check paged loading in fresh processes."""
import argparse
import gzip
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

ROOT = str(Path(__file__).resolve().parents[1])
U64 = struct.Struct('>Q')


def varint(value):
    data = bytearray()
    while value >= 128:
        data.append((value & 127) | 128)
        value >>= 7
    data.append(value)
    return bytes(data)


def repeated(value, count):
    while count:
        n = min(count, 65536 // len(value))
        yield value * n
        count -= n


def compressed(chunks, output):
    decoder = zlib.compressobj(1)
    total = 0
    for chunk in chunks:
        total += len(chunk)
        output.write(decoder.compress(chunk))
    output.write(decoder.flush())
    size = output.tell()
    output.seek(0)
    return total, size


def create_fst(path, events=10000000, signals=1, width=1, *, scope_per_signal=False):
    """One scalar chain or a broad initial frame. No external converter needed."""
    if signals > 1 or width > 1:
        events = 1
    def hierarchy():
        yield b'\xfe\x00top\0\0'
        for i in range(signals):
            if scope_per_signal:
                yield b'\xfe\x00u%d\0\0' % i
            yield b'\x10\x00signal%d\0' % i + varint(width) + b'\0'
            if scope_per_signal:
                yield b'\xff'
        yield b'\xff'
    with open(path, 'wb') as output, tempfile.TemporaryFile() as chain, tempfile.TemporaryFile() as times, tempfile.TemporaryFile() as hier, tempfile.TemporaryFile() as frame:
        def chain_data():
            yield b'\x00'
            for _ in range((events - 1) // 65536):
                yield b'\x06\x04' * 32768
            rest = (events - 1) % 65536
            yield (b'\x06\x04' * ((rest + 1) // 2))[:rest]
        chain_u, chain_c = compressed(chain_data(), chain) if events > 1 else (0, 0)
        def time_data():
            yield b'\0'
            yield from repeated(b'\x01', events - 1)
        time_u, time_c = compressed(time_data(), times)
        hier_u, hier_c = compressed(hierarchy(), hier)
        frame_u, frame_c = compressed(repeated(b'0', signals * width), frame)
        payload = (U64.pack(0) + U64.pack(events - 1) + struct.pack('<d', 2.7182818284590452354) +
                   U64.pack(134217728) + U64.pack(1 + signals if scope_per_signal else 1) + U64.pack(signals) + U64.pack(signals) + U64.pack(1) +
                   struct.pack('b', -9) + b'Alwavelets stress'.ljust(128, b'\0') + bytes(119) + b'\0' + U64.pack(0))
        output.write(b'\0' + U64.pack(len(payload) + 8) + payload)
        vc_start = output.tell()
        output.write(b'\x08' + U64.pack(0))
        output.write(U64.pack(0) + U64.pack(events - 1) + U64.pack(chain_u))
        output.write(varint(frame_u) + varint(frame_c) + varint(signals))
        shutil.copyfileobj(frame, output)
        output.write(varint(signals) + b'Z')
        if events > 1:
            output.write(varint(chain_u))
            shutil.copyfileobj(chain, output)
            table = b'\x03'  # first chain at offset 1, encoded as signed delta
        else:
            table = varint(signals << 1)  # no per-signal chains: values live in the frame
        output.write(table + U64.pack(len(table)))
        shutil.copyfileobj(times, output)
        output.write(U64.pack(time_u) + U64.pack(time_c) + U64.pack(events))
        end = output.tell()
        output.seek(vc_start + 1)
        output.write(U64.pack(end - vc_start - 1))
        output.seek(end)
        output.write(b'\x04' + U64.pack(hier_c + 16) + U64.pack(hier_u))
        shutil.copyfileobj(hier, output)


def wrap_fst(source, destination):
    with open(source, 'rb') as incoming, open(destination, 'wb') as output:
        output.write(b'\xfe' + U64.pack(0) + U64.pack(Path(source).stat().st_size))
        with gzip.GzipFile(fileobj=output, mode='wb', mtime=0) as wrapped:
            shutil.copyfileobj(incoming, wrapped)
        end = output.tell()
        output.seek(1)
        output.write(U64.pack(end - 1))


def worker(path, events, signals, width, memory_mib, repo, background=False):
    sys.path.insert(0, repo)
    from alwavelets.storage import open_trace, use_disk_storage
    from tools.stress_workflows import peak_rss
    before = time.perf_counter()
    auto = use_disk_storage(path)
    with open_trace(path, memory_mib=memory_mib, use_cache=False, background=background) as trace:
        metadata_s = time.perf_counter() - before
        first_waveform_s = None
        if background:
            first_signal = trace.signals[trace.signal_order[0]]
            while trace.is_loading:
                if first_waveform_s is None and first_signal.num_changes():
                    assert first_signal.value_at(0) == '0' * width
                    first_waveform_s = time.perf_counter() - before
                trace._store.thread.join(0.005)
        trace.require_loaded()
        elapsed = time.perf_counter() - before
        assert len(trace.signal_order) == signals
        for ordinal in sorted({0, signals // 2, signals - 1}):
            signal = trace.signals[trace.signal_order[ordinal]]
            assert signal.num_changes() == events
            for t in sorted({0, events // 2, events - 1}):
                expected = str(t % 2) if events > 1 else '0' * width
                assert signal.value_at(t) == expected, (ordinal, t)
        index_path = Path(trace._store.path)
        result = {'events_per_signal': events, 'signals': signals, 'width': width,
                  'input_bytes': Path(path).stat().st_size, 'auto_disk': auto,
                  'load_s': elapsed, 'peak_rss_mib': peak_rss(),
                  'index_bytes': index_path.stat().st_size, 'page_cache_bytes': trace._store.cache_bytes}
        if background:
            result.update(metadata_s=metadata_s, first_waveform_s=first_waveform_s or elapsed)
    assert not index_path.exists()
    return result


def create_block(path, codec, size):
    with open(path, 'wb') as output:
        if codec == 'zlib':
            compressed(repeated(b'a', size), output)
            return
        extra = size - (25 if codec == 'lz4' else 10)
        output.write(b'\x1fa\x01\x00' if codec == 'lz4' else b'\x20a\xe0')
        for chunk in repeated(b'\xff', extra // 255):
            output.write(chunk)
        output.write(bytes([extra % 255]))
        output.write(b'\x50aaaaa' if codec == 'lz4' else b'\x00')


def block_worker(path, codec, size, repo):
    sys.path.insert(0, repo)
    from alwavelets.fst_stream import CHUNK, FileSlice, decompress
    from tools.stress_workflows import peak_rss
    start = time.perf_counter()
    with open(path, 'rb') as source, decompress(FileSlice(source, 0, Path(path).stat().st_size), codec, size, limit=CHUNK) as block:
        assert len(block) == size and block.file is not None
        for offset in (0, size // 2, size - CHUNK):
            assert block[offset:offset + CHUNK] == b'a' * CHUNK
        result = {'decompressed_bytes': size, 'compressed_bytes': Path(path).stat().st_size,
                  'seconds': time.perf_counter() - start, 'peak_rss_mib': peak_rss()}
    assert block.file.closed
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=10000000)
    parser.add_argument('--signals', type=int, default=100000)
    parser.add_argument('--memory-mib', type=int, default=8)
    parser.add_argument('--repo', default=ROOT)
    parser.add_argument('--block-mib', type=int, default=0, help='Also stress large zlib/LZ4/FastLZ blocks (e.g. 1024)')
    parser.add_argument('--background', action='store_true', help='Also measure metadata readiness and first readable waveform')
    parser.add_argument('--wrapped', action='store_true', help='Repeat each trace check with an outer gzip wrapper')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--worker', nargs=4, metavar=('PATH', 'EVENTS', 'SIGNALS', 'WIDTH'))
    parser.add_argument('--block-worker', nargs=3, metavar=('PATH', 'CODEC', 'SIZE'))
    args = parser.parse_args()
    if min(args.events, args.signals, args.memory_mib) < 1:
        parser.error('counts and memory must be positive')
    if args.block_mib < 0:
        parser.error('--block-mib must be nonnegative')
    if args.worker:
        path, events, signals, width = args.worker
        print(json.dumps(worker(path, int(events), int(signals), int(width), args.memory_mib, args.repo, args.background)))
        return
    if args.block_worker:
        path, codec, size = args.block_worker
        print(json.dumps(block_worker(path, codec, int(size), args.repo)))
        return
    results = {}
    with tempfile.TemporaryDirectory(prefix='wavelet-fst-stress-') as directory:
        for name, events, signals, width in [('deep', args.events, 1, 1), ('wide', 1, args.signals, 1), ('frame', 1, 2048, 4096)]:
            print('Generating and checking ' + name + ' FST…', file=sys.stderr, flush=True)
            path = Path(directory) / (name + '.fst')
            create_fst(path, events, signals, width)
            paths = [(name, path)]
            if args.wrapped:
                wrapped = path.with_suffix('.wrapped.fst')
                wrap_fst(path, wrapped)
                paths.append((name + '_wrapped', wrapped))
            for label, source in paths:
                run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--repo', args.repo,
                                      '--worker', str(source), str(events), str(signals), str(width),
                                      '--memory-mib', str(args.memory_mib)] + (['--background'] if args.background else []),
                                     check=True, stdout=subprocess.PIPE, text=True)
                results[label] = json.loads(run.stdout)
        for codec in ('zlib', 'lz4', 'fastlz') if args.block_mib else ():
            print('Checking large ' + codec + ' block…', file=sys.stderr, flush=True)
            path = Path(directory) / (codec + '.block')
            size = args.block_mib * 1024 ** 2
            create_block(path, codec, size)
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--repo', args.repo,
                                  '--block-worker', str(path), codec, str(size)],
                                 check=True, stdout=subprocess.PIPE, text=True)
            results['block_' + codec] = json.loads(run.stdout)
    if args.output:
        args.output.write_text(json.dumps(results, indent=2) + '\n')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
