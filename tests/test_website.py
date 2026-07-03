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

from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

# Patch DATABASE_URL before importing app so engine initialises correctly.
# Force-override PM_USERNAME/PM_PASSWORD: CI sets them to ci_admin/ci_test_password,
# but every test in this file uses AUTH=("admin", "testpassword"). website.auth
# reads these env vars at module import, so we must set them before the
# website.* imports below.
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", ""))
os.environ["PM_USERNAME"] = "admin"
os.environ["PM_PASSWORD"] = "testpassword"

from website.main import app
from website.models import Base, Product, Feature, Session as DBSession, Alert, Phase
from website.database import get_db
from website.auth import _reset_rate_limit_state_for_tests

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


class _AsyncSessionFacade:
    """
    Test-only async wrapper over a sync sqlalchemy.orm.Session.

    The website code is built on AsyncSession (asyncpg) and awaits methods like
    ``execute``, ``get``, ``flush``, ``commit``, ``rollback``, ``refresh``,
    ``delete``. The test harness drives the app through the sync ``TestClient``
    on a single rolled-back transaction, so we keep a real sync ``Session``
    underneath and expose the async surface as zero-cost coroutine adapters.

    ``add`` / ``add_all`` are sync on AsyncSession too — passed through verbatim.
    """

    def __init__(self, sync_session):
        self._s = sync_session

    async def execute(self, *a, **kw):
        return self._s.execute(*a, **kw)

    async def scalar(self, *a, **kw):
        return self._s.scalar(*a, **kw)

    async def scalars(self, *a, **kw):
        return self._s.scalars(*a, **kw)

    async def get(self, *a, **kw):
        return self._s.get(*a, **kw)

    async def flush(self, *a, **kw):
        return self._s.flush(*a, **kw)

    async def commit(self):
        # Tests run inside a single outer transaction that is rolled back at
        # teardown; a real commit would end that transaction and leak rows
        # into the test database. Treat commit as flush — the website's
        # in-request consistency still holds, and isolation is preserved.
        return self._s.flush()

    async def rollback(self):
        return self._s.rollback()

    async def refresh(self, *a, **kw):
        return self._s.refresh(*a, **kw)

    async def delete(self, instance):
        return self._s.delete(instance)

    def add(self, instance):
        return self._s.add(instance)

    def add_all(self, instances):
        return self._s.add_all(instances)


@pytest.fixture
def client(db):
    """TestClient with DB dependency overridden to use the rolled-back sync
    session, wrapped in an async facade so the website's ``await db.execute()``
    style code can run unchanged against it.

    Lifecycle mirrors website.database.get_db: commit (= flush, in tests) on
    request success, rollback on exception. Without this, attribute mutations
    made inside an endpoint (e.g. ``product.status = "paused"``) never reach
    the connection's transaction and subsequent ``db.refresh()`` reads return
    the pre-mutation value.

    Also clears the in-memory auth rate-limiter — without this, a few
    intentional-401 tests trip AUTH_MAX_FAILS and every later test in the
    session sees 429 Too Many Requests."""
    _reset_rate_limit_state_for_tests()
    facade = _AsyncSessionFacade(db)

    async def override_get_db():
        try:
            yield facade
            await facade.commit()
        except HTTPException:
            # HTTPException is FastAPI's normal way of returning 4xx — not a
            # transactional error. The real get_db rolls back on it, but in
            # tests rolling back would detach the test-held setup objects
            # (e.g. ``p = make_product(db); client.post(...) -> 404;
            # db.refresh(p)`` would then raise "not persistent within this
            # Session"). Treat HTTPException as a normal exit.
            raise
        except Exception:
            await facade.rollback()
            raise

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


AUTH = ("admin", "testpassword")


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_product(db, working_dir="/projects/test-app", **kwargs) -> Product:
    kwargs.setdefault("status", "ready")
    p = Product(working_dir=working_dir, **kwargs)
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
        # The feature row renders the PR as a link to the pr_url labelled "#<n>".
        assert "https://github.com/x/y/pull/1" in r.text
        assert "#1" in r.text

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
        # The button is now an icon action; identify it by its trigger_analysis
        # form action and "Run analysis" tooltip/aria-label.
        assert "trigger_analysis" in r.text
        assert "Run analysis" in r.text

    def test_analysis_run_button_hidden_after_done(self, client, db):
        p = make_product(db, type="brownfield", analysis_status="done")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert "Run Analysis" not in r.text


