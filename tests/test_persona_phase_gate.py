"""Tests for the human-in-loop phase gate in orchestrator.cycle.persona
(_decide_action, migration 045).

The gate is opt-in via product.config.human_gate_phases. When on, features in
phases ordered AFTER the current gating phase (lowest-order phase not yet
'approved') are frozen out of the coder/designer dispatch pools. When off (or
the product/phases endpoints 404, as in the legacy mock), behavior is
unchanged — verified by the existing dispatcher suite staying green.
"""
from __future__ import annotations

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from unittest.mock import MagicMock
from types import SimpleNamespace

from orchestrator.cycle.persona import _decide_action


def _resp(payload, ok=True, code=200):
    return SimpleNamespace(is_success=ok, status_code=code, json=lambda: payload)


def _client(features, phases, config, pid=25, sys_cfg=None):
    """Mock httpx client serving features, phases, the product (with config),
    and system-config. Order of substring checks matters — /phases and
    /features are checked before the bare product URL."""
    client = MagicMock()
    sys_cfg = sys_cfg or {}

    def _get(url, *a, **kw):
        if "/features" in url:
            return _resp(features)
        if "/phases" in url:
            return _resp(phases)
        if "/system-config" in url:
            return _resp(sys_cfg)
        if url == f"/api/products/{pid}":
            return _resp({"config": config})
        return _resp({}, ok=False, code=404)

    client.get.side_effect = _get
    return client


def _feature(fid, status, phase_id, **kw):
    base = {
        "id": fid, "status": status, "phase_id": phase_id,
        "design_doc_path": None, "pr_number": None, "review_outcome": None,
    }
    base.update(kw)
    return base


def _is_coder(result):
    return result.get("action") == "launch_session" and result.get("persona") == "coder"


# Phase 10 = order 0, Phase 11 = order 1. One Pushed feature parks phase 10 as
# the (un-approved) gating phase; one Designed feature sits in the later phase 11.
_PHASES_OPEN = [
    {"id": 10, "order": 0, "gate_state": "open"},
    {"id": 11, "order": 1, "gate_state": "open"},
]


def _later_phase_codeable():
    return [
        _feature(1, "Pushed", phase_id=10),
        _feature(2, "Designed", phase_id=11, design_doc_path="d.md"),
    ]


class TestPhaseGate:
    def test_gate_off_dispatches_later_phase(self):
        """Flag off → the later-phase Designed feature is coded normally."""
        client = _client(_later_phase_codeable(), _PHASES_OPEN, config={"human_gate_phases": False})
        result = _decide_action(product_id=25, client=client)
        assert _is_coder(result), result

    def test_gate_on_freezes_later_phase(self):
        """Flag on, phase 10 (order 0) is the gating phase → the order-1 feature
        is frozen, so the coder is NOT dispatched for it."""
        client = _client(_later_phase_codeable(), _PHASES_OPEN,
                         config={"human_gate_phases": True})
        result = _decide_action(product_id=25, client=client)
        assert not _is_coder(result), result

    def test_gate_on_current_phase_flows(self):
        """A codeable feature IN the current gating phase still dispatches."""
        feats = [_feature(2, "Designed", phase_id=10, design_doc_path="d.md")]
        client = _client(feats, _PHASES_OPEN, config={"human_gate_phases": True})
        result = _decide_action(product_id=25, client=client)
        assert _is_coder(result), result

    def test_approving_phase_unlocks_next(self):
        """When phase 10 is 'approved', the gating phase becomes phase 11 — its
        feature is no longer frozen."""
        phases = [
            {"id": 10, "order": 0, "gate_state": "approved"},
            {"id": 11, "order": 1, "gate_state": "open"},
        ]
        feats = [_feature(2, "Designed", phase_id=11, design_doc_path="d.md")]
        client = _client(feats, phases, config={"human_gate_phases": True})
        result = _decide_action(product_id=25, client=client)
        assert _is_coder(result), result

    def test_unphased_feature_never_gated(self):
        """Unphased features flow regardless of the gate (the planner phases
        them later)."""
        feats = [_feature(2, "Designed", phase_id=None, design_doc_path="d.md")]
        client = _client(feats, _PHASES_OPEN, config={"human_gate_phases": True})
        result = _decide_action(product_id=25, client=client)
        assert _is_coder(result), result
