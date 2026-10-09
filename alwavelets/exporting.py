"""Streaming file output. Incomplete exports never replace a destination."""
import contextlib
import csv
import json
import os
import shutil
import tempfile

from .streaming import check_cancel, iter_window


def write_instruction_csv(matches, destination, *, cancel=None, progress=None):
    """Stream the complete selection, including same-tick records in capture order."""
    fields = ('time', 'hart_id', 'thread_id', 'pc', 'bits', 'rd_addr', 'rd_data',
              'mem_addr', 'mem_wdata', 'mem_rdata', 'csr_addr', 'csr_wdata', 'stall')
    count = 0
    check_cancel(cancel)
    with atomic_output(destination) as output:
        writer = csv.writer(output, lineterminator='\n')
        writer.writerow(fields + ('mnemonic', 'asm', 'decoded_json'))
        iterator = iter(matches)
        try:
            for entry in iterator:
                check_cancel(cancel)
                decoded = entry.get('decoded', {})
                writer.writerow(tuple(entry.get(field) for field in fields) +
                                (decoded.get('mnemonic'), decoded.get('asm'), json.dumps(decoded, sort_keys=True)))
                count += 1
                if progress and count % 4096 == 0:
                    progress(count, len(matches))
        finally:
            close = getattr(iterator, 'close', None)
            if close:
                close()
        if progress:
            progress(count, len(matches))
        check_cancel(cancel)
    return count


@contextlib.contextmanager
def atomic_output(destination, binary=False):
    """A supplied stream is caller-owned; paths are replaced only on success."""
    if hasattr(destination, 'write'):
        yield destination
        return
    target = os.path.abspath(destination)
    descriptor, temporary = tempfile.mkstemp(prefix='.alwavelets-export-', dir=os.path.dirname(target))
    try:
        options = {} if binary else {'encoding': 'utf-8', 'newline': ''}
        with os.fdopen(descriptor, 'wb' if binary else 'w', **options) as stream:
            yield stream
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_signal_csv(signal, destination, start=0, end=None, *, cancel=None, progress=None):
    """Write exact events (including deltas), returning the number of rows."""
    end = signal._times[-1] if end is None and len(signal._times) else (0 if end is None else end)
    count = 0
    check_cancel(cancel)
    with atomic_output(destination) as stream:
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(('time', 'value'))
        for t, value in iter_window(signal, start, end, baseline=False, cancel=cancel):
            writer.writerow((t, value))
            count += 1
            if progress is not None and count % 4096 == 0:
                progress(t, end)
        check_cancel(cancel)
        if progress is not None:
            progress(end, end)
        check_cancel(cancel)
    return count


def write_svg(vcd, destination, *args, cancel=None, **kwargs):
    """Write SVG incrementally; render_svg remains the string-returning API."""
    from .snapshot import iter_svg
    with atomic_output(destination) as stream:
        for line in iter_svg(vcd, *args, cancel=cancel, **kwargs):
            stream.write(line)
            stream.write('\n')
        check_cancel(cancel)


def write_pdf(destination, content, page_width=842, page_height=595):
    """Spool PDF commands to disk to obtain /Length without a giant string."""
    with tempfile.TemporaryFile() as commands:
        for line in content:
            commands.write(line.encode('latin-1', errors='replace'))
            commands.write(b'\n')
        length = commands.tell()
        commands.seek(0)
        with atomic_output(destination, binary=True) as output:
            output.write(b'%PDF-1.4\n')
            offsets = []
            def obj(number, body):
                offsets.append(output.tell())
                output.write(('%d 0 obj' % number).encode() + body + b'endobj\n')
            obj(1, b'<</Type/Catalog/Pages 2 0 R>>')
            obj(2, b'<</Type/Pages/Kids[3 0 R]/Count 1>>')
            obj(3, ('<</Type/Page/Parent 2 0 R/MediaBox[0 0 %d %d]/Contents 4 0 R'
                    '/Resources<</Font<</F1 5 0 R>>>>>>' % (page_width, page_height)).encode())
            offsets.append(output.tell())
            output.write(('4 0 obj<</Length %d>>stream\n' % length).encode())
            shutil.copyfileobj(commands, output, length=1024 * 1024)
            output.write(b'\nendstream\nendobj\n')
            obj(5, b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>')
            xref = output.tell()
            output.write(b'xref\n0 6\n0000000000 65535 f \n')
            for offset in offsets:
                output.write(('%010d 00000 n \n' % offset).encode())
            output.write(('trailer<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF' % xref).encode())
