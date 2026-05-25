#!/usr/bin/env python3
"""mcp-resume-proxy — transparent MCP-SSE session-resumption proxy.

Sits in front of an MCP-SSE server. Each connected client gets a proxy-issued
session_id mapped 1:1 to an upstream session_id that the proxy holds. When the
upstream server restarts (SSE stream EOFs) OR rejects calls with JSON-RPC
error -32602 ("Received request before initialization was complete"), the
proxy:

  1. Opens a fresh upstream SSE connection (gets a new upstream session_id).
  2. Replays the client's cached `initialize` request against the new session.
  3. Replays any not-yet-responded POSTs whose ids are still pending.
  4. Suppresses the duplicate `initialize` response and the original -32602 so
     the client never sees the disconnect.

The long-lived client→proxy SSE stream stays open the whole time; only the
upstream SSE is recreated.

Why this exists: MCP clients (including Claude Code) auto-reconnect the MCP
transport but do NOT re-run `initialize` after a server restart — every
subsequent tool call then fails -32602 until the user runs `/mcp` (interactive
only) or restarts the client (loses conversation). See open MCP-client issues:

  https://github.com/anthropics/claude-code/issues/27142
  https://github.com/anthropics/claude-code/issues/30224
  https://github.com/anthropics/claude-code/issues/54136
  https://github.com/anthropics/claude-code/issues/57207

This proxy hides that bug.

Stdlib only — no pip dependencies. Threading. Python 3.10+.

Configuration (environment variables):
  UPSTREAM_URL       — required, e.g. http://mcp-server:8080
  PROXY_PORT         — default 8765
  UPSTREAM_TIMEOUT   — default 30 (seconds)
"""
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

UPSTREAM_URL = os.environ.get("UPSTREAM_URL", "").rstrip("/")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8765"))
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "30"))

if not UPSTREAM_URL:
    print("FATAL: UPSTREAM_URL env var is required (e.g. http://mcp-server:8080)", file=sys.stderr)
    sys.exit(2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("mcp-resume-proxy")


# ── Per-client state ──
class SessionState:
    """Tracks one client's MCP session and its current upstream binding."""
    def __init__(self, client_sid: str):
        self.client_sid = client_sid
        self.upstream_sid = None              # current upstream session_id
        self.upstream_resp = None             # urllib SSE response from upstream
        self.initialize_body = None           # bytes — verbatim client `initialize` POST body, for replay
        self.initialize_id = None             # JSON-RPC id of the initialize request
        self.init_response_forwarded = False  # have we already forwarded an initialize result to the client?
        self.initialized_notified = False     # has client sent `notifications/initialized`?
        self.pending_posts = {}               # rid -> body bytes (in-flight requests awaiting response)
        self.alive = True
        self.lock = threading.Lock()


SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


# ── SSE I/O helpers ──
def write_sse_event(wfile, event_type: str, data: str) -> bool:
    """Send one SSE event downstream. Returns False on write failure."""
    try:
        wfile.write(f"event: {event_type}\ndata: {data}\n\n".encode())
        wfile.flush()
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def read_sse_event(resp):
    """Read one SSE event from an upstream response.
    Returns dict {type, data, json} on event, or None on EOF/read error."""
    event_type = "message"
    data_lines = []
    while True:
        try:
            line = resp.readline()
        except Exception:
            return None
        if not line:
            return None
        line = line.rstrip(b"\r\n").decode("utf-8", errors="replace")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    parsed = json.loads(data)
                except Exception:
                    parsed = None
                return {"type": event_type, "data": data, "json": parsed}
            event_type = "message"
            continue
        if line.startswith(":"):
            continue  # SSE comment / keep-alive
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        # other field types (id:, retry:) are ignored


# ── Upstream operations ──
def _open_upstream_sse():
    """Open a fresh GET /sse against UPSTREAM_URL, read the mandatory
    `endpoint` event, return (response, upstream_session_id). Raises on failure."""
    req = urllib.request.Request(
        f"{UPSTREAM_URL}/sse",
        headers={"Accept": "text/event-stream"},
    )
    resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
    ev = read_sse_event(resp)
    if not ev or ev["type"] != "endpoint":
        try: resp.close()
        except: pass
        raise RuntimeError(f"upstream did not emit endpoint event first: {ev}")
    qs = urlparse(ev["data"]).query
    sids = parse_qs(qs).get("session_id", [])
    if not sids:
        try: resp.close()
        except: pass
        raise RuntimeError(f"no session_id in endpoint event: {ev['data']!r}")
    return resp, sids[0]


def _post_to_upstream(upstream_sid: str, body_bytes: bytes) -> int:
    """POST a JSON-RPC body to upstream /messages?session_id=<sid>.
    Returns HTTP status code (or 0 on transport failure)."""
    url = f"{UPSTREAM_URL}/messages/?session_id={upstream_sid}"
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        log.error("upstream POST failed: %s", e)
        return 0


def _is_session_invalid_error(ev_json) -> bool:
    """Detect the JSON-RPC error MCP servers emit for an uninitialized session
    (the symptom of an upstream restart): code == -32602."""
    if not isinstance(ev_json, dict):
        return False
    err = ev_json.get("error")
    return isinstance(err, dict) and err.get("code") == -32602


def _reconnect_upstream(state: SessionState) -> bool:
    """Replace state.upstream_resp + upstream_sid with a fresh upstream SSE,
    then replay the cached initialize + notifications/initialized + any
    still-pending POSTs. Returns True on success."""
    with state.lock:
        log.info("reconnect: client_sid=%s old_upstream_sid=%s",
                 state.client_sid[:8], (state.upstream_sid or "")[:8])
        if state.upstream_resp:
            try: state.upstream_resp.close()
            except: pass
            state.upstream_resp = None
        try:
            new_resp, new_sid = _open_upstream_sse()
        except Exception as e:
            log.error("reconnect: upstream unreachable: %s", e)
            return False
        state.upstream_resp = new_resp
        state.upstream_sid = new_sid
        # Re-run init handshake — this is the whole point of this proxy.
        if state.initialize_body:
            log.info("reconnect: replaying initialize against upstream %s", new_sid[:8])
            _post_to_upstream(new_sid, state.initialize_body)
        if state.initialized_notified:
            _post_to_upstream(
                new_sid,
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}',
            )
        # Replay any in-flight requests whose responses never arrived.
        for rid, body in list(state.pending_posts.items()):
            log.info("reconnect: replaying pending request id=%r", rid)
            _post_to_upstream(new_sid, body)
        return True


