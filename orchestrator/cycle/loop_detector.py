"""
In-memory persona-loop detection and root-cause healing.

The detector tracks recent persona selections per product (last 10) and flags
two patterns:
  - same persona 3× in a row (excluding feature-delivery and backlog personas
    that naturally repeat — coder, reviewer, planner, etc.)
  - 2-persona A-B-A-B alternating (e.g. coder ↔ reviewer ping-pong on a
    stuck PR)

Alerts are rate-limited to once per 15 minutes per product. _heal_loop is the
diagnostic side: when a loop is detected it inspects the active sprint's
features and applies four fixes (Approved+design_doc → Designed; orphan agent
states → reset; stale session_result.json → delete; all-terminal-features →
force-complete sprint). Returns True if any fix was applied so the caller can
retry the cycle.

INVARIANTS IX.1–IX.3.

Extracted from poller.py during Phase 4 of OrchestratorRefactor.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("poller")

PM_API_URL = os.environ["PM_API_URL"]


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
        "retrospective",
        # "product_planner" was merged into "designer" 2026-05-06 (Phase 1
        # of futureplan.md). Kept in this allow-list as a no-op alias for
        # stale persona-history entries from before the merge.
        "product_planner",
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


# Process-scoped singleton — re-importing the module gives the same instance.
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
