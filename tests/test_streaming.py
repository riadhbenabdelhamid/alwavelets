"""Streaming must preserve events, yield early, and avoid window-sized data."""
import io
import os
from pathlib import Path
import random
import struct
import threading
import tracemalloc
import zlib

import pytest

from alwavelets.core import VCDSignal
from alwavelets.parsers import VCDFile, parse_file, ConverterError
from alwavelets.graphics import render_waveform_png, write_waveform_png
from alwavelets.exporting import write_signal_csv, write_svg, write_pdf
from alwavelets.streaming import OperationCancelled
from alwavelets.decoders import SPIDecoder, I2CDecoder, AXIDecoder, _PendingResponses


def trace(*signals):
    result = VCDFile()
    result.signals = {sig.identifier: sig for sig in signals}
    result.signal_order = list(result.signals)
    result.max_time = max((sig._times[-1] for sig in signals if len(sig._times)), default=0)
    return result


def signal(name, changes, size=1):
    sig = VCDSignal(name, name, size)
    for t, value in changes:
        sig.add_change(t, value)
    return sig


def pixels(png):
    pos, data = 8, bytearray()
    while pos < len(png):
        n = struct.unpack('>I', png[pos:pos + 4])[0]
        if png[pos + 4:pos + 8] == b'IDAT':
            data.extend(png[pos + 8:pos + 8 + n])
        pos += 12 + n
    return zlib.decompress(data)


def test_png_streams_rows_and_preserves_image(tmp_path):
    sig = signal('a', [(0, '0'), (11, '1'), (11, 'x'), (12, 'z'), (13, '0'), (100, '1')])
    vcd = trace(sig)
    report = []
    path = tmp_path / 'wave.png'
    size = write_waveform_png(vcd, path, ['a'], width=20, progress=lambda a, b: report.append((a, b)))
    assert size == path.stat().st_size
    assert pixels(path.read_bytes()) == pixels(render_waveform_png(vcd, ['a'], width=20))
    assert report == [(1, 1)]


def test_png_working_memory_does_not_scale_with_changes(monkeypatch):
    sig = signal('a', ((i, str(i % 2)) for i in range(32000)))
    vcd = trace(sig)
    def materialization_forbidden(*args):
        raise AssertionError('export must stream events, including eager traces')
    monkeypatch.setattr(VCDSignal, 'changes_in_window', materialization_forbidden)
    tracemalloc.start()
    try:
        data = render_waveform_png(vcd, ['a'], width=32)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(data) > 50
    assert peak < 1024 * 1024  # old segment+edge lists alone require several MiB


@pytest.mark.parametrize('writer,extension', [(write_waveform_png, 'png'), (write_svg, 'svg')])
def test_cancelled_export_preserves_destination(tmp_path, writer, extension):
    vcd = trace(signal('a', [(0, '0'), (1, '1')]))
    path = tmp_path / ('old.' + extension)
    path.write_bytes(b'keep me')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(OperationCancelled):
        writer(vcd, path, ['a'], cancel=cancel)
    assert path.read_bytes() == b'keep me'
    assert list(tmp_path.iterdir()) == [path]


