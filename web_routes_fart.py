"""POST route handlers for the real-sample fart machine.

  POST /fart/preview  {variation}  -> audio/wav  (browser audition, no RF)
  POST /fart/send     {variation}  -> {"ok":true} (TX a random real fart)

Each call picks a fresh random fart (mash the button for variety). Plays through
the same priority-0, PTT-keying playback path as the soundboard.
"""

import json as _json

import fart_player


def _read(handler):
    try:
        n = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(n).decode('utf-8') if n else '{}'
        d = _json.loads(body) if body.strip() else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _variation(d):
    try:
        return float(d.get('variation', 0.12))
    except (TypeError, ValueError):
        return 0.12


def _send_json(handler, obj, code=200):
    body = _json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def handle_fart_preview(handler, parent):
    """Audition a random real fart in the browser (no RF)."""
    try:
        pcm, name = fart_player.random_fart(_variation(_read(handler)))
        wav = fart_player.to_wav_bytes(pcm)
    except Exception as e:
        _send_json(handler, {'ok': False, 'error': str(e)}, code=500)
        return
    handler.send_response(200)
    handler.send_header('Content-Type', 'audio/wav')
    handler.send_header('Content-Length', str(len(wav)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Fart-Name', name)
    handler.end_headers()
    handler.wfile.write(wav)


def handle_fart_send(handler, parent):
    """Transmit a random real fart over the radio path."""
    gw = getattr(parent, 'gateway', None)
    if gw is None or not getattr(gw, 'playback_source', None):
        _send_json(handler, {'ok': False, 'error': 'playback not available'}, code=503)
        return
    try:
        pcm, name = fart_player.random_fart(_variation(_read(handler)))
        import text_commands
        vol = getattr(gw.config, 'PLAYBACK_VOLUME', 4.0)
        text_commands.trigger_playback(
            gw,
            lambda pb, _b=pcm: pb.queue_pcm(_b, name="fart:" + name),
            label="fart:" + name,
            volume=vol,
        )
    except Exception as e:
        _send_json(handler, {'ok': False, 'error': str(e)}, code=500)
        return
    _send_json(handler, {'ok': True, 'fart': name})