class TestLifetimeAggregates:
    """Regression for the 2026-05-30 'Tokens lifetime' shrinking bug.

    Pre-fix: the product_detail route LIMIT'd the sessions query at 50; the
    template then summed tokens_input/tokens_output across that slice for
    the metric cards labeled "Tokens lifetime" / "Sessions lifetime" /
    "Session success rate." As new sessions landed, older ones fell off
    the window and their tokens vanished from the displayed total — a
    monotonically-increasing lifetime metric was actually shrinking.

    Post-fix: lifetime aggregates come from a separate unbounded query.
    These tests create 60+ sessions (well past the 50 cap) with known
    token counts and assert the rendered HTML reflects the FULL totals.
    """

    def _make_session(self, db, product_id, *, uid, exit_code=0,
                      tokens_input=100_000, tokens_output=10_000,
                      ended=True):
        # Both started_at and ended_at are set explicitly so the duration
        # is 5 min — well past the 10s ghost-session threshold. Without
        # this, started_at defaults to func.now() (test wall-clock) and a
        # fixed ended_at lands BEFORE it, making (ended_at - started_at)
        # negative and matching the < 10s ghost predicate, which silently
        # filters every failed session out of the lifetime aggregate.
        started = datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc)
        ended_at = (datetime(2026, 5, 30, 12, 5, tzinfo=timezone.utc)
                    if ended else None)
        s = DBSession(
            product_id=product_id,
            session_uid=uid,
            started_at=started,
            ended_at=ended_at,
            exit_code=exit_code,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
        db.add(s)
        db.flush()
        return s

    def test_tokens_lifetime_includes_sessions_past_limit_50(self, client, db):
        # Pre-fix: only the most-recent 50 sessions contributed to the
        # displayed total. With 60 sessions × 100k input + 10k output =
        # 6.6M tokens; LIMIT(50) would show 5.5M; the missing 1.1M is the
        # bug. The "X in" foot text renders with comma-formatting, so we
        # can grep for the exact byte string.
        p = make_product(db)
        for i in range(60):
            self._make_session(db, p.id, uid=f"u{i:03d}")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert r.status_code == 200
        # Full lifetime input total: 60 × 100,000 = 6,000,000
        assert "6,000,000 in" in r.text, (
            "tokens_input lifetime total must include all 60 sessions, "
            "not just the LIMIT(50) display slice. Look for the 'X in' "
            "foot text on the Tokens lifetime card."
        )
        # Full lifetime output: 60 × 10,000 = 600,000
        assert "600,000 out" in r.text

    def test_sessions_lifetime_count_includes_past_limit_50(self, client, db):
        p = make_product(db)
        for i in range(75):
            self._make_session(db, p.id, uid=f"u{i:03d}")
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert r.status_code == 200
        # The "Sessions lifetime" metric card renders the count as the
        # metric value. With 75 sessions, the page must contain "75",
        # not "50" (the LIMIT cap).
        # Use the foot text format which is unique: "X OK · Y killed"
        # to anchor the assertion away from incidental numbers.
        assert "75 OK · 0 killed" in r.text

    def test_session_success_rate_uses_lifetime_not_slice(self, client, db):
        # 60 OK sessions + 40 killed sessions → 100 total, 60% success.
        # Pre-fix: slice was the 50 most-recent (whatever mix of OK/killed
        # those happened to be, depending on insertion order) which gave
        # the WRONG ratio. Post-fix: 60/100 = 60%, stable regardless of
        # what's in the LIMIT(50) display slice.
        p = make_product(db)
        for i in range(60):
            self._make_session(db, p.id, uid=f"ok-{i:03d}", exit_code=0)
        for i in range(40):
            self._make_session(db, p.id, uid=f"kill-{i:03d}", exit_code=137)
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert r.status_code == 200
        # The metric card renders the percentage as the value and the
        # supporting count in foot text "across N runs". 60/100 = 60.
        assert "across 100 runs" in r.text
        # And the percentage must appear — guard against ratio computed
        # off the slice. Format from the template: {{ ok_pct }}<suffix>%
        # so look for ">60<" inside the metric-value span.
        assert ">60<" in r.text

    def test_ghost_sessions_excluded_from_lifetime(self, client, db):
        # Ghost sessions (failed AND ended within 10s of start) are
        # excluded by _ghost_filter in BOTH the display query and the
        # lifetime aggregate. Without this, infra crashes would inflate
        # killed counts and depress success rate. 5 real OK + 3 ghosts
        # ⇒ lifetime shows 5 sessions, 100% success.
        p = make_product(db)
        for i in range(5):
            self._make_session(db, p.id, uid=f"real-{i:03d}")
        # Insert ghost sessions: failed (exit != 0), ended_at - started_at < 10s
        for i in range(3):
            g = DBSession(
                product_id=p.id,
                session_uid=f"ghost-{i}",
                exit_code=125,
                started_at=datetime(2026, 5, 30, 12, 0, tzinfo=timezone.utc),
                ended_at  =datetime(2026, 5, 30, 12, 0, 5, tzinfo=timezone.utc),
            )
            db.add(g)
        db.flush()
        r = client.get(f"/product/{p.id}", auth=AUTH)
        assert r.status_code == 200
        # 5 real, all OK
        assert "5 OK · 0 killed" in r.text


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
        # /api/features/approved is the coder-eligibility feed: a feature is
        # returned only when it's Designed, or Approved *with a design doc*
        # already written. A bare Approved feature (no design doc) is the
        # designer's queue, not the coder's, so it's excluded. Pending is
        # always excluded.
        p = make_product(db)
        make_feature(db, p.id, "feat-a", status="Designed", priority=10)
        make_feature(db, p.id, "feat-b", status="Approved", priority=20,
                     design_doc_path="docs/feat-b.md")
        make_feature(db, p.id, "feat-c", status="Approved", priority=5)  # no design doc → excluded
        make_feature(db, p.id, "feat-d", status="Pending")
        r = client.get(f"/api/features/approved?product_id={p.id}")
        assert r.status_code == 200
        names = [f["name"] for f in r.json()]
        assert names == ["feat-a", "feat-b"]  # priority order, undesigned + Pending excluded

    def test_create_feature_json(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id,
                              "name": "json-feature",
                              "description": "A test feature for the JSON path."})
        assert r.status_code == 201
        assert r.json()["status"] == "Pending"

    def test_create_ai_feature(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id,
                              "name": "ai-feat",
                              "description": "AI-generated feature description.",
                              "source": "ai"})
        assert r.status_code == 201
        assert r.json()["source"] == "ai"

    def test_create_feature_invalid_source(self, client, db):
        p = make_product(db)
        r = client.post("/api/features",
                        json={"product_id": p.id,
                              "name": "Valid feature name",
                              "description": "Description long enough for validation.",
                              "source": "robot"})
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
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.status_code == 200
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Approved"

    def test_resets_stale_implementing_to_designed_when_has_design_doc(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing", design_doc_path="docs/feature_001_design.md")
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Designed"

    def test_resets_stale_designing(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Designing")
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Approved"

    def test_resets_stale_reviewing(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Reviewing", pr_number=42)
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Implementing"

    def test_resets_stale_implemented_with_pr_to_reviewing(self, client, db):
        # post-coder failure: agent wrote Implemented but the PATCH to Reviewing
        # never ran. PR was already pushed, so promote forward.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", pr_number=42)
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Reviewing"

    def test_resets_stale_implemented_no_pr_to_designed_when_design_doc(self, client, db):
        # Agent wrote Implemented, no PR pushed, design doc exists → Designed
        # (re-pickable by coder via Designed branch of next-for-persona).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", design_doc_path="docs/story_1.md")
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Designed"

    def test_resets_stale_implemented_no_pr_to_approved_when_no_design_doc(self, client, db):
        # Agent wrote Implemented, no PR, no design doc → Approved (designer retries).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented")
        self._age_feature(db, f.id, "3 hours")
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
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 0
        db.refresh(f)
        assert f.status == "Blocked"

    # ── 2026-06-30 RCA: don't race the live post-coder pipeline ──────────────
    def _coder_session(self, db, product_id, *, status, uid="cs"):
        s = DBSession(product_id=product_id, session_uid=uid, persona="coder",
                      status=status,
                      started_at=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc))
        db.add(s); db.flush(); return s

    def _age_feature(self, db, fid, interval):
        """Backdate a feature's updated_at by `interval` ('3 hours', '7 minutes').

        The previous raw `UPDATE ... updated_at = NOW() - INTERVAL + db.flush()`
        passed locally but found 0 stale rows in CI (all reset_count==1 asserts
        got 0). Root cause: the trailing ORM flush re-stamped updated_at back to
        now() via the column's onupdate=func.now(). This version is CI-robust:
        a Core UPDATE with an EXPLICIT updated_at value (onupdate only fills
        columns NOT in the SET, so an explicit value wins), computed off the same
        aware-UTC clock the endpoint's cutoff uses, and no trailing flush —
        expire_all() forces the endpoint (same session) to re-read the row.
        """
        import sqlalchemy as sa
        # features carries a BEFORE UPDATE trigger (trg_features_updated →
        # set_updated_at(), migration 001) that forces updated_at=now() on EVERY
        # update. It exists in the alembic-built schema (CI) but NOT the
        # create_all schema (local test_engine), so a plain backdating UPDATE was
        # silently reset to now() in CI — every reset_count==1 assert saw 0 while
        # passing locally. Disable user triggers on the table for just this
        # UPDATE so the backdate sticks; DISABLE/ENABLE TRIGGER USER is a no-op
        # where the trigger is absent, so this is schema-agnostic. Raw SQL (not
        # sa.update) so the ORM's onupdate=func.now() doesn't re-stamp it either.
        db.execute(sa.text("ALTER TABLE features DISABLE TRIGGER USER"))
        db.execute(
            sa.text(f"UPDATE features SET updated_at = NOW() - INTERVAL '{interval}' WHERE id = :id"),
            {"id": fid},
        )
        db.execute(sa.text("ALTER TABLE features ENABLE TRIGGER USER"))
        db.expire_all()

    def test_skips_implemented_while_coder_session_active(self, client, db):
        # An Implemented feature whose product still has a live (wrapping) coder
        # session is in-flight — post-coder is finalizing it. Must NOT be rescued
        # mid-pipeline (that steals the transition / can advance past the gates).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", pr_number=42)
        self._coder_session(db, p.id, status="wrapping")
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 0
        db.refresh(f)
        assert f.status == "Implemented"          # left for post-coder

    def test_rescues_implemented_when_session_ended(self, client, db):
        # Same orphan, but the coder session has ENDED → genuinely orphaned →
        # rescued forward to Reviewing (PR exists). The guard is scoped to LIVE
        # sessions, so recovery of true orphans is unaffected.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", pr_number=42)
        self._coder_session(db, p.id, status="ended")
        self._age_feature(db, f.id, "3 hours")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 1
        db.refresh(f)
        assert f.status == "Reviewing"

    def test_implemented_under_10min_cutoff_not_reset(self, client, db):
        # Sat in Implemented 7 min (past the OLD 5-min cutoff, under the NEW
        # 10-min cutoff) with no active session → not yet rescued; post-coder
        # still has headroom for a slow suite.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", pr_number=42)
        self._age_feature(db, f.id, "7 minutes")
        r = client.post("/api/features/reset_stuck")
        assert r.json()["reset_count"] == 0
        db.refresh(f)
        assert f.status == "Implemented"


class TestBlockedQuarantine:
    """Regression suite for the 2026-05-27 Blocked-state inversion bug.

    Supervisor PATCHes feature status=Blocked on rapid-flap detection.
    The intent was: only PM can move OUT of Blocked. Previous logic had
    an `_exits_blocked` carve-out that actually permitted ANY non-PM
    caller to un-block as long as the PATCH carried a different status,
    which is exactly what post-coder:lint-guard does in its bounce
    path. Result: supervisor's Block on feature 973 lasted 5 seconds
    before post-coder reverted it, then 35 more bounce cycles ran on
    the unblocked feature.
    """

    def test_post_coder_lint_guard_cannot_unblock(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing", "changed_by": "post-coder:lint-guard"},
        )
        assert r.status_code == 422
        assert "Blocked" in r.json()["detail"]

    def test_supervisor_cannot_unblock(self, client, db):
        # Even the supervisor itself can't re-engage — only PM closes the loop.
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing", "changed_by": "supervisor"},
        )
        assert r.status_code == 422

    def test_pm_can_unblock(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing", "changed_by": "pm"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "Implementing"

    def test_blocked_rank_rejects_demote_as_downgrade(self, client, db):
        # Defense-in-depth: even if the Blocked-state guard above ever
        # regresses, Blocked is rank 7, so the rank guard catches the
        # demote independently. Bypass entries (rollback, kill_recovery,
        # etc.) still pass — but any new non-bypassed caller is rejected.
        p = make_product(db)
        f = make_feature(db, p.id, status="Blocked")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing", "changed_by": "some-new-agent"},
        )
        assert r.status_code == 422


class TestBlockedTransitionClosesPR:
    """Regression suite for the 2026-05-30 DocumentSign 7-open-PR incident.

    Both supervisor.detect_rapid_flap and supervisor.detect_divergent_review_feedback
    PATCH `status=Blocked` directly via /api/features/{id}, bypassing
    _close_blocked_feature_pr's two prior callsites (api_route_to_blocked_sprint
    + the fix_attempts cap inline path). Result: PRs accumulated open on
    Blocked features because neither detector closed them.

    Fix: api_update_feature is now the single chokepoint — any PATCH that
    transitions a feature into Blocked closes the open session PR and clears
    pr_number / pr_url / branch_name. Both detectors get it for free.
    """

    @pytest.fixture
    def stub_gh(self, monkeypatch):
        """Stub the website.main module-level close_pr import + force a
        non-empty GitHub token so _close_blocked_feature_pr proceeds past
        its config gates. Records (repo, pr_number) of every close call.
        """
        from website import main as web_main
        calls: list[tuple[str, int]] = []

        def fake_close_pr(github_repo, pr_number, token, reason=""):
            calls.append((github_repo, int(pr_number)))
            return True

        monkeypatch.setattr(web_main, "close_pr", fake_close_pr)
        monkeypatch.setattr(
            web_main, "_github_token_from_config",
            lambda _cfg: "fake-token",
        )
        return calls

    def test_supervisor_rapid_flap_patch_closes_pr(self, client, db, stub_gh):
        """detect_rapid_flap PATCH shape: status + blocked_reason, no
        pr_number clear. The route must still close the PR + clear all
        three link fields."""
        p = make_product(db, github_repo="org/repo")
        f = make_feature(
            db, p.id, status="Implementing", pr_number=21,
            pr_url="https://github.com/org/repo/pull/21",
            branch_name="coder/abc12345",
        )
        r = client.patch(
            f"/api/features/{f.id}",
            json={
                "status": "Blocked",
                "blocked_reason": "Auto-routed: rapid status flap loop",
                "changed_by": "supervisor",
            },
        )
        assert r.status_code == 200
        assert stub_gh == [("org/repo", 21)]
        db.refresh(f)
        assert f.status == "Blocked"
        assert f.pr_number is None
        assert f.pr_url is None
        assert f.branch_name is None

    def test_supervisor_divergent_feedback_patch_closes_pr(self, client, db, stub_gh):
        """detect_divergent_review_feedback PATCH shape: status +
        pr_number=None together. Without snapshotting pre-PATCH pr_number
        the chokepoint would see None and silently skip. Asserts the
        snapshot path closes the GitHub PR correctly."""
        p = make_product(db, github_repo="org/repo")
        f = make_feature(
            db, p.id, status="Implementing", pr_number=26,
            pr_url="https://github.com/org/repo/pull/26",
            branch_name="coder/def67890",
        )
        r = client.patch(
            f"/api/features/{f.id}",
            json={
                "status": "Blocked",
                "pr_number": None,
                "blocked_reason": "Auto-blocked: divergent cascade",
                "changed_by": "supervisor.divergent_review_feedback",
            },
        )
        assert r.status_code == 200
        assert stub_gh == [("org/repo", 26)]
        db.refresh(f)
        assert f.status == "Blocked"
        assert f.pr_number is None
        assert f.pr_url is None
        assert f.branch_name is None

    def test_no_pr_no_close_attempt(self, client, db, stub_gh):
        """Block-routing a feature with no open PR must not call close_pr
        (cheap-path; also avoids confusing log warnings about missing token
        on features that legitimately never opened a PR)."""
        p = make_product(db, github_repo="org/repo")
        f = make_feature(db, p.id, status="Approved")  # no pr_number
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Blocked", "blocked_reason": "manual",
                  "changed_by": "pm"},
        )
        assert r.status_code == 200
        assert stub_gh == []

    def test_already_blocked_idempotent(self, client, db, stub_gh):
        """Re-PATCHing a Blocked feature (e.g. PM updates blocked_reason)
        must NOT re-close — prev_status == Blocked guards against double
        close attempts on every subsequent edit of a Blocked feature."""
        p = make_product(db, github_repo="org/repo")
        f = make_feature(
            db, p.id, status="Blocked", pr_number=99,
            pr_url="https://github.com/org/repo/pull/99",
            blocked_reason="initial",
        )
        # Trying to update blocked_reason is rejected by the quarantine
        # guard for non-PM callers — use changed_by=pm.
        r = client.patch(
            f"/api/features/{f.id}",
            json={"blocked_reason": "PM edited", "changed_by": "pm"},
        )
        assert r.status_code == 200
        assert stub_gh == []


