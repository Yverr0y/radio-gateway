"""AllStar / USRP MCP tools — node connect/disconnect, link status, stats.

The gateway supports up to two USRP plugin instances: 'usrp' (node 1) and
'usrp2' (node 2).  Each registers its own HTTP routes at /{id}/status and
/{id}/control.  Use usrp_nodes() to discover what's loaded.
"""

import json

from mcp_server.server import mcp, _get, _post


@mcp.tool()
def usrp_nodes() -> str:
    """
    List all AllStar USRP plugin instances loaded in the gateway.
    Returns each instance's ID, ASL node number, and status/control URLs.
    Use the returned ID as the node_id argument for other usrp_* tools.
    """
    data = _get('/usrp/nodes')
    if isinstance(data, dict) and 'error' in data:
        return f"Error: {data['error']}"
    if not data:
        return "No USRP instances loaded (ENABLE_USRP = false?)"
    lines = [f"{len(data)} USRP instance(s):"]
    for n in data:
        lines.append(
            f"  id={n.get('id')}  node={n.get('node')}  "
            f"name={n.get('name')}  enabled={n.get('enabled')}"
        )
    return '\n'.join(lines)


@mcp.tool()
def usrp_status(node_id: str = 'usrp') -> str:
    """
    Get AllStar USRP plugin status: audio counters, TX/RX keyed flags,
    connected link list (cached from last AMI poll), and AMI health.

    Args:
        node_id: Plugin ID — 'usrp' (node 1) or 'usrp2' (node 2).
                 Use usrp_nodes() to see available instances.
    """
    data = _get(f'/{node_id}/status')
    if 'error' in data:
        return f"Error: {data['error']}"
    return json.dumps(data, indent=2)


@mcp.tool()
def usrp_connect(
    node: str,
    mode: str = 'transceive',
    node_id: str = 'usrp',
) -> str:
    """
    Connect the AllStar node to another node via iLink.

    Args:
        node:    Target AllStar node number (e.g. '27339').
        mode:    'transceive' (full duplex, default) or 'monitor' (RX only).
        node_id: USRP plugin ID — 'usrp' or 'usrp2'.
    """
    mode = mode.lower().strip()
    if mode not in ('transceive', 'monitor'):
        return "Error: mode must be 'transceive' or 'monitor'"
    result = _post(f'/{node_id}/control', {
        'action': 'connect',
        'node': str(node).strip(),
        'mode': mode,
    })
    if result.get('ok'):
        return f"Connected {node_id} → node {node} ({mode})"
    return f"Connect failed: {result.get('error', json.dumps(result))}"


@mcp.tool()
def usrp_disconnect(
    node: str,
    node_id: str = 'usrp',
) -> str:
    """
    Disconnect the AllStar node from a specific linked node.

    Args:
        node:    AllStar node number to disconnect.
        node_id: USRP plugin ID — 'usrp' or 'usrp2'.
    """
    result = _post(f'/{node_id}/control', {
        'action': 'disconnect',
        'node': str(node).strip(),
    })
    if result.get('ok'):
        return f"Disconnected node {node} from {node_id}"
    return f"Disconnect failed: {result.get('error', json.dumps(result))}"


@mcp.tool()
def usrp_disconnect_all(node_id: str = 'usrp') -> str:
    """
    Disconnect the AllStar node from all linked nodes at once.

    Args:
        node_id: USRP plugin ID — 'usrp' or 'usrp2'.
    """
    result = _post(f'/{node_id}/control', {'action': 'disconnect_all'})
    if result.get('ok'):
        return f"All nodes disconnected from {node_id}"
    return f"Failed: {result.get('error', json.dumps(result))}"


@mcp.tool()
def usrp_links(node_id: str = 'usrp') -> str:
    """
    List current AllStar link topology: direct links (which we opened and can
    close) and indirect/conference nodes (reachable through a hub, read-only).

    Args:
        node_id: USRP plugin ID — 'usrp' or 'usrp2'.
    """
    result = _post(f'/{node_id}/control', {'action': 'links'})
    if not result.get('ok'):
        return f"Error: {result.get('error', json.dumps(result))}"
    direct = result.get('direct', [])
    indirect = result.get('indirect', [])
    lines = [f"Links for {node_id}:"]
    if direct:
        lines.append(f"  Direct ({len(direct)}) — can disconnect:")
        for d in direct:
            lines.append(
                f"    node={d.get('node')}  dir={d.get('dir')}  "
                f"up={d.get('ctime')}  state={d.get('state')}"
            )
    else:
        lines.append("  Direct: none")
    if indirect:
        lines.append(f"  Indirect/conference ({len(indirect)}) — read-only:")
        lines.append(f"    {', '.join(indirect)}")
    return '\n'.join(lines)


@mcp.tool()
def usrp_node_stats(node_id: str = 'usrp') -> str:
    """
    Get AllStar node statistics: keyups today/total, TX time, uptime,
    timeouts, and kerchunks.  Sourced from Asterisk 'rpt stats'.

    Args:
        node_id: USRP plugin ID — 'usrp' or 'usrp2'.
    """
    result = _post(f'/{node_id}/control', {'action': 'node_stats'})
    if not result.get('ok'):
        return f"Error: {result.get('error', json.dumps(result))}"
    lines = [f"Stats for {node_id}:"]
    for k, v in result.items():
        if k == 'ok':
            continue
        lines.append(f"  {k:<22} {v}")
    return '\n'.join(lines)
