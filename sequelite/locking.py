"""Short-lived, cross-process locks for atomic database saves."""
from contextlib import contextmanager
import errno
import os

if os.name == 'nt':
    import msvcrt
else:
    import fcntl


@contextmanager
def writer_lock(path):
    # Keep the sidecar: deleting it could let processes lock different files.
    with open(str(path) + '.sequelite-lock', 'a+b') as handle:
        if os.name == 'nt':
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
        try:
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError('Database is busy: another writer is saving') from exc
            raise
        # Closing the handle releases the lock, including on errors.
        yield
