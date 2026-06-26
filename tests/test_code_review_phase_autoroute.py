"""
Increment 1.5 of the code_auditor filing promotion (2026-06-25).

AI-filed audit findings (code_auditor / security_auditor: source=ai +
feature_type=bug) must auto-attach to a standing 'Code-Review Hardening' phase
at filing time, so they're dispatchable the moment the PM approves them instead
of sitting un-phased (the planner buries new phases at order=max, and un-phased
Approved work is never claimed). PM creates, AI features, and AI chores are NOT
auto-routed.

Reuses the HTTP client + DB fixtures from test_website.py.
"""

from tests.test_website import client, db, test_engine, make_product  # noqa: F401

from website.models import Phase
from sqlalchemy import select


_AC = "A real defect.\n- AC1: returns 403\n- AC2: row persisted"


def _post_bug(client, pid, name="Code-review: trigger_notify IDOR", **extra):
    body = {"product_id": pid, "name": name, "description": _AC,
            "source": "ai", "feature_type": "bug"}
    body.update(extra)
    return client.post("/api/features", json=body)


def test_ai_bug_autoroutes_to_hardening_phase(client, db):
    p = make_product(db)
    r = _post_bug(client, p.id)
    assert r.status_code == 201, r.text
    phase_id = r.json()["phase_id"]
    assert phase_id is not None
    ph = db.execute(select(Phase).where(Phase.id == phase_id)).scalar_one()
    assert ph.name == "Code-Review Hardening"
    assert ph.order == 0
    assert ph.product_id == p.id


def test_hardening_phase_is_reused_idempotent(client, db):
    p = make_product(db)
    r1 = _post_bug(client, p.id, name="Code-review: IDOR one")
    r2 = _post_bug(client, p.id, name="Security: SQLi two")
    assert r1.json()["phase_id"] == r2.json()["phase_id"]
    # exactly one such phase exists for the product
    phases = db.execute(
        select(Phase).where(Phase.product_id == p.id,
                            Phase.name == "Code-Review Hardening")
    ).scalars().all()
    assert len(phases) == 1


def test_ai_feature_is_not_routed(client, db):
    p = make_product(db)
    r = client.post("/api/features", json={
        "product_id": p.id, "name": "ai story", "description": _AC,
        "source": "ai", "feature_type": "feature"})
    assert r.status_code == 201, r.text
    assert r.json()["phase_id"] is None


def test_ai_chore_is_not_routed(client, db):
    p = make_product(db)
    r = client.post("/api/features", json={
        "product_id": p.id, "name": "drift chore", "description": _AC,
        "source": "ai", "feature_type": "chore"})
    assert r.status_code == 201, r.text
    assert r.json()["phase_id"] is None


def test_explicit_phase_id_is_respected(client, db):
    p = make_product(db)
    ph = Phase(product_id=p.id, name="Custom", order=3)
    db.add(ph); db.flush()
    r = _post_bug(client, p.id, phase_id=ph.id)
    assert r.status_code == 201, r.text
    # caller pinned a phase → not overridden by the hardening route
    assert r.json()["phase_id"] == ph.id
