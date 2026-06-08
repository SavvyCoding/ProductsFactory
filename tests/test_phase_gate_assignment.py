"""Phase-gate enforcement at the FEATURE-ASSIGNMENT layer (migration 045).

Regression test for the gap where persona._decide_action was gated but
docker_runner._fetch_assigned_features was not — so a designer/coder session
launched for the current phase could still claim a later-phase feature, and the
priority-ASC sort meant a low-priority-number later-phase feature would even be
picked FIRST. See orchestrator/cycle/phase_gate.py.
"""
import os
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from unittest.mock import MagicMock

import orchestrator.docker_runner as dr
from orchestrator.cycle.phase_gate import gated_out_feature_ids


# ── pure helper ──────────────────────────────────────────────────────────────

PHASES = [{"id": 10, "order": 0, "gate_state": "open"},
          {"id": 11, "order": 1, "gate_state": "open"}]


def _feat(fid, phase_id):
    return {"id": fid, "phase_id": phase_id, "status": "Approved"}


class TestGatedOutHelper:
    def test_off_returns_empty(self):
        feats = [_feat(1, 10), _feat(2, 11)]
        assert gated_out_feature_ids(feats, PHASES, {}) == set()
        assert gated_out_feature_ids(feats, PHASES, None) == set()

    def test_freezes_later_phase_only(self):
        feats = [_feat(1, 10), _feat(2, 11)]
        assert gated_out_feature_ids(feats, PHASES, {"human_gate_phases": True}) == {2}

    def test_all_approved_no_freeze(self):
        phases = [{"id": 10, "order": 0, "gate_state": "approved"},
                  {"id": 11, "order": 1, "gate_state": "approved"}]
        feats = [_feat(1, 10), _feat(2, 11)]
        assert gated_out_feature_ids(feats, phases, {"human_gate_phases": True}) == set()

    def test_approving_first_phase_unlocks_second(self):
        phases = [{"id": 10, "order": 0, "gate_state": "approved"},
                  {"id": 11, "order": 1, "gate_state": "open"}]
        feats = [_feat(2, 11)]
        assert gated_out_feature_ids(feats, phases, {"human_gate_phases": True}) == set()

    def test_unphased_never_frozen(self):
        feats = [_feat(1, None)]
        assert gated_out_feature_ids(feats, PHASES, {"human_gate_phases": True}) == set()


# ── _fetch_assigned_features integration ─────────────────────────────────────

def _resp(payload, is_success=True):
    m = MagicMock()
    m.json.return_value = payload
    m.is_success = is_success
    m.status_code = 200 if is_success else 404
    m.raise_for_status = MagicMock()
    return m


class _FakeClient:
    def __init__(self, features, phases, product):
        self._f, self._p, self._prod = features, phases, product

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, *a, **kw):
        if url.endswith("/features"):
            return _resp(self._f)
        if url.endswith("/phases"):
            return _resp(self._p)
        return _resp(self._prod)  # /api/products/{id}


# Two Approved-no-design features; the LATER-phase one (#2) has a LOWER priority
# number, so under the priority-ASC sort it would be picked FIRST if ungated.
_FEATURES = [
    {"id": 1, "name": "current", "status": "Approved", "design_doc_path": None,
     "phase_id": 10, "priority": 50},
    {"id": 2, "name": "later", "status": "Approved", "design_doc_path": None,
     "phase_id": 11, "priority": 5},
]


class TestFetchAssignedGate:
    def test_gate_on_freezes_later_phase_feature(self, monkeypatch):
        client = _FakeClient(_FEATURES, PHASES, {"config": {"human_gate_phases": True}})
        monkeypatch.setattr("httpx.Client", lambda *a, **kw: client)
        selected, _, _ = dr._fetch_assigned_features(product_id=1, persona="designer", max_count=10)
        assert {f["id"] for f in selected} == {1}  # #2 frozen despite higher priority

    def test_gate_off_picks_later_phase_first(self, monkeypatch):
        client = _FakeClient(_FEATURES, PHASES, {"config": {}})
        monkeypatch.setattr("httpx.Client", lambda *a, **kw: client)
        selected, _, _ = dr._fetch_assigned_features(product_id=1, persona="designer", max_count=10)
        ids = [f["id"] for f in selected]
        assert set(ids) == {1, 2}
        assert ids[0] == 2  # demonstrates the ungated bug: later phase ranks first
