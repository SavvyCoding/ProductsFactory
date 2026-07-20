"""
Tool handlers for the ProductFactory Hermes plugin.

Hermes dispatches tools as: handler(args: dict, **kwargs) where args is the
JSON argument dict and kwargs may include task_id, session_id, etc.

Every handler returns a JSON string. Errors are caught and returned as
{"error": "message"} — handlers never raise.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
from typing import Any

import httpx

# The orchestrator package lives at /app/orchestrator inside the Hermes container.
# Insert /app so lazy imports (launch_session, run_cycle, etc.) resolve.
if "/app" not in sys.path:
    sys.path.insert(0, "/app")

log = logging.getLogger("orchestrator.tools")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")
PM_USERNAME = os.environ.get("PM_USERNAME", "admin")
PM_PASSWORD = os.environ.get("PM_PASSWORD", "")
REQUEST_TIMEOUT = 15


def _ok(data: Any) -> str:
    return json.dumps({"ok": True, "data": data})


def _err(msg: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": msg, **extra})


# Optional HMAC signing for internal write endpoints (Phase #9 opt-in).
# When PF_INTERNAL_API_SECRET is set on BOTH the orchestrator and the website,
# every outbound POST/PATCH gets an X-PF-Signature header keyed by the secret.
# The website's verify_internal_signature dependency rejects unsigned writes
# when the secret is set there. Unset on either side = transparent no-op.
_INTERNAL_API_SECRET = os.environ.get("PF_INTERNAL_API_SECRET", "")


def _sign_internal_body(body: bytes) -> str:
    """Compute `sha256=<hex>` HMAC-SHA256 of body keyed by the internal secret."""
    import hashlib, hmac as _hmac
    digest = _hmac.new(_INTERNAL_API_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _SigningClient(httpx.Client):
    """httpx.Client that auto-signs every POST/PATCH/PUT/DELETE body when
    PF_INTERNAL_API_SECRET is set. Read methods (GET, HEAD) are untouched.

    The signature is computed against the JSON-serialized body matching what
    the website's verify_internal_signature reads from request.body() — so we
    must pre-serialize once and pass `content=` rather than `json=` for write
    methods. Falls back to no-op when the secret is unset.
    """
    def request(self, method: str, url, **kwargs):
        if _INTERNAL_API_SECRET and method.upper() in ("POST", "PATCH", "PUT", "DELETE"):
            json_body = kwargs.pop("json", None)
            # httpx.Client.post() passes every body kwarg (content/data/files/json)
            # in the call to self.request(); content is usually None. We only
            # take over when there's a json body AND no explicit content.
            if json_body is not None and not kwargs.get("content"):
                payload = json.dumps(json_body).encode()
                kwargs["content"] = payload
                headers = dict(kwargs.get("headers") or {})
                headers["X-PF-Signature"] = _sign_internal_body(payload)
                headers.setdefault("Content-Type", "application/json")
                kwargs["headers"] = headers
            elif json_body is not None:
                # Caller passed both json and content — restore json so the
                # ambiguity surfaces as an httpx error, don't silently drop it.
                kwargs["json"] = json_body
        return super().request(method, url, **kwargs)


def _pm_client() -> httpx.Client:
    auth = (PM_USERNAME, PM_PASSWORD) if PM_PASSWORD else None
    # Default header on every request so GET /api/system-config returns real
    # secret values (not masked hints). Keyed by the same PF_INTERNAL_API_SECRET
    # used for write-signing; inert when unset (website reveals regardless).
    headers = {"X-PF-Internal-Token": _INTERNAL_API_SECRET}
    return _SigningClient(base_url=PM_API_URL, timeout=REQUEST_TIMEOUT, auth=auth, headers=headers)


def _get_github_token() -> str:
    """Return a fresh GitHub App installation token, or ``""``.

    Per CLAUDE.md "Git auth: GitHub App only": the PAT fallback was
    removed in the drift-cleanup pass. The App module caches the
    installation token and auto-refreshes when <5 min of life remains;
    on mint failure the empty string surfaces here so callers fail
    visibly rather than silently degrade.
    """
    try:
        from orchestrator.integrations.github_app import get_installation_token
        return get_installation_token() or ""
    except Exception as e:
        log.warning("App token mint failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Generic PM API passthrough
# ---------------------------------------------------------------------------

import re as _re
_FEATURE_ID_RE  = _re.compile(r"^/api/features/\d+$")
_SPRINT_ID_RE   = _re.compile(r"^/api/sprints/\d+$")


def pm_api(args: dict, **kwargs) -> str:
    method = args.get("method", "GET")
    path = args.get("path", "")
    body = args.get("body")

    # Guard: if the model tries to GET individual features/sprints, return a
    # helpful refusal to avoid blowing up the context window.
    if method.upper() == "GET":
        if _FEATURE_ID_RE.match(path):
            return json.dumps({"ok": False, "error":
                "Use get_features(product_id) instead of fetching individual features."})

    try:
        with _pm_client() as client:
            resp = client.request(method.upper(), path, json=body)
        data: Any
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return json.dumps({"ok": resp.is_success, "status": resp.status_code, "data": data})
    except httpx.HTTPError as e:
        return _err(f"pm_api {method} {path} failed: {e}")


def _pm(method: str, path: str, body: dict | None = None) -> str:
    """Internal helper for simple PM calls."""
    return pm_api({"method": method, "path": path, "body": body})


def _bump_product_last_run(product_id: int) -> None:
    """Mark this product as visited by the orchestrator (advances round-robin).

    Best-effort PATCH — never raises so a transient PM API hiccup doesn't
    crash the cycle. Round-robin is `last_run_at ASC NULLS FIRST`, so
    bumping to NOW pushes this product to the back of the queue and the
    next cycle picks the next-oldest product.
    """
    try:
        from datetime import datetime as _dt, timezone as _tz
        _pm("PATCH", f"/api/products/{product_id}",
            {"last_run_at": _dt.now(_tz.utc).isoformat()})
    except Exception:
        log.debug(f"[round-robin] could not bump last_run_at for product {product_id}")


_PRODUCT_KEEP = {"id", "name", "status", "working_dir", "github_repo", "tech_stack",
                 "run_now", "run_trainer_now", "run_persona_now",
                 "quiet_hours_start", "quiet_hours_end",
                 "daily_session_cap", "last_run_at", "config", "type"}

_ONDEMAND_PERSONAS = ("documenter", "analytics", "refactorer", "devops", "recommender",
                      # Phase 8 of quality-specs (2026-05-19): architect persona
                      # for quantitative drift detection. Maintenance-shape
                      # (read-only on source); PM triggers on demand via
                      # run_persona_now="architect", or _check_architect_due
                      # fires it automatically every ~3 features pushed (default N).
                      "architect",
                      # Wave-6 (2026-06-13): read-only product-wide security
                      # audit that files findings as bug features. PM-triggered
                      # only — opt-in by design, never auto-scheduled.
                      "security_auditor",
                      # 2026-06-24: read-only whole-product code review fired at
                      # phase boundaries by _run_phase_review_gate (or PM click).
                      # Comment-only soak: raises dashboard alerts, files no bugs.
                      "code_auditor")
_FEATURE_KEEP = {"id", "product_id", "phase_id", "parent_id", "name", "status",
                 "feature_type", "design_doc_path", "pr_number", "pr_url",
                 "fix_attempts", "merge_notes"}


def _slim(obj: Any, keep: set) -> Any:
    if isinstance(obj, list):
        return [_slim(x, keep) for x in obj]
    if isinstance(obj, dict):
        return {k: v for k, v in obj.items() if k in keep}
    return obj


def _slim_response(raw: str, keep: set) -> str:
    try:
        parsed = json.loads(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else parsed
        slimmed = _slim(data, keep)
        if isinstance(parsed, dict):
            parsed["data"] = slimmed
            return json.dumps(parsed)
        return json.dumps(slimmed)
    except Exception:
        return raw


def get_products(args: dict, **kwargs) -> str:
    # Phases→features flat model (migration 043): no active-sprint augmentation;
    # the cycle uses feature.status + feature.phase_id directly.
    raw = _slim_response(_pm("GET", "/api/products"), _PRODUCT_KEEP)
    try:
        parsed = json.loads(raw)
        products = parsed.get("data") if isinstance(parsed, dict) else parsed
        if isinstance(products, list):
            if isinstance(parsed, dict):
                parsed["data"] = products
                return json.dumps(parsed)
            return json.dumps(products)
    except Exception:
        pass
    return raw


def get_features(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    raw = _slim_response(_pm("GET", f"/api/products/{product_id}/features"), _FEATURE_KEEP)
    # Only return non-terminal features — terminal ones (Pushed/Deferred/Rejected/Reverted)
    # are irrelevant to orchestration decisions and bloat the context window.
    try:
        parsed = json.loads(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else parsed
        if isinstance(data, list):
            _TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted", "Stuck"}
            data = [f for f in data if f.get("status") not in _TERMINAL]
            if isinstance(parsed, dict):
                parsed["data"] = data
                return json.dumps(parsed)
            return json.dumps(data)
    except Exception:
        pass
    return raw


_SYSCFG_KEEP = {"auto_merge_enabled", "stuck_feature_timeout_hours",
                "poll_interval", "session_timeout_minutes", "stale_threshold_minutes"}


def _discover_registered_products(products: list, **kwargs) -> int:
    """
    For every product in `registered` status, run discovery (reads working
    dir, installs CLAUDE.md / AGENT_WORKFLOW.md / ARCHITECTURE.md templates,
    detects tech stack, sets status). For greenfield products discover_and_
    populate sets status='discovered' which requires a manual PM gate; we
    auto-promote that to 'ready' here so wizard-submitted products run end
    to end without an extra click — the wizard submission IS the PM intent.

    Ported from the legacy host poller (poller.py:1225-1228).
    Returns the count of products discovered.
    """
    pending = [p for p in products if p.get("status") == "registered"]
    if not pending:
        return 0
    try:
        from orchestrator.setup_product import discover_and_populate
    except Exception:
        log.exception("[discover] could not import setup_product")
        return 0

    n = 0
    for product in pending:
        try:
            log.info("[discover] starting product=%s name=%s",
                     product.get("id"), product.get("name"))
            discover_and_populate(product)
            n += 1
            # discover_and_populate has already PATCH'd the product. Re-fetch
            # to see the resulting status — if greenfield ('discovered'),
            # bump to 'ready' so the orchestrator picks it up next cycle.
            with _pm_client() as client:
                resp = client.get(f"/api/products/{product['id']}")
                if resp.is_success and resp.json().get("status") == "discovered":
                    client.patch(f"/api/products/{product['id']}",
                                 json={"status": "ready"})
                    log.info("[discover] auto-promoted greenfield product=%s discovered → ready",
                             product.get("id"))
        except Exception:
            log.exception("[discover] failed for product=%s", product.get("id"))
    return n


def _scaffold_greenfield_pending(products: list, sys_cfg: dict | None = None, **kwargs) -> int:
    """
    For every product in `greenfield_pending`, invoke the host-side
    scaffold helper that creates the GitHub repo, generates the deploy
    key, writes initial files, and flips the product to `registered`.

    Ported from the legacy host poller (poller.py:1216) — was lost when
    the orchestrator moved into a container. Returns the number scaffolded.
    """
    pending = [p for p in products if p.get("status") == "greenfield_pending"]
    if not pending:
        return 0
    try:
        from pathlib import Path as _Path
        from orchestrator.greenfield_scaffold import scaffold_greenfield
    except Exception:
        log.exception("[scaffold] could not import greenfield_scaffold")
        return 0

    # Resolve SSH_DIR inside the orchestrator container.
    ssh_dir = _Path(os.environ.get("SSH_DIR", "/home/orchestrator/.ssh"))
    if not ssh_dir.exists():
        log.error("[scaffold] SSH_DIR=%s does not exist — cannot generate deploy keys", ssh_dir)
        return 0

    # system_config supplies github_org / GitHub App credentials.
    # Normally passed in by run_cycle's per-cycle fetch; self-fetch fallback.
    if sys_cfg is None:
        try:
            with _pm_client() as client:
                sys_cfg = client.get("/api/system-config").json()
        except Exception:
            log.exception("[scaffold] could not fetch system-config")
            return 0

    scaffolded = 0
    for product in pending:
        try:
            log.info("[scaffold] starting product=%s name=%s",
                     product.get("id"), product.get("name"))
            scaffold_greenfield(product, sys_cfg, PM_API_URL, ssh_dir)
            scaffolded += 1
        except Exception:
            log.exception("[scaffold] failed for product=%s", product.get("id"))
    return scaffolded


def run_cycle(args: dict, **kwargs) -> str:
    """
    Run a complete orchestration cycle in Python:
    1. Heartbeat (returns 409_stop if lock stolen)
    2. Kill stale containers
    3. Reset stuck features
    4. DoD checks + PR reconciliation for all ready products
    5. Find next product + determine action
    6. If action==launch_session: return {action, product_id, persona}
    7. If action==plan_sprints: call plan-sprints and return {action: "exit"}
    8. If nothing to do: return {action: "exit"}
    """
    try:
        # 1. Heartbeat
        hb = json.loads(poller_heartbeat({}, **kwargs))
        if not hb.get("ok") or hb.get("status") == 409:
            return _ok({"action": "409_stop", "reason": "Lock stolen"})

        # 1b. Re-normalise the bind-mounted ~/.ssh perms. Windows Docker
        # bind-mounts surface any host-side write as `root:root 777`, which
        # OpenSSH rejects ("Bad owner or permissions"). bootstrap.sh fixes
        # this at container startup but cannot react to subsequent host
        # writes. Running an alpine sidecar once per cycle catches any
        # manual ~/.ssh/config edits or scaffolding writes before the
        # next git fetch/push fires. Best-effort, non-fatal — see helper
        # docstring for the underlying incident.
        try:
            from orchestrator.integrations.docker_cli import _chmod_ssh_dir_via_alpine  # type: ignore
            _chmod_ssh_dir_via_alpine()
        except Exception:
            log.exception("ssh-perm helper failed (non-fatal)")

        # 2. WATCHDOG — DB-authoritative. Asks PM API for sessions past their
        # expected_deadline or without a recent heartbeat, then kills their
        # docker containers and closes the DB records. No mtime parsing, no
        # RunningFor string matching, no GitHub API calls. Single source of
        # truth: the `sessions` table.
        active_sessions: list = []
        try:
            with _pm_client() as client:
                wd_resp = client.get("/api/sessions/watchdog/targets")
                active_resp = client.get("/api/sessions/active")
            targets = wd_resp.json() if wd_resp.is_success else []
            active_sessions = active_resp.json() if active_resp.is_success else []

            # Phase II.X.3 fix (#3): also flag any running session whose
            # container_id is no longer present in `docker ps`. The watchdog
            # endpoint runs in pm-api which has no docker access, so the
            # docker-state probe lives here. Closes the orphan-session class
            # observed today (incident #1698) where the container died but
            # the session row stayed `running` until expected_deadline 90 min
            # away. The DB-only check would not have fired in that window.
            try:
                ps = subprocess.run(
                    ["docker", "ps", "--filter", "name=pf-", "--format", "{{.Names}}"],
                    capture_output=True, text=True, timeout=10,
                )
                live_containers = {n for n in ps.stdout.strip().splitlines()
                                   if n and n.startswith("pf-") and n != "pf-orchestrator"}
            except Exception:
                live_containers = None  # docker probe failed — be conservative, don't synthesize targets
                log.exception("[watchdog] docker ps probe failed")

            if live_containers is not None:
                already_targeted = {t["id"] for t in targets}
                for s in active_sessions:
                    if s.get("id") in already_targeted:
                        continue
                    # `wrapping` sessions exited their agent container by
                    # design — the orchestrator-side post-coder pipeline is
                    # what's still running. A missing container is the
                    # expected state, NOT an orphan. Skip the docker-ps
                    # orphan check; the dedicated 10-min stale-wrapping
                    # watchdog (separate endpoint) handles wrapping that
                    # genuinely hangs. Without this skip, the addition of
                    # `wrapping` to /api/sessions/active (cycle CX-2) would
                    # cause every wrapping session to be killed seconds
                    # after the agent finishes — defeats the purpose of the
                    # state machine.
                    if s.get("status") == "wrapping":
                        continue
                    cid = s.get("container_id")
                    if not cid:
                        continue
                    if cid not in live_containers:
                        targets.append({
                            "id":           s["id"],
                            "product_id":   s.get("product_id"),
                            "persona":      s.get("persona"),
                            "container_id": cid,
                            "session_uid":  s.get("session_uid"),
                            "reason":       "container exited (docker ps does not list it)",
                        })

            for t in targets:
                name = t.get("container_id") or f"pf-{t['product_id']}-{t.get('session_uid','')}"
                reason = t.get("reason", "watchdog")
                log.warning("[watchdog] killing session %s container=%s reason=%s",
                            t["id"], name, reason)
                subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
                with _pm_client() as client:
                    client.post(f"/api/sessions/{t['id']}/kill", json={"reason": reason})
        except Exception:
            log.exception("[watchdog] failed")

        # Sidecar-service reaper (orchestrator/services.py): remove pf-svc-*
        # containers whose session is no longer active (running OR wrapping —
        # wrapping sessions still run gate containers against their
        # services). The session watchdog above deliberately ignores
        # pf-svc-* (they have no session row).
        try:
            from orchestrator.services import reap_orphan_services
            _active_uids = {s.get("session_uid") for s in active_sessions
                            if s.get("session_uid")}
            reap_orphan_services(_active_uids)
        except Exception:
            log.exception("[services] orphan reaper failed (non-fatal)")

        reset_stuck_features({}, **kwargs)

        # 3. Active containers are tracked per-product via the DB and the
        # per-product launch_lock. We no longer short-circuit globally on
        # "any pf-* running" — that blocks other products unnecessarily.
        # The round-robin product selection below already picks products that
        # don't have active sessions; the launch_lock prevents double-spawn
        # for a given product. No global guard needed.

        # 4b. Single per-cycle system-config fetch. The helpers below
        # (greenfield scaffold, auto-merge sweep, supervisor detectors,
        # architect scheduler) each used to fetch /api/system-config
        # themselves — with N ready products that was ~2N+2 identical GETs
        # per 60s cycle. Operators can still hot-rotate config: one cycle
        # of latency, same as before. Empty dict on failure → each helper
        # falls back to its own fetch (or its env default), preserving the
        # old failure semantics.
        try:
            with _pm_client() as client:
                _sc_raw = client.get("/api/system-config").json()
            cycle_sys_cfg: dict = _sc_raw if isinstance(_sc_raw, dict) else {}
        except Exception:
            log.exception("per-cycle system-config fetch failed (helpers fall back)")
            cycle_sys_cfg = {}

        # 5. Per-product preflight
        products_raw = json.loads(get_products({}, **kwargs))
        products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
        products = products_data if isinstance(products_data, list) else []

        # 5a. Scaffold greenfield_pending products (creates GitHub repo, deploy
        # key, initial files, flips status → registered).
        try:
            n = _scaffold_greenfield_pending(products, sys_cfg=cycle_sys_cfg or None, **kwargs)
            if n:
                products_raw = json.loads(get_products({}, **kwargs))
                products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
                products = products_data if isinstance(products_data, list) else []
        except Exception:
            log.exception("[scaffold] greenfield_pending pass failed")

        # 5b. Discover registered products (reads working_dir, installs agent
        # templates, detects stack, flips status → ready). Auto-promotes
        # greenfield 'discovered' → 'ready' so wizard-submitted products run
        # without a separate PM approval click.
        try:
            n = _discover_registered_products(products, **kwargs)
            if n:
                products_raw = json.loads(get_products({}, **kwargs))
                products_data = products_raw.get("data") if isinstance(products_raw, dict) else products_raw
                products = products_data if isinstance(products_data, list) else []
        except Exception:
            log.exception("[discover] registered pass failed")

        ready = [p for p in products if p.get("status") in ("ready", "running")]

        # Phases→features flat model (migration 043): no sprint DoD to
        # check. DoD per-sprint is gone; per-feature shipping is the gate.

        for p in ready:
            try:
                # Pass the in-hand product row (same ProductOut schema as
                # GET /api/products/{id}) so reconcile_prs skips its
                # per-product re-fetch.
                reconcile_prs({"product_id": p["id"], "product": p}, **kwargs)
            except Exception:
                log.exception(f"reconcile_prs failed for product {p.get('id')} (non-fatal)")

        # Phase 1 of PollerRevamp (INVARIANTS.md VII.1): per-cycle auto-merge
        # sweep. Walks every ready product and squash-merges any feature that
        # is Reviewed+approved+pr_number, regardless of sprint membership.
        # In sprint-PR mode (VII.5) it holds the sprint PR until every feature
        # in the sprint is merge-eligible, preventing premature ship of
        # incomplete work. Best-effort — never raises into run_cycle.
        try:
            from orchestrator.auto_merge import sweep_all  # type: ignore
            _sc_for_merge = cycle_sys_cfg
            if not _sc_for_merge:
                with _pm_client() as client:
                    _sc_for_merge = client.get("/api/system-config").json()
            if isinstance(_sc_for_merge, dict):
                sweep_all(ready, _sc_for_merge)
        except Exception:
            log.exception("auto-merge sweep failed (non-fatal)")

        # Per-cycle per-product feature snapshot, fetched ONCE after the
        # reconcile pass (so just-merged PRs are reflected) and shared by the
        # supervisor / phase-gate / architect detectors below — each used to
        # re-fetch the same list itself (3 GETs/product/cycle → 1).
        features_by_pid: dict = {}
        for p in ready:
            try:
                with _pm_client() as client:
                    _fr = client.get(f"/api/products/{p['id']}/features")
                _fl = _fr.json() if _fr.is_success else None
                features_by_pid[p["id"]] = _fl if isinstance(_fl, list) else None
            except Exception:
                features_by_pid[p["id"]] = None  # helpers fall back to self-fetch

        # Phase-1 supervisor detectors that operate per-product on data the
        # PM API already serves cheaply: orphan-Approved features and rapid
        # status flaps. Both run every cycle (each has its own cooldown to
        # prevent action spam). Best-effort — never raises.
        for p in ready:
            try:
                _run_supervisor_per_product_detectors(
                    p, sys_cfg=cycle_sys_cfg or None,
                    features=features_by_pid.get(p["id"]))
            except Exception:
                log.exception(f"supervisor per-product detectors failed for product {p.get('id')}")

        # Infra-story executor (service provisioning, Phase B): Approved
        # feature_type='infra' stories are implemented HERE, deterministically
        # — never dispatched to coder/designer sessions (both selection
        # points also exclude them). Runs before the phase-gate detector so
        # a just-Pushed infra story settles into this cycle's gate math.
        # Blocked-feature re-processor (wave-8): give Blocked features whose
        # blocker class now has a remedy ONE automatic retry (drains the
        # accumulated pool). Gated OFF by default. Runs before the infra
        # executor + detectors so a just-unblocked feature settles into this
        # cycle's dispatch.
        # Premium-model escalation (docs/blocked_escalation_plan.md): runs BEFORE
        # the base reprocessor so a Blocked feature is offered the stronger LLM
        # first. OFF unless configured (cap 0/blank ⇒ disabled). Refreshes the
        # snapshot so the base reprocessor sees escalated features as no-longer-
        # Blocked and skips them.
        for p in ready:
            try:
                n_esc = _escalate_blocked_features(p, features=features_by_pid.get(p["id"]))
                if n_esc:
                    with _pm_client() as client:
                        _fr = client.get(f"/api/products/{p['id']}/features")
                    _fl = _fr.json() if _fr.is_success else None
                    if isinstance(_fl, list):
                        features_by_pid[p["id"]] = _fl
            except Exception:
                log.exception(f"escalation-reprocessor failed for product {p.get('id')}")

        for p in ready:
            try:
                n_re = _reprocess_blocked_features(p, features=features_by_pid.get(p["id"]))
                if n_re:
                    with _pm_client() as client:
                        _fr = client.get(f"/api/products/{p['id']}/features")
                    _fl = _fr.json() if _fr.is_success else None
                    if isinstance(_fl, list):
                        features_by_pid[p["id"]] = _fl
            except Exception:
                log.exception(f"blocked-reprocessor failed for product {p.get('id')}")

        # Dangling-dependency catch-net: re-home any feature whose depends_on
        # points at a Rejected/Reverted (dead) target onto the live replacement
        # child. The post_doc pipeline fixes this at the SOURCE (designer split
        # time); this per-cycle sweep cleans up what escapes — PM-side
        # rejections, pre-existing tangles, edge cases. Shares the detection
        # logic with the source path (one source of truth). ON by default
        # (DEPENDENCY_REHOME_ENABLED falsy → dry-run logs only).
        for p in ready:
            try:
                n_rh = _repair_dangling_dependencies(p, features=features_by_pid.get(p["id"]))
                if n_rh:
                    with _pm_client() as client:
                        _fr = client.get(f"/api/products/{p['id']}/features")
                    _fl = _fr.json() if _fr.is_success else None
                    if isinstance(_fl, list):
                        features_by_pid[p["id"]] = _fl
            except Exception:
                log.exception(f"dependency-rehome catch-net failed for product {p.get('id')}")

        for p in ready:
            try:
                n_infra = _execute_infra_stories(p, features=features_by_pid.get(p["id"]))
                if n_infra:
                    # Refresh this product's feature snapshot for the
                    # detectors below — statuses just changed.
                    with _pm_client() as client:
                        _fr = client.get(f"/api/products/{p['id']}/features")
                    _fl = _fr.json() if _fr.is_success else None
                    if isinstance(_fl, list):
                        features_by_pid[p["id"]] = _fl
            except Exception:
                log.exception(f"infra-story executor failed for product {p.get('id')}")

        # Human-in-loop phase gate (migration 045). Opt-in per product via
        # config.human_gate_phases; no-op otherwise. Settles completed phases
        # into 'awaiting_review' (+ report + dashboard alert) and reopens a
        # phase when the PM un-blocks a feature. The persona decision tree
        # reads gate_state to freeze later phases until a human approves.
        for p in ready:
            try:
                _run_phase_gate_detector(p, features=features_by_pid.get(p["id"]))
            except Exception:
                log.exception(f"phase-gate detector failed for product {p.get('id')}")

        # Per-cycle architect scheduler. Queues the architect persona when
        # features-pushed delta crosses N (default 3) or the 7-day fallback
        # elapses. Sets run_persona_now="architect" so the Priority-0 block
        # below picks it up on this same cycle. See _check_architect_due for
        # the trigger logic.
        for p in ready:
            try:
                _check_architect_due(p, sys_cfg=cycle_sys_cfg or None,
                                     features=features_by_pid.get(p["id"]))
                # On the architect cadence, also run the deterministic main-suite
                # health check (run_persona_now is set to "architect" by the line
                # above, or by a PM click). Out-of-band in a daemon thread — it
                # runs a containerized pytest on main (~1-2 min) and must NOT block
                # the cycle loop. Files a deduped chore + alert if main is red.
                if p.get("run_persona_now") == "architect":
                    import threading as _th
                    from orchestrator.main_suite_health import detect_broken_main_suite
                    _th.Thread(target=detect_broken_main_suite, args=(dict(p),),
                               daemon=True).start()
                    # Sibling check, same cadence/thread pattern: does a FRESH
                    # clone install+collect from requirements.txt alone? The
                    # pre-baked agent image masks missing declarations from
                    # the per-commit test gate; this is the exhaustive
                    # slow-path (deduped chore `fresh_install:{id}`).
                    from orchestrator.fresh_env_check import detect_broken_fresh_install
                    _th.Thread(target=detect_broken_fresh_install, args=(dict(p),),
                               daemon=True).start()
            except Exception:
                log.exception(f"architect-scheduler failed for product {p.get('id')}")

        # Phase-boundary code-review trigger (2026-06-24, comment-only soak).
        # Runs AFTER the architect scheduler on purpose: a cycle that just queued
        # the architect leaves run_persona_now set, so the gate defers and the
        # semantic audit follows in a later cycle — reading the architect's fresh
        # ARCHITECTURE.md + chores. OPT-IN via CODE_AUDITOR_ENABLED /
        # config.code_auditor; no-op otherwise. Runs for ALL products (autonomous
        # included), like reap_empty_phases — not gated behind human_gate_phases.
        for p in ready:
            try:
                _run_phase_review_gate(p, features=features_by_pid.get(p["id"]))
            except Exception:
                log.exception(f"phase-review gate failed for product {p.get('id')}")
        # and the determine_next_action decision tree — the PM clicked a
        # button, run that persona for that product. Flag is cleared up
        # front so a crashing launch doesn't re-fire on every cycle.
        #
        # 2026-05-19: the up-front clear was over-eager. launch_session has
        # two non-error skip paths (status=already_active / already_launching)
        # that fire when the product happens to have a coder or designer
        # session in flight at the moment the PM clicks the button. Those are
        # not crashes — they're "try again next cycle." Pre-fix, the flag was
        # already cleared by then and the request was silently dropped; the
        # PM had to re-click. Now we inspect launch_session's status and
        # restore the flag on those specific deferred cases. A real crash
        # (run_claude_in_docker raising, GitHub API down, etc.) still doesn't
        # carry those status values, so the original crash-safety is preserved.
        for p in ready:
            persona = None
            patch: dict = {}
            restore_patch: dict = {}
            queued = p.get("run_persona_now")
            if queued and queued in _ONDEMAND_PERSONAS:
                persona = queued
                patch["run_persona_now"] = None
                restore_patch["run_persona_now"] = queued
            elif p.get("run_trainer_now"):
                persona = "product_trainer"
                patch["run_trainer_now"] = False
                restore_patch["run_trainer_now"] = True
            if not persona:
                continue
            pid = p["id"]
            try:
                _pm("PATCH", f"/api/products/{pid}", patch)
            except Exception:
                log.exception(f"[on-demand] could not clear flag on product {pid}")
            log.info(f"[on-demand] launching {persona} for product {pid} ({p.get('name','?')})")
            launch_result = json.loads(launch_session(
                {"product_id": pid, "persona": persona}, **kwargs
            ))
            launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
            if isinstance(launch_data, dict) and launch_data.get("status") in (
                "already_active", "already_launching",
            ):
                log.info(
                    f"[on-demand] launch deferred (status={launch_data.get('status')}, "
                    f"existing_session={launch_data.get('existing_session_id')}) — "
                    f"restoring {list(restore_patch)[0]} on product {pid}"
                )
                try:
                    _pm("PATCH", f"/api/products/{pid}", restore_patch)
                except Exception:
                    log.exception(f"[on-demand] could not restore flag on product {pid}")
                # Honest reporting (matches Priority 2 below) — don't claim
                # "launched" when launch_session short-circuited via the
                # active-session guard.
                return _ok({"action": "deferred", "product_id": pid, "persona": persona,
                            "reason": (
                                f"on-demand {persona} deferred "
                                f"(status={launch_data.get('status')}); "
                                f"flag restored for next cycle"
                            ),
                            "launch": launch_result})
            return _ok({"action": "launched", "product_id": pid, "persona": persona,
                        "reason": f"on-demand {persona}",
                        "launch": launch_result})

        # 5. Find next work
        # Priority 1: reviewer work across all products
        reviewer_raw = json.loads(_pm("GET", "/api/features/next-for-persona?persona=reviewer"))
        reviewer_feature = (reviewer_raw.get("data") if isinstance(reviewer_raw, dict) else reviewer_raw)
        if reviewer_feature and isinstance(reviewer_feature, dict):
            pid = reviewer_feature.get("product_id")
            if any(p["id"] == pid and p.get("status") in ("ready", "running") for p in products):
                # Actually spawn the reviewer container (consistent with the
                # launch_session branch below — run_cycle is single-call).
                launch_result = json.loads(launch_session(
                    {"product_id": pid, "persona": "reviewer"}, **kwargs
                ))
                # If launch_session deferred because a session is already
                # running on this product, fall through to Priority 2
                # (round-robin) so OTHER products' work isn't starved for
                # the duration of the active session. Pre-fix (2026-05-19)
                # this branch returned action=launched regardless of the
                # deferral, which (a) produced misleading "launched reviewer"
                # log lines every cycle for the duration of the blocking
                # session (typically up to 90 minutes), and (b) starved
                # other ready products of any dispatch slot during that
                # window. See the e60fd18 precedent for the Priority-0
                # on-demand path (which had the same shape).
                launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
                if isinstance(launch_data, dict) and launch_data.get("status") in (
                    "already_active", "already_launching",
                ):
                    log.info(
                        f"[reviewer] deferred for product {pid} "
                        f"(feature {reviewer_feature.get('id')}, status="
                        f"{launch_data.get('status')}, existing_session="
                        f"{launch_data.get('existing_session_id')}) — falling "
                        f"through to round-robin"
                    )
                    # fall through (don't return) so Priority 2 gets a turn
                else:
                    return _ok({"action": "launched", "product_id": pid, "persona": "reviewer",
                                "reason": f"reviewer work for feature {reviewer_feature.get('id')}",
                                "launch": launch_result})

        # Priority 2: round-robin product
        next_raw = json.loads(_pm("GET", "/api/products/next"))
        next_product = (next_raw.get("data") if isinstance(next_raw, dict) else next_raw)
        if not next_product or not isinstance(next_product, dict):
            return _ok({"action": "exit", "reason": "No products need work"})

        product_id = next_product["id"]
        action_raw = json.loads(determine_next_action({"product_id": product_id}, **kwargs))
        action_data = action_raw.get("data") if isinstance(action_raw, dict) else action_raw
        if not isinstance(action_data, dict):
            # Even on error, mark this product visited so round-robin advances.
            _bump_product_last_run(product_id)
            return _ok({"action": "exit", "reason": "determine_next_action returned nothing"})

        # Bump last_run_at on every cycle visit, NOT just on successful Docker
        # exits. Without this, products that resolve to action=exit (no
        # actionable work, PR-gated, etc.) keep their stale last_run_at
        # forever and dominate the round-robin — starving other products.
        # The launch_session branch's post-success bump still happens; this
        # is just defensive coverage for the no-launch paths.
        _bump_product_last_run(product_id)

        action = action_data.get("action")
        if action in ("plan_phases", "plan_sprints"):
            # Both names accepted for backward compat — `plan_sprints` was the
            # legacy decide_action name before migration 043 (phases→features
            # flat model). The actual endpoint is now /plan-phases.
            plan_response = _pm("POST", f"/api/products/{product_id}/plan-phases")
            try:
                parsed = json.loads(plan_response)
                # pm_api wraps successes as {"ok": True, "data": ...}; non-2xx
                # surface as {"ok": False, "error": "..."} or include status in
                # the error string.
                if isinstance(parsed, dict) and not parsed.get("ok", True):
                    log.warning(
                        f"[plan-sprints] product {product_id}: planning call "
                        f"failed: {parsed.get('error') or plan_response[:200]}"
                    )
                else:
                    log.info(
                        f"[plan-sprints] product {product_id}: planning call "
                        f"returned {plan_response[:200]}"
                    )
            except Exception:
                log.info(f"[plan-sprints] product {product_id}: {plan_response[:200]}")
            return _ok({"action": "exit", "reason": f"Planned sprints for product {product_id}"})
        elif action == "launch_session":
            persona = action_data.get("persona", "planner")
            # Clear run_now before launching
            _pm("PATCH", f"/api/products/{product_id}", {"run_now": False})
            # Launch directly — we run as a deterministic orchestrator, no LLM
            # multi-turn reasoning needed.
            launch_result = json.loads(launch_session(
                {"product_id": product_id, "persona": persona}, **kwargs
            ))
            # Same shape as the Priority 0 (on-demand) and Priority 1 (reviewer)
            # branches: report the deferred status honestly when launch_session
            # short-circuits via the active-session guard. Priority 2 is the
            # terminal priority — there's nothing to fall through to — so this
            # is a log-honesty fix, not a behavior change. action="deferred"
            # makes the cycle-summary line accurate (operator was seeing
            # "launched session" every cycle while a coder was running, with no
            # container actually spawned).
            launch_data = launch_result.get("data") if isinstance(launch_result, dict) else None
            if isinstance(launch_data, dict) and launch_data.get("status") in (
                "already_active", "already_launching",
            ):
                return _ok({"action": "deferred", "product_id": product_id, "persona": persona,
                            "reason": (
                                f"launch deferred (status={launch_data.get('status')}, "
                                f"existing_session={launch_data.get('existing_session_id')}); "
                                f"product {product_id} already has work in flight"
                            ),
                            "launch": launch_result})
            return _ok({"action": "launched", "product_id": product_id, "persona": persona,
                        "reason": action_data.get("reason", ""),
                        "launch": launch_result})
        else:
            return _ok({"action": "exit", "reason": action_data.get("reason", "nothing to do")})

    except Exception as e:
        log.exception("run_cycle failed")
        return _err(f"run_cycle crashed: {e}")


def determine_next_action(args: dict, **kwargs) -> str:
    """
    Deterministic persona decision tree for a product.

    Phase 5 of OrchestratorRefactor: the body now lives in
    ``orchestrator.cycle.persona._decide_action``. This function is a thin
    wrapper that supplies the signing _pm_client() and JSON-wraps the
    response in tools.py's standard {"ok": ..., "data": ...} envelope.

    Returns ``{"ok": True, "data": {"action": "launch_session"|"plan_sprints"|"exit", ...}}``.
    """
    product_id = args.get("product_id")
    try:
        from orchestrator.cycle.persona import _decide_action  # type: ignore
        with _pm_client() as client:
            result = _decide_action(product_id, client)
        return _ok(result)
    except Exception as e:
        return _err(f"determine_next_action failed: {e}")


def reset_stuck_features(args: dict, **kwargs) -> str:
    return _pm("POST", "/api/features/reset_stuck")


def poller_heartbeat(args: dict, **kwargs) -> str:
    pid = os.getpid()
    host = f"orchestrator-{socket.gethostname()}"
    return _pm("POST", "/api/poller/heartbeat", {"pid": pid, "host": host})


def alert(args: dict, **kwargs) -> str:
    severity = args.get("severity", "info")
    message = args.get("message", "")
    product_name = args.get("product_name")
    try:
        from orchestrator.alerts import send_alert  # type: ignore
        send_alert(severity, message, product_name)
        return _ok({"sent": True})
    except Exception as e:
        return _err(f"alert failed: {e}")


# ---------------------------------------------------------------------------
# Docker / session management
# ---------------------------------------------------------------------------

_PERSONA_ALIASES = {
    "developer": "coder",
    "dev": "coder",
    "coding": "coder",
    "programmer": "coder",
    "writer": "documenter",
    "design": "designer",
}

# Launch-lock: prevents duplicate launches for the same product while
# docker_runner is doing setup (workspace sync, feature assign) before it
# POSTs /api/sessions. Without this, run_cycle re-launches every 60s because
# it sees neither an active container nor an active DB session yet.
import threading as _threading
_LAUNCH_LOCK = _threading.Lock()
_LAUNCHING: set[int] = set()


def launch_session(args: dict, **kwargs) -> str:
    """
    Spawn the agent Docker container for the given product+persona.
    Returns immediately — the container runs in a background daemon thread.
    A launch-lock prevents the same product from being launched concurrently
    while docker_runner is still in its setup phase.
    """
    product_id = args.get("product_id")
    persona = args.get("persona")
    if persona in _PERSONA_ALIASES:
        log.warning("launch_session: aliasing persona %r → %r", persona, _PERSONA_ALIASES[persona])
        persona = _PERSONA_ALIASES[persona]

    # DB-backed guard: survives orchestrator restarts (the in-memory
    # _LAUNCHING set doesn't). If the product already has a session in
    # pending/starting/running state, do NOT launch a second one.
    try:
        with _pm_client() as client:
            active = client.get("/api/sessions/active", params={"product_id": product_id})
        if active.is_success and active.json():
            existing = active.json()
            log.info("launch_session: product %s already has active session %s (status=%s) — skipping",
                     product_id, existing.get("id"), existing.get("status"))
            return _ok({"status": "already_active", "product_id": product_id,
                        "existing_session_id": existing.get("id")})
    except Exception:
        pass  # PM API transient — fall through to in-memory lock

    # In-memory lock catches the narrow window where two launches race
    # before either has created a DB session record.
    with _LAUNCH_LOCK:
        if product_id in _LAUNCHING:
            log.info("launch_session: product %s already launching, skipping", product_id)
            return _ok({"status": "already_launching", "product_id": product_id})
        _LAUNCHING.add(product_id)

    try:
        from orchestrator.docker_runner import run_claude_in_docker  # type: ignore
        with _pm_client() as client:
            product_resp = client.get(f"/api/products/{product_id}")
        if not product_resp.is_success:
            with _LAUNCH_LOCK:
                _LAUNCHING.discard(product_id)
            return _err(f"product {product_id} not found", status=product_resp.status_code)
        product = product_resp.json()

        # Guard: planner with unphased Approved features → call plan-phases instead.
        # Phases→features flat model (migration 043): no active-sprint check;
        # we look at unphased Approved features and call the new flat planner.
        if persona == "planner":
            with _pm_client() as client:
                features_resp = client.get(f"/api/products/{product_id}/features")
            features = features_resp.json() if features_resp.is_success else []
            if isinstance(features, list):
                unphased = [f for f in features
                            if f.get("status") == "Approved" and f.get("phase_id") is None]
                if unphased:
                    log.info("launch_session(planner) intercepted: calling plan-phases for %d features", len(unphased))
                    with _LAUNCH_LOCK:
                        _LAUNCHING.discard(product_id)
                    return _pm("POST", f"/api/products/{product_id}/plan-phases")

        # Fire and forget — run_claude_in_docker blocks for up to SESSION_TIMEOUT minutes.
        def _run():
            try:
                exit_code = run_claude_in_docker(product, persona=persona)
                log.info("launch_session background: product=%s persona=%s exit=%d",
                         product_id, persona, exit_code)
            except Exception:
                log.exception("launch_session background thread crashed")
            finally:
                with _LAUNCH_LOCK:
                    _LAUNCHING.discard(product_id)

        t = _threading.Thread(target=_run, daemon=True, name=f"session-{product_id}-{persona}")
        t.start()
        return _ok({"status": "launched", "product_id": product_id, "persona": persona,
                    "note": "container started in background — run_cycle will skip if already running"})
    except Exception as e:
        with _LAUNCH_LOCK:
            _LAUNCHING.discard(product_id)
        log.exception("launch_session failed")
        return _err(f"launch_session crashed: {e}")


# check_stale_sessions (the progress.md-push-timestamp heartbeat) was
# removed 2026-05-28. It was never called from the cycle loop — the
# per-cycle watchdog (/api/sessions/watchdog/targets + docker-ps
# presence check) replaced it. Agents stopped writing progress.md, so
# the timestamp it keyed on never advanced. orchestrator/heartbeat.py
# was deleted in the same change.


# ---------------------------------------------------------------------------
# PR reconciliation (per-product)
# ---------------------------------------------------------------------------

def reconcile_prs(args: dict, **kwargs) -> str:
    product_id = args.get("product_id")
    try:
        if product_id is None:
            return reset_stuck_features({})
        # Phase 4 of PollerRevamp: route through orchestrator.reconcile, the
        # single per-product entry point that sequences reconcile_merged_prs
        # then reconcile_in_flight_prs with isolated try/except per pass.
        from orchestrator.reconcile import reconcile_product  # type: ignore
        # run_cycle passes the already-fetched product row (same ProductOut
        # schema as GET /api/products/{id}); only re-fetch when absent.
        product = args.get("product")
        if not isinstance(product, dict) or not product.get("id"):
            with _pm_client() as client:
                product_resp = client.get(f"/api/products/{product_id}")
            if not product_resp.is_success:
                return _err(f"product {product_id} not found")
            product = product_resp.json()
        reconcile_product(product)
        # Phase-1 supervisor detectors that operate on open PRs.
        # Cheap to run after the reconcile pass since we re-hit GitHub once
        # for the full open-PR list. Best-effort — never raises.
        try:
            _run_supervisor_pr_detectors(product)
        except Exception:
            log.exception("supervisor PR detectors failed")
        # Open-PR-count invariant retired with the two-tier (session-PR)
        # model: a healthy product now has 1 sprint integration PR + N
        # session PRs open in parallel, so the >1 alert would fire every
        # cycle. The reconcile sweep above already detects orphan PRs.
        return _ok({"reconciled": product_id})
    except Exception as e:
        log.exception("reconcile_prs failed")
        return _err(f"reconcile_prs failed: {e}")


def _run_supervisor_per_product_detectors(
    product: dict,
    sys_cfg: dict | None = None,
    features: list | None = None,
) -> None:
    """Per-cycle supervisor detectors that operate on product-level state
    fetched from the PM API: orphan-Approved + rapid status flap.

    ``sys_cfg``/``features`` are normally supplied by run_cycle's per-cycle
    fetch; self-fetch fallback keeps standalone calls working.
    """
    import httpx as _httpx
    from orchestrator.supervisor import (  # type: ignore
        detect_orphan_approved, detect_rapid_flap, detect_placeholder_blocks,
        detect_repeated_gate_rejection, detect_no_progress_sessions,
        detect_designer_bounce, detect_dead_dependency,
    )

    pid = product.get("id")
    if not pid:
        return

    # Features (full payload — orphan detector needs updated_at)
    if features is None:
        try:
            with _pm_client() as client:
                feat_resp = client.get(f"/api/products/{pid}/features")
                features = feat_resp.json() if feat_resp.is_success else []
        except Exception:
            features = []
    if isinstance(features, list):
        try:
            detect_orphan_approved(product_id=pid, features=features)
        except Exception:
            log.exception(f"orphan_approved detector failed for product {pid}")
        # Reject blocked placeholder/probe junk (runs BEFORE the phase-gate
        # detector so reap_empty_phases sees the post-reject state this cycle).
        try:
            detect_placeholder_blocks(product_id=pid, features=features)
        except Exception:
            log.exception(f"placeholder_reject detector failed for product {pid}")
        # Gate-loop circuit-breaker: route features stuck on the SAME post-coder
        # gate rejection (lint/test/verify) to the diagnose-first escalation
        # early, before they burn hours or eat a misleading cap/flap block
        # (#1873 class). Only for actively-cycling features below the escalation
        # threshold — the detector self-fetches each one's comments and no-ops
        # cheaply otherwise.
        try:
            _esc_thr = int(os.environ.get("ESCALATION_FIX_ATTEMPTS_THRESHOLD", "4"))
            for _f in features:
                if (_f.get("status") in ("Implementing", "Implemented", "Reviewing")
                        and int(_f.get("fix_attempts") or 0) < _esc_thr):
                    detect_repeated_gate_rejection(feature_id=_f["id"], product_id=pid)
        except Exception:
            log.exception(f"gate_loop detector failed for product {pid}")
        # No-progress guard: block a feature whose recent coder sessions keep
        # running long and pushing nothing (hang/timeout token-burn loop that
        # produces no bounce comment, so nothing else catches it — #1873 class).
        # Self-fetches the product's recent sessions.
        try:
            detect_no_progress_sessions(product_id=pid)
        except Exception:
            log.exception(f"no_progress detector failed for product {pid}")
        # Designer-starvation guard: a designer-pool feature repeatedly claimed
        # (→Designing) and rolled back to Approved with no design doc — the
        # designer finds nothing to design (moot / already-shipped duplicate).
        # rapid_flap misses it (slow, <10 transitions/h). Self-fetches changelog
        # for the top-ranked candidates only.
        try:
            detect_designer_bounce(product_id=pid, features=features)
        except Exception:
            log.exception(f"designer_bounce detector failed for product {pid}")
        # Dependency-deadlock guard: a feature waiting for dispatch whose
        # depends_on target is terminal (Rejected/Reverted/Deferred) is frozen
        # forever (the gate only releases on Pushed). Pure in-memory check over
        # the features payload — Block the dependent for PM review.
        try:
            detect_dead_dependency(product_id=pid, features=features)
        except Exception:
            log.exception(f"dead_dependency detector failed for product {pid}")

    # Pull flapping features (uses default thresholds from system_config
    # — endpoint accepts overrides via query string but we fall back to
    # the supervisor defaults to keep wiring simple).
    try:
        sc = sys_cfg
        if sc is None:
            with _pm_client() as client:
                sc_resp = client.get("/api/system-config")
                sc = sc_resp.json() if sc_resp.is_success else {}
        win = sc.get("supervisor_rapid_flap_window_hours") or 1
        thr = sc.get("supervisor_rapid_flap_min_transitions") or 5
        # Oscillation threshold: flag only when a single status is re-entered
        # this many times (default 3) — forward pipeline progression visits each
        # status once and must NOT trip the flap detector.
        rev = sc.get("supervisor_rapid_flap_min_revisits") or 3
        with _pm_client() as client:
            flap_resp = client.get(
                f"/api/products/{pid}/flapping-features",
                params={"window_hours": win, "min_transitions": thr, "min_revisits": rev},
            )
            flapping = flap_resp.json() if flap_resp.is_success else []
        if isinstance(flapping, list) and flapping:
            detect_rapid_flap(product_id=pid, flapping_features=flapping)
    except Exception:
        log.exception(f"rapid_flap detector failed for product {pid}")


# Feature statuses that mean "still actively being worked." Any of these in a
# phase keeps its gate open. The complement (Pushed/Deferred/Rejected/Reverted/
# Blocked) is "settled" — Blocked/Reverted are settled-but-unresolved and become
# the SUBJECT of the phase report, not a reason to keep the gate open. Mirrors
# website.main._SETTLED_STATUSES.
_GATE_ACTIVE_STATUSES = frozenset({
    "Pending", "Approved", "Designing", "Designed",
    "Implementing", "Reviewing", "Reviewed",
})


# Audit after this many architect runs since the last audit. Default 1 during the
# SOAK (max data density — one audit per architect pass for precision sampling);
# steady-state target is 2 (~every 6 features). Env-tunable so the cadence changes
# with a container recreate, no rebuild.
try:
    _CODE_AUDIT_ARCHITECT_RUNS = max(1, int(os.environ.get("CODE_AUDIT_ARCHITECT_RUNS", "1")))
except (TypeError, ValueError):
    _CODE_AUDIT_ARCHITECT_RUNS = 1


def _code_auditor_enabled(product: dict) -> bool:
    """The whole-product semantic audit is ALWAYS ON for every product
    (2026-07-16): the per-product opt-out and its UI checkbox were removed — the
    audit (code_auditor for correctness/tests + security_auditor for security) is
    a non-negotiable quality gate, not an opt-in. `product` is kept in the
    signature for call-site compatibility but is no longer consulted. The ONLY
    remaining off-ramp is the global CODE_AUDITOR_ENABLED kill switch (ops-level,
    default ON) — set it to a falsy value (0/false/no/off) to disable the whole
    environment.
    """
    return os.environ.get("CODE_AUDITOR_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _code_auditor_filing_enabled(product: dict) -> bool:
    """Increment 1 of the filing promotion (2026-06-25): does this product's
    code_auditor FILE findings as `bug` features (filing mode) or only raise
    dashboard alerts (comment-only soak)? OPT-IN, default OFF. Resolution
    mirrors _code_auditor_enabled and the prompt-builder's _code_auditor_filing_on:
      1. product.config["code_auditor_filing"] explicit bool wins.
      2. else CODE_AUDITOR_FILING_ENABLED env — ON only if truthy.
    The prompt builder owns the actual filing instructions; this helper exists so
    the orchestrator can report the mode (and, later, gate the blocker check on it).
    """
    cfg = (product.get("config") or {}).get("code_auditor_filing")
    if isinstance(cfg, bool):
        return cfg
    return os.environ.get("CODE_AUDITOR_FILING_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on")


def _security_auditor_scheduled_enabled(product: dict) -> bool:
    """Resolve the security-auditor SCHEDULING flag. security_auditor has always
    been PM-triggered (on-demand, "🔒 Security audit" button) — this promotes it
    to ALSO ride the phase-review gate on the architect-run cadence, so a product
    still gets a periodic whole-product security sweep without a human clicking.
    Pairs with narrowing code_auditor to correctness+tests: security ownership
    moves fully to this focused single-concern pass. ALWAYS ON per product
    (2026-07-16): like _code_auditor_enabled, the per-product opt-out and UI
    checkbox were removed — this rides the phase-review gate for every product.
    `product` is kept for signature compatibility but not consulted. The ONLY
    off-ramp is the global SECURITY_AUDITOR_SCHEDULED_ENABLED kill switch
    (ops-level, default ON) — set it falsy to disable the whole environment.
    The on-demand PM "🔒 Security audit" button is unaffected either way.
    """
    return os.environ.get("SECURITY_AUDITOR_SCHEDULED_ENABLED", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _select_phase_for_review(phases: list, features: list) -> dict | None:
    """Pure: the lowest-order SETTLED phase with a real outcome — the "phase
    boundary" checkpoint (is there reviewable shipped work at all?). Settled =
    no feature in _GATE_ACTIVE_STATUSES AND >=1 Pushed. The size floor is NOT
    here: cadence is gated on cumulative features-Pushed since the last audit
    (>= _CODE_AUDIT_FEATURE_FLOOR) in the caller, so small phases BATCH instead
    of each triggering a sweep. Returns the phase dict (for logging) or None.
    """
    if not isinstance(phases, list) or not isinstance(features, list):
        return None
    by_phase: dict = {}
    for f in features:
        if f.get("phase_id") is not None:
            by_phase.setdefault(f["phase_id"], []).append(f)
    candidates = []
    for ph in phases:
        feats = by_phase.get(ph.get("id"), [])
        if not feats:
            continue
        if any(f.get("status") in _GATE_ACTIVE_STATUSES for f in feats):
            continue                                         # still active — not settled
        if not any(f.get("status") == "Pushed" for f in feats):
            continue                                         # no real outcome to review
        candidates.append(ph)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.get("order", 0), p.get("id", 0)))
    return candidates[0]


def _run_phase_review_gate(product: dict, features: list | None = None) -> None:
    """Phase-boundary semantic-review trigger (2026-06-24; security split out
    2026-07-16). When a phase settles, queue a read-only whole-product auditor for
    the product (run_persona_now → Priority-0 launch). TWO disjoint auditors ride
    this same gate:
      - ``security_auditor`` — the security sweep (authz/IDOR, secrets, injection,
        CORS). Promoted here from PM-button-only so it runs on a cadence. Gated by
        _security_auditor_scheduled_enabled; always files bugs.
      - ``code_auditor`` — correctness + tests (security dimension removed). Gated
        by _code_auditor_enabled; comment-only soak or filing per its own flag.

    OPT-IN: no-op unless at least one of the two flags is on. Runs for ALL products
    (autonomous included), like reap_empty_phases — NOT gated behind human_gate_phases.

    Cadence: a phase boundary is the CHECKPOINT (>=1 settled phase with a ship);
    the GATE is architect-run count — fire after _CODE_AUDIT_ARCHITECT_RUNS architect
    runs since that auditor's last run. Architect runs every ~3 features Pushed,
    refreshing ARCHITECTURE.md and filing drift chores each time, so each semantic
    audit follows a FRESH architect pass. Each auditor keeps its OWN counter
    (code_auditor → config['architect_runs_at_last_audit']; security_auditor →
    config['architect_runs_at_last_security_audit']) vs the architect's
    config['architect_run_count'] (no migration for the soak). run_persona_now
    SERIALIZES them: if both are due the same cycle, the first fires now and the
    second the next eligible cycle (its counter is untouched, so it stays due).
    Best-effort; never raises.

    MUST be called AFTER the architect scheduler in run_cycle: a cycle that queues
    the architect leaves run_persona_now set, so this returns early and the audit
    follows in a later cycle — never preempting the architect run it rides behind.
    """
    pid = product.get("id")
    if not pid:
        return
    code_on = _code_auditor_enabled(product)
    sec_on = _security_auditor_scheduled_enabled(product)
    if not (code_on or sec_on):
        return
    if product.get("run_persona_now"):                       # don't stomp a queued session
        return
    try:
        with _pm_client() as client:
            ph_resp = client.get(f"/api/products/{pid}/phases")
            phases = ph_resp.json() if ph_resp.is_success else []
            if features is None:
                feat_resp = client.get(f"/api/products/{pid}/features")
                features = feat_resp.json() if feat_resp.is_success else []
            if not _select_phase_for_review(phases, features):
                return                                       # no settled phase → nothing to review
            cfg = product.get("config") or {}
            arch_runs = cfg.get("architect_run_count")
            arch_runs = arch_runs if isinstance(arch_runs, int) else 0
            # Security first (deeper, single-concern), then code_auditor. Each is a
            # (persona, counter_key) that fires when N architect runs have elapsed
            # since its own last run; one persona per cycle (return after queuing).
            candidates = []
            if sec_on:
                candidates.append(("security_auditor", "architect_runs_at_last_security_audit"))
            if code_on:
                candidates.append(("code_auditor", "architect_runs_at_last_audit"))
            for persona, key in candidates:
                last_audit = cfg.get(key)
                last_audit = last_audit if isinstance(last_audit, int) else 0
                if arch_runs - last_audit < _CODE_AUDIT_ARCHITECT_RUNS:
                    continue                                 # wait for N architect runs since this
                                                             # auditor's last run
                new_cfg = dict(cfg)
                new_cfg[key] = arch_runs
                client.patch(f"/api/products/{pid}",
                             json={"run_persona_now": persona, "config": new_cfg})
                product["run_persona_now"] = persona          # reflect for this cycle's Priority-0
                product["config"] = new_cfg
                if persona == "code_auditor":
                    detail = "filing" if _code_auditor_filing_enabled(product) else "comment-only"
                else:
                    detail = "files bugs"
                log.info(f"[phase-review] product={pid}: {arch_runs - last_audit} architect "
                         f"run(s) since last {persona} audit (>= {_CODE_AUDIT_ARCHITECT_RUNS}) "
                         f"— queued {persona} ({detail})")
                return                                        # one persona per cycle
    except Exception:
        log.exception(f"phase-review gate failed for product {pid}")


def _run_phase_gate_detector(product: dict, features: list | None = None) -> None:
    """Human-in-loop phase gate sweep (migration 045). Opt-in per product via
    ``config.human_gate_phases``. Keeps each phase's gate_state in sync with
    feature reality so the persona decision tree (orchestrator/cycle/persona.py)
    can freeze later phases until a human approves:

      - active features present → 'open' (also REOPENS an awaiting_review phase
        when the PM un-blocks a feature back into the pipeline — the rework loop)
      - all features settled + ≥1 Pushed → POST the report (which sets
        'awaiting_review') and raise ONE dashboard alert on the transition
      - 'approved' is a one-way latch only the human sets; never touched here

    Best-effort; never raises. Alert dedup is structural: the report/alert only
    fire while gate is 'open', and the report flips it to 'awaiting_review', so
    each settle transition alerts exactly once.
    """
    pid = product.get("id")
    if not pid:
        return

    try:
        with _pm_client() as client:
            ph_resp = client.get(f"/api/products/{pid}/phases")
            phases = ph_resp.json() if ph_resp.is_success else []
            if features is None:
                feat_resp = client.get(f"/api/products/{pid}/features")
                features = feat_resp.json() if feat_resp.is_success else []
    except Exception:
        log.exception(f"phase-gate detector fetch failed for product {pid}")
        return
    if not isinstance(phases, list) or not isinstance(features, list):
        return

    # Empty-phase reap runs for ALL products, regardless of the human-gate flag —
    # empty/all-rejected phases are cruft either way. (Runs after this cycle's
    # placeholder-reject, so phases emptied this cycle get cleaned up now.)
    try:
        from orchestrator.supervisor import reap_empty_phases  # type: ignore
        reap_empty_phases(product_id=pid, phases=phases, features=features)
    except Exception:
        log.exception(f"empty_phase_reap failed for product {pid}")

    # The human-in-loop gate sweep below is opt-in per product.
    cfg = product.get("config") or {}
    if cfg.get("human_gate_phases", True) is False:   # ON by default (2026-06-09)
        return

    by_phase: dict = {}
    for f in features:
        if f.get("phase_id") is not None:
            by_phase.setdefault(f["phase_id"], []).append(f)

    for ph in phases:
        gate = ph.get("gate_state", "open")
        if gate == "approved":
            continue
        feats = by_phase.get(ph["id"], [])
        if not feats:
            continue
        active = any(f.get("status") in _GATE_ACTIVE_STATUSES for f in feats)
        # A phase warrants a report once it can make no further autonomous
        # progress (no active work) AND has a real outcome to review — shipped
        # work OR unresolved blockers. An all-Blocked/Reverted phase still fires
        # (surface, don't silently freeze — this is the Q1 decision); a phase
        # that is purely Deferred/Rejected (deliberately dropped) does not.
        reportable = any(f.get("status") in ("Pushed", "Blocked", "Reverted") for f in feats)

        if active:
            # Live work in the phase. If it was awaiting review, the PM must have
            # un-blocked a feature — reopen so it re-settles and re-reports.
            if gate == "awaiting_review":
                try:
                    with _pm_client() as client:
                        client.patch(f"/api/phases/{ph['id']}", json={"gate_state": "open"})
                    log.info(f"phase-gate: reopened phase {ph['id']} (active work returned)")
                except Exception:
                    log.exception(f"phase-gate reopen failed for phase {ph['id']}")
            continue

        # All features settled. Generate the report + alert once on transition.
        if reportable and gate != "awaiting_review":
            try:
                with _pm_client() as client:
                    rep = client.post(f"/api/phases/{ph['id']}/report")
                if rep.is_success:
                    with _pm_client() as client:
                        client.post("/api/alerts", json={
                            "product_id": pid,
                            "level": "info",
                            "message": (
                                f"Phase '{ph.get('name')}' has settled and is awaiting "
                                f"your review before the next phase unlocks. "
                                f"See the phase report for blockers and downstream impact."
                            ),
                        })
                    log.info(f"phase-gate: phase {ph['id']} settled → awaiting_review + alert")
            except Exception:
                log.exception(f"phase-gate report/alert failed for phase {ph['id']}")


# Dedupe-marker author. Bumped to -v2 (2026-06-13): the first deploy used
# changed_by="supervisor", which the website Blocked-re-engage gate rejects
# (only pm / blocked-reprocessor may re-engage) — so the unblock PATCHes
# silently 422'd while the marker comment was still posted, falsely spending
# the one-shot without unblocking anything. The new author voids those stale
# v1 markers so falsely-spent features get a real retry.
_REPROCESS_MARKER_AUTHOR = "blocked-reprocessor-v2"
# changed_by for the unblock PATCH — must be in the website's
# _BLOCKED_REENGAGE_CALLERS + _RANK_GUARD_BYPASS allowlists.
_REPROCESS_CHANGED_BY = "blocked-reprocessor"
# Bounce-author substrings that mean a CODE-quality grind (the coder can
# plausibly fix it with a diagnose-first escalation). Env/service/tool/spec
# blocks are deliberately excluded — re-running the coder won't help; those
# need a designer redesign or the service/tool-missing paths, which already
# own them.
_CODE_QUALITY_AUTHORS = ("lint-guard", "post-coder:test-check",
                         "post-coder:verify-check", "reviewer")
_ENV_SPEC_AUTHORS = ("service-missing", "tool-missing", "test-env",
                     "env_broken", "service_missing", "tool_missing")


_ESCALATION_MARKER_AUTHOR = "escalation-reprocessor"


def _truthy_cfg(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _escalate_blocked_features(product: dict, features: list | None = None,
                               max_per_cycle: int = 2) -> int:
    """Premium-model escalation (docs/blocked_escalation_plan.md). Per cycle, give
    each Blocked feature ONE retry on the globally-configured stronger LLM:
    → Approved, escalation_active=true, fix_attempts=0, priority bumped to the top
    band so the orchestrator works it FIRST. Bounded by a daily USD cap. A feature
    that re-blocks after the premium pass goes to 'Stuck' (post_coder) and is never
    re-escalated → no loop.

    OFF unless configured: requires blocked_escalation_enabled + a backend/model +
    a non-zero daily cap (blank/0 ⇒ disabled, a safety default). Returns the number
    escalated. Best-effort; never raises into the cycle.

    RETIRED (migration 049): the coder model ladder supersedes this whole path —
    escalation now happens INLINE per coder session (First Attempt → escalation
    tiers), driven by features.escalation_step, not by unblocking to a separate
    premium pass. Early-return 0 so no feature is routed through the legacy
    escalation_active mechanism. Kept as a no-op (rather than deleted) to avoid
    ripping out the call site + markers mid-change; safe to remove in a follow-up.
    """
    return 0
    pid = product.get("id")
    if not pid:
        return 0
    pname = product.get("name", "?")
    try:
        with _pm_client() as client:
            try:
                cfg = client.get("/api/system-config").json()
            except Exception:
                return 0
            if not _truthy_cfg(cfg.get("blocked_escalation_enabled")):
                return 0
            backend = (cfg.get("blocked_escalation_backend") or "").strip()
            model = (cfg.get("blocked_escalation_model") or "").strip()
            if backend not in ("claude-api", "openai") or not model:
                return 0
            try:
                cap = float(cfg.get("blocked_escalation_daily_usd_cap") or 0)
            except (TypeError, ValueError):
                cap = 0.0
            if cap <= 0:
                return 0  # blank/0 ⇒ disabled (safety: no unbounded spend)
            # Daily-cost gate (sum of today's premium sessions' cost_usd).
            try:
                spend = float(client.get(
                    "/api/sessions/escalation-spend-today").json().get("spend_usd", 0))
            except Exception:
                spend = 0.0
            if spend >= cap:
                log.info(f"[escalation] {pname}: budget exhausted "
                         f"(${spend:.2f} >= cap ${cap:.2f}) — skipping")
                return 0

            if features is None:
                try:
                    fr = client.get(f"/api/products/{pid}/features")
                    features = fr.json() if fr.is_success else []
                except Exception:
                    return 0
            blocked = [f for f in (features or [])
                       if isinstance(f, dict) and f.get("status") == "Blocked"]
            max_attempts = cfg.get("blocked_escalation_max_attempts", 3)
            escalated = 0
            for f in blocked:
                if escalated >= max_per_cycle:
                    break
                fid = f.get("id")
                if not isinstance(fid, int):
                    continue
                reason = (f.get("blocked_reason") or "").lower()
                # env/service/tool/spec blocks: a stronger model won't fix infra.
                if any(a in reason for a in _ENV_SPEC_AUTHORS):
                    continue
                # Dedupe: one escalation per feature, ever (re-block → Stuck, so a
                # Blocked feature has never been escalated — the marker is belt-and-
                # suspenders against a stray return-to-Blocked).
                try:
                    cr = client.get(f"/api/features/{fid}/comments", params={"limit": 50})
                    comments = cr.json() if cr.is_success else []
                except Exception:
                    comments = []
                if any(isinstance(c, dict) and c.get("author") == _ESCALATION_MARKER_AUTHOR
                       for c in comments):
                    continue
                try:
                    _r = client.patch(f"/api/features/{fid}", json={
                        "status": "Approved", "changed_by": "escalation-reprocessor",
                        "escalation_active": True, "fix_attempts": 0, "priority": 1,
                    })
                    # Only mark/count on a CONFIRMED unblock. A rejected PATCH
                    # (e.g. rank guard) must NOT leave the one-shot marker comment,
                    # or the feature gets permanently deduped out of escalation.
                    if not _r.is_success:
                        log.warning(f"[escalation] {pname}: #{fid} unblock PATCH "
                                    f"failed ({_r.status_code}): {(_r.text or '')[:200]}")
                        continue
                    client.post(f"/api/features/{fid}/comments", json={
                        "author": _ESCALATION_MARKER_AUTHOR,
                        "body": (f"🚀 **Premium escalation** — base model exhausted; retrying on "
                                 f"`{backend}` / `{model}` (≤{max_attempts} attempts), prioritised. "
                                 f"If it still fails → **Stuck** (no further auto-retry)."),
                    })
                    escalated += 1
                    log.info(f"[escalation] {pname}: #{fid} → premium pass "
                             f"({backend}/{model}), prioritised")
                except Exception as e:
                    log.warning(f"[escalation] {pname}: #{fid} escalate failed: {e}")
            return escalated
    except Exception:
        log.exception(f"[escalation] {pname}: driver failed (non-fatal)")
        return 0


# Seconds figures like "300s" / "1800 s" cited in a diagnosis. Bounded (2-4
# digits) and range-checked at the callsite so we don't pick up "335-test".
_GATE_BUDGET_SECONDS_RE = _re.compile(r"(\d{2,4})\s*s\b")


def _stale_test_gate_env_block(reason: str, comments: list,
                               product: dict) -> tuple[int, int] | None:
    """Detect a Blocked feature whose env block is a post-coder TEST-GATE
    TIMEOUT that is now STALE — the cited gate budget is below the product's
    *current* resolved budget, because the gate self-calibrates up as green-run
    samples accumulate. Returns ``(current_budget, cited_budget)`` when a
    one-shot re-test is warranted, else ``None``.

    Why this exists: env_impossible blocks are otherwise skipped forever ("a
    coder re-run won't help") — correct for service/tool/spec, but WRONG for a
    gate-timeout, because the *environment itself changed* (the budget grew).
    A transient low-budget window (a product that inflated its suite before
    accumulating green-run samples, so the budget was floored) then becomes a
    PERMANENT block even after the budget auto-calibrates up. Canonical:
    DogTinder product 31 (2026-07), six features blocked citing a 300s gate that
    had since grown to the 1800s ceiling; a manual re-test cleared them.

    Only the timeout class qualifies. Service-missing, tool-missing,
    verify-recipe, and read-only-file env blocks are NOT gate-timeouts and stay
    skipped. Best-effort: any resolution failure returns None (preserve skip).
    """
    blob = (reason or "") + " " + " ".join(
        str(c.get("body", "")) for c in (comments or []) if isinstance(c, dict))
    low = blob.lower()
    # Signal 1 — a post-coder TEST gate/suite/check context.
    gate_ctx = (("post-coder" in low or "post coder" in low)
                and "test" in low
                and any(k in low for k in ("gate", "suite", "check")))
    # Signal 2 — the failure is a TIMEOUT / budget-exceed (not a missing dep).
    timeout_ctx = any(k in low for k in (
        "timeout", "budget", "exceed", "cannot be met", "cannot accommodate",
        "unachievable", "too short", "structurally", "300s"))
    # Signal 3 — exclude OTHER env classes that may mention the gate in passing.
    other_env = any(k in low for k in (
        "service", "connection refused", "tool-missing", "read-only",
        "read only", "erofs", "verify-check", "verify recipe", "cannot execute"))
    if not (gate_ctx and timeout_ctx) or other_env:
        return None
    try:
        from orchestrator.pipelines.post_coder import _resolve_test_gate_timeout
        current = int(_resolve_test_gate_timeout(product))
    except Exception:
        return None
    # Cited budget = the LARGEST plausible seconds figure in the diagnosis. Max
    # (not min) is the conservative choice: a diagnosis often quotes BOTH the gate
    # budget and the suite runtime (e.g. "suite takes 1298s (gate budget 1800s)"),
    # so we only unblock when the current budget exceeds EVERY figure — i.e. the
    # budget grew past the old gate AND the suite would fit. A block that already
    # cites the current ceiling (nothing grew, the suite genuinely doesn't fit)
    # gives current == max → skip, which is correct. Require an explicit citation:
    # with no seconds figure we can't confirm the budget grew, so skip.
    cited = None
    for m in _GATE_BUDGET_SECONDS_RE.finditer(blob):
        v = int(m.group(1))
        if 60 <= v <= 3600:
            cited = v if cited is None else max(cited, v)
    if cited is None:
        return None
    return (current, cited) if current > cited else None


def _reprocess_blocked_features(product: dict, features: list | None = None,
                                max_per_cycle: int = 2) -> int:
    """Wave-8: give a Blocked feature ONE automatic retry through the
    machinery that can now handle its blocker class — draining the
    accumulated pool instead of letting it grow (2026-06-13 audit: 75
    Blocked, class distribution unchanged across waves because the fixes
    are all PREVENTIVE and nothing re-processes the existing pool).

    Each feature is reprocessed AT MOST ONCE (a `blocked-reprocessor`
    comment is the dedupe marker), so this can't loop. ON by default for all
    products (2026-06-13): product.config.blocked_reprocessor explicit bool
    wins (per-product opt-OUT); BLOCKED_REPROCESSOR_ENABLED is a global kill
    switch (set to a falsy value to disable everywhere). Routing by the
    wave-6-enriched blocked_reason + bounce-author signature:

      - divergent_review_feedback → unblock to Approved, clear the design
        doc, fix_attempts=0. The 25-comment cumulative checklist converges
        this class (proven: DogTinder #1490 shipped this way).
      - rapid_flap / fix_attempts cap whose bounce authors are CODE-quality
        (lint/test/verify/reviewer, not env/spec) → unblock to Approved,
        keep the doc, set fix_attempts = the escalation threshold so the
        NEXT coder session runs the diagnose-first escalation (wave-5)
        instead of another blind attempt. This is also the fix for the
        flap-preempts-escalation gap: rapid_flap blocks on transition count
        at fix_attempts=3, before the escalation trigger (>=4) fires — the
        reprocessor sets the trigger condition explicitly.
      - STALE test-gate timeout env block (cited gate budget < the product's
        current resolved budget) → unblock to Approved, fix_attempts=0. The
        environment CHANGED (the gate self-calibrated up), so unlike other env
        blocks a re-run CAN now pass. See _stale_test_gate_env_block.
      - other env/service/tool/spec blocks → SKIP (a coder re-run won't help;
        the service/tool-missing + spec_defect paths own those).

    Returns the number reprocessed. Best-effort; never raises into the cycle.
    """
    cfg = product.get("config") or {}
    _flag = cfg.get("blocked_reprocessor")
    if isinstance(_flag, bool):
        enabled = _flag                              # per-product opt-out wins
    else:
        enabled = os.environ.get(                    # default ON; env kill switch
            "BLOCKED_REPROCESSOR_ENABLED", "").strip().lower() \
            not in ("0", "false", "no", "off")
    if not enabled:
        return 0
    pid = product.get("id")
    if not pid:
        return 0
    if features is None:
        try:
            with _pm_client() as client:
                fr = client.get(f"/api/products/{pid}/features")
            features = fr.json() if fr.is_success else []
        except Exception:
            return 0
    if not isinstance(features, list):
        return 0
    blocked = [f for f in features if f.get("status") == "Blocked"]
    if not blocked:
        return 0

    esc_threshold = int(os.environ.get("ESCALATION_FIX_ATTEMPTS_THRESHOLD", "4"))
    reprocessed = 0
    pname = product.get("name", "?")
    try:
        with _pm_client() as client:
            for f in blocked:
                if reprocessed >= max_per_cycle:
                    break
                fid = f.get("id")
                if not isinstance(fid, int):
                    continue
                reason = (f.get("blocked_reason") or "").lower()

                # Coder model ladder (migration 049): a feature Blocked because it
                # climbed the ENTIRE model ladder and still failed has already had
                # every configured tier. escalation_step stays maxed (the reprocessor
                # never resets it), so an auto-retry would just re-run the base model
                # and re-Block. Leave it Blocked for human triage.
                if "escalation ladder exhausted" in reason:
                    continue

                # Dedupe: one auto-retry per feature, ever.
                try:
                    cr = client.get(f"/api/features/{fid}/comments", params={"limit": 50})
                    comments = cr.json() if cr.is_success else []
                except Exception:
                    comments = []
                if any(isinstance(c, dict) and c.get("author") == _REPROCESS_MARKER_AUTHOR
                       for c in comments):
                    continue

                # Classify by blocked_reason + bounce authors.
                authors_blob = " ".join(
                    (c.get("author") or "") for c in comments if isinstance(c, dict))
                is_env_spec = any(a in reason or a in authors_blob for a in _ENV_SPEC_AUTHORS)
                is_divergent = "divergent" in reason
                is_flap_or_cap = ("rapid_flap" in reason or "flap loop" in reason
                                  or "fix_attempts" in reason or "max_fix_attempts" in reason)
                is_code_quality = any(a in reason or a in authors_blob
                                      for a in _CODE_QUALITY_AUTHORS)
                # Stale test-gate timeout: env changed (budget grew), so a
                # re-test CAN pass — the one exception to the env skip below.
                stale_gate = _stale_test_gate_env_block(reason, comments, product)

                if is_env_spec and not is_divergent and stale_gate is None:
                    continue  # coder re-run won't help — owned by other paths

                if is_divergent:
                    # → Approved: the handler zeroes fix_attempts on this
                    # transition, which is exactly what a divergent retry
                    # wants (clean slate for the cumulative checklist).
                    patch = {"status": "Approved", "changed_by": _REPROCESS_CHANGED_BY,
                             "design_doc_path": None, "design_doc": None,
                             "fix_attempts": 0}
                    note = ("Auto-retry (blocked-reprocessor): divergent_review "
                            "class — cumulative-feedback checklist converges this "
                            "(cf. #1490). Doc cleared for clean redesign. One-shot.")
                elif is_flap_or_cap and is_code_quality:
                    # → Implementing + changes_requested (NOT Approved): the
                    # handler zeroes fix_attempts on →Approved, which would
                    # defeat the escalation signal. Implementing preserves
                    # fix_attempts, and changes_requested makes it coder-
                    # eligible as a rework, so the next session escalates.
                    patch = {"status": "Implementing", "changed_by": _REPROCESS_CHANGED_BY,
                             "review_outcome": "changes_requested",
                             "fix_attempts": esc_threshold}
                    note = (f"Auto-retry (blocked-reprocessor): code-quality "
                            f"flap/cap — re-engaged to Implementing with "
                            f"fix_attempts={esc_threshold} so the next coder "
                            f"session runs the diagnose-first escalation instead "
                            f"of another blind attempt. One-shot.")
                elif stale_gate is not None:
                    # → Approved with a clean fix_attempts slate: the prior
                    # attempts were spent against an impossible gate, not the
                    # feature's code. Keep the design doc (design was fine; the
                    # env was the blocker), so a doc-bearing feature goes
                    # straight back to the coder.
                    cur_budget, cited_budget = stale_gate
                    patch = {"status": "Approved", "changed_by": _REPROCESS_CHANGED_BY,
                             "fix_attempts": 0}
                    note = (f"Auto-retry (blocked-reprocessor): stale test-gate env "
                            f"block — the post-coder test-gate budget is now "
                            f"{cur_budget}s, above the {cited_budget}s cited when it "
                            f"blocked, so the suite now fits. Re-testing under the "
                            f"current budget. One-shot.")
                else:
                    continue  # unclassified — leave for human triage

                try:
                    pr = client.patch(f"/api/features/{fid}", json=patch)
                    # Only spend the one-shot if the unblock actually landed —
                    # a rejected re-engage must NOT post the dedup marker (the
                    # v1 bug: 422'd unblocks still marked the feature spent).
                    if not (200 <= pr.status_code < 300):
                        log.warning(f"[reprocessor] {pname}: #{fid} unblock PATCH "
                                    f"returned {pr.status_code} — not marking; will retry")
                        continue
                    client.post(f"/api/features/{fid}/comments",
                                json={"author": _REPROCESS_MARKER_AUTHOR, "body": note})
                    reprocessed += 1
                    log.info(f"[reprocessor] {pname}: #{fid} auto-retried ({note[:60]}...)")
                except Exception:
                    log.exception(f"[reprocessor] {pname}: retry PATCH failed for #{fid}")
    except Exception:
        log.exception(f"[reprocessor] {pname}: failed (non-fatal)")
    return reprocessed


def _repair_dangling_dependencies(product: dict, features: list | None = None) -> int:
    """Catch-net for the re-decomposition stranding class: re-home any feature
    whose ``depends_on`` points at a Rejected/Reverted (dead) target onto its
    live replacement child, so the dispatch gate stops holding it forever.

    The post_doc pipeline fixes this at the SOURCE (designer split time); this
    per-cycle sweep is the safety net for what escapes — PM-side rejections,
    pre-existing tangles, edge cases the source path can't see. Shares the
    detection logic (``orchestrator.cycle.dependencies.dangling_dependency_repairs``)
    with the source path — one source of truth, no drift.

    ON by default for all products; ``DEPENDENCY_REHOME_ENABLED`` falsy
    (``0``/``false``/``no``/``off``) disables (dry-run: detect + log, mutate
    nothing). Best-effort; never raises into the cycle. Returns count applied.
    """
    enabled = os.environ.get("DEPENDENCY_REHOME_ENABLED", "on").strip().lower() \
        not in ("0", "false", "no", "off", "")
    pid = product.get("id")
    if not pid:
        return 0
    try:
        from orchestrator.cycle.dependencies import dangling_dependency_repairs
    except Exception:
        return 0
    if features is None:
        try:
            with _pm_client() as client:
                _r = client.get(f"/api/products/{pid}/features")
            features = _r.json() if _r.is_success else None
        except Exception:
            return 0
    if not isinstance(features, list):
        return 0
    repairs = dangling_dependency_repairs(features)
    if not repairs:
        return 0
    if not enabled:
        for r in repairs:
            log.info(f"[rehome-dry-run] product {pid}: would repair "
                     f"#{r['feature_id']}: {r['reason']}")
        return 0
    applied = 0
    try:
        with _pm_client() as client:
            for r in repairs:
                fid, new_dep = r["feature_id"], r["new_dep"]
                try:
                    if new_dep is not None:
                        client.patch(f"/api/features/{fid}", json={
                            "depends_on": new_dep, "changed_by": "reconciler:rehome"})
                        client.post(f"/api/features/{fid}/comments", json={
                            "author": "reconciler:rehome",
                            "body": (f"🔧 Dependency repair (catch-net) — {r['reason']}. "
                                     f"Re-homed off the Rejected target onto its live "
                                     f"replacement so the dispatch gate releases this once "
                                     f"the replacement ships."),
                        })
                        applied += 1
                    else:
                        client.post("/api/alerts", json={
                            "level": "warning",
                            "message": f"Dangling dependency — feature #{fid}: {r['reason']}"})
                except Exception as e:
                    log.warning(f"[rehome] product {pid}: repair for #{fid} failed: {e}")
    except Exception:
        log.exception(f"[rehome] product {pid}: catch-net sweep failed")
        return applied
    if applied:
        log.info(f"[rehome] product {pid}: re-homed {applied} dangling dependency(ies)")
    return applied


def _execute_infra_stories(product: dict, features: list | None = None) -> int:
    """Deterministic executor for Approved ``feature_type='infra'`` stories
    (service provisioning, Phase B). No LLM, no coder session:

      1. Parse the service name out of the story text against
         orchestrator/services.py's SERVICE_CATALOG allowlist (agent-
         authored text can NAME a service, never supply an image).
      2. Merge it into product.config.services (DB JSONB — orchestrator/PM
         territory; the RO-mounted product_config.json file is untouched).
      3. Provision once + readiness-probe + teardown (smoke test).
      4. PATCH the story to Pushed with merge_notes, so it flows through
         phases/release-notes like any shipped feature.

    A story naming no catalog service is Blocked with a clear reason
    (decisive — no silent every-cycle retry loop). Failure to provision
    leaves the story Approved (retried next cycle) + one operator alert.
    Returns the number executed. Best-effort; never raises into run_cycle.
    """
    from orchestrator.services import (
        SERVICE_CATALOG, ensure_session_services, teardown_session_services,
    )
    pid = product.get("id")
    if not pid:
        return 0
    if features is None:
        try:
            with _pm_client() as client:
                fr = client.get(f"/api/products/{pid}/features")
            features = fr.json() if fr.is_success else []
        except Exception:
            return 0
    if not isinstance(features, list):
        return 0
    infra = [f for f in features
             if f.get("feature_type") == "infra" and f.get("status") == "Approved"]
    executed = 0
    for f in infra:
        fid = f.get("id")
        text = f"{f.get('name', '')} {f.get('description', '')}".lower()
        svc = next((s for s in SERVICE_CATALOG if s in text), None)
        if svc is None:
            try:
                with _pm_client() as client:
                    client.patch(f"/api/features/{fid}", json={
                        "status": "Blocked",
                        "blocked_reason": (
                            "infra story names no catalog service. Provisionable "
                            f"services: {', '.join(sorted(SERVICE_CATALOG))}. "
                            "Rename the story to include one, or implement the "
                            "dependency another way (in-memory fake)."
                        ),
                    })
                log.warning(f"[infra] feature #{fid}: no catalog service in story text — Blocked")
            except Exception:
                log.exception(f"[infra] could not Block unrecognized infra story #{fid}")
            continue

        # Smoke-test provisioning BEFORE declaring, so a broken image/daemon
        # doesn't leave the product declaring a service that can't start.
        smoke_uid = f"infra{fid}"
        try:
            env = ensure_session_services(
                {"config": {"services": [svc]}, "name": product.get("name", "?")},
                smoke_uid,
            )
        finally:
            teardown_session_services(smoke_uid)
        if not env:
            log.warning(f"[infra] feature #{fid}: {svc} failed smoke provisioning — leaving Approved (retry next cycle)")
            try:
                with _pm_client() as client:
                    client.post("/api/alerts", json={
                        "product_id": pid, "level": "warning",
                        "message": (
                            f"Infra story #{fid}: provisioning smoke-test for "
                            f"'{svc}' failed (image pull or readiness). Will "
                            f"retry each cycle; check the orchestrator's docker "
                            f"daemon / network."
                        ),
                    })
            except Exception:
                pass
            continue

        try:
            # Fresh read right before the config write to shrink the
            # read-modify-write window (other actors touch config.last_*_at).
            with _pm_client() as client:
                pr = client.get(f"/api/products/{pid}")
                cfg = dict((pr.json() or {}).get("config") or {}) if pr.is_success \
                    else dict(product.get("config") or {})
                svcs = list(cfg.get("services") or [])
                if svc not in svcs:
                    svcs.append(svc)
                cfg["services"] = svcs
                client.patch(f"/api/products/{pid}", json={"config": cfg})
                client.patch(f"/api/features/{fid}", json={
                    "status": "Pushed",
                    "merge_notes": (
                        f"{SERVICE_CATALOG[svc]['image']} provisioned per-session by the "
                        f"orchestrator; {SERVICE_CATALOG[svc]['env_var']} injected into "
                        f"agent/test/verify containers. Executed deterministically "
                        f"(no coder session)."
                    ),
                })
            executed += 1
            log.info(f"[infra] {product.get('name', '?')}: feature #{fid} → Pushed ({svc} declared + smoke-tested)")
        except Exception:
            log.exception(f"[infra] declare/PATCH failed for feature #{fid}")
    return executed


_DEFAULT_ARCHITECT_PUSHED_THRESHOLD = 3
_ARCHITECT_FALLBACK_SECONDS = 7 * 24 * 3600


def _check_architect_due(
    product: dict,
    sys_cfg: dict | None = None,
    features: list | None = None,
) -> None:
    """Per-cycle: launch the architect persona when ARCHITECTURE.md drift
    is likely. Two triggers, either is sufficient:

      1. **Delta-since-last-run >= N.** Count of *product features*
         (feature_type='feature' — NOT bugs/chores/infra) with status=Pushed
         minus `product.config.features_pushed_at_last_architect`. N defaults
         to 3, soft-overrideable via `system_config.architect_pending_threshold`
         (no migration required — falls back if the column doesn't exist).
         The architect persona's prompt step 7 already writes both
         `last_architect_at` and `features_pushed_at_last_architect` on
         completion, so the counters self-maintain.

      2. **7-day fallback.** Even if no features pushed (dormant product),
         run architect once a week to catch quality-gate tampering,
         DEPRECATED entries whose files were quietly deleted, etc. Without
         this, a paused product's ARCHITECTURE.md never gets reviewed.

    Side-effect: PATCHes `run_persona_now="architect"` on the product. The
    existing on-demand launch path at run_cycle line 603+ then picks it up
    on the same cycle. If `run_persona_now` is already set (PM clicked a
    button, or a prior scheduler decision wasn't consumed yet), do nothing
    -- the existing flag wins.

    Best-effort: never raises into the cycle.
    """
    import httpx as _httpx
    from datetime import datetime as _dt, timezone as _tz

    pid = product.get("id")
    if not pid:
        return
    if product.get("run_persona_now"):
        return  # something else already queued; don't overwrite

    cfg = product.get("config") or {}
    threshold = _DEFAULT_ARCHITECT_PUSHED_THRESHOLD
    last_at_iso = cfg.get("last_architect_at")
    last_pushed_count = cfg.get("features_pushed_at_last_architect") or 0

    try:
        sc = sys_cfg
        if sc is None:
            with _pm_client() as client:
                sc_resp = client.get("/api/system-config")
                sc = sc_resp.json() if sc_resp.is_success else {}
        if isinstance(sc, dict):
            override = sc.get("architect_pending_threshold")
            if isinstance(override, int) and override > 0:
                threshold = override
    except Exception:
        pass  # use default

    # Count Pushed *product features* — feature_type='feature' ONLY. Bugs and
    # chores (and infra) are deliberately EXCLUDED from the architect cadence:
    #   (a) they're small/corrective and don't reshape the architecture the way
    #       feature work does, so 3 bug-fixes in a row shouldn't trigger a drift
    #       review; and
    #   (b) counting them creates a self-reinforcing loop — the code_auditor
    #       (whose cadence rides on architect_run_count) FILES bugs, those bugs
    #       get fixed + Pushed, that count re-triggers the architect, which
    #       re-triggers the code_auditor, which files more bugs. Excluding
    #       bug/chore here breaks that loop at the source.
    # Same pushed_count is reused for the baseline write below, so read and
    # write stay consistent. The stored baseline from the old all-types count
    # self-heals: the first post-change delta errs toward NOT firing, and the
    # next architect run re-baselines on the feature-only count.
    # Normally supplied by run_cycle's per-cycle snapshot; self-fetch fallback
    # for standalone calls.
    pushed_count = 0
    try:
        if features is None:
            with _pm_client() as client:
                f_resp = client.get(f"/api/products/{pid}/features")
                features = f_resp.json() if f_resp.is_success else []
        if isinstance(features, list):
            pushed_count = sum(
                1 for f in features
                if f.get("status") == "Pushed"
                and (f.get("feature_type") or "feature") == "feature"
            )
    except Exception:
        return  # can't decide without features; defer to next cycle

    delta = pushed_count - int(last_pushed_count or 0)
    reasons: list[str] = []

    if delta >= threshold:
        reasons.append(
            f"{delta} features Pushed since last architect run "
            f"(threshold {threshold})"
        )

    if last_at_iso:
        try:
            last_at = _dt.fromisoformat(str(last_at_iso).replace("Z", "+00:00"))
            age = (_dt.now(_tz.utc) - last_at).total_seconds()
            if age >= _ARCHITECT_FALLBACK_SECONDS:
                reasons.append(f"7d fallback ({int(age/3600)}h since last run)")
        except Exception:
            pass
    else:
        # Never run before. Trigger on the fallback condition so first-cycle
        # ARCHITECTURE.md gets at least one architect pass without waiting
        # for the delta threshold.
        reasons.append("first architect run for this product")

    # Retry-on-failure override (2026-05-26): when the cadence says "skip" but
    # the most recent architect session for this product ended badly
    # (exit_code != 0 OR status in {killed, orphaned}), re-queue immediately.
    # Without this, the counter-advances-at-queue-time policy below silently
    # consumes the architect's budget on a failed run — e.g. an Ollama 429
    # rate-limit cascade kills the architect, the counter still advances,
    # and ARCHITECTURE.md MODULES stays empty for 7 days until the fallback
    # fires. Canonical 2026-05-26 SmokeTest incident: the architect ran
    # exactly once on the new product, MODULES never got populated, and the
    # post-coder drift defenses were left with no source-of-truth registry.
    if not reasons:
        try:
            with _pm_client() as client:
                r = client.get(f"/api/products/{pid}/sessions?limit=10")
            sessions = r.json() if r.is_success else []
            last_arch = next(
                (s for s in sessions if s.get("persona") == "architect"),
                None,
            )
            if last_arch is not None:
                bad_exit = (last_arch.get("exit_code") or 0) != 0
                bad_status = (last_arch.get("status") or "") in (
                    "killed", "orphaned",
                )
                if bad_exit or bad_status:
                    reasons.append(
                        f"previous architect session #{last_arch.get('id','?')} "
                        f"failed (exit={last_arch.get('exit_code')}, "
                        f"status={last_arch.get('status')}) — retrying"
                    )
        except Exception:
            pass  # any error → defer to next cycle, don't loop on API issues

    if not reasons:
        return

    # Advance the cadence counters AT QUEUE TIME, not at agent completion.
    # Originally the architect's prompt step 7 was responsible for writing
    # last_architect_at + features_pushed_at_last_architect via curl PATCH,
    # but Ollama LLMs (qwen3-coder etc.) routinely skip that step -- session
    # 2903 on MyDocusign 2026-05-20 exited 0 without writing the counter,
    # so the next cycle's scheduler re-fired the architect (since last_at
    # was still null) and session 2904 launched immediately after. With
    # cadence ownership in the scheduler, the counter advances even when
    # the agent fails / skips / crashes; the worst case is a missed drift
    # detection bounded by the 7-day fallback.
    #
    # Config is a JSONB column and the PATCH replaces it wholesale (no
    # field-level merge in api_update_product), so fetch existing config
    # and merge our two keys before sending.
    existing_cfg = dict(cfg)  # cfg captured at top of this function
    merged_cfg = {
        **existing_cfg,
        "last_architect_at": _dt.now(_tz.utc).isoformat(),
        "features_pushed_at_last_architect": pushed_count,
        # Monotonic count of architect runs (advanced at queue time, same as the
        # other cadence counters above). The code_auditor gate rides on this:
        # it fires after every _CODE_AUDIT_ARCHITECT_RUNS architect runs, so the
        # semantic audit always follows a fresh architect pass.
        "architect_run_count": (existing_cfg.get("architect_run_count") or 0) + 1,
    }
    try:
        with _pm_client() as client:
            client.patch(
                f"/api/products/{pid}",
                json={
                    "run_persona_now": "architect",
                    "config": merged_cfg,
                },
            )
        # Mutate the in-memory product dict too so the Priority-0 on-demand
        # block in the same run_cycle picks the flag up immediately, AND so
        # subsequent _check_architect_due calls in the same cycle (e.g. if
        # multiple products are processed in a loop) see the updated cfg
        # for *this* product.
        product["run_persona_now"] = "architect"
        product["config"] = merged_cfg
        log.info(
            f"[architect-scheduler] product {pid} ({product.get('name','?')}): "
            f"queued architect -- {'; '.join(reasons)}"
        )
    except Exception:
        log.exception(
            f"[architect-scheduler] could not queue architect for product {pid}"
        )


def _run_supervisor_pr_detectors(product: dict) -> None:
    """Pull open PRs + features once, run dirty-PR + overlap-PR detectors."""
    import re as _re
    import httpx as _httpx
    from orchestrator.supervisor import detect_dirty_prs, detect_overlapping_prs  # type: ignore

    repo_url = product.get("github_repo") or ""
    if not repo_url:
        return
    m = _re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", repo_url)
    if not m:
        return
    slug = m.group(1)

    # Bearer token — App installation first, PAT only as transition fallback.
    pat = _get_github_token()
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"

    # Fetch open PRs (one call covers both detectors)
    try:
        prs_resp = _httpx.get(
            f"https://api.github.com/repos/{slug}/pulls",
            headers=headers, params={"state": "open", "per_page": 30}, timeout=10,
        )
        prs = prs_resp.json() if prs_resp.status_code == 200 else []
    except Exception:
        prs = []
    if not isinstance(prs, list) or not prs:
        return

    # Augment each PR with last_commit_at (fetched per-PR; cap at 30 to bound
    # GitHub API calls per cycle).
    enriched = []
    for pr in prs[:30]:
        if not isinstance(pr, dict):
            continue
        pr_n = pr.get("number")
        try:
            commits_resp = _httpx.get(
                f"https://api.github.com/repos/{slug}/pulls/{pr_n}/commits",
                headers=headers, params={"per_page": 1, "direction": "desc"}, timeout=10,
            )
            cs = commits_resp.json() if commits_resp.status_code == 200 else []
            last_commit_at = (
                cs[-1]["commit"]["committer"]["date"]
                if cs and isinstance(cs, list) and cs[-1].get("commit") else None
            )
        except Exception:
            last_commit_at = None
        enriched.append({
            "number":          pr_n,
            "title":           pr.get("title", ""),
            "created_at":      pr.get("created_at"),
            "mergeable_state": pr.get("mergeable_state"),
            "last_commit_at":  last_commit_at,
        })

    # Pull features once for the dirty-PR feature reset
    try:
        with _pm_client() as client:
            feat_resp = client.get(f"/api/products/{product['id']}/features")
            features = feat_resp.json() if feat_resp.is_success else []
    except Exception:
        features = []
    if not isinstance(features, list):
        features = []

    # 1-PR model: every open PR is a session PR; no sprint integration
    # PR exists to exclude. Both detectors operate uniformly.
    detect_dirty_prs(
        product_id=product["id"], github_repo=repo_url,
        open_prs_with_state=enriched, features=features, github_token=pat,
    )
    detect_overlapping_prs(
        product_id=product["id"], github_repo=repo_url,
        open_prs=enriched, github_token=pat,
    )


