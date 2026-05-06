"""ProductFactory orchestrator main loop.

Runs on the host (typically Windows via NSSM / Task Scheduler), holds the
distributed lock, and drives every product cycle: auth check → discovery
→ stale-container kill → stuck-feature reset → PR reconciliation →
auto-merge sweep → persona dispatch → docker session.

This module is the cycle controller. The actual decisions live in
sibling modules — read those for the *what* and *why*:

    dispatch.py    persona priority list (ACTIVE_SPRINT_DECISIONS,
                   NO_SPRINT_DECISIONS) — replaces the legacy 220-line
                   determine_persona cascade
    auto_merge.py  per-cycle sweep that merges every Reviewed+approved+pr
                   feature regardless of sprint membership
    reconcile.py   single per-product entry point sequencing
                   reconcile_merged_prs + reconcile_in_flight_prs
    supervisor.py  rule-based detectors (false-success, kill-recovery,
                   dirty-PR, merge-stall, orphan-Approved, rapid-flap)
    docker_runner.py  session lifecycle, agent-contract validation,
                      _apply_session_entry, post-coder pipeline
    INVARIANTS.md  the behavioral contract every change must preserve

Conventions specific to this subsystem:

  - Sync HTTP only. The orchestrator uses synchronous httpx; async is
    reserved for the website. A cycle is a single thread of control with
    blocking calls — adding asyncio here breaks the lock-and-FSM model.

  - last_run_at advances on *every cycle visit*, not only on session
    launch (INVARIANTS.md II.2). A product that resolves to "no actionable
    work" still rotates in the round-robin so it cannot starve others.
    The post-success bump from launch_session on Docker exit code 0 is
    an additional independent write.

  - Threads: the heartbeat refresher (15s lock TTL keep-alive), the
    log-streaming tail, and the live-poll on session_result.json are all
    daemon threads owned by this loop. They communicate back via
    threading.Event flags; the main loop checks _hb_lock_stolen at the
    top of each cycle and exits cleanly if another poller has taken over.
"""

import os
from pathlib import Path

# Auto-load .env from repo root - MUST happen before any other imports
# because docker_runner.py reads env vars at module level.
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()

import time
import logging
import socket
import subprocess
import threading
from datetime import datetime, timezone, date, timedelta

import httpx

from orchestrator.setup_product import discover_and_populate
from orchestrator.docker_runner import run_claude_in_docker
from orchestrator.github_client import count_open_prs
from orchestrator.reconcile import reconcile_product
from orchestrator.heartbeat import check_stale_sessions
from orchestrator.alerts import send_alert
from orchestrator.greenfield_scaffold import scaffold_greenfield

PM_API_URL    = os.environ["PM_API_URL"]
SSH_DIR       = Path(os.environ.get("SSH_DIR", "C:/Users/digvi/.ssh"))

# Env-var defaults - overridden by DB system config each cycle (see _load_runtime_cfg)
_ENV_DEFAULTS = {
    "poll_interval":               int(os.environ.get("POLL_INTERVAL",            "60")),
    "auth_check_timeout":          int(os.environ.get("AUTH_CHECK_TIMEOUT",       "30")),
    "session_timeout_minutes":     int(os.environ.get("SESSION_TIMEOUT_MINUTES",  "90")),
    "stale_threshold_minutes":     int(os.environ.get("STALE_THRESHOLD_MINUTES",  "45")),
    "max_features_per_run":        int(os.environ.get("MAX_FEATURES_PER_SPRINT",  "5")),
    "brownfield_file_threshold":   int(os.environ.get("BROWNFIELD_FILE_THRESHOLD","10")),
}

# Runtime config - refreshed from DB at the start of each cycle
_cfg: dict = dict(_ENV_DEFAULTS)


def _load_runtime_cfg():
    """Fetch system config from PM API and overlay onto env defaults."""
    global _cfg
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            data = resp.json()
        merged = dict(_ENV_DEFAULTS)
        for key in _ENV_DEFAULTS:
            if data.get(key) is not None:
                merged[key] = data[key]
        _cfg = merged
    except Exception as e:
        log.warning(f"Could not load runtime config from DB - using env defaults: {e}")

import sys as _sys
import io as _io
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
from orchestrator.log_context import build_formatter, log_scope  # noqa: F401 (log_scope re-exported)

