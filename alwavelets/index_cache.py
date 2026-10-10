"""Exclusive ownership and cleanup of resumable waveform indexes."""
import os
from pathlib import Path
import sqlite3


class IndexLease:
    """A separate SQLite transaction survives index commits and file renames.

    Locks are released by the OS after a crash. Busy or unwritable caches fall
    back to temporary indexing, so two viewers never modify the same partial.
    The small lock databases stay in place to avoid lock-file unlink races.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(str(path) + '.lock', timeout=0, check_same_thread=False)
        try:
            self.db.execute('BEGIN IMMEDIATE')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()


def discard_partial(path):
    """Caller must hold the lease and have closed the index connection."""
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            os.unlink(str(path) + suffix)
        except FileNotFoundError:
            pass


def purge_partial_indexes():
    """Remove idle incomplete indexes; skip indexes owned by an open viewer."""
    from .cache import _cache_dir
    count = size = skipped = 0
    for path in Path(_cache_dir()).glob('*.wlc.sqlite.partial'):
        if path.is_symlink() or not path.is_file():
            continue
        lease = None
        try:
            lease = IndexLease(path)
            n = path.stat().st_size
            discard_partial(path)
            count += 1
            size += n
        except (OSError, sqlite3.Error):
            skipped += 1
        finally:
            if lease is not None:
                lease.close()
    return count, size, skipped
