"""Waveform cache retention, shared readers, and exclusive index builders."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import shutil
import sqlite3

QUOTA_BYTES = None  # opt-in retention limit; zero disables waveform caching
_CACHE_NAME = re.compile(r'.+\.[0-9a-f]{16}\.wlc(?:\.sqlite(?:\.partial)?)?', re.DOTALL)


class IndexLease:
    """A separate SQLite transaction survives index commits and file renames.

    Locks are released by the OS after a crash. Busy or unwritable caches fall
    back to temporary indexing, so two viewers never modify the same partial.
    The small lock databases stay in place to avoid lock-file unlink races.
    """
    def __init__(self, path, *, shared=False, timeout=0):
        path = str(path)
        # Complete and partial SQLite indexes have the same lifetime lock.
        if path.endswith('.wlc.sqlite'):
            path += '.partial'
        self.db = sqlite3.connect(path + '.lock', timeout=timeout, check_same_thread=False)
        try:
            self.db.execute('CREATE TABLE IF NOT EXISTS lease(id INTEGER PRIMARY KEY)')
            if shared:
                self.share()
            else:
                self.db.execute('BEGIN EXCLUSIVE')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def share(self):
        # Caller holds the maintenance lock while downgrading a builder.
        self.db.rollback()
        try:
            self.db.execute('BEGIN')
            self.db.execute('SELECT COUNT(*) FROM lease').fetchone()
        except sqlite3.Error:
            self.db.rollback()
            self.db.execute('BEGIN EXCLUSIVE')


def disk_full(exc):
    code = getattr(exc, 'sqlite_errorcode', 0)
    return (isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', errno.ENOSPC)) or
            isinstance(exc, sqlite3.Error) and (code & 255 == 13 or 'database or disk is full' in str(exc).lower()))


class WaveformCache:
    def __init__(self, directory=None, quota=None):
        from .cache import _cache_dir
        self.directory = Path(directory if directory is not None else _cache_dir())
        self.quota = QUOTA_BYTES if quota is None else quota
        if self.quota is not None and self.quota < 0:
            raise ValueError('waveform cache quota must be nonnegative')

    @contextmanager
    def locked(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease = IndexLease(self.directory / 'waveform-control', timeout=1)
        try:
            yield
        finally:
            lease.close()

    def acquire(self, path, *, shared=False):
        with self.locked():
            return IndexLease(path, shared=shared)

    def share(self, lease):
        # Purge and other opens cannot enter the gap between lock modes.
        try:
            with self.locked():
                lease.share()
        except (OSError, sqlite3.Error):
            # A failed maintenance acquisition leaves the original lock intact.
            # A failed downgrade must have restored an exclusive transaction.
            if not lease.db.in_transaction:
                raise

    @staticmethod
    def touch(path):
        try:
            os.utime(path, None)
        except OSError:
            pass

    def _files(self):
        if not self.directory.is_dir():
            return []
        files = []
        for path in self.directory.iterdir():
            if not _CACHE_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                size = stat.st_size
                if '.wlc.sqlite' in path.name:
                    for suffix in ('-journal', '-wal', '-shm'):
                        sidecar = Path(str(path) + suffix)
                        if sidecar.is_file() and not sidecar.is_symlink():
                            size += sidecar.stat().st_size
                files.append((stat.st_mtime_ns, path, size))
            except FileNotFoundError:
                pass
        return sorted(files)

    def inspect(self):
        entries = []
        if self.directory.is_dir():
            with self.locked():
                for used, path, size in self._files():
                    try:
                        lease = IndexLease(path)
                    except (OSError, sqlite3.Error):
                        busy = True
                    else:
                        lease.close()
                        busy = False
                    entries.append({'name': path.name, 'bytes': size, 'last_used_ns': used,
                                    'kind': 'partial' if path.name.endswith('.partial') else
                                            ('index' if path.name.endswith('.sqlite') else 'eager'),
                                    'busy': busy})
        return {'directory': str(self.directory), 'quota_bytes': self.quota,
                'used_bytes': sum(e['bytes'] for e in entries),
                'free_bytes': shutil.disk_usage(self.directory).free if self.directory.is_dir() else None,
                'entries': entries}

    def _remove(self, files, *, limit=None, exclude=()):
        total = sum(n for _, _, n in files)
        excluded = {os.path.abspath(path) for path in exclude}
        count = size = skipped = 0
        for _, path, n in files:
            if limit is not None and total <= limit:
                break
            if os.path.abspath(path) in excluded:
                continue
            lease = None
            try:
                lease = IndexLease(path)
                discard_partial(path)
                count += 1
                size += n
                total -= n
            except (OSError, sqlite3.Error):
                skipped += 1
            finally:
                if lease is not None:
                    lease.close()
        return count, size, skipped

    def trim(self, *, exclude=()):
        """Best-effort retention: active readers/builders can exceed the quota."""
        if not self.quota:
            return 0, 0, 0
        try:
            with self.locked():
                return self._remove(self._files(), limit=self.quota, exclude=exclude)
        except (OSError, sqlite3.Error):
            return 0, 0, 0

    def purge(self, names=None, *, partial_only=False):
        """Remove only known cache entries, taking each file's exclusive lease."""
        if names is not None:
            names = set(names)
            if any(not _CACHE_NAME.fullmatch(name) or Path(name).name != name for name in names):
                raise ValueError('Use exact cache entry names from --list-waveform-cache')
        if not self.directory.is_dir():
            return 0, 0, 0
        with self.locked():
            files = [entry for entry in self._files()
                     if (names is None or entry[1].name in names) and
                        (not partial_only or entry[1].name.endswith('.partial'))]
            return self._remove(files)


def discard_partial(path):
    """Caller must hold the lease and have closed the index connection."""
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            os.unlink(str(path) + suffix)
        except FileNotFoundError:
            pass


def purge_partial_indexes():
    """Remove idle incomplete indexes; skip indexes owned by an open viewer."""
    return WaveformCache().purge(partial_only=True)