_stdout_utf8 = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True) if hasattr(_sys.stdout, "buffer") else _sys.stdout
# Formatter is selected by LOG_FORMAT env var (text|json). Both formats include
# the active log_scope() fields (product_id, session_uid, persona) automatically.
_log_fmt = build_formatter()
_file_handler = _RotatingFileHandler(
    "orchestrator/poller.log",
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=5,               # keep poller.log + 5 rotated copies
    encoding="utf-8",
)
_file_handler.setFormatter(_log_fmt)
_stream_handler = logging.StreamHandler(stream=_stdout_utf8)
_stream_handler.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
log = logging.getLogger("poller")

# Track sessions launched today per product: {product_id: count}
# Mutated from main thread + heartbeat/live-poll threads - guarded by _daily_counts_lock.
_daily_session_counts: dict[int, int] = {}
_daily_session_date: date | None = None
_daily_counts_lock = threading.Lock()


def _reset_daily_counts_if_new_day():
    """Caller must hold _daily_counts_lock."""
    global _daily_session_counts, _daily_session_date
    today = datetime.now(timezone.utc).date()
    if _daily_session_date != today:
        _daily_session_counts = {}
        _daily_session_date = today


def claude_auth_healthy() -> bool:
    """
    Real auth check - makes an actual API call.
    claude --version always returns 0 even when logged out. Don't use it.
    A budget-exceeded error means the API was reached and auth is valid.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "ping", "--max-budget-usd", "0.001"],
            timeout=_cfg["auth_check_timeout"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        # Budget exceeded = API reached = auth is healthy
        combined = (result.stdout + result.stderr).lower()
        if "budget" in combined or "cost" in combined:
            return True
        # "not logged in" / "unauthorized" / "oauth" = truly unhealthy
        return False
    except subprocess.TimeoutExpired:
        log.warning("Auth check timed out")
        return False
    except FileNotFoundError:
        log.error("'claude' CLI not found in PATH")
        return False


def get_system_config() -> dict:
    """Fetch system config (github_pat, github_org, etc.) from PM API."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as e:
        log.error(f"get_system_config failed: {e}")
        return {}


# Phase 4b: round-robin and retro/reviewer selection helpers moved to
# orchestrator/cycle/selection.py. Re-exported below alongside is_quiet_hours
# and the _POST_SPRINT_PERSONAS cadence list.
from orchestrator.cycle.selection import (
    get_next_product,
    get_next_reviewer_product,
    get_next_retro_product,
    is_quiet_hours,
    _POST_SPRINT_PERSONAS,
)


def _clear_sprint_context(working_dir: str) -> None:
    """
    Clear workspace artifacts between sprints so the next sprint starts fresh.
    Removes session_result.json, features.md, and session_summary.md.
    Does NOT touch git history, docs/, or committed files.
    """
    wd = Path(working_dir)
    for artifact in ("session_result.json", "features.md", "session_summary.md"):
        p = wd / artifact
        if p.exists():
            try:
                p.unlink()
                log.info(f"[sprint-clear] Deleted {artifact} from {working_dir}")
            except Exception as e:
                log.warning(f"[sprint-clear] Could not delete {artifact}: {e}")


