"""Per-product reconciliation umbrella — Phase 4 of PollerRevamp.

Provides a single entry point `reconcile_product(product)` that the
poller's main loop calls per cycle for every `ready` product. It runs
the existing reconciliation passes in a defined order:

  1. reconcile_merged_prs  — sync GitHub-side merges/closures back to DB
  2. reconcile_in_flight_prs — drill into each in-flight feature, route
                               to Blocked sprint when fix_attempts cap hits
  3. reconcile_sprint_pr_state — null out sprint metadata when its sprint
                               PR is closed-unmerged on GitHub, so the
                               next post-coder cycle falls back to
                               per-feature mode instead of pushing to a
                               dead branch (incident 2026-05-06).

The two PR-feature passes have subtly different scopes
(merged_prs walks the 20 most-recent closed PRs by date; in_flight_prs
walks every feature with a PR reference and asks GitHub one at a time)
and different correctness invariants. They're kept as separate
implementations for now — this module only changes the call shape so
the poller has one symbol to import and future consolidation has a
single fence to push back against.

Stuck-feature recovery (`reset_stuck`) is a global, time-based,
website-side endpoint and stays separate. Real-time agent input
validation (`_apply_session_entry`) is in docker_runner.py because it
runs inside the live-poll thread; keeping the call sites local to
session lifecycle is intentional. See INVARIANTS.md V.1-V.5 for the
contract each layer holds.
"""
from __future__ import annotations

import logging
import os

import httpx

from orchestrator.github_client import (
    reconcile_in_flight_prs,
    reconcile_merged_prs,
)

log = logging.getLogger("reconcile")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")


