"""Turn Prometheus API responses into compact text an LLM can read cheaply.

Raw Prometheus JSON for a range query can be megabytes. These helpers cap the
number of series and points, and lead with per-series statistics so the model
gets the shape of the data without every sample.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def fmt_value(raw: str) -> str:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return str(raw)
    if math.isnan(v) or math.isinf(v):
        return raw
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return f"{v:.6g}"


def fmt_labels(metric: dict[str, str]) -> str:
    name = metric.get("__name__", "")
    rest = ", ".join(f'{k}="{v}"' for k, v in sorted(metric.items()) if k != "__name__")
    return f"{name}{{{rest}}}" if rest or not name else name


def _warnings(warnings: Iterable[str]) -> list[str]:
    return [f"Prometheus warning: {w}" for w in warnings]


def instant(data: dict[str, Any], warnings: list[str], max_series: int) -> str:
    kind, result = data["resultType"], data["result"]
    lines = _warnings(warnings)
    if kind == "scalar":
        return "\n".join(lines + [f"scalar {fmt_value(result[1])} at {fmt_time(result[0])} UTC"])
    if kind == "string":
        return "\n".join(lines + [f'string "{result[1]}"'])
    if not result:
        return "\n".join(lines + ["No series matched. Check the metric name and label values (try list_metrics)."])

    # Sort by value, largest first, so "top N" questions work even when truncated.
    def sort_key(series: dict) -> float:
        try:
            v = float(series["value"][1])
            return -v if not math.isnan(v) else math.inf
        except (TypeError, ValueError, KeyError):
            return math.inf

    shown = sorted(result, key=sort_key)[:max_series]
    at = fmt_time(shown[0]["value"][0]) if "value" in shown[0] else ""
    head = f"{len(result)} series" + (f", showing top {len(shown)} by value" if len(result) > len(shown) else "")
    lines.append(f"{head} (instant vector at {at} UTC):")
    lines += [f"{fmt_labels(s['metric'])} = {fmt_value(s['value'][1])}" for s in shown]
    return "\n".join(lines)


def _stats(values: list[float]) -> str:
    finite = [v for v in values if not math.isnan(v)]
    if not finite:
        return "all NaN"
    return (f"min={fmt_value(str(min(finite)))} max={fmt_value(str(max(finite)))} "
            f"avg={fmt_value(str(sum(finite) / len(finite)))} last={fmt_value(str(finite[-1]))}")


def matrix(data: dict[str, Any], warnings: list[str], max_series: int, max_points: int, step: float) -> str:
    result = data["result"]
    lines = _warnings(warnings)
    if not result:
        return "\n".join(lines + ["No series matched in this window. Check the metric name, labels and time range."])

    shown = result[:max_series]
    lines.append(f"{len(result)} series, step {step:g}s"
                 + (f", showing first {len(shown)}" if len(result) > len(shown) else "") + ":")
    for series in shown:
        samples = [(float(t), float(v)) for t, v in series["values"]]
        stride = max(math.ceil(len(samples) / max_points), 1)
        sampled = samples[::stride]
        if samples and sampled[-1] != samples[-1]:
            sampled.append(samples[-1])  # always include the latest point
        lines.append(f"\n{fmt_labels(series['metric'])}")
        lines.append(f"  {len(samples)} points, {fmt_time(samples[0][0])} to {fmt_time(samples[-1][0])} UTC; "
                     + _stats([v for _, v in samples]))
        clock = slice(11, 19) if step < 60 else slice(11, 16)  # show seconds when steps are sub-minute
        lines.append("  " + " ".join(f"{fmt_time(t)[clock]}={fmt_value(str(v))}" for t, v in sampled))
    return "\n".join(lines)
