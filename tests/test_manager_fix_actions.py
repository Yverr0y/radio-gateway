"""Verify the rewritten fleet-manager fix actions."""
import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import manager_engine  # noqa: E402

ME = manager_engine.ManagerEngine
FAIL = []
sent = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def new_engine(gateway=None):
    e = object.__new__(ME)
    e.config = types.SimpleNamespace(TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID='')
    e.gateway = gateway
    e._lock = threading.Lock()
    e._state = {}
    e._save_state = lambda: None
    e._telegram_send = lambda text, note: sent.append(text)
    return e


print("\n1. restart-stream targets stream_output, not darkice")
calls = []


class FakeStream:
    connected = False

    def reconnect(self):
        calls.append('reconnect')
        self.connected = True


gw = types.SimpleNamespace(stream_output=FakeStream())
e = new_engine(gw)
ok, detail = e._fix_restart_stream()
check("calls stream_output.reconnect()", calls == ['reconnect'], f"calls={calls}")
check("reports success when reconnected", ok is True, detail)

print("\n2. restart-stream reports failure honestly when it does not recover")


class DeadStream:
    connected = False
    _last_error = 'broken pipe'

    def reconnect(self):
        pass


e = new_engine(types.SimpleNamespace(stream_output=DeadStream()))
ok, detail = e._fix_restart_stream()
check("does not claim success", ok is False)
check("surfaces the underlying error", 'broken pipe' in detail, detail)

print("\n3. missing stream_output is reported, not crashed on")
e = new_engine(types.SimpleNamespace())
ok, detail = e._fix_restart_stream()
check("returns a clean failure", ok is False and 'no stream_output' in detail, detail)

print("\n4. unit fixes use sudo -n and surface stderr")
e = new_engine()
ok, detail = e._fix_restart_unit('definitely-not-a-real-unit.service')
check("fails for a nonexistent unit", ok is False)
check("stderr is surfaced, not swallowed",
      'not found' in detail.lower(), detail)
check("no longer an auth failure",
      'interactive authentication' not in detail.lower(), detail)

print("\n5. deprecated restart-darkice is remapped, not dropped")
e = new_engine(types.SimpleNamespace(stream_output=FakeStream()))
seen = {}
e._fix_restart_stream = lambda: (seen.setdefault('stream', True), (True, 'ok'))[1]
entry = {'ts': 'now', 'summary': 's', 'findings': []}
e._send_fix_telegram = lambda f, en: None
e._send_fix_result_telegram = lambda f, o, d: sent.append(f'result:{f}:{o}')
e._apply_fix('restart-darkice', entry)
check("aliased to the stream fix", seen.get('stream') is True)

print("\n6. a failed fix annotates the report and keeps the alert unread")
e = new_engine(types.SimpleNamespace())
e._send_fix_telegram = lambda f, en: None
results = []
e._send_fix_result_telegram = lambda f, o, d: results.append((f, o, d))
entry = {'ts': 'now', 'summary': 's', 'findings': []}
e._apply_fix('restart-stream', entry)
check("outcome telegram says FAILED", results and results[0][1] is False, str(results))
check("AUTO-FIX FAILED added to findings",
      any('AUTO-FIX FAILED' in f for f in entry['findings']), str(entry['findings']))
check("alert left unread", e._state.get('unread_alerts') is True)

print("\n7. unknown fixes still report rather than silently vanishing")
e = new_engine()
e._send_fix_telegram = lambda f, en: None
res = []
e._send_fix_result_telegram = lambda f, o, d: res.append((f, o, d))
e._apply_fix('restart-teapot', {'ts': 'now', 'summary': '', 'findings': []})
check("reported as failed", res and res[0][1] is False, str(res))

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
