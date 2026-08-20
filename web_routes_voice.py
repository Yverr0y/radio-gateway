"""POST handlers for the /voice page (talk-to-Claude tmux session)."""

"""POST route handlers extracted from web_server.py."""

import json as json_mod
import os
import time
import subprocess
import threading as _thr

from audio_sources import generate_cw_pcm
from cat_client import RadioCATClient


# ── Idle context clear ────────────────────────────────────────────────────
# /voice is genuinely conversational, so it cannot be made one-shot the way the
# manager runs were. But it is never *resumed* across a gap — nobody picks up a
# two-day-old spoken thread — while its context is re-sent and re-cached on
# every single turn for as long as the session lives. Clearing once it has gone
# idle bounds that growth at no cost the user can perceive.
# Set VOICE_IDLE_CLEAR_MIN=0 to disable.
try:
    _VOICE_IDLE_CLEAR_MIN = float(os.environ.get('VOICE_IDLE_CLEAR_MIN', '20'))
except (TypeError, ValueError):
    _VOICE_IDLE_CLEAR_MIN = 20.0

_voice_lock          = _thr.Lock()
_voice_last_activity = 0.0
_voice_cleared       = True     # a fresh session has nothing worth clearing
_voice_watcher       = None


def _voice_pane_idle(target):
    """True when the Claude TUI input line is on screen and empty.

    This is the guard on the idle clear: a pane that is mid-response has no
    empty prompt line, so `/clear` can never land in the middle of a turn.
    An unreadable pane returns False — skip this round rather than guess.
    """
    try:
        r = subprocess.run(['tmux', 'capture-pane', '-p', '-t', target],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    for line in r.stdout.splitlines():
        ls = line.strip()
        for marker in ('\u276f', '>'):
            if ls.startswith(marker):
                return not ls[len(marker):].strip()
    return False


def _voice_idle_tick(target):
    """One watcher pass. Returns why it did or did not clear (for tests/logs)."""
    global _voice_cleared
    with _voice_lock:
        last, cleared = _voice_last_activity, _voice_cleared
    if cleared or not last:
        return 'nothing-to-clear'
    if (time.time() - last) < _VOICE_IDLE_CLEAR_MIN * 60:
        return 'still-active'
    if subprocess.run(['tmux', 'has-session', '-t', target],
                      capture_output=True).returncode != 0:
        return 'no-session'
    if not _voice_pane_idle(target):
        return 'mid-response'   # retry next minute
    subprocess.run(['tmux', 'send-keys', '-t', target, '-l', '/clear'])
    subprocess.run(['tmux', 'send-keys', '-t', target, 'Enter'])
    with _voice_lock:
        _voice_cleared = True
    print(f"  [Voice] idle {_VOICE_IDLE_CLEAR_MIN:.0f}m \u2014 context cleared")
    return 'cleared'


def _voice_idle_watcher():
    target = os.environ.get('TMUX_TARGET', 'claude-voice')
    while True:
        time.sleep(60)
        try:
            _voice_idle_tick(target)
        except Exception as e:
            print(f"  [Voice] idle watcher error: {e}")


def _note_voice_activity():
    """Mark the session dirty and make sure the idle watcher is running."""
    global _voice_last_activity, _voice_cleared, _voice_watcher
    if _VOICE_IDLE_CLEAR_MIN <= 0:
        return
    with _voice_lock:
        _voice_last_activity = time.time()
        _voice_cleared = False
        if _voice_watcher is None or not _voice_watcher.is_alive():
            _voice_watcher = _thr.Thread(target=_voice_idle_watcher, daemon=True,
                                         name='voice-idle-clear')
            _voice_watcher.start()


def _mark_voice_fresh():
    """A just-started/cleared session holds nothing worth clearing."""
    global _voice_cleared
    with _voice_lock:
        _voice_cleared = True


def handle_voice_send(handler, parent):
    """POST /voice/send"""
    import json as json_mod
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
    except Exception:
        data = {}
    text = data.get('text', '').strip()
    tmux_target = os.environ.get('TMUX_TARGET', 'claude-voice')
    if not text:
        handler.send_response(400)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(b'{"error":"empty text"}')
        return
    chk = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
    if chk.returncode != 0:
        handler.send_response(503)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_mod.dumps({'error': f"tmux session '{tmux_target}' not found"}).encode())
        return
    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', text])
    subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
    _note_voice_activity()
    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps({'ok': True, 'sent': text}).encode())
    return

def handle_voice_session(handler, parent):
    """POST /voice/session"""
    import json as json_mod
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    try:
        data = json_mod.loads(body)
    except Exception:
        data = {}
    action = data.get('action', '')
    tmux_target = 'claude-voice'
    result = {'ok': False}

    if action == 'start':
        # Create session if it doesn't exist, then launch claude
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode == 0:
            result = {'ok': True, 'msg': 'session already exists'}
        else:
            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions --model sonnet --effort medium'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            # Auto-confirm workspace trust prompt after startup
            import time; time.sleep(3)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            _mark_voice_fresh()
            result = {'ok': True, 'msg': 'session created, claude started'}

    elif action == 'restart':
        # Send Ctrl+C to stop current process, wait, then start claude again
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode != 0:
            subprocess.run(['tmux', 'new-session', '-d', '-s', tmux_target, '-c', '/home/user'])
        else:
            # Send Ctrl+C twice to kill any running process
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(1)
            # Clear the screen before starting fresh
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            import time; time.sleep(0.3)
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'claude --dangerously-skip-permissions --model sonnet --effort medium'])
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
        # Auto-confirm workspace trust prompt after startup
        import time; time.sleep(3)
        subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
        _mark_voice_fresh()
        result = {'ok': True, 'msg': 'claude restarted'}

    elif action == 'stop':
        has = subprocess.run(['tmux', 'has-session', '-t', tmux_target], capture_output=True)
        if has.returncode == 0:
            # Send Ctrl+C to stop Claude, clear screen, leave the shell running
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'C-c', ''])
            import time; time.sleep(0.5)
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, '-l', 'clear'])
            subprocess.run(['tmux', 'send-keys', '-t', tmux_target, 'Enter'])
            _mark_voice_fresh()
            result = {'ok': True, 'msg': 'claude stopped'}
        else:
            result = {'ok': True, 'msg': 'session not running'}
    else:
        result = {'ok': False, 'error': 'unknown action'}

    handler.send_response(200)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json_mod.dumps(result).encode())
    return
