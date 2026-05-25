#!/bin/bash
# test/smoke.sh — manual smoke test scaffold.
#
# Goal: prove the proxy survives an upstream restart and the client never sees
# it. Requires a running MCP-SSE server you can restart.
#
# Steps (manual):
#
#   1. Start your MCP server (or a mock) — note its URL, e.g. http://localhost:8080
#
#   2. Start the proxy pointing at it:
#        UPSTREAM_URL=http://localhost:8080 PROXY_PORT=8767 python proxy.py
#
#   3. Connect a client to the proxy (http://localhost:8767/sse) and run any
#      tool call. Verify it succeeds.
#
#   4. Restart the upstream MCP server.
#
#   5. From the same client, run another tool call.
#      Expected: it succeeds. Proxy logs should show a "reconnect:" line and
#      a "replaying initialize" line. The client should NOT see -32602.
#
# Pass: client tool calls keep working across the upstream restart.
# Fail: client sees -32602 or hangs.
#
# A proper automated smoke test with a mock upstream is planned for v0.2 —
# contributions welcome.

set -euo pipefail
echo "smoke.sh is a manual procedure — read this file's header for steps."
exit 0
