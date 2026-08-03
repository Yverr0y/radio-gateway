"""BGM source, Announcer source, partial ducking, and message persistence."""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from _tmpdirs import mkdtemp  # noqa: E402
import audio_sources  # noqa: E402
import audio_bus  # noqa: E402



FAIL = []
RATE_BYTES = 4800          # one 50ms mono chunk at 48k, 16-bit


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def fake_gw(**cfg):
    c = types.SimpleNamespace(AUDIO_CHANNELS=1, AUDIO_RATE=48000, **cfg)
    return types.SimpleNamespace(config=c, playback_source=None)


print("\n1. BGM loops its bed seamlessly")
gw = fake_gw()
b = audio_sources.BGMSource(gw)
bed = bytes(range(256)) * 10          # 2560 bytes — shorter than one chunk
b._pcm = bed
b._slot = 1
out1, ptt = b.get_audio(2400)         # asks for 4800 bytes
check("returns a full chunk despite a short bed", len(out1) == RATE_BYTES, str(len(out1)))
check("keys PTT while playing", ptt is True)
check("is_active while a bed is loaded", b.is_active() is True)
outs = b'' .join(b.get_audio(2400)[0] for _ in range(5))
check("keeps producing full chunks", len(outs) == RATE_BYTES * 5, str(len(outs)))
b.stop()
check("stop clears the bed", b.is_active() is False)
check("silent once stopped", b.get_audio(2400)[0] is None)

print("\n2. BGM duck level is a linear gain from dB")
for db, want in ((-12.0, 0.251), (0.0, 1.0), (-6.0, 0.501)):
    b2 = audio_sources.BGMSource(fake_gw(BGM_DUCK_DB=db))
    check(f"{db} dB -> ~{want}", abs(b2.duck_level - want) < 0.01, f"{b2.duck_level:.3f}")
b3 = audio_sources.BGMSource(fake_gw(BGM_DUCK_DB='nonsense'))
check("bad config falls back to -12 dB", abs(b3.duck_level - 0.251) < 0.01, f"{b3.duck_level:.3f}")
b4 = audio_sources.BGMSource(fake_gw())
check("default is -12 dB", abs(b4.duck_level - 0.251) < 0.01, f"{b4.duck_level:.3f}")

print("\n3. partial duck attenuates instead of dropping (the whole point)")
# Sources WITHOUT duck_level must keep the old hard-mute behaviour.
plain = types.SimpleNamespace(name='SDR1')
check("a normal source exposes no duck_level",
      getattr(plain, 'duck_level', None) is None)
check("BGM does expose one", audio_sources.BGMSource(fake_gw()).duck_level is not None)
check("apply_gain exists for the bus to use", hasattr(audio_bus, 'apply_gain'))
loud = (b'\x00\x40' * 1200)            # constant 0x4000 samples
quiet = audio_bus.apply_gain(loud, 0.251)
import struct  # noqa: E402
lv = struct.unpack('<h', loud[:2])[0]
qv = struct.unpack('<h', quiet[:2])[0]
check("attenuated, not silenced", 0 < abs(qv) < abs(lv), f"{lv} -> {qv}")
check("roughly the requested ratio", abs(abs(qv) / abs(lv) - 0.251) < 0.02,
      f"{abs(qv)/abs(lv):.3f}")

print("\n4. Announcer repeats on an interval and is silent between")
gw2 = fake_gw(ANNOUNCER_INTERVAL=2.0)
a = audio_sources.AnnouncerSource(gw2)
check("silent with no message", a.get_audio(2400)[0] is None)
check("inactive with no message", a.is_active() is False)
a.set_message_pcm(b'\x01\x02' * 2400)      # exactly one chunk
a.set_enabled(True)
check("still silent before the first due time", a.get_audio(2400)[0] is None)
a._next_at = time.monotonic() - 1          # make it due
first = a.get_audio(2400)[0]
check("speaks when due", first is not None and len(first) == RATE_BYTES,
      str(len(first) if first else None))
check("silent again immediately after", a.get_audio(2400)[0] is None)
check("inactive between announcements", a.is_active() is False)
check("next due ~interval away", 1.0 < (a._next_at - time.monotonic()) <= 2.0,
      f"{a._next_at - time.monotonic():.1f}s")

print("\n5. Announcer interval is clamped and configurable")
check("default 10s", audio_sources.AnnouncerSource(fake_gw()).interval == 10.0)
check("honours config", audio_sources.AnnouncerSource(fake_gw(ANNOUNCER_INTERVAL=45)).interval == 45.0)
check("clamps absurdly short", audio_sources.AnnouncerSource(fake_gw(ANNOUNCER_INTERVAL=0.1)).interval == 2.0)
check("survives junk", audio_sources.AnnouncerSource(fake_gw(ANNOUNCER_INTERVAL='x')).interval == 10.0)

