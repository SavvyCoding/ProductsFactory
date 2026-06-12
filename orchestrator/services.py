"""Per-session sidecar services — Phase A of service provisioning (2026-06-12).

Agent/test/verify containers are sandboxed by design: no docker socket, no
--privileged, non-root, RO PM-curated files. When a product's tests need a
LIVE service (redis, postgres, ...), the ORCHESTRATOR provisions it as a
sibling container on productfactory-net and injects connection env vars
into every container belonging to that session. Agents never start
services; products DECLARE them in ``product.config.services`` (written by
the infra-story executor or the PM via the API — never by agents; the
canonical incident is DogTinder #1582, where a coder vendored the entire
Redis source tree because its sandbox offered no other way to satisfy a
live-Redis AC).

Security boundary: ``SERVICE_CATALOG`` is the allowlist. Only catalog
entries are ever provisioned — agent-authored text can NAME a service but
can never supply an image reference, so the worst a hostile/confused agent
can request is a blessed, pinned, resource-capped container.

Lifecycle: one container per (session, service), named
``pf-svc-{session_uid}-{service}``. Provisioned at session launch, kept
through the post-coder gates (test-check/verify-check containers connect
to the same names), torn down after session finalization. The per-cycle
reaper removes leftovers whose session is no longer active (the cycle
watchdog ignores ``pf-svc-*`` — it only targets containers tied to
session rows).
"""
from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("services")

# Allowlist of provisionable services. Image references are pinned here and
# ONLY here — see module docstring. `url_template` is formatted with
# host=<container name> and the catalog port; `ready` is exec'd INSIDE the
# service container until it succeeds (or the deadline passes).
SERVICE_CATALOG: dict[str, dict] = {
    "redis": {
        "image":        "redis:7-alpine",
        "port":         6379,
        "env_var":      "REDIS_URL",
        "url_template": "redis://{host}:{port}/0",
        "run_env":      [],
        "ready":        ["redis-cli", "ping"],
    },
    "postgres": {
        "image":        "postgres:16-alpine",
        "port":         5432,
        "env_var":      "DATABASE_URL",
        "url_template": "postgresql://pf:pf@{host}:{port}/pf",
        "run_env":      ["-e", "POSTGRES_USER=pf", "-e", "POSTGRES_PASSWORD=pf",
                         "-e", "POSTGRES_DB=pf"],
        "ready":        ["pg_isready", "-U", "pf"],
    },
}

_READY_DEADLINE_S = 20
_NAME_PREFIX = "pf-svc-"


def _svc_name(session_uid: str, service: str) -> str:
    return f"{_NAME_PREFIX}{session_uid}-{service}"


def declared_services(product: dict) -> list[str]:
    """Catalog-valid service names declared in product.config.services."""
    cfg = product.get("config") or {}
    raw = cfg.get("services") or []
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str) and s in SERVICE_CATALOG]


def session_service_env(product: dict, session_uid: str) -> dict[str, str]:
    """Pure mapping of env vars for a session's declared services (no docker
    calls — assumes ensure_session_services ran). Shared by the agent
    container launch and the post-coder gate containers so both see the
    same connection URLs."""
    env: dict[str, str] = {}
    for svc in declared_services(product):
        entry = SERVICE_CATALOG[svc]
        env[entry["env_var"]] = entry["url_template"].format(
            host=_svc_name(session_uid, svc), port=entry["port"])
    return env


def ensure_session_services(product: dict, session_uid: str) -> dict[str, str]:
    """Start every declared service for this session and wait until ready.

    Returns the env-var mapping (same as session_service_env). Best-effort
    per service: a service that fails to start/ready is logged and skipped —
    the session proceeds, its tests fail with connection-refused, and the
    service_missing triage surfaces the infra failure (never a feature
    bounce with a fix_attempts bump).
    """
    env: dict[str, str] = {}
    for svc in declared_services(product):
        entry = SERVICE_CATALOG[svc]
        name = _svc_name(session_uid, svc)
        try:
            run = subprocess.run(
                ["docker", "run", "-d", "--rm",
                 "--name", name,
                 "--network", "productfactory-net",
                 "--memory", "512m", "--cpus", "0.5", "--pids-limit", "128",
                 *entry["run_env"],
                 entry["image"]],
                capture_output=True, text=True, timeout=120,
            )
            if run.returncode != 0:
                log.warning("[services] %s: start failed for %s: %s",
                            product.get("name", "?"), name,
                            (run.stderr or "")[:300])
                continue
            if _wait_ready(name, entry["ready"]):
                env[entry["env_var"]] = entry["url_template"].format(
                    host=name, port=entry["port"])
                log.info("[services] %s: %s ready (%s)",
                         product.get("name", "?"), name, entry["image"])
            else:
                log.warning("[services] %s: %s did not become ready in %ss — removing",
                            product.get("name", "?"), name, _READY_DEADLINE_S)
                subprocess.run(["docker", "rm", "-f", name],
                               capture_output=True, timeout=30)
        except Exception:
            log.exception("[services] ensure failed for %s", name)
    return env


def _wait_ready(name: str, ready_cmd: list[str]) -> bool:
    deadline = time.monotonic() + _READY_DEADLINE_S
    while time.monotonic() < deadline:
        try:
            r = subprocess.run(["docker", "exec", name, *ready_cmd],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def teardown_session_services(session_uid: str) -> None:
    """Remove every service container belonging to this session. Idempotent."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "-aq", "--filter",
             f"name={_NAME_PREFIX}{session_uid}-"],
            capture_output=True, text=True, timeout=15,
        )
        ids = [c for c in (ps.stdout or "").split() if c]
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids],
                           capture_output=True, timeout=60)
            log.info("[services] tore down %d service container(s) for session %s",
                     len(ids), session_uid)
    except Exception:
        log.exception("[services] teardown failed for session %s", session_uid)


def reap_orphan_services(active_session_uids: set[str]) -> int:
    """Remove pf-svc-* containers whose session is no longer active
    (running OR wrapping — wrapping sessions still run the post-coder gate
    containers against their services). Called once per cycle from
    run_cycle; the session-row watchdog deliberately ignores pf-svc-*.
    Returns the count reaped."""
    reaped = 0
    try:
        ps = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name={_NAME_PREFIX}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=15,
        )
        for name in (ps.stdout or "").split():
            if not name.startswith(_NAME_PREFIX):
                continue
            # pf-svc-{uid}-{service}: uid is everything between the prefix
            # and the LAST hyphen (uids themselves contain no hyphens).
            rest = name[len(_NAME_PREFIX):]
            uid = rest.rsplit("-", 1)[0]
            if uid in active_session_uids:
                continue
            subprocess.run(["docker", "rm", "-f", name],
                           capture_output=True, timeout=30)
            reaped += 1
            log.info("[services] reaped orphan service container %s", name)
    except Exception:
        log.exception("[services] orphan reap failed")
    return reaped