def reset_stuck_features():
    """Reset features stuck in Implementing for >2h back to Approved."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/features/reset_stuck")
        resp.raise_for_status()
        count = resp.json().get("reset_count", 0)
        if count:
            log.info(f"Reset {count} stuck feature(s) from Implementing -> Approved")
    except httpx.HTTPError as e:
        log.error(f"reset_stuck_features failed: {e}")


def is_daily_cap_reached(product: dict) -> bool:
    """Return True if this product has hit its daily session cap."""
    cap = product.get("daily_session_cap")
    if not cap:
        return False
    with _daily_counts_lock:
        _reset_daily_counts_if_new_day()
        return _daily_session_counts.get(product["id"], 0) >= cap


# Phase 4 of OrchestratorRefactor: in-memory persona-loop detection moved
# into orchestrator/cycle/loop_detector.py. Re-exported here so main() and
# tests still see _LoopDetector / _loop_detector / _heal_loop on poller.
from orchestrator.cycle.loop_detector import (
    _LoopDetector,
    _loop_detector,
    _heal_loop,
)


def _post_sprint_persona_due(product: dict, last_completed_sprint: dict | None) -> str | None:
    """
    Return the next post-sprint persona that hasn't run since the last sprint completed.
    Compares each persona's last_{persona}_at timestamp against sprint.completed_at.
    Returns None if no sprint has ever completed (planner handles cold-start features).
    """
    if not last_completed_sprint or not last_completed_sprint.get("completed_at"):
        return None
    try:
        sprint_done_at = datetime.fromisoformat(last_completed_sprint["completed_at"])
    except (ValueError, TypeError):
        return None
    config = product.get("config") or {}
    for persona in _POST_SPRINT_PERSONAS:
        last_run_str = config.get(f"last_{persona}_at")
        if not last_run_str:
            return persona  # Never run — due
        try:
            last_run = datetime.fromisoformat(last_run_str)
            if last_run < sprint_done_at:
                return persona  # Ran before this sprint completed — due again
        except (ValueError, TypeError):
            return persona
    return None


def _auto_create_sprint_for_unsprinted(product: dict, client: httpx.Client) -> bool:
    """
    If there are Approved unsprinted features and no active sprint, create a new sprint
    in the last existing phase (or a new phase if none exist), assign the features, and
    activate it. Returns True if a sprint was created.
    """
    pid = product["id"]

    # Fetch approved unsprinted features
    feat_resp = client.get(f"/api/products/{pid}/features")
    if feat_resp.status_code != 200:
        return False
    all_features = feat_resp.json() if isinstance(feat_resp.json(), list) else []
    unsprinted_approved = [
        f for f in all_features
        if f.get("status") == "Approved" and not f.get("sprint_id")
    ]
    if not unsprinted_approved:
        return False

    # Find last phase, or create one
    phases_resp = client.get(f"/api/products/{pid}/phases")
    phases = phases_resp.json() if phases_resp.status_code == 200 and isinstance(phases_resp.json(), list) else []

    if phases:
        last_phase = max(phases, key=lambda p: (p.get("order", 0), p["id"]))
        phase_id = last_phase["id"]
        log.info(f"[auto-sprint] Using existing phase '{last_phase['name']}' (id={phase_id})")
    else:
        phase_resp = client.post("/api/phases", json={
            "product_id": pid,
            "name": "Phase 1",
            "order": 1,
            "status": "active",
        })
        if phase_resp.status_code not in (200, 201):
            log.warning(f"[auto-sprint] Failed to create phase: {phase_resp.status_code}")
            return False
        phase_id = phase_resp.json()["id"]
        log.info(f"[auto-sprint] Created new Phase 1 (id={phase_id})")

    # Determine sprint number within this phase
    sprints_resp = client.get(f"/api/products/{pid}/sprints")
    sprints = sprints_resp.json() if sprints_resp.status_code == 200 and isinstance(sprints_resp.json(), list) else []
    phase_sprints = [s for s in sprints if s.get("phase_id") == phase_id]
    sprint_num = len(phase_sprints) + 1
    sprint_name = f"Sprint {sprint_num}"

    from datetime import date, timedelta
    today = date.today()
    sprint_resp = client.post("/api/sprints", json={
        "product_id": pid,
        "phase_id": phase_id,
        "name": sprint_name,
        "goal": f"Deliver {len(unsprinted_approved)} approved feature(s)",
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(weeks=2)).isoformat(),
        "status": "active",
    })
    if sprint_resp.status_code not in (200, 201):
        log.warning(f"[auto-sprint] Failed to create sprint: {sprint_resp.status_code} {sprint_resp.text[:100]}")
        return False

    sprint_id = sprint_resp.json()["id"]
    log.info(f"[auto-sprint] Created {sprint_name} (id={sprint_id}) — assigning {len(unsprinted_approved)} features")

    # Assign features to the new sprint
    for f in unsprinted_approved:
        try:
            client.patch(f"/api/features/{f['id']}", json={"sprint_id": sprint_id})
        except Exception as e:
            log.warning(f"[auto-sprint] Could not assign feature #{f['id']}: {e}")

    log.info(f"[auto-sprint] Sprint '{sprint_name}' activated with {len(unsprinted_approved)} features")
    return True


def determine_persona(product: dict) -> str | None:
    """Decide which persona should run for this product, or None if nothing.

    Phase 2 of PollerRevamp: thin wrapper delegating to orchestrator.dispatch —
    the real logic is a priority list of small decision functions, each
    testable in isolation. Behavior is preserved against the legacy 220-line
    cascade except that the inline auto-merge path was removed because Phase 1's
    per-cycle sweep already covers Reviewed+pr_number features (INVARIANTS.md
    VII.1).
    """
    from orchestrator.dispatch import determine_persona as _dispatch
    return _dispatch(product)


def deliver_pm_messages(product: dict):
    """
    Write pending PM messages from product.config to pm_message.md in the workspace.
    Clears them from DB after writing.
    """
    config = product.get("config") or {}
    messages = config.get("pm_messages", [])
    if not messages:
        return

    working_dir = Path(product["working_dir"])
    if not working_dir.exists():
        return

    msg_file = working_dir / "pm_message.md"
    lines = [f"# PM Messages\n\n"]
    for m in messages:
        lines.append(f"- [{m.get('sent_at', '')}] {m.get('text', '')}\n")
    msg_file.write_text("".join(lines), encoding="utf-8")
    log.info(f"Wrote {len(messages)} PM message(s) to {msg_file}")

    # Clear from DB
    new_config = {**config, "pm_messages": []}
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            client.patch(f"/api/products/{product['id']}", json={"config": new_config})
    except Exception as e:
        log.warning(f"Failed to clear pm_messages from DB: {e}")


def clear_run_now(product_id: int):
    """Clear the run_now flag after picking the product."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            client.patch(f"/api/products/{product_id}", json={"run_now": False})
    except Exception as e:
        log.warning(f"Failed to clear run_now: {e}")


