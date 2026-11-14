"""Entry point: ``mcp-prometheus`` (stdio by default) or ``mcp-prometheus --transport streamable-http``."""

from __future__ import annotations

import argparse
import logging
import os

from .prom import Settings
from .server import build_server


def settings_from_env() -> Settings:
    env = os.environ.get
    return Settings(
        url=env("PROMETHEUS_URL", "http://localhost:9090"),
        token=env("PROMETHEUS_TOKEN") or None,
        username=env("PROMETHEUS_USERNAME") or None,
        password=env("PROMETHEUS_PASSWORD") or None,
        timeout_seconds=float(env("PROMETHEUS_TIMEOUT", "15")),
        max_series=int(env("MCP_PROM_MAX_SERIES", "20")),
        max_points=int(env("MCP_PROM_MAX_POINTS", "120")),
        verify_tls=env("PROMETHEUS_VERIFY_TLS", "true").lower() != "false",
    )


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="mcp-prometheus", description="Read-only Prometheus MCP server.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1", help="for streamable-http")
    parser.add_argument("--port", type=int, default=8000, help="for streamable-http")
    args = parser.parse_args(argv)
    # Per-request HTTP logs are noise for an MCP client; keep warnings and errors.
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    server = build_server(settings_from_env())
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port, stateless_http=True)


if __name__ == "__main__":
    main()
