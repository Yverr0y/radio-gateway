"""Per-endpoint log store — receives stdout/stderr batches from link
endpoints (frame type P.LOG) and appends them to rotating files under
``logs/endpoints/<name>.log``.

See ``docs/endpoint_logs_design.md`` for the protocol and rationale.
The motivating bug: the DietPi running the D75 endpoint has no
persistent journald store, so every reboot loses the ``[D75-WD]``
watchdog ticks that would have told us when BT serial went dead.
"""

import logging
import logging.handlers
import os
import re
import threading
from datetime import datetime


# Files larger than this get rotated. 5 MB × 3 backups = 20 MB per endpoint.
# Typical fleet of 5 endpoints → ~100 MB worst-case footprint, easily ignored
# next to the loop recorder's hundreds of MB.
_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 3

# Endpoint names come over the trusted link, but path-traversal-proof anyway.
_SAFE_NAME = re.compile(r'[^a-zA-Z0-9_-]+')


class EndpointLogStore:
    """Append-only per-endpoint log store with size-based rotation."""

    def __init__(self, base_dir='logs/endpoints'):
        self._base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self._handlers = {}   # endpoint_name → RotatingFileHandler
        self._lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def append(self, endpoint_name, lines):
        """Append a batch of log lines for *endpoint_name*.

        *lines* is a list of dicts ``{'ts': float, 'stream': str, 'text': str}``
        as sent over the link in P.LOG frames. Called from the link reader
        thread — must never raise back into the dispatcher.
        """
        if not lines:
            return
        try:
            handler = self._get_handler(endpoint_name)
            for line in lines:
                ts = line.get('ts')
                stream = str(line.get('stream', '?'))[:8]
                text = str(line.get('text', ''))
                if ts:
                    t = datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%dT%H:%M:%S')
                else:
                    t = '?'
                handler.emit(_LogRecord(f'{t} [{stream}] {text}'))
        except Exception as e:
            # Last-resort: don't propagate into the link reader.
            print(f"  [EndpointLogs] append failed for {endpoint_name!r}: {e}")

    def tail(self, endpoint_name, lines=50):
        """Return the last *lines* lines of the on-disk log as a string.

        Reads the current file only (not rotated backups) — sufficient for
        the typical "what's happening now?" query. Returns '' if no log
        exists yet for the endpoint.
        """
        path = self._path_for(endpoint_name)
        if not os.path.exists(path):
            return ''
        # Tail by reading from the end. Files are small (≤5 MB by rotation
        # cap) so just slurp + slice.
        try:
            with open(path, 'rb') as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                # Read at most ~256 KB tail; enough for 1000+ short log lines.
                read_bytes = min(size, 262144)
                f.seek(size - read_bytes)
                blob = f.read(read_bytes)
        except OSError as e:
            return f'[EndpointLogs] read error: {e}\n'
        text = blob.decode('utf-8', errors='replace')
        all_lines = text.splitlines()
        # If we truncated mid-line, drop the first (partial) one.
        if size > 262144 and all_lines:
            all_lines = all_lines[1:]
        return '\n'.join(all_lines[-max(1, lines):])

    def endpoint_names(self):
        """Return a sorted list of endpoint names that have any log on disk."""
        try:
            files = os.listdir(self._base_dir)
        except OSError:
            return []
        names = []
        for f in files:
            if f.endswith('.log'):
                names.append(f[:-4])
        return sorted(names)

    # -- internals ----------------------------------------------------------

    def _path_for(self, endpoint_name):
        safe = _SAFE_NAME.sub('_', endpoint_name) or 'unknown'
        return os.path.join(self._base_dir, f'{safe}.log')

    def _get_handler(self, endpoint_name):
        with self._lock:
            handler = self._handlers.get(endpoint_name)
            if handler is None:
                path = self._path_for(endpoint_name)
                handler = logging.handlers.RotatingFileHandler(
                    path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
                    encoding='utf-8', delay=True,
                )
                # Bare message — we already format the line in append().
                handler.setFormatter(logging.Formatter('%(message)s'))
                self._handlers[endpoint_name] = handler
            return handler

    def close(self):
        with self._lock:
            for h in self._handlers.values():
                try:
                    h.close()
                except Exception:
                    pass
            self._handlers.clear()


class _LogRecord:
    """Minimal stand-in for logging.LogRecord — RotatingFileHandler's
    Formatter sets ``record.message`` on us during format(), so we must
    allow arbitrary attribute assignment (no __slots__). We pay the
    per-line attribute-dict cost (~100 bytes) in exchange for not pulling
    in the full LogRecord constructor's argument parsing.
    """

    def __init__(self, text):
        self.msg = text
        self.args = None
        self.exc_info = None
        self.exc_text = None
        self.stack_info = None
        self.levelno = logging.INFO
        self.levelname = 'INFO'
        self.name = 'endpoint'
        self.created = 0

    def getMessage(self):
        return self.msg
