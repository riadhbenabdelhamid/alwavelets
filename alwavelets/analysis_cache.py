"""Reusable instruction indexes. Cache failures leave a usable temporary index."""
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from .streaming import OperationCancelled, check_cancel

QUOTA_BYTES = 8 * 1024 ** 3
FORMAT_VERSION = 1
_CACHE_NAME = re.compile(r'^[0-9a-f]{64}\.instructions\.sqlite$')
_ERRORS = (OSError, sqlite3.Error, ValueError, KeyError, TypeError, EOFError, AttributeError)


def source_signature(path):
    path = os.path.realpath(path)
    st = os.stat(path)
    return [path, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def stamp_source(trace, path):
    """Remember the source at parse time, not when an analysis is requested."""
    trace._analysis_source = source_signature(path)


@lru_cache(maxsize=1)
def implementation_signature():
    digest = hashlib.sha256()
    for name in ('decoders', 'instruction_store', 'parsers', 'fst', 'core', 'value_stream', 'storage'):
        digest.update(Path(__file__).with_name(name + '.py').read_bytes())
    return digest.hexdigest()


def identity_for(trace, tags, decoder):
    from .decoders import RV32Decoder
    from .persistence import SignalTagStore
    if type(decoder) is not RV32Decoder or type(tags) is not SignalTagStore:
        return None  # Custom implementations retain the ordinary build path.
    try:
        source = source_signature(trace._filename)
        if source != getattr(trace, '_analysis_source', None):
            return None
        identity = {'format': FORMAT_VERSION, 'implementation': implementation_signature(),
                    'python': list(sys.version_info[:2]), 'source': source,
                    'extent': [trace.min_time, trace.max_time],
                    'decoder': vars(decoder), 'isa': tags.isa, 'hart': tags.hart_id, 'thread': tags.thread_id,
                    'tags': tags.tags, 'hart_tags': sorted([list(k) + [v] for k, v in tags.hart_tags.items()])}
        return json.loads(json.dumps(identity, sort_keys=True))
    except _ERRORS:
        return None


def cache_directory():
    from .cache import _cache_dir
    return Path(_cache_dir()) / 'instructions'


class InstructionCache:
    def __init__(self, directory=None, quota=None):
        self.directory = Path(directory) if directory is not None else cache_directory()
        self.quota = QUOTA_BYTES if quota is None else max(0, int(quota))

    def path(self, identity):
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return self.directory / (key + '.instructions.sqlite')

    @contextmanager
    def locked(self, cancel=None):
        # SQLite serializes publishers and quota/purge operations across processes.
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.directory / 'control.sqlite'), timeout=0.1)
        try:
            for _ in range(100):
                check_cancel(cancel)
                try:
                    db.execute('BEGIN IMMEDIATE')
                    break
                except sqlite3.OperationalError as exc:
                    if 'locked' not in str(exc).lower():
                        raise
            else:
                raise OSError('Instruction cache is busy')
            # A publisher holds this lock throughout its backup. Any staging
            # files here belong to an interrupted process and can be reclaimed.
            for path in self.directory.glob('.building-*.sqlite*'):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            yield
        finally:
            db.close()

    def _files(self):
        files = []
        for path in self.directory.iterdir():
            if _CACHE_NAME.fullmatch(path.name) and not path.is_symlink():
                try:
                    st = path.stat()
                    files.append((st.st_mtime_ns, path, st.st_size))
                except FileNotFoundError:
                    pass
        return sorted(files)

    def _trim(self, limit, exclude=None):
        files = self._files()
        total = sum(size for _, path, size in files if path != exclude)
        for _, path, size in files:
            if total <= limit:
                break
            if path == exclude:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                pass  # An open file on some platforms cannot be evicted yet.
        return total <= limit

    def load(self, identity, owner=None, cancel=None):
        from .instruction_store import InstructionResults
        if not self.quota or not self.directory.exists():
            return None
        try:
            with self.locked(cancel):
                self._trim(self.quota)
                path = self.path(identity)
                if path.is_symlink() or not path.is_file():
                    return None
                result = InstructionResults.open_cached(path, identity, owner)
                try:
                    os.utime(path, None)  # LRU without changing the read-only database.
                except OSError:
                    pass
                return result
        except OperationCancelled:
            raise
        except _ERRORS:
            return None

    def publish(self, results, identity, cancel=None, progress=None):
        """Publish a complete snapshot atomically, with bounded copy memory."""
        if not self.quota or os.path.getsize(results.path) > self.quota:
            return False
        temporary = None
        try:
            with self.locked(cancel):
                descriptor, temporary = tempfile.mkstemp(prefix='.building-', suffix='.sqlite', dir=self.directory)
                os.close(descriptor)
                with results.sql_operation(cancel) as db:
                    db.execute('CREATE TABLE analysis_meta(key TEXT PRIMARY KEY,value TEXT)')
                    db.executemany('INSERT INTO analysis_meta VALUES(?,?)',
                                   [('identity', json.dumps(identity, sort_keys=True)),
                                    ('count', str(len(results))), ('complete', '1')])
                    db.commit()
                    destination = sqlite3.connect(temporary)
                    try:
                        def copied(status, remaining, total):
                            check_cancel(cancel)
                            if progress:
                                progress(total - remaining, total)
                        db.backup(destination, pages=256, progress=copied)
                    finally:
                        destination.close()
                size = os.path.getsize(temporary)
                if size > self.quota:
                    return False
                if source_signature(identity['source'][0]) != identity['source']:
                    return False
                target = self.path(identity)
                if not self._trim(self.quota - size, exclude=target):
                    return False
                check_cancel(cancel)
                os.replace(temporary, target)
            return True
        except OperationCancelled:
            raise
        except _ERRORS:
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def purge(self, cancel=None):
        """Remove only completed instruction cache files, never source traces."""
        if not self.directory.exists():
            return 0, 0
        count = size = 0
        with self.locked(cancel):
            for _, path, n in self._files():
                check_cancel(cancel)
                try:
                    path.unlink()
                    count += 1
                    size += n
                except OSError:
                    pass
        return count, size


def collect_instructions(trace, vcd, tags, decoder, *, use_cache=True, cancel=None, progress=None):
    from . import cache
    from .core import ensure_trace_loaded
    from .instruction_store import InstructionResults
    check_cancel(cancel)
    ensure_trace_loaded(vcd)
    identity = identity_for(vcd, tags, decoder) if use_cache and cache.ENABLED and QUOTA_BYTES else None
    manager = InstructionCache()
    if identity is not None:
        cached = manager.load(identity, vcd, cancel)
        if cached is not None:
            try:
                check_cancel(cancel)
                if identity_for(vcd, tags, decoder) == identity:
                    return cached
            except BaseException:
                cached.close()
                raise
            cached.close()
            identity = None
    results = InstructionResults.collect(trace.iter_entries(vcd, tags, decoder, cancel=cancel), dict,
                                         owner=vcd, cancel=cancel, progress=progress, end=vcd.max_time)
    try:
        if identity is not None and identity_for(vcd, tags, decoder) == identity:
            if manager.publish(results, identity, cancel, progress):
                cached = manager.load(identity, vcd, cancel)
                if cached is not None:
                    cached.cache_hit = False  # This request built the index.
                    results.close()
                    results = cached
        check_cancel(cancel)
        return results
    except BaseException:
        results.close()
        raise
