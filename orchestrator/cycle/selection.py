"""
Round-robin product selection and per-product schedule gates.

Owns the stateless scheduling helpers:

  get_next_product            — INVARIANTS II.1–II.3. Priority: run_now=True
                                first, then PM API's /api/products/next does
                                last_run_at ASC.
  get_next_reviewer_product   — INVARIANT II.5. Reviewer work preempts
                                normal selection when a feature is in
                                Reviewing+pr_number on a ready product.
  is_quiet_hours              — UTC-hour window check; supports midnight wrap.

Stays in poller.py: is_daily_cap_reached (depends on the mutable
_daily_session_counts global that _reset_daily_counts_if_new_day rebinds —
moving the reader without the state would silently desync them).

Also exports _ONDEMAND_PERSONAS, the set of maintenance personas that
are PM-triggered (via product.run_persona_now). The poller stamps
last_{persona}_at in product.config when one of these runs, so the UI
can show "ran 2h ago".

Extracted from poller.py during Phase 4 of OrchestratorRefactor.
"""

import logging
import os
from datetime import datetime, timezone

import httpx

log = logging.getLogger("poller")

PM_API_URL = os.environ["PM_API_URL"]


# Maintenance personas — PM-triggered via product.run_persona_now (see
# website/main.py:run_persona). Listed here so the poller knows to stamp
# last_{persona}_at in product.config after a successful run, for UI display.
# The post-sprint scheduled cadence that referenced this list was never wired
# up by the Phase 5 dispatcher and has been removed; these personas are now
# strictly on-demand.
_ONDEMAND_PERSONAS = [
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


