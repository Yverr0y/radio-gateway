# AllStarLink bridge — USRP gateway plugin

This wires the gateway into AllStar **without administering a radio node**: a
headless ASL3 instance (no RF hardware) whose "radio" is a USRP UDP socket
pointed at the gateway. It connects to any AllStar node on demand from the
gateway's `/usrp` control panel — nothing is baked into `rpt.conf`.

```
 target public node  ◄──IAX2/4569──►  ASL3 bridge (node 68397, no radio)
                                            │ USRP UDP (8 kHz s16 + PTT)
                                            ▼
                                       gateway UsrpPlugin ──► 48 kHz bus
                                       AMI 5038  ◄── /usrp control panel
```

Run it **on the gateway box with host networking** so all of USRP, AMI, and
outbound IAX2 share `127.0.0.1`. Podman shown; Docker is identical (`docker`
for `podman`).

The gateway supports **two independent USRP instances** (`usrp` / `usrp2`), each with its own bridge node, ASL node number, and AMI credentials. They appear as separate bus sources/sinks and have separate `/usrp` and `/usrp2` control panels.

> **DEPLOYED 2026-05-31** on the gateway box (.140) as rootful Podman container
> `asl-bridge`, image `localhost/asl-bridge:latest`, running `asterisk -f` as
> PID 1 with `--restart=always`; `podman-restart.service` is enabled for boot.
> Node 68397 is registered; AMI user `gateway` works.

## 1. Gateway config (`gateway_config.txt`, NOT committed — holds secrets)

**Node 1 (usrp):**
```ini
ENABLE_USRP = True
USRP_REMOTE_HOST = 127.0.0.1      # bridge node, same box
USRP_REMOTE_PORT = 32001          # ASL listens here
USRP_LISTEN_PORT = 34001          # gateway listens here
USRP_NODE = 68397
USRP_AMI_HOST = 127.0.0.1
USRP_AMI_PORT = 5038
USRP_AMI_USER = gateway
USRP_AMI_SECRET = <choose-an-AMI-secret>   # must match manager.conf below
```

**Node 2 (usrp2) — optional second instance:**
```ini
ENABLE_USRP2 = True
USRP2_REMOTE_HOST = 127.0.0.1
USRP2_REMOTE_PORT = 32002
USRP2_LISTEN_PORT = 34002
USRP2_NODE = 683972               # second registered node
USRP2_AMI_HOST = 127.0.0.1
USRP2_AMI_PORT = 5038
USRP2_AMI_USER = gateway
USRP2_AMI_SECRET = <same-or-different-secret>
```

## 2. The container

```bash
# 1. Empty config dir mounted first, so the package populates it with defaults
mkdir -p ~/asl-bridge/etc
sudo podman run -d --name asl-bridge --network=host \
  -v ~/asl-bridge/etc:/etc/asterisk \
  docker.io/library/debian:12 sleep infinity

# 2. Install Asterisk + app_rpt + chan_usrp WITHOUT the asl3 metapackage
#    (skips dahdi-dkms, which can't build in a container and isn't needed)
sudo podman exec asl-bridge bash -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y --no-install-recommends curl ca-certificates
  cd /tmp && curl -fsSL -O https://repo.allstarlink.org/public/asl-apt-repos.deb12_all.deb
  dpkg -i asl-apt-repos.deb12_all.deb && apt-get update -qq
  apt-get install -y --no-install-recommends asl3-asterisk asl3-asterisk-config asl3-asterisk-modules'

# 3. (edit the four config files per section 3 below, then) make Asterisk the
#    container's main process so it auto-starts, and persist across reboot:
sudo podman commit asl-bridge localhost/asl-bridge:latest
sudo podman rm -f asl-bridge
sudo podman run -d --name asl-bridge --network=host --restart=always \
  -v ~/asl-bridge/etc:/etc/asterisk \
  localhost/asl-bridge:latest asterisk -f
sudo systemctl enable podman-restart.service     # starts it on host boot
```

