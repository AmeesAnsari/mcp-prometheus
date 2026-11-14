"""Unit tests: a fake Prometheus behind httpx2.MockTransport, driven through a real MCP client."""

import httpx2 as httpx
import pytest
from mcp import Client

from mcp_prometheus import formatting
from mcp_prometheus.prom import PrometheusClient, Settings
from mcp_prometheus.server import build_server

pytestmark = pytest.mark.anyio

T = 1794680000.0


def ok(data, warnings=None):
    body = {"status": "success", "data": data}
    if warnings:
        body["warnings"] = warnings
    return httpx.Response(200, json=body)


class FakeProm:
    """Routes /api/v1/<path> to canned responses and records every request."""

    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/")
        handler = self.routes.get(path)
        if handler is None:
            return httpx.Response(404, json={"status": "error", "errorType": "not_found", "error": path})
        return handler(request) if callable(handler) else handler


def make(routes, **settings):
    fake = FakeProm(routes)
    s = Settings(url="http://prom.test", **settings)
    server = build_server(s, PrometheusClient(s, transport=httpx.MockTransport(fake)))
    return server, fake


async def call(server, name, args=None):
    async with Client(server) as c:
        r = await c.call_tool(name, args or {})
        return r.is_error, "\n".join(getattr(b, "text", "") for b in r.content)


def vector(*pairs):
    return {"resultType": "vector",
            "result": [{"metric": {"__name__": "up", "job": j}, "value": [T, str(v)]} for j, v in pairs]}


# ---------------------------------------------------------------- tools


async def test_all_tools_are_read_only():
    server, _ = make({})
    async with Client(server) as c:
        tools = (await c.list_tools()).tools
    assert {t.name for t in tools} == {"list_metrics", "describe_metric", "query", "query_range",
                                       "list_targets", "list_alerts"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)


async def test_instant_query_sorts_and_caps_series():
    server, fake = make({"query": ok(vector(("a", 1), ("b", 5), ("c", 3)))}, max_series=2)
    err, text = await call(server, "query", {"promql": "up"})
    assert not err
    assert text.splitlines()[0].startswith("3 series, showing top 2 by value")
    assert 'up{job="b"} = 5' in text and 'up{job="c"} = 3' in text and 'job="a"' not in text
    assert fake.requests[0].url.params["timeout"] == "15s"


async def test_range_query_auto_step_caps_points():
    values = [[T + i * 15, str(i)] for i in range(500)]
    matrix = {"resultType": "matrix", "result": [{"metric": {"__name__": "x"}, "values": values}]}
    server, fake = make({"query_range": ok(matrix)}, max_points=60)
    err, text = await call(server, "query_range", {"promql": "x", "minutes": 120, "step_seconds": 1})
    assert not err
    # 2h / 60 points = 120s minimum step; a smaller requested step is raised to it.
    assert fake.requests[0].url.params["step"] == "120s"
    assert "min=0 max=499 avg=249.5 last=499" in text
    timeline = text.splitlines()[-1].split()
    assert len(timeline) <= 61 and timeline[-1].endswith("=499")  # downsampled, latest point kept


async def test_prometheus_errors_become_tool_errors():
    bad = httpx.Response(400, json={"status": "error", "errorType": "bad_data", "error": "parse error"})
    server, _ = make({"query": bad})
    err, text = await call(server, "query", {"promql": "sum(("})
    assert err and "bad_data" in text and "parse error" in text


async def test_non_json_and_unreachable():
    server, _ = make({"query": httpx.Response(502, text="<html>bad gateway</html>")})
    err, text = await call(server, "query", {"promql": "up"})
    assert err and "HTTP 502" in text

    def boom(request):
        raise httpx.ConnectError("connection refused", request=request)

    server, _ = make({"query": boom})
    err, text = await call(server, "query", {"promql": "up"})
    assert err and "Could not reach Prometheus" in text


async def test_query_length_is_capped():
    server, fake = make({}, max_query_chars=10)
    err, text = await call(server, "query", {"promql": "x" * 11})
    assert err and "longer than 10" in text and not fake.requests


async def test_argument_validation():
    server, _ = make({})
    err, text = await call(server, "query_range", {"promql": "up", "minutes": 0})
    assert err and "minutes" in text


async def test_list_metrics_filter():
    server, _ = make({"label/__name__/values": ok(["http_requests_total", "up", "HTTP_errors"])})
    err, text = await call(server, "list_metrics", {"contains": "http"})
    assert text == "2 metric names containing 'http':\nhttp_requests_total\nHTTP_errors"


async def test_describe_counter():
    server, _ = make({
        "metadata": ok({"reqs": [{"type": "counter", "help": "Requests.", "unit": ""}]}),
        "series": ok([{"__name__": "reqs", "code": "200", "job": "api"},
                      {"__name__": "reqs", "code": "500", "job": "api"}]),
    })
    err, text = await call(server, "describe_metric", {"metric": "reqs"})
    assert "type=counter" in text and "code: 200, 500" in text and "rate(reqs[5m])" in text


async def test_targets_and_alerts():
    server, _ = make({
        "targets": ok({"activeTargets": [
            {"health": "up", "labels": {"job": "api", "instance": "a:1"}, "lastScrapeDuration": 0.004},
            {"health": "down", "labels": {"job": "db", "instance": "b:2"}, "lastError": "connection refused"},
        ]}),
        "alerts": ok({"alerts": [
            {"state": "pending", "labels": {"alertname": "Slow"}, "annotations": {}, "activeAt": "2026-11-14T08:00:00Z"},
            {"state": "firing", "labels": {"alertname": "Down", "job": "db"},
             "annotations": {"summary": "db is down"}, "activeAt": "2026-11-14T07:59:00Z"},
        ]}),
    })
    _, targets = await call(server, "list_targets", {"only_unhealthy": True})
    assert targets.splitlines() == ["1/2 targets up.", "- [down] job=db instance=b:2 error: connection refused"]
    _, alerts = await call(server, "list_alerts")
    assert alerts.splitlines()[1].startswith("- [firing] Down (job=db)")  # firing sorted first


async def test_bearer_token_sent():
    server, fake = make({"alerts": ok({"alerts": []})}, token="s3cret")
    await call(server, "list_alerts")
    assert fake.requests[0].headers["authorization"] == "Bearer s3cret"


# ---------------------------------------------------------------- formatting


def test_format_values_and_labels():
    assert formatting.fmt_value("3.0") == "3" and formatting.fmt_value("0.1234567891") == "0.123457"
    assert formatting.fmt_value("NaN") == "NaN"
    assert formatting.fmt_labels({"__name__": "up", "job": "x"}) == 'up{job="x"}'
    assert formatting.fmt_labels({"job": "x"}) == '{job="x"}'


def test_scalar_and_empty_results():
    assert formatting.instant({"resultType": "scalar", "result": [T, "2"]}, [], 5).startswith("scalar 2 at")
    assert "No series matched" in formatting.instant({"resultType": "vector", "result": []}, [], 5)
    assert "Prometheus warning: partial" in formatting.instant(vector(("a", 1)), ["partial"], 5)
