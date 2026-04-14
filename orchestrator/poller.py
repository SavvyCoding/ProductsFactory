"""
ProductFactory Poller — main loop (Windows localhost).

Runs as a Windows Service via NSSM.
Poll interval: 60s when idle, 5s between products.

Multi-agent persona routing:
  - Designer: writes design docs for Approved features (before coding)
  - Coder:    implements Designed (or skip_design Approved) features, opens PRs, sets Reviewing
  - Reviewer: reviews open PRs, approves or requests changes, sets Reviewed

Flow per cycle:
  1. Auth health check (real API call, not --version)
  2. Scaffold any greenfield_pending products
  3. Discover any newly-registered folders
  4. Heartbeat — kill stale containers (progress.md not pushed in >45m)
  5. Reset stuck features (Implementing/Designing/Reviewing > 2h)
  6. Deliver pending PM messages to workspace files
  7a. Reviewer-first: check globally for Reviewing features with PRs
  7b. If none: pick next product via round-robin (has Approved/Designed features)
  7c. Determine persona: designer (Approved+no skip_design) or coder
  8. Quiet hours gate (reviewers skip this gate — reviews are time-sensitive)
  9. Daily session cap gate (reviewers exempt)
  10. PR count gate (coder only — designer/reviewer don't open new PRs)
  11. GitHub PR reconciliation (sync merged PRs → DB → Pushed)
  12. Run Claude in Docker with persona (blocks until session ends)
  13. Update last_run_at ONLY on clean exit (exit code 0)
"""

import os
from pathlib import Path

# Auto-load .env from repo root — MUST happen before any other imports
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
from orchestrator.github_client import count_open_prs, reconcile_merged_prs
from orchestrator.heartbeat import check_stale_sessions
from orchestrator.alerts import send_alert
from orchestrator.greenfield_scaffold import scaffold_greenfield

PM_API_URL    = os.environ["PM_API_URL"]
SSH_DIR       = Path(os.environ.get("SSH_DIR", "C:/Users/digvi/.ssh"))

# Env-var defaults — overridden by DB system config each cycle (see _load_runtime_cfg)
_ENV_DEFAULTS = {
    "poll_interval":               int(os.environ.get("POLL_INTERVAL",            "60")),
    "auth_check_timeout":          int(os.environ.get("AUTH_CHECK_TIMEOUT",       "30")),
    "max_open_prs":                int(os.environ.get("MAX_OPEN_PRS",             "3")),
    "pr_gate_sleep":               int(os.environ.get("PR_GATE_SLEEP",            "300")),
    "session_timeout_minutes":     int(os.environ.get("SESSION_TIMEOUT_MINUTES",  "90")),
    "stale_threshold_minutes":     int(os.environ.get("STALE_THRESHOLD_MINUTES",  "45")),
    "max_features_per_run":        int(os.environ.get("MAX_FEATURES_PER_RUN",     "1")),
    "brownfield_file_threshold":   int(os.environ.get("BROWNFIELD_FILE_THRESHOLD","10")),
}

# Runtime config — refreshed from DB at the start of each cycle
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
        log.warning(f"Could not load runtime config from DB — using env defaults: {e}")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("orchestrator/poller.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("poller")

# Track sessions launched today per product: {product_id: count}
_daily_session_counts: dict[int, int] = {}
_daily_session_date: date | None = None


def _reset_daily_counts_if_new_day():
    global _daily_session_counts, _daily_session_date
    today = datetime.now(timezone.utc).date()
    if _daily_session_date != today:
        _daily_session_counts = {}
        _daily_session_date = today


def claude_auth_healthy() -> bool:
    """
    Real auth check — makes an actual API call.
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


def get_next_product(products: list[dict]) -> dict | None:
    """
    Select next product to run:
    1. run_now=True products have priority (first one found)
    2. Otherwise: status=ready, has Approved features, round-robin by last_run_at
    """
    # Priority: run_now flag
    for p in products:
        if p.get("run_now") and p["status"] == "ready":
            return p

    # Normal round-robin via API
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/products/next")
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        log.error(f"get_next_product failed: {e}")
        return None


def reset_stuck_features():
    """Reset features stuck in Implementing for >2h back to Approved."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/features/reset_stuck")
        resp.raise_for_status()
        count = resp.json().get("reset_count", 0)
        if count:
            log.info(f"Reset {count} stuck feature(s) from Implementing → Approved")
    except httpx.HTTPError as e:
        log.error(f"reset_stuck_features failed: {e}")


