#!/usr/bin/env python3
"""
Direct orchestration loop for ProductFactory.

Calls run_cycle() every 60s to check for work and launch agent containers.
Deterministic — no LLM reasoning. Claude is reserved for agent personas
(coder, designer, reviewer) where reasoning adds value.

On startup: runs orphan reconciliation — kills unknown pf-* containers and
closes DB session records whose containers are gone. This makes the system
self-healing across orchestrator restarts.
"""

import json
import logging
import os
import subprocess
import sys
import time

# The orchestrator package lives at /app/orchestrator inside the container.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

import orchestrator_runtime as tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s orchestrate: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("orchestrate")

CYCLE_INTERVAL = int(os.environ.get("ORCHESTRATION_CYCLE_SECONDS", "60"))
PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")


def _drain_orphan_session_results():
    """
    Walk every product's working_dir and apply any unconsumed session_result.json
    entries to the PM API BEFORE the next cycle's _reset_workspace deletes them.

    Background: when the orchestrator is restarted (deploy, OOM, force-recreate)
    while an agent session is in flight, the in-process live-poll thread dies
    with the orchestrator. The agent container survives, finishes its work, and
    writes Reviewing/Blocked entries to session_result.json — but no-one is left
    to PATCH them into the PM API. Next launch's _reset_workspace +
    _delete_session_result then unlinks the file unread.

    This drainer reads the file, applies each entry using the existing
    _apply_session_entry (which has rank-guarding + persona filtering), then
    rotates the file to session_result.drained-<ts>.json so it isn't re-applied.
    Best-effort: any product that fails to drain is logged and skipped.
    """
    import datetime as _dt
    import json as _json
    import httpx
    from pathlib import Path
    from orchestrator.paths import container_path
    from orchestrator.docker_runner import _apply_session_entry

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/products")
            products = resp.json() if resp.is_success else []
    except Exception:
        log.exception("[reconcile] could not fetch products for session_result drain")
        return

    drained_total = 0
    for p in products:
        wd_host = p.get("working_dir") or ""
        if not wd_host:
            continue
        try:
            wd = Path(container_path(wd_host))
        except Exception:
            continue
        sr = wd / "session_result.json"
        if not sr.exists():
            continue
        try:
            lines = sr.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            log.warning("[reconcile] read failed for %s: %s", sr, e)
            continue

        applied = 0
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for raw in lines:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = _json.loads(raw)
                    except Exception:
                        continue
                    if not isinstance(entry, dict) or "id" not in entry:
                        continue
                    try:
                        if _apply_session_entry(client, entry):
                            applied += 1
                    except Exception:
                        log.exception("[reconcile] apply failed for entry %s", entry)
        except Exception:
            log.exception("[reconcile] PM client error draining %s", sr)
            continue

        # Rotate so the next cycle's _delete_session_result doesn't unlink
        # entries we just applied (and so we keep a forensic copy on disk).
        ts = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup = sr.with_name(f"session_result.drained-{ts}.json")
        try:
            sr.rename(backup)
        except Exception:
            log.exception("[reconcile] could not rotate %s", sr)

        log.info("[reconcile] drained %d/%d session_result entries from %s -> %s",
                 applied, len(lines), wd_host, backup.name)
        drained_total += applied

    if drained_total:
        log.info("[reconcile] startup drain: %d total entries applied", drained_total)


def startup_reconcile():
    """
    Called once when the orchestrator starts. Reconciles reality:
      - Drain unconsumed session_result.json (work the agent did but no live-poll
        thread was around to apply — see _drain_orphan_session_results).
      - Any pf-<product>-<uid> container running with NO matching DB session
        row (or session is already ended) → reap the container.
      - Any DB session with status in {pending,starting,running} whose
        container is NOT in docker ps → mark orphaned, close.

    This handles the case where the orchestrator was restarted while agent
    containers were running — the subprocess.Popen watcher died, so those
    sessions would otherwise linger forever.
    """
    import httpx

    # 0. Drain orphaned session_result.json BEFORE workspace resets eat them.
    try:
        _drain_orphan_session_results()
    except Exception:
        log.exception("[reconcile] session_result drain failed (non-fatal)")
    # 1. Running containers
    try:
        res = subprocess.run(
            ["docker", "ps", "--filter", "name=pf-", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=10,
        )
        live = {n for n in res.stdout.strip().splitlines()
                if n and n.startswith("pf-") and n != "pf-orchestrator"}
    except Exception:
        log.exception("[reconcile] docker ps failed")
        live = set()

    # 2. DB sessions that claim to be active
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/sessions/active")
        db_sessions = resp.json() if resp.is_success else []
    except Exception:
        log.exception("[reconcile] GET /api/sessions/active failed")
        db_sessions = []

    db_container_names = {s.get("container_id") for s in db_sessions if s.get("container_id")}

    # 3. Containers live but not in DB as active → orphaned from a crashed launch.
    #    Conservative action: leave them running (they may be valid sessions the
    #    DB just hasn't caught up with). The per-container watchdog will time
    #    them out on expected_deadline anyway.
    stranded_containers = live - db_container_names
    if stranded_containers:
        log.warning("[reconcile] %d container(s) with no active DB session — leaving for watchdog: %s",
                    len(stranded_containers), list(stranded_containers)[:5])

    # 4. DB sessions claiming active but container is gone → close as orphaned.
    for s in db_sessions:
        if s.get("container_id") and s["container_id"] not in live:
            log.warning("[reconcile] closing orphaned session %s (container %s not running)",
                        s["id"], s["container_id"])
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    client.post(f"/api/sessions/{s['id']}/kill",
                                json={"reason": "orphaned - container gone on orchestrator restart"})
            except Exception:
                log.exception("[reconcile] POST /kill failed for session %s", s["id"])

    log.info("[reconcile] startup: live containers=%d, db_active=%d, orphans_closed=%d",
             len(live), len(db_sessions),
             sum(1 for s in db_sessions if s.get("container_id") and s["container_id"] not in live))


def run_orchestration_loop():
    log.info("ProductFactory orchestrator starting (cycle: %ds)", CYCLE_INTERVAL)
    try:
        startup_reconcile()
    except Exception:
        log.exception("startup reconcile failed — continuing")

    while True:
        try:
            log.info("=== cycle start ===")
            result_json = tools.run_cycle({})
            result = json.loads(result_json)

            if result.get("ok"):
                data = result.get("data", {})
                action = data.get("action")
                reason = data.get("reason", "")
                log.info("action=%s reason=%s", action, reason)

                if action == "409_stop":
                    log.warning("Another orchestrator holds the lock. Exiting.")
                    sys.exit(0)
                elif action == "launched":
                    product_id = data.get("product_id")
                    persona = data.get("persona")
                    log.info("launched session: product=%s persona=%s", product_id, persona)
                # action == "skipped_active": run_cycle decided a persona
                # but launch_session refused because there's already an
                # active session for that product. The "action=... reason=..."
                # log line above already captured the decision; no extra
                # "launched session" line — that was the gaslight bug.
                # action == "launch_failed": run_cycle couldn't reach
                # launch_session or it crashed; the run_cycle level would
                # have logged the exception already.
            else:
                log.error("run_cycle failed: %s", result.get("error", "unknown"))

        except Exception:
            log.exception("unhandled error in orchestration cycle")

        time.sleep(CYCLE_INTERVAL)


if __name__ == "__main__":
    run_orchestration_loop()
