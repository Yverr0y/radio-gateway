"""WebMicSource hold-to-talk: keying, dead-man, and the time-out timer.

The point of every check here is that the transmitter stops. A stuck key is
the one failure this source must not have, so the watchdogs are exercised
directly on the bus-thread path (get_audio) rather than through the socket.
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import audio_sources  # noqa: E402

FAIL = []
CHUNK = 2400               # AUDIO_CHUNK_SIZE samples
CHUNK_BYTES = CHUNK * 2


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def make_src(**over):
    cfg = types.SimpleNamespace(
        AUDIO_CHANNELS=1, AUDIO_RATE=48000, AUDIO_CHUNK_SIZE=CHUNK,
        WEB_MIC_VOLUME=1.0, WEB_MIC_KEY_TIMEOUT=0.3, WEB_MIC_MAX_TX=1.0,
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    gw = types.SimpleNamespace(config=cfg)
    s = audio_sources.WebMicSource(cfg, gw)
    s.client_connected = True
    return s


def tone(n_bytes=CHUNK_BYTES, val=6000):
    import numpy as np
    return (np.full(n_bytes // 2, val, dtype=np.int16)).tobytes()


print("\n=== WebMicSource hold-to-talk ===\n")

print("1. Connected but not keyed must NOT transmit")
s = make_src()
s.push_audio(tone())
audio, ptt = s.get_audio(CHUNK)
check("no audio while unkeyed", audio is None, f"got {type(audio).__name__}")
check("no PTT while unkeyed", ptt is False)
check("is_active() false while unkeyed", s.is_active() is False)
check("status reads Armed", s.get_status() == "WEBMIC: Armed", s.get_status())

print("\n2. Key → audio and PTT flow")
s = make_src()
check("key() accepted", s.key() is True)
s.push_audio(tone())
audio, ptt = s.get_audio(CHUNK)
check("audio delivered", audio is not None and len(audio) == CHUNK_BYTES)
check("PTT asserted", ptt is True)
check("is_active() true while keyed", s.is_active() is True)
check("key_count incremented", s.key_count == 1, str(s.key_count))

print("\n3. Underrun mid-over holds the key with silence")
s = make_src()
s.key()
audio, ptt = s.get_audio(CHUNK)   # queue empty
check("silence returned", audio == b'\x00' * CHUNK_BYTES)
check("PTT held through underrun", ptt is True)

print("\n4. Explicit unkey stops TX immediately")
s = make_src()
s.key()
s.push_audio(tone())
s.unkey('release')
audio, ptt = s.get_audio(CHUNK)
check("no audio after unkey", audio is None)
check("no PTT after unkey", ptt is False)
check("queued audio dropped", s._chunk_queue.qsize() == 0, str(s._chunk_queue.qsize()))

print("\n5. Dead-man: a lapsed refresh unkeys with no client involvement")
s = make_src()
s.key()
_, ptt = s.get_audio(CHUNK)
check("keyed before the deadline", ptt is True)
time.sleep(0.35)                    # > WEB_MIC_KEY_TIMEOUT (0.3)
audio, ptt = s.get_audio(CHUNK)
check("PTT dropped by dead-man", ptt is False)
check("tx_keyed cleared", s.tx_keyed is False)
check("deadman_trips counted", s.deadman_trips == 1, str(s.deadman_trips))
check("reason recorded", s.last_unkey_reason == 'deadman', s.last_unkey_reason)

print("\n6. Refreshing the key holds TX open past the dead-man")
s = make_src()
s.key()
held = True
for _ in range(4):
    time.sleep(0.15)                # half the 0.3s timeout
    s.key()                         # refresh, as the browser does
    _, ptt = s.get_audio(CHUNK)
    held = held and ptt
check("stays keyed across refreshes", held is True)
check("still one logical key", s.key_count == 1, str(s.key_count))

print("\n7. Time-out timer bounds a single over, refreshes cannot extend it")
s = make_src()
s.key()
t0 = time.monotonic()
tripped_at = None
while time.monotonic() - t0 < 2.0:
    time.sleep(0.1)
    s.key()                         # operator still holding
    _, ptt = s.get_audio(CHUNK)
    if not ptt:
        tripped_at = time.monotonic() - t0
        break
check("TOT tripped", tripped_at is not None)
check("tripped at ~MAX_TX", tripped_at is not None and 0.9 < tripped_at < 1.4,
      f"{tripped_at:.2f}s" if tripped_at else 'never')
check("tot_trips counted", s.tot_trips == 1, str(s.tot_trips))
check("re-key refused while held", s.key() is False)
_, ptt = s.get_audio(CHUNK)
check("stays unkeyed while refused", ptt is False)

print("\n8. Release resets the tripped TOT")
check("unkey clears the trip", (s.unkey('release'), s.key())[1] is True)
_, ptt = s.get_audio(CHUNK)
check("transmits again after re-press", ptt is True)
check("counted as a new key", s.key_count == 2, str(s.key_count))

print("\n9. Disconnect unkeys")
s = make_src()
s.key()
s.unkey('socket closed')
s.client_connected = False
audio, ptt = s.get_audio(CHUNK)
check("no PTT after disconnect", ptt is False)
check("status reads Idle", s.get_status() == "WEBMIC: Idle", s.get_status())

print("\n10. Gain above unity soft-clips instead of flat-topping")
s = make_src(WEB_MIC_VOLUME=4.0)
s.key()
s.push_audio(tone(val=20000))       # 20000 * 4 would clip hard at 32767
audio, _ = s.get_audio(CHUNK)
import numpy as np
peak = int(np.abs(np.frombuffer(audio, dtype=np.int16)).max())
check("peak below the rail", peak < 32767, str(peak))
check("still boosted", peak > 20000, str(peak))

print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILURES: ' + ', '.join(FAIL)}\n")
sys.exit(1 if FAIL else 0)