def clear_run_trainer_now(product_id: int):
    """Clear the run_trainer_now flag after queuing the trainer session."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            client.patch(f"/api/products/{product_id}", json={"run_trainer_now": False})
    except Exception as e:
        log.warning(f"Failed to clear run_trainer_now: {e}")


_LOCK_PID  = os.getpid()
_LOCK_HOST = socket.gethostname()

_PID_FILE = Path(__file__).parent / "poller.pid"


def _check_single_instance() -> bool:
    """
    Return True if we're the only running instance.
    Writes our PID to poller.pid; quits early if another process with the
    stored PID is still alive (stale files from crashed pollers are ignored).
    """
    if _PID_FILE.exists():
        try:
            existing_pid = int(_PID_FILE.read_text().strip())
            if existing_pid != _LOCK_PID:
                try:
                    os.kill(existing_pid, 0)  # signal 0 = check existence only
                    log.error(
                        f"Another poller is already running (pid={existing_pid}). Exiting."
                    )
                    sys.exit(42)  # sentinel: wrapper must NOT restart on this code
                except OSError:
                    pass  # stale PID file — previous poller died without cleanup
        except (ValueError, OSError):
            pass  # corrupt or unreadable file — proceed
    _PID_FILE.write_text(str(_LOCK_PID))
    return True


def _cleanup_pid_file():
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(_LOCK_PID):
            _PID_FILE.unlink()
    except Exception:
        pass
# Phase 4 of OrchestratorRefactor (option A — partial extraction):
# _acquire_db_lock and _heartbeat_loop moved to orchestrator/cycle/locks.py
# along with their immutable per-process state (PID, hostname, TTL constants).
# _release_db_lock stays here because it reads the mutable _hb_stop global
# that main() rebinds at runtime — keeping both in the same namespace
# preserves the behavior of the `global _hb_stop` rebinding pattern.
from orchestrator.cycle.locks import (
    _HEARTBEAT_INTERVAL,
    _LOCK_TTL,
    _LOCK_PID,
    _LOCK_HOST,
    _hb_lock_stolen,
    _acquire_db_lock,
    _heartbeat_loop,
)

_hb_stop: threading.Event | None = None


def _release_db_lock():
    """Release the lock. Called via atexit - best-effort, never raises."""
    global _hb_stop
    if _hb_stop is not None:
        _hb_stop.set()
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.delete("/api/poller/lock", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
        log.info("Poller lock released")
    except Exception:
        pass


def _close_orphaned_sessions():
    """
    On startup: close any sessions that have no ended_at but whose containers
    are no longer running. Happens when the poller was killed mid-session.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/sessions/active")
            orphans = resp.json() if isinstance(resp.json(), list) else []
        if not orphans:
            return
        # Check which containers are actually running
        try:
            docker_result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True, text=True, timeout=15,
            )
            running_set = set(docker_result.stdout.strip().splitlines())
        except Exception as docker_err:
            # If docker ps fails, close any orphan older than SESSION_TIMEOUT_MINUTES
            # to avoid permanently blocking those products.
            log.warning(f"docker ps failed during orphan check: {docker_err} - closing timed-out orphans only")
            running_set = None

        now = datetime.now(timezone.utc)
        timeout_minutes = _cfg.get("session_timeout_minutes", 90)
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for s in orphans:
                cid = s.get("container_id") or ""
                if running_set is not None:
                    should_close = cid not in running_set
                else:
                    # Fallback: close if session is older than the timeout threshold
                    started = s.get("started_at", "")
                    try:
                        age_minutes = int((now - datetime.fromisoformat(started)).total_seconds() / 60)
                        should_close = age_minutes > timeout_minutes
                    except Exception:
                        should_close = True  # unknown age - close it
                if should_close:
                    client.patch(f"/api/sessions/{s['id']}", json={
                        "ended_at":  now.isoformat(),
                        "exit_code": 1,
                    })
                    log.info(f"Closed orphaned session {s['id']} (container={cid or 'none'}) on startup")
    except Exception as e:
        log.warning(f"Could not close orphaned sessions: {e}")