def reconcile_sprint_pr_state(product: dict) -> None:
    """Null out sprint metadata when its sprint PR is closed-unmerged.

    The sprint table caches `branch_name`, `pr_number`, `pr_url` set at
    sprint activation time (orchestrator.sprint_pr.provision_sprint_pr).
    Once cached, nothing automatically refreshes them. If the sprint PR
    is closed (manually, by auto_merge.sweep, or by an external script)
    without being merged, the metadata stays stale forever. The next
    post-coder cycle reads the stale metadata, pushes commits to the
    sprint branch, and the website thinks features are "Reviewing"
    pointing at a closed PR. Reconcile_in_flight_prs bounces them right
    back to Implementing the same cycle. The result is a tight loop
    where the agent burns turns producing commits no live PR points at.

    Real incident 2026-05-06: PR #1 on DigitalSign was closed at
    2026-05-05 18:38 UTC. For ~9 hours the orchestrator kept pushing
    to sprint/79 with the website reporting "0 features in Reviewing"
    while the agent ran successfully and the post-coder pipeline logged
    "reusing sprint PR #1" — pointing at a corpse.

    On detecting state=closed AND merged=False on the sprint PR, this
    nulls out the sprint's `pr_number`, `pr_url`, and `branch_name` via
    PATCH /api/sprints/{id}. The next post-coder cycle then sees
    `_sprint_pr_number is None` and falls back to per-feature mode (the
    safe path). PMs can re-provision via the website or by re-activating
    the sprint.

    state=closed AND merged=True is left alone — the merge is the sprint
    completion signal; the existing complete-sprint flow handles that.
    """
    pid = product.get("id")
    pname = product.get("name", "?")
    github_repo = product.get("github_repo") or ""
    if not pid or not github_repo:
        return

    # Fetch the active sprint + GH PAT from PM API. The product dict from
    # /api/products doesn't carry active sprint metadata, so we look it up
    # explicitly here.
    sprint_id: int | None = None
    sprint_pr: int | None = None
    gh_token: str | None = None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            sp = client.get(f"/api/products/{pid}/sprints/active")
            if not sp.is_success:
                return
            body = sp.json() if sp.headers.get("content-type", "").startswith("application/json") else None
            if not isinstance(body, dict):
                return
            sprint_id = body.get("id")
            sprint_pr = body.get("pr_number")
            if not sprint_id or not sprint_pr:
                return  # Sprint not in sprint-PR mode

            sc = client.get("/api/system-config")
            if not sc.is_success or "application/json" not in sc.headers.get("content-type", ""):
                return
            scd = sc.json()
            gh_token = scd.get("github_pat") if isinstance(scd, dict) else None
        if not gh_token:
            return
    except Exception:
        return

    # Slug parsing — match the docker_runner regex for consistency.
    import re
    m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", github_repo)
    repo_slug = m.group(1) if m else github_repo

    try:
        pr = httpx.get(
            f"https://api.github.com/repos/{repo_slug}/pulls/{int(sprint_pr)}",
            headers={"Authorization": f"Bearer {gh_token}",
                     "Accept": "application/vnd.github+json"},
            timeout=10,
        )
    except Exception as e:
        log.debug(f"[reconcile-sprint-pr] {pname} pr#{sprint_pr}: GitHub error {e}")
        return

    if pr.status_code == 404:
        log.warning(
            f"[reconcile-sprint-pr] {pname}: sprint PR #{sprint_pr} returned 404 "
            f"on GitHub — nulling out sprint metadata so post-coder falls back"
        )
    elif pr.status_code != 200:
        # Transient — leave alone; we'll retry next cycle.
        return
    else:
        body = pr.json() if pr.headers.get("content-type", "").startswith("application/json") else {}
        if not isinstance(body, dict):
            return
        if body.get("state") == "open":
            return  # All good — nothing to do.
        if body.get("merged"):
            # Closed via merge: that's the sprint-completion signal; let the
            # complete-sprint flow handle it. Don't null metadata — auto_merge
            # may still need pr_number to mark features Pushed.
            return
        log.warning(
            f"[reconcile-sprint-pr] {pname}: sprint PR #{sprint_pr} state="
            f"{body.get('state')!r} merged={body.get('merged')} on GitHub — "
            f"nulling out sprint metadata so next post-coder cycle falls back "
            f"to per-feature mode rather than pushing to a closed PR"
        )

    # Null out the sprint's PR metadata. Requires the schema fix that adds
    # branch_name/pr_number/pr_url to SprintUpdate (the matching commit on
    # this branch). Without that fix, this PATCH succeeds HTTP-wise but
    # silently drops the fields — caller sees no error, sprint metadata
    # stays stale.
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            r = client.patch(
                f"/api/sprints/{sprint_id}",
                json={"pr_number": None, "pr_url": None, "branch_name": None},
            )
            if r.status_code >= 300:
                log.warning(
                    f"[reconcile-sprint-pr] {pname}: PATCH sprint {sprint_id} "
                    f"failed: HTTP {r.status_code} {r.text[:200]}"
                )
            else:
                log.info(
                    f"[reconcile-sprint-pr] {pname}: nulled metadata on sprint "
                    f"{sprint_id} (was pr#{sprint_pr})"
                )
    except Exception as e:
        log.warning(f"[reconcile-sprint-pr] {pname}: PATCH error: {e}")


def reconcile_product(product: dict) -> None:
    """Run all per-product PR reconciliation passes in defined order.

    Best-effort: each pass wraps its own exceptions, so a failure in
    `reconcile_merged_prs` does not skip `reconcile_in_flight_prs`.
    The poller calls this once per `ready` product per cycle.
    """
    if product.get("status") != "ready":
        return

    pid = product.get("id")
    pname = product.get("name", "?")

    try:
        reconcile_merged_prs(product)
    except Exception:
        log.exception(f"reconcile_merged_prs crashed for product {pid} ({pname})")

    try:
        reconcile_in_flight_prs(product)
    except Exception:
        log.exception(f"reconcile_in_flight_prs crashed for product {pid} ({pname})")

    try:
        reconcile_sprint_pr_state(product)
    except Exception:
        log.exception(f"reconcile_sprint_pr_state crashed for product {pid} ({pname})")
