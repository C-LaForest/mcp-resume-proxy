# mcp-resume-proxy

> **MCP-SSE session-resumption sidecar.** Catches upstream `-32602` and EOF, re-runs `initialize`, replays pending requests — the client never sees the disconnect.

A tiny stdlib-Python proxy that sits in front of any MCP-SSE server. When the upstream server restarts (or rejects calls because its session state was wiped), the proxy transparently re-opens the upstream SSE connection, re-runs the `initialize` handshake, and replays any in-flight tool calls. The connected MCP client (Claude Code, etc.) stays on the same SSE stream the whole time and is unaware that anything happened.

- ~350 LOC of stdlib Python — no pip dependencies
- Threading (no async framework)
- Single file: `proxy.py`
- One Docker / podman container, one health endpoint, one config knob (`UPSTREAM_URL`)

## The problem

MCP clients (including Claude Code) auto-reconnect the MCP transport when an SSE connection drops — but they do NOT re-run `initialize` on the new connection. Every tool call after a server restart then fails:

```
MCP error -32602: Invalid request parameters
[server]   Received request before initialization was complete
```

Recovery today is either:
- **interactive `/mcp` reconnect** — unavailable in headless / CLI environments
- **full client restart** — drops conversation, context, and any pending work

This behaviour is documented in open MCP-client issues, none fixed at time of writing:

