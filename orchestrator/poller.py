"""
ProductFactory Poller - main loop (Windows localhost).

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
  4. Heartbeat - kill stale containers (progress.md not pushed in >45m)
  5. Reset stuck features (Implementing/Designing/Reviewing > 2h)
  6. Deliver pending PM messages to workspace files
  7a. Reviewer-first: check globally for Reviewing features with PRs
  7b. If none: pick next product via round-robin (has Approved/Designed features)
  7c. Determine persona: designer (Approved+no skip_design) or coder
  8. Quiet hours gate (reviewers skip this gate - reviews are time-sensitive)
  9. Daily session cap gate (reviewers exempt)
  10. PR count gate (coder only - designer/reviewer don't open new PRs)
  11. GitHub PR reconciliation (sync merged PRs -> DB -> Pushed)
  12. Run Claude in Docker with persona (blocks until session ends)
  13. Update last_run_at ONLY on clean exit (exit code 0)
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
from orchestrator.github_client import count_open_prs, reconcile_merged_prs, reconcile_in_flight_prs
from orchestrator.heartbeat import check_stale_sessions
from orchestrator.alerts import send_alert
from orchestrator.greenfield_scaffold import scaffold_greenfield

PM_API_URL    = os.environ["PM_API_URL"]
SSH_DIR       = Path(os.environ.get("SSH_DIR", "C:/Users/digvi/.ssh"))

# Env-var defaults - overridden by DB system config each cycle (see _load_runtime_cfg)
_ENV_DEFAULTS = {
    "poll_interval":               int(os.environ.get("POLL_INTERVAL",            "60")),
    "auth_check_timeout":          int(os.environ.get("AUTH_CHECK_TIMEOUT",       "30")),
    "max_open_prs":                int(os.environ.get("MAX_OPEN_PRS",             "3")),
    "pr_gate_sleep":               int(os.environ.get("PR_GATE_SLEEP",            "300")),
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


def is_quiet_hours(product: dict) -> bool:
    """Return True if current UTC hour falls within the product's quiet window."""
    start = product.get("quiet_hours_start")
    end   = product.get("quiet_hours_end")
    if start is None or end is None:
        return False
    current_hour = datetime.now(timezone.utc).hour
    if start <= end:
        return start <= current_hour < end
    else:  # wraps midnight e.g. 22-6
        return current_hour >= start or current_hour < end


def is_daily_cap_reached(product: dict) -> bool:
    """Return True if this product has hit its daily session cap."""
    cap = product.get("daily_session_cap")
    if not cap:
        return False
    with _daily_counts_lock:
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


# Post-sprint personas — run once after each sprint completes, in this order.
# Agents write last_{persona}_at on completion; the poller compares that timestamp
# against the sprint's completed_at to decide if the persona is due again.
_POST_SPRINT_PERSONAS = [
    "documenter",
    "analytics",
    "refactorer",
    "devops",
    "recommender",
]


class _LoopDetector:
    """
    Tracks recent persona selections per product and detects repeating patterns.
    In-memory only - resets on poller restart. No DB storage needed.
    """
    def __init__(self, window: int = 10):
        self._history: dict[int, list[str]] = {}
        self._window = window
        self._alert_cooldown: dict[int, datetime] = {}

    def record(self, product_id: int, persona: str):
        buf = self._history.setdefault(product_id, [])
        buf.append(persona)
        if len(buf) > self._window:
            buf.pop(0)

    # Feature-delivery and backlog personas naturally run back-to-back — exclude them
    # from single-persona 3x detection. Real stalls in these are caught by
    # stuck_feature_timeout. Only maintenance personas (documenter, analytics, etc.)
    # should rotate; repeated maintenance is a scheduling bug worth flagging.
    _EXPECTED_REPEATS = frozenset({
        "planner", "product_trainer",
        "coder", "reviewer", "designer",
        "qa_tester", "security_auditor",
        "retrospective", "product_planner",
    })

    def detect_loop(self, product_id: int) -> str | None:
        """Returns a description of the loop pattern, or None."""
        buf = self._history.get(product_id, [])
        # 2-persona alternating: A-B-A-B (skip if either is an expected repeater)
        if len(buf) >= 4:
            last4 = buf[-4:]
            if (last4[0] == last4[2] and last4[1] == last4[3] and last4[0] != last4[1]
                    and last4[0] not in self._EXPECTED_REPEATS
                    and last4[1] not in self._EXPECTED_REPEATS):
                return f"{last4[0]}->{last4[1]} alternating loop"
        # Same persona 3x in a row (skip expected repeaters)
        if len(buf) >= 3 and buf[-1] == buf[-2] == buf[-3]:
            if buf[-1] not in self._EXPECTED_REPEATS:
                return f"{buf[-1]} repeated 3x"
        return None

    def should_alert(self, product_id: int) -> bool:
        last = self._alert_cooldown.get(product_id)
        now = datetime.now(timezone.utc)
        if last and (now - last).total_seconds() < 900:
            return False
        self._alert_cooldown[product_id] = now
        return True

    def clear(self, product_id: int):
        self._history.pop(product_id, None)

