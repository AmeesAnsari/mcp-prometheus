"""A small async client for the read-only parts of the Prometheus HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx2 as httpx


class PrometheusError(Exception):
    """An error Prometheus (or the network) returned, worded for the model to act on."""


@dataclass(frozen=True)
class Settings:
    url: str = "http://localhost:9090"
    token: Optional[str] = None  # bearer token
    username: Optional[str] = None  # basic auth
    password: Optional[str] = None
    timeout_seconds: float = 15.0
    max_series: int = 20
    max_points: int = 120  # per series, for query_range
    max_query_chars: int = 2_000
    verify_tls: bool = True


class PrometheusClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        headers = {"Accept": "application/json"}
        if settings.token:
            headers["Authorization"] = f"Bearer {settings.token}"
        auth = (settings.username, settings.password or "") if settings.username else None
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=settings.url.rstrip("/"),
            headers=headers,
            auth=auth,
            # Give Prometheus a chance to cancel its own query first (see `timeout` param below).
            timeout=settings.timeout_seconds + 5,
            verify=settings.verify_tls,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        try:
            response = await self.http.get(f"/api/v1/{path}", params=params)
        except httpx.TimeoutException as exc:
            raise PrometheusError(f"Prometheus did not answer within {self.settings.timeout_seconds:g}s") from exc
        except httpx.HTTPError as exc:
            raise PrometheusError(f"Could not reach Prometheus at {self.settings.url}: {exc}") from exc

        try:
            body = response.json()
        except ValueError:
            raise PrometheusError(f"Prometheus returned HTTP {response.status_code} with a non-JSON body")
        if body.get("status") != "success":
            kind = body.get("errorType", f"http_{response.status_code}")
            raise PrometheusError(f"Prometheus rejected the request ({kind}): {body.get('error', 'no detail')}")
        return body.get("data"), body.get("warnings") or []

    def _query_params(self, promql: str) -> dict[str, Any]:
        promql = promql.strip()
        if not promql:
            raise PrometheusError("The PromQL expression is empty.")
        if len(promql) > self.settings.max_query_chars:
            raise PrometheusError(f"The PromQL expression is longer than {self.settings.max_query_chars} characters.")
        return {"query": promql, "timeout": f"{self.settings.timeout_seconds:g}s"}

    async def query(self, promql: str, time: Optional[str] = None):
        params = self._query_params(promql)
        if time:
            params["time"] = time
        return await self._get("query", params)

    async def query_range(self, promql: str, start: float, end: float, step: float):
        params = self._query_params(promql) | {"start": start, "end": end, "step": f"{step:g}s"}
        return await self._get("query_range", params)

    async def metric_names(self):
        return await self._get("label/__name__/values")

    async def metadata(self, metric: str):
        return await self._get("metadata", {"metric": metric})

    async def series(self, match: str, start: float, end: float):
        return await self._get("series", {"match[]": match, "start": start, "end": end})

    async def targets(self):
        return await self._get("targets", {"state": "active"})

    async def alerts(self):
        return await self._get("alerts")

    async def buildinfo(self):
        return await self._get("status/buildinfo")
