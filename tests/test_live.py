"""End-to-end tests against a real Prometheus. Skipped unless PROMETHEUS_URL is set.

CI runs Prometheus as a service container; locally:
    PROMETHEUS_URL=http://localhost:9090 pytest tests/test_live.py
"""

import os
import sys

import pytest
from mcp import Client, StdioServerParameters

from mcp_prometheus.prom import Settings
from mcp_prometheus.server import build_server

URL = os.environ.get("PROMETHEUS_URL")
pytestmark = [pytest.mark.anyio, pytest.mark.skipif(not URL, reason="PROMETHEUS_URL not set")]


def text(result):
    return "\n".join(getattr(b, "text", "") for b in result.content)


async def test_tools_against_real_prometheus():
    async with Client(build_server(Settings(url=URL))) as c:
        targets = await c.call_tool("list_targets", {})
        assert not targets.is_error and "targets up" in text(targets)

        up = await c.call_tool("query", {"promql": "up"})
        assert not up.is_error and "up{" in text(up)

        rng = await c.call_tool("query_range", {"promql": "up", "minutes": 5})
        assert not rng.is_error and "points" in text(rng)

        names = await c.call_tool("list_metrics", {"contains": "prometheus_build"})
        assert "prometheus_build_info" in text(names)

        bad = await c.call_tool("query", {"promql": "sum(("})
        assert bad.is_error and "bad_data" in text(bad)


async def test_stdio_entry_point():
    """Launch the server the way an MCP client (Claude Desktop, an IDE) would: as a subprocess over stdio."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_prometheus"],
                                   env={**os.environ, "PROMETHEUS_URL": URL})
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "query_range" in names
        result = await c.call_tool("query", {"promql": "vector(42)"})
        assert "= 42" in text(result)
