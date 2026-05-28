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


class _PatMixin:
    def _start_pat(self):
        """Start Pat Winlink client via the gateway's ProcessSupervisor."""
        import shutil
        pat_bin = shutil.which('pat')
        if not pat_bin:
            print("  [Packet] Pat not found — install from https://getpat.io/")
            return False
        sup = getattr(self._gateway, 'process_supervisor', None) if self._gateway else None
        if not sup:
            print("  [Packet] No process supervisor — cannot start pat")
            return False
        try:
            sup.add('pat', [pat_bin, 'http'], restart=True, backoff=(2, 30))
            print(f"  [Packet] Pat supervised (web UI on port {self._pat_port})")
            return True
        except ValueError:
            sup.restart('pat')
            return True
        except Exception as e:
            print(f"  [Packet] Pat start failed: {e}")
            return False

    def _stop_pat(self):
        """Stop the supervised pat process (gateway shutdown also reaps it)."""
        sup = getattr(self._gateway, 'process_supervisor', None) if self._gateway else None
        if not sup:
            return
        try:
            sup.stop('pat')
            print("  [Packet] Pat stopped")
        except KeyError:
            pass
        except Exception as e:
            print(f"  [Packet] Pat stop error: {e}")
    def _is_pat_running(self):
        """Ask the supervisor whether pat is alive."""
        sup = getattr(self._gateway, 'process_supervisor', None) if self._gateway else None
        if not sup:
            return False
        try:
            return bool(sup.status('pat').get('pid'))
        except KeyError:
            return False
        except Exception:
            return False
    def _delayed_pat_start(self):
        """Wait for Direwolf to be ready then start Pat HTTP server.
        Tests the remote KISS port (not the local AGWPE proxy) to avoid
        opening a spurious AGWPE session that would trigger a Direwolf restart."""
        import socket as _sock
        if self._is_pat_running():
            return  # already running
        ep_ip = self._get_endpoint_ip()
        if ep_ip:
            for _attempt in range(15):
                time.sleep(1)
                try:
                    s = _sock.socket()
                    s.settimeout(2)
                    s.connect((ep_ip, self._kiss_port))
                    s.close()
                    break
                except Exception:
                    continue
        self._start_pat()