def is_quiet_hours(product: dict) -> bool:
    """Return True if current UTC hour falls within the product's quiet window."""
    start = product.get("quiet_hours_start")
    end   = product.get("quiet_hours_end")
    if start is None or end is None:
        return False
    current_hour = datetime.now(timezone.utc).hour
    if start <= end:
        return start <= current_hour < end
    else:  # wraps midnight e.g. 22–6
        return current_hour >= start or current_hour < end


def is_daily_cap_reached(product: dict) -> bool:
    """Return True if this product has hit its daily session cap."""
    cap = product.get("daily_session_cap")
    if not cap:
        return False
    _reset_daily_counts_if_new_day()
    return _daily_session_counts.get(product["id"], 0) >= cap


def get_next_reviewer_product(products: list[dict]) -> tuple[dict | None, str | None]:
    """
    Check if any ready product has features in 'Reviewing' state with a PR number.
    Reviewer sessions take global priority over normal designer/coder scheduling.
    Returns (product, 'reviewer') or (None, None).
    """
    ready_ids = {p["id"] for p in products if p["status"] == "ready"}
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.get("/api/features/next-for-persona", params={"persona": "reviewer"})
        if resp.status_code == 200 and resp.json():
            feature = resp.json()
            pid = feature["product_id"]
            if pid in ready_ids:
                product = next((p for p in products if p["id"] == pid), None)
                return product, "reviewer"
    except httpx.HTTPError as e:
        log.error(f"get_next_reviewer_product failed: {e}")
    return None, None


# Maintenance persona schedule: persona → interval in days
_MAINTENANCE_SCHEDULE = [
    ("documenter",       3),
    ("analytics",        7),
    ("refactorer",       7),
    ("devops",          14),
]


def _maintenance_persona_due(product: dict) -> str | None:
    """Return the next maintenance persona that is due to run, or None."""
    config = product.get("config") or {}
    now = datetime.now(timezone.utc)
    for persona, interval_days in _MAINTENANCE_SCHEDULE:
        last_run_str = config.get(f"last_{persona}_at")
        if not last_run_str:
            return persona  # Never run before — schedule it
        try:
            last_run = datetime.fromisoformat(last_run_str)
            if (now - last_run) >= timedelta(days=interval_days):
                return persona
        except ValueError:
            return persona  # Malformed date — run it
    return None


