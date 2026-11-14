"""The MCP server: read-only Prometheus tools for any MCP client."""

from __future__ import annotations

import math
import time as _time
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import formatting
from .prom import PrometheusClient, PrometheusError, Settings

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

INSTRUCTIONS = """Read-only access to a Prometheus server.

Typical flow: list_metrics to find names, describe_metric to learn type and labels, then query (current value)
or query_range (how it changed). Use rate() on counters. list_targets shows scrape health; list_alerts shows
what is firing. Results are summarised and capped; narrow the query with label matchers or aggregation
(sum by, topk) rather than asking for everything."""


def build_server(settings: Settings, client: Optional[PrometheusClient] = None) -> MCPServer:
    prom = client or PrometheusClient(settings)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            if client is None:  # we created it, we close it
                await prom.aclose()

    mcp = MCPServer("prometheus", title="Prometheus", instructions=INSTRUCTIONS, version="0.1.0", lifespan=lifespan)

    async def call(coro):
        try:
            return await coro
        except PrometheusError as exc:
            # ToolError becomes an isError result the model can read and correct.
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=READ_ONLY)
    async def list_metrics(
        contains: Annotated[str, Field(description="Case-insensitive substring filter, e.g. 'http_request'. Empty for all.")] = "",
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum names to return.")] = 100,
    ) -> str:
        """List metric names known to Prometheus, optionally filtered by substring."""
        names, warnings = await call(prom.metric_names())
        matched = [n for n in names if contains.lower() in n.lower()]
        if not matched:
            return f"No metric names contain {contains!r} ({len(names)} metrics exist)."
        shown = matched[:limit]
        head = f"{len(matched)} metric names" + (f" containing {contains!r}" if contains else "")
        head += f", showing {len(shown)}" if len(matched) > len(shown) else ""
        return head + ":\n" + "\n".join(shown)

    @mcp.tool(annotations=READ_ONLY)
    async def describe_metric(
        metric: Annotated[str, Field(min_length=1, description="Exact metric name, e.g. 'http_requests_total'.")],
    ) -> str:
        """Show a metric's type, help text and unit, plus its label names and example values from the last hour."""
        meta, _ = await call(prom.metadata(metric))
        now = _time.time()
        series, _ = await call(prom.series(metric, now - 3600, now))
        lines = []
        for entry in meta.get(metric, [])[:1]:
            lines.append(f"{metric}: type={entry.get('type')}, unit={entry.get('unit') or 'none'}")
            lines.append(f"help: {entry.get('help')}")
        if not lines:
            lines.append(f"{metric}: no metadata (it may be a recording rule or not exist).")
        if not series:
            lines.append("No series in the last hour.")
            return "\n".join(lines)
        labels: dict[str, set[str]] = {}
        for s in series:
            for k, v in s.items():
                if k != "__name__":
                    labels.setdefault(k, set()).add(v)
        lines.append(f"{len(series)} series in the last hour. Labels:")
        for k in sorted(labels):
            values = sorted(labels[k])
            more = f" (+{len(values) - 8} more)" if len(values) > 8 else ""
            lines.append(f"  {k}: {', '.join(values[:8])}{more}")
        if meta.get(metric, [{}])[0].get("type") == "counter":
            lines.append(f"Tip: this is a counter; use rate({metric}[5m]) rather than the raw value.")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def query(
        promql: Annotated[str, Field(description="PromQL expression, e.g. 'sum by (job) (rate(http_requests_total[5m]))'.")],
        time: Annotated[str, Field(description="Evaluation time as RFC3339 or Unix seconds. Empty for now.")] = "",
    ) -> str:
        """Evaluate PromQL at a single point in time (an instant query). Series are sorted by value, largest first."""
        (data, warnings) = await call(prom.query(promql, time or None))
        return formatting.instant(data, warnings, settings.max_series)

    @mcp.tool(annotations=READ_ONLY)
    async def query_range(
        promql: Annotated[str, Field(description="PromQL expression to evaluate over time.")],
        minutes: Annotated[int, Field(ge=1, le=10080, description="Window length, ending now (max 7 days).")] = 60,
        step_seconds: Annotated[int, Field(ge=0, le=86400, description="Resolution. 0 picks one automatically.")] = 0,
    ) -> str:
        """Evaluate PromQL over a time window ending now: per-series min/max/avg/last plus a downsampled timeline."""
        end = _time.time()
        start = end - minutes * 60
        auto = max(math.ceil(minutes * 60 / settings.max_points), 1)
        step = max(step_seconds or auto, auto)  # never return more than max_points per series
        (data, warnings) = await call(prom.query_range(promql, start, end, step))
        return formatting.matrix(data, warnings, settings.max_series, settings.max_points, step)

    @mcp.tool(annotations=READ_ONLY)
    async def list_targets(
        only_unhealthy: Annotated[bool, Field(description="Show only targets that are not up.")] = False,
    ) -> str:
        """Show scrape targets with health, last error and scrape duration."""
        data, _ = await call(prom.targets())
        targets = data.get("activeTargets", [])
        rows = [t for t in targets if not only_unhealthy or t.get("health") != "up"]
        if not rows:
            return "All targets are up." if only_unhealthy else "No active targets."
        up = sum(1 for t in targets if t.get("health") == "up")
        lines = [f"{up}/{len(targets)} targets up."]
        for t in rows:
            labels = t.get("labels", {})
            err = f" error: {t['lastError']}" if t.get("lastError") else ""
            dur = t.get("lastScrapeDuration")
            took = f", scrape {dur * 1000:.0f}ms" if isinstance(dur, (int, float)) else ""
            lines.append(f"- [{t.get('health')}] job={labels.get('job')} instance={labels.get('instance')}{took}{err}")
        return "\n".join(lines)

    @mcp.tool(annotations=READ_ONLY)
    async def list_alerts() -> str:
        """Show alerts that are currently pending or firing, with labels and annotations."""
        data, _ = await call(prom.alerts())
        alerts = data.get("alerts", [])
        if not alerts:
            return "No alerts are pending or firing."
        lines = [f"{len(alerts)} alert(s):"]
        for a in sorted(alerts, key=lambda a: (a.get("state") != "firing", a["labels"].get("alertname", ""))):
            labels = a.get("labels", {})
            name = labels.get("alertname", "?")
            rest = ", ".join(f"{k}={v}" for k, v in sorted(labels.items()) if k != "alertname")
            summary = a.get("annotations", {}).get("summary") or a.get("annotations", {}).get("description", "")
            lines.append(f"- [{a.get('state')}] {name} ({rest}) since {a.get('activeAt', '?')[:19]}Z"
                         + (f": {summary}" if summary else ""))
        return "\n".join(lines)

    @mcp.prompt()
    def investigate(service: str) -> str:
        """Start an investigation of one service using the Prometheus tools."""
        return (f"Investigate the health of '{service}' using the Prometheus tools. Check list_alerts and "
                f"list_targets first, then find its metrics with list_metrics, and use query_range to see how "
                f"error rate, latency and traffic changed over the last hour. Summarise what is wrong, since when, "
                f"and which metrics show it.")

    return mcp
