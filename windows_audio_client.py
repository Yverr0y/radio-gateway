#!/usr/bin/env python3
"""Windows Audio Client for Radio Gateway — Full Duplex.

Runs both directions simultaneously:
  TX: Captures audio from a local input device and sends it to the gateway
      via TCP (connects out to gateway's REMOTE_AUDIO_RX_PORT, default 9602).
  RX: Listens on a local port for the gateway to connect in and push audio,
      then plays it on a local output device (gateway connects from port 9600).

Protocol: length-prefixed PCM — [4-byte big-endian uint32 length][PCM payload]
Audio: 48000 Hz, mono, 16-bit signed little-endian PCM, 2400 frames per chunk.

Keyboard controls:
  l = Toggle TX ON/MUTE (mic capture)
  p = Toggle RX PLAY/MUTE (speaker output)
  , (or <) = TX volume down 5%
  . (or >) = TX volume up 5%
  [ = RX volume down 5%
  ] = RX volume up 5%

Usage:
    pip install sounddevice numpy
    python windows_audio_client.py [gateway_host]

On first run the script will prompt for audio devices and gateway host,
then save the selection to windows_audio_client.json alongside this script.
"""

import json
import math
import os
import socket
import struct
import sys
import threading
import time
import wave
from datetime import datetime

try:
    import sounddevice as sd
except ImportError:
    print("sounddevice is required.  Install it with:  python -m pip install sounddevice")
    sys.exit(1)

import numpy as np

# Optional resampler backends (used only when capture device's native rate != 48k)
_RESAMPLER = None
try:
    import soxr as _soxr  # best: stateful streaming resampler
    _RESAMPLER = "soxr"
except ImportError:
    try:
        from scipy import signal as _scipy_signal  # fallback: per-chunk polyphase
        _RESAMPLER = "scipy"
    except ImportError:
        _RESAMPLER = None

# ---------------------------------------------------------------------------
# Constants — must match gateway defaults
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48000
CHANNELS = 1
FRAMES_PER_BUFFER = 2400  # 2400 frames x 2 bytes = 4800 bytes per chunk
RECONNECT_INTERVAL = 5  # seconds between connection attempts
SILENCE = b'\x00' * (FRAMES_PER_BUFFER * 2)  # 4800 bytes of silence

DEFAULT_TX_PORT = 9602  # Gateway's RX listen port (we connect out to this)
DEFAULT_RX_PORT = 9600  # Local listen port (gateway connects in on this)

# ANSI colors
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
WHITE = "\033[97m"
GRAY = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

CONFIG_FILENAME = "windows_audio_client.json"

# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------
def _config_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config():
    path = _config_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(cfg):
    path = _config_path()
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)

# ---------------------------------------------------------------------------
# Keyboard input (cross-platform)
# ---------------------------------------------------------------------------
def _keyboard_listener(state):
    """Background thread: read single keypresses and update shared state."""
    try:
        # Windows
        import msvcrt
        while state["running"]:
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                try:
                    ch = ch.decode("utf-8", errors="ignore").lower()
                except Exception:
                    ch = ""
                _handle_key(ch, state)
            time.sleep(0.05)
    except ImportError:
        # Unix / Linux / macOS
        import tty
        import termios
        import select
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while state["running"]:
                if select.select([sys.stdin], [], [], 0.05)[0]:
                    ch = sys.stdin.read(1).lower()
                    _handle_key(ch, state)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _handle_key(ch, state):
    if ch == "l":
        state["tx_live"] = not state["tx_live"]
    elif ch == "p":
        state["rx_play"] = not state["rx_play"]
    elif ch in (",", "<"):
        state["tx_vol"] = max(0, state["tx_vol"] - 5)
    elif ch in (".", ">"):
        state["tx_vol"] = min(100, state["tx_vol"] + 5)
    elif ch == "[":
        state["rx_vol"] = max(0, state["rx_vol"] - 5)
    elif ch == "]":
        state["rx_vol"] = min(100, state["rx_vol"] + 5)
    elif ch == "r":
        state["tx_resample_enabled"] = not state.get("tx_resample_enabled", True)
    elif ch == "w":
        # toggle WAV dump of the post-processed (48 kHz mono) TX stream
        state["tx_wav_request"] = not state.get("tx_wav_request", False)
    elif ch == "d":
        # toggle expanded diagnostics
        state["show_diag"] = not state.get("show_diag", False)

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------
def list_input_devices():
    devices = []
    for d in sd.query_devices():
        if d["max_input_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_input_channels"]))
    return devices


def list_output_devices():
    devices = []
    for d in sd.query_devices():
        if d["max_output_channels"] > 0:
            devices.append((d["index"], d["name"], d["max_output_channels"]))
    return devices


