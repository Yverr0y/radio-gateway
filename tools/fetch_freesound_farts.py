#!/usr/bin/env python3
"""Bulk-download CC0 fart sounds from Freesound into audio/farts/.

CC0 only → public domain, no attribution required (CREDITS.txt kept for
provenance anyway). Downloads the high-quality preview MP3 for each (works with
just the API token; original-file download would need OAuth2).

Token: env FREESOUND_API_KEY, or FREESOUND_API_KEY line in gateway_config.txt.
Usage: python3 tools/fetch_freesound_farts.py [max_count]
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(BASE, 'audio', 'farts')


def get_token():
    t = os.environ.get('FREESOUND_API_KEY')
    if t:
        return t.strip()
    cfg = os.path.join(BASE, 'gateway_config.txt')
    if os.path.exists(cfg):
        for line in open(cfg):
            if line.strip().startswith('FREESOUND_API_KEY'):
                return line.split('=', 1)[1].strip()
    raise SystemExit('No FREESOUND_API_KEY (set env or gateway_config.txt)')


def _api(url, token):
    req = urllib.request.Request(url, headers={'Authorization': 'Token ' + token})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def search(token, query='fart', max_items=2000, min_dur=0.2, max_dur=10.0,
           cc0_only=False):
    items, seen = [], set()
    url = ('https://freesound.org/apiv2/search/text/?query=' + query +
           '&fields=id,name,license,username,previews,duration'
           '&page_size=150&sort=score')
    if cc0_only:
        url += '&filter=' + urllib.parse.quote('license:"Creative Commons 0"')
    while url and len(items) < max_items:
        d = _api(url, token)
        for r in d.get('results', []):
            dur = r.get('duration') or 0
            pv = (r.get('previews') or {}).get('preview-hq-mp3')
            if pv and min_dur <= dur <= max_dur and r['id'] not in seen:
                seen.add(r['id'])
                items.append(r)
        url = d.get('next')
        time.sleep(0.1)
    return items[:max_items]


def download(item, token):
    dst = os.path.join(DEST, 'fs_%d.mp3' % item['id'])
    if os.path.exists(dst):
        return 'skip'
    try:
        req = urllib.request.Request(item['previews']['preview-hq-mp3'],
                                     headers={'Authorization': 'Token ' + token})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        tmp = dst + '.partial'
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, dst)
        return 'ok'
    except Exception as e:
        sys.stderr.write('  fail %s: %s\n' % (item['id'], e))
        return 'fail'


def main():
    token = get_token()
    os.makedirs(DEST, exist_ok=True)
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    cc0_only = '--cc0' in sys.argv
    print('Searching Freesound for farts (%s)…'
          % ('CC0 only' if cc0_only else 'all licenses'))
    items = search(token, max_items=target, cc0_only=cc0_only)
    print('%d fart sounds found, downloading (8 workers)…' % len(items))

    counts = {'ok': 0, 'skip': 0, 'fail': 0}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for st in ex.map(lambda it: download(it, token), items):
            counts[st] += 1

    with open(os.path.join(DEST, 'CREDITS.txt'), 'w') as f:
        f.write('Fart samples from Freesound.org.\n'
                'CC0 sounds are public domain (no attribution required); all\n'
                'other Creative Commons licenses below REQUIRE crediting the\n'
                'author — this file satisfies that. Format: file  license  by author  url\n\n')
        for it in items:
            f.write('fs_%d.mp3  [%s]  by %s  https://freesound.org/s/%d/\n'
                    % (it['id'], it.get('license', '?'), it['username'], it['id']))

    total = len([x for x in os.listdir(DEST)
                 if x.lower().endswith(('.mp3', '.wav', '.ogg', '.flac'))])
    print('ok=%(ok)d skip=%(skip)d fail=%(fail)d' % counts)
    print('total farts now in audio/farts/: %d' % total)


if __name__ == '__main__':
    main()
