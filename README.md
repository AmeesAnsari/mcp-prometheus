# mcp-prometheus

An [MCP](https://modelcontextprotocol.io) server that gives any AI client **read-only** access to Prometheus: find metrics, run PromQL, see how things changed over time, and check scrape health and firing alerts.

Results are **summarised for an LLM**: series are capped, range queries are downsampled to a fixed number of points, and each series leads with min/max/avg/last. A question about latency costs a few hundred tokens instead of megabytes of JSON.

Built on the official MCP Python SDK v2 and the **2026-07-28** spec (stateless), with stdio and streamable HTTP transports.

## Tools

| Tool | What it does |
|---|---|
| `list_metrics` | Metric names, filtered by substring |
| `describe_metric` | Type, help text, unit, and label names with example values; suggests `rate()` for counters |
| `query` | Instant PromQL query; series sorted by value so "top N" questions work |
| `query_range` | PromQL over a window ending now: per-series stats plus a downsampled timeline |
| `list_targets` | Scrape targets with health, last error and scrape duration |
| `list_alerts` | Pending and firing alerts with labels and summaries |

Every tool is annotated `readOnlyHint: true`, and the server only calls Prometheus's read endpoints. There's also an `investigate` prompt that walks a model through checking a service.

### Example (real output from a local Prometheus)

```
> list_alerts
1 alert(s):
- [firing] TargetDown (instance=localhost:19999, job=missing-exporter, severity=warning) since 2026-11-14T18:20:45Z: missing-exporter target localhost:19999 is down

> query  topk(3, sum by (handler) (rate(prometheus_http_requests_total[1m])))
3 series (instant vector at 2026-11-14 18:21:34 UTC):
{handler="/metrics"} = 0.183983
{handler="/-/ready"} = 0.0167258
{handler="/api/v1/label/:name/values"} = 0.0167258

> describe_metric  prometheus_http_requests_total
prometheus_http_requests_total: type=counter, unit=none
help: Counter of HTTP requests.
64 series in the last hour. Labels:
  code: 200
  handler: /, /-/healthy, /-/quit, /-/ready, /-/reload, /alertmanager-discovery, /alerts, /api/v1/*path (+56 more)
  ...
Tip: this is a counter; use rate(prometheus_http_requests_total[5m]) rather than the raw value.
```

## Use it

Requires Python 3.11+. With [uv](https://docs.astral.sh/uv/) there's nothing to install first.

**Claude Code**

```bash
claude mcp add prometheus -e PROMETHEUS_URL=http://localhost:9090 \
  -- uvx --from git+https://github.com/AmeesAnsari/mcp-prometheus mcp-prometheus
```

**Claude Desktop, Cursor and other clients** (`mcpServers` in the client's config file)

```json
{
  "mcpServers": {
    "prometheus": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/AmeesAnsari/mcp-prometheus", "mcp-prometheus"],
      "env": { "PROMETHEUS_URL": "http://localhost:9090" }
    }
  }
}
```

**As an HTTP service** (stateless, so it can sit behind a load balancer)

```bash
PROMETHEUS_URL=http://prometheus:9090 mcp-prometheus --transport streamable-http --host 0.0.0.0 --port 8000
# clients connect to http://<host>:8000/mcp
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `PROMETHEUS_URL` | `http://localhost:9090` | Prometheus base URL |
| `PROMETHEUS_TOKEN` | | Bearer token (e.g. Grafana Cloud, a reverse proxy) |
| `PROMETHEUS_USERNAME` / `PROMETHEUS_PASSWORD` | | Basic auth |
| `PROMETHEUS_TIMEOUT` | `15` | Query timeout in seconds, also passed to Prometheus so it cancels its own work |
| `MCP_PROM_MAX_SERIES` | `20` | Series returned per query |
| `MCP_PROM_MAX_POINTS` | `120` | Points per series in `query_range`; the step is raised to respect it |
| `PROMETHEUS_VERIFY_TLS` | `true` | Set `false` only for self-signed test setups |

## Safety

- **Read-only.** Only `/api/v1/query`, `query_range`, `series`, `metadata`, `label`, `targets` and `alerts` are called. No admin or write endpoints.
- **Bounded cost.** Query timeout, maximum query length, capped series and points. An expensive query fails fast with a message instead of tying up Prometheus.
- **Errors the model can fix.** PromQL errors, bad arguments and an unreachable server come back as tool errors with Prometheus's own message ("parse error: unclosed left parenthesis"), so the model can correct itself.
- **Use a scoped credential.** Give the server a token that can only read the metrics you're happy for the AI client to see.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
pytest -q                                              # unit tests, fake Prometheus
PROMETHEUS_URL=http://localhost:9090 pytest -q         # plus end-to-end, incl. stdio subprocess
```

- **Unit tests** drive the server through a real MCP client against a fake Prometheus, covering sorting, truncation, step capping, error mapping, auth headers and annotations.
- **Live tests** run against a real Prometheus. CI starts one as a service container, and they include launching the server as a stdio subprocess, the way a desktop client does.

## Related

- [ops-agent](https://github.com/AmeesAnsari/ops-agent): a read-only investigation agent; these tools are the real-world version of its `get_metrics`.
- [AI Agents Handbook, chapter 03 (MCP)](https://github.com/AmeesAnsari/ai-agents-handbook/blob/main/chapters/03-mcp.md).

## License

MIT