def _startup_sync_features():
    """
    Disabled: DB is the single source of truth for feature status.
    features.md sync was causing status downgrades (e.g. Approved -> Pending)
    when the file was stale. Kept as a no-op in case manual re-enable is needed.
    """
    log.info("Startup features.md sync disabled - DB is source of truth")


def _post_session_comments(product: dict, persona: str, session_uid: str, features_updated: list[int]):
    """
    After a session completes, post the session summary as a comment on each
    feature that was updated. Author = persona name (e.g. 'coder', 'reviewer').
    Fault-tolerant - never raises.
    """
    if not features_updated:
        return
    working_dir = product.get("working_dir", "")
    body = f"{persona} session {session_uid} completed."
    if working_dir:
        summary_path = Path(working_dir) / "session_summary.md"
        try:
            if summary_path.exists():
                raw = summary_path.read_text(encoding="utf-8", errors="replace")
                body = raw[:500].strip() or body
        except Exception:
            pass
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for fid in features_updated:
                try:
                    client.post(f"/api/features/{fid}/comments", json={"author": persona, "body": body})
                except Exception:
                    pass
    except Exception as e:
        log.warning(f"_post_session_comments: could not post comments: {e}")


def _check_due_date_alerts():
    """
    Post a comment and send a Slack alert for features past their due_date.
    Called once per poller cycle. Fault-tolerant - never raises.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/features/overdue")
            if resp.status_code != 200:
                return
            overdue = resp.json()
            for f in overdue:
                fid  = f["id"]
                name = f.get("name", f"Feature #{fid}")
                due  = f.get("due_date", "?")
                stat = f.get("status", "?")
                msg  = f"⚠️ Overdue: '{name}' was due {due} - current status: {stat}"
                try:
                    client.post(f"/api/features/{fid}/comments", json={"author": "poller", "body": msg})
                except Exception:
                    pass
                send_alert("warning", msg)
    except Exception as e:
        log.warning(f"_check_due_date_alerts: {e}")


def _check_sprint_dod_all_products(products: list[dict]):
    """
    For every product with an active sprint, call the DoD endpoint.
    If all gates pass (features done, no open PRs, QA + security signed off),
    trigger sprint auto-completion via the sign-off endpoint (retro_done=False means
    the poller just fires the structural gates; agent sign-offs set qa/security).
    Fault-tolerant - never raises.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            for product in products:
                pid = product.get("id")
                if not pid:
                    continue
                # Get active sprint
                resp = client.get(f"/api/products/{pid}/sprints/active")
                if resp.status_code != 200 or not resp.json():
                    continue
                sprint = resp.json()
                sid = sprint.get("id")
                if not sid or sprint.get("status") == "completed":
                    continue

                # Check DoD - auto-completes if all gates pass
                check_resp = client.post(f"/api/sprints/{sid}/check-dod")
                if check_resp.status_code == 200:
                    result = check_resp.json()
                    if result.get("action") == "auto_completed":
                        log.info(
                            f"[dod] Sprint {sid} ({product.get('name')}) auto-completed "
                            f"- all gates passed"
                        )
    except Exception as e:
        log.warning(f"_check_sprint_dod_all_products: {e}")


def _install_crash_handler():
    """Ensure un-caught exceptions land in poller.log with a full traceback.

    The stdlib RotatingFileHandler flushes lazily, so a process that dies mid-
    startup can lose the exception message. Here we register a sys.excepthook
    that explicitly logs + flushes every root handler before the interpreter
    exits, then delegates to the default hook so the traceback still hits
    stderr as usual.
    """
    _prev_hook = _sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            log.critical("Poller crashed - un-caught exception",
                         exc_info=(exc_type, exc, tb))
            for h in list(logging.getLogger().handlers) + list(log.handlers):
                try:
                    h.flush()
                except Exception:
                    pass
        finally:
            _prev_hook(exc_type, exc, tb)

    _sys.excepthook = _hook