def determine_persona(product: dict) -> str:
    """
    Decide which persona should run for this product (priority order):
    1. designer  — Approved features with skip_design=False
    2. coder     — Designed or skip_design Approved features
    3. documenter / analytics / refactorer / devops  — scheduled maintenance
    4. planner   — no actionable features; generate new ones
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            # Check for designer work
            resp = client.get(
                "/api/features/next-for-persona",
                params={"persona": "designer", "product_id": product["id"]}
            )
            if resp.status_code == 200 and resp.json():
                return "designer"
            # Check for coder work
            resp2 = client.get(
                "/api/features/next-for-persona",
                params={"persona": "coder", "product_id": product["id"]}
            )
            if resp2.status_code == 200 and resp2.json():
                return "coder"
    except httpx.HTTPError as e:
        log.error(f"determine_persona failed: {e}")
        return "coder"

    # No design/code work — check scheduled maintenance personas
    maintenance = _maintenance_persona_due(product)
    if maintenance:
        log.info(f"Maintenance persona due: {maintenance}")
        return maintenance

    # Nothing else — run planner to generate new feature ideas
    return "planner"


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


_LOCK_PID  = os.getpid()
_LOCK_HOST = socket.gethostname()
# These must stay in sync with the server's TTL logic in /api/poller/heartbeat.
# Do NOT make them env-configurable without also updating the server endpoint.
_HEARTBEAT_INTERVAL = 15   # seconds between heartbeat updates
_LOCK_TTL           = 30   # seconds — stale lock threshold (must match server)

_hb_stop: threading.Event | None = None


def _acquire_db_lock() -> bool:
    """
    Atomically acquire the poller distributed lock via PM API.
    Uses a single PostgreSQL UPDATE WHERE so two callers can never both succeed.
    Returns True on success, False if another live poller holds the lock.
    Falls back to True (allow start) if the API is unreachable — better to risk
    a duplicate than to prevent all pollers from ever starting.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/poller/lock", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
        if resp.status_code == 200:
            log.info(f"Poller lock acquired (pid={_LOCK_PID}, host={_LOCK_HOST})")
            return True
        if resp.status_code == 409:
            d = resp.json().get("detail", {})
            log.error(
                f"Another poller holds the lock — "
                f"pid={d.get('holder_pid')}, host={d.get('holder_host')}, "
                f"last_heartbeat={d.get('heartbeat_at')}. Exiting."
            )
            return False
        log.error(f"Unexpected response from lock endpoint: {resp.status_code} — allowing start")
        return True
    except Exception as e:
        log.warning(f"Could not acquire DB lock ({e}) — allowing start (API may be starting up)")
        return True


