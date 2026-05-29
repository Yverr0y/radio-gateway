"""Packet mode state machine.

Before this mixin, ``self._mode`` was the only signal of where packet was.
It conflated "what the user asked for" with "what subsystems are actually
up", and the asynchronous threads (KISS connect loop, delayed Pat start)
silently swallowed failures.

The state machine has two axes:

* **target** — what was requested. One of ``'idle' | 'aprs' | 'winlink' | 'bbs'``.
  Set synchronously by ``_set_mode``; this is what the user sees the mode
  set to immediately.
* **phase** — where we are in the transition lifecycle. One of:
  ``'steady'`` (target is achieved), ``'starting'`` (transitioning toward
  target), ``'stopping'`` (going back to idle), ``'error'`` (transition
  failed; ``last_error`` carries the reason).

A third field, **step**, names which subsystem is currently being
exercised: ``'send_endpoint_mode'``, ``'start_local_tnc'``,
``'connect_kiss'``, ``'start_pat'``, or None when phase is steady/error.

Substep transitions are issued by ``_advance``. The legacy ``self._mode``
attribute stays as a back-compat alias for ``target``; existing readers
(get_status, /packet page, MCP tools) keep working unchanged.
"""

import threading
import time


# Targets
TARGET_IDLE = 'idle'
TARGET_APRS = 'aprs'
TARGET_WINLINK = 'winlink'
TARGET_BBS = 'bbs'
VALID_TARGETS = (TARGET_IDLE, TARGET_APRS, TARGET_WINLINK, TARGET_BBS)

# Phases
PHASE_STEADY = 'steady'
PHASE_STARTING = 'starting'
PHASE_STOPPING = 'stopping'
PHASE_ERROR = 'error'

# Steps
STEP_NONE = None
STEP_SEND_ENDPOINT_MODE = 'send_endpoint_mode'
STEP_START_LOCAL_TNC = 'start_local_tnc'
STEP_CONNECT_KISS = 'connect_kiss'
STEP_START_PAT = 'start_pat'
STEP_STOP_PAT = 'stop_pat'
STEP_STOP_LOCAL_TNC = 'stop_local_tnc'


class _PacketStateMixin:
    """State-machine tracking + accessors.

    All fields live on ``self`` because ``PacketRadioPlugin.__init__``
    initialises them; this mixin only owns the read/update helpers.
    """

    def _init_packet_state(self):
        """Call from PacketRadioPlugin.__init__ to seed the state fields.

        Kept out of a real __init__ so it composes cleanly with the other
        mixins (Python's MRO + cooperative __init__ is a footgun for the
        kind of incremental refactor we're doing here).
        """
        self._state_lock = threading.Lock()
        self._target = TARGET_IDLE
        self._phase = PHASE_STEADY
        self._step = STEP_NONE
        self._last_error = None
        self._phase_changed_at = time.monotonic()

    # ── state mutation ─────────────────────────────────────────────

    def _advance(self, *, phase=None, step=None, error=None, target=None):
        """Update one or more state fields under the lock.

        Passing ``target`` also keeps the legacy ``self._mode`` attribute
        in sync, which the existing UI/MCP/status readers consult.
        """
        with self._state_lock:
            now = time.monotonic()
            if target is not None:
                self._target = target
                self._mode = target  # back-compat alias
            if phase is not None and phase != self._phase:
                self._phase = phase
                self._phase_changed_at = now
            if step is not None or phase == PHASE_STEADY or phase == PHASE_ERROR:
                # STEP_NONE on steady/error so the UI doesn't show a stale step.
                self._step = step if (phase != PHASE_STEADY and phase != PHASE_ERROR) else STEP_NONE
            if error is not None:
                self._last_error = error
            # Clear last_error on a successful steady transition so the UI
            # only shows it while phase=='error' or until next transition.
            if phase == PHASE_STEADY:
                self._last_error = None

    def _reach_steady(self):
        """Convenience: declare the current target reached, no error."""
        self._advance(phase=PHASE_STEADY)

    def _fail(self, error):
        """Convenience: mark the current transition as failed."""
        self._advance(phase=PHASE_ERROR, error=error)

    # ── state read ────────────────────────────────────────────────

    def state_snapshot(self):
        """Atomic view of the state machine for status responses."""
        with self._state_lock:
            return {
                'target': self._target,
                'phase': self._phase,
                'step': self._step,
                'last_error': self._last_error,
                'phase_age_secs': round(time.monotonic() - self._phase_changed_at, 1),
            }