Note: the package's postinst prints harmless `modprobe`/`systemctl: not found`
warnings in a container — DAHDI module-load and service-enable, neither used here.
(If `--network=host` isn't acceptable, publish UDP 32001 + outbound 4569 and
TCP 5038 instead, and set `USRP_REMOTE_HOST`/`USRP_AMI_HOST` to the container.)

## 3. ASL config — the four files in `~/asl-bridge/etc/`

**`modules.conf`** — make sure chan_usrp loads:
```
load => chan_usrp.so
```

**`rpt.conf`** — node 68397 as a no-radio USRP bridge, connection-agnostic:
```ini
[68397]
rxchannel = USRP/127.0.0.1:34001:32001   ; ASL→gw :34001, gw listens; ASL listens :32001
duplex = 0                ; no telemetry, no hang time (it's a bridge)
hangtime = 0
althangtime = 0
telemdefault = 0          ; quiet — no courtesy tones/IDs into the link
linktolink = no
; NO startup connect — the gateway /usrp panel issues ilink connects via AMI
```

**`iax.conf`** — node 68397 registration with AllStarLink (PUBLIC repo: put
the real node password in the live file only, never commit it):
```ini
[general]
bindport = 4569
register => 68397:<node-password>@register.allstarlink.org

[radio]
type = friend
context = radio-secure
auth = rsa,md5,plaintext
host = dynamic
```

**`manager.conf`** — AMI user the gateway panel logs in as:
```ini
[general]
enabled = yes
bindaddr = 127.0.0.1
port = 5038

[gateway]
secret = <same-as-USRP_AMI_SECRET-above>
read = system,call,command
write = system,call,command
```

Then: `podman exec asl-bridge asterisk -rx "module reload"` (or restart the
container). Check it registered: `podman exec asl-bridge asterisk -rx "iax2 show registry"`.

## 4. Use it

1. Gateway: restart the service (you do this), confirm `[USRP] up:` in the log
   and that `usrp` appears in the routing UI.
2. Routing UI: drop **AllStar (USRP)** onto an RX bus (→ Mumble/web/IC-7100)
   and a TX bus (mic/Mumble → AllStar).
3. Open `/usrp`, type a node number, hit **Connect**. Talk.

## 5. MCP tools

The MCP server exposes 7 AllStar tools in `mcp_server/tools/usrp.py`:

| Tool | What it does |
|------|-------------|
| `usrp_nodes()` | List all loaded USRP plugin instances (IDs, node numbers) |
| `usrp_status(node_id)` | Audio counters, TX/RX keyed, link cache, AMI health |
| `usrp_connect(node, mode, node_id)` | iLink connect — `transceive` or `monitor` |
| `usrp_disconnect(node, node_id)` | Disconnect a specific linked node |
| `usrp_disconnect_all(node_id)` | Drop all links at once |
| `usrp_links(node_id)` | Direct links (can disconnect) + indirect conference nodes |
| `usrp_node_stats(node_id)` | Keyups today/total, TX time, uptime, timeouts |

`node_id` defaults to `'usrp'`; pass `'usrp2'` for the second instance.

## Sanity checks / gotchas

- `chan_usrp` logs a harmless "Receive queue exceeds threshold 320" warning — ignore.
- AMI `rpt cmd 68397 ilink 3 <node>` = connect-transceive; `ilink 1 <node>` =
  disconnect; `ilink 6 0` = disconnect-all. The panel and MCP tools use these.
- If `/usrp` shows "AMI NOT configured", `USRP_AMI_USER/SECRET` are unset or
  don't match `manager.conf`.
- No audio but pkts_rx climbing → check the routing UI bus connection, not the link.
- Connecting to a public node is permissionless (node-to-node), but the target
  must allow inbound connects (most do; some private/closed nodes don't).
- HamVOIP nodes can drop the link (~10 s) due to the `newkey` handshake — a
  remote-node-side issue; ASL3 nodes and hubs hold fine.
