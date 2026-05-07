"""
Round-robin product selection and per-product schedule gates.

Owns the four stateless scheduling helpers:

  get_next_product            — INVARIANTS II.1–II.3. Priority: run_now=True
                                first, then PM API's /api/products/next does
                                last_run_at ASC.
  get_next_reviewer_product   — INVARIANT II.5. Reviewer work preempts
                                normal selection when a feature is in
                                Reviewing+pr_number on a ready product.
  get_next_retro_product      — Picks a product whose active sprint is fully
                                terminal and missing retro_doc_path, or any
                                completed sprint with no retro yet.
  is_quiet_hours              — UTC-hour window check; supports midnight wrap.

Stays in poller.py: is_daily_cap_reached (depends on the mutable
_daily_session_counts global that _reset_daily_counts_if_new_day rebinds —
moving the reader without the state would silently desync them).

Also exports _POST_SPRINT_PERSONAS, the cadence list the cycle uses to
schedule one-shot maintenance personas after each sprint completes.

Extracted from poller.py during Phase 4 of OrchestratorRefactor.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("poller")

PM_API_URL = os.environ["PM_API_URL"]


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


def get_next_retro_product(products: list[dict]) -> dict | None:
    """
    Check if any ready product has a sprint needing a retrospective:
    - Active sprint where all features are terminal and retro_doc_path is not set
    - OR a completed sprint with no retro_doc_path
    Returns the product dict (with `_retro_sprint_id` set on it) or None.

    Phase 3 (2026-05-06): the caller used to launch a `retrospective`
    LLM persona session against this product. Now it runs the inline
    `retro_generator.generate_and_commit_retro(product, sprint_id)`
    instead. We attach `_retro_sprint_id` so the caller doesn't need a
    second round-trip to find the sprint id.
    """
    TERMINAL = {"Pushed", "Deferred", "Rejected", "Reverted"}
    ready = [p for p in products if p["status"] == "ready"]
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for product in ready:
                pid = product["id"]
                active_resp = client.get(f"/api/products/{pid}/sprints/active")
                if active_resp.status_code == 200 and active_resp.json():
                    sprint = active_resp.json()
                    if not sprint.get("retro_doc_path"):
                        feat_resp = client.get(f"/api/products/{pid}/features")
                        features = feat_resp.json() if feat_resp.status_code == 200 else []
                        sprint_features = [f for f in features if f.get("sprint_id") == sprint["id"]]
                        non_terminal = [f for f in sprint_features if f.get("status") not in TERMINAL]
                        if sprint_features and not non_terminal:
                            product["_retro_sprint_id"] = sprint["id"]
                            return product
                    continue  # active sprint not yet done — retro not due
                # No active sprint — check for completed sprint missing retro
                sprints_resp = client.get(f"/api/products/{pid}/sprints")
                if sprints_resp.status_code == 200:
                    completed_no_retro = [
                        s for s in sprints_resp.json()
                        if s.get("status") == "completed" and not s.get("retro_doc_path")
                    ]
                    if completed_no_retro:
                        # Pick the oldest unsigned-off completed sprint —
                        # generally only one is pending at a time, but if
                        # multiple, the oldest needs the retro first.
                        completed_no_retro.sort(key=lambda s: s.get("id") or 0)
                        product["_retro_sprint_id"] = completed_no_retro[0]["id"]
                        return product
    except httpx.HTTPError as e:
        log.error(f"get_next_retro_product failed: {e}")
    return None
