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


# Terminally-dead statuses: a dependency in one of these will NEVER reach
# ``Pushed``, so a dependent pointing at it is stalled forever (the gate above
# keeps it Blocked by design). These are the targets the repair sweep re-homes.
_DEAD_STATUSES = frozenset({"Rejected", "Reverted"})


def dangling_dependency_repairs(features) -> list:
    """Find features whose ``depends_on`` points at a terminally-dead
    (``Rejected``/``Reverted``) target and compute a repair for each.

    ``dependency_blocked_feature_ids`` deliberately keeps such a dependent
    BLOCKED — you must not ship a slice onto a foundation that never landed.
    But the dead target is almost always a designer sizing-split that was
    ``Rejected`` as "Replaced by children #X, #Y" (the split re-homes the
    children via ``parent_id`` but NOT the external dependents' ``depends_on``).
    The dependent then sits invisibly stalled forever. This sweep surfaces it
    and proposes the fix: re-home ``depends_on`` onto the live replacement.

    The live replacement is a NON-dead child of the dead target
    (``parent_id == dead_id``); we pick the HIGHEST-id such child — the last
    slice in the split chain, which is the API/integration a dependent usually
    consumes. If no live child exists, ``new_dep`` is ``None`` (can't auto-
    re-home → flag for a human; clearing it would let the dependent ship
    without its prerequisite, trading "stuck forever" for "fails on missing
    prerequisite").

    Pure function — returns the actions, applies NOTHING. Each action is::

        {"feature_id": int, "old_dep": int, "new_dep": int | None, "reason": str}

    Canonical: IndianFoodTruck #1633 (Order History Frontend) pointed at the
    Rejected #1632 (Backend), which had been re-split into #1634/#1635 — the
    sweep re-homes it to the live #1635 (GET /api/orders).
    """
    by_id = {f.get("id"): f for f in (features or [])
             if isinstance(f.get("id"), int)}
    # Index of live (non-dead) children per parent id.
    live_children: dict = {}
    for f in (features or []):
        pid = f.get("parent_id")
        if isinstance(pid, int) and f.get("status") not in _DEAD_STATUSES:
            cid = f.get("id")
            if isinstance(cid, int):
                live_children.setdefault(pid, []).append(cid)

    repairs: list = []
    for f in (features or []):
        fid = f.get("id")
        dep = f.get("depends_on")
        if not isinstance(dep, int) or dep == fid:
            continue
        dep_feat = by_id.get(dep)
        if dep_feat is None or dep_feat.get("status") not in _DEAD_STATUSES:
            continue
        candidates = live_children.get(dep, [])
        new_dep = max(candidates) if candidates else None
        if new_dep is not None:
            reason = (f"depends_on #{dep} is {dep_feat.get('status')} (dead); "
                      f"re-home to live replacement #{new_dep} (child of #{dep})")
        else:
            reason = (f"depends_on #{dep} is {dep_feat.get('status')} (dead) with "
                      f"no live replacement child — needs PM re-point")
        repairs.append({
            "feature_id": fid, "old_dep": dep,
            "new_dep": new_dep, "reason": reason,
        })
    return repairs
