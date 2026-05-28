# Mega Plan — Observability, Refactor, Plugin System

**Status legend:** ⬜ todo · 🟡 in-progress · ✅ done · ❌ blocked · ⏭️ skipped (with reason)

**Rule:** Test as you go. Measure, don't guess. After each step, run the smoke test and tick the box. If the gateway stops working, fix before moving on.

**Constraint:** NEVER restart the radio-gateway service from Claude. Ask the user to do it.

---

## Resumption guide (read first if picking up cold)

1. Find the next ⬜ checkbox below — that's the next action.
2. Each phase has a **Smoke test** subsection. Run it before ticking the phase.
3. Commits are atomic per phase. If `git status` is dirty mid-phase, finish or revert before starting the next.
4. `MEGA_PLAN.md` itself gets committed alongside the phase it tracks — the file is the source of truth for progress.

**Current focus:** Phase 0 (groundwork)

---

## Ordering rationale

1. **Phase 3 first (metrics)** — gives observability *before* refactoring, so regressions show up on a dashboard.
2. **Phase 1 (monolith split)** — refactor with metrics watching.
3. **Phase 2 (plugin migration)** — slow rollout, one radio per release. Now safe because metrics + smaller files.

---

# PHASE 0 — Groundwork

- ✅ 0.1 Confirm `prometheus_client` install path — installed via `pip install --user --break-system-packages prometheus-client` (v0.25.0). Not yet in requirements.txt; added in 3.A.1.
- ✅ 0.2 Baseline LOC recorded below.
- ✅ 0.3 Gateway reachable at localhost:8080 (12h uptime, /status returns JSON).
- ✅ 0.4 Existing test runs via `python3 tests/test_ic7100_civ.py` — **96/104 failing pre-existing**, attribute error in `tools/ic7100_link_plugin.py:378` referencing `self._tls`. Not on critical path; logged for later. Cannot rely on this suite as smoke gate.

**Smoke test:** `curl -s http://localhost:8080/status | head -c 100` returns JSON.

---

# PHASE 3 — `/metrics` endpoint + Grafana

## 3.A — Library + bare endpoint
- ✅ 3.A.1 Added `prometheus-client` to `requirements.txt`
- ✅ 3.A.2 Installed (v0.25.0) via `pip install --user --break-system-packages`
- ✅ 3.A.3 Created `metrics.py` — single `REGISTRY`, 13 metric objects covering Phase 3.B+3.D, `render()` returns (bytes, content_type)
- ✅ 3.A.4 Added `handle_metrics` to `web_routes_get.py` with LAN/Tailnet allowlist (127./192.168./10./100.); wired in `web_server.py` do_GET dispatch
- 🟡 3.A.5 **Test:** standalone HTTPServer test passed end-to-end (handler returns 200 + correct content-type + rg_* metrics). Live gateway returns 404 until restart — **user action required: restart radio-gateway service to load new route**

