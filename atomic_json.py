"""Atomic JSON persistence — write to a temp file, then os.replace().

Truncate-then-write (`open(path, 'w')` + `json.dump`) has two failure
modes for state files: a crash / power loss / full disk mid-write leaves
truncated JSON, and a concurrent reader can see the file empty between
the truncate and the write. For routing_config.json the consequence is a
gateway that boots with zero buses. os.replace() is atomic on POSIX, so
readers see either the old file or the new one — never a partial.

Same pattern as endpoints_state.save_state, factored out so every state
file in the tree can use it.
"""

import json
import os


def save_json(path, data, indent=2, **dump_kwargs):
    """Atomically serialise *data* as JSON to *path*.

    Raises on failure (caller decides whether to log or swallow); the
    temp file is cleaned up on error so retries don't trip over it.
    """
    tmp = f'{path}.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=indent, **dump_kwargs)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