def _release_db_lock():
    """Release the lock. Called via atexit — best-effort, never raises."""
    global _hb_stop
    if _hb_stop is not None:
        _hb_stop.set()
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            client.delete("/api/poller/lock", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
        log.info("Poller lock released")
    except Exception:
        pass


def _heartbeat_loop(stop_event: threading.Event):
    """
    Background thread: refreshes the DB lock heartbeat every 15s.
    If the API returns 404, the lock was stolen (another poller took over) — log it.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL):
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
                resp = client.post("/api/poller/heartbeat", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
            if resp.status_code == 404:
                log.critical("Heartbeat 404 — lock was stolen or expired. A duplicate poller may start.")
            elif resp.status_code != 200:
                log.warning(f"Heartbeat unexpected status: {resp.status_code}")
        except Exception as e:
            log.warning(f"Heartbeat failed: {e}")


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
            log.warning(f"docker ps failed during orphan check: {docker_err} — closing timed-out orphans only")
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
                        should_close = True  # unknown age — close it
                if should_close:
                    client.patch(f"/api/sessions/{s['id']}", json={
                        "ended_at":  now.isoformat(),
                        "exit_code": 1,
                    })
                    log.info(f"Closed orphaned session {s['id']} (container={cid or 'none'}) on startup")
    except Exception as e:
        log.warning(f"Could not close orphaned sessions: {e}")


def main():
    if not _acquire_db_lock():
        return

    import atexit
    atexit.register(_release_db_lock)

    # Start heartbeat background thread
    global _hb_stop
    _hb_stop = threading.Event()
    _hb_thread = threading.Thread(
        target=_heartbeat_loop, args=(_hb_stop,),
        daemon=True, name="poller-heartbeat"
    )
    _hb_thread.start()

    log.info("ProductFactory Poller starting...")
    _close_orphaned_sessions()

    while True:
        try:
            # Refresh runtime config from DB at start of each cycle
            _load_runtime_cfg()

            # ① Auth check
            if not claude_auth_healthy():
                send_alert("critical", "Claude OAuth session expired — re-login needed")
                log.warning("Auth unhealthy — skipping cycle")
                time.sleep(_cfg["poll_interval"])
                continue

            # Fetch all products for this cycle — retry on transient PM API failures
            products = None
            for _attempt in range(3):
                try:
                    with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                        products = client.get("/api/products").json()
                    break
                except Exception as _fetch_err:
                    if _attempt < 2:
                        log.warning(f"Failed to fetch products (attempt {_attempt + 1}/3): {_fetch_err} — retrying")
                        time.sleep(2 ** _attempt)
                    else:
                        log.error(f"Could not fetch products after 3 attempts: {_fetch_err} — skipping cycle")
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

            # ④ Heartbeat — kill stale containers
            check_stale_sessions(products)

            # ⑤ Reset stuck features
            reset_stuck_features()

            # ⑥ Deliver PM messages
            for product in products:
                if (product.get("config") or {}).get("pm_messages"):
                    deliver_pm_messages(product)

            # ⑦a Reviewer-first: check globally for PRs awaiting review
            reviewer_product, persona = get_next_reviewer_product(products)

            if reviewer_product:
                product = reviewer_product
                log.info(f"Reviewer session for: {product['name']} (id={product['id']})")
            else:
                # ⑦b Normal round-robin for designer/coder work
                product = get_next_product(products)
                if not product:
                    log.debug("No products ready — sleeping")
                    time.sleep(_cfg["poll_interval"])
                    continue

                log.info(f"Selected product: {product['name']} (id={product['id']})")

                # ⑦c Determine whether to run designer or coder
                persona = determine_persona(product)
                log.info(f"Persona: {persona}")

            # Clear run_now flag if set
            if product.get("run_now"):
                clear_run_now(product["id"])

            # ⑧ Quiet hours gate (skip for reviewer — reviews are time-sensitive)
            if persona != "reviewer" and is_quiet_hours(product):
                h_start = product.get("quiet_hours_start")
                h_end   = product.get("quiet_hours_end")
                log.info(f"Quiet hours ({h_start}–{h_end} UTC) — skipping {product['name']}")
                time.sleep(_cfg["poll_interval"])
                continue

            # ⑨ Daily session cap gate (reviewer doesn't count against cap)
            if persona != "reviewer" and is_daily_cap_reached(product):
                log.info(f"Daily cap reached for {product['name']} — skipping")
                time.sleep(_cfg["poll_interval"])
                continue

            # ⑩ PR count gate (only applies to coder — designer/reviewer don't open new PRs)
            if persona == "coder":
                open_pr_count = count_open_prs(product)
                if open_pr_count >= _cfg["max_open_prs"]:
                    log.info(f"PR gate: {open_pr_count} open PRs — skipping")
                    send_alert("warning", f"{product['name']}: ≥{_cfg['max_open_prs']} open PRs unmerged — pausing")
                    time.sleep(_cfg["pr_gate_sleep"])
                    continue

            # ⑪ GitHub PR reconciliation
            reconcile_merged_prs(product)

            # ⑫ Run Claude session
            log.info(f"Launching {persona} session for: {product['name']}")
            exit_code = run_claude_in_docker(product, persona=persona)
            log.info(f"Session ended — exit_code={exit_code} persona={persona}")

            # exit_code=99 means "already running — skipped". Not an error; don't count or alert.
            if exit_code == 99:
                log.info(f"Session skipped (already running) for {product['name']} — will retry next cycle")
                time.sleep(30)  # Short sleep so we re-check soon
                continue

            # Track daily count (reviewers exempt)
            if persona != "reviewer":
                _reset_daily_counts_if_new_day()
                _daily_session_counts[product["id"]] = _daily_session_counts.get(product["id"], 0) + 1

            # ⑬ Update last_run_at ONLY on clean exit
            if exit_code == 0:
                with httpx.Client(base_url=PM_API_URL) as client:
                    client.patch(
                        f"/api/products/{product['id']}",
                        json={"last_run_at": datetime.now(timezone.utc).isoformat()},
                    )
            else:
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