_loop_detector = _LoopDetector()


def _heal_loop(product: dict, pattern: str) -> bool:
    """
    Diagnose and fix the root cause of a detected persona loop.
    Returns True if a fix was applied (caller should retry).
    """
    pid = product["id"]
    log.warning(f"[loop-heal] Detected loop for {product['name']}: {pattern}")

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            active_resp = client.get(f"/api/products/{pid}/sprints/active")
            if active_resp.status_code != 200 or not active_resp.json():
                return False
            sid = active_resp.json()["id"]

            feat_resp = client.get(f"/api/products/{pid}/features")
            all_features = feat_resp.json() if feat_resp.status_code == 200 else []
            sprint_features = [f for f in all_features if f.get("sprint_id") == sid]

            healed = 0

            # Fix 1: Approved features with design docs -> should be Designed
            for f in sprint_features:
                if f.get("status") == "Approved" and f.get("design_doc_path"):
                    client.patch(f"/api/features/{f['id']}", json={"status": "Designed"})
                    log.info(f"[loop-heal] Feature #{f['id']}: Approved->Designed (has design doc)")
                    healed += 1

            # Fix 2: In-agent state with no active session -> reset properly
            active_sess = client.get("/api/sessions/active", params={"product_id": pid})
            has_active = active_sess.status_code == 200 and active_sess.json()
            if not has_active:
                for f in sprint_features:
                    if f.get("status") in ("Implementing", "Designing", "Reviewing"):
                        reset_to = "Designed" if f.get("design_doc_path") else "Approved"
                        client.patch(f"/api/features/{f['id']}", json={"status": reset_to})
                        log.info(f"[loop-heal] Feature #{f['id']}: {f['status']}->{reset_to} (no active session)")
                        healed += 1

            # Fix 3: Delete stale session_result.json
            working_dir = product.get("working_dir")
            if working_dir:
                sr = Path(working_dir) / "session_result.json"
                if sr.exists():
                    sr.unlink()
                    log.info(f"[loop-heal] Deleted stale session_result.json in {working_dir}")
                    healed += 1

            # Fix 4: All features terminal but sprint still active -> complete it
            TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
            non_terminal = [f for f in sprint_features if f.get("status") not in TERMINAL]
            if sprint_features and not non_terminal:
                log.info(f"[loop-heal] All sprint features terminal - forcing sprint completion")
                for gate in ("qa_passed", "security_clean"):
                    try:
                        client.post(f"/api/sprints/{sid}/sign-off", json={
                            "gate": gate, "value": True,
                            "notes": "Auto-signed by loop healer",
                        })
                    except Exception:
                        pass
                client.patch(f"/api/sprints/{sid}", json={"status": "completed"})
                healed += 1

            if healed:
                log.info(f"[loop-heal] Applied {healed} fix(es) for {product['name']}")
            return healed > 0

    except Exception as e:
        log.warning(f"[loop-heal] Error: {e}")
        return False


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