- [#27142 — Streamable HTTP client does not re-initialize after session invalidation](https://github.com/anthropics/claude-code/issues/27142)
- [#30224 — Auto-reconnect SSE MCP servers after server-side restart](https://github.com/anthropics/claude-code/issues/30224)
- [#54136 — Reconnect/restart MCP servers without full app restart](https://github.com/anthropics/claude-code/issues/54136)
- [#57207 — `claude mcp reconnect <name>` CLI subcommand](https://github.com/anthropics/claude-code/issues/57207)
- [#36308](https://github.com/anthropics/claude-code/issues/36308) · [#56937](https://github.com/anthropics/claude-code/issues/56937) · [#26112](https://github.com/anthropics/claude-code/issues/26112) · [#60061](https://github.com/anthropics/claude-code/issues/60061)

This proxy hides the bug from connected clients until the upstream client fixes it.

## How it works

```
Client (Claude Code, …)
   │ SSE :8767/sse
   ▼
mcp-resume-proxy            ← state: client_sid ↔ upstream_sid
   │  cached `initialize` body + pending JSON-RPC posts by id
   │  detects upstream EOF (restart) OR -32602 (session invalid)
   │  → opens fresh upstream SSE, replays initialize + pending posts
   │  → suppresses the duplicate init response + the original -32602
   ▼ SSE
Upstream MCP server (unchanged)
```

Per-client state the proxy holds:
- proxy-issued `client_sid` (UUID) — what the client sees
- current `upstream_sid` — what the upstream server's current session id is
- verbatim cached `initialize` request body (for replay)
- `initialized_notified` flag (for replaying `notifications/initialized` after reconnect)
- `pending_posts` dict keyed by JSON-RPC id (for replaying requests still awaiting a response)
- `init_response_forwarded` flag (so a replayed initialize doesn't produce a duplicate response to the client)

Reconnect triggers:
1. Upstream SSE stream EOFs (container restart, network drop, process death)
2. Upstream sends a JSON-RPC error event with code `-32602` (session known-invalid)

On either, the proxy: closes the old upstream response, opens a new upstream `GET /sse` to get a fresh `upstream_sid`, POSTs the cached `initialize` body, POSTs a synthetic `notifications/initialized` if appropriate, POSTs every still-pending request. The relay loop swaps to the new upstream response and continues. The duplicate `initialize` result (same JSON-RPC id) is suppressed; the original `-32602` is suppressed; the long-lived client SSE stream stays open the whole time.

## Quick start

### Docker Compose

```yaml
services:
  mcp-resume-proxy:
    build:
      context: .
      dockerfile: Containerfile
    container_name: mcp-resume-proxy
    ports: ["8767:8765"]
    environment:
      UPSTREAM_URL: http://mcp-server:8080
    restart: unless-stopped
```

```bash
docker compose up -d
curl http://localhost:8767/health
# {"status":"ok"}
```

### podman quadlet (system container)

See [`mcp-resume-proxy.container`](mcp-resume-proxy.container) — drop into `/etc/containers/systemd/`, then `systemctl daemon-reload && systemctl start mcp-resume-proxy`.

### Native Python

```bash
UPSTREAM_URL=http://mcp-server:8080 python proxy.py
```

Then point your client at `http://<host>:8767/sse` instead of the upstream's `/sse`.

## Configuration

All via environment variables. Stdlib only — no config files.

| Variable | Default | Description |
|---|---|---|
| `UPSTREAM_URL` | (required) | Full URL of the MCP-SSE server to front, no trailing slash. e.g. `http://mcp-server:8080` |
| `PROXY_PORT` | `8765` | Port the proxy listens on inside the container. Map to whatever you want on the host. |
| `UPSTREAM_TIMEOUT` | `30` | Seconds to wait for upstream HTTP responses. SSE stream reads are not bounded by this — only initial connect + each POST. |

## Endpoints

| Path | Behavior |
|---|---|
| `GET /sse` | Long-lived SSE. Allocates `client_sid`, opens upstream SSE, captures upstream session id, relays events, reconnects + replays on EOF / `-32602`. |
| `POST /messages/?session_id=<client_sid>` | Forwards JSON-RPC body to upstream `/messages/?session_id=<upstream_sid>`. Returns 202. Real response arrives via the SSE stream. Per-session bookkeeping (cached init, pending requests, `initialized_notified` flag) updated under lock. |
| `GET /health` | Liveness only — `{"status":"ok"}`. Use for container healthchecks or external monitoring. No version / topology info exposed. |

## Verify

```bash
curl http://localhost:8767/health
# {"status":"ok"}

curl -sN --max-time 3 http://localhost:8767/sse | head -3
# event: endpoint
# data: /messages/?session_id=<uuid>
```

Logs:
```
docker logs mcp-resume-proxy
# Look for "reconnect:" lines — that's the proxy doing its job after an upstream restart.
```

## Limitations

- **SSE transport only.** The newer Streamable HTTP MCP transport variant is not implemented — same class of bug exists there, but it caches `Mcp-Session-Id` differently. Tracked for v0.2.
- **No state preservation.** If the upstream MCP server holds session-only in-memory state (cursors, partial results), that state is lost when it restarts. The proxy replays the *handshake*, not the upstream server's internal state.
- **Restarting the proxy itself** drops every connected client's session — same problem the proxy hides, applied to itself. Minimize proxy restarts; treat it as a stable layer.
- **No auth / TLS termination.** Put it behind a reverse proxy or in a private network if exposed off-LAN.

## How this differs from other MCP gateways

| Tool | Approach | Fit for this problem |
|---|---|---|
| [IBM mcp-context-forge](https://github.com/ibm/mcp-context-forge) | Enterprise control plane, RBAC, federation | Overkill for a single-server reconnect |
| [Microsoft mcp-gateway](https://github.com/microsoft/mcp-gateway) | Kubernetes-native, multi-tenant, lifecycle mgmt | Overkill, K8s-centric |
| [decocms/mcp-mesh](https://github.com/decocms/mesh) | Open-source control plane, multi-tenant | Different problem scope |
| [FastMCP proxy](https://gofastmcp.com/servers/proxy) | "Fresh session per request" — re-inits on every tool call | Works, but pays an init handshake every call instead of only after a restart |
| **mcp-resume-proxy** | Tracks one session per client; only re-inits when upstream actually restarts | Lowest overhead, smallest footprint, exact-fit for this bug |

If you need auth/policy/federation/multi-tenant: use one of the gateways. If you need a small reliable thing in front of one MCP server: this.

## Status

Pre-1.0. Used in production by the author against a real MCP-SSE deployment. Wider use will surface edge cases — file issues with logs.

When the upstream MCP-client bug is fixed natively, this proxy becomes optional. Until then it's the smallest workaround that keeps long-running client sessions stable across server restarts.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).
