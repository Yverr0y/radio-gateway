"""Verify hot-swapping the TTS engine."""
import io
import json
import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import gateway_setup  # noqa: E402
import web_routes_automation as wa  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


def new_gw(backend='kokoro'):
    gw = types.SimpleNamespace()
    gw.config = types.SimpleNamespace(TTS_ENGINE=backend, ENABLE_TTS=True)
    gw._tts_lock = threading.Lock()
    gw.tts_engine = None
    gw._tts_backend = backend
    gw._kokoro_instance = None
    return gw


print("\n1. engine table is coherent")
check("three engines", gateway_setup.TTS_ENGINES == ('kokoro', 'edge', 'gtts'),
      str(gateway_setup.TTS_ENGINES))
check("every engine has a label",
      all(e in gateway_setup.TTS_ENGINE_LABELS for e in gateway_setup.TTS_ENGINES))

print("\n2. switching to a real engine works")
gw = new_gw('kokoro')
ok, msg = gateway_setup.apply_tts_engine(gw, 'edge')
check("edge switch ok", ok, msg)
check("backend updated", gw._tts_backend == 'edge', gw._tts_backend)
check("engine object published", gw.tts_engine is not None)

ok, msg = gateway_setup.apply_tts_engine(gw, 'gtts')
check("gtts switch ok", ok, msg)
check("backend updated", gw._tts_backend == 'gtts', gw._tts_backend)

print("\n3. an unknown engine is rejected and changes nothing")
before = (gw._tts_backend, gw.tts_engine)
ok, msg = gateway_setup.apply_tts_engine(gw, 'banjo')
check("rejected", ok is False)
check("message names the valid set", 'kokoro' in msg and 'edge' in msg, msg)
check("live engine untouched", (gw._tts_backend, gw.tts_engine) == before)

print("\n4. a FAILED switch must not drop TTS (the important one)")
gw2 = new_gw('gtts')
gateway_setup.apply_tts_engine(gw2, 'gtts')
good = (gw2._tts_backend, gw2.tts_engine)

# Drive the REAL apply_tts_engine into its failure branch by making the model
# files look absent, rather than stubbing the function out (which would assert
# nothing about the code under test).
_real_exists = os.path.exists
os.path.exists = lambda p: False if 'models/kokoro' in p.replace('\\', '/') else _real_exists(p)
try:
    ok, msg = gateway_setup.apply_tts_engine(gw2, 'kokoro')
finally:
    os.path.exists = _real_exists
check("switch reported failure", ok is False, msg)
check("failure names the cause", 'missing' in msg.lower(), msg)
check("previous engine still live", (gw2._tts_backend, gw2.tts_engine) == good,
      f"{gw2._tts_backend}")
check("tts_engine is not None", gw2.tts_engine is not None)

print("\n5. case/whitespace tolerance")
gw3 = new_gw()
ok, _ = gateway_setup.apply_tts_engine(gw3, '  EDGE ')
check("' EDGE ' accepted", ok and gw3._tts_backend == 'edge', gw3._tts_backend)

print("\n6. kokoro instance is reused, not reloaded on every switch")
gw4 = new_gw()
sentinel = object()
gw4._kokoro_instance = sentinel
ok, _ = gateway_setup.apply_tts_engine(gw4, 'kokoro')
check("reused the cached instance", ok and gw4.tts_engine is sentinel)

print("\n7. the swap is atomic under the lock")
gw5 = new_gw()
gateway_setup.apply_tts_engine(gw5, 'edge')
seen = []
stop = threading.Event()


def reader():
    # Mirrors speak_text's snapshot: the pair must always be consistent.
    while not stop.is_set():
        with gw5._tts_lock:
            seen.append((gw5._tts_backend, gw5.tts_engine))


t = threading.Thread(target=reader, daemon=True)
t.start()
for _ in range(30):
    gateway_setup.apply_tts_engine(gw5, 'gtts')
    gateway_setup.apply_tts_engine(gw5, 'edge')
stop.set()
t.join(timeout=5)
import edge_tts  # noqa: E402
from gtts import gTTS  # noqa: E402
expected = {('edge', edge_tts), ('gtts', gTTS)}
bad = [p for p in seen if p not in expected]
check(f"no torn (backend, engine) pair in {len(seen)} reads", not bad, str(bad[:2]))

