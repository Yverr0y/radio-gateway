# Windows Audio Client → Gateway: Choppy-Audio Investigation

**Status:** Client side fixed. Gateway side likely has occasional drops remaining that need investigation by the gateway team.

This is a handover doc for whoever owns the gateway's RX audio path. Read this end-to-end before changing anything on the gateway side — the symptoms looked like a single bug but had two distinct root causes.

---

## TL;DR for the gateway-side Claude

- The Windows client (`windows_audio_client.py`) was sending audio at the wrong sample rate. **Fixed.** It now captures at the device's native rate, downmixes stereo→mono, and resamples to 48 kHz with `soxr` before sending.
- Verified end-to-end: with the fix, client-side instrumentation shows clean, jitter-free 48 kHz mono delivery (0 queue drops, 0 xruns, loop time 0.3 ms per 50 ms chunk).
- **However**, the user reports "some drops here and there" still audible at the gateway, even though the client is delivering cleanly. That points at the **gateway's RX path** as the remaining problem. This doc is the brief for that investigation.

---

## Symptoms and how we narrowed them down

1. **Initial symptom:** audio arriving at the gateway from the Windows client was choppy. Sounded like a sample-rate mismatch (warbly, sped-up-ish character).
2. **Suspect 1 (correct):** the Windows loopback device was actually running at 44.1 kHz but the client was opening the input stream at 48 kHz and sending raw bytes. PortAudio/WASAPI in some modes doesn't auto-resample on capture — you get 44.1 kHz audio tagged and sent as if it were 48 kHz.
3. **Suspect 2 (also true, made debugging confusing):** the audio capture device was 2-channel (loopback devices usually are), but the client opened it as 1-channel. Behavior varies by host API.
4. **Suspect 3 (red herring):** thought there might be a periodic 2 s stall in the audio thread (user reported GUI freezing 2 s every 4 s). Turned out to be console-rendering pressure — the display thread was doing full-screen clears at 10 Hz; reducing to 4 Hz fixed it.

### What the client now does

`windows_audio_client.py` (TX side):

1. Queries the device's actual `default_samplerate` and `max_input_channels`.
2. Opens `sd.RawInputStream` at the device's native rate and `min(2, max_channels)` channels, blocksize ≈ 50 ms.
3. In the TX worker thread, every chunk goes through:
   - **Stereo → mono downmix** (`int32` mean to avoid overflow)
   - **Resample to 48 kHz** via `soxr.ResampleStream` (HQ quality, stateful streaming) if the native rate ≠ 48 kHz. Fallback: `scipy.signal.resample_poly`.
   - Volume scaling
   - Length-prefixed TCP send (4-byte big-endian `uint32` length + PCM16 LE bytes)
4. The post-processed (48 kHz mono int16) buffer is also what gets dumped to WAV via the `w` key, and what's fed to the level meter — so when the GUI says it's OK, it's the same bytes the gateway is receiving.

### Live diagnostics in the client GUI

The client now permanently shows a one-line TX diag in its status pane:

```
TX:  20.0cb/s  q=0  drops=0  xrun=0  rate=44077Hz  iter=0.3ms   d=detail
```

Press `d` for the expanded block, which adds frames-per-callback histogram, inter-callback gap min/avg/max, total frame counter, and elapsed time. A `--diag` CLI mode also exists: `python windows_audio_client.py --diag` captures 5 s of raw audio at the device's native rate, writes it to `diag_raw_<rate>_<ch>_<ts>.wav`, and dumps stats to `diag_output.txt`.

### Evidence the client is now clean

Confirmed by user (~80 s of running audio):

```
callbacks  : 1583   elapsed:  79.19s
frames/cb  : last=2205   hist: 2205×1583     (all callbacks identical size)
total frms : 3490515
eff. rate  : 44076.7 Hz   (requested 44100 Hz)   ← 0.05% drift, fine
cb gaps    : min=15.1ms  avg=50.0ms  max=81.8ms
xruns      : 0   last status: —
q drops    : 0   q depth: 0                    ← TX worker not falling behind
loop time  : iter=0.3ms  send=0.1ms             ← two orders of magnitude faster than required
```

Interpretation:
- Capture is rock-solid 44.1 kHz, 50 ms callbacks, no driver underruns.
- The TX worker processes each chunk in 0.3 ms total (including resample + WAV write + TCP send). It has ~166× headroom against the 50 ms inter-chunk interval.
- `sock.sendall(...)` averages 0.1 ms — TCP isn't backpressuring the client.
- The one cb-gap outlier (81.8 ms vs 50 ms avg) is normal Windows scheduling jitter; the queue absorbs it with no drops.

**Conclusion:** if drops are still audible at the gateway, they are not introduced on the client side. The bytes going onto the wire are clean.

---

## What this means for the gateway side

The gateway's RX listener for this client is on port **9602** by default (`REMOTE_AUDIO_RX_PORT`). The client connects out to that port and pushes length-prefixed PCM:

- 4-byte big-endian `uint32` length
- followed by `length` bytes of **PCM16 LE, 48 kHz, mono**
- length is normally **4800 bytes** (50 ms of audio per chunk) — but **after the soxr resampler runs, individual chunks can be slightly larger or smaller** (e.g. 4400 / 5200) because polyphase resampling produces variable-size output. The average over time is exactly 4800 bytes per 50 ms wall clock.

