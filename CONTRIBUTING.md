# Contributing

Issues and PRs welcome. This is a small, focused tool — keep contributions in scope.

## Scope

In scope:
- Bug fixes in the SSE reconnect / replay logic
- Better edge-case handling (network blips, malformed events, slow upstreams)
- Smoke tests / integration tests
- Documentation improvements

Likely out of scope:
- Adding pip dependencies (project goal: stdlib only)
- Rewriting in another language
- Auth / TLS termination (put behind a reverse proxy)
- Streamable HTTP transport (see open issues — separate work; PR welcome but discuss first)

## Development

- Python 3.10+.
- No pip deps.
- Style: 4-space indent, ~100-char lines, type hints where useful.
- Verify: `python -m py_compile proxy.py` before committing.

## Testing changes that touch reconnect logic

Bare minimum: run `test/smoke.sh` and document the result. For PRs that touch
the reconnect / replay path, include a description of what failure modes you
exercised manually.

## Filing issues

Include:
- What MCP client you use (e.g. "Claude Code 2.x", "custom MCP client", etc.)
- What upstream MCP server you put behind the proxy
- What you expected to see vs. what happened
- Proxy logs around the failure (`journalctl -u mcp-resume-proxy` or `docker logs`)
- Proxy version (git commit hash if built from source)

## License

By contributing you agree your contribution is MIT-licensed.
