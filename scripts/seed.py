#!/usr/bin/env python3
"""
seed.py — Reset and seed the ProductFactory database with realistic demo data.

Usage:
    python scripts/seed.py                     # seed everything
    python scripts/seed.py --wipe-only         # just reset, no seed data
    python scripts/seed.py --no-sessions       # skip session history

Requires DATABASE_URL in env (or .env file in repo root).
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import choice, randint, uniform

# ── Load .env if present ─────────────────────────────────────────────────────
repo_root = Path(__file__).parent.parent
env_file  = repo_root / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

sys.path.insert(0, str(repo_root))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from website.models import (
    Base, Product, Feature, FeatureReview, Session as AgentSession,
    SystemConfig, PMUser, Alert,
)

# ── DB connection (sync psycopg2, same as Alembic) ───────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    sys.exit("ERROR: DATABASE_URL not set")

engine = create_engine(DATABASE_URL, echo=False)


def wipe(db: Session) -> None:
    """Truncate all tables in dependency order."""
    db.execute(text("TRUNCATE feature_reviews, sessions, alerts, features, products, system_config, pm_users RESTART IDENTITY CASCADE"))
    db.commit()
    print("✓ Database wiped")


def seed(db: Session) -> None:
    now = datetime.now(timezone.utc)

    # ── System config ─────────────────────────────────────────────────────────
    cfg = SystemConfig(
        id=1,
        products_root_dir="C:/Users/digvi/Personal/Products",
        github_org="SavvyCoding",
        github_ssh_key_name="productfactory-deploy",
        max_sessions_per_day=20,
        poll_interval=60,
        session_timeout_minutes=90,
        stale_threshold_minutes=45,
        max_open_prs=3,
        pr_gate_sleep=300,
        stuck_feature_timeout_hours=2,
        max_features_per_run=1,
        brownfield_file_threshold=10,
        agent_backend="claude",
    )
    db.add(cfg)

    # ── PM users ──────────────────────────────────────────────────────────────
    import bcrypt as _bcrypt
    def _hash(pw): return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()

    db.add(PMUser(name="Digvijay", username="digvi", password_hash=_hash("admin123")))
    db.add(PMUser(name="Alice PM", username="alice", password_hash=_hash("admin123")))
    db.commit()
    print("✓ System config + PM users")

    # ── Products ──────────────────────────────────────────────────────────────
    products = []

    p1 = Product(
        working_dir="C:/Users/digvi/Products/TaskFlow",
        name="TaskFlow",
        github_repo="https://github.com/digvi/taskflow",
        tech_stack=["python", "fastapi", "postgresql", "react"],
        type="greenfield",
        status="ready",
        analysis_status="done",
        last_run_at=now - timedelta(minutes=45),
        config={"last_planner_at": (now - timedelta(days=2)).isoformat()},
    )
    p2 = Product(
        working_dir="C:/Users/digvi/Products/ShopLite",
        name="ShopLite",
        github_repo="https://github.com/digvi/shoplite",
        tech_stack=["python", "django", "postgresql"],
        type="brownfield",
        status="ready",
        analysis_status="done",
        last_run_at=now - timedelta(hours=2),
        config={
            "baseline_tests": {"passed": 42, "recorded": "2026-04-10"},
            "test_command":   "pytest tests/ -q",
            "last_documenter_at": (now - timedelta(days=4)).isoformat(),
        },
    )
    p3 = Product(
        working_dir="C:/Users/digvi/Products/NoteSync",
        name="NoteSync",
        github_repo="https://github.com/digvi/notesync",
        tech_stack=["node", "express", "mongodb"],
        type="greenfield",
        status="paused",
        analysis_status="pending",
        last_run_at=now - timedelta(days=1),
    )
    p4 = Product(
        working_dir="C:/Users/digvi/Products/MetricsPro",
        name="MetricsPro",
        github_repo="https://github.com/digvi/metricspro",
        tech_stack=["go", "postgresql", "grafana"],
        type="brownfield",
        status="ready",
        analysis_status="done",
        last_run_at=now - timedelta(hours=6),
        quiet_hours_start=0,
        quiet_hours_end=9,
        max_features_per_run=2,
    )

    for p in [p1, p2, p3, p4]:
        db.add(p)
    db.commit()
    products = [p1, p2, p3, p4]
    print(f"✓ {len(products)} products")

    # ── Features for TaskFlow ─────────────────────────────────────────────────
    tf_features = [
        Feature(product_id=p1.id, name="User authentication (JWT)", description="Sign up, login, refresh token, logout. Store hashed passwords.", status="Pushed",       priority=90, feature_type="feature", source="pm",  fix_attempts=0, branch_name="feat/user-auth",    pr_number=3, pr_url="https://github.com/digvi/taskflow/pull/3",   review_outcome="approved"),
        Feature(product_id=p1.id, name="Task CRUD API",              description="Create, list, update, delete tasks. Pagination support.",       status="Pushed",       priority=85, feature_type="feature", source="pm",  fix_attempts=0, branch_name="feat/task-crud",    pr_number=4, pr_url="https://github.com/digvi/taskflow/pull/4",   review_outcome="approved"),
        Feature(product_id=p1.id, name="Email notifications",        description="Send email on task due-date and assignment.",                   status="Reviewing",    priority=70, feature_type="feature", source="pm",  fix_attempts=0, branch_name="feat/email-notifs", pr_number=7, pr_url="https://github.com/digvi/taskflow/pull/7"),
        Feature(product_id=p1.id, name="Team workspaces",            description="Users belong to one or more teams. Tasks scoped to team.",     status="Implementing", priority=75, feature_type="feature", source="pm",  fix_attempts=1),
        Feature(product_id=p1.id, name="Recurring tasks",            description="Daily/weekly/monthly recurrence rules (iCal RRULE).",          status="Designed",     priority=60, feature_type="feature", source="ai",  fix_attempts=0, skip_design=False, design_doc="Recurring tasks use RRULE. On trigger, clone with next due date."),
        Feature(product_id=p1.id, name="Add test coverage for auth", description="Unit + integration tests for auth endpoints.",                  status="Approved",     priority=80, feature_type="bug",     source="ai",  fix_attempts=0, skip_design=True),
        Feature(product_id=p1.id, name="CSV export",                 description="Export tasks to CSV with filters.",                            status="Pending",      priority=40, feature_type="feature", source="pm",  fix_attempts=0),
        Feature(product_id=p1.id, name="Dark mode UI",               description="CSS custom properties for dark/light toggle.",                 status="Pending",      priority=30, feature_type="feature", source="pm",  fix_attempts=0),
        Feature(product_id=p1.id, name="Dockerfile hardening",       description="Audit Dockerfile for security and size optimisations.",        status="Pending",      priority=20, feature_type="chore",   source="ai",  fix_attempts=0),
        Feature(product_id=p1.id, name="Rate limiting middleware",   description="Throttle 100 req/min per IP. Return 429 with Retry-After.",    status="Blocked",      priority=65, feature_type="feature", source="pm",  fix_attempts=2, blocked_reason="Waiting on Redis infrastructure PR to merge first"),
    ]
    for f in tf_features:
        db.add(f)

    # ── Features for ShopLite ─────────────────────────────────────────────────
    sl_features = [
        Feature(product_id=p2.id, name="Product catalogue API",     description="CRUD for products with image uploads.",          status="Pushed",    priority=90, feature_type="feature", source="pm", fix_attempts=0, pr_number=12, pr_url="https://github.com/digvi/shoplite/pull/12", review_outcome="approved"),
        Feature(product_id=p2.id, name="Shopping cart",             description="Session-based cart with quantity management.",   status="Pushed",    priority=85, feature_type="feature", source="pm", fix_attempts=0, pr_number=14, pr_url="https://github.com/digvi/shoplite/pull/14", review_outcome="approved"),
        Feature(product_id=p2.id, name="Stripe checkout",           description="Stripe payment intent + webhook confirmation.",  status="Reviewed",  priority=80, feature_type="feature", source="pm", fix_attempts=0, branch_name="feat/stripe", pr_number=18, pr_url="https://github.com/digvi/shoplite/pull/18", review_outcome="approved"),
        Feature(product_id=p2.id, name="Inventory management",      description="Stock levels, low-stock alerts, reorder points.", status="Designing", priority=70, feature_type="feature", source="pm", fix_attempts=0),
        Feature(product_id=p2.id, name="SQL injection audit",       description="Review all raw queries for injection vectors.",  status="Approved",  priority=95, feature_type="bug",     source="ai", fix_attempts=0, skip_design=True),
        Feature(product_id=p2.id, name="Order history page",        description="Customer-facing order history with pagination.", status="Pending",   priority=50, feature_type="feature", source="pm", fix_attempts=0),
    ]
    for f in sl_features:
        db.add(f)

    # ── Features for MetricsPro ───────────────────────────────────────────────
    mp_features = [
        Feature(product_id=p4.id, name="Prometheus metrics endpoint", description="Expose /metrics for Prometheus scraping.",         status="Pushed",    priority=90, feature_type="feature", source="pm", fix_attempts=0, pr_number=5, review_outcome="approved"),
        Feature(product_id=p4.id, name="Alerting rules engine",       description="Define threshold rules, fire webhooks on breach.", status="Approved",  priority=85, feature_type="feature", source="pm", fix_attempts=0),
        Feature(product_id=p4.id, name="Dashboard persistence",       description="Save/load Grafana-style dashboard configs.",       status="Pending",   priority=60, feature_type="feature", source="pm", fix_attempts=0),
        Feature(product_id=p4.id, name="Refactor metrics collector",  description="Extract collector into separate service.",         status="Pending",   priority=30, feature_type="chore",   source="ai", fix_attempts=0),
    ]
    for f in mp_features:
        db.add(f)
    db.commit()

    total_features = len(tf_features) + len(sl_features) + len(mp_features)
    print(f"✓ {total_features} features across products")

    # ── Feature reviews ───────────────────────────────────────────────────────
    pushed = [f for f in tf_features + sl_features + mp_features if f.pr_number]
    for f in pushed:
        db.add(FeatureReview(
            feature_id=f.id,
            review_outcome=f.review_outcome or "approved",
            review_notes="LGTM — tests pass, no issues found." if f.review_outcome == "approved" else "Needs error handling in edge cases.",
            session_uid=f"review-seed-{f.id}",
        ))
    db.commit()
    print(f"✓ {len(pushed)} feature reviews")

    # ── Session history ───────────────────────────────────────────────────────
    def make_session(product_id, persona, minutes_ago, duration_m, exit_code=0,
                     features_attempted=1, features_pushed=1, cost=None, notes=None):
        start = now - timedelta(minutes=minutes_ago)
        end   = start + timedelta(minutes=duration_m)
        return AgentSession(
            product_id=product_id,
            session_uid=f"seed-{product_id}-{persona}-{minutes_ago}",
            started_at=start,
            ended_at=end,
            exit_code=exit_code,
            features_attempted=features_attempted,
            features_pushed=features_pushed,
            tokens_input=randint(15000, 80000),
            tokens_output=randint(3000, 18000),
            cost_usd=cost if cost is not None else round(uniform(0.02, 0.45), 6),
            persona=persona,
            notes=notes,
        )

    sessions = [
        # TaskFlow
        make_session(p1.id, "planner",          minutes_ago=3200, duration_m=8,  features_pushed=0, cost=0.018),
        make_session(p1.id, "designer",          minutes_ago=2900, duration_m=12, features_pushed=0, cost=0.031),
        make_session(p1.id, "coder",             minutes_ago=2700, duration_m=38, features_pushed=1, cost=0.214),
        make_session(p1.id, "qa_tester",         minutes_ago=2660, duration_m=14, features_pushed=0, cost=0.052),
        make_session(p1.id, "security_auditor",  minutes_ago=2645, duration_m=10, features_pushed=0, cost=0.041),
        make_session(p1.id, "recommender",       minutes_ago=2635, duration_m=6,  features_pushed=0, cost=0.019),
        make_session(p1.id, "designer",          minutes_ago=1800, duration_m=11, features_pushed=0, cost=0.028),
        make_session(p1.id, "coder",             minutes_ago=1600, duration_m=42, features_pushed=1, cost=0.198),
        make_session(p1.id, "reviewer",          minutes_ago=1550, duration_m=9,  features_pushed=0, cost=0.033),
        make_session(p1.id, "coder",             minutes_ago=900,  duration_m=55, features_pushed=1, cost=0.312),
        make_session(p1.id, "coder",             minutes_ago=480,  duration_m=61, features_pushed=1, cost=0.278, notes="Implemented team workspaces; one retry needed for migration"),
        make_session(p1.id, "coder",             minutes_ago=120,  duration_m=47, features_pushed=0, exit_code=1, features_attempted=1, cost=0.089, notes="Failed: rate-limit dependency on Redis not available"),
        # ShopLite
        make_session(p2.id, "coder",             minutes_ago=4000, duration_m=35, features_pushed=1, cost=0.187),
        make_session(p2.id, "coder",             minutes_ago=3000, duration_m=44, features_pushed=1, cost=0.231),
        make_session(p2.id, "reviewer",          minutes_ago=2500, duration_m=8,  features_pushed=0, cost=0.024),
        make_session(p2.id, "documenter",        minutes_ago=2000, duration_m=15, features_pushed=0, cost=0.038),
        make_session(p2.id, "coder",             minutes_ago=300,  duration_m=52, features_pushed=1, cost=0.267),
        make_session(p2.id, "security_auditor",  minutes_ago=245,  duration_m=11, features_pushed=0, cost=0.044, notes="Found 2 potential SQL injection vectors, filed bug features"),
        # MetricsPro
        make_session(p4.id, "coder",             minutes_ago=5000, duration_m=29, features_pushed=1, cost=0.155),
        make_session(p4.id, "analytics",         minutes_ago=1000, duration_m=13, features_pushed=0, cost=0.031),
    ]

    for s in sessions:
        db.add(s)
    db.commit()
    print(f"✓ {len(sessions)} session history records")

    # ── Alerts ────────────────────────────────────────────────────────────────
    alerts = [
        Alert(product_id=p1.id, level="error",   message="Session seed-1-coder-120 exited with code 1. Feature 'Rate limiting middleware' set to Blocked.", delivered=True),
        Alert(product_id=p2.id, level="warning",  message="Security Auditor found 2 potential SQL injection vectors in ShopLite. Bug features filed.", delivered=True),
        Alert(product_id=p1.id, level="info",     message="TaskFlow: 3 features pushed this week (auth, task CRUD, email notifs).", delivered=False),
        Alert(product_id=None,  level="warning",  message="PR gate active: TaskFlow has 2 open PRs. Coder sessions paused until merged.", delivered=False),
    ]
    for a in alerts:
        db.add(a)
    db.commit()
    print(f"✓ {len(alerts)} alerts ({sum(1 for a in alerts if not a.delivered)} unread)")


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Seed ProductFactory database")
    parser.add_argument("--wipe-only",    action="store_true", help="Wipe DB without seeding")
    parser.add_argument("--no-sessions",  action="store_true", help="Skip session history")
    args = parser.parse_args()

    with Session(engine) as db:
        wipe(db)
        if not args.wipe_only:
            seed(db)
            print("\n✓ Seed complete. Login: digvi / admin123")
        else:
            print("\n✓ Wipe complete. DB is empty (schema intact).")


if __name__ == "__main__":
    main()