## 3.B — First six metrics (highest signal)
- ✅ 3.B.1 `rg_bus_audio_level{bus}` — wired at `bus_manager.py:1436` in the per-tick gateway-mirror block; covers stream/mumble_tx/transcription/remote_audio_tx/nul + every `link_<name>` bus. Normalized 0-100 → 0.0-1.0.
- ✅ 3.B.2 `rg_bus_ptt_active{bus}` — wired in `gateway_core.py:set_ptt_state` (label = TX_RADIO).
- ✅ 3.B.3 `rg_transcription_inflight{engine}` — set on dispatch + finally clause in `transcriber.py:_run_inference`.
- ✅ 3.B.4 `rg_transcription_seconds{engine}` — histogram observed in `_run_inference` finally (guarded by `locals()` check so a transcribe exception doesn't crash).
- ✅ 3.B.5 `rg_stream_bytes_sent_total{stream}` — delta-tracked in `stream_stats.get_stream_stats` so reconnect resets don't break monotonicity.
- ✅ 3.B.6 `rg_link_endpoint_up{endpoint}` — set in `gateway_link._heartbeat_loop` after dead-peer detection.
- 🟡 3.B.7 **Test:** all touched modules import cleanly; standalone handler test passed. Awaiting gateway restart for live-traffic verification.

## 3.C — Grafana on macmini
- ✅ 3.C.1 Added prometheus + grafana to `/opt/media-stack/docker-compose.yml` (with prometheus_data + grafana_data volumes). Backup at `/opt/media-stack/docker-compose.yml.bak.20260528-094959`.
- ✅ 3.C.2 Prometheus config at `docs/prometheus/prometheus.yml` (mirrored to `/opt/media-stack/configs/prometheus/`). Scrapes gateway:8080/metrics every 15s.
- ✅ 3.C.3 Dashboard JSON at `docs/grafana/dashboards/radio-gateway.json` (6 panels: bus level, PTT, link endpoints, stream kbps, transcription throughput, transcription p95 latency). Provisioning files at `docs/grafana/provisioning/{datasources,dashboards}/`.
- ✅ 3.C.4 **Verified:** `curl -u admin:radio http://192.168.2.109:3000/api/datasources/proxy/uid/prometheus/api/v1/query?query=rg_link_endpoint_up` returns all 3 endpoints = 1 (IC7100, kv4p-v, D75). Stream rate = 31.3 kbps via PromQL.
- 🟡 3.C.5 Gateway restart gap test deferred (no need to restart gateway again right now).

**Grafana access:** http://192.168.2.109:3000 — admin / radio
**Prometheus access:** http://192.168.2.109:9090
**Datasource UID:** `prometheus` (pinned in provisioning so dashboard JSON targeting works)
**Gotcha caught:** Grafana auto-assigns datasource UID on first provision; if you change the UID later you must wipe `media-stack_grafana_data` volume or Grafana refuses to re-provision (error: "data source not found"). Documented for future config changes.

## 3.D — Remaining metrics
- ⬜ 3.D.1 `rg_transcription_dispatched_total{engine}` — counter
- ⬜ 3.D.2 `rg_stream_reconnects_total{stream}` — counter
- ⬜ 3.D.3 `rg_link_audio_underruns_total{endpoint}` — counter
- ⬜ 3.D.4 `rg_cpu_temp_c`, `rg_fan_rpm{fan}` — gauges (reuse transcribe-worker status)
- ⬜ 3.D.5 `rg_vad_speech_events_total{bus}` — counter
- ⬜ 3.D.6 `rg_denoise_apply_ms{bus,engine}` — histogram in D13 worker

## 3.E — Alerting + Fleet Manager hook
- ⬜ 3.E.1 Prometheus alertmanager rules: stream down >2min, link down >1min, worker >85°C, denoise p99 >50ms
- ⬜ 3.E.2 Alertmanager → Telegram via existing notifier
- ⬜ 3.E.3 New `hourly.md` task that queries Prometheus for last-10min stream + link health
- ⬜ 3.E.4 **Test:** stop a link endpoint, confirm alert fires in Telegram within 90s

**Phase 3 acceptance:** Grafana dashboard live, six core metrics flowing, one alert end-to-end.

---

# PHASE 1 — Monolith split

## 1.A — gateway_core.py → core/ package
- ⬜ 1.A.1 Create `core/__init__.py` re-exporting `RadioGateway`, `__version__`, `LogWriter`
- ⬜ 1.A.2 Move `LogWriter` → `core/log_writer.py` ; smoke test gateway start
- ⬜ 1.A.3 Extract `_AudioProcMixin` → `core/audio_proc.py` ; smoke test VAD/HPF still work
- ⬜ 1.A.4 Extract `_PTTMixin` → `core/ptt.py` ; smoke test PTT round-trip
- ⬜ 1.A.5 Extract `_USBAudioMixin` → `core/usb_audio.py` ; smoke test AIOC detect
- ⬜ 1.A.6 Extract `_SetupMixin` → `core/setup.py` ; smoke test Mumble connect
- ⬜ 1.A.7 Extract `_MumbleIOMixin` → `core/mumble_io.py` ; smoke test `!speak`
- ⬜ 1.A.8 Extract `_TransmitMixin` → `core/transmit.py` ; smoke test TX
- ⬜ 1.A.9 Extract `_StreamMixin` → `core/stream.py` ; smoke test Broadcastify
- ⬜ 1.A.10 Extract `_LifecycleMixin` → `core/lifecycle.py` ; smoke test status loop
- ⬜ 1.A.11 Shrink `core/gateway.py` to <300 LOC ; smoke test full feature set
- ⬜ 1.A.12 **Acceptance:** every file in `core/` under 800 LOC, gateway boots cleanly, metrics still scrape

## 1.B — web_server.py split
- ⬜ 1.B.1 Extract `WebConfigServer` HTTP plumbing → `web/http_server.py`
- ⬜ 1.B.2 Extract auth helpers → `web/auth.py`
- ⬜ 1.B.3 Extract static-file serving → `web/static.py`
- ⬜ 1.B.4 Extract shared utilities → `web/util.py`
- ⬜ 1.B.5 **Acceptance:** all 20+ pages load, no file >800 LOC

## 1.C — gateway_mcp.py split
- ⬜ 1.C.1 Create `mcp/server.py` with shared `mcp` instance + config helpers
- ⬜ 1.C.2 Move SDR tools → `mcp/tools/sdr.py`
- ⬜ 1.C.3 Move radio tools → `mcp/tools/radio.py`
- ⬜ 1.C.4 Move transcribe tools → `mcp/tools/transcribe.py`
- ⬜ 1.C.5 Move packet tools → `mcp/tools/packet.py`
- ⬜ 1.C.6 Move stream tools → `mcp/tools/stream.py`
- ⬜ 1.C.7 Move system tools → `mcp/tools/system.py`
- ⬜ 1.C.8 Move manager tools → `mcp/tools/manager.py`
- ⬜ 1.C.9 Auto-import in `mcp/__init__.py`
- ⬜ 1.C.10 **Test:** Telegram bot can still call every tool category

**Phase 1 acceptance:** all monolith files split, full feature smoke pass, no file over 800 LOC.

---

# PHASE 2 — Real plugin system

## 2.A — Formalise contract
- ⬜ 2.A.1 Write `plugins/_base.py` with `RadioPlugin` Protocol
- ⬜ 2.A.2 Add `CAPABILITIES` set, optional `web_routes()`, `mcp_tools()`, lifecycle hooks
- ⬜ 2.A.3 Update `plugin_loader.py` to honour optional hooks
- ⬜ 2.A.4 Update `docs/plugin-development.md`
- ⬜ 2.A.5 Move `example_radio.py` → `examples/example_plugin/` with working dummy

## 2.B — Migrate KV4P (smallest)
- ⬜ 2.B.1 Move `kv4p_endpoints.py` → `plugins/kv4p.py`, wrap in class
- ⬜ 2.B.2 Add `ENABLE_KV4P` config (default true to match current behaviour)
- ⬜ 2.B.3 Remove direct imports from `gateway_core` ; use `self.plugins.get('kv4p')`
- ⬜ 2.B.4 **Test:** `/kv4p` page works, routing graph survives restart, MCP `kv4p_*` tools respond
- ⬜ 2.B.5 Cut a release (v3.9)

## 2.C — Migrate TH-9800
- ⬜ 2.C.1 Move `th9800_plugin.py` → `plugins/th9800.py`
- ⬜ 2.C.2 Validate CAT + AIOC + PTT routing via capability flags
- ⬜ 2.C.3 **Test:** TX, RX, CAT control, AIOC PTT
- ⬜ 2.C.4 Release v4.0

## 2.D — Migrate SDR
- ⬜ 2.D.1 Move `sdr_plugin.py` → `plugins/sdr.py`
- ⬜ 2.D.2 Plugin contributes its own `/sdr` web route via `web_routes()`
- ⬜ 2.D.3 **Test:** dual-tuner master/slave, channels, ADS-B unchanged
- ⬜ 2.D.4 Release v4.1

## 2.E — Migrate packet
- ⬜ 2.E.1 Move `packet_radio.py` + `packet_tnc.py` → `plugins/packet/`
- ⬜ 2.E.2 Direwolf + Pat + BBS via plugin lifecycle hooks
- ⬜ 2.E.3 **Test:** Winlink mail send/receive, APRS decode, BBS terminal
- ⬜ 2.E.4 Release v4.2

**Phase 2 acceptance:** all four radios under `plugins/`, adding a fifth requires zero core edits.

---

# Baseline measurements (filled in during Phase 0)

| File | LOC at start | LOC after |
|---|---|---|
| gateway_core.py | 3023 | |
| web_server.py | 2424 | |
| gateway_mcp.py | 3175 | |
| audio_sources.py | 2731 | |
| **plugins/ count** | 1 (example_radio.py only) | |
| **tests/ count** | 1 (test_ic7100_civ.py — currently 96/104 failing pre-existing) | |

---

# Session log (append-only — record what each work session did)

## 2026-05-28 — Plan created
- Wrote MEGA_PLAN.md, ordered Phase 3 → 1 → 2
- Confirmed prometheus_client not yet installed
- Repo clean on main, ahead 0 / behind 0
- Next action: Phase 0.1

## 2026-05-28 — Phase 0 + 3.A + 3.B done (code)
- prometheus-client 0.25.0 installed + added to requirements.txt
- `metrics.py` created — 13 metric objects, single REGISTRY
- `/metrics` route in `web_routes_get.handle_metrics` with LAN/Tailnet allowlist; wired in `web_server.py` GET dispatch
- 6 metrics wired into live code paths: bus_audio_level (bus_manager), bus_ptt_active (gateway_core), transcription_inflight + transcription_seconds + transcription_dispatched_total (transcriber), stream_bytes_sent_total (stream_stats), link_endpoint_up (gateway_link)
- All touched modules import cleanly. Standalone HTTPServer test of handle_metrics returns 200 + correct content-type + rg_* metrics.
- Pre-existing tests broken: tests/test_ic7100_civ.py — 96/104 fail with `AttributeError: 'CIVController' object has no attribute '_tls'` in tools/ic7100_link_plugin.py:378. Not in scope.
- **BLOCKED ON USER:** restart radio-gateway service so the running process picks up new /metrics route. Then run `curl http://localhost:8080/metrics | grep '^rg_'` and confirm metrics are emitting on live traffic.
- Next action after restart: Phase 3.C (Grafana on macmini)

## 2026-05-28 — Phase 3.C done (Grafana + Prometheus stack)
- User restarted gateway; /metrics emitting live data (3 link endpoints up, Broadcastify counter ticking)
- prometheus + grafana containers added to macmini docker-compose, both Up
- Datasource + dashboard provisioned; end-to-end query through Grafana proxy returns expected values
- Hit and fixed: Grafana auto-UID issue (had to wipe grafana_data volume after pinning uid=prometheus in provisioning)
- Next: Phase 3.D (remaining metrics) OR jump to Phase 1 (refactor) — recommend 3.D for quick wins, then 1.

## 2026-05-28 — Observability moved to gateway-local + installer captured
- User chose option 2: run Prom+Grafana locally on the gateway, not on macmini.
- Tore down macmini containers, restored docker-compose backup, dropped both volumes.
- Native pacman install on gateway: `prometheus` (3.11.3) + `grafana` (13.0.1).
- Lay down `/etc/prometheus/prometheus.yml` (scrape localhost:8080), Grafana provisioning at `/var/lib/grafana/conf/provisioning/{datasources,dashboards}/`, dashboard JSON at `/var/lib/grafana/dashboards/`.
- Datasource URL changed `http://prometheus:9090` → `http://localhost:9090` (no docker DNS now). Re-provisioned cleanly.
- `admin_password = radio` set in `/etc/grafana.ini` via sed.
- Both services enabled + started; end-to-end Grafana→Prometheus→/metrics query returns all 3 endpoints UP.
- **Installer updated** (`scripts/install.sh`):
  - New section 16 "Observability stack" before gateway start, idempotent.
  - Arch: `pacman -S prometheus grafana`. Debian: `apt install prometheus` + Grafana's apt repo (best-effort).
  - Handles both Arch path (`/var/lib/grafana/conf/provisioning`) and Debian path (`/etc/grafana/provisioning`).
  - Step counters bumped /15 → /16 throughout.
  - Health check: prometheus + grafana active state.
  - NEXT STEPS section now references the Grafana URL + default creds.
- **INSTALL.md updated**: phase 16 documented.
- Smoke: idempotent rerun of section 16 on this machine returns "already up to date", services stay active, queries still work.
- Access URLs now:
  - Grafana: http://localhost:3000 (admin / radio)
  - Prometheus: http://localhost:9090
- Next: Phase 3.D (remaining metrics) OR Phase 1 (refactor) — user choice.