def determine_persona(product: dict) -> str:
    """
    Decide which persona should run for this product.
    Sprint-gated: if an active sprint exists, only work on features in THAT sprint.
    Never jump to a different sprint until the current one is marked done.

    Priority order:
    1. retrospective   - a sprint was just completed with no retro yet
    2. product_planner - Approved features in the active sprint (writes detailed stories)
    3. designer        - Approved features with no design doc in the active sprint
    4. coder           - Designed features (or Approved with existing design doc)
    5. (if no active sprint) post-sprint maintenance personas (documenter/analytics/etc.)
    6. documenter / analytics / refactorer / devops / recommender  - once per completed sprint
    7. planner         - no actionable features; generate new ones
    """
    TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            # Retrospective disabled - DoD gates are auto-signed on sprint completion.
            # The retrospective agent was causing infinite loops. If needed, re-enable
            # by un-commenting and ensuring the agent calls the sign-off endpoint.

            # Check for active sprint
            active_sprint_resp = client.get(f"/api/products/{product['id']}/sprints/active")
            active_sprint = None
            if active_sprint_resp.status_code == 200 and active_sprint_resp.json():
                active_sprint = active_sprint_resp.json()

            # Fetch last completed sprint (for post-sprint persona gating)
            last_completed_sprint = None
            sprints_resp = client.get(f"/api/products/{product['id']}/sprints")
            if sprints_resp.status_code == 200:
                all_sprints = sprints_resp.json()
                completed = [s for s in all_sprints if s.get("status") == "completed"]
                if completed:
                    last_completed_sprint = max(completed, key=lambda s: s["id"])
                # Retrospective priority check: any completed sprint with no retro doc yet
                completed_no_retro = [s for s in completed if not s.get("retro_doc_path")]
                if completed_no_retro:
                    log.info(f"Completed sprint(s) missing retrospective: {[s['id'] for s in completed_no_retro]}")
                    return "retrospective"

            # Fetch all product features
            feat_resp = client.get(f"/api/products/{product['id']}/features")
            all_features = feat_resp.json() if feat_resp.status_code == 200 else []
            if not isinstance(all_features, list):
                all_features = []

            if active_sprint:
                sid = active_sprint.get("id")
                sprint_features = [f for f in all_features if f.get("sprint_id") == sid]

                # If all sprint features are terminal, run retrospective then complete the sprint
                non_terminal = [f for f in sprint_features if f.get("status") not in TERMINAL]
                if not non_terminal and sprint_features:
                    retro_done = active_sprint.get("retro_doc_path")
                    if not retro_done:
                        log.info(f"Active sprint {sid}: all features terminal — running retrospective first")
                        return "retrospective"

                    # Retro is done — now complete the sprint
                    log.info(f"Active sprint {sid}: retro complete, completing sprint")
                    complete_resp = client.post(f"/api/sprints/{sid}/force-complete")
                    if complete_resp.status_code == 200:
                        result = complete_resp.json()
                        log.info(f"Sprint {sid} completed via API: {result.get('action')}")
                        if result.get("release_notes"):
                            log.info(f"Release notes generated ({len(result['release_notes'])} chars)")

                    working_dir = product.get("working_dir")
                    if working_dir:
                        _clear_sprint_context(working_dir)
                    return None

                # Approved features with no design doc -> product_planner writes the story
                needs_design = [f for f in sprint_features
                                if f.get("status") == "Approved"
                                and not f.get("design_doc_path")]
                if needs_design:
                    return "product_planner"

                # Designed features or Approved with existing design doc -> coder
                codable = [f for f in sprint_features
                           if f.get("status") == "Designed"
                           or (f.get("status") == "Approved" and f.get("design_doc_path"))]
                if codable:
                    return "coder"

                # Reviewing features -> reviewer
                reviewable = [f for f in sprint_features
                              if f.get("status") == "Reviewing" and f.get("pr_number")]
                if reviewable:
                    return "reviewer"

                # Reviewed with no PR -> auto-push (PR was already merged/closed)
                reviewed_no_pr = [f for f in sprint_features
                                  if f.get("status") == "Reviewed" and not f.get("pr_number")]
                for f in reviewed_no_pr:
                    try:
                        client.patch(f"/api/features/{f['id']}", json={"status": "Pushed"})
                        log.info(f"Feature #{f['id']}: Reviewed with no PR -> Pushed")
                    except Exception:
                        pass

                # Reviewed features with PRs -> auto-merge if enabled
                reviewed = [f for f in sprint_features
                            if f.get("status") == "Reviewed" and f.get("pr_number")]
                if reviewed:
                    sys_cfg_resp = client.get("/api/system-config")
                    sys_cfg = sys_cfg_resp.json() if sys_cfg_resp.status_code == 200 else {}
                    if sys_cfg.get("auto_merge_enabled"):
                        github_pat = sys_cfg.get("github_pat", "")
                        github_repo = product.get("github_repo", "")
                        if github_pat and github_repo:
                            # Extract owner/repo from URL
                            repo_slug = github_repo.rstrip("/").split("github.com/")[-1].replace(".git", "")
                            log.info(f"Active sprint {sid}: auto-merging {len(reviewed)} Reviewed PRs on {repo_slug}")
                            merged_ids = set()
                            merged_features = []
                            for f in reviewed:
                                pr_num = f["pr_number"]
                                if pr_num in merged_ids:
                                    # PR already merged, just update this feature
                                    client.patch(f"/api/features/{f['id']}", json={"status": "Pushed"})
                                    merged_features.append(f)
                                    continue
                                try:
                                    merge_resp = httpx.put(
                                        f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}/merge",
                                        headers={"Authorization": f"token {github_pat}", "Accept": "application/vnd.github+json"},
                                        json={"merge_method": "squash"},
                                        timeout=30,
                                    )
                                    if merge_resp.status_code == 200:
                                        log.info(f"Auto-merged PR #{pr_num} for feature #{f['id']}")
                                        merged_ids.add(pr_num)
                                        client.patch(f"/api/features/{f['id']}", json={"status": "Pushed"})
                                        merged_features.append(f)
                                    elif merge_resp.status_code == 405:
                                        log.warning(f"PR #{pr_num} not mergeable (conflicts?) - skipping")
                                    elif merge_resp.status_code == 422:
                                        log.info(f"PR #{pr_num} already merged")
                                        client.patch(f"/api/features/{f['id']}", json={"status": "Pushed"})
                                        merged_features.append(f)
                                    else:
                                        log.warning(f"Merge PR #{pr_num} returned {merge_resp.status_code}: {merge_resp.text[:100]}")
                                except Exception as e:
                                    log.warning(f"Failed to merge PR #{pr_num}: {e}")
                            # Record auto-merge as a session in history
                            if merged_features:
                                import uuid
                                session_uid = str(uuid.uuid4())[:8]
                                notes = "Auto-merged PRs: " + ", ".join(
                                    f"#{f['pr_number']} ({f['name'][:30]})" for f in merged_features
                                )
                                try:
                                    client.post("/api/sessions", json={
                                        "product_id": product["id"],
                                        "session_uid": session_uid,
                                        "persona": "auto-merge",
                                        "backend": "poller",
                                        "container_id": "poller",
                                        "exit_code": 0,
                                        "features_attempted": len(merged_features),
                                        "features_pushed": len(merged_features),
                                        "notes": notes,
                                    })
                                except Exception:
                                    pass  # non-critical
                        else:
                            log.warning(f"Auto-merge enabled but missing github_pat or github_repo")
                        return None  # re-check on next cycle after merges
                    else:
                        log.info(f"Active sprint {sid}: {len(reviewed)} Reviewed features awaiting manual merge")
                        return None

                # Check what's blocking progress
                pending = [f for f in sprint_features if f.get("status") == "Pending"]
                in_agent = [f for f in sprint_features
                            if f.get("status") in ("Designing", "Implementing", "Reviewing")]
                if in_agent:
                    # Check if there's actually a running container - if not, reset the features
                    active_session = client.get(f"/api/sessions/active", params={"product_id": product["id"]})
                    has_active = active_session.status_code == 200 and active_session.json()
                    if not has_active:
                        log.info(f"Active sprint {sid}: {len(in_agent)} features in agent states but no active session - resetting")
                        for f in in_agent:
                            # Reset to Designed if design doc exists, otherwise Approved
                            reset_to = "Designed" if f.get("design_doc_path") else "Approved"
                            client.patch(f"/api/features/{f['id']}", json={"status": reset_to})
                            log.info(f"  Feature #{f['id']} {f['status']} -> {reset_to} (no active session)")
                        return None  # next cycle will pick them up
                    log.info(f"Active sprint {sid}: {len(in_agent)} features being processed by agents - waiting")
                    return None
                if pending and not in_agent:
                    log.info(f"Active sprint {sid}: {len(pending)} Pending features awaiting PM approval - nothing for agents to do")
                    return None

                log.info(f"Active sprint {sid}: {len(non_terminal)} features in other states - waiting")
                return None

            # No active sprint — auto-create one for any Approved unsprinted features.
            if _auto_create_sprint_for_unsprinted(product, client):
                return None  # next cycle picks up the new active sprint
            log.info(f"No active sprint and no approved unsprinted features for product {product['id']}")
    except httpx.HTTPError as e:
        log.error(f"determine_persona failed: {e}")
        return None

    # No design/code work — check post-sprint personas (documenter/analytics/refactorer/devops/recommender)
    post_sprint = _post_sprint_persona_due(product, last_completed_sprint)
    if post_sprint:
        log.info(f"Post-sprint persona due: {post_sprint} (last sprint: {last_completed_sprint.get('id') if last_completed_sprint else None})")
        return post_sprint

    # Nothing else - run planner to generate new feature ideas
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
# These must stay in sync with the server's TTL logic in /api/poller/heartbeat.
# Do NOT make them env-configurable without also updating the server endpoint.
_HEARTBEAT_INTERVAL = 15   # seconds between heartbeat updates
_LOCK_TTL           = 30   # seconds - stale lock threshold (must match server)