def find_device_by_name(name, output=False):
    for d in sd.query_devices():
        if output:
            if d["max_output_channels"] > 0 and d["name"] == name:
                return d["index"]
        else:
            if d["max_input_channels"] > 0 and d["name"] == name:
                return d["index"]
    return None


def choose_input_device(cfg):
    saved_name = cfg.get("tx_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=False)
        if idx is not None:
            return idx, saved_name
        print(f"Saved input device not found: {saved_name}")

    devices = list_input_devices()
    if not devices:
        print("No input devices found.")
        sys.exit(1)

    print("\nAvailable input devices (TX mic):")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nSelect device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")


def choose_output_device(cfg):
    saved_name = cfg.get("rx_device_name")
    if saved_name:
        idx = find_device_by_name(saved_name, output=True)
        if idx is not None:
            return idx, saved_name
        print(f"Saved output device not found: {saved_name}")

    devices = list_output_devices()
    if not devices:
        print("No output devices found.")
        sys.exit(1)

    print("\nAvailable output devices (RX speaker):")
    for n, (idx, name, ch) in enumerate(devices, 1):
        print(f"  {n}) {name}  (index {idx}, {ch}ch)")

    while True:
        try:
            choice = int(input("\nSelect device number: "))
            if 1 <= choice <= len(devices):
                idx, name, _ = devices[choice - 1]
                return idx, name
        except (ValueError, EOFError):
            pass
        print("Invalid selection, try again.")

# ---------------------------------------------------------------------------
# Level meter
# ---------------------------------------------------------------------------
def rms_db(pcm_bytes):
    n_samples = len(pcm_bytes) // 2
    if n_samples == 0:
        return -100.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    rms = np.sqrt(np.mean(samples * samples))
    if rms < 1:
        return -100.0
    return 20.0 * math.log10(rms / 32768.0)


def level_bar(db, width=20, vol_pct=100):
    vol_frac = max(0.0, min(1.0, vol_pct / 100.0))
    if vol_frac > 0:
        scaled_db = db + 20.0 * math.log10(vol_frac)
    else:
        scaled_db = -100.0
    clamped = max(-60.0, min(0.0, scaled_db))
    filled = int((clamped + 60.0) / 60.0 * width)
    marker_pos = int(vol_frac * width)
    marker_pos = max(0, min(width, marker_pos))
    bar_chars = []
    for i in range(width):
        if i == marker_pos and marker_pos < width:
            bar_chars.append("|")
        elif i < filled:
            bar_chars.append("#")
        else:
            bar_chars.append("-")
    return "".join(bar_chars), scaled_db

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------
def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)