print("\n6. disabling silences it immediately")
a.set_message_pcm(b'\x01\x02' * 2400)
a.set_enabled(True)
a._next_at = time.monotonic() - 1
check("speaking when enabled", a.get_audio(2400)[0] is not None)
a.set_enabled(False)
check("silent once disabled", a.get_audio(2400)[0] is None)

print("\n7. message store persists")
import announcer  # noqa: E402
announcer._PATH = os.path.join(mkdtemp('bgm-test-'), 'announcer.json')
check("defaults when absent",
      announcer.load()['messages'] == {} and announcer.load()['interval'] == 10.0)
announcer.save({'messages': {'1': 'Net at 8pm', '3': 'ID please'},
                'interval': 25.0, 'voice': '19', 'enabled': True})
st = announcer.load()
check("round-trips per-bed messages",
      st['messages'] == {'1': 'Net at 8pm', '3': 'ID please'}, str(st['messages']))
check("round-trips interval", st['interval'] == 25.0, str(st['interval']))
check("round-trips enabled", st['enabled'] is True)
check("round-trips voice", st['voice'] == '19', st['voice'])
open(announcer._PATH, 'w').write('{not json')
check("corrupt file falls back to defaults, no raise", announcer.load()['messages'] == {})
announcer.save({'messages': {'1': 'x'}, 'interval': 0.5, 'voice': '', 'enabled': True})
check("interval clamped on load", announcer.load()['interval'] == 2.0,
      str(announcer.load()['interval']))
announcer.save({'messages': 'not a dict', 'interval': 10, 'voice': '', 'enabled': True})
check("non-dict messages tolerated", announcer.load()['messages'] == {},
      str(announcer.load()['messages']))

print("\n8. apply() is the startup alias for on_bgm_changed")
check("apply exists for setup to call", callable(announcer.apply))

print("\n9. BGM play/stop lives on BGMSource now (not the playback source)")
_d = mkdtemp('bgm-test-')
for _f in ('bgm1.mp3', 'bgm2.mp3', 'bgm3.mp3'):
    open(os.path.join(_d, _f), 'wb').write(b'\0' * 16)
gwb = fake_gw(BGM_FILES='', PLAYBACK_DIRECTORY=_d)
gwb.playback_source = types.SimpleNamespace(
    announcement_directory=_d, _decode_file=lambda p, normalize=True: b'\x01\x02' * 1000)
bs = audio_sources.BGMSource(gwb)

check("three beds, all available", [b['available'] for b in bs.bgm_state()] == [True]*3,
      str(bs.bgm_state()))
r = bs.play_slot(2, 'start')
check("start bed 2", r['ok'] and r['playing'] == 2, str(r))
check("playing_slot follows", bs.playing_slot == 2)
check("state marks only 2", [b['playing'] for b in bs.bgm_state()] == [False, True, False])
r = bs.play_slot(3, 'start')
check("starting 3 swaps off 2", r['playing'] == 3 and bs.playing_slot == 3)
r = bs.play_slot(3, 'toggle')
check("toggle stops it", r['playing'] is None and bs.playing_slot is None)
r = bs.play_slot(9, 'start')
check("unconfigured bed rejected", r['ok'] is False and 'not configured' in r['error'], str(r))
r = bs.play_slot('x', 'start')
check("non-numeric rejected", r['ok'] is False)
gwb.playback_source._decode_file = lambda p, normalize=True: None
r = bs.play_slot(1, 'start')
check("decode failure reported and cleared",
      r['ok'] is False and 'decode' in r['error'] and bs.playing_slot is None, str(r))

print("\n10. per-bed messages follow the playing bed")
announcer.invalidate()
announcer._PATH = os.path.join(mkdtemp('bgm-test-'), 'announcer.json')
announcer.save({'messages': {'1': 'one', '2': 'two'}, 'interval': 10.0,
                'voice': '', 'enabled': True})
st = announcer.load()
check("messages round-trip per bed", st['messages'] == {'1': 'one', '2': 'two'}, str(st['messages']))

gwc = fake_gw(BGM_FILES='', PLAYBACK_DIRECTORY=_d)
gwc.playback_source = types.SimpleNamespace(
    announcement_directory=_d, _decode_file=lambda p, normalize=True: b'\x01\x02' * 1000)
gwc.bgm_source = audio_sources.BGMSource(gwc)
gwc.announcer_source = audio_sources.AnnouncerSource(gwc)
gwc.tts_engine = None            # synthesis will fail