def test_csv_delta_events_and_cancellation_midway(tmp_path):
    sig = signal('a', ((i // 2, str(i % 2)) for i in range(10000)))
    path = tmp_path / 'events.csv'
    assert write_signal_csv(sig, path, 0, 2) == 6
    assert path.read_text().splitlines() == ['time,value', '0,0', '0,1', '1,0', '1,1', '2,0', '2,1']
    old = path.read_bytes()
    cancel = threading.Event()
    with pytest.raises(OperationCancelled):
        write_signal_csv(sig, path, cancel=cancel, progress=lambda *_: cancel.set())
    assert path.read_bytes() == old


def test_svg_is_written_incrementally(monkeypatch):
    from alwavelets.snapshot import iter_svg
    sig = signal('a', ((i, format(i, '016b')) for i in range(1000)), size=16)
    def forbidden(*args):
        raise AssertionError('window materialization')
    monkeypatch.setattr(VCDSignal, 'changes_in_window', forbidden)
    output = io.StringIO()
    vcd = trace(sig)
    write_svg(vcd, output, ['a'])
    assert output.getvalue() == ''.join(line + '\n' for line in iter_svg(vcd, ['a']))
    assert output.getvalue().endswith('</svg>\n')


def test_pdf_stream_length_and_xref(tmp_path):
    path = tmp_path / 'test.pdf'
    write_pdf(path, ('%d 0 m 1 1 l S' % i for i in range(10000)))
    pdf = path.read_bytes()
    xref = int(pdf.split(b'startxref\n')[1].splitlines()[0])
    assert pdf[xref:xref + 4] == b'xref'
    lines = pdf[xref:].splitlines()
    for number, entry in enumerate(lines[3:8], 1):
        offset = int(entry.split()[0])
        assert pdf[offset:].startswith(('%d 0 obj' % number).encode())
    length = int(pdf.split(b'4 0 obj<</Length ')[1].split(b'>>')[0])
    commands = pdf.split(b'>>stream\n', 1)[1].split(b'\nendstream', 1)[0]
    assert len(commands) == length


def test_spi_first_frame_does_not_scan_capture(monkeypatch):
    clock = signal('clk', ((i, str(i % 2)) for i in range(10000)))
    data = signal('data', [(0, '1')])
    original = VCDSignal.iter_changes
    def guarded(sig, *args):
        for count, event in enumerate(original(sig, *args)):
            assert count < 40, 'first byte must not read the whole clock'
            yield event
    monkeypatch.setattr(VCDSignal, 'iter_changes', guarded)
    frames = SPIDecoder(clock, data).iter_decode(0, 9999)
    assert next(frames).label == '0xFF'
    frames.close()


def test_i2c_yields_final_ack_without_scanning_to_stop(monkeypatch):
    clock = signal('clk', ((i, str(i % 2)) for i in range(10000)))
    # SDA changes only while clock is low, with no START/STOP until EOF.
    data = signal('data', ((i, str((i // 2) % 2)) for i in range(0, 10000, 2)))
    original = VCDSignal.iter_changes
    def guarded(sig, *args):
        for count, event in enumerate(original(sig, *args)):
            assert count < 40, 'a byte should not scan to the next STOP'
            yield event
    monkeypatch.setattr(VCDSignal, 'iter_changes', guarded)
    frames = I2CDecoder(data, clock).iter_decode(0, 9999)
    first = next(frames)
    assert first.t_end == 17 and first.data == 'ACK'
    snapshot = first.label, first.data, first.t_end
    next(frames)
    assert (first.label, first.data, first.t_end) == snapshot
    frames.close()


def test_axi_response_spool_matches_legacy_matching_order():
    rng = random.Random(91)
    events = [(i // 2, rng.randrange(3)) for i in range(5000)]
    remaining = list(events)
    with _PendingResponses(iter(events)) as pending:
        for _ in range(200):
            minimum, key = rng.randrange(2500), rng.randrange(4)
            index = next((i for i, (t, k) in enumerate(remaining) if t >= minimum and k == key), None)
            expected = remaining.pop(index)[0] if index is not None else None
            assert pending.take(minimum, key) == expected
        assert pending.writes > 1000


def test_axi_iterator_cancellation():
    valid = signal('v', ((i, str(i % 2)) for i in range(10000)))
    ready = signal('r', [(0, '1')])
    cancel = threading.Event()
    points = AXIDecoder._iter_handshakes(valid, ready, cancel=cancel)
    assert next(points) == 1
    cancel.set()
    with pytest.raises(OperationCancelled):
        list(points)


def converter_script(tmp_path, failure=False):
    import sys
    path = tmp_path / 'fake-converter'
    path.write_text('#!' + sys.executable + '\nimport sys\n' +
                    ("sys.stderr.write('E' * 1000000)\nsys.exit(2)\n" if failure else
                     "sys.stdout.write('$scope module top $end\\n$var wire 1 ! a $end\\n$upscope $end\\n$enddefinitions $end\\n')\n"
                     "for i in range(30000): sys.stdout.write('#%d\\n%d!\\n' % (i, i % 2))\n"))
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize('extension', ['lxt', 'lxt2', 'ghw', 'vzt'])
def test_converter_streams_to_disk_and_preserves_source(tmp_path, monkeypatch, extension):
    import alwavelets.parsers as parsers
    source = tmp_path / ('source.' + extension)
    source.write_bytes(b'legacy')
    converter = converter_script(tmp_path)
    monkeypatch.setattr(parsers.shutil, 'which', lambda _: converter)
    original = parsers.subprocess.run
    paths = []
    def run(*args, **kwargs):
        assert 'capture_output' not in kwargs
        assert hasattr(kwargs['stdout'], 'fileno') and hasattr(kwargs['stderr'], 'fileno')
        paths.append(Path(kwargs['stdout'].name))
        return original(*args, **kwargs)
    monkeypatch.setattr(parsers.subprocess, 'run', run)
    with parse_file(source, storage='disk', memory_mib=1, use_cache=False) as vcd:
        assert vcd._filename == os.fspath(source)
        assert vcd.signals['!'].num_changes() == 30000
        assert vcd.signals['!'].value_at(29999) == '1'
        assert all(not path.exists() for path in paths)


def test_converter_stderr_is_bounded_and_timeout_cleans_up(tmp_path, monkeypatch):
    import alwavelets.parsers as parsers
    converter = converter_script(tmp_path, failure=True)
    with pytest.raises(ConverterError, match='stderr truncated') as failure:
        parsers._convert_trace('unused', converter)
    assert len(str(failure.value)) < 66000
    paths = []
    def timeout(*args, **kwargs):
        paths.append(Path(kwargs['stdout'].name))
        raise parsers.subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(parsers.subprocess, 'run', timeout)
    with pytest.raises(ConverterError, match='timed out'):
        parsers._convert_trace('unused', converter)
    assert all(not path.parent.exists() for path in paths)


@pytest.mark.parametrize('storage', ['auto', 'disk'])
def test_converter_storage_policy_bypasses_eager_cache(tmp_path, monkeypatch, storage):
    import alwavelets.cache as cache
    import alwavelets.parsers as parsers
    source = tmp_path / 'cached.lxt'
    source.write_bytes(b'legacy')
    converter = converter_script(tmp_path)
    monkeypatch.setattr(parsers.shutil, 'which', lambda _: converter)
    def forbidden(*args):
        raise AssertionError('eager cache must not override converted storage policy')
    monkeypatch.setattr(cache, 'load_cache', forbidden)
    with parse_file(source, storage=storage, memory_mib=1) as vcd:
        assert vcd.signals['!'].num_changes() == 30000
        assert (getattr(vcd, '_store', None) is not None) == (storage == 'disk')


def test_very_wide_summary_keeps_exact_values(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    path = tmp_path / 'wide.vcd'
    bits = 65536
    path.write_text('$scope module top $end\n$var wire %d ! wide $end\n'
                    '$upscope $end\n$enddefinitions $end\n#0\nb%s !\n#1\nb%s !\n'
                    % (bits, '0' * bits, '1' * bits))
    for _ in range(2):
        with parse_file(path, storage='disk', memory_mib=1) as vcd:
            assert vcd.signals['!'].numeric_range_in_window(0, 1) == (0, (1 << bits) - 1)
            assert vcd.signals['!'].value_at(1) == '1' * bits


def test_viewer_exports_use_streaming_and_current_radix(tmp_path, monkeypatch):
    from alwavelets.ui_export import ExportMixin
    from alwavelets.core import Radix
    import xml.etree.ElementTree as ET
    class View(ExportMixin):
        pass
    view = View()
    sig = signal('a', ((i, str(i % 2)) for i in range(2000)))
    bus = signal('bus', [(0, '00001111'), (1000, '00101010')], size=8)
    view.vcd = trace(sig, bus)
    view.vcd._filename = 'streaming-test.vcd'
    view.display_order = ['a', 'bus']
    view.sig_offset, view.visible_signals = 0, 2
    view.time_start, view.time_end, view.cursor_time = 0, 1999, 0
    view.cursor_on, view.wave_width = True, 80
    view.sig_height, view.analog_signals = {}, set()
    view.get_signal = lambda sid: view.vcd.signals[sid]
    view.get_signal_radix = lambda sid: Radix.DEC
    view.col_to_time = lambda col: int(col / 80 * view.time_end)
    view.format_ruler_time = str
    def forbidden(*args):
        raise AssertionError('viewer export must not allocate a window')
    monkeypatch.setattr(VCDSignal, 'changes_in_window', forbidden)
    svg, pdf = tmp_path / 'view.svg', tmp_path / 'view.pdf'
    view._generate_svg(svg)
    view._generate_pdf(pdf)
    assert '15' in ''.join(ET.parse(svg).getroot().itertext())
    assert pdf.read_bytes().endswith(b'%%EOF')