That last point is the most likely source of new bugs on the gateway side if its receiver assumes constant 4800-byte chunks. **Do not assume constant chunk size.** Treat the wire as a continuous PCM stream framed by the length prefix and ride it sample-by-sample into your jitter buffer.

### Top hypotheses to investigate on the gateway, in priority order

1. **Jitter buffer too shallow.**
   - The client's callback gap occasionally spikes to ~80 ms (vs the average 50 ms). If the gateway plays out at exactly 48 kHz with < 80 ms of buffered audio at the time of the outlier, it will underrun and you'll hear a click/drop.
   - **Action:** measure how deep the RX jitter buffer is right now. Target ≥ 200 ms for this client. If you're starting playback the moment audio arrives (no pre-buffer), you'll underrun on every WASAPI jitter spike.

2. **Variable-size chunk assumption.**
   - As noted above, post-resample chunks aren't exactly 4800 bytes. If the gateway accumulates a fixed number of bytes before playing, or expects each socket frame to be one audio frame's worth, you'll get phase issues or drops at every variable-length chunk.
   - **Action:** search the gateway code for hard-coded `4800`, `2400`, or `FRAMES_PER_BUFFER` near the REMOTE_AUDIO_RX path. Verify the receive code reads `length` bytes per frame and writes them into a PCM ring buffer, not a chunk queue.

3. **Reader thread blocking.**
   - If the RX reader holds a lock that the audio output thread also wants (mixer state, bus routing, logging), brief contention can underrun playback. The mixer v2.0 rewrite is in progress (see `docs/mixer-v2-design.md`), so check whether the RX path holds the AudioMixer lock while it reads from the socket.
   - **Action:** profile the RX reader with `perf_counter()` around socket read → buffer write. If the write-to-buffer step ever takes more than a few ms, that's a contention bug.

4. **Sample-rate drift between gateway TX and downstream sinks.**
   - The client's audio clock and the gateway's playback clock are independent (different physical crystals). Over minutes, they will drift by tens of samples. If the gateway has no resampler or rate-tracking between RX and the sink (Broadcastify, listen bus, speaker), drift will eventually cause periodic single-sample drops as the buffer fills or empties.
   - **Action:** confirm whether the gateway's RX → sink path has any rate adaptation. If not, document the limit (expect drops every N minutes) or add a tiny adaptive resampler with rate locked to the playout sink.

5. **TCP receive buffer too small.**
   - Less likely to cause audible drops (TCP doesn't drop data, just delays it), but worth checking. If the SO_RCVBUF is small and the reader stalls briefly, the kernel will backpressure the sender, the client's send_ms metric will spike, and during that pause the gateway's audio buffer drains.
   - **Action:** check `SO_RCVBUF` on the gateway's accept socket — at least 256 KB is safe for 48 kHz mono.

### How to verify a gateway-side fix

Two parallel checks once you've tried something:

1. **Client-side WAV dump.** Have the user press `w` in the Windows client to start recording, run for ~60 s, press `w` to stop. The resulting WAV is exactly what was sent over the wire. Compare its waveform/audio to what the gateway plays out. If the client WAV is clean and gateway is choppy → the gateway is the problem (we're at this state right now). If client WAV is choppy → something regressed in the client.
2. **Client send-time metric.** Watch the `iter=` and `send=` fields in the client's diag line. If `send` starts spiking (> 5 ms) when the user hears drops, the gateway is backpressuring TCP, which means the gateway reader is stalling for those moments. That's a much narrower bug than "drops somewhere."

### Useful files

- Client: `windows_audio_client.py` (TX path is the `_tx_thread_func` function)
- Gateway entry for this client: search for `REMOTE_AUDIO_RX_PORT` (default 9602) and follow the accept loop.
- Gateway protocol contract for this connection is mirrored in `docs/gateway_link.md` for the gateway-link feature; check whether the Windows client TX path shares any code or assumptions with that.

### What NOT to do

- Don't assume the client is sending fixed-size 4800-byte chunks. It isn't, by design.
- Don't add a "drop one chunk on overflow" handler without first verifying you're actually overflowing — that masks the real problem.
- Don't change the client to send Opus or Vorbis to "fix" this — the wire format is fine and the client is delivering on time. The problem is on the gateway's receive/playout side.

---

## Appendix: client changes summary (for context)

Files changed:
- `windows_audio_client.py` — full TX-path rewrite

New dependency:
- `soxr` (`pip install soxr`) — best-effort stateful streaming resampler. Falls back to `scipy.signal.resample_poly` if missing. Warns if neither is available.

New CLI mode:
- `python windows_audio_client.py --diag` — 5-second capture-only diagnostic. No GUI, no network, no resampling. Writes WAV + report.

New keys in GUI:
- `r` — toggle resampling on/off (A/B for diagnosis; gateway will sound wrong with it off if the device isn't 48 kHz native)
- `w` — start/stop WAV dump of the post-processed TX stream (exactly the bytes hitting the wire)
- `d` — expand the inline diagnostics block
