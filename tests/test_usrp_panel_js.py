"""The USRP panel must emit JavaScript that actually parses.

_PANEL_HTML is a NON-RAW Python triple-quoted string, so every backslash in it
is a Python escape first and a JavaScript one second. On 2026-08-03 an inline
`onclick="pick(\'...\')"` became `onclick="pick(''...'')"` by the time it
reached the browser — a SyntaxError that killed the entire panel script, so no
button on either AllStar page did anything and nothing was logged server-side.

So: render through the REAL code path (_render_panel, including the usrp2
rewrites) and parse the result. Extracting the template by hand and tidying up
the backslashes is what hid the bug the first time.
"""
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'plugins'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _tmpdirs import mkdtemp  # noqa: E402
import usrp   # noqa: E402
import usrp2  # noqa: E402

FAIL = []





def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


print("\n=== USRP panel JS ===\n")

_node = shutil.which('node')
_tmp = mkdtemp('panel-js-')

for cls, label in ((usrp.UsrpPlugin, 'usrp'), (usrp2.Usrp2Plugin, 'usrp2')):
    p = cls()
    p.node = '683970'
    html = p._render_panel()

    print(f"{label}:")
    check("placeholders substituted",
          '__NODE__' not in html and '__LABEL__' not in html)

    # The bug signature: a JS string literal closed immediately by another
    # quote with no operator between them, from a Python-eaten backslash.
    mangled = re.findall(r"onclick=\"[a-zA-Z_]+\(''", html)
    check("no Python-eaten quote escapes", not mangled, str(mangled[:2]))

    scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
    check("panel has a script block", bool(scripts))
    js = max(scripts, key=len) if scripts else ''

    if _node:
        path = os.path.join(_tmp, f'{label}.js')
        with open(path, 'w') as f:
            f.write(js)
        r = subprocess.run([_node, '--check', path],
                           capture_output=True, text=True, timeout=60)
        err = (r.stderr or '').strip().splitlines()
        check("JavaScript parses", r.returncode == 0,
              err[-1] if r.returncode and err else '')
    else:
        print("  SKIP  JavaScript parses — node not installed")

    # Row alignment: the buttons must sit in their own grid column rather
    # than trailing a variable-length name.
    check("arow grid rule present", '.links li.arow' in html and 'display:grid' in html)
    check("three fixed columns", 'grid-template-columns:5.5rem minmax(0,1fr) auto' in html)
    for cls in ('arow', 'nodenum', 'nm', 'acts'):
        check(f"rows emit .{cls}", f'"{cls}"' in js or f'class="{cls}' in js)

    # Stats link on each saved-node row, opening in a new tab safely.
    check("stats URL present", 'https://stats.allstarlink.org/stats/' in js)
    # Both node lists link out — saved nodes AND kept-connected rows. Each
    # renderer builds its own row markup, so one can gain the link and the
    # other silently not.
    # Every node number on the page links out: saved nodes, kept-connected,
    # your direct links, conference members, the reconnect-stats rows, and
    # the header. Each builds its own markup, so one can silently miss out.
    check("all five renderers use statsLink",
          js.count('statsLink(') >= 6, f"{js.count('statsLink(')} refs (1 def + 5 uses)")
    check("header node number links too",
          re.search(r'<h1>.*?stats\.allstarlink\.org.*?</h1>', html, re.S) is not None)
    # A bare interpolated node number left anywhere means a missed spot.
    bare = re.findall(r'<span>\$\{(?:d\.node|n)\}', js)
    check("no un-linked node interpolations", not bare, str(bare))
    check("node appended to the URL", 'STATS_URL+encodeURIComponent' in js)
    check("opens a new tab", 'target="_blank"' in js)
    check("noopener set", 'noopener' in js)

    # The panel is standalone and does not load common.css, so every theme
    # var it uses must be defined here or the colour silently does nothing.
    used = set(re.findall(r'var\((--t-[a-z]+)\)', html))
    declared = set(re.findall(r'(--t-[a-z]+)\s*:', html))
    check("all theme vars declared", used <= declared, f"missing {sorted(used - declared)}")

    # The input cap and the server-side cap must not drift apart: a longer
    # name would be silently truncated on save, which reads as a bug.
    m = re.search(r'id="nodename"[^>]*maxlength="(\d+)"', html)
    check("name maxlength matches MAX_NAME",
          m is not None and int(m.group(1)) == usrp._NodeBook.MAX_NAME,
          f"input={m.group(1) if m else '?'} server={usrp._NodeBook.MAX_NAME}")

    # usrp2 must talk to its own endpoints or its buttons silently drive AS1.
    if label == 'usrp2':
        check("control URL rewritten", "'/usrp2/control'" in js)
        check("status URL rewritten", "'/usrp2/status'" in js)
        check("no bare /usrp/ URLs left", "'/usrp/control'" not in js
              and "'/usrp/status'" not in js)
    print()

print(f"{'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILURES: ' + ', '.join(FAIL)}\n")
sys.exit(1 if FAIL else 0)
