"""Playback slot count, reserved files, and the test-loop state machine."""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from _tmpdirs import mkdtemp  # noqa: E402
import audio_sources  # noqa: E402



FP = audio_sources.FilePlaybackSource
FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def make_src(slots=20, files=(), enable_soundboard=False):
    d = mkdtemp('playback-test-')
    for f in files:
        open(os.path.join(d, f), 'wb').write(b'\0' * 16)
    s = object.__new__(FP)
    s.config = types.SimpleNamespace(
        PLAYBACK_SLOTS=slots, PLAYBACK_DIRECTORY=d, ENABLE_SOUNDBOARD=enable_soundboard,
        PLAYBACK_ANNOUNCEMENT_INTERVAL=0, VERBOSE_LOGGING=False,
        SOUNDBOARD_CATEGORIES='', SOUNDBOARD_MAX_SECONDS=15)
    s.gateway = types.SimpleNamespace(config=s.config)
    s.announcement_directory = d
    s.slot_count = max(1, min(99, int(slots)))
    s.file_status = {k: {'exists': False, 'playing': False, 'path': None}
                     for k in s.slot_keys(include_station_id=True)}
    s._loop_active = False
    return s


print("\n1. slot count is configurable")
for n in (9, 20, 1):
    s = make_src(slots=n)
    check(f"{n} slots -> {n} numbered keys + station id",
          s.slot_keys() == [str(i) for i in range(1, n + 1)] and
          s.slot_keys(include_station_id=True)[-1] == '0',
          f"{len(s.slot_keys())} keys")
s = make_src(slots=500)
check("absurd values are clamped", s.slot_count == 99, str(s.slot_count))

print("\n2. loop.mp3 must NOT occupy a playback slot (the reported bug)")
s = make_src(slots=20, files=['loop.mp3', 'station_id.wav', 'horn.mp3', 'siren.mp3'])
s.check_file_availability()
names = {k: v.get('filename') for k, v in s.file_status.items() if v['exists']}
check("loop.mp3 is in no numbered slot",
      not any(str(v).startswith('loop.') for k, v in names.items() if k != '0'), str(names))
check("station_id still claims slot 0", names.get('0') == 'station_id.wav', str(names.get('0')))
check("real sounds still load", {'horn.mp3', 'siren.mp3'} <= set(names.values()), str(names))
check("_is_reserved catches loop + station_id",
      FP._is_reserved('loop.mp3') and FP._is_reserved('LOOP.WAV')
      and FP._is_reserved('station_id.wav') and not FP._is_reserved('horn.mp3'))

print("\n3. numeric filename prefixes claim their slot, including multi-digit")
s = make_src(slots=20, files=['3_three.mp3', '12_twelve.mp3', 'zz_other.mp3'])
s.check_file_availability()
check("3_ -> slot 3", s.file_status['3'].get('filename') == '3_three.mp3',
      str(s.file_status['3'].get('filename')))
check("12_ -> slot 12 (would have been impossible with 9 slots)",
      s.file_status['12'].get('filename') == '12_twelve.mp3',
      str(s.file_status['12'].get('filename')))

print("\n4. a prefix beyond the slot count does not crash or vanish")
s = make_src(slots=5, files=['12_twelve.mp3'])
s.check_file_availability()
placed = [k for k, v in s.file_status.items() if v['exists']]
check("still placed in some free slot", placed, str(placed))
check("not placed in a nonexistent slot 12", '12' not in s.file_status)

print("\n5. test loop: explicit start/stop (the 'Stop Loop won't stop it' bug)")
s = make_src(slots=9, files=['loop.mp3'])
s.queue_file = lambda p: None
s.stop_playback = lambda: setattr(s, '_loop_active', False)

r = s.toggle_test_loop('start')
check("start -> looping True", r['ok'] and r['looping'] is True, str(r))
r = s.toggle_test_loop('stop')
check("stop -> looping False", r['ok'] and r['looping'] is False, str(r))

# The exact failure: something else stopped the loop, so a blind toggle would
# have STARTED one. An explicit stop must stay stopped.
s._loop_active = True
s.stop_playback()               # e.g. the user pressed Stop
check("external stop clears the flag", s._loop_active is False)
r = s.toggle_test_loop('stop')  # button still said "Stop Loop"
check("explicit stop is idempotent — does not start a loop",
      r['looping'] is False and s._loop_active is False, str(r))
r = s.toggle_test_loop('toggle')
check("toggle from stopped still starts", r['looping'] is True)

print("\n6. missing loop file must not leave the button asserted")
s2 = make_src(slots=9, files=[])
s2.queue_file = lambda p: None
s2.stop_playback = lambda: None
r = s2.toggle_test_loop('start')
check("reports failure", r['ok'] is False, str(r))
check("looping False in the response", r['looping'] is False)
check("flag not left set", s2._loop_active is False)

print("\n7. loop_active is exposed for the UI to sync from")
s3 = make_src(slots=9, files=['loop.mp3'])
s3.queue_file = lambda p: None
s3.stop_playback = lambda: setattr(s3, '_loop_active', False)
check("property reflects the flag", s3.loop_active is False)
s3.toggle_test_loop('start')
check("property follows a start", s3.loop_active is True)

print("\n8. BGM beds are excluded from numbered slots")
s = make_src(slots=20, files=['bgm1.mp3', 'bgm2.mp3', 'bgm3.mp3', 'horn.mp3', 'loop.mp3'])
s.check_file_availability()
slotted = {v.get('filename') for k, v in s.file_status.items() if v['exists'] and k != '0'}
check("no BGM bed occupies a numbered slot",
      not any(str(n).startswith('bgm') for n in slotted), str(slotted))
check("loop.mp3 still excluded too", 'loop.mp3' not in slotted, str(slotted))
check("ordinary sounds still load", 'horn.mp3' in slotted, str(slotted))

s4 = make_src(slots=9, files=['a.mp3', 'b.mp3'])
s4.config.BGM_FILES = 'a.mp3, b.mp3'
s4.check_file_availability()
slotted4 = {v.get('filename') for k, v in s4.file_status.items() if v['exists'] and k != '0'}
check("custom BGM_FILES are excluded too", not slotted4, str(slotted4))

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
