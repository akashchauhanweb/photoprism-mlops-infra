"""
Shared Prometheus instrumentation for photoprism-mlops services.
Copied (not imported as a package) into each service image at build time.

Exposes:
  - install_fastapi(app, service_name): adds /metrics + RED middleware
  - install_worker(service_name): for non-FastAPI workers; starts an HTTP exporter on $METRICS_PORT
  - REQUEST_COUNT, REQUEST_LATENCY, INFLIGHT: standard RED
  - ml_counter(name, doc, labels=()), ml_histogram(name, doc, labels=(), buckets=None): factory helpers
    so each service can declare its own ML-specific signals without polluting a global registry.

Conventions:
  - Metric names are prefixed `pp_` to keep them grep-able
  - HTTP labels: service, method, route_template, status_class (2xx/4xx/5xx)
  - Latency buckets tuned for sub-second APIs and a few seconds for clip/reranker
"""
from __future__ import annotations

import os
import time
from typing import Iterable, Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    start_http_server,
)

DEFAULT_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
)

# ---- Standard RED metrics (HTTP services) ----------------------------------
REQUEST_COUNT = Counter(
    "pp_http_requests_total",
    "HTTP requests, labelled by service/method/route/status_class",
    ["service", "method", "route", "status_class"],
)
REQUEST_LATENCY = Histogram(
    "pp_http_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route"],
    buckets=DEFAULT_BUCKETS,
)
INFLIGHT = Gauge(
    "pp_http_inflight_requests",
    "In-flight HTTP requests",
    ["service"],
)


def _route_template(request) -> str:
    """Use the matched route template (e.g. /search) rather than the raw path,
    to keep label cardinality bounded. Falls back to the URL path for unmatched."""
    try:
        route = request.scope.get("route")
        if route is not None and getattr(route, "path", None):
            return route.path
    except Exception:
        pass
    return request.url.path


def install_fastapi(app, service_name: str) -> None:
    """Wire RED metrics + /metrics endpoint into a FastAPI app."""
    from fastapi import Request
    from fastapi.responses import Response

    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        # /metrics, /health, /ready do not contribute to RED
        path = request.url.path
        if path in ("/metrics", "/health", "/ready"):
            return await call_next(request)
        method = request.method
        INFLIGHT.labels(service=service_name).inc()
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - start
            INFLIGHT.labels(service=service_name).dec()
            route = _route_template(request)
            status_class = f"{status_code // 100}xx"
            REQUEST_COUNT.labels(
                service=service_name, method=method, route=route, status_class=status_class
            ).inc()
            REQUEST_LATENCY.labels(
                service=service_name, method=method, route=route
            ).observe(elapsed)

    @app.get("/metrics", include_in_schema=False)
    def _metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def install_worker(service_name: str, port: Optional[int] = None) -> None:
    """For non-FastAPI processes. Starts a Prom exporter HTTP server in a daemon thread."""
    p = port if port is not None else int(os.environ.get("METRICS_PORT", "9100"))
    start_http_server(p)


# ---- Factory helpers for service-specific ML signals -----------------------
def ml_counter(name: str, doc: str, labels: Iterable[str] = ()) -> Counter:
    return Counter(name, doc, list(labels))


def ml_histogram(
    name: str,
    doc: str,
    labels: Iterable[str] = (),
    buckets: Optional[Iterable[float]] = None,
) -> Histogram:
    return Histogram(name, doc, list(labels), buckets=tuple(buckets) if buckets else DEFAULT_BUCKETS)


def ml_gauge(name: str, doc: str, labels: Iterable[str] = ()) -> Gauge:
    return Gauge(name, doc, list(labels))
