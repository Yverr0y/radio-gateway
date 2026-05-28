"""Extracted from gateway_core.py during Phase 1.A.

Methods kept class-bound; the original code freely reads/writes self.*
attributes that are initialised in RadioGateway.__init__, so composing
back via inheritance keeps the runtime semantics identical without
threading attribute references through arguments.
"""

import collections
import json as json_mod
import math as _math_mod
import os
import queue as _queue_mod
import re
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
from audio_util import pcm_level, pcm_db


class _TransmitMixin:
    def _get_cross_clock_drift_ms(self):
        """Measure drift between main loop and BusManager clocks.

        Compares wall-clock timestamps of the most recent tick from each.
        Positive = BM ticked after main (BM lagging).
        """
        if not self.bus_manager:
            return 0.0
        bm_tick, bm_mono = self.bus_manager._bm_tick_mono
        if bm_tick == 0 or bm_mono == 0.0:
            return 0.0
        # Both clocks target 50ms ticks. Compare when they last ticked.
        main_mono = time.monotonic()  # we're inside the main tick right now
        return (main_mono - bm_mono) * 1000  # ms since BM last ticked

    def audio_transmit_loop(self):
        """Continuously capture audio from sources and send to Mumble via mixer"""
        # Elevate this thread to realtime scheduling so the 50ms tick isn't
        # delayed when the terminal window loses desktop focus.  Only this
        # thread needs it — it feeds both Mumble and the speaker callback.
        try:
            os.sched_setscheduler(0, os.SCHED_RR, os.sched_param(10))
            print("  Audio thread: SCHED_RR (realtime, priority 10)")
        except (PermissionError, OSError):
            try:
                os.nice(-10)
                print("  Audio thread: nice -10")
            except (PermissionError, OSError):
                pass  # best-effort
        if self.config.VERBOSE_LOGGING:
            print("✓ Audio transmit thread started (with mixer)")

        # ── GC control: disable automatic collection in this hot path ──
        import gc as _gc
        _gc.disable()
        self._gc_events_main = []  # GC pause records for trace
        def _gc_cb(phase, info):
            if phase == 'start':
                _gc_cb._t0 = time.monotonic()
            elif phase == 'stop' and hasattr(_gc_cb, '_t0'):
                dur_ms = (time.monotonic() - _gc_cb._t0) * 1000
                self._gc_events_main.append((time.monotonic(), info.get('generation', -1), dur_ms))
        _gc.callbacks.append(_gc_cb)
        print("  Audio thread: GC disabled, manual gen-0 every 5s")

        consecutive_errors = 0
        max_consecutive_errors = 10

        # 50ms self-clock: the main loop runs at this cadence regardless of
        # whether sources return data.  Sources are non-blocking; this tick
        # replaces the old pacing that was inside AIOCRadioSource.get_audio().
        _TICK = self.config.AUDIO_CHUNK_SIZE / self.config.AUDIO_RATE  # 0.05s
        _next_tick = time.monotonic()
        _prev_tick_time = time.monotonic()
        _trace = self._audio_trace  # local ref for speed
        _out_last_sample = 0  # output-side discontinuity tracking
        _out_disc = 0.0       # output-side sample jump at chunk boundary

        while self.running:
            self._tx_loop_tick += 1
            # ── 50ms self-clock ──────────────────────────────────────────────
            _now = time.monotonic()
            _slept = 0.0
            if _next_tick > _now:
                _slept = _next_tick - _now
                time.sleep(_slept)
            elif _now - _next_tick > _TICK:
                _next_tick = _now  # snap forward after stall
            _next_tick += _TICK
            _tick_start = time.monotonic()
            _tick_dt = (_tick_start - _prev_tick_time) * 1000  # ms since last tick
            _prev_tick_time = _tick_start

            # Trace defaults — overwritten inside the try body as we progress
            _tr_outcome = '?'
            _tr_mumble_ms = 0.0
            _tr_spk_ok = False
            _tr_spk_qd = -1
            _tr_data_rms = 0.0
            _tr_mixer_got = False
            _tr_mixer_ms = 0.0
            _tr_mixer_state = {}
            _tr_sdr_q = -1
            _tr_sdr_sb = -1
            _tr_sdr2_q = -1
            _tr_sdr2_sb = -1
            _tr_aioc_q = -1
            _tr_aioc_sb = -1
            _tr_sdr_prebuf = False
            _tr_sdr2_prebuf = False
            _out_disc = 0.0  # reset per tick
            _tr_rebro = ''  # rebroadcast state: ''=off, 'sig'=sending, 'hold'=PTT hold, 'idle'=on but no signal
            _tr_sv_ms = 0.0   # RemoteAudioServer send_audio cumulative time (ms)
            _tr_sv_sent = 0   # number of send_audio calls this tick
            active_sources = []

            try:
                # ── Apply pending PTT state change ───────────────────────────────
                # The keyboard thread queues PTT changes here instead of calling
                # set_ptt_state() directly.  Applying it now (between audio reads)
                # keeps the HID write off the USB bus while input_stream.read() is
                # blocking, eliminating the USB contention that causes an audio click.
                pending_ptt = self._pending_ptt_state
                if pending_ptt is not None:
                    self._pending_ptt_state = None
                    self.set_ptt_state(pending_ptt)
                    self._ptt_change_time = time.monotonic()  # Tell get_audio to fade in next chunk

                # AIOC stream health now handled by TH9800Plugin.check_watchdog()

                # Safety: clear announcement delay if its timer has expired.
                # This handles the case where stop_playback() is called during
                # the delay window — the PTT branch (which normally clears the
                # flag) never runs when ptt_required is False, so without this
                # check the flag stays True and the next announcement skips its
                # first chunk on load.
                if self.announcement_delay_active and time.time() >= self._announcement_ptt_delay_until:
                    self.announcement_delay_active = False

                # ── All bus ticks + sink delivery handled by BusManager ──
                # Main loop drains queues for SDR rebroadcast TX and WebSocket push.
                data = None  # no longer produced here; kept for trace compat
                if not self.bus_manager:
                    self.audio_capture_active = False
                    continue

                # Drain SDR rebroadcast queue (duckee_only_audio + ptt flag)
                sdr_only_audio, ptt_required = self.bus_manager.drain_sdr_rebroadcast()

                # Drain PCM/MP3 for WebSocket push
                _bm_pcm = self.bus_manager.drain_pcm()
                _bm_mp3 = self.bus_manager.drain_mp3()
                self._last_pcm_drain_n = getattr(self.bus_manager, '_last_pcm_drain_n', 0)

                # Read listen bus state for trace
                _lbid = getattr(self.bus_manager, '_listen_bus_id', None)
                if _lbid:
                    _tr_mixer_got = self.bus_manager._bus_levels.get(_lbid, 0) > 0
                    _tr_mixer_state = getattr(self, '_last_mixer_trace_state', {})
                # Populate trace source breakdown from active busses (non-zero level).
                # Under v2.0 bus routing the legacy per-source mixer list is empty,
                # so without this the trace shows SOURCE BREAKDOWN: (none) 100%.
                _bm_levels = getattr(self.bus_manager, '_bus_levels', None)
                if _bm_levels:
                    active_sources = [bid for bid, lvl in _bm_levels.items() if lvl > 0]

                # SDR rebroadcast: route SDR-only mix to AIOC radio TX
                if self.sdr_rebroadcast and not ptt_required and sdr_only_audio is not None:
                    sdr_has_signal = pcm_db(sdr_only_audio) > -50.3  # was rms > 100

                    if sdr_has_signal:
                        self._rebroadcast_ptt_hold_until = time.monotonic() + self.config.SDR_REBROADCAST_PTT_HOLD
                        self._rebroadcast_sending = True
                        self.last_sound_time = time.time()
                    else:
                        self._rebroadcast_sending = False

                    rebroadcast_ptt_needed = time.monotonic() < self._rebroadcast_ptt_hold_until

                    if rebroadcast_ptt_needed:
                        self.last_sound_time = time.time()

                        if not self._rebroadcast_ptt_active and not self.tx_muted and not self.manual_ptt_mode:
                            self.set_ptt_state(True)
                            self._ptt_change_time = time.monotonic()
                            self._rebroadcast_ptt_active = True
                            if self.radio_source:
                                self.radio_source.enabled = False
                            self._trace_events.append((time.monotonic(), 'rebro_ptt', 'on'))

                        pcm = sdr_only_audio if sdr_has_signal else b'\x00' * len(sdr_only_audio)
                        if self.output_stream and not self.tx_muted:
                            if self.config.OUTPUT_VOLUME != 1.0:
                                arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                                pcm = np.clip(arr * self.config.OUTPUT_VOLUME, -32768, 32767).astype(np.int16).tobytes()
                            try:
                                self.output_stream.write(pcm, exception_on_overflow=False)
                            except TypeError:
                                self.output_stream.write(pcm)

                        tx_level_pcm = pcm if sdr_has_signal else sdr_only_audio
                        self.rx_audio_level = pcm_level(tx_level_pcm, self.rx_audio_level)
                        self.last_rx_audio_time = time.time()

                        _tr_rebro = 'sig' if sdr_has_signal else 'hold'
                    else:
                        if self._rebroadcast_ptt_active and self.ptt_active:
                            self.set_ptt_state(False)
                            self._ptt_change_time = time.monotonic()
                            self._rebroadcast_ptt_active = False
                            if self.radio_source:
                                self.radio_source.enabled = True
                            self._trace_events.append((time.monotonic(), 'rebro_ptt', 'off'))
                        self._rebroadcast_sending = False
                        _tr_rebro = 'idle'
                elif self.sdr_rebroadcast and not ptt_required and sdr_only_audio is None:
                    self._rebroadcast_sending = False
                    if time.monotonic() >= self._rebroadcast_ptt_hold_until:
                        if self._rebroadcast_ptt_active and self.ptt_active:
                            self.set_ptt_state(False)
                            self._ptt_change_time = time.monotonic()
                            self._rebroadcast_ptt_active = False
                            if self.radio_source:
                                self.radio_source.enabled = True
                            self._trace_events.append((time.monotonic(), 'rebro_ptt', 'off'))
                        _tr_rebro = 'idle'
                    else:
                        _tr_rebro = 'hold'

                # WebSocket PCM push — now done directly from bus tick thread
                # Main loop drain kept for level metering only, no WS push
                if _bm_pcm is not None:
                    if self._stream_trace and self._stream_trace.active:
                        self._stream_trace.record('pcm_ws', 'drain_only', _bm_pcm,
                                                  self._last_pcm_drain_n)

                # MP3 stream push
                if _bm_mp3 is not None:
                    if self.web_config_server and self.web_config_server._stream_subscribers:
                        self.web_config_server.push_audio(_bm_mp3)

                consecutive_errors = 0
                _tr_outcome = 'bus_ok'

            except Exception as e:
                consecutive_errors += 1
                self.audio_capture_active = False
                _tr_outcome = 'exception'

                error_type = type(e).__name__
                error_msg = str(e)

                # Always log first occurrence of each error burst
                if consecutive_errors <= 2:
                    print(f"  [MainLoop] Exception #{consecutive_errors}: {error_type}: {error_msg}")
                    if consecutive_errors == 1:
                        import traceback; traceback.print_exc()

                self.last_stream_error = f"{error_type}: {error_msg}"

                if consecutive_errors >= max_consecutive_errors:
                    # Stream lifecycle is managed by TH9800Plugin reader thread.
                    # Just reset the counter — don't call the old restart_audio_input().
                    consecutive_errors = 0
                # else: self-clock at top of loop handles pacing
            finally:
                # ── Trace record (toggled by 'i' key) ──
                if self._trace_recording:
                    # Snapshot enhanced instrumentation from sources
                    _sdr1_disc = 0.0
                    _sdr1_sb_after = -1
                    _sdr1_cb_ovf = 0
                    _sdr1_cb_drop = 0
                    _aioc_disc = 0.0
                    _aioc_sb_after = -1
                    _aioc_cb_ovf = 0
                    _aioc_cb_drop = 0
                    # KV4P trace counters used to be filled by the in-core
                    # plugin's per-tick instrumentation. The endpoint plugin
                    # no longer reports these to the gateway, so the columns
                    # stay zero — keep the slot to preserve the CSV layout.
                    _kv4p_snap = {}

                    _trace.append((
                        _tick_start - self._audio_trace_t0,  # 0: time (s)
                        _tick_dt,                             # 1: tick interval (ms)
                        _tr_sdr_q,                            # 2: SDR1 queue depth before
                        _tr_sdr_sb,                           # 3: SDR1 sub-buffer bytes before
                        _tr_aioc_q,                           # 4: AIOC queue depth before
                        _tr_aioc_sb,                          # 5: AIOC sub-buffer bytes before
                        _tr_mixer_got,                        # 6: mixer returned audio?
                        ','.join(active_sources) if active_sources else '',  # 7: active sources
                        _tr_mixer_ms,                         # 8: mixer call duration (ms)
                        0.0,  # 9: SDR blocked (ms)
                        0.0,  # 10: AIOC blocked (ms) — legacy field, always 0
                        _tr_outcome,                          # 11: outcome (sent/no_mumble/no_sndout/no_codec/ptt/exception)
                        _tr_mumble_ms,                        # 12: Mumble add_sound time (ms)
                        _tr_spk_ok,                           # 13: speaker enqueue attempted?
                        _tr_spk_qd,                           # 14: speaker queue depth before enqueue
                        _tr_data_rms,                         # 15: RMS of data sent
                        len(data) if data else 0,              # 16: data length (bytes)
                        _tr_mixer_state,                       # 17: mixer internal state dict
                        _tr_sdr2_q,                           # 18: SDR2 queue depth before
                        _tr_sdr2_sb,                          # 19: SDR2 sub-buffer bytes before
                        _tr_sdr_prebuf,                       # 20: SDR1 _prebuffering flag
                        _tr_sdr2_prebuf,                      # 21: SDR2 _prebuffering flag
                        _tr_rebro,                            # 22: rebroadcast state (''=off, sig/hold/idle)
                        _tr_sv_ms,                            # 23: RemoteAudioServer send_audio time (ms)
                        _tr_sv_sent,                          # 24: number of SV send_audio calls this tick
                        # === Enhanced instrumentation (25+) ===
                        _sdr1_disc,                           # 25: SDR1 sample discontinuity (abs delta)
                        _sdr1_sb_after,                       # 26: SDR1 sub-buffer bytes AFTER serve
                        _sdr1_cb_ovf,                         # 27: SDR1 cumulative callback overflow count
                        _sdr1_cb_drop,                        # 28: SDR1 cumulative callback queue drops
                        _aioc_disc,                           # 29: AIOC sample discontinuity (abs delta)
                        _aioc_sb_after,                       # 30: AIOC sub-buffer bytes AFTER serve
                        _aioc_cb_ovf,                         # 31: AIOC cumulative callback overflow count
                        _aioc_cb_drop,                        # 32: AIOC cumulative callback queue drops
                        _out_disc,                            # 33: output-side sample discontinuity (mixer output)
                        # === KV4P trace fields (34+) ===
                        _kv4p_snap.get('rx_frames', 0),       # 34: KV4P Opus frames received this tick
                        _kv4p_snap.get('rx_bytes', 0),        # 35: KV4P Opus bytes received this tick
                        _kv4p_snap.get('queue_drops', 0),     # 36: KV4P queue overflow drops this tick
                        _kv4p_snap.get('sub_buf_before', 0),  # 37: KV4P sub_buffer bytes before get_audio
                        _kv4p_snap.get('sub_buf_after', 0),   # 38: KV4P sub_buffer bytes after get_audio
                        _kv4p_snap.get('returned_data', False),  # 39: KV4P returned audio this tick?
                        _kv4p_snap.get('pcm_rms', 0.0),      # 40: KV4P output PCM RMS
                        _kv4p_snap.get('queue_len', 0),       # 41: KV4P queue length at snapshot
                        _kv4p_snap.get('decode_errors', 0),   # 42: KV4P Opus decode errors this tick
                        # === KV4P TX trace fields (43+) ===
                        _kv4p_snap.get('tx_frames', 0),       # 43: Opus frames encoded+sent to radio
                        _kv4p_snap.get('tx_dropped', 0),      # 44: PCM bytes dropped (partial-frame remainder)
                        _kv4p_snap.get('tx_input_rms', 0.0),  # 45: RMS of PCM fed to encoder
                        _kv4p_snap.get('tx_errors', 0),       # 46: encoder exceptions
                        self.announcement_delay_active and not (
                            (str(getattr(self.config, 'TX_RADIO', '')).lower() == 'kv4p'
                             or str(getattr(self.config, 'TX_RADIO', '')).lower().startswith('kv4p-'))
                            and bool(self.kv4p_plugin)),  # 47: TX to KV4P silenced by PTT settle delay (False when TX_RADIO=kv4p*, fix in place)
                        0.0,  # 48: SDR2 sample discontinuity (abs delta)
                        -1,  # 49: SDR2 sub-buffer bytes after serve
                        # === Audio quality diagnostics (50+) ===
                        getattr(self, '_spk_drop_count', 0),        # 50: speaker queue drops this tick
                        getattr(self, '_last_pcm_drain_n', 0),      # 51: PCM drain chunk count (1=good, 2+=drift)
                        self._get_cross_clock_drift_ms(),            # 52: BusManager cross-clock drift (ms)
                        len(self._gc_events_main),                   # 53: cumulative GC events (main loop)
                    ))
                # Reset per-tick counters
                self._spk_drop_count = 0
                self._last_pcm_drain_n = 0
                # Manual GC: gen-0 only, every 100 ticks (~5s), during sleep window
                if self._tx_loop_tick % 100 == 0:
                    _gc.collect(0)