# ── HTTP handler ──
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            data = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/sse":
            return self._handle_sse()
        self.send_error(404, "not found")

    def _handle_sse(self):
        """Long-lived: open upstream SSE, allocate client_sid, relay events to
        client, transparently reconnect on EOF/-32602. Returns when client
        disconnects or reconnect fails permanently."""
        cid = uuid.uuid4().hex
        state = SessionState(cid)
        with SESSIONS_LOCK:
            SESSIONS[cid] = state

        try:
            # Open upstream FIRST — if it's down we want to fail before sending
            # the client any SSE headers (so they see 502 immediately).
            try:
                resp, upstream_sid = _open_upstream_sse()
            except Exception as e:
                log.error("initial upstream open failed: %s", e)
                self.send_error(502, "upstream unreachable")
                return
            state.upstream_resp = resp
            state.upstream_sid = upstream_sid

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            if not write_sse_event(self.wfile, "endpoint", f"/messages/?session_id={cid}"):
                state.alive = False
                return

            # Relay loop. Each iteration reads ONE event from the CURRENT upstream
            # (state.upstream_resp may be swapped by a reconnect).
            while state.alive:
                current_resp = state.upstream_resp
                ev = read_sse_event(current_resp)
                if ev is None:
                    log.warning("upstream EOF; attempting reconnect (client %s)", cid[:8])
                    if not _reconnect_upstream(state):
                        log.error("reconnect failed permanently; closing client session")
                        break
                    continue

                ej = ev.get("json")

                # 1) -32602: upstream rejecting because session is uninitialized.
                #    Hide from client, reconnect + replay, continue.
                if _is_session_invalid_error(ej):
                    log.info("upstream -32602 (session invalid); hiding from client, reconnecting")
                    if not _reconnect_upstream(state):
                        break
                    continue

                # 2) Duplicate initialize response: after a reconnect we POST
                #    `initialize` again and upstream sends back an init result.
                #    Client already got an init result for this id; suppress.
                if (isinstance(ej, dict)
                        and ej.get("id") is not None
                        and state.initialize_id is not None
                        and ej.get("id") == state.initialize_id):
                    if state.init_response_forwarded:
                        log.info("suppressing duplicate initialize result (id=%r)", ej.get("id"))
                        continue
                    state.init_response_forwarded = True

                # 3) Clear pending tracking on successful response.
                if isinstance(ej, dict) and "id" in ej and "error" not in ej:
                    with state.lock:
                        state.pending_posts.pop(ej["id"], None)

                # 4) Forward to client.
                if not write_sse_event(self.wfile, ev["type"], ev["data"]):
                    state.alive = False
                    break
        finally:
            state.alive = False
            with SESSIONS_LOCK:
                SESSIONS.pop(cid, None)
            try:
                if state.upstream_resp:
                    state.upstream_resp.close()
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        # MCP-SSE convention: client POSTs to /messages/?session_id=<sid>
        if parsed.path not in ("/messages/", "/messages"):
            self.send_error(404, "not found")
            return
        cids = parse_qs(parsed.query).get("session_id", [])
        if not cids:
            self.send_error(400, "missing session_id")
            return
        cid = cids[0]
        with SESSIONS_LOCK:
            state = SESSIONS.get(cid)
        if not state or not state.alive:
            self.send_error(404, "unknown session")
            return

        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n > 0 else b""

        # Inspect the request to update per-session bookkeeping.
        try:
            pb = json.loads(body)
        except Exception:
            pb = None
        if isinstance(pb, dict):
            method = pb.get("method")
            with state.lock:
                if method == "initialize":
                    state.initialize_body = body
                    state.initialize_id = pb.get("id")
                elif method == "notifications/initialized":
                    state.initialized_notified = True
                else:
                    rid = pb.get("id")
                    if rid is not None:
                        state.pending_posts[rid] = body

        # Forward to upstream. Snapshot upstream_sid under lock to avoid racing
        # with an in-progress reconnect.
        with state.lock:
            upstream_sid = state.upstream_sid

        _post_to_upstream(upstream_sid, body)

        # MCP-SSE convention: response (if any) arrives via the SSE stream.
        # The POST itself returns 202 immediately.
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


def main():
    log.info("mcp-resume-proxy starting on :%d → %s", PROXY_PORT, UPSTREAM_URL)
    srv = ThreadingHTTPServer(("0.0.0.0", PROXY_PORT), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
