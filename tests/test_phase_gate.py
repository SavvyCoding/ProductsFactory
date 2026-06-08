"""Phase gate + reporter (migration 045, human-in-loop).

Covers the deterministic core of the report endpoint — the forward-dependency
walk into later phases, the gate_state transition, the approved-phase skip —
and the PM approve form. The LLM narration is monkeypatched so these stay fast
and deterministic; the narration is best-effort and not under test here.
"""
import json

import pytest

from website.models import Phase, FeatureLink
from tests.test_website import (  # noqa: F401
    test_engine, db, client, AUTH, make_product, make_feature,
)


def _make_phase(db, product_id, name, order, gate_state="open"):
    p = Phase(product_id=product_id, name=name, order=order, gate_state=gate_state)
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace the report's LLM narration with a deterministic stub."""
    async def _fake(prompt, db, max_tokens=3000):  # noqa: ARG001
        return json.dumps({
            "summary": "stub summary",
            "code_quality": "stub quality",
            "challenges": "stub challenges",
            "recommendations": ["resolve blockers first"],
        })
    monkeypatch.setattr("website.main._llm_call", _fake)


class TestPhaseReport:
    def test_settles_gate_and_walks_dependencies(self, client, db, stub_llm):
        prod = make_product(db, "/projects/gate-a")
        ph0 = _make_phase(db, prod.id, "Foundation", 0)
        ph1 = _make_phase(db, prod.id, "Integrations", 1)

        # Phase 0: one shipped, one blocked.
        make_feature(db, prod.id, name="auth", status="Pushed",
                     phase_id=ph0.id, merge_notes="added auth")
        blocked = make_feature(db, prod.id, name="data-layer", status="Blocked",
                               phase_id=ph0.id, blocked_reason="schema deadlock")

        # Two later-phase features depend on the blocked one — one via the typed
        # feature_links graph, one via the depends_on FK. Both must surface.
        dep_link = make_feature(db, prod.id, name="reporting", status="Approved", phase_id=ph1.id)
        dep_fk = make_feature(db, prod.id, name="export", status="Approved",
                              phase_id=ph1.id, depends_on=blocked.id)
        db.add(FeatureLink(source_id=blocked.id, target_id=dep_link.id, link_type="blocks"))
        db.flush()

        r = client.post(f"/api/phases/{ph0.id}/report")
        assert r.status_code == 200
        body = r.json()
        assert body["gate_state"] == "awaiting_review"

        rep = body["report"]
        assert rep["stats"]["pushed"] == 1
        assert rep["stats"]["blocked"] == 1
        assert rep["summary"] == "stub summary"  # narration merged

        assert len(rep["blockers"]) == 1
        blk = rep["blockers"][0]
        assert blk["feature_id"] == blocked.id
        assert blk["reason"] == "schema deadlock"
        downstream_ids = {d["feature_id"] for d in blk["downstream_blocked"]}
        assert downstream_ids == {dep_link.id, dep_fk.id}
        assert all(d["phase"] == "Integrations" for d in blk["downstream_blocked"])

        # The DB row was actually mutated, not just the response.
        db.refresh(ph0)
        assert ph0.gate_state == "awaiting_review"
        assert ph0.report is not None

    def test_same_phase_dependency_not_flagged(self, client, db, stub_llm):
        """A dependency within the SAME phase is current work, not a downstream
        warning — only strictly-later phases count."""
        prod = make_product(db, "/projects/gate-b")
        ph0 = _make_phase(db, prod.id, "Foundation", 0)
        blocked = make_feature(db, prod.id, name="core", status="Blocked", phase_id=ph0.id)
        make_feature(db, prod.id, name="sibling", status="Approved",
                     phase_id=ph0.id, depends_on=blocked.id)

        r = client.post(f"/api/phases/{ph0.id}/report")
        assert r.status_code == 200
        blk = r.json()["report"]["blockers"][0]
        assert blk["downstream_blocked"] == []

    def test_approved_phase_is_skipped(self, client, db, stub_llm):
        prod = make_product(db, "/projects/gate-c")
        ph = _make_phase(db, prod.id, "Done", 0, gate_state="approved")
        r = client.post(f"/api/phases/{ph.id}/report")
        assert r.status_code == 200
        assert r.json()["skipped"] == "phase already approved"
        db.refresh(ph)
        assert ph.gate_state == "approved"

    def test_report_404_unknown_phase(self, client, db, stub_llm):
        r = client.post("/api/phases/999999/report")
        assert r.status_code == 404


class TestAlertCreate:
    def test_create_alert_row(self, client, db):
        prod = make_product(db, "/projects/gate-alert")
        r = client.post("/api/alerts", json={
            "product_id": prod.id, "level": "info", "message": "Phase X awaiting review",
        })
        assert r.status_code == 201
        body = r.json()
        assert body["message"] == "Phase X awaiting review"
        assert body["delivered"] is False
        # Surfaces in the unread feed the dashboard nav badge reads.
        unread = client.get("/api/alerts/unread", auth=AUTH).json()
        assert any(a["id"] == body["id"] for a in unread)

    def test_create_alert_rejects_bad_level(self, client, db):
        r = client.post("/api/alerts", json={"level": "bogus", "message": "x"})
        assert r.status_code == 422


class TestWorkflowSettingsForm:
    def test_enables_flag_and_preserves_siblings(self, client, db):
        prod = make_product(db, "/projects/gate-wf1",
                            config={"daily_session_cap": 3, "last_architect_at": "2026-06-01T00:00:00"})
        r = client.post(f"/product/{prod.id}/workflow-settings",
                        data={"human_gate_phases": "on"}, auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        db.refresh(prod)
        assert prod.config["human_gate_phases"] is True
        # Sibling config keys must survive the merge.
        assert prod.config["daily_session_cap"] == 3
        assert prod.config["last_architect_at"] == "2026-06-01T00:00:00"

    def test_unchecked_checkbox_disables(self, client, db):
        prod = make_product(db, "/projects/gate-wf2", config={"human_gate_phases": True})
        # Unchecked checkbox => field absent from the POST body.
        r = client.post(f"/product/{prod.id}/workflow-settings",
                        data={}, auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        db.refresh(prod)
        assert prod.config["human_gate_phases"] is False

    def test_requires_auth(self, client, db):
        prod = make_product(db, "/projects/gate-wf3")
        r = client.post(f"/product/{prod.id}/workflow-settings",
                        data={"human_gate_phases": "on"}, follow_redirects=False)
        assert r.status_code == 401


class TestPhaseApproveForm:
    def test_approve_from_awaiting_review(self, client, db):
        prod = make_product(db, "/projects/gate-d")
        ph = _make_phase(db, prod.id, "P1", 0, gate_state="awaiting_review")
        r = client.post(f"/product/{prod.id}/phase/{ph.id}/approve",
                        auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        db.refresh(ph)
        assert ph.gate_state == "approved"

    def test_cannot_approve_open_phase(self, client, db):
        prod = make_product(db, "/projects/gate-e")
        ph = _make_phase(db, prod.id, "P1", 0, gate_state="open")
        r = client.post(f"/product/{prod.id}/phase/{ph.id}/approve", auth=AUTH)
        assert r.status_code == 422
        db.refresh(ph)
        assert ph.gate_state == "open"

    def test_approve_requires_auth(self, client, db):
        prod = make_product(db, "/projects/gate-f")
        ph = _make_phase(db, prod.id, "P1", 0, gate_state="awaiting_review")
        r = client.post(f"/product/{prod.id}/phase/{ph.id}/approve", follow_redirects=False)
        assert r.status_code == 401
