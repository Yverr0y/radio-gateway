"""Plugin contract for in-gateway radio plugins.

A plugin is any Python class that satisfies the ``RadioPlugin`` Protocol
below. The class lives in a single file under ``plugins/`` and the
loader (``plugin_loader.py``) auto-discovers it on startup.

What the contract guarantees:

* The gateway calls ``setup(config, gateway)`` once during init. The
  plugin returns True to register, False to skip.
* While registered, the bus tick calls ``get_audio(chunk_size)`` every
  50 ms. The plugin returns 4800 bytes of 48 kHz int16 mono or
  ``(None, False)`` for silence. The call must be non-blocking.
* When a bus needs to TX through this plugin, it calls ``put_audio(pcm)``
  with one chunk at a time. PTT timing is owned by the bus, not the plugin.
* ``execute(cmd)`` handles control surface commands routed from the web
  UI, MCP tools, or automation. The plugin returns ``{'ok': bool, ...}``.
* ``cleanup()`` is called once at gateway shutdown.

What's optional (use what your hardware actually has):

* ``CAPABILITIES`` — a set of capability flags (see CAPABILITY_* below).
  The routing UI uses this to decide what a source can do (TX or RX-only,
  PTT, frequency tuning, etc.). Default: no capabilities.
* ``web_routes()`` — return a list of ``(path, handler)`` tuples to register
  on the gateway's HTTP server. Lets a plugin own its own status page or
  control endpoints without touching ``web_server.py``.
* ``mcp_tools()`` — return a list of ``@mcp.tool()``-decorated callables to
  contribute MCP tools. Lets the plugin expose AI-controllable functions
  without touching ``mcp_server/``.
* ``on_bus_attach(bus_id)`` / ``on_bus_detach(bus_id)`` — fire when a
  routing connection is made or broken. Most plugins don't need this.
* ``on_ptt_change(state)`` — fire when PTT state changes upstream. Useful
  for plugins that need to react (e.g., switch a relay).

Existing in-tree plugins (``sdr_plugin.py``, ``th9800_plugin.py``,
``packet_radio.py``) predate this file. They satisfy the required parts
of the Protocol by duck-typing — the loader does not enforce inheritance.
Plugins migrated under ``plugins/`` are encouraged to declare
``CAPABILITIES`` explicitly so the routing UI can light up the right
controls without inspecting plugin internals.

Audio format everywhere: 48000 Hz, signed 16-bit little-endian, mono.
One chunk = 2400 samples = 4800 bytes = 50 ms.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


# ── Capability flags ───────────────────────────────────────────────
# Plugins declare these in their ``CAPABILITIES`` class attribute so the
# routing UI and MCP layer know what controls to surface without poking
# the plugin's internals. Keep this list short and orthogonal — capabilities
# describe what the hardware can do, not how the plugin implements it.

CAPABILITY_AUDIO_RX = 'audio_rx'        # produces RX audio via get_audio
CAPABILITY_AUDIO_TX = 'audio_tx'        # accepts TX audio via put_audio
CAPABILITY_PTT      = 'ptt'             # supports {'cmd': 'ptt', 'state': bool}
CAPABILITY_FREQUENCY = 'frequency'      # supports {'cmd': 'frequency', 'freq_mhz': float}
CAPABILITY_CTCSS    = 'ctcss'           # supports {'cmd': 'ctcss', 'tone': float}
CAPABILITY_POWER    = 'power'           # supports {'cmd': 'power', 'level': str}
CAPABILITY_RX_GAIN  = 'rx_gain'         # supports {'cmd': 'rx_gain', 'db': int}
CAPABILITY_TX_GAIN  = 'tx_gain'         # supports {'cmd': 'tx_gain', 'db': int}
CAPABILITY_SMETER   = 'smeter'          # reports signal strength in status
CAPABILITY_STATUS   = 'status'          # supports {'cmd': 'status'}
CAPABILITY_CAT      = 'cat'             # has CAT control surface
CAPABILITY_PACKET   = 'packet'          # offers packet/AGWPE socket
CAPABILITY_PACKET_LOCAL_TNC = 'packet_local_tnc'  # has on-device direwolf


@runtime_checkable
class RadioPlugin(Protocol):
    """The interface every plugin must satisfy.

    All methods are required EXCEPT the ones marked Optional in the module
    docstring. The loader does not enforce inheritance; this Protocol exists
    for type checkers and for documentation. Plugins that don't implement an
    optional method should simply omit it — the loader checks for presence
    with ``hasattr``.
    """

    PLUGIN_ID: str
    PLUGIN_NAME: str
    CAPABILITIES: set[str]  # optional in practice; defaults to empty

    def setup(self, config, gateway=None) -> bool: ...

    def get_audio(self, chunk_size=None) -> tuple[bytes | None, bool]: ...

    def put_audio(self, pcm: bytes) -> None: ...

    def execute(self, cmd: dict) -> dict: ...

    def get_status(self) -> dict: ...

    def cleanup(self) -> None: ...


# ── Optional-hook helpers ──────────────────────────────────────────
# These free functions let the loader dispatch optional plugin methods
# without each call site re-checking ``hasattr``. Each one returns a
# sensible default (no routes, no tools, no-op) when the hook is missing.

def get_web_routes(plugin) -> list:
    """Collect optional web routes contributed by a plugin."""
    fn = getattr(plugin, 'web_routes', None)
    if not callable(fn):
        return []
    try:
        return list(fn() or [])
    except Exception as e:
        print(f"  [Plugins] {plugin.PLUGIN_ID}.web_routes() raised: {e}")
        return []


def get_mcp_tools(plugin) -> list:
    """Collect optional MCP tools contributed by a plugin."""
    fn = getattr(plugin, 'mcp_tools', None)
    if not callable(fn):
        return []
    try:
        return list(fn() or [])
    except Exception as e:
        print(f"  [Plugins] {plugin.PLUGIN_ID}.mcp_tools() raised: {e}")
        return []


def fire_bus_attach(plugin, bus_id: str) -> None:
    """Notify the plugin that it has been routed into a bus."""
    fn = getattr(plugin, 'on_bus_attach', None)
    if callable(fn):
        try:
            fn(bus_id)
        except Exception as e:
            print(f"  [Plugins] {plugin.PLUGIN_ID}.on_bus_attach({bus_id}) raised: {e}")


def fire_bus_detach(plugin, bus_id: str) -> None:
    fn = getattr(plugin, 'on_bus_detach', None)
    if callable(fn):
        try:
            fn(bus_id)
        except Exception as e:
            print(f"  [Plugins] {plugin.PLUGIN_ID}.on_bus_detach({bus_id}) raised: {e}")


def fire_ptt_change(plugin, state: bool) -> None:
    fn = getattr(plugin, 'on_ptt_change', None)
    if callable(fn):
        try:
            fn(state)
        except Exception as e:
            print(f"  [Plugins] {plugin.PLUGIN_ID}.on_ptt_change({state}) raised: {e}")