# No bed playing -> announcer silent, and that is NOT an error.
ok, err = announcer.on_bgm_changed(gwc, st)
check("no bed playing -> quiet, no error", ok is True and err == '', err)
check("announcer disabled", gwc.announcer_source._enabled is False)

# Bed 3 has no message -> still quiet, still not an error.
gwc.bgm_source.play_slot(3, 'start')
ok, err = announcer.on_bgm_changed(gwc, st)
check("bed without a message -> quiet, no error", ok is True and err == '', err)

# Bed 1 HAS a message, but TTS is down -> reported, left disabled.
gwc.bgm_source.play_slot(1, 'start')
ok, err = announcer.on_bgm_changed(gwc, st)
check("bed with a message but no TTS -> error", ok is False and 'TTS' in err, err)
check("still disabled, not enabled-but-mute", gwc.announcer_source._enabled is False)

# Master switch off -> quiet regardless.
st_off = dict(st, enabled=False)
ok, err = announcer.on_bgm_changed(gwc, st_off)
check("master off -> quiet, no error", ok is True and err == '', err)

print("\n11. synthesis cache keys on text+voice+backend")
calls = []
real_syn = announcer.synthesize
announcer.synthesize = lambda gw, t, v='': (calls.append((t, v)), (b'\x00' * 100, ''))[1]
try:
    announcer.invalidate()
    announcer._pcm_for(gwc, 1, 'hello', '')
    announcer._pcm_for(gwc, 1, 'hello', '')
    check("same text synthesised once", len(calls) == 1, str(calls))
    announcer._pcm_for(gwc, 1, 'changed', '')
    check("changed text re-synthesises", len(calls) == 2, str(calls))
    announcer._pcm_for(gwc, 2, 'hello', '')
    check("a different bed has its own entry", len(calls) == 3, str(calls))
    announcer.invalidate(1)
    announcer._pcm_for(gwc, 2, 'hello', '')
    check("invalidating bed 1 leaves bed 2 cached", len(calls) == 3, str(calls))
finally:
    announcer.synthesize = real_syn

print("\n12. per-bed voices")
announcer.invalidate()
announcer._PATH = os.path.join(mkdtemp('bgm-test-'), 'announcer.json')
announcer.save({'messages': {'1': 'one', '2': 'two'},
                'voices': {'1': '19', '2': 'af_heart'},
                'interval': 10.0, 'voice': '', 'enabled': True})
st = announcer.load()
check("voices round-trip per bed", st['voices'] == {'1': '19', '2': 'af_heart'},
      str(st['voices']))
announcer.save({'messages': {}, 'voices': 'not a dict', 'interval': 10,
                'voice': '', 'enabled': False})
check("non-dict voices tolerated", announcer.load()['voices'] == {},
      str(announcer.load()['voices']))

# valid_voice must reject a voice belonging to a different engine.
gwv = fake_gw()
gwv._get_tts_voices = lambda: [{'value': '1', 'label': 'Andrew'},
                               {'value': '19', 'label': 'Christopher'}]
check("valid voice kept", announcer.valid_voice(gwv, '19') == '19')
check("blank stays blank", announcer.valid_voice(gwv, '') == '')
check("voice from another engine dropped", announcer.valid_voice(gwv, 'af_heart') == '',
      announcer.valid_voice(gwv, 'af_heart'))
check("unknown index dropped", announcer.valid_voice(gwv, '999') == '')
gwbad = fake_gw()   # no _get_tts_voices at all
check("missing voice list is not fatal", announcer.valid_voice(gwbad, '19') == '')

# The per-bed voice must actually reach synthesis.
seen = []
real_syn = announcer.synthesize
announcer.synthesize = lambda gw, t, v='': (seen.append((t, v)), (b'\x00' * 100, ''))[1]
try:
    announcer.invalidate()
    gwx = fake_gw(BGM_FILES='', PLAYBACK_DIRECTORY=_d)
    gwx._get_tts_voices = gwv._get_tts_voices
    gwx.playback_source = types.SimpleNamespace(
        announcement_directory=_d, _decode_file=lambda p, normalize=True: b'\x01' * 100)
    gwx.bgm_source = audio_sources.BGMSource(gwx)
    gwx.announcer_source = audio_sources.AnnouncerSource(gwx)
    gwx.tts_engine = object()
    stx = {'messages': {'1': 'hello', '2': 'world'},
           'voices': {'1': '19', '2': ''},
           'interval': 10.0, 'voice': '', 'enabled': True}
    gwx.bgm_source.play_slot(1, 'start')
    announcer.on_bgm_changed(gwx, stx)
    check("bed 1 synthesised with its own voice", seen[-1] == ('hello', '19'), str(seen[-1]))
    gwx.bgm_source.play_slot(2, 'start')
    announcer.on_bgm_changed(gwx, stx)
    check("bed 2 with no voice falls back to default", seen[-1] == ('world', ''), str(seen[-1]))
    # A voice from the wrong engine must degrade, not raise.
    stx['voices']['1'] = 'af_heart'
    announcer.invalidate()
    gwx.bgm_source.play_slot(1, 'start')
    announcer.on_bgm_changed(gwx, stx)
    check("wrong-engine voice degrades to default", seen[-1] == ('hello', ''), str(seen[-1]))