def main():
    _install_crash_handler()

    if not _check_single_instance():
        return

    import atexit
    atexit.register(_cleanup_pid_file)

    if not _acquire_db_lock():
        _cleanup_pid_file()
        return

    atexit.register(_release_db_lock)

    # Start heartbeat background thread
    global _hb_stop
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(_hb_stop,),
        daemon=True, name="poller-heartbeat"
    )
    _hb_thread.start()

    # Prometheus metrics endpoint + optional dead-man's-switch heartbeat.
    # Both are no-ops when their respective env vars (PROMETHEUS_PORT,
    # HEARTBEAT_URL) are unset, so this is safe to always invoke.
    from orchestrator.metrics import start_prometheus_exporter, start_heartbeat
    start_prometheus_exporter()
    start_heartbeat()

    log.info("ProductFactory Poller starting...")
    # Belt-and-braces: wrap the two startup helpers in explicit logging so that
    # if one raises we at least see which one, even if excepthook doesn't fire
    # (e.g. under an embedded interpreter or a subthread).
    try:
        _close_orphaned_sessions()
    except Exception:
        log.exception("startup: _close_orphaned_sessions failed")
        for h in list(logging.getLogger().handlers) + list(log.handlers):
            try: h.flush()
            except Exception: pass
        raise
    try:
        _startup_sync_features()
    except Exception:
        log.exception("startup: _startup_sync_features failed")
        for h in list(logging.getLogger().handlers) + list(log.handlers):
            try: h.flush()
            except Exception: pass
        raise

    while True:
        try:
            # Abort if the heartbeat thread detected our lock was stolen
            if _hb_lock_stolen.is_set():
                log.critical("Lock stolen detected - exiting poller to prevent duplicate runs")
                break

            # Refresh runtime config from DB at start of each cycle
            _load_runtime_cfg()

            # ① Auth check
            if not claude_auth_healthy():
                send_alert("critical", "Claude OAuth session expired - re-login needed")
                log.warning("Auth unhealthy - skipping cycle")
                time.sleep(_cfg["poll_interval"])
                continue

            # Fetch all products for this cycle - retry on transient PM API failures
            products = None
            for _attempt in range(3):
                try:
                    with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                        products = client.get("/api/products").json()
                    break
                except Exception as _fetch_err:
                    if _attempt < 2:
                        log.warning(f"Failed to fetch products (attempt {_attempt + 1}/3): {_fetch_err} - retrying")
                        time.sleep(2 ** _attempt)
                    else:
                        log.error(f"Could not fetch products after 3 attempts: {_fetch_err} - skipping cycle")
                        send_alert("warning", f"Poller: PM API unreachable after 3 retries: {_fetch_err}")
            if products is None:
                time.sleep(_cfg["poll_interval"])
                continue

            # ② Scaffold greenfield_pending products
            greenfield_pending = [p for p in products if p["status"] == "greenfield_pending"]
            if greenfield_pending:
                system_config = get_system_config()
                for product in greenfield_pending:
                    log.info(f"Scaffolding greenfield: {product.get('name', product['id'])}")
                    scaffold_greenfield(product, system_config, PM_API_URL, SSH_DIR)

            # ③ Discover newly-registered products
            for product in products:
                if product["status"] == "registered":
                    log.info(f"Discovering product: {product['working_dir']}")
                    discover_and_populate(product)

            # ④ Heartbeat - kill stale containers
            check_stale_sessions(products)

            # ⑤ Reset stuck features
            reset_stuck_features()

            # ⑤b Due-date alerts
            _check_due_date_alerts()

            # ⑤c Sprint DoD auto-check - complete any sprints where all gates pass
            _check_sprint_dod_all_products(products)

            # ⑥ Deliver PM messages
            for product in products:
                if (product.get("config") or {}).get("pm_messages"):
                    deliver_pm_messages(product)

            # ⑥b PR reconciliation for all products — runs every cycle so Implementing+open-PR
            # features are advanced to Reviewing before the reviewer-first check below.
            # Phase 4 of PollerRevamp: single per-product entry point in
            # orchestrator/reconcile.py — see INVARIANTS.md V.3-V.4 for contract.
            #
            # Also runs the open-PR-count invariant (Phase 6.5): in sprint-PR
            # mode there should be exactly one open PR per product. >1 alerts
            # the operator once per product per run, but no longer gates work
            # (the old MAX_OPEN_PRS gate was removed).
            from orchestrator.sprint_pr import check_open_pr_invariant
            for _p in products:
                reconcile_product(_p)
                if _p.get("github_repo"):
                    try:
                        check_open_pr_invariant(_p, count_open_prs(_p), alerter=send_alert)
                    except Exception:
                        log.exception(f"open-PR invariant check failed for {_p.get('name', '?')}")

            # ⑥c Auto-merge sweep — Phase 1 of PollerRevamp.
            # Merges every Reviewed+approved+pr_number feature across ALL ready
            # products, regardless of sprint membership. Independent of persona
            # selection, so Reviewed features in non-active sprints (the
            # webcalculator class of deadlock) get merged on schedule.
            # See orchestrator/INVARIANTS.md VII.1 for the contract.
            try:
                from orchestrator.auto_merge import sweep_all as _auto_merge_sweep
                _auto_merge_sweep(products, get_system_config())
            except Exception:
                log.exception("auto-merge sweep failed (non-fatal)")

            # ⑦a Reviewer-first: any product with a Reviewing feature + PR takes priority
            reviewer_product, reviewer_persona = get_next_reviewer_product(products)
            retro_product = get_next_retro_product(products)
            if any(p.get("run_trainer_now") for p in products if p["status"] == "ready"):
                # ⑦a2 On-demand trainer: a PM requested a showcase video
                product = next(p for p in products if p["status"] == "ready" and p.get("run_trainer_now"))
                persona = "product_trainer"
                clear_run_trainer_now(product["id"])
                log.info(f"On-demand Product Trainer for: {product['name']} (id={product['id']})")
            elif reviewer_product:
                product = reviewer_product
                persona = "reviewer"
                log.info(f"Reviewer-first: {product['name']} has Reviewing features with PRs")
            elif retro_product:
                product = retro_product
                persona = "retrospective"
                log.info(f"Retro-first: {product['name']} has sprint needing retrospective")
            else:
                # ⑦b Normal round-robin for designer/coder work
                product = get_next_product(products)
                if not product:
                    log.debug("No products ready - sleeping")
                    time.sleep(_cfg["poll_interval"])
                    continue

                log.info(f"Selected product: {product['name']} (id={product['id']})")

                # ⑦c Determine whether to run designer or coder
                persona = determine_persona(product)
                if persona is None:
                    log.info(f"No actionable work for {product['name']} - sprint in progress, waiting")
                    time.sleep(_cfg["poll_interval"])
                    continue
                log.info(f"Persona: {persona}")

            # ⑦d Persona history (debug only). Real loop prevention lives in the
            # reconciler's fix_attempts → Blocked policy: when a feature has been
            # bounced back from a later state too many times, it is moved to
            # Blocked rather than being retried forever. Skipping cycles based on
            # persona-pattern matching produced too many false positives.
            _loop_detector.record(product["id"], persona)
            loop_pattern = _loop_detector.detect_loop(product["id"])
            if loop_pattern:
                if _loop_detector.should_alert(product["id"]):
                    send_alert("warning", f"Loop detected: {loop_pattern} - attempting self-heal", product["name"])
                if _heal_loop(product, loop_pattern):
                    _loop_detector.clear(product["id"])
                    continue  # re-run determine_persona with healed state
                # Heal failed — log and proceed; do not skip the cycle. The
                # reconciler/Blocked policy will surface real stuck features.
                log.warning(f"[loop-detect] Heal not applied for {product['name']} - proceeding")

            # Clear run_now flag if set
            if product.get("run_now"):
                clear_run_now(product["id"])

            # ⑧ Quiet hours gate (skip for reviewer - reviews are time-sensitive)
            if persona != "reviewer" and is_quiet_hours(product):
                h_start = product.get("quiet_hours_start")
                h_end   = product.get("quiet_hours_end")
                log.info(f"Quiet hours ({h_start}-{h_end} UTC) - skipping {product['name']}")
                time.sleep(_cfg["poll_interval"])
                continue

            # ⑨ Daily session cap gate (reviewer doesn't count against cap)
            if persona != "reviewer" and is_daily_cap_reached(product):
                log.info(f"Daily cap reached for {product['name']} - skipping")
                time.sleep(_cfg["poll_interval"])
                continue

            # ⑩ PR reconciliation already ran for all ready products at step ⑥b
            # (before reviewer-first selection). Calling it a second time here was
            # redundant and created races with the live-poll thread of any session
            # that was about to start.

            # ⑪ The MAX_OPEN_PRS coder gate is gone (Phase 6.5). With sprint-PR
            # mode there's exactly one open PR per product (the sprint PR);
            # gating on its existence would block every coder run forever.
            # Anomaly detection (>1 open PR) lives in the per-cycle invariant
            # check in step ⑥b instead, which alerts but does not pause.

            # ⑫ Run Claude session - wrap in a log scope so every record emitted
            # by the docker_runner, the live-poll thread, and the log-stream thread
            # carries (product_id, session_uid, persona) fields automatically.
            log.info(f"Launching {persona} session for: {product['name']}")
            _session_start = datetime.now(timezone.utc)
            session_uid = f"{product['id']}-{int(_session_start.timestamp())}"
            with log_scope(product_id=product["id"], persona=persona, session_uid=session_uid):
                exit_code = run_claude_in_docker(product, persona=persona)
            log.info(f"Session ended - exit_code={exit_code} persona={persona}")

            # Prometheus metrics - no-op stub when prometheus_client isn't installed.
            try:
                from orchestrator.metrics import record_session
                _duration = (datetime.now(timezone.utc) - _session_start).total_seconds()
                _outcome = "success" if exit_code == 0 else "failure"
                record_session(persona=persona or "unknown",
                               product=product.get("name", "unknown"),
                               outcome=_outcome,
                               duration_seconds=_duration)
            except Exception as _me:
                log.debug(f"metric emission failed: {_me}")

            # Clear loop history after a successful coder session (progress was made)
            if exit_code == 0 and persona == "coder":
                _loop_detector.clear(product["id"])

            # exit_code=99 means "already running - skipped". Not an error; don't count or alert.
            if exit_code == 99:
                log.info(f"Session skipped (already running) for {product['name']} - will retry next cycle")
                time.sleep(30)  # Short sleep so we re-check soon
                continue

            # Post session summary as comments on features updated during this session
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as _cc:
                    _feats = _cc.get(f"/api/products/{product['id']}/features").json()
                    _updated_ids = [
                        f["id"] for f in _feats
                        if f.get("updated_at") and f["updated_at"] >= _session_start.isoformat()
                    ]
            except Exception:
                _updated_ids = []
            _post_session_comments(product, persona, session_uid, _updated_ids)

            # Detect zero-progress sessions: agent exited cleanly but no feature
            # state advanced. Without this guard, the round-robin keeps picking
            # the same product and the same persona, masking session-contract
            # bugs (e.g. reviewer with empty assignment, coder with no work).
            zero_progress = exit_code == 0 and not _updated_ids
            if zero_progress:
                log.warning(
                    f"[zero-progress] {product['name']} {persona} session exited code=0 "
                    f"but no features were updated — not advancing last_run_at"
                )
                send_alert(
                    "warning",
                    f"{product['name']}: {persona} session made no progress (0 features updated)",
                )

            # Track daily count (reviewers exempt; zero-progress sessions also exempt
            # so the cap doesn't burn through on agents that aren't doing real work).
            if persona != "reviewer" and not zero_progress:
                with _daily_counts_lock:
                    _reset_daily_counts_if_new_day()
                    _daily_session_counts[product["id"]] = _daily_session_counts.get(product["id"], 0) + 1

            # ⑬ Update last_run_at ONLY on clean exit AND if work was done.
            # Also stamp last_{persona}_at for post-sprint personas that don't
            # self-report (recommender is poller-launched, not agent-written).
            if exit_code == 0 and not zero_progress:
                now_iso = datetime.now(timezone.utc).isoformat()
                patches: dict = {"last_run_at": now_iso}
                if persona in _POST_SPRINT_PERSONAS:
                    try:
                        with httpx.Client(base_url=PM_API_URL, timeout=10) as _cfg_client:
                            _cfg_resp = _cfg_client.get(f"/api/products/{product['id']}")
                            current_cfg = dict((_cfg_resp.json().get("config") or {}) if _cfg_resp.status_code == 200 else {})
                        current_cfg[f"last_{persona}_at"] = now_iso
                        patches["config"] = current_cfg
                    except Exception as _ce:
                        log.warning(f"Could not stamp last_{persona}_at: {_ce}")
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    client.patch(f"/api/products/{product['id']}", json=patches)
            elif exit_code != 0:
                send_alert("warning", f"{product['name']}: {persona} session exited with code {exit_code}")

        except KeyboardInterrupt:
            log.info("Poller stopped by user")
            if _hb_stop is not None:
                _hb_stop.set()
            break
        except Exception as e:
            log.exception(f"Unexpected error in poll loop: {e}")
            send_alert("error", f"Poller loop error: {e}")

        time.sleep(5)


if __name__ == "__main__":
    main()
