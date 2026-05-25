FROM docker.io/library/python:3.12-slim

LABEL org.opencontainers.image.title="mcp-resume-proxy"
LABEL org.opencontainers.image.description="Transparent MCP-SSE session-resumption proxy"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/C-LaForest/mcp-resume-proxy"

# stdlib only — no pip dependencies
COPY proxy.py /app/proxy.py

ENV PROXY_PORT=8765
ENV UPSTREAM_TIMEOUT=30
# UPSTREAM_URL has no default — must be set at runtime, e.g.
#   podman run -e UPSTREAM_URL=http://mcp-server:8080 ...

EXPOSE 8765

CMD ["python", "-u", "/app/proxy.py"]