finally:
    announcer.synthesize = real_syn

print("\n13. duck envelope: smooth, held, and never to zero")
import numpy as _np  # noqa: E402


class _Ann:
    def __init__(self): self.speaking = False
    def is_active(self): return self.speaking


def env_gw(**kw):
    g = fake_gw(BGM_DUCK_DB=-12.0, BGM_DUCK_ATTACK=0.25, BGM_DUCK_HOLD=0.4,
                BGM_DUCK_RELEASE=1.2, **kw)
    g.announcer_source = _Ann()
    return g


ge = env_gw()
be = audio_sources.BGMSource(ge)
be._pcm = (b'\x00\x40' * 4800)      # constant 0x4000
be._slot = 1

CH = 2400                            # 2400 frames = 50 ms at 48k


def peak(chunk):
    a = _np.frombuffer(chunk, dtype=_np.int16).astype(_np.float32)
    return float(_np.abs(a).max()) / 16384.0     # 1.0 == unducked


check("starts at full level", abs(peak(be.get_audio(CH)[0]) - 1.0) < 0.02,
      f"{peak(be.get_audio(CH)[0]):.3f}")

# Announcer starts — must fall gradually, not in one step.
ge.announcer_source.speaking = True
g1 = peak(be.get_audio(CH)[0])
check("not an instant drop", g1 > 0.6, f"{g1:.3f} after one 50ms chunk")
gains = [peak(be.get_audio(CH)[0]) for _ in range(10)]
check("falls monotonically", all(b <= a + 0.01 for a, b in zip(gains, gains[1:])), str([round(g,2) for g in gains]))

for _ in range(20):
    be.get_audio(CH)
settled = peak(be.get_audio(CH)[0])
check("settles at the duck level, NOT zero", 0.20 < settled < 0.30, f"{settled:.3f} (want ~0.251)")
check("audibly present, not muted", settled > 0.1)

# Attack should take roughly BGM_DUCK_ATTACK to traverse the range.
be2 = audio_sources.BGMSource(env_gw())
be2._pcm = (b'\x00\x40' * 4800); be2._slot = 1
be2.get_audio(CH)
be2.gateway.announcer_source.speaking = True
n = 0
while peak(be2.get_audio(CH)[0]) > 0.26 and n < 200:
    n += 1
check("attack ~0.25s", 3 <= n <= 8, f"{n} chunks = {n*0.05:.2f}s")

# Hold: stopping speech must NOT immediately release.
ge.announcer_source.speaking = False
held = peak(be.get_audio(CH)[0])
check("holds down right after speech ends", held < 0.30, f"{held:.3f}")

# Release is slower than attack.
rel = 0
while peak(be.get_audio(CH)[0]) < 0.98 and rel < 400:
    rel += 1
check("release slower than attack", rel > n, f"release {rel} chunks vs attack {n}")
check("returns to full level", abs(peak(be.get_audio(CH)[0]) - 1.0) < 0.02)

print("\n14. envelope is ramped within the chunk (no zipper)")
be3 = audio_sources.BGMSource(env_gw())
be3._pcm = (b'\x00\x40' * 4800); be3._slot = 1
be3.get_audio(CH)
be3.gateway.announcer_source.speaking = True
chunk = be3.get_audio(CH)[0]
a = _np.abs(_np.frombuffer(chunk, dtype=_np.int16).astype(_np.float32))
check("gain varies across the chunk, not one step", a[0] > a[-1] + 10, f"{a[0]:.0f} -> {a[-1]:.0f}")
steps = _np.abs(_np.diff(a))
check("no discontinuity inside the chunk", steps.max() < 40, f"max step {steps.max():.1f}")

check("stop() resets the envelope", (be3.stop(), be3._duck_gain)[1] == 1.0)
check("ducking flag exposed for the UI", audio_sources.BGMSource(env_gw()).ducking is False)

print("\n15. BGM runtime cap")


