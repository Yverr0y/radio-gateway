"""Temp directories for tests that clean themselves up.

Why this exists: several suites here create a temp dir per case and fill it
with stub files — test_soundboard_categories pre-creates one empty file per
soundboard pool entry (~780) so _fill_soundboard_slots never touches the
network. Left behind, a single run leaked 144 directories and ~64,000 inodes.
Repeated runs exhausted /tmp's inode table (2,035,767 inodes, 100% used) while
`df -h` still showed gigabytes free — the stub files are empty, so it is inodes
that run out, not bytes. Every process on the box writing to /tmp then failed
with "No space left on device", and the tests themselves began failing because
they could not create files, which reads as a code regression rather than a
full disk. Check `df -i` before believing `df -h`.

Cleanup is registered three ways because one is not enough:

  * atexit           — the normal path, including a non-zero exit after a
                       failed check.
  * SIGTERM/SIGINT   — atexit does NOT run when a test runner's timeout kills
                       the process. These suites are slow enough to be killed
                       in practice (test_ic7100_civ legitimately takes ~117 s),
                       and a run killed mid-way is exactly when the most dirs
                       are outstanding.
  * stale sweep      — SIGKILL and power loss cannot be trapped at all, so each
                       run also removes same-prefix dirs older than an hour.
                       Whatever escapes is collected by the next run.
"""
import atexit
import os
import shutil
import signal
import tempfile
import time

_TMPDIRS = []
_SWEPT = set()
_STALE_AFTER = 3600.0


def _sweep(prefix):
    """Remove leftovers from a previous run that was killed before cleanup."""
    if prefix in _SWEPT:
        return
    _SWEPT.add(prefix)
    root = tempfile.gettempdir()
    cutoff = time.time() - _STALE_AFTER
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def mkdtemp(prefix):
    """A temp dir removed when this test process ends, however it ends."""
    if not prefix.endswith('-'):
        prefix += '-'
    _sweep(prefix)
    d = tempfile.mkdtemp(prefix=prefix)
    _TMPDIRS.append(d)
    return d


def cleanup():
    while _TMPDIRS:
        shutil.rmtree(_TMPDIRS.pop(), ignore_errors=True)


atexit.register(cleanup)


def _on_signal(signum, _frame):
    cleanup()
    # Re-raise with the default handler so the exit status still reflects the
    # signal — swallowing it would hide a timeout from the caller.
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


for _sig in (signal.SIGTERM, signal.SIGINT):
    try:
        signal.signal(_sig, _on_signal)
    except (ValueError, OSError):     # not on the main thread
        pass
