"""Plugin auto-discovery — loads radio plugins from the plugins/ directory.

Drop a .py file in plugins/ that defines a class with these attributes:
    PLUGIN_ID   = 'myradio'       # routing config source ID
    PLUGIN_NAME = 'My Radio'      # display name

The class must implement the standard plugin interface:
    setup(config, gateway=None) → bool
    get_audio(chunk_size) → (bytes|None, bool)
    put_audio(pcm)
    execute(cmd) → dict
    get_status() → dict
    cleanup()

See plugins/example_radio.py for a complete template.
"""

import importlib
import os
import sys
import traceback


def discover_plugins(config, gateway):
    """Scan plugins/ directory, load and setup each plugin.

    Returns: dict of {plugin_id: plugin_instance}
    """
    plugins_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'plugins')
    if not os.path.isdir(plugins_dir):
        return {}

    # Add plugins dir to path so imports work
    if plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)

    loaded = {}
    for fname in sorted(os.listdir(plugins_dir)):
        if not fname.endswith('.py') or fname.startswith('_'):
            continue
        module_name = fname[:-3]
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            print(f"  [Plugins] Failed to import {fname}: {e}")
            continue

        # Find the plugin class (look for PLUGIN_ID attribute)
        plugin_cls = None
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name)
            if (isinstance(obj, type) and
                    hasattr(obj, 'PLUGIN_ID') and
                    hasattr(obj, 'setup') and
                    obj is not getattr(mod, '__builtins__', None)):
                plugin_cls = obj
                break

        if not plugin_cls:
            continue

        plugin_id = plugin_cls.PLUGIN_ID
        enable_key = f'ENABLE_{plugin_id.upper()}'

        # Check if enabled in config (default: disabled)
        if not getattr(config, enable_key, False):
            print(f"  [Plugins] {plugin_id}: skipped (set {enable_key} = True to enable)")
            continue

        try:
            instance = plugin_cls()
            if instance.setup(config, gateway=gateway):
                loaded[plugin_id] = instance
                name = getattr(instance, 'name', plugin_id)
                caps = getattr(instance, 'CAPABILITIES', None) or set()
                cap_str = f' [{",".join(sorted(caps))}]' if caps else ''
                print(f"  [Plugins] {name} loaded from {fname}{cap_str}")

                # Optional: web_routes() — register per-plugin HTTP handlers
                _register_web_routes(instance, gateway)
                # Optional: mcp_tools() — register plugin-contributed MCP tools
                _register_mcp_tools(instance)
            else:
                print(f"  [Plugins] {plugin_id}: setup() returned False")
        except Exception as e:
            print(f"  [Plugins] {plugin_id} setup failed: {e}")
            traceback.print_exc()

    return loaded


def _register_web_routes(plugin, gateway):
    """Register HTTP routes contributed by a plugin via its web_routes() hook.

    Stashes them on ``gateway._plugin_web_routes`` as a {path: handler} dict;
    ``web_server.do_GET`` consults this map before falling through to its
    own dispatch. Plugin authors don't need to know how the gateway routes
    requests — they just return ``[(path, handler), ...]``.
    """
    from plugins._base import get_web_routes
    routes = get_web_routes(plugin)
    if not routes:
        return
    target = getattr(gateway, '_plugin_web_routes', None)
    if target is None:
        target = {}
        if gateway is not None:
            gateway._plugin_web_routes = target
    for entry in routes:
        try:
            path, handler = entry
        except (TypeError, ValueError):
            print(f"  [Plugins] {plugin.PLUGIN_ID}: bad web_routes entry: {entry!r}")
            continue
        target[path] = handler
        print(f"    web_route: {path}")


def _register_mcp_tools(plugin):
    """Register MCP tools contributed by a plugin via its mcp_tools() hook.

    The MCP server runs in a separate process (spawned from .mcp.json) so we
    can't actually hook tools into a running mcp.server here — instead we
    drop a marker file the MCP entry point reads on startup. For now this
    just logs what the plugin would contribute so we can wire the mcp_server
    side in a follow-up.
    """
    from plugins._base import get_mcp_tools
    tools = get_mcp_tools(plugin)
    if not tools:
        return
    for fn in tools:
        name = getattr(fn, '__name__', '?')
        print(f"    mcp_tool: {name} (registration deferred — see plan)")