class _Ann2:
    def __init__(self): self.speaking=False; self.enabled=True
    def is_active(self): return self.speaking
    def set_enabled(self, on, interval=None): self.enabled = bool(on)


def cap_gw(maxs):
    g = fake_gw(BGM_DUCK_DB=-12.0, BGM_DUCK_ATTACK=0.25, BGM_DUCK_HOLD=0.4,
                BGM_DUCK_RELEASE=1.2, BGM_MAX_SECONDS=maxs)
    g.announcer_source = _Ann2()
    return g


CH2 = 2400          # 50 ms chunks
gcap = cap_gw(1.0)  # 1 second cap = 20 chunks
bc = audio_sources.BGMSource(gcap)
bc._pcm = b'\x00\x40' * 4800
bc._slot = 1
n = 0
while bc.get_audio(CH2)[0] is not None and n < 100:
    n += 1
check("stops at the cap", 19 <= n <= 21, f"{n} chunks = {n*0.05:.2f}s (want ~1.0s)")
check("bed cleared", bc.is_active() is False)
check("playing_slot cleared", bc.playing_slot is None)
check("announcer silenced too", gcap.announcer_source.enabled is False)
check("silent afterwards", bc.get_audio(CH2)[0] is None)

print("\n16. cap edge cases")
g0 = cap_gw(0)      # 0 = no cap
b0 = audio_sources.BGMSource(g0); b0._pcm = b'\x00\x40' * 4800; b0._slot = 1
for _ in range(200):
    b0.get_audio(CH2)
check("0 means run for ever", b0.is_active() is True)
gbad = cap_gw('nonsense')
check("junk falls back to 120s", audio_sources.BGMSource(gbad).max_secs == 120.0,
      str(audio_sources.BGMSource(gbad).max_secs))
check("default is 120s", audio_sources.BGMSource(fake_gw()).max_secs == 120.0)
gneg = cap_gw(-5)
check("negative clamps to 0 (no cap)", audio_sources.BGMSource(gneg).max_secs == 0.0)

# The cap must measure THIS bed, not total uptime since boot.
gr = cap_gw(1.0)
br = audio_sources.BGMSource(gr)
br.gateway.playback_source = types.SimpleNamespace(
    announcement_directory=_d, _decode_file=lambda p, normalize=True: b'\x00\x40' * 4800)
br.play(1, os.path.join(_d, 'bgm1.mp3'))
for _ in range(10):
    br.get_audio(CH2)          # half the cap
br.play(2, os.path.join(_d, 'bgm2.mp3'))   # switching resets the clock
n2 = 0
while br.get_audio(CH2)[0] is not None and n2 < 100:
    n2 += 1
check("switching beds restarts the clock", 19 <= n2 <= 21, f"{n2} chunks")

print("\n17. remaining time is reported for the UI")
gu = cap_gw(10.0)
bu = audio_sources.BGMSource(gu); bu._pcm = b'\x00\x40' * 4800; bu._slot = 1
for _ in range(20):
    bu.get_audio(CH2)          # 1 second in
st = [x for x in bu.bgm_state() if x['playing']][0]
check("remaining counts down", 8.5 < st['remaining'] < 9.5, str(st['remaining']))
idle = [x for x in bu.bgm_state() if not x['playing']][0]
check("idle beds report no remaining", idle['remaining'] is None, str(idle['remaining']))
gnc = cap_gw(0)
bnc = audio_sources.BGMSource(gnc); bnc._pcm = b'\x00\x40'*4800; bnc._slot = 1
st2 = [x for x in bnc.bgm_state() if x['playing']][0]
check("no cap reports no remaining", st2['remaining'] is None, str(st2['remaining']))

print("\n18. max_seconds persists")
announcer._PATH = os.path.join(mkdtemp('bgm-test-'), 'announcer.json')
check("default 120", announcer.load()['max_seconds'] == 120.0)
announcer.save({'messages': {}, 'voices': {}, 'interval': 10.0,
                'max_seconds': 45.0, 'voice': '', 'enabled': False})
check("round-trips", announcer.load()['max_seconds'] == 45.0)
announcer.save({'messages': {}, 'voices': {}, 'interval': 10.0,
                'max_seconds': 0, 'voice': '', 'enabled': False})
check("0 survives (not coerced to the default)", announcer.load()['max_seconds'] == 0.0,
      str(announcer.load()['max_seconds']))
announcer.save({'messages': {}, 'voices': {}, 'interval': 10.0,
                'max_seconds': 'x', 'voice': '', 'enabled': False})
check("junk falls back to 120", announcer.load()['max_seconds'] == 120.0)

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
