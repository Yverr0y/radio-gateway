"""USRP link reconciler + shared node address book.

The reconciler exists because non-permanent AllStar links are never
re-established by app_rpt, so a link dropped overnight was still down in the
morning. The two things worth testing are that it DOES reconnect, and that it
STOPS — an unbounded retry loop against a node that is off the air for a week
is its own kind of broken.
"""
import json
import os
import sys
import tempfile
import types

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'plugins'))
import usrp  # noqa: E402

FAIL = []


def check(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{' — ' + detail if detail else ''}")
    if not cond:
        FAIL.append(name)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def time(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def make_plugin(tmpdir, clock, desired=None):
    p = usrp.UsrpPlugin()
    p.node = '683970'
    p.ami_user = 'gateway'
    p.ami_secret = 'x'
    p._desired_path = os.path.join(tmpdir, 'desired.json')
    p._recent_path = os.path.join(tmpdir, 'recent.json')
    p._desired = dict(desired or {})
    p.RELINK_MAX_FAILS = 3           # keep the test short
    p.sent = []
    p._lstats_ok_mono = clock.monotonic()
    # connect_node settles before verifying; don't actually sleep for it.
    p._stop = types.SimpleNamespace(wait=lambda s: None, is_set=lambda: False)

    def fake_ami(command, timeout=3.0):
        p.sent.append(command)
        return True, 'OK'
    p._ami_command = fake_ami
    return p


def set_links(p, clock, nodes):
    p._links_direct = [{'node': n, 'peer': '1.2.3.4', 'reconnects': 0,
                        'dir': 'OUT', 'ctime': '00:01:00:000',
                        'state': 'ESTABLISHED'} for n in nodes]
    p._lstats_ok_mono = clock.monotonic()


print("\n=== USRP relink reconciler ===\n")

_tmp = tempfile.mkdtemp(prefix='usrp-test-')
_clock = FakeClock()
usrp.time = _clock                     # module-level clock swap (no threads here)
usrp.NODE_BOOK = usrp._NodeBook(os.path.join(_tmp, 'book.json'))

print("1. Connect is PERSISTENT: plain ilink + recorded desired state")
p = make_plugin(_tmp, _clock)
res = p.connect_node('45412')
check("plain ilink 3 issued (not app_rpt permanent)",
      any('ilink 3 45412' in c for c in p.sent)
      and not any('ilink 13' in c for c in p.sent), str(p.sent))
check("reported persistent", res.get('persistent') is True)
check("added to desired", p._desired.get('45412', {}).get('mode') == usrp.ILINK_TRANSCEIVE)
check("desired persisted", os.path.exists(p._desired_path))
p2 = make_plugin(_tmp, _clock)
p2._desired_path = p._desired_path
check("desired survives reload", '45412' in p2._load_desired())

print("\n2. Monitor mode stays monitor (ilink 2), not transceive")
p = make_plugin(_tmp, _clock)
p.connect_node('45412', usrp.ILINK_MONITOR)
check("plain ilink 2 issued", any('ilink 2 45412' in c for c in p.sent)
      and not any('ilink 12' in c for c in p.sent), str(p.sent))

print("\n3. Link present → reconciler does nothing")
p = make_plugin(_tmp, _clock, {'45412': {'mode': 3}})
set_links(p, _clock, ['45412'])
p.sent.clear()
p._reconcile_links()
check("no AMI traffic", p.sent == [], str(p.sent))
check("attempts still 0", p.relink_attempts == 0)

print("\n4. Link missing → exactly ONE attempt, then it waits")
p = make_plugin(_tmp, _clock, {'45412': {'mode': 3}})
set_links(p, _clock, [])
p._reconcile_links()
check("one reconnect issued", len(p.sent) == 1, str(p.sent))
check("used the plain connect mode", any('ilink 3 45412' in c for c in p.sent), str(p.sent))
# Poll repeatedly inside the backoff window — this is the hammering guard.
for _ in range(5):
    _clock.advance(2)
    set_links(p, _clock, [])
    p._reconcile_links()
check("no hammering inside backoff", len(p.sent) == 1, f"{len(p.sent)} sent")

print("\n5. Backoff doubles between attempts")
gaps = []
last = len(p.sent)
for expected in (30, 60):
    _clock.advance(expected + 1)
    set_links(p, _clock, [])
    p._reconcile_links()
    gaps.append(len(p.sent) - last)
    last = len(p.sent)
check("one attempt per elapsed window", gaps == [1, 1], str(gaps))
check("attempts counted", p.relink_attempts == 3, str(p.relink_attempts))

print("\n6. Backoff exhausts into dormant, not into silence")
_clock.advance(200)
set_links(p, _clock, [])
p._reconcile_links()                     # third failure -> give up
rep = {r['node']: r for r in p.relink_report()}
check("state is dormant", rep['45412']['state'] == 'dormant', rep['45412']['state'])
check("dormant counted", p.relink_dormant == 1, str(p.relink_dormant))
sent_at_dormant = len(p.sent)
for _ in range(20):                      # a long outage, hourly cadence
    _clock.advance(3600)
    set_links(p, _clock, [])
    p._reconcile_links()
extra = len(p.sent) - sent_at_dormant
check("keeps trying slowly", extra > 0, f"{extra} in 20h")
check("does not hammer", extra <= 21, f"{extra} in 20h")

print("\n7. A manual connect resets dormant to full speed")
p.connect_node('45412')
rep = {r['node']: r for r in p.relink_report()}
check("state reset to ok", rep['45412']['state'] == 'ok', rep['45412']['state'])
check("fails reset", rep['45412']['fails'] == 0)
p.sent.clear()
_clock.advance(3600)
set_links(p, _clock, [])
p._reconcile_links()
check("retries resume", len(p.sent) == 1, str(len(p.sent)))

print("\n8. Recovery resets the backoff and is reported")
set_links(p, _clock, ['45412'])
_clock.advance(1)
p._reconcile_links()
rep = {r['node']: r for r in p.relink_report()}
check("state ok after relink", rep['45412']['state'] == 'ok')
check("recovery counted", p.relink_recovered >= 1, str(p.relink_recovered))

print("\n9. Disconnect leaves the desired set (no reconciler race)")
p = make_plugin(_tmp, _clock, {'45412': {'mode': 3}})
set_links(p, _clock, ['45412'])
p.disconnect_node('45412')
check("removed from desired", '45412' not in p._desired)
check("perma-disconnect issued", any('ilink 11 45412' in c for c in p.sent), str(p.sent))
p.sent.clear()
set_links(p, _clock, [])
_clock.advance(3600)
p._reconcile_links()
check("not reconnected after disconnect", p.sent == [], str(p.sent))

print("\n10. disconnect_all clears every desired link")
p = make_plugin(_tmp, _clock, {'45412': {'mode': 3}, '41413': {'mode': 3}})
p.disconnect_all()
check("desired emptied", p._desired == {}, str(p._desired))
check("perma-cleared per node", sum('ilink 11' in c for c in p.sent) == 2, str(p.sent))

print("\n11. A stale link list must not trigger reconnects")
p = make_plugin(_tmp, _clock, {'45412': {'mode': 3}})
p._links_direct = []
p._lstats_ok_mono = _clock.monotonic() - (p.AMI_POLL_SEC * 5)   # AMI is down
p._reconcile_links()
check("no attempt on stale data", p.sent == [], str(p.sent))

print("\n16. A link stuck CONNECTING is NOT treated as connected")
# The real 49171 case: present in lstats forever, never established. Counting
# mere presence made it report state=ok / 0 fails and never say anything.
p = make_plugin(_tmp, _clock, {'49171': {'mode': 3}})
p._links_direct = [{'node': '49171', 'peer': '(none)', 'reconnects': 0,
                    'dir': 'OUT', 'ctime': '00:00:06:394', 'state': 'CONNECTING'}]
p._lstats_ok_mono = _clock.monotonic()
p._reconcile_links()
check("CONNECTING triggers a reconnect", len(p.sent) == 1, str(p.sent))
rep = {r['node']: r for r in p.relink_report()}
check("reported as retrying", rep['49171']['state'] == 'retrying', rep['49171']['state'])

print("\n17. Exhausted backoff goes DORMANT — slower, never abandoned")
for _ in range(p.RELINK_MAX_FAILS + 1):
    _clock.advance(3600)
    p._lstats_ok_mono = _clock.monotonic()
    p._reconcile_links()
rep = {r['node']: r for r in p.relink_report()}
check("state dormant", rep['49171']['state'] == 'dormant', rep['49171']['state'])
check("still in the persistent set", '49171' in p._desired)
check("never uses app_rpt permanent mode",
      not any('ilink 13' in c or 'ilink 12' in c for c in p.sent),
      str([c for c in p.sent if 'ilink 1' in c][:2]))
# The moon test: still retrying months later, at the slow cadence.
before = len(p.sent)
for _ in range(72):                       # three days of hourly checks
    _clock.advance(3600)
    p._lstats_ok_mono = _clock.monotonic()
    p._reconcile_links()
tries = len(p.sent) - before
check("still retrying 3 days later", tries > 0, f"{tries} tries")
check("but only hourly, not hammering", tries <= 73, f"{tries} tries in 72h")
# ...and it relinks by itself when the far end returns, with no human action.
# Advance FIRST, then refresh the link list: set_links stamps the freshness
# clock, and reconcile rightly refuses a list older than a couple of polls.
_clock.advance(3600)
set_links(p, _clock, ['49171'])
p._reconcile_links()
rep = {r['node']: r for r in p.relink_report()}
check("self-heals when the node returns", rep['49171']['state'] == 'ok',
      rep['49171']['state'])

print("\n18. connect_node reports what actually happened, not what it asked for")
p = make_plugin(_tmp, _clock)
lstats_state = {'v': 'ESTABLISHED'}


def ami_with_lstats(command, timeout=3.0):
    p.sent.append(command)
    if command.startswith('rpt lstats'):
        if lstats_state['v'] is None:
            return True, 'NODE PEER\n---- ----\n'
        return True, ('NODE      PEER      RECONNECTS DIRECTION CTIME STATE\n'
                      '----      ----      ---------- --------- ----- -----\n'
                      f'45412     1.2.3.4   0          OUT       00:01 {lstats_state["v"]}\n')
    return True, 'OK'


p._ami_command = ami_with_lstats
p._stop = types.SimpleNamespace(wait=lambda s: None, is_set=lambda: False)
res = p.connect_node('45412')
check("established -> ok + established flag",
      res['established'] is True and res['state'] == 'ESTABLISHED', str(res))
check("message names the node", '45412' in res['output'], res['output'])

lstats_state['v'] = 'CONNECTING'
res = p.connect_node('45412')
check("CONNECTING is NOT reported as established", res['established'] is False)
check("message explains the far end", 'not accepted' in res['output'], res['output'])

lstats_state['v'] = None
res = p.connect_node('45412')
check("absent link reported", res['established'] is False and res['state'] == 'none')
check("message suggests the node number", 'node number' in res['output'], res['output'])

print("\n19. THE PROMISE: a link comes back after a gateway restart")
# Simulate the whole cycle: connect, process dies, new process reads only what
# is on disk, and the link is restored with nobody clicking anything.
restart_dir = tempfile.mkdtemp(prefix='usrp-restart-')
a = make_plugin(restart_dir, _clock)
a.connect_node('45412')
check("connect recorded on disk", os.path.exists(a._desired_path))

b = usrp.UsrpPlugin()                      # a fresh process
b.node, b.ami_user, b.ami_secret = '683970', 'gateway', 'x'
b._desired_path = a._desired_path
b._desired = b._load_desired()
b.sent = []
b._stop = types.SimpleNamespace(wait=lambda s: None, is_set=lambda: False)
b._ami_command = lambda command, timeout=3.0: (b.sent.append(command), (True, 'OK'))[1]
check("new process wants the link", '45412' in b._desired, str(b._desired))

set_links(b, _clock, [])                   # nothing connected after the restart
b._reconcile_links()
check("restored with no human action",
      any('ilink 3 45412' in c for c in b.sent), str(b.sent))

# And Disconnect really is the off switch — the promise has to end somewhere.
b.disconnect_node('45412')
c = usrp.UsrpPlugin()
c._desired_path = a._desired_path
check("disconnect survives restart too", '45412' not in c._load_desired())

print("\n=== shared node address book ===\n")

print("12. Naming is shared between instances")
# Point the root at the temp dir so migration finds no real recents files and
# these two checks see only what they put in.
_real_root2 = usrp._gw_root
usrp._gw_root = lambda: _tmp
usrp.NODE_BOOK = usrp._NodeBook(os.path.join(_tmp, 'book2.json'))
a = make_plugin(_tmp, _clock)
b = make_plugin(_tmp, _clock)
a.connect_node('45412')
usrp.NODE_BOOK.set_name('45412', 'Bay-Net Hub')
names_from_b = {e['node']: e['name'] for e in b.get_status()['book']}
check("AS2 sees the name AS1 saved", names_from_b.get('45412') == 'Bay-Net Hub',
      str(names_from_b))
check("book persisted", os.path.exists(os.path.join(_tmp, 'book2.json')))

print("\n13. Book survives a reload and sorts most-recent-first")
_clock.advance(10)
a.connect_node('41413')
fresh = usrp._NodeBook(os.path.join(_tmp, 'book2.json'))
rows = fresh.all()
check("both nodes present", {r['node'] for r in rows} == {'45412', '41413'}, str(rows))
check("most recent first", rows[0]['node'] == '41413', str([r['node'] for r in rows]))
check("name survived reload", fresh.name_of('45412') == 'Bay-Net Hub')
usrp._gw_root = _real_root2

print("\n14. Old per-instance recents are migrated, not discarded")
mig = tempfile.mkdtemp(prefix='usrp-mig-')
with open(os.path.join(mig, 'usrp_recent.json'), 'w') as f:
    json.dump(['49172', '41413'], f)
with open(os.path.join(mig, 'usrp2_recent.json'), 'w') as f:
    json.dump(['45412'], f)
usrp._gw_root = lambda: mig
try:
    book = usrp._NodeBook(os.path.join(mig, 'book.json'))
    got = {r['node'] for r in book.all()}
    check("all three carried over", got == {'49172', '41413', '45412'}, str(got))
    check("migration persisted", os.path.exists(os.path.join(mig, 'book.json')))
finally:
    usrp._gw_root = _real_root2

print("\n15. Book rejects rubbish, caps name length")
book = usrp._NodeBook(os.path.join(_tmp, 'book3.json'))
check("non-numeric node refused", book.set_name('abc', 'x') is False)
book.set_name('12345', 'y' * 200)
check("name capped", len(book.name_of('12345')) == book.MAX_NAME,
      str(len(book.name_of('12345'))))
check("remove works", book.remove('12345') is True and book.name_of('12345') == '')

print(f"\n{'ALL PASS' if not FAIL else str(len(FAIL)) + ' FAILURES: ' + ', '.join(FAIL)}\n")
sys.exit(1 if FAIL else 0)
