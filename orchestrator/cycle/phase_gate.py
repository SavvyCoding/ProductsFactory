"""Shared human-in-loop phase-gate logic (migration 045).

The gate has TWO enforcement points that must agree, or a later-phase feature
slips through:

  1. orchestrator/cycle/persona._decide_action — decides which PERSONA to
     launch. Gating its candidate pools stops a session from being launched
     *for* frozen work.
  2. orchestrator/docker_runner._fetch_assigned_features — decides which
     FEATURES a launched session actually claims. This selects features
     INDEPENDENTLY of _decide_action (by status + priority), so without gating
     here a designer/coder session launched for the current phase could still
     grab a later-phase feature — and because both selectors sort priority ASC
     (lower number = higher rank), a low-priority-number later-phase feature
     would actively outrank current-phase work.

Both call gated_out_feature_ids() so the two points stay consistent.
"""


def gated_out_feature_ids(features, phases, config) -> set:
    """Return the set of feature ids frozen by the phase gate.

    The gate is **ON by default** (2026-06-09): it engages unless
    ``config.human_gate_phases`` is explicitly ``False``. Empty set when the
    gate is off (explicitly disabled) or when every phase is approved. A feature
    is frozen when its phase is ordered strictly AFTER the current gating phase
    (the lowest-order phase not yet ``approved``). Unphased features are never
    frozen (the planner phases them later).
    """
    if (config or {}).get("human_gate_phases", True) is False:
        return set()
    phases = phases or []
    unapproved_orders = [p.get("order") for p in phases if p.get("gate_state") != "approved"]
    if not unapproved_orders:
        return set()
    current_order = min(unapproved_orders)
    order_by_phase = {p.get("id"): p.get("order") for p in phases}
    out: set = set()
    for f in (features or []):
        ph_id = f.get("phase_id")
        if ph_id is not None and order_by_phase.get(ph_id, current_order) > current_order:
            out.add(f.get("id"))
    return out
