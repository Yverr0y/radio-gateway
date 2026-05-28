#!/usr/bin/env python3
"""
Radio Gateway MCP Server (entry point)

Exposes the radio gateway as a set of AI-callable tools via the Model Context
Protocol (MCP) stdio transport.  Claude Code (or any MCP client) can load this
server and control the gateway without API keys.

Usage (stdio, local):
    python3 gateway_mcp.py

Claude Code configuration (.mcp.json at the repo root):
    {
      "mcpServers": {
        "radio-gateway": {
          "command": "python3",
          "args": ["./gateway_mcp.py"],
          "cwd": "."
        }
      }
    }

The implementation lives in the ``mcp_server`` package:
  - ``mcp_server/server.py`` — FastMCP instance + HTTP helpers + config loader
  - ``mcp_server/tools/control.py``     — gateway/SDR/radio TX/recordings/logs/automation/audio-trace/telegram
  - ``mcp_server/tools/radios.py``      — TH-9800 / D75 / KV4P / IC-7100 / processes / mixer / config / process control
  - ``mcp_server/tools/routing.py``     — audio routing (bus/sink/gain/denoise) + transcription + link endpoints + loop recorder
  - ``mcp_server/tools/fleet.py``       — D75 endpoint SSH / packet / Winlink / stream-trace / sink-stats / scheme mgmt / endpoint battery

This file is intentionally a thin shim so the ``.mcp.json`` entry, tmux session
launchers, and any external scripts that spawn ``python3 gateway_mcp.py``
continue working without modification.
"""

from mcp_server import run


if __name__ == '__main__':
    run()
