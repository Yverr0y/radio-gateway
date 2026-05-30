"""Gateway Link endpoint MCP tools — status of connected remote endpoints
and command dispatch to them.

Split out of mcp_server/tools/routing.py on 2026-05-30; tools registered
against the shared ``mcp`` instance via @mcp.tool() decorator side
effects on import.
"""

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def link_endpoint_status() -> str:
    """
    Get detailed status of all connected Gateway Link endpoints including
    audio levels, PTT state, capabilities, and endpoint-reported radio state.
    """
    result = _get('/status')
    endpoints = result.get('link_endpoints', [])
    if not endpoints:
        return "No link endpoints connected"
    lines = []
    for ep in endpoints:
        _conn = 'CF' if ep.get('via_tunnel') else 'LAN'
        _ping = f"{ep.get('ping_ms', -1)}ms" if ep.get('ping_ms', -1) >= 0 else '?'
        lines.append(f"Endpoint: {ep['name']} ({_conn} {_ping})")
        lines.append(f"  Plugin: {ep.get('plugin', '?')}  Addr: {ep.get('addr', '?')}")
        lines.append(f"  Source: {ep.get('source_id', '?')}  Sink: {ep.get('sink_id', '?')}")
        lines.append(f"  RX level: {ep.get('level', 0)}  TX level: {ep.get('tx_level', 0)}")
        lines.append(f"  RX muted: {ep.get('rx_muted')}  TX muted: {ep.get('tx_muted')}  PTT: {ep.get('ptt_active')}")
        caps = ep.get('capabilities', {})
        lines.append(f"  Capabilities: {', '.join(k for k, v in caps.items() if v)}")
        es = ep.get('endpoint_status', {})
        if es:
            for k in ('model', 'firmware', 'serial_connected', 'audio_connected',
                       'battery_level', 'transmitting', 'active_band'):
                if k in es:
                    lines.append(f"  {k}: {es[k]}")
            bands = es.get('band', [])
            for i, b in enumerate(bands):
                if isinstance(b, dict) and b.get('frequency'):
                    lines.append(f"  Band {i}: {b['frequency']} MHz power={b.get('power','')} s_meter={b.get('s_meter','')}")
    return '\n'.join(lines)


@mcp.tool()
def link_endpoint_command(
    endpoint: str,
    cmd: str,
    args: str = '',
) -> str:
    """
    Send a command to a specific link endpoint.

    Args:
        endpoint: Endpoint name (e.g. 'd75-bt')
        cmd:      Command — 'ptt', 'frequency', 'cat', 'tone', 'shift', 'offset',
                  'memscan', 'status', 'rx_gain', 'tx_gain'
        args:     Command arguments (e.g. freq in MHz, CAT command string,
                  'on'/'off' for PTT)
    """
    payload = {'cmd': cmd}
    if cmd == 'ptt':
        payload['state'] = args.lower() in ('on', 'true', '1')
    elif cmd == 'frequency':
        payload['freq'] = args
    elif cmd in ('cat', 'tone', 'shift', 'offset'):
        payload['raw'] = args
    elif cmd in ('rx_gain', 'tx_gain'):
        try:
            payload['gain'] = float(args)
        except ValueError:
            return f"Error: {cmd} requires a numeric value"
    result = _post('/linkcmd', {'endpoint': endpoint, **payload})
    if result.get('ok'):
        resp = result.get('response', '')
        return f"Endpoint {endpoint} {cmd} OK" + (f': {resp}' if resp else '')
    return f"Error: {result.get('error', 'unknown')}"
