"""
Phase 1 — DB schema tests.
All tests use real PostgreSQL (rolled-back per test, no side effects).

Run:  TEST_DATABASE_URL=postgresql://user:pass@localhost/productfactory_test pytest tests/test_schema.py -v
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from website.models import Product, Feature, Session as DBSession, Alert


# ══════════════════════════════════════════════════════
# PRODUCTS
# ══════════════════════════════════════════════════════

class TestProductPositive:
    def test_create_minimal(self, db_session):
        """Minimum required field: working_dir only."""
        p = Product(working_dir="/projects/my-app")
        db_session.add(p)
        db_session.flush()
        assert p.id is not None
        assert p.status == "registered"
        assert p.type == "greenfield"
        assert p.analysis_status == "pending"

    def test_create_full(self, db_session):
        """All fields populated."""
        p = Product(
            working_dir="/projects/full-app",
            name="Full App",
            github_repo="https://github.com/user/full-app",
            tech_stack=["python", "node"],
            type="brownfield",
            status="ready",
            analysis_status="done",
            config={"max_batch_size": 3},
        )
        db_session.add(p)
        db_session.flush()
        assert p.name == "Full App"
        assert p.tech_stack == ["python", "node"]
        assert p.config["max_batch_size"] == 3

    def test_updated_at_trigger(self, db_session):
        """updated_at changes on UPDATE (trigger)."""
        p = Product(working_dir="/projects/trigger-test")
        db_session.add(p)
        db_session.flush()
        original = p.updated_at
        db_session.execute(text("UPDATE products SET name = 'changed' WHERE id = :id"), {"id": p.id})
        db_session.flush()
        db_session.refresh(p)
        assert p.updated_at >= original


class TestProductNegative:
    def test_duplicate_working_dir(self, db_session):
        """working_dir must be unique."""
        db_session.add(Product(working_dir="/projects/dup"))
        db_session.flush()
        db_session.add(Product(working_dir="/projects/dup"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_missing_working_dir(self, db_session):
        """working_dir is NOT NULL."""
        db_session.add(Product(working_dir=None))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_status(self, db_session):
        """Invalid status value rejected by CHECK constraint."""
        db_session.add(Product(working_dir="/projects/bad-status", status="running"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_type(self, db_session):
        """Invalid type value rejected by CHECK constraint."""
        db_session.add(Product(working_dir="/projects/bad-type", type="legacy"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_analysis_status(self, db_session):
        """Invalid analysis_status rejected."""
        db_session.add(Product(working_dir="/projects/bad-analysis", analysis_status="complete"))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestProductEdge:
    def test_very_long_working_dir(self, db_session):
        """TEXT columns accept long strings."""
        long_path = "/projects/" + "a" * 500
        p = Product(working_dir=long_path)
        db_session.add(p)
        db_session.flush()
        assert p.working_dir == long_path

    def test_config_nested_json(self, db_session):
        """JSONB stores arbitrary nested structure."""
        config = {"baseline_tests": {"passed": 127, "failed": 0}, "max_batch_size": 3}
        p = Product(working_dir="/projects/json-test", config=config)
        db_session.add(p)
        db_session.flush()
        db_session.refresh(p)
        assert p.config["baseline_tests"]["passed"] == 127


# ══════════════════════════════════════════════════════
# FEATURES
# ══════════════════════════════════════════════════════

@pytest.fixture
def product(db_session):
    p = Product(working_dir="/projects/feature-owner")
    db_session.add(p)
    db_session.flush()
    return p


class TestFeaturePositive:
    def test_create_minimal(self, db_session, product):
        f = Feature(product_id=product.id, name="user-login")
        db_session.add(f)
        db_session.flush()
        assert f.id is not None
        assert f.status == "Pending"
        assert f.priority == 50
        assert f.fix_attempts == 0
        assert f.source == "pm"

    def test_all_valid_statuses(self, db_session, product):
        """Every valid status value should be accepted."""
        statuses = ["Pending", "Approved", "Implementing", "Implemented",
                    "Testing", "Committed", "Pushed", "Blocked", "Rejected", "Reverted"]
        for s in statuses:
            f = Feature(product_id=product.id, name=f"feat-{s}", status=s)
            db_session.add(f)
        db_session.flush()

    def test_depends_on_self_reference(self, db_session, product):
        """Features can depend on other features (self-referential FK)."""
        f1 = Feature(product_id=product.id, name="base-feature")
        db_session.add(f1)
        db_session.flush()
        f2 = Feature(product_id=product.id, name="dependent-feature", depends_on=f1.id)
        db_session.add(f2)
        db_session.flush()
        assert f2.depends_on == f1.id

    def test_ai_source(self, db_session, product):
        f = Feature(product_id=product.id, name="ai-recommended", source="ai")
        db_session.add(f)
        db_session.flush()
        assert f.source == "ai"


class TestFeatureNegative:
    def test_invalid_status(self, db_session, product):
        db_session.add(Feature(product_id=product.id, name="bad", status="InProgress"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_priority_below_range(self, db_session, product):
        db_session.add(Feature(product_id=product.id, name="bad", priority=0))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_priority_above_range(self, db_session, product):
        db_session.add(Feature(product_id=product.id, name="bad", priority=101))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_fix_attempts(self, db_session, product):
        db_session.add(Feature(product_id=product.id, name="bad", fix_attempts=-1))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_invalid_source(self, db_session, product):
        db_session.add(Feature(product_id=product.id, name="bad", source="human"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_cascade_delete(self, db_session, product):
        """Deleting a product deletes its features."""
        f = Feature(product_id=product.id, name="will-be-deleted")
        db_session.add(f)
        db_session.flush()
        feature_id = f.id
        db_session.delete(product)
        db_session.flush()
        assert db_session.get(Feature, feature_id) is None


class TestFeatureEdge:
    def test_priority_boundary_values(self, db_session, product):
        """Priority 1 and 100 are both valid."""
        f1 = Feature(product_id=product.id, name="highest", priority=1)
        f2 = Feature(product_id=product.id, name="lowest", priority=100)
        db_session.add_all([f1, f2])
        db_session.flush()


# ══════════════════════════════════════════════════════
# SESSIONS
# ══════════════════════════════════════════════════════

class TestSessionPositive:
    def test_create(self, db_session, product):
        s = DBSession(product_id=product.id, session_uid="abc-123")
        db_session.add(s)
        db_session.flush()
        assert s.id is not None
        assert s.features_attempted == 0
        assert s.features_pushed == 0

    def test_unique_session_uid(self, db_session, product):
        """Two different session_uids on same product are fine."""
        db_session.add(DBSession(product_id=product.id, session_uid="uid-1"))
        db_session.add(DBSession(product_id=product.id, session_uid="uid-2"))
        db_session.flush()

    def test_cascade_delete(self, db_session, product):
        """Deleting a product deletes its sessions."""
        s = DBSession(product_id=product.id, session_uid="uid-cascade")
        db_session.add(s)
        db_session.flush()
        session_id = s.id
        db_session.delete(product)
        db_session.flush()
        assert db_session.get(DBSession, session_id) is None


class TestSessionNegative:
    def test_duplicate_session_uid(self, db_session, product):
        db_session.add(DBSession(product_id=product.id, session_uid="dup-uid"))
        db_session.flush()
        db_session.add(DBSession(product_id=product.id, session_uid="dup-uid"))
        with pytest.raises(IntegrityError):
            db_session.flush()


# ══════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════

class TestAlertPositive:
    def test_all_valid_levels(self, db_session):
        for level in ("info", "warning", "error", "critical"):
            db_session.add(Alert(level=level, message=f"test {level}"))
        db_session.flush()

    def test_null_product_id(self, db_session):
        """Alerts can exist without a product (system-level alerts)."""
        a = Alert(level="warning", message="system alert", product_id=None)
        db_session.add(a)
        db_session.flush()
        assert a.product_id is None

    def test_set_null_on_product_delete(self, db_session, product):
        """Deleting a product NULLs product_id on its alerts (not cascade delete)."""
        a = Alert(level="info", message="test", product_id=product.id)
        db_session.add(a)
        db_session.flush()
        alert_id = a.id
        db_session.delete(product)
        db_session.flush()
        db_session.refresh(a)
        assert a.product_id is None


class TestAlertNegative:
    def test_invalid_level(self, db_session):
        db_session.add(Alert(level="debug", message="bad level"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_negative_retry_count(self, db_session):
        db_session.add(Alert(level="info", message="bad retry", retry_count=-1))
        with pytest.raises(IntegrityError):
            db_session.flush()