class TestImplementedBounceCircuitBreaker:
    """Fix #2 for the 2026-05-28 calc3 #1022 infinite loop.

    A post-coder lint bounce moves a feature Implemented→Implementing with
    review_outcome ALREADY changes_requested (never reset on re-claim), so it
    matched none of the website's fix_attempts triggers — the loop never
    auto-Blocked (fix_attempts stuck at 1 over ~12 bounces). Implemented→
    Implementing is now a rework trigger feeding the existing cap→auto-Block.
    """

    def test_implemented_to_implementing_bumps_fix_attempts(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", fix_attempts=1,
                         review_outcome="changes_requested")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing",
                  "review_outcome": "changes_requested",   # unchanged value
                  "changed_by": "post-coder:lint-guard"},
        )
        assert r.status_code == 200
        db.refresh(f)
        # Bumped despite review_outcome being unchanged (the old conditions
        # both missed this path).
        assert f.fix_attempts == 2

    def test_repeated_bounces_auto_block_at_cap(self, client, db):
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", fix_attempts=4,
                         review_outcome="changes_requested")
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing",
                  "review_outcome": "changes_requested",
                  "changed_by": "post-coder:lint-guard"},
        )
        assert r.status_code == 200
        db.refresh(f)
        assert f.fix_attempts == 5
        # Crossing the cap auto-Blocks, overriding the requested Implementing.
        assert f.status == "Blocked"
        assert f.blocked_reason

    def test_escalation_active_block_redirects_to_stuck(self, client, db):
        # An escalation_active feature that terminally Blocks BELOW the
        # fix_attempts cap (e.g. false_success) must NOT land in plain Blocked
        # (limbo: driver's one-shot marker skips re-escalation, Blocked hides
        # it from dispatch). It routes to Stuck. Canonical: HomeChoreService
        # #1659 — false_success block at fix_attempts=1 (2026-06-19).
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented",
                         escalation_active=True, fix_attempts=1)
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Blocked",
                  "blocked_reason": "Coder session X exited 0 with no code changes",
                  "changed_by": "supervisor"},
        )
        assert r.status_code == 200
        db.refresh(f)
        assert f.status == "Stuck"            # redirected, not Blocked
        assert f.escalation_active is False   # cleared → driver won't re-escalate
        assert "premium escalation failed" in (f.blocked_reason or "")

    def test_non_escalation_block_stays_blocked(self, client, db):
        # Guard: the redirect is scoped to escalation_active features only — a
        # normal feature still Blocks normally.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implemented", fix_attempts=1)
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Blocked", "blocked_reason": "x",
                  "changed_by": "supervisor"},
        )
        assert r.status_code == 200
        db.refresh(f)
        assert f.status == "Blocked"

    def test_normal_reclaim_does_not_bump(self, client, db):
        # Implementing→Implementing (coder re-claim) is not a bounce.
        p = make_product(db)
        f = make_feature(db, p.id, status="Implementing", fix_attempts=1)
        r = client.patch(
            f"/api/features/{f.id}",
            json={"status": "Implementing", "changed_by": "agent"},
        )
        assert r.status_code == 200
        db.refresh(f)
        assert f.fix_attempts == 1


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

    def test_unknown_persona_returns_no_work(self, client, db):
        # next-for-persona only assigns work for the three dispatch personas
        # (designer/coder/reviewer). Any other persona — maintenance personas
        # like documenter/security_auditor, or an unrecognised name — falls
        # through to the "discover your own work" branch and gets a null body
        # (200), never a match. There is deliberately no persona whitelist here.
        p = make_product(db)
        make_feature(db, p.id, status="Approved")  # would be dispatched to a real persona
        r = client.get(f"/api/features/next-for-persona?persona=hacker&product_id={p.id}")
        assert r.status_code == 200
        assert r.json() is None

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

    def test_terminal_state_guard_refuses_status_downgrade(self, client, db):
        """Once a session is killed/orphaned, the generic PATCH must not
        clobber status + exit_code back to ended/0 — that was the 2406 /
        2418 misleading-row bug. Non-status fields (e.g. features_pushed)
        still merge through so reconciliation can record activity even on
        a killed session.
        """
        # Create a session, mark it killed via the dedicated kill endpoint.
        p = make_product(db)
        create_r = client.post("/api/sessions",
                               json={"product_id": p.id, "session_uid": "uid-guard"})
        session_id = create_r.json()["id"]
        kill_r = client.post(
            f"/api/sessions/{session_id}/kill",
            json={"reason": "container exited (docker ps does not list it)"},
        )
        assert kill_r.status_code == 200

        # Now simulate the racing docker_runner finalize that tries to
        # write status=ended + exit_code=0 + features_pushed=2.
        patch_r = client.patch(
            f"/api/sessions/{session_id}",
            json={"status": "ended", "exit_code": 0, "features_pushed": 2},
        )
        assert patch_r.status_code == 200
        body = patch_r.json()
        # exit_code must be preserved at the killed value (-1), NOT clobbered to
        # the incoming 0. SessionOut doesn't serialise `status`, so we assert the
        # guarded status directly on the DB row (the endpoint mutated it on the
        # same session the `db` fixture wraps).
        assert body["exit_code"] == -1
        # …but features_pushed must still be merged in.
        assert body["features_pushed"] == 2
        killed = db.get(DBSession, session_id)
        assert killed.status == "killed"
        assert killed.exit_code == -1
        assert killed.features_pushed == 2


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


