"""Extracted from packet_radio.py during Phase 2.D.

Class-bound mixin. The original code freely reads ``self.*`` state set in
``PacketRadioPlugin.__init__``; composing the mixins back via inheritance
preserves the runtime semantics without threading state through arguments.
"""

import collections
import math
import re
import socket
import threading
import time


class _ModeMixin:
    def _tnc_status_fields(self):
        """Pull direwolf state from gateway.packet_tnc for status dicts."""
        tnc = getattr(self._gateway, 'packet_tnc', None) if self._gateway else None
        if not tnc:
            return {"dw_audio_level": 0, "dw_audio_peak": 0, "log_tail": []}
        s = tnc.status()
        return {
            "dw_audio_level": s.get('audio_level', 0),
            "dw_audio_peak": s.get('audio_peak', 0),
            "log_tail": s.get('log_tail', []),
        }

    def _set_mode(self, mode):
        """Switch TNC mode — tells endpoint to start/stop Direwolf.

        States: 'idle' | 'aprs' | 'winlink' | 'bbs'.

        Endpoint-side responsibilities:
          * AIOC-style endpoints: gateway owns Direwolf locally (the
            endpoint's audio is fed across the link to the gateway box).
          * IC-7100-style endpoints: endpoint runs Direwolf where the
            USB codec lives; gateway just connects KISS over the network.
            ``packet_local_tnc`` capability is the discriminator.
        """
        if mode not in ('idle', 'aprs', 'winlink', 'bbs'):
            return {"ok": False, "error": f"invalid mode: {mode}"}
        if mode == self._mode:
            return {"ok": True, "mode": self._mode}

        print(f"  [Packet] Mode: {self._mode} -> {mode}")

        # Disconnect current KISS regardless of next state.
        self._disconnect_kiss()
        self._mode = mode

        # Compute once — was called twice with risk of drifting answers if
        # the endpoint set changes mid-call.
        endpoint_owns_tnc = self._endpoint_has_local_tnc()
        gw_tnc = getattr(self._gateway, 'packet_tnc', None) if not endpoint_owns_tnc else None

        if mode == 'idle':
            self._stop_pat()
            # Stop gateway-side direwolf BEFORE releasing the endpoint's
            # ALSA — the order matters so direwolf doesn't briefly clutch
            # at a soon-to-be-shared device. Endpoint-owned TNCs (IC-7100)
            # reap their own direwolf when we tell them to go to audio.
            if gw_tnc is not None:
                gw_tnc.stop()
            if not self._send_endpoint_mode('audio'):
                return {"ok": False, "mode": "idle",
                        "warning": "mode set to idle but failed to send audio command to endpoint"}
            return {"ok": True, "mode": "idle"}

        if not self._find_endpoint() and not self._remote_tnc:
            self._mode = 'idle'
            return {"ok": False, "error": "No AIOC endpoint connected for packet radio"}

        # Tell endpoint to release its ALSA stream (data mode = no arecord/aplay)
        if not self._send_endpoint_mode('data'):
            self._mode = 'idle'
            return {"ok": False, "error": "failed to send data mode command to endpoint"}
        # Start gateway-side direwolf only for endpoints we own the TNC for.
        if gw_tnc is not None:
            gw_tnc.start(callsign=self._callsign, ssid=self._ssid,
                         modem=self._modem_rate, kiss_port=self._kiss_port,
                         ptt_channel=int(getattr(self._config, 'AIOC_PTT_CHANNEL', 3)))
        # Connect KISS TCP to remote Direwolf in the background.
        # Failure surfaces only in the journal — wiring this into a real
        # state machine is a future cleanup (see plan).
        threading.Thread(target=self._kiss_connect_loop, daemon=True,
                         name="KISSConnect").start()
        # Start Pat HTTP server so the web UI is available.
        if mode == 'winlink':
            threading.Thread(target=self._delayed_pat_start, daemon=True,
                             name="PatStart").start()
        return {"ok": True, "mode": mode}

