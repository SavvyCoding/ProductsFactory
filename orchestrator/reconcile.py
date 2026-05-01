"""Per-product reconciliation umbrella — Phase 4 of PollerRevamp.

Provides a single entry point `reconcile_product(product)` that the
poller's main loop calls per cycle for every `ready` product. It runs
the existing reconciliation passes in a defined order:

  1. reconcile_merged_prs  — sync GitHub-side merges/closures back to DB
  2. reconcile_in_flight_prs — drill into each in-flight feature, route
                               to Blocked sprint when fix_attempts cap hits

The two underlying functions have subtly different scopes
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

from orchestrator.github_client import (
    reconcile_in_flight_prs,
    reconcile_merged_prs,
)

log = logging.getLogger("reconcile")


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