print("\n8. HTTP endpoint")


class P:
    def __init__(self, gw):
        self.gateway = gw
        self.config = gw.config
        self.saved = {}
        self.config.load_config = lambda: None

    def _save_config(self, v):
        self.saved.update(v)


class H:
    def __init__(self, cmd, body=b''):
        self.command = cmd
        self.headers = {'Content-Length': str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()

    def send_response(self, c): pass
    def send_header(self, *a): pass
    def end_headers(self): pass


gw6 = new_gw()
gateway_setup.apply_tts_engine(gw6, 'edge')
p = P(gw6)
h = H('GET')
wa.handle_tts_engine(h, p)
d = json.loads(h.wfile.getvalue())
check("GET ok", d['ok'])
check("lists all three", len(d['engines']) == 3, str(len(d['engines'])))
check("marks the active one", [e['value'] for e in d['engines'] if e['active']] == ['edge'])
check("reports availability", all('available' in e for e in d['engines']))

h = H('POST', json.dumps({'engine': 'gtts'}).encode())
wa.handle_tts_engine(h, p)
d = json.loads(h.wfile.getvalue())
check("POST switched", d['ok'] and d['active'] == 'gtts', str(d.get('active')))
check("persisted to config", p.saved.get('TTS_ENGINE') == 'gtts', str(p.saved))

h = H('POST', json.dumps({'engine': 'banjo'}).encode())
wa.handle_tts_engine(h, p)
d = json.loads(h.wfile.getvalue())
check("bad engine -> ok False", d['ok'] is False)
check("still on gtts", gw6._tts_backend == 'gtts', gw6._tts_backend)
check("config NOT rewritten on failure", p.saved.get('TTS_ENGINE') == 'gtts')

h = H('POST', b'{bad json')
wa.handle_tts_engine(h, p)
check("malformed body handled", json.loads(h.wfile.getvalue())['ok'] is False)

print("\n9. voice index resolution (the 'every edge voice sounds the same' bug)")
import text_commands  # noqa: E402
import core.setup_audio_mumble as sm  # noqa: E402

EDGE = sm._AudioMumbleMixin.EDGE_TTS_VOICES if hasattr(sm, '_AudioMumbleMixin') else None
if EDGE is None:
    import gateway_core
    EDGE = gateway_core.RadioGateway.EDGE_TTS_VOICES
    GTTS = gateway_core.RadioGateway.TTS_VOICES

gwv = types.SimpleNamespace(config=types.SimpleNamespace(TTS_DEFAULT_VOICE=1))
R = text_commands._resolve_voice_index

check("web sends '12' (string) -> 12", R(gwv, '12', EDGE) == 12, str(R(gwv, '12', EDGE)))
check("int 12 -> 12", R(gwv, 12, EDGE) == 12)
check("' 47 ' -> 47", R(gwv, ' 47 ', EDGE) == 47, str(R(gwv, ' 47 ', EDGE)))
check("None -> configured default", R(gwv, None, EDGE) == 1)
check("out-of-range -> default", R(gwv, '999', EDGE) == 1, str(R(gwv, '999', EDGE)))
check("stale kokoro id -> default", R(gwv, 'af_heart', EDGE) == 1)
check("True is not voice 1", R(gwv, True, EDGE) == 1)
gbad = types.SimpleNamespace(config=types.SimpleNamespace(TTS_DEFAULT_VOICE='oops'))
check("non-numeric config default survives", R(gbad, None, EDGE) == 1)

# THE regression: distinct selections must map to DISTINCT Microsoft voices.
picked = {}
for idx in ('1', '10', '19', '21', '47'):
    n = R(gwv, idx, EDGE)
    picked[idx] = EDGE[n][0]
check("distinct picks -> distinct voices", len(set(picked.values())) == len(picked), str(picked))
check("'10' really is Ana (Cartoon)", picked['10'] == 'en-US-AnaNeural', picked['10'])
check("'19' really is Christopher", picked['19'] == 'en-US-ChristopherNeural', picked['19'])
# The old expression, for contrast — everything collapsed onto voice 1.
old = lambda v: v if isinstance(v, int) else 1
check("old code collapsed all web picks to one voice",
      len({EDGE[old(i)][0] for i in ('1','10','19','21','47')}) == 1)

print(f"\n{'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
