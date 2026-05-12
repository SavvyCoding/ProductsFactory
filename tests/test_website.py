"""
Phase 2 — PM Website tests.
Uses FastAPI TestClient (HTTPX) + real PostgreSQL via rolled-back transactions.

Run:
  TEST_DATABASE_URL=postgresql://user:pass@localhost/productfactory_test pytest tests/test_website.py -v

Auth note: all PM routes use HTTP Basic Auth (admin / testpassword).
Set PM_USERNAME=admin PM_PASSWORD=testpassword in env before running.
"""

import os
import pytest
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Patch DATABASE_URL before importing app so engine initialises correctly
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
os.environ.setdefault("PM_USERNAME", "admin")
os.environ.setdefault("PM_PASSWORD", "testpassword")

from website.main import app
from website.models import Base, Product, Feature, Session as DBSession, Alert
from website.database import get_db

# ── Test DB setup (sync engine, overrides async dependency) ──────────────────

_sync_url = (
    os.environ.get("TEST_DATABASE_URL", "")
    .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    .replace("postgresql://", "postgresql+psycopg2://")
)


@pytest.fixture(scope="session")
def test_engine():
    if not _sync_url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_engine(_sync_url)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def db(test_engine):
    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def client(db):
    """TestClient with DB dependency overridden to use the rolled-back sync session."""
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


AUTH = ("admin", "testpassword")


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_product(db, working_dir="/projects/test-app", **kwargs) -> Product:
    p = Product(working_dir=working_dir, status="ready", **kwargs)
    db.add(p)
    db.flush()
    return p


def make_feature(db, product_id, name="user-login", status="Pending", **kwargs) -> Feature:
    f = Feature(product_id=product_id, name=name, status=status, **kwargs)
    db.add(f)
    db.flush()
    return f


# ══════════════════════════════════════════════════════════════════════════════
# DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

class TestDashboard:
    def test_requires_auth(self, client):
        r = client.get("/", auth=None)
        assert r.status_code == 401

    def test_empty_dashboard(self, client):
        r = client.get("/", auth=AUTH)
        assert r.status_code == 200
        assert "No products" in r.text

    def test_shows_registered_products(self, client, db):
        make_product(db, "/projects/alpha", name="Alpha App")
        r = client.get("/", auth=AUTH)
        assert r.status_code == 200
        assert "Alpha App" in r.text

    def test_shows_multiple_products(self, client, db):
        make_product(db, "/projects/a", name="App A")
        make_product(db, "/projects/b", name="App B")
        r = client.get("/", auth=AUTH)
        assert "App A" in r.text
        assert "App B" in r.text


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT REGISTRATION (HTML form)
# ══════════════════════════════════════════════════════════════════════════════