# ── DELETE /api/phases/{id} — empty-phase reaper guard ───────────────────────

def _mk_phase(db, product_id, name="Phase A", order=0):
    ph = Phase(product_id=product_id, name=name, order=order)
    db.add(ph)
    db.flush()
    return ph


def test_delete_empty_phase_succeeds(client, db):
    p = make_product(db, working_dir="/projects/reap-empty")
    ph = _mk_phase(db, p.id)
    r = client.delete(f"/api/phases/{ph.id}", auth=AUTH)
    assert r.status_code == 204
    assert db.get(Phase, ph.id) is None


def test_delete_phase_with_live_feature_refused(client, db):
    p = make_product(db, working_dir="/projects/reap-live")
    ph = _mk_phase(db, p.id)
    make_feature(db, p.id, name="real-feature", status="Approved", phase_id=ph.id)
    r = client.delete(f"/api/phases/{ph.id}", auth=AUTH)
    assert r.status_code == 409
    assert db.get(Phase, ph.id) is not None  # still there


def test_delete_phase_with_only_rejected_features_succeeds(client, db):
    p = make_product(db, working_dir="/projects/reap-rejected")
    ph = _mk_phase(db, p.id)
    # Only dead features → phase is effectively empty → deletable.
    make_feature(db, p.id, name="junk-1", status="Rejected", phase_id=ph.id)
    make_feature(db, p.id, name="junk-2", status="Reverted", phase_id=ph.id)
    r = client.delete(f"/api/phases/{ph.id}", auth=AUTH)
    assert r.status_code == 204
    assert db.get(Phase, ph.id) is None


