"""The /voice Claude session clears its context once it has gone idle.

Why this exists: /voice is genuinely conversational, so unlike the manager runs
it cannot be made one-shot. But it is never resumed across a gap -- nobody picks
up a two-day-old spoken thread -- while its context is re-sent and re-cached on
every turn for as long as the session lives. The gateway's long-lived sessions
grew to 400k tokens of context and paid ~370k cache-write tokens per wake-up.

The clear must never land mid-response, which is what the empty-input-line check
guards: a pane that is still answering has no empty prompt line. An unreadable
pane counts as busy, so a capture failure skips the round instead of guessing.

Covers: the idle clear firing, every reason it declines to fire, no repeat clear
until there is new activity, and start/restart/stop marking the session clean.
"""
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import web_routes_voice as V

IDLE_SECS = V._VOICE_IDLE_CLEAR_MIN * 60

BUSY   = 'Reading logs...\n  esc to interrupt\n'
IDLE   = '❯ \n'
TYPING = '❯ what is the stream doing\n'


def fake_tmux(pane, has_session=True, capture_fails=False):
    sent = []

    def run(cmd, **kw):
        if 'has-session' in cmd:
            return subprocess.CompletedProcess(cmd, 0 if has_session else 1, '', '')
        if 'capture-pane' in cmd:
            if capture_fails:
                raise OSError('pane gone')
            return subprocess.CompletedProcess(cmd, 0, pane, '')
        if 'send-keys' in cmd:
            sent.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, '', '')

    return run, sent


def tick(pane=IDLE, idle_for=IDLE_SECS + 60, cleared=False, **kw):
    V._voice_last_activity = __import__('time').time() - idle_for
    V._voice_cleared = cleared
    run, sent = fake_tmux(pane, **kw)
    real, V.subprocess.run = V.subprocess.run, run
    try:
        return V._voice_idle_tick('claude-voice'), sent
    finally:
        V.subprocess.run = real


print(f'--- idle threshold: {V._VOICE_IDLE_CLEAR_MIN:.0f} min ---')
for label, kwargs in (
    ('idle, prompt empty',            dict()),
    ('idle but still responding',     dict(pane=BUSY)),
    ('idle but user has typed',       dict(pane=TYPING)),
    ('active within the window',      dict(idle_for=60)),
    ('already cleared',               dict(cleared=True)),
    ('session gone',                  dict(has_session=False)),
    ('pane unreadable',               dict(capture_fails=True)),
):
    reason, sent = tick(**kwargs)
    print(f"{label:>30}: {reason:<17} sent={sent}")

print()
print('--- clears once, then re-arms on new activity ---')
V._voice_last_activity = __import__('time').time() - (IDLE_SECS + 60)
V._voice_cleared = False
run, sent = fake_tmux(IDLE)
real, V.subprocess.run = V.subprocess.run, run
try:
    first  = V._voice_idle_tick('claude-voice')
    second = V._voice_idle_tick('claude-voice')
    V._note_voice_activity()
    third  = V._voice_idle_tick('claude-voice')          # fresh activity, not idle yet
    V._voice_last_activity -= (IDLE_SECS + 60)
    fourth = V._voice_idle_tick('claude-voice')
finally:
    V.subprocess.run = real
print(f"{'first pass':>30}: {first}")
print(f"{'immediately again':>30}: {second}")
print(f"{'after a new message':>30}: {third}")
print(f"{'once idle again':>30}: {fourth}")
print(f"{'total /clear commands':>30}: {sent.count('/clear')}")

print()
print('--- a fresh session holds nothing worth clearing ---')
V._note_voice_activity()
V._mark_voice_fresh()
reason, sent = tick(cleared=V._voice_cleared)
print(f"{'after start/restart/stop':>30}: {reason:<17} sent={sent}")

print()
print('--- disabled via VOICE_IDLE_CLEAR_MIN=0 ---')
saved, V._VOICE_IDLE_CLEAR_MIN = V._VOICE_IDLE_CLEAR_MIN, 0
V._voice_last_activity = V._voice_cleared = 0
V._note_voice_activity()
print(f"{'activity never arms watcher':>30}: last_activity={V._voice_last_activity}")
V._VOICE_IDLE_CLEAR_MIN = saved
