# Changelog

All notable changes to this project will be documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-24

Initial release.

### Added

- **`proxy.py`** — single-file stdlib-Python MCP-SSE session-resumption proxy
  (~350 LOC, no pip dependencies). Threaded `http.server.ThreadingHTTPServer`.
  Tracks per-client state (`client_sid` ↔ `upstream_sid`, cached `initialize`
  body, `pending_posts` by JSON-RPC id, `initialized_notified` flag,
  `init_response_forwarded` suppress flag). On upstream SSE EOF or `-32602`,
  opens a fresh upstream connection, replays cached `initialize` + pending
  POSTs, suppresses the duplicate init response and the original `-32602`.
  Long-lived client SSE stream stays open across upstream restarts.
- **`Containerfile`** — `FROM python:3.12-slim`, stdlib-only build. `UPSTREAM_URL`
  required at runtime (no default).
- **`mcp-resume-proxy.container`** — sample podman quadlet (system container,
  port `8767:8765`, `--memory=64m --cpus=0.25`, `Restart=on-failure`).
- **`docker-compose.yml`** — alternative deploy with healthcheck and resource
  limits.
- **`README.md`** — problem statement (cites open MCP-client issues #27142,
  #30224, #36308, #54136, #56937, #57207, #26112, #60061), architecture
  diagram, quick-start (compose / podman / native), config table, endpoint
  reference, limitations, comparison vs other MCP gateways
  (IBM mcp-context-forge, MS mcp-gateway, decocms/mcp-mesh, FastMCP proxy).
- **`CONTRIBUTING.md`** — scope rules, dev conventions (stdlib only, no pip),
  issue-template fields.
- **`LICENSE`** — MIT.
- **`.github/workflows/ci.yml`** — Python syntax check (`py_compile`), bash
  syntax check on test scripts, container build on PRs and pushes to `main`.
- **`.github/workflows/publish.yml`** — multi-arch container image
  (`linux/amd64`, `linux/arm64`) published to
  `ghcr.io/c-laforest/mcp-resume-proxy` on tagged releases.
- **`test/smoke.sh`** — manual restart-survival test procedure (scaffold; an
  automated mock-upstream test is planned for v0.2).

### Endpoints exposed by the proxy

- `GET /sse` — long-lived SSE. Allocates `client_sid`, opens upstream SSE,
  relays events with transparent reconnect on EOF / `-32602`.
- `POST /messages/?session_id=<client_sid>` — forwards to upstream
  `/messages/?session_id=<upstream_sid>`. Returns 202; real response arrives
  via the SSE stream.
- `GET /health` — `{"status":"ok"}` liveness only.

### Known limitations

- **SSE transport only.** The newer Streamable HTTP MCP transport variant is
  not implemented yet — same class of upstream-restart bug exists there but
  differs in mechanics (caches `Mcp-Session-Id` header). Tracked for v0.2.
- **No state preservation.** The proxy replays the handshake, not the
  upstream server's internal state (cursors, partial results, etc.).
- **Restarting the proxy itself drops every connected client's session** —
  same problem the proxy hides, applied to itself. Treat as a stable layer.
- **No auth / TLS termination.** Put behind a reverse proxy if exposed off-LAN.
