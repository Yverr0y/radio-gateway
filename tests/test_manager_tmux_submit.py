"""Manager prompt submission into the Claude tmux session.

The Enter used to be fired in the same breath as the paste and was
consumed by the bracketed paste, leaving the prompt unsent and the run to
time out 600s later. Covers: the idle hint text not reading as "pending",
a swallowed Enter being retried exactly once, giving up rather than
hanging, and a stale buffer being cleared instead of appended to.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import manager_engine as ME

ME_cls = ME.ManagerEngine

class FakePane:
    """Models the Claude Code input line: an idle hint, a pasted placeholder,
    and an Enter that is swallowed the first `swallow` times."""
    def __init__(self, swallow):
        self.swallow = swallow
        self.content = ''      # '' == idle
        self.enters = 0
        self.submitted = []
    HINT = 'Try "edit <filepath>"'
    def line(self):
        return self.HINT if not self.content else self.content
    def send_literal(self, text):
        self.content += '[Pasted text #1 +%d lines]' % len(text.splitlines())
    def enter(self):
        self.enters += 1
        if self.swallow > 0:
            self.swallow -= 1
            return
        if self.content:
            self.submitted.append(self.content)
            self.content = ''
    def clear(self):
        self.content = ''

def make(pane):
    calls = []
    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == 'capture-pane':
            return types.SimpleNamespace(stdout='some output\n❯ %s\n' % pane.line(),
                                         returncode=0)
        if cmd[1] == 'send-keys':
            if '-l' in cmd:      pane.send_literal(cmd[-1])
            elif cmd[-1]=='Enter': pane.enter()
            elif cmd[-1]=='C-u':   pane.clear()
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')
    return fake_run, calls

class Stub:
    _send_to_tmux = ME_cls._send_to_tmux
    _prompt_line  = ME_cls._prompt_line

import time as _t
_t.sleep = lambda *a: None      # scale the test down

for swallow, label in ((0,'Enter lands first try'),
                       (1,'Enter swallowed once (the real bug)'),
                       (99,'Enter never lands')):
    pane = FakePane(swallow)
    fake_run, calls = make(pane)
    ME.subprocess.run = fake_run
    s = Stub()
    ok = s._send_to_tmux('sess', 'line\n'*151)
    print(f"{label:>36}: submitted={ok!s:5}  prompts_delivered={len(pane.submitted)}  enters={pane.enters}")

# stale buffer must be cleared, not appended to
pane = FakePane(0); pane.content = '[Pasted text #23 +151 lines]'
fake_run, calls = make(pane)
ME.subprocess.run = fake_run
Stub()._send_to_tmux('sess','x\n'*10)
print(f"{'stale buffer present beforehand':>36}: delivered={pane.submitted}")