def test_delete_completed_phase_refused(client, db):
    # A phase whose features all shipped (Pushed) is NOT empty — keep it.
    p = make_product(db, working_dir="/projects/reap-shipped")
    ph = _mk_phase(db, p.id)
    make_feature(db, p.id, name="shipped", status="Pushed", phase_id=ph.id)
    r = client.delete(f"/api/phases/{ph.id}", auth=AUTH)
    assert r.status_code == 409
    assert db.get(Phase, ph.id) is not None


def test_delete_missing_phase_404(client, db):
    r = client.delete("/api/phases/99999999", auth=AUTH)
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# CODER MODEL LADDER — Admin save round-trip (migration 049 / Slice B)
# ══════════════════════════════════════════════════════════════════════════════

from website.models import SystemConfig


def _save_agent_settings(client, **extra):
    """POST the poller/agent form (which owns the Coder Models sub-tab)."""
    data = {"coder_tier_backend_0": "ollama", "coder_tier_model_0": "",
            "coder_tier_attempts_0": ""}
    data.update(extra)
    return client.post("/admin/settings/poller", data=data, auth=AUTH,
                       follow_redirects=False)


def test_coder_ladder_saves_multi_tier(client, db):
    r = _save_agent_settings(
        client,
        coder_tier_backend_0="ollama",     coder_tier_model_0="minimax-m2",  coder_tier_attempts_0="2",
        coder_tier_backend_1="ollama",     coder_tier_model_1="glm-4.6",     coder_tier_attempts_1="3", coder_tier_enabled_1="1",
        coder_tier_backend_2="claude-api", coder_tier_model_2="claude-opus-4-8", coder_tier_attempts_2="2", coder_tier_enabled_2="1",
    )
    assert r.status_code == 303
    cfg = db.get(SystemConfig, 1)
    assert cfg.coder_tiers == [
        {"backend": "ollama",     "model": "minimax-m2",      "max_attempts": 2, "enabled": True},
        {"backend": "ollama",     "model": "glm-4.6",         "max_attempts": 3, "enabled": True},
        {"backend": "claude-api", "model": "claude-opus-4-8", "max_attempts": 2, "enabled": True},
    ]