class TestRegisterProduct:
    def test_register_positive(self, client):
        r = client.post("/product/register",
                        data={"working_dir": "/projects/new-app"},
                        auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        assert "/product/" in r.headers["location"]

    def test_register_duplicate_rejected(self, client, db):
        make_product(db, "/projects/dup")
        r = client.post("/product/register",
                        data={"working_dir": "/projects/dup"},
                        auth=AUTH)
        assert r.status_code == 409

    def test_register_requires_auth(self, client):
        r = client.post("/product/register",
                        data={"working_dir": "/projects/x"},
                        follow_redirects=False)
        assert r.status_code == 401

    def test_register_missing_working_dir(self, client):
        r = client.post("/product/register", data={}, auth=AUTH)
        assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# PRODUCT DETAIL PAGE
# ══════════════════════════════════════════════════════════════════════════════

class TestProductDetail:
    def test_shows_kanban(self, client, db):
        p = make_product(db, name="My Product")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert r.status_code == 200
        assert "My Product" in r.text
        assert "Pending" in r.text

    def test_shows_features(self, client, db):
        p = make_product(db)
        make_feature(db, p.id, name="user-signup", status="Pending")
        make_feature(db, p.id, name="password-reset", status="Approved")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "user-signup" in r.text
        assert "password-reset" in r.text

    def test_shows_pr_url(self, client, db):
        p = make_product(db)
        make_feature(db, p.id, name="feat", status="Pushed",
                     pr_url="https://github.com/x/y/pull/1", pr_number=1)
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "PR #1" in r.text

    def test_shows_blocked_reason(self, client, db):
        p = make_product(db)
        make_feature(db, p.id, name="feat", status="Blocked",
                     blocked_reason="Test suite fails due to missing env var")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "missing env var" in r.text

    def test_404_for_unknown_product(self, client):
        r = client.get("/product/99999", auth=AUTH)
        assert r.status_code == 404

    def test_pause_button_visible_when_ready(self, client, db):
        p = make_product(db, status="ready")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "Pause" in r.text

    def test_resume_button_visible_when_paused(self, client, db):
        p = make_product(db, status="paused")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "Resume" in r.text

    def test_analysis_run_button_visible_for_brownfield(self, client, db):
        p = make_product(db, type="brownfield", analysis_status="pending")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "Run Analysis" in r.text

    def test_analysis_run_button_hidden_after_done(self, client, db):
        p = make_product(db, type="brownfield", analysis_status="done")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "Run Analysis" not in r.text


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE MANAGEMENT (HTML forms)
# ══════════════════════════════════════════════════════════════════════════════

class TestAddFeature:
    def test_add_positive(self, client, db):
        p = make_product(db)
        r = client.post(f"/product/{p.id}/features",
                        data={"name": "new feature", "description": "desc", "priority": "30"},
                        auth=AUTH, follow_redirects=False)
        assert r.status_code == 303

    def test_add_missing_name(self, client, db):
        p = make_product(db)
        r = client.post(f"/product/{p.id}/features",
                        data={"description": "no name"},
                        auth=AUTH)
        assert r.status_code == 422

    def test_add_to_unknown_product(self, client):
        r = client.post("/product/99999/features",
                        data={"name": "feat"},
                        auth=AUTH)
        assert r.status_code == 404


class TestFeatureStatusChange:
    def test_pending_to_approved(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Pending")
        r = client.post(f"/product/{p.id}/features/{f.id}/status",
                        data={"status": "Approved"},
                        auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        db.refresh(f)
        assert f.status == "Approved"

    def test_approved_to_pending(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Approved")
        r = client.post(f"/product/{p.id}/features/{f.id}/status",
                        data={"status": "Pending"},
                        auth=AUTH, follow_redirects=False)
        assert r.status_code == 303

    def test_blocked_to_approved_resets_fix_attempts(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked", fix_attempts=3)
        client.post(f"/product/{p.id}/features/{f.id}/status",
                    data={"status": "Approved"},
                    auth=AUTH)
        db.refresh(f)
        assert f.fix_attempts == 0

    def test_invalid_transition_rejected(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing")
        r = client.post(f"/product/{p.id}/features/{f.id}/status",
                        data={"status": "Pushed"},
                        auth=AUTH)
        assert r.status_code == 422

    def test_transition_requires_auth(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Pending")
        r = client.post(f"/product/{p.id}/features/{f.id}/status",
                        data={"status": "Approved"})
        assert r.status_code == 401


class TestPauseResume:
    def test_pause_ready_product(self, client, db):
        p = make_product(db, status="ready")
        client.post(f"/product/{p.id}/pause", auth=AUTH)
        db.refresh(p)
        assert p.status == "paused"

    def test_resume_paused_product(self, client, db):
        p = make_product(db, status="paused")
        client.post(f"/product/{p.id}/resume", auth=AUTH)
        db.refresh(p)
        assert p.status == "ready"

    def test_pause_already_paused_is_noop(self, client, db):
        p = make_product(db, status="paused")
        client.post(f"/product/{p.id}/pause", auth=AUTH)
        db.refresh(p)
        assert p.status == "paused"


class TestTriggerAnalysis:
    def test_trigger_brownfield(self, client, db):
        p = make_product(db, type="brownfield", analysis_status="pending", status="discovered")
        r = client.post(f"/product/{p.id}/trigger_analysis", auth=AUTH, follow_redirects=False)
        assert r.status_code == 303
        db.refresh(p)
        assert p.analysis_status == "running"
        assert p.status == "ready"

    def test_trigger_greenfield_rejected(self, client, db):
        p = make_product(db, type="greenfield")
        r = client.post(f"/product/{p.id}/trigger_analysis", auth=AUTH)
        assert r.status_code == 422

    def test_trigger_while_running_rejected(self, client, db):
        p = make_product(db, type="brownfield", analysis_status="running")
        r = client.post(f"/product/{p.id}/trigger_analysis", auth=AUTH)
        assert r.status_code == 409


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Products
# ══════════════════════════════════════════════════════════════════════════════

class TestApiProducts:
    def test_list_empty(self, client):
        r = client.get("/api/products")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_returns_products(self, client, db):
        make_product(db, "/projects/api-test")
        r = client.get("/api/products")
        assert len(r.json()) == 1
        assert r.json()[0]["working_dir"] == "/projects/api-test"

    def test_create_product_json(self, client):
        r = client.post("/api/products", json={"working_dir": "/projects/json-new"}, auth=AUTH)
        assert r.status_code == 201
        assert r.json()["status"] == "registered"

    def test_update_product(self, client, db):
        p = make_product(db)
        r = client.patch(f"/api/products/{p.id}",
                         json={"name": "Updated Name", "tech_stack": ["python"]})
        assert r.status_code == 200
        assert r.json()["name"] == "Updated Name"
        assert r.json()["tech_stack"] == ["python"]

    def test_update_unknown_product(self, client):
        r = client.patch("/api/products/99999", json={"name": "x"})
        assert r.status_code == 404


class TestApiNextProduct:
    def test_returns_none_when_no_products(self, client):
        r = client.get("/api/products/next")
        assert r.status_code == 200
        assert r.json() is None

    def test_returns_none_when_no_approved_features(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Pending")
        r = client.get("/api/products/next")
        assert r.json() is None

    def test_returns_product_with_approved_feature(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Approved")
        r = client.get("/api/products/next")
        assert r.json()["id"] == p.id

    def test_respects_last_run_at_order(self, client, db):
        older = make_product(db, "/projects/older", status="ready",
                             last_run_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        newer = make_product(db, "/projects/newer", status="ready",
                             last_run_at=datetime(2026, 3, 1, tzinfo=timezone.utc))
        make_feature(db, older.id, status="Approved")
        make_feature(db, newer.id, status="Approved")
        r = client.get("/api/products/next")
        assert r.json()["id"] == older.id

    def test_null_last_run_at_comes_first(self, client, db):
        never_run = make_product(db, "/projects/never", status="ready", last_run_at=None)
        ran = make_product(db, "/projects/ran", status="ready",
                           last_run_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        make_feature(db, never_run.id, status="Approved")
        make_feature(db, ran.id, status="Approved")
        r = client.get("/api/products/next")
        assert r.json()["id"] == never_run.id

    def test_skips_paused_products(self, client, db):
        p = make_product(db, status="paused")
        make_feature(db, p.id, status="Approved")
        r = client.get("/api/products/next")
        assert r.json() is None


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Features
# ══════════════════════════════════════════════════════════════════════════════

class TestApiFeatures:
    def test_get_approved_features(self, client, db):
        p = make_product(db)
        make_feature(db, p.id, "feat-a", status="Approved", priority=10)
        make_feature(db, p.id, "feat-b", status="Approved", priority=20)
        make_feature(db, p.id, "feat-c", status="Pending")
        r = client.get(f"/api/features/approved?product_id={p.id}")
        assert r.status_code == 200
        names = [f["name"] for f in r.json()]
        assert names == ["feat-a", "feat-b"]  # priority order, Pending excluded

    def test_create_feature_json(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id, "name": "json-feature"})
        assert r.status_code == 201
        assert r.json()["status"] == "Pending"

    def test_create_ai_feature(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id, "name": "ai-feat", "source": "ai"})
        assert r.status_code == 201
        assert r.json()["source"] == "ai"

    def test_create_feature_invalid_source(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id, "name": "x", "source": "robot"})
        assert r.status_code == 422

    def test_story_size_cap_ac_bullets_under_limit(self, client, db):
        """4 AC bullets is at the cap → accepted."""
        p = make_product(db)
        desc = "\n".join(f"- AC {i}" for i in range(4))
        r = client.post("/api/features", json={
            "product_id": p.id, "name": "story", "source": "ai",
            "description": desc,
        })
        assert r.status_code == 201, r.text

    def test_story_size_cap_ac_bullets_over_limit(self, client, db):
        """5 AC bullets exceeds cap → 422 with explanatory message."""
        p = make_product(db)
        desc = "\n".join(f"- AC {i}" for i in range(5))
        r = client.post("/api/features", json={
            "product_id": p.id, "name": "story", "source": "ai",
            "description": desc,
        })
        assert r.status_code == 422
        assert "story too big" in r.text.lower()

    def test_story_size_cap_pm_bypass(self, client, db):
        """source=pm bypasses the cap (PM is the override authority)."""
        p = make_product(db)
        desc = "\n".join(f"- AC {i}" for i in range(20))
        r = client.post("/api/features", json={
            "product_id": p.id, "name": "story", "source": "pm",
            "description": desc,
        })
        assert r.status_code == 201, r.text

    def test_update_feature_status(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Approved")
        r = client.patch(f"/api/features/{f.id}",
                         json={"status": "Implementing"})
        assert r.status_code == 200
        assert r.json()["status"] == "Implementing"

    def test_update_with_blocked_reason(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Testing")
        r = client.patch(f"/api/features/{f.id}",
                         json={"status": "Blocked", "blocked_reason": "DB not available in CI"})
        assert r.status_code == 200
        assert r.json()["blocked_reason"] == "DB not available in CI"

    def test_update_unknown_feature(self, client):
        r = client.patch("/api/features/99999", json={"status": "Implementing"})
        assert r.status_code == 404

    def test_pm_status_update_valid(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Pending")
        r = client.patch(f"/api/features/{f.id}/pm-status",
                         json={"status": "Approved"}, auth=AUTH)
        assert r.status_code == 200
        assert r.json()["status"] == "Approved"

    def test_pm_status_update_invalid_transition(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing")
        r = client.patch(f"/api/features/{f.id}/pm-status",
                         json={"status": "Pushed"}, auth=AUTH)
        assert r.status_code == 422

    def test_pm_status_requires_auth(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Pending")
        r = client.patch(f"/api/features/{f.id}/pm-status", json={"status": "Approved"})
        assert r.status_code == 401


class TestResetStuck:
    def test_resets_stale_implementing_to_approved(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing")  # no design_doc_path → Approved
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.status_code == 200
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Approved"

    def test_resets_stale_implementing_to_designed_when_has_design_doc(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing", design_doc_path="docs/feature_001_design.md")
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Designed"

    def test_resets_stale_designing(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Designing")
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Approved"

    def test_resets_stale_reviewing(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=42)
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Implementing"

    def test_resets_stale_implemented_with_pr_to_reviewing(self, client, db):
        # post-coder failure: agent wrote Implemented but the PATCH to Reviewing
        # never ran. PR was already pushed, so promote forward.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", pr_number=42)
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Reviewing"

    def test_resets_stale_implemented_no_pr_to_designed_when_design_doc(self, client, db):
        # Agent wrote Implemented, no PR pushed, design doc exists → Designed
        # (re-pickable by coder via Designed branch of next-for-persona).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", design_doc_path="docs/story_1.md")
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Designed"

    def test_resets_stale_implemented_no_pr_to_approved_when_no_design_doc(self, client, db):
        # Agent wrote Implemented, no PR, no design doc → Approved (designer retries).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented")
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Approved"

    def test_does_not_reset_recent_implementing(self, client, db):
        p = make_product(db)
        make_feature(db, p.id, status="Implementing")  # updated_at = now
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 0

    def test_does_not_affect_other_statuses(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked")
        db.execute(
            __import__("sqlalchemy").text(
                "UPDATE features SET updated_at = NOW() - INTERVAL '3 hours' WHERE id = :id"
            ),
            {"id": f.id}
        )
        db.flush()
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 0
        db.refresh(f)
        assert f.status == "Blocked"


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Sessions
# ══════════════════════════════════════════════════════════════════════════════

class TestPersonaRouting:
    def test_designer_picks_approved_with_no_design_doc(self, client, db):
        p = make_product(db, status="ready")
        f = make_feature(db, p.id, status="Approved")
        r = client.get(f"/api/features/next-for-persona?persona=designer&product_id={p.id}")
        assert r.status_code == 200
        assert r.json()["id"] == f.id

    def test_designer_skips_approved_with_design_doc(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Approved", design_doc_path="docs/feature_001_design.md")
        r = client.get(f"/api/features/next-for-persona?persona=designer&product_id={p.id}")
        assert r.json() is None

    def test_coder_picks_designed(self, client, db):
        p = make_product(db, status="ready")
        f = make_feature(db, p.id, status="Designed")
        r = client.get(f"/api/features/next-for-persona?persona=coder&product_id={p.id}")
        assert r.status_code == 200
        assert r.json()["id"] == f.id

    def test_coder_picks_approved_with_design_doc(self, client, db):
        p = make_product(db, status="ready")
        f = make_feature(db, p.id, status="Approved", design_doc_path="docs/feature_001_design.md")
        r = client.get(f"/api/features/next-for-persona?persona=coder&product_id={p.id}")
        assert r.json()["id"] == f.id

    def test_coder_skips_approved_without_design_doc(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Approved")
        r = client.get(f"/api/features/next-for-persona?persona=coder&product_id={p.id}")
        assert r.json() is None

    def test_reviewer_picks_reviewing_with_pr(self, client, db):
        p = make_product(db, status="ready")
        f = make_feature(db, p.id, status="Reviewing", pr_number=5)
        r = client.get(f"/api/features/next-for-persona?persona=reviewer&product_id={p.id}")
        assert r.json()["id"] == f.id

    def test_reviewer_skips_reviewing_without_pr(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Reviewing")  # no pr_number
        r = client.get(f"/api/features/next-for-persona?persona=reviewer&product_id={p.id}")
        assert r.json() is None

    def test_unknown_persona_rejected(self, client, db):
        p = make_product(db)
        r = client.get(f"/api/features/next-for-persona?persona=hacker&product_id={p.id}")
        assert r.status_code == 422

    def test_next_product_includes_designed_features(self, client, db):
        p = make_product(db, status="ready")
        make_feature(db, p.id, status="Designed")  # not Approved — should still trigger
        r = client.get("/api/products/next")
        assert r.json()["id"] == p.id


    def test_feature_update_stores_design_doc(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Designing")
        r = client.patch(f"/api/features/{f.id}", json={
            "status": "Designed",
            "design_doc_path": "docs/feature_001_design.md",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "Designed"
        assert r.json()["design_doc_path"] == "docs/feature_001_design.md"

    def test_feature_update_stores_review_outcome(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=10)
        r = client.patch(f"/api/features/{f.id}", json={
            "status": "Reviewed",
            "review_outcome": "approved",
            "review_notes": "LGTM",
        })
        assert r.status_code == 200
        assert r.json()["review_outcome"] == "approved"


class TestApiSessions:
    def test_start_session(self, client, db):
        p = make_product(db)
        r = client.post("/api/sessions",
                        json={"product_id": p.id, "session_uid": "uid-abc"})
        assert r.status_code == 201
        assert r.json()["session_uid"] == "uid-abc"

    def test_end_session(self, client, db):
        p = make_product(db)
        create_r = client.post("/api/sessions",
                               json={"product_id": p.id, "session_uid": "uid-end"})
        session_id = create_r.json()["id"]
        r = client.patch(f"/api/sessions/{session_id}",
                         json={"exit_code": 0, "features_pushed": 2})
        assert r.status_code == 200
        assert r.json()["exit_code"] == 0
        assert r.json()["features_pushed"] == 2

    def test_duplicate_session_uid_rejected(self, client, db):
        p = make_product(db)
        client.post("/api/sessions", json={"product_id": p.id, "session_uid": "dup"})
        r = client.post("/api/sessions", json={"product_id": p.id, "session_uid": "dup"})
        assert r.status_code in (409, 500)  # DB unique constraint

    def test_end_unknown_session(self, client):
        r = client.patch("/api/sessions/99999", json={"exit_code": 0})
        assert r.status_code == 404


class TestFeatureReviews:
    def test_review_recorded_on_feature_patch(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=10)
        client.patch(f"/api/features/{f.id}", json={
            "status": "Reviewed",
            "review_outcome": "approved",
            "review_notes": "LGTM",
        })
        r = client.get(f"/api/features/{f.id}/reviews")
        assert r.status_code == 200
        reviews = r.json()
        assert len(reviews) == 1
        assert reviews[0]["review_outcome"] == "approved"
        assert reviews[0]["review_notes"] == "LGTM"

    def test_multiple_review_cycles_preserved(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=11)
        # First review: request changes
        client.patch(f"/api/features/{f.id}", json={
            "status": "Implementing",
            "review_outcome": "changes_requested",
            "review_notes": "Missing tests",
        })
        # Second review: approved
        client.patch(f"/api/features/{f.id}", json={
            "status": "Reviewed",
            "review_outcome": "approved",
            "review_notes": "LGTM now",
        })
        r = client.get(f"/api/features/{f.id}/reviews")
        assert r.status_code == 200
        reviews = r.json()
        assert len(reviews) == 2
        # Newest first
        assert reviews[0]["review_outcome"] == "approved"
        assert reviews[1]["review_outcome"] == "changes_requested"

    def test_patch_without_review_outcome_creates_no_review(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing")
        client.patch(f"/api/features/{f.id}", json={"status": "Reviewing", "pr_number": 12})
        r = client.get(f"/api/features/{f.id}/reviews")
        assert r.status_code == 200
        assert r.json() == []

    def test_review_history_404_for_unknown_feature(self, client):
        r = client.get("/api/features/99999/reviews")
        assert r.status_code == 404

    def test_session_uid_stored_with_review(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=13)
        client.patch(f"/api/features/{f.id}", json={
            "status": "Reviewed",
            "review_outcome": "approved",
            "review_notes": "ok",
            "session_uid": "test-abc123",
        })
        r = client.get(f"/api/features/{f.id}/reviews")
        assert r.json()[0]["session_uid"] == "test-abc123"