_hb_stop: threading.Event | None = None
_hb_lock_stolen = threading.Event()  # set by heartbeat thread when lock is stolen/expired


def _acquire_db_lock() -> bool:
    """
    Atomically acquire the poller distributed lock via PM API.
    Uses a single PostgreSQL UPDATE WHERE so two callers can never both succeed.
    Returns True on success, False if another live poller holds the lock.
    Falls back to True (allow start) if the API is unreachable - better to risk
    a duplicate than to prevent all pollers from ever starting.

    On 409, if the holder is on the same host but the PID is dead, force-unlock
    and retry once — recovers from hard-crashed pollers without waiting for the
    30s TTL.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/poller/lock", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
            if resp.status_code == 200:
                log.info(f"Poller lock acquired (pid={_LOCK_PID}, host={_LOCK_HOST})")
                return True
            if resp.status_code == 409:
                d = resp.json().get("detail", {})
                holder_pid  = d.get("holder_pid")
                holder_host = d.get("holder_host")
                # Same-host stale-PID recovery: probe the holder PID locally.
                if holder_host == _LOCK_HOST and isinstance(holder_pid, int) and holder_pid != _LOCK_PID:
                    try:
                        os.kill(holder_pid, 0)
                        # Holder is alive — genuine conflict.
                    except OSError:
                        log.warning(
                            f"Lock holder pid={holder_pid} on this host is dead — "
                            f"force-unlocking and retrying."
                        )
                        client.post("/api/poller/force-unlock")
                        retry = client.post(
                            "/api/poller/lock",
                            json={"pid": _LOCK_PID, "host": _LOCK_HOST},
                        )
                        if retry.status_code == 200:
                            log.info(f"Poller lock acquired after force-unlock (pid={_LOCK_PID})")
                            return True
                log.error(
                    f"Another poller holds the lock - "
                    f"pid={holder_pid}, host={holder_host}, "
                    f"last_heartbeat={d.get('heartbeat_at')}. Exiting."
                )
                return False
            log.error(f"Unexpected response from lock endpoint: {resp.status_code} - allowing start")
            return True
    except Exception as e:
        log.warning(f"Could not acquire DB lock ({e}) - allowing start (API may be starting up)")
        return True


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


def _heartbeat_loop(stop_event: threading.Event):
    """
    Background thread: refreshes the DB lock heartbeat every 15s.
    If the API returns 404, the lock was stolen (another poller took over) - signal the
    main thread to abort the current cycle and re-acquire the lock or exit.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL):
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
                resp = client.post("/api/poller/heartbeat", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
            if resp.status_code == 404:
                log.critical("Heartbeat 404 - lock was stolen or expired. Signalling main thread to abort cycle.")
                _hb_lock_stolen.set()
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
            for _p in products:
                if _p.get("status") == "ready":
                    reconcile_merged_prs(_p)
                    reconcile_in_flight_prs(_p)

            # ⑦a Reviewer-first: any product with a Reviewing feature + PR takes priority
            reviewer_product, reviewer_persona = get_next_reviewer_product(products)
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

            # ⑪ PR count gate (only applies to coder - designer/reviewer don't open new PRs)
            # Exclude PRs already being fixed (feature is Implementing + review_outcome set)
            # — those are in-flight fix cycles and should not block new coder work.
            if persona == "coder":
                try:
                    _feats_resp = httpx.get(f"{PM_API_URL}/api/products/{product['id']}/features", timeout=10)
                    _fixing_prs = {
                        f["pr_number"]
                        for f in (_feats_resp.json() if _feats_resp.status_code == 200 else [])
                        if f.get("status") == "Implementing" and f.get("review_outcome") and f.get("pr_number")
                    }
                except Exception:
                    _fixing_prs = set()
                open_pr_count = max(0, count_open_prs(product) - len(_fixing_prs))
                if open_pr_count >= _cfg["max_open_prs"]:
                    log.info(f"PR gate: {open_pr_count} open PRs - skipping")
                    send_alert("warning", f"{product['name']}: ≥{_cfg['max_open_prs']} open PRs unmerged - pausing")
                    time.sleep(_cfg["pr_gate_sleep"])
                    continue

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
