"""Shared depends_on dispatch-gating logic (wave-10 keystone, 2026-06-13).

Until wave-10 the ``features.depends_on`` FK was decorative — written by the
designer, walked by the phase-report forward-dependency analysis
(``website/main.py``), but NEVER consulted when deciding what a coder/designer
session may work on. So two features where ``B.depends_on == A`` would dispatch
in PARALLEL.

That gap is what makes *vertical slicing* collapse. Vertical slicing splits a
coupled foundation (e.g. a NextAuth setup that must create one shared
``[...nextauth].ts``) into a sequence: slice 1 creates the shared file with a
minimal working thread; slice 2+ set ``depends_on`` on slice 1 and EXTEND the
file. The whole point is that the slices run **in order** — if they race, they
co-create the same file and collide exactly like the horizontal split they were
meant to replace (canonical 2026-06-13 IndianFoodTruck NextAuth cascade, #1621).

This module makes ``depends_on`` an ENFORCED dispatch gate, so vertical slices
physically sequence. Like the phase gate, it has TWO enforcement points that
must agree (``cycle/persona._decide_action`` and
``docker_runner._fetch_assigned_features``); both call
``dependency_blocked_feature_ids`` so they stay consistent.

It also fixes a real pre-existing bug: a story whose designer set
``depends_on`` at an ``infra`` provisioning story (the documented pattern in
designer.md) was never actually held until that service was provisioned — the
coder could claim it immediately and fail on a connection-refused.
"""


def dependency_blocked_feature_ids(features) -> set:
    """Return the ids of features that must NOT be dispatched yet because
    their ``depends_on`` points at a feature that has not shipped.

    A feature is dependency-blocked when ``depends_on`` is set, the referenced
    feature is present in ``features``, and its status is anything other than
    ``Pushed``. The reference must be resolved against the FULL product feature
    list (including ``infra`` stories) — a vertical slice's predecessor, or the
    infra story a feature depends on, is itself excluded from the persona pools,
    so resolving against a pre-filtered subset would treat a live dependency as
    dangling.

    Edge cases (all → NOT blocked, fail-open to avoid permanent deadlock):
      - ``depends_on`` is null / not an int → no dependency declared.
      - the referenced feature is absent from the list → dangling ref (the FK
        is ``ON DELETE SET NULL``, so a deleted dep normally becomes null; a
        stale id should not freeze the dependent forever).
      - a self-reference (``depends_on == id``) → ignored.

    A dependency that is terminally failed (``Rejected``/``Reverted``) keeps the
    dependent blocked — the chain is genuinely broken and that should surface as
    a stalled feature for triage, not silently ship a slice onto a foundation
    that never landed.
    """
    by_id = {f.get("id"): f for f in (features or [])
             if isinstance(f.get("id"), int)}
    blocked: set = set()
    for f in (features or []):
        fid = f.get("id")
        dep = f.get("depends_on")
        if not isinstance(dep, int) or dep == fid:
            continue
        dep_feat = by_id.get(dep)
        if dep_feat is None:
            continue  # dangling ref — fail open
        if dep_feat.get("status") != "Pushed" and isinstance(fid, int):
            blocked.add(fid)
    return blocked