# ---------------------------------------------------------------------------
# TX thread — capture mic and send to gateway
# ---------------------------------------------------------------------------
def _tx_thread_func(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name):
    """Capture mic audio and send to gateway's RX port.

    Captures at the device's native rate / channel count, then downmixes to
    mono and resamples to 48 kHz before sending. Resampling can be toggled
    off at runtime with the 'r' key (for A/B diagnosis).
    """
    import queue
    tx_q = queue.Queue(maxsize=32)

    # --- Determine native capture parameters --------------------------------
    try:
        dev_info = sd.query_devices(in_dev_index)
        capture_rate = int(round(float(dev_info["default_samplerate"]))) or SAMPLE_RATE
        max_in_ch = int(dev_info["max_input_channels"])
    except Exception:
        capture_rate = SAMPLE_RATE
        max_in_ch = 1
    capture_channels = 2 if max_in_ch >= 2 else 1
    needs_resample = (capture_rate != SAMPLE_RATE)

    # --- Set up resampler ---------------------------------------------------
    resampler = None
    resample_backend = "none"
    if needs_resample:
        if _RESAMPLER == "soxr":
            try:
                resampler = _soxr.ResampleStream(
                    capture_rate, SAMPLE_RATE, 1,
                    dtype="int16", quality="HQ",
                )
                resample_backend = "soxr"
            except Exception as e:
                print(f"\n  soxr init failed: {e}")
                resampler = None
        if resampler is None and _RESAMPLER in ("soxr", "scipy"):
            # fall back to scipy polyphase
            try:
                from math import gcd
                g = gcd(capture_rate, SAMPLE_RATE)
                resampler = ("scipy", SAMPLE_RATE // g, capture_rate // g)
                resample_backend = "scipy"
            except Exception:
                resampler = None
        if resampler is None:
            print(f"\n  WARNING: capture device runs at {capture_rate} Hz, gateway needs 48000 Hz.")
            print("  No resampler available. Install one with:  pip install soxr")
            print("  Audio will sound choppy until resampling is available.\n")
            resample_backend = "missing"

    # Initialise resampling state (used by display + 'r' key toggle)
    state["tx_capture_rate"] = float(capture_rate)
    state["tx_capture_channels"] = capture_channels
    state["tx_needs_resample"] = needs_resample
    state["tx_resample_backend"] = resample_backend
    state.setdefault("tx_resample_enabled", True)
    state.setdefault("tx_xruns", 0)
    state.setdefault("tx_last_frames", 0)

    # --- Stream setup -------------------------------------------------------
    # ~50 ms blocks at the capture rate
    capture_block = max(1, int(round(capture_rate * FRAMES_PER_BUFFER / SAMPLE_RATE)))

    # Callback statistics (always visible in GUI)
    cb_stats = {
        "xruns": 0,
        "callbacks": 0,
        "total_frames": 0,
        "last_frames": 0,
        "last_status": "",
        "last_ts": None,
        "gap_min": None,
        "gap_max": None,
        "gap_sum": 0.0,
        "gap_count": 0,
        "frames_hist": {},   # frames -> count
        "started": time.monotonic(),
    }

    def _tx_callback(indata, frames, time_info, status):
        """Called by sounddevice from audio thread — push PCM to queue."""
        now = time.monotonic()
        if status:
            cb_stats["xruns"] += 1
            cb_stats["last_status"] = str(status)
        if cb_stats["last_ts"] is not None:
            gap = now - cb_stats["last_ts"]
            if cb_stats["gap_min"] is None or gap < cb_stats["gap_min"]:
                cb_stats["gap_min"] = gap
            if cb_stats["gap_max"] is None or gap > cb_stats["gap_max"]:
                cb_stats["gap_max"] = gap
            cb_stats["gap_sum"] += gap
            cb_stats["gap_count"] += 1
        cb_stats["last_ts"] = now
        cb_stats["callbacks"] += 1
        cb_stats["total_frames"] += frames
        cb_stats["last_frames"] = frames
        h = cb_stats["frames_hist"]
        h[frames] = h.get(frames, 0) + 1
        # Mirror into shared state for display thread
        state["tx_xruns"] = cb_stats["xruns"]
        state["tx_last_frames"] = frames
        state["tx_cb_stats"] = cb_stats
        try:
            tx_q.put_nowait(bytes(indata))
        except queue.Full:
            cb_stats["q_drops"] = cb_stats.get("q_drops", 0) + 1
            state["tx_q_drops"] = cb_stats["q_drops"]

    stream = sd.RawInputStream(
        samplerate=capture_rate,
        blocksize=capture_block,
        device=in_dev_index,
        channels=capture_channels,
        dtype="int16",
        callback=_tx_callback,
    )
    stream.start()
    try:
        state["tx_stream_rate"] = float(stream.samplerate)
    except Exception:
        state["tx_stream_rate"] = float(capture_rate)
    state["tx_device_rate"] = float(capture_rate)

    # One-time diagnostic dump (written to stderr so it survives the display
    # thread's screen-clears in the scrollback)
    try:
        sys.stderr.write(
            f"[tx] device={in_dev_name!r}\n"
            f"[tx] requested: rate={capture_rate} channels={capture_channels} "
            f"blocksize={capture_block}\n"
            f"[tx] actual stream rate={stream.samplerate} latency={stream.latency}\n"
            f"[tx] resample: needed={needs_resample} backend={resample_backend} "
            f"enabled={state.get('tx_resample_enabled', True)}\n"
        )
        sys.stderr.flush()
    except Exception:
        pass

    sock = None
    wav_writer = None

    def connect():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((gateway_host, tx_port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            return s
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            return None

    last_connect_attempt = 0.0
    try:
        while state["running"]:
            # Non-blocking connect / reconnect: try every RECONNECT_INTERVAL
            # without blocking the audio-processing path, so the level meter
            # keeps updating even when the gateway is down.
            if sock is None:
                state["tx_connected"] = False
                if time.monotonic() - last_connect_attempt >= RECONNECT_INTERVAL:
                    last_connect_attempt = time.monotonic()
                    sock = connect()
                    if sock is not None:
                        state["tx_connected"] = True

            # Get audio from callback queue (ALWAYS — regardless of socket)
            try:
                pcm = tx_q.get(timeout=0.1)
            except queue.Empty:
                continue

            # Snapshot queue depth right after the get, so the GUI shows how
            # much the loop is behind.
            state["tx_q_depth"] = tx_q.qsize()
            iter_start = time.monotonic()

            try:
                # Decode → numpy
                samples = np.frombuffer(pcm, dtype=np.int16)

                # Downmix stereo → mono (avoid int16 overflow via int32)
                if capture_channels == 2:
                    samples = (samples.reshape(-1, 2).astype(np.int32)
                               .mean(axis=1).astype(np.int16))

                # Resample to 48 kHz if needed and currently enabled
                if needs_resample and state.get("tx_resample_enabled", True) and resampler is not None:
                    if resample_backend == "soxr":
                        out = resampler.resample_chunk(samples)
                        if out is None or out.size == 0:
                            continue  # filter warm-up
                        samples = np.ascontiguousarray(out, dtype=np.int16)
                    else:  # scipy polyphase per chunk
                        _, up, down = resampler
                        out = _scipy_signal.resample_poly(
                            samples.astype(np.float32), up, down,
                        )
                        samples = np.clip(out, -32768, 32767).astype(np.int16)

                # Apply volume
                vol = state["tx_vol"]
                if vol < 100:
                    fs = samples.astype(np.float32) * (vol / 100.0)
                    samples = np.clip(fs, -32768, 32767).astype(np.int16)

                pcm = samples.tobytes()
            except Exception as e:
                print(f"\n  TX processing error: {type(e).__name__}: {e}")
                continue

            # WAV dump of post-processed audio (toggled with 'w'). The header
            # rate must match the data rate: 48 kHz when resampling is on,
            # native capture_rate when it's off (so the file plays at the
            # correct speed in either mode).
            want_wav = state.get("tx_wav_request", False)
            if want_wav and wav_writer is None:
                resampling_now = (needs_resample
                                  and state.get("tx_resample_enabled", True)
                                  and resampler is not None)
                wav_rate = SAMPLE_RATE if resampling_now else capture_rate
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                wav_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    f"tx_dump_{wav_rate}_{ts}.wav",
                )
                try:
                    wav_writer = wave.open(wav_path, "wb")
                    wav_writer.setnchannels(1)
                    wav_writer.setsampwidth(2)
                    wav_writer.setframerate(wav_rate)
                    state["tx_wav_path"] = wav_path
                    state["tx_wav_bytes"] = 0
                    state["tx_wav_rate"] = wav_rate
                except Exception as e:
                    print(f"\n  Failed to open WAV file: {e}")
                    wav_writer = None
                    state["tx_wav_request"] = False
            elif not want_wav and wav_writer is not None:
                try:
                    wav_writer.close()
                except Exception:
                    pass
                wav_writer = None
                state["tx_wav_path"] = None
            if wav_writer is not None:
                try:
                    wav_writer.writeframesraw(pcm)
                    state["tx_wav_bytes"] = state.get("tx_wav_bytes", 0) + len(pcm)
                except Exception:
                    pass

            # Compute level for display (always — even when offline)
            state["tx_db"] = rms_db(pcm)

            t_before_send = time.monotonic()

            # Send if connected; otherwise quietly drop and keep going so
            # the meter / WAV dump still work while waiting on the gateway.
            if sock is not None:
                send_pcm = pcm if state["tx_live"] else SILENCE
                try:
                    header = struct.pack(">I", len(send_pcm))
                    sock.sendall(header + send_pcm)
                except Exception:
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock = None

            now = time.monotonic()
            state["tx_iter_ms"] = (now - iter_start) * 1000.0
            state["tx_send_ms"] = (now - t_before_send) * 1000.0
    except Exception as e:
        import traceback
        print(f"\n  TX thread FATAL: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        stream.stop()
        stream.close()
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if wav_writer is not None:
            try:
                wav_writer.close()
            except Exception:
                pass
        state["tx_connected"] = False

# ---------------------------------------------------------------------------
# RX thread — receive audio from gateway and play
# ---------------------------------------------------------------------------
def _rx_thread_func(state, cfg, rx_port, out_dev_index, out_dev_name):
    """Listen for gateway connection and play received audio."""
    listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listen_sock.bind(("0.0.0.0", rx_port))
    except OSError as e:
        print(f"\n  Port {rx_port} already in use — is another instance running? ({e})")
        return
    listen_sock.listen(1)
    listen_sock.settimeout(1.0)

    try:
        while state["running"]:
            # Accept a connection
            try:
                conn, addr = listen_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(0.2)
            state["rx_connected"] = True
            state["rx_from"] = f"{addr[0]}:{addr[1]}"

            import queue as _queue
            rx_q = _queue.Queue(maxsize=32)

            def _rx_callback(outdata, frames, time_info, status):
                """Called by sounddevice from audio thread — pull PCM from queue."""
                try:
                    pcm = rx_q.get_nowait()
                    expected = frames * CHANNELS * 2  # 16-bit
                    if len(pcm) >= expected:
                        outdata[:] = pcm[:expected]
                    else:
                        outdata[:len(pcm)] = pcm
                        outdata[len(pcm):] = b'\x00' * (expected - len(pcm))
                except _queue.Empty:
                    outdata[:] = b'\x00' * len(outdata)

            out_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=FRAMES_PER_BUFFER,
                device=out_dev_index,
                channels=CHANNELS,
                dtype="int16",
                callback=_rx_callback,
            )
            out_stream.start()
            try:
                state["rx_stream_rate"] = float(out_stream.samplerate)
            except Exception:
                state["rx_stream_rate"] = float(SAMPLE_RATE)
            try:
                state["rx_device_rate"] = float(sd.query_devices(out_dev_index)["default_samplerate"])
            except Exception:
                state["rx_device_rate"] = 0.0

            try:
                while state["running"]:
                    try:
                        hdr = _recv_exact(conn, 4)
                    except socket.timeout:
                        continue
                    if hdr is None:
                        break
                    length = struct.unpack(">I", hdr)[0]
                    if length == 0 or length > 960000:
                        break

                    conn.settimeout(None)
                    pcm = _recv_exact(conn, length)
                    conn.settimeout(0.2)
                    if pcm is None:
                        break

                    state["rx_db"] = rms_db(pcm)

                    if state["rx_play"]:
                        vol = state["rx_vol"]
                        if vol < 100:
                            samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                            samples *= vol / 100.0
                            pcm_out = np.clip(samples, -32768, 32767).astype(np.int16).tobytes()
                        else:
                            pcm_out = pcm
                        try:
                            rx_q.put_nowait(pcm_out)
                        except _queue.Full:
                            try:
                                rx_q.get_nowait()
                            except _queue.Empty:
                                pass
                            try:
                                rx_q.put_nowait(pcm_out)
                            except _queue.Full:
                                pass

            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                out_stream.stop()
                out_stream.close()
                try:
                    conn.close()
                except Exception:
                    pass
                state["rx_connected"] = False
                state["rx_from"] = None
                state["rx_stream_rate"] = 0.0
                state["rx_device_rate"] = 0.0
    finally:
        listen_sock.close()

# ---------------------------------------------------------------------------
# Display thread — update status line
# ---------------------------------------------------------------------------
def _display_thread_func(state, gateway_host, tx_port, rx_port,
                         in_dev_name, out_dev_name):
    """Periodically redraw the status display."""
    while state["running"]:
        # --- TX rate / resample status -------------------------------------
        cap_rate = state.get("tx_capture_rate", 0.0)
        cap_ch = state.get("tx_capture_channels", 0)
        needs_r = state.get("tx_needs_resample", False)
        backend = state.get("tx_resample_backend", "none")
        rs_on = state.get("tx_resample_enabled", True)

        if cap_rate <= 0:
            tx_rate_str = f"{GRAY}—{RESET}"
        elif not needs_r:
            tx_rate_str = f"{GREEN}{cap_rate/1000:g} kHz {cap_ch}ch (passthrough){RESET}"
        elif backend == "missing":
            tx_rate_str = (f"{RED}{cap_rate/1000:g} kHz {cap_ch}ch → 48 kHz "
                           f"(NO RESAMPLER — install soxr!){RESET}")
        elif rs_on:
            tx_rate_str = (f"{GREEN}{cap_rate/1000:g} kHz {cap_ch}ch → "
                           f"48 kHz mono ({backend}){RESET}")
        else:
            tx_rate_str = (f"{YELLOW}{cap_rate/1000:g} kHz {cap_ch}ch → 48 kHz "
                           f"RESAMPLE OFF ({backend}){RESET}")

        # --- RX rate status -------------------------------------------------
        rx_stream = state.get("rx_stream_rate", 0.0)
        rx_dev = state.get("rx_device_rate", 0.0)
        if not rx_stream:
            rx_rate_str = f"{GRAY}—{RESET}"
        elif rx_dev and abs(rx_dev - rx_stream) > 1.0:
            rx_rate_str = (f"{RED}{rx_stream/1000:g} kHz "
                           f"(device {rx_dev/1000:g} kHz!){RESET}")
        else:
            rx_rate_str = f"{GREEN}{rx_stream/1000:g} kHz{RESET}"

        # TX status
        tx_conn = state["tx_connected"]
        tx_live = state["tx_live"]
        tx_db = state.get("tx_db", -100.0)
        tx_vol = state["tx_vol"]
        if not tx_conn:
            tx_tag = f"{YELLOW}DISCONNECTED{RESET}"
        elif tx_live:
            tx_tag = f"{GREEN}  ON{RESET}"
        else:
            tx_tag = f"{YELLOW}MUTE{RESET}"
        tx_bar, tx_sdb = level_bar(tx_db, vol_pct=tx_vol)

        # RX status
        rx_conn = state["rx_connected"]
        rx_play = state["rx_play"]
        rx_db = state.get("rx_db", -100.0)
        rx_vol = state["rx_vol"]
        rx_from = state.get("rx_from")
        if not rx_conn:
            rx_tag = f"{YELLOW}WAITING{RESET}"
        elif rx_play:
            rx_tag = f"{GREEN}PLAY{RESET}"
        else:
            rx_tag = f"{YELLOW}MUTE{RESET}"
        rx_bar, rx_sdb = level_bar(rx_db, vol_pct=rx_vol)

        # --- Diagnostic block (always visible) -----------------------------
        xruns = state.get("tx_xruns", 0)
        xrun_color = RED if xruns else GRAY
        cb = state.get("tx_cb_stats") or {}
        cbs = cb.get("callbacks", 0)
        total_frames = cb.get("total_frames", 0)
        last_frames = cb.get("last_frames", 0)
        last_status = cb.get("last_status", "")
        gap_min = cb.get("gap_min")
        gap_max = cb.get("gap_max")
        gap_sum = cb.get("gap_sum", 0.0)
        gap_count = cb.get("gap_count", 0)
        gap_avg = (gap_sum / gap_count) if gap_count else None
        started = cb.get("started")
        elapsed = (time.monotonic() - started) if started else 0.0
        # Effective rate from total frames over elapsed wall time
        eff_rate = (total_frames / elapsed) if elapsed > 0 else 0.0
        # Top 3 most-common frames-per-callback values
        hist = cb.get("frames_hist") or {}
        top = sorted(hist.items(), key=lambda kv: -kv[1])[:3]
        hist_str = ", ".join(f"{k}×{v}" for k, v in top) if top else "—"

        def _ms(x):
            return f"{x*1000:.1f}ms" if x is not None else "—"

        # Effective-rate sanity color: red if it diverges from the requested
        # capture rate by more than 2%.
        cap_rate = state.get("tx_capture_rate", 0.0) or 1.0
        if eff_rate > 0:
            err = abs(eff_rate - cap_rate) / cap_rate
            eff_color = RED if err > 0.02 else GREEN
        else:
            eff_color = GRAY

        # WAV recording status
        wav_path = state.get("tx_wav_path")
        wav_bytes = state.get("tx_wav_bytes", 0)
        if wav_path:
            wav_secs = wav_bytes / (SAMPLE_RATE * 2)
            wav_line = (f"  {RED}● REC{RESET} {os.path.basename(wav_path)}  "
                        f"{wav_secs:6.1f} s  ({wav_bytes/1024:.1f} KB)\n")
        else:
            wav_line = ""

        if rx_from:
            conn_line = f"     Gateway connected from {rx_from}\n"
        else:
            conn_line = f"     Listening on port {rx_port} ...\n"

        # Render full frame each refresh — no absolute cursor positioning
        # (avoids the meter rendering over a wrapped header line).
        frame = (
            "\033[2J\033[H"
            f"{BOLD}Radio Gateway — Full Duplex Audio Client{RESET}\n"
            f"\n"
            f"  TX mic    : {in_dev_name}\n"
            f"              {tx_rate_str}\n"
            f"  RX speaker: {out_dev_name}\n"
            f"              {rx_rate_str}\n"
            f"  Gateway   : {gateway_host}  (TX→{tx_port}  RX←{rx_port})\n"
            f"\n"
            f"  Keys: {CYAN}l{RESET}=TX on/mute  {CYAN}p{RESET}=RX play/mute  "
            f"{CYAN}r{RESET}=resample toggle  {CYAN}w{RESET}=record TX→wav\n"
            f"        {CYAN}</>={RESET}TX vol  {CYAN}[/]={RESET}RX vol  Ctrl+C=quit\n"
            f"\n"
            f"  TX {tx_tag:>20s}  [{tx_bar}] {tx_sdb:+6.1f} dBFS  Vol:{tx_vol:3d}%\n"
            f"  RX {rx_tag:>20s}  [{rx_bar}] {rx_sdb:+6.1f} dBFS  Vol:{rx_vol:3d}%\n"
            f"{conn_line}"
        )

        # Compact one-line diag (always visible). Anything unhealthy lights
        # up in red so problems are obvious at a glance.
        q_drops = state.get("tx_q_drops", 0)
        cps = cbs / elapsed if elapsed > 0 else 0.0
        rate_field = f"{eff_color}{eff_rate:.0f}Hz{RESET}"
        drops_field = (f"{RED}drops={q_drops}{RESET}" if q_drops
                       else f"{GRAY}drops=0{RESET}")
        xrun_field = (f"{RED}xrun={xruns}{RESET}" if xruns
                      else f"{GRAY}xrun=0{RESET}")
        frame += (
            f"     {GRAY}TX: {cps:5.1f}cb/s  "
            f"q={state.get('tx_q_depth',0)}  "
            f"{drops_field}{GRAY}  {xrun_field}{GRAY}  "
            f"rate={rate_field}{GRAY}  "
            f"iter={state.get('tx_iter_ms',0):.1f}ms"
            f"{RESET}  {CYAN}d{RESET}=detail\n"
        )

        # Expanded diag (toggled with 'd')
        if state.get("show_diag", False):
            frame += (
                f"\n"
                f"  {BOLD}TX capture diagnostics{RESET}\n"
                f"     callbacks  : {cbs}   elapsed: {elapsed:6.2f}s\n"
                f"     frames/cb  : last={last_frames}   hist: {hist_str}\n"
                f"     total frms : {total_frames}\n"
                f"     eff. rate  : {eff_color}{eff_rate:8.1f} Hz{RESET}   "
                f"(requested {cap_rate:.0f} Hz)\n"
                f"     cb gaps    : min={_ms(gap_min)}  "
                f"avg={_ms(gap_avg)}  max={_ms(gap_max)}\n"
                f"     xruns      : {xrun_color}{xruns}{RESET}   "
                f"last status: {last_status or '—'}\n"
                f"     q drops    : {(RED if q_drops else GRAY)}{q_drops}{RESET}   "
                f"q depth: {state.get('tx_q_depth',0)}\n"
                f"     loop time  : iter={state.get('tx_iter_ms',0):.1f}ms  "
                f"send={state.get('tx_send_ms',0):.1f}ms\n"
            )

        frame += f"{wav_line}"
        sys.stdout.write(frame)
        sys.stdout.flush()

        time.sleep(0.25)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def diag_capture(seconds=5):
    """Capture raw audio at the input device's native rate. No resampling,
    no network, no GUI. Writes a WAV at the device's native rate and prints
    per-callback statistics. Use this to isolate capture problems from
    everything downstream.
    """
    import queue, collections
    here = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(here, "diag_output.txt")
    log_fh = open(log_path, "w", encoding="utf-8")
    def log(msg=""):
        print(msg, flush=True)
        log_fh.write(msg + "\n")
        log_fh.flush()
    cfg = load_config()
    in_dev_index, in_dev_name = choose_input_device(cfg)
    dev_info = sd.query_devices(in_dev_index)
    capture_rate = int(round(float(dev_info["default_samplerate"]))) or 48000
    max_in_ch = int(dev_info["max_input_channels"])
    capture_channels = 2 if max_in_ch >= 2 else 1
    capture_block = max(1, int(round(capture_rate * FRAMES_PER_BUFFER / SAMPLE_RATE)))

    log(f"\n[diag] device: {in_dev_name}")
    log(f"[diag] device info: {dev_info}")
    log(f"[diag] capture: rate={capture_rate} ch={capture_channels} "
        f"block={capture_block}  (recording {seconds}s)\n")

    q = queue.Queue()
    frame_sizes = collections.Counter()
    status_msgs = []
    t_start = [None]
    t_last = [None]
    gap_log = []
    total_frames = [0]

    def cb(indata, frames, time_info, status):
        now = time.monotonic()
        if t_start[0] is None:
            t_start[0] = now
        if t_last[0] is not None:
            gap_log.append(now - t_last[0])
        t_last[0] = now
        if status:
            status_msgs.append(str(status))
        frame_sizes[frames] += 1
        total_frames[0] += frames
        q.put(bytes(indata))

    stream = sd.RawInputStream(
        samplerate=capture_rate,
        blocksize=capture_block,
        device=in_dev_index,
        channels=capture_channels,
        dtype="int16",
        callback=cb,
    )
    stream.start()
    log(f"[diag] stream actual: rate={stream.samplerate} latency={stream.latency}")
    log(f"[diag] recording...")

    deadline = time.monotonic() + seconds
    chunks = []
    while time.monotonic() < deadline:
        try:
            chunks.append(q.get(timeout=0.5))
        except queue.Empty:
            pass

    stream.stop()
    stream.close()

    elapsed = (t_last[0] - t_start[0]) if t_start[0] else 0.0
    raw = b"".join(chunks)
    expected_frames = int(elapsed * capture_rate)
    log(f"\n[diag] callbacks received   : {sum(frame_sizes.values())}")
    log(f"[diag] frames-per-callback   : {dict(frame_sizes)}")
    log(f"[diag] total frames captured : {total_frames[0]}")
    log(f"[diag] elapsed wall time     : {elapsed:.3f} s")
    log(f"[diag] expected frames @rate : {expected_frames}  "
        f"(ratio: {total_frames[0]/max(1,expected_frames):.3f})")
    log(f"[diag] status flags seen     : "
        f"{collections.Counter(status_msgs) if status_msgs else 'none'}")
    if gap_log:
        gmin = min(gap_log); gmax = max(gap_log)
        gavg = sum(gap_log) / len(gap_log)
        log(f"[diag] inter-callback gaps   : min={gmin*1000:.1f}ms "
            f"avg={gavg*1000:.1f}ms max={gmax*1000:.1f}ms  (n={len(gap_log)})")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"diag_raw_{capture_rate}_{capture_channels}ch_{ts}.wav",
    )
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(capture_channels)
        w.setsampwidth(2)
        w.setframerate(capture_rate)
        w.writeframes(raw)
    log(f"\n[diag] wrote raw capture → {wav_path}")
    log(f"[diag] open this WAV at {capture_rate} Hz / {capture_channels}ch — "
        f"if it sounds choppy, the problem is capture itself, not resampling.")
    log(f"\n[diag] full report saved to: {log_path}")
    try:
        log_fh.close()
    except Exception:
        pass


def main():
    # Persist startup info to a file so it survives any later screen clears.
    here = os.path.dirname(os.path.abspath(__file__))
    startup_log = os.path.join(here, "startup.log")
    try:
        with open(startup_log, "w", encoding="utf-8") as f:
            f.write(f"argv = {sys.argv!r}\n")
            f.write(f"cwd  = {os.getcwd()!r}\n")
            f.write(f"file = {__file__!r}\n")
    except Exception:
        pass

    sys.stderr.write(f"[startup] argv = {sys.argv!r}\n")
    sys.stderr.flush()

    # Diagnostic mode: pure capture, no network, no GUI. Permissive arg match.
    want_diag = any("diag" in a.lower() for a in sys.argv[1:])
    if want_diag:
        sys.stderr.write("[startup] entering diag_capture()\n")
        sys.stderr.flush()
        try:
            diag_capture()
        except Exception as e:
            import traceback
            sys.stderr.write(f"[diag] CRASHED: {type(e).__name__}: {e}\n")
            traceback.print_exc(file=sys.stderr)
        try:
            input("\nPress Enter to exit ")
        except Exception:
            pass
        return

    cfg = load_config()

    # --- Gateway host -------------------------------------------------------
    gateway_host = None
    if len(sys.argv) >= 2 and not sys.argv[1].startswith("--"):
        gateway_host = sys.argv[1]
    if not gateway_host:
        gateway_host = cfg.get("gateway_host")
    if not gateway_host:
        gateway_host = input("Gateway host (IP or hostname): ").strip()
        if not gateway_host:
            print("No host provided.")
            sys.exit(1)

    # --- Ports --------------------------------------------------------------
    tx_port = cfg.get("tx_port", DEFAULT_TX_PORT)
    rx_port = cfg.get("rx_port", DEFAULT_RX_PORT)

    # --- Audio devices ------------------------------------------------------
    try:
        in_dev_index, in_dev_name = choose_input_device(cfg)
        out_dev_index, out_dev_name = choose_output_device(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

    # --- Save config --------------------------------------------------------
    cfg["gateway_host"] = gateway_host
    cfg["tx_port"] = tx_port
    cfg["rx_port"] = rx_port
    cfg["tx_device_name"] = in_dev_name
    cfg["rx_device_name"] = out_dev_name
    save_config(cfg)

    # --- Shared state -------------------------------------------------------
    state = {
        "running": True,
        "tx_live": False,
        "rx_play": True,
        "tx_vol": 100,
        "rx_vol": 100,
        "tx_connected": False,
        "rx_connected": False,
        "tx_db": -100.0,
        "rx_db": -100.0,
        "rx_from": None,
        "tx_resample_enabled": True,
        "tx_xruns": 0,
        "tx_last_frames": 0,
    }

    # --- Start threads ------------------------------------------------------
    threads = [
        threading.Thread(target=_keyboard_listener, args=(state,), daemon=True),
        threading.Thread(target=_tx_thread_func, args=(state, cfg, gateway_host, tx_port, in_dev_index, in_dev_name), daemon=True, name="TX"),
        threading.Thread(target=_rx_thread_func, args=(state, cfg, rx_port, out_dev_index, out_dev_name), daemon=True, name="RX"),
        threading.Thread(target=_display_thread_func, args=(state, gateway_host, tx_port, rx_port, in_dev_name, out_dev_name), daemon=True, name="Display"),
    ]
    for t in threads:
        t.start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\nShutting down.")
        state["running"] = False
        time.sleep(0.3)


if __name__ == "__main__":
    main()
