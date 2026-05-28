# Example plugin

A working dummy radio that produces silence and accepts (ignored) TX audio.

## What this shows

* The minimum surface area required by `plugins/_base.py`'s `RadioPlugin`
  Protocol — `setup`, `get_audio`, `put_audio`, `execute`, `get_status`,
  `cleanup`.
* How to wire `audio_boost`, `tx_audio_boost`, mute, and level metering
  the way the routing UI expects.
* Lifecycle threading: a background reader posting frames into a bounded
  queue; `get_audio()` drains the queue with `get_nowait()`.

## Trying it locally

```bash
cp examples/example_plugin/plugin.py plugins/example.py
echo 'ENABLE_EXAMPLE = True' >> gateway_config.txt
# restart the gateway
```

The plugin appears as `example` in the routing UI. Connect it to a Listen
bus; you'll see it idle at level 0 forever (it produces no audio).

## Extending it

Add capability flags by setting `CAPABILITIES` on the class:

```python
from plugins._base import (
    CAPABILITY_AUDIO_RX, CAPABILITY_PTT, CAPABILITY_FREQUENCY,
)

class MyRadioPlugin:
    CAPABILITIES = {CAPABILITY_AUDIO_RX, CAPABILITY_PTT, CAPABILITY_FREQUENCY}
```

Optional hooks the loader will pick up automatically (omit any you don't
need):

| Hook | Purpose |
|---|---|
| `web_routes()` | Return `[(path, handler), ...]` to register on the gateway's HTTP server |
| `mcp_tools()` | Return MCP tool callables to expose via the AI control interface |
| `on_bus_attach(bus_id)` | Notified when a routing connection adds your plugin to a bus |
| `on_bus_detach(bus_id)` | Notified when a routing connection removes your plugin from a bus |
| `on_ptt_change(state)` | Notified when an upstream bus toggles PTT |

See `plugins/_base.py` for the full contract reference.
