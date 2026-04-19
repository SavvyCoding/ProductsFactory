"""
Minimal Prometheus exporter for the orchestrator.

Exposes counters / histograms on a background HTTP server (default port 9100).
Design goals:
  - Zero hard dependency: if `prometheus_client` isn't installed we fall back
    to a tiny in-proc stub that accepts inc/observe calls but exposes nothing.
  - No global config surgery: the poller just imports and calls inc().
  - Optional uptime heartbeat: POST to HEARTBEAT_URL every HEARTBEAT_INTERVAL
    seconds so a dead-man's-switch service (healthchecks.io, Grafana Alerting)
    can page when the poller stops.

Enable Prometheus by setting:
    PROMETHEUS_PORT=9100   (scrape http://host:9100/metrics)

Enable heartbeat by setting:
    HEARTBEAT_URL=https://hc-ping.com/<uuid>
    HEARTBEAT_INTERVAL=60  (seconds)
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("poller.metrics")


# ── Backend: real prometheus_client if installed, otherwise a stub ────────────

class _StubMetric:
    def labels(self, **_kw): return self
    def inc(self, _n: float = 1.0) -> None: pass
    def observe(self, _v: float) -> None: pass
    def set(self, _v: float) -> None: pass


try:
    from prometheus_client import (
        Counter, Histogram, Gauge, start_http_server,
    )
    _HAVE_PROM = True
except Exception:  # pragma: no cover — package not installed
    _HAVE_PROM = False


def _mk_counter(name: str, doc: str, labels: list[str] | None = None):
    if not _HAVE_PROM:
        return _StubMetric()
    return Counter(name, doc, labels or [])


def _mk_histogram(name: str, doc: str, labels: list[str] | None = None):
    if not _HAVE_PROM:
        return _StubMetric()
    return Histogram(name, doc, labels or [])


def _mk_gauge(name: str, doc: str, labels: list[str] | None = None):
    if not _HAVE_PROM:
        return _StubMetric()
    return Gauge(name, doc, labels or [])


# ── Metric definitions ───────────────────────────────────────────────────────
# Named with a pf_ prefix so they're easy to filter in a shared Prometheus.

sessions_total = _mk_counter(
    "pf_sessions_total",
    "Agent sessions launched, by persona / product / outcome",
    ["persona", "product", "outcome"],  # outcome: success|failure|timeout
)

features_pushed_total = _mk_counter(
    "pf_features_pushed_total",
    "Features transitioned to Pushed, by persona / product",
    ["persona", "product"],
)

loop_detections_total = _mk_counter(
    "pf_loop_detections_total",
    "Times the loop detector fired for a product",
    ["product"],
)

session_duration_seconds = _mk_histogram(
    "pf_session_duration_seconds",
    "Wall-clock duration of agent sessions",
    ["persona"],
)

poller_up = _mk_gauge(
    "pf_poller_up",
    "1 while the poller process is alive (scraped via /metrics)",
    [],
)


# ── Public API ───────────────────────────────────────────────────────────────

def record_session(persona: str, product: str, outcome: str, duration_seconds: float) -> None:
    """Call once per session ended, regardless of outcome."""
    sessions_total.labels(persona=persona or "unknown",
                          product=product or "unknown",
                          outcome=outcome).inc()
    session_duration_seconds.labels(persona=persona or "unknown").observe(duration_seconds)


def record_feature_pushed(persona: str, product: str) -> None:
    features_pushed_total.labels(persona=persona or "unknown",
                                 product=product or "unknown").inc()


def record_loop_detection(product: str) -> None:
    loop_detections_total.labels(product=product or "unknown").inc()


# ── Startup ──────────────────────────────────────────────────────────────────

def start_prometheus_exporter() -> None:
    """If PROMETHEUS_PORT is set, spin up the /metrics HTTP server."""
    port = os.environ.get("PROMETHEUS_PORT")
    if not port:
        return
    if not _HAVE_PROM:
        log.warning(
            "PROMETHEUS_PORT=%s set but prometheus_client not installed — "
            "metrics endpoint not started. `pip install prometheus_client` to enable.",
            port,
        )
        return
    try:
        start_http_server(int(port))
        poller_up.set(1)
        log.info("Prometheus /metrics endpoint listening on :%s", port)
    except Exception as e:
        log.warning("Could not start prometheus exporter on :%s: %s", port, e)


def start_heartbeat() -> None:
    """If HEARTBEAT_URL is set, POST to it every HEARTBEAT_INTERVAL seconds.

    Intended for dead-man's-switch services: healthchecks.io, Grafana Alerting,
    BetterUptime, PagerDuty generic webhooks — anything that pages when a
    heartbeat stops arriving.
    """
    url = os.environ.get("HEARTBEAT_URL")
    if not url:
        return
    try:
        interval = int(os.environ.get("HEARTBEAT_INTERVAL", "60"))
    except ValueError:
        interval = 60

    def _loop() -> None:
        import httpx  # local import to avoid circular issues at module load
        while True:
            try:
                httpx.get(url, timeout=10)
            except Exception as e:
                log.debug("heartbeat ping failed: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="pf-heartbeat", daemon=True)
    t.start()
    log.info("Heartbeat started — pinging %s every %ds", url, interval)