def test_coder_ladder_empty_stores_null(client, db):
    # No tier models → NULL (runtime builds the default single tier).
    r = _save_agent_settings(client)
    assert r.status_code == 303
    assert db.get(SystemConfig, 1).coder_tiers is None


def test_coder_ladder_escalation_row_disabled_when_unchecked(client, db):
    r = _save_agent_settings(
        client,
        coder_tier_backend_0="ollama", coder_tier_model_0="minimax-m2", coder_tier_attempts_0="2",
        coder_tier_backend_1="ollama", coder_tier_model_1="glm-4.6",    coder_tier_attempts_1="3",
        # no coder_tier_enabled_1 → stored disabled but preserved
    )
    assert r.status_code == 303
    tiers = db.get(SystemConfig, 1).coder_tiers
    assert tiers[1] == {"backend": "ollama", "model": "glm-4.6", "max_attempts": 3, "enabled": False}


def test_coder_ladder_bad_backend_rejected(client, db):
    r = _save_agent_settings(
        client,
        coder_tier_backend_0="mistral", coder_tier_model_0="x", coder_tier_attempts_0="2",
    )
    assert r.status_code == 422
    assert "Coder ladder" in r.json()["detail"]


def test_coder_ladder_daily_cap_saved(client, db):
    r = _save_agent_settings(
        client,
        coder_tier_backend_0="ollama", coder_tier_model_0="minimax-m2", coder_tier_attempts_0="2",
        blocked_escalation_daily_usd_cap="7.50",
    )
    assert r.status_code == 303
    assert float(db.get(SystemConfig, 1).blocked_escalation_daily_usd_cap) == 7.50


def test_admin_renders_coder_ladder_tab(client, db):
    r = client.get("/admin", auth=AUTH)
    assert r.status_code == 200
    html = r.text
    assert "🧠 Coder Models" in html
    assert 'id="asub-coder"' in html
    assert 'name="coder_tier_model_0"' in html          # First Attempt row
    assert 'name="coder_tier_enabled_3"' in html        # Escalation 3 row
    assert 'name="blocked_escalation_daily_usd_cap"' in html  # cap moved here
    assert 'id="asub-escalation"' not in html           # legacy tab removed
