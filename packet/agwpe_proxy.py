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


class _AGWPEProxyMixin:
    def _start_agwpe_proxy(self):
        """Start a local TCP proxy on 127.0.0.1:8010 → endpoint AGWPE port.
        Pat connects here; we forward to whichever endpoint is the packet radio."""
        import threading
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(('127.0.0.1', 8010))
            srv.listen(2)
            srv.settimeout(1.0)
            self._agwpe_proxy_sock = srv
            threading.Thread(target=self._agwpe_proxy_loop, daemon=True,
                             name="agwpe-proxy").start()
            print(f"  [Packet] AGWPE proxy listening on 127.0.0.1:8010")
        except OSError as e:
            print(f"  [Packet] AGWPE proxy failed to start: {e}")

    def _agwpe_proxy_loop(self):
        """Accept connections on local AGWPE proxy and forward to endpoint.

        If the endpoint isn't in data mode when Pat connects, automatically
        switches to data mode and waits for Direwolf's AGW port to open
        (up to 20 seconds).
        """
        srv = self._agwpe_proxy_sock
        while self._running and srv:
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._agwpe_proxy_handle,
                             args=(client, addr), daemon=True,
                             name="agwpe-proxy-conn").start()

    _AGWPE_MAX_SESSIONS = 10

    def _agwpe_proxy_handle(self, client, addr):
        """Handle one Pat → endpoint AGWPE forwarding session."""
        if self._proxy_sessions_active >= self._AGWPE_MAX_SESSIONS:
            print(f"  [Packet] AGWPE proxy: session cap reached "
                  f"({self._proxy_sessions_active}), rejecting {addr}")
            try:
                client.close()
            except Exception:
                pass
            return
        self._proxy_sessions_active += 1
        try:
            self._agwpe_proxy_session(client, addr)
        finally:
            self._proxy_sessions_active -= 1

    def _agwpe_proxy_session(self, client, addr):
        """Inner session handler — called from _agwpe_proxy_handle."""
        ep_ip = self._get_endpoint_ip()
        if not ep_ip:
            print(f"  [Packet] AGWPE proxy: no endpoint available, rejecting {addr}")
            client.close()
            return

        # Auto-switch to data mode if needed so Direwolf starts
        if self._mode == 'idle':
            print(f"  [Packet] AGWPE proxy: Pat connected, auto-switching to winlink mode")
            self._set_mode('winlink')

        # Retry connecting to endpoint AGW port while Direwolf starts (up to 20s)
        remote = None
        deadline = time.monotonic() + 20.0
        attempt = 0
        while time.monotonic() < deadline and self._running:
            attempt += 1
            try:
                ep_ip = self._get_endpoint_ip()  # re-resolve in case endpoint reconnected
                r = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                r.settimeout(2.0)
                r.connect((ep_ip, 8010))
                r.settimeout(None)  # clear connect timeout for data transfer
                remote = r
                break
            except Exception:
                if attempt == 1:
                    print(f"  [Packet] AGWPE proxy: waiting for Direwolf on {ep_ip}:8010...")
                time.sleep(1.0)

        if remote is None:
            print(f"  [Packet] AGWPE proxy: Direwolf not ready on {ep_ip}:8010 after 20s, rejecting")
            client.close()
            return

        print(f"  [Packet] AGWPE proxy: connected {addr} → {ep_ip}:8010 (attempt {attempt})")
        done = threading.Event()
        _t0 = time.monotonic()

        def _fwd(src, dst, label):
            _bytes = 0
            _frames = 0
            _last_t = _t0
            try:
                while self._running:
                    data = src.recv(4096)
                    if not data:
                        elapsed = time.monotonic() - _t0
                        print(f"  [Packet] AGWPE [{label}]: EOF after {_frames}f "
                              f"{_bytes}B {elapsed:.1f}s", flush=True)
                        break
                    now = time.monotonic()
                    gap = now - _last_t
                    _last_t = now
                    _bytes += len(data)
                    _frames += 1
                    elapsed = now - _t0
                    print(f"  [Packet] AGWPE [{label}]: #{_frames} {len(data)}B "
                          f"t={elapsed:.1f}s gap={gap:.1f}s", flush=True)
                    dst.sendall(data)
            except Exception as _e:
                elapsed = time.monotonic() - _t0
                print(f"  [Packet] AGWPE [{label}]: ERR after {_frames}f "
                      f"{_bytes}B {elapsed:.1f}s: {_e}", flush=True)
            finally:
                try: src.close()
                except: pass
                try: dst.close()
                except: pass
                done.set()

        threading.Thread(target=_fwd, args=(client, remote, 'pat→dw'),
                         daemon=True).start()
        threading.Thread(target=_fwd, args=(remote, client, 'dw→pat'),
                         daemon=True).start()

        # Block until session ends — keeps _proxy_sessions_active > 0 while live
        done.wait()
        elapsed = time.monotonic() - _t0
        print(f"  [Packet] AGWPE proxy: session ended after {elapsed:.1f}s "
              f"(active_sessions={self._proxy_sessions_active})", flush=True)

        if self._mode in ('winlink', 'bbs'):
            # If another session already started, skip Direwolf restart to avoid
            # disrupting the new active connection (counter still includes us here)
            if self._proxy_sessions_active > 1:
                print("  [Packet] AGWPE proxy: new session active — skipping restart")
            else:
                print("  [Packet] AGWPE proxy: restarting Direwolf for clean reconnect")
                self._send_endpoint_mode('data')

