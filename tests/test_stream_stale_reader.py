"""Stale-reader engine behind the 2026-08-19 Broadcastify flap.

A reader thread bound to shared state instead of its own connection runs
its cleanup path against a SUCCESSOR connection: flipping connected false,
killing the live encoder and queueing another reconnect, whose reader then
does the same. 909 successful reconnects were each murdered this way.
"""
import threading, time, itertools

class Sim:
    def __init__(self, legacy):
        self.legacy = legacy
        self.lock = threading.Lock()
        self.encoder = None          # current generation id
        self.connected = False
        self.teardown_intentional = False
        self.gen = itertools.count(1)
        self.reconnects = 0
        self.murders = 0             # live connection killed by a stale reader
        self.stop = False

    def teardown_encoder(self):
        self.encoder = None

    def close(self):
        self.connected = False
        self.teardown_intentional = True
        self.teardown_encoder()

    def connect(self):
        enc = next(self.gen)
        self.encoder = enc
        threading.Thread(target=self.reader, args=(enc,), daemon=True).start()
        self.teardown_intentional = False
        self.connected = True

    def reader(self, enc):
        # Block until our encoder is reaped (SIGKILL makes the read return).
        while self.encoder == enc and not self.stop:
            time.sleep(0.01)
        # ...then a scheduling delay before the cleanup path runs. This is the
        # window a reconnect slips into.
        time.sleep(0.15)
        if self.stop:
            return
        if not self.legacy:
            if self.encoder is not enc:      # THE FIX
                return
        if self.connected:
            if self.encoder is not None and self.encoder != enc:
                self.murders += 1            # we just killed a live successor
            self.connected = False
            self.teardown_encoder()
            if not self.teardown_intentional:
                self.reconnect()

    def reconnect(self):
        with self.lock:
            self.reconnects += 1
        def w():
            time.sleep(0.02)
            self.close()
            time.sleep(0.02)
            self.connect()
        threading.Thread(target=w, daemon=True).start()


def run(legacy, seconds=8.0):
    s = Sim(legacy)
    s.connect()
    time.sleep(0.2)
    s.reconnect()                    # the initiating drop
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.05)
    s.stop = True
    time.sleep(0.3)
    return s

for legacy in (True, False):
    s = run(legacy)
    label = "LEGACY (pre-fix)" if legacy else "FIXED"
    print(f"{label:>18}: reconnects={s.reconnects:5d}  "
          f"stale-reader kills of a live connection={s.murders:4d}  "
          f"final_connected={s.connected}")
