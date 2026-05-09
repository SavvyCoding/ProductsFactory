"""
SQLAlchemy ORM models — mirrors db/schema.sql exactly.
Add columns here + create an Alembic migration to apply to live DB.
"""

from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    Integer, String, Text, DateTime, Boolean, ARRAY, Date,
    ForeignKey, func, CheckConstraint, event, Numeric, Float, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from website.database import Base


REVIEW_OUTCOMES    = ('approved', 'changes_requested')
PRODUCT_STATUSES   = ('registered', 'discovering', 'discovered', 'ready', 'paused', 'error',
                      'greenfield_pending')
PRODUCT_TYPES      = ('greenfield', 'brownfield')
ANALYSIS_STATUSES  = ('pending', 'running', 'done')
FEATURE_TYPES      = ('feature', 'bug', 'chore')
FEATURE_STATUSES   = ('Pending', 'Approved',
                      'Designing', 'Designed',
                      'Implementing', 'Implemented',
                      'Reviewing', 'Reviewed',
                      'Testing', 'Committed', 'Pushed',
                      'Blocked', 'Rejected', 'Reverted', 'Deferred')
ALERT_LEVELS       = ('info', 'warning', 'error', 'critical')


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(f"type IN {PRODUCT_TYPES}", name="ck_products_type"),
        CheckConstraint(f"status IN {PRODUCT_STATUSES}", name="ck_products_status"),
        CheckConstraint(f"analysis_status IN {ANALYSIS_STATUSES}", name="ck_products_analysis_status"),
    )

    id:              Mapped[int]           = mapped_column(Integer, primary_key=True)
    working_dir:     Mapped[str]           = mapped_column(Text, unique=True, nullable=False)
    name:            Mapped[Optional[str]] = mapped_column(Text)
    github_repo:     Mapped[Optional[str]] = mapped_column(Text)
    tech_stack:      Mapped[Optional[List[str]]] = mapped_column(ARRAY(String))
    ui_template:     Mapped[Optional[str]] = mapped_column(Text)  # picked at wizard time for web stacks
    type:            Mapped[str]           = mapped_column(Text, nullable=False, default="greenfield")
    status:          Mapped[str]           = mapped_column(Text, nullable=False, default="registered")
    analysis_status: Mapped[str]           = mapped_column(Text, nullable=False, default="pending")
    config:             Mapped[Optional[dict]] = mapped_column(JSONB)
    last_run_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    run_now:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    run_trainer_now:    Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    custom_prompt:      Mapped[Optional[str]]  = mapped_column(Text)
    quiet_hours_start:  Mapped[Optional[int]]  = mapped_column(Integer)
    quiet_hours_end:    Mapped[Optional[int]]  = mapped_column(Integer)
    daily_session_cap:      Mapped[Optional[int]]  = mapped_column(Integer)
    max_features_per_run:   Mapped[Optional[int]]  = mapped_column(Integer)
    created_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    features: Mapped[List["Feature"]] = relationship("Feature", back_populates="product", cascade="all, delete")
    sessions:  Mapped[List["Session"]]  = relationship("Session",  back_populates="product", cascade="all, delete")


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        CheckConstraint(f"status IN {FEATURE_STATUSES}", name="ck_features_status"),
        CheckConstraint(f"feature_type IN {FEATURE_TYPES}", name="ck_features_type"),
        CheckConstraint("priority BETWEEN 1 AND 100", name="ck_features_priority"),
        CheckConstraint("fix_attempts >= 0", name="ck_features_fix_attempts"),
        CheckConstraint("source IN ('pm', 'ai')", name="ck_features_source"),
    )

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True)
    product_id:     Mapped[int]            = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    name:           Mapped[str]            = mapped_column(Text, nullable=False)
    description:    Mapped[Optional[str]]  = mapped_column(Text)
    status:         Mapped[str]            = mapped_column(Text, nullable=False, default="Pending", index=True)
    priority:       Mapped[int]            = mapped_column(Integer, nullable=False, default=50)
    depends_on:     Mapped[Optional[int]]  = mapped_column(Integer, ForeignKey("features.id", ondelete="SET NULL"), index=True)
    fix_attempts:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    source:         Mapped[str]            = mapped_column(Text, nullable=False, default="pm")
    feature_type:   Mapped[str]            = mapped_column(Text, nullable=False, default="feature")
    branch_name:    Mapped[Optional[str]]  = mapped_column(Text)
    pr_url:         Mapped[Optional[str]]  = mapped_column(Text)
    pr_number:      Mapped[Optional[int]]  = mapped_column(Integer)
    blocked_reason: Mapped[Optional[str]]  = mapped_column(Text)
    version:        Mapped[int]            = mapped_column(Integer, nullable=False, default=0, server_default="0")
    design_doc:     Mapped[Optional[str]]  = mapped_column(Text)
    design_doc_path: Mapped[Optional[str]] = mapped_column(Text)
    review_outcome: Mapped[Optional[str]]  = mapped_column(String(32))
    review_notes:   Mapped[Optional[str]]  = mapped_column(Text)
    # Powers supervisor.detect_repeated_review_feedback: hash of the
    # reviewer's last changes_requested feedback. Null = no prior cycle.
    # The counter resets to 0 whenever the signature changes (coder
    # addressed something) or the feature is approved.
    last_changes_signature: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    repeated_changes_count: Mapped[int]            = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sprint_id:    Mapped[Optional[int]]  = mapped_column(Integer, ForeignKey("sprints.id", ondelete="SET NULL"), nullable=True, index=True)
    story_points: Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    due_date:     Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    product:   Mapped["Product"]              = relationship("Product", back_populates="features")
    reviews:   Mapped[List["FeatureReview"]]  = relationship("FeatureReview", back_populates="feature", cascade="all, delete")
    comments:  Mapped[List["FeatureComment"]] = relationship("FeatureComment", back_populates="feature", cascade="all, delete")
    changelog: Mapped[List["FeatureChangelog"]] = relationship("FeatureChangelog", back_populates="feature", cascade="all, delete")
    labels:    Mapped[List["FeatureLabel"]]   = relationship("FeatureLabel", back_populates="feature", cascade="all, delete")
    links_out: Mapped[List["FeatureLink"]]    = relationship("FeatureLink", foreign_keys="FeatureLink.source_id", back_populates="source", cascade="all, delete")
    links_in:  Mapped[List["FeatureLink"]]    = relationship("FeatureLink", foreign_keys="FeatureLink.target_id", back_populates="target", cascade="all, delete")


class FeatureReview(Base):
    __tablename__ = "feature_reviews"
    __table_args__ = (
        CheckConstraint(f"review_outcome IN {REVIEW_OUTCOMES}", name="ck_feature_reviews_outcome"),
    )

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True)
    feature_id:     Mapped[int]            = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True)
    review_outcome: Mapped[str]            = mapped_column(String(32), nullable=False)
    review_notes:   Mapped[Optional[str]]  = mapped_column(Text)
    session_uid:    Mapped[Optional[str]]  = mapped_column(Text)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())

    feature: Mapped["Feature"] = relationship("Feature", back_populates="reviews")


class Session(Base):
    __tablename__ = "sessions"

    id:                  Mapped[int]            = mapped_column(Integer, primary_key=True)
    product_id:          Mapped[int]            = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    session_uid:         Mapped[str]            = mapped_column(Text, unique=True, nullable=False)
    container_id:        Mapped[Optional[str]]  = mapped_column(Text)
    started_at:          Mapped[datetime]        = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at:            Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    exit_code:           Mapped[Optional[int]]  = mapped_column(Integer)
    features_attempted:  Mapped[int]            = mapped_column(Integer, default=0)
    features_pushed:     Mapped[int]            = mapped_column(Integer, default=0)
    notes:               Mapped[Optional[str]]  = mapped_column(Text)
    tokens_input:        Mapped[Optional[int]]  = mapped_column(Integer)
    tokens_output:       Mapped[Optional[int]]  = mapped_column(Integer)
    cost_usd:            Mapped[Optional[float]] = mapped_column(Numeric(10, 6))
    persona:             Mapped[Optional[str]]  = mapped_column(String(32))
    backend:             Mapped[Optional[str]]  = mapped_column(String(16))  # "claude" | "ollama"

    # FSM — canonical lifecycle state. Watchdog/reconciler/harvester drive
    # transitions. Never parse docker output or file mtimes; consult these.
    status:              Mapped[str]            = mapped_column(Text, nullable=False, default="pending")
    heartbeat_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expected_deadline:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    kill_reason:         Mapped[Optional[str]]  = mapped_column(Text)
    log:                 Mapped[Optional[str]]  = mapped_column(Text)

    product: Mapped["Product"] = relationship("Product", back_populates="sessions")


SESSION_STATUSES = (
    "pending",    # DB row created, docker run not yet started
    "starting",   # docker run launched, container not yet visible
    "running",    # container live, agent working
    "wrapping",   # agent exited, harvester applying results
    "ended",      # clean close, exit_code=0
    "killed",     # watchdog/timeout/external SIGKILL
    "orphaned",   # DB says running but no container found; reconciler recovers
)


class SessionEvent(Base):
    """Lifecycle audit log — every transition + watchdog action written here."""
    __tablename__ = "session_events"

    id:         Mapped[int]       = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int]       = mapped_column(Integer, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    event:      Mapped[str]       = mapped_column(Text, nullable=False)
    detail:     Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now())


class SystemConfig(Base):
    """
    Global system configuration — single row (id=1 always).
    Upserted via POST /admin/settings. Never create multiple rows.

    Operational settings here override env vars in the poller and website.
    NULL means "use the env var / built-in default".
    """
    __tablename__ = "system_config"

    id:                    Mapped[int]           = mapped_column(Integer, primary_key=True, default=1)
    products_root_dir:     Mapped[Optional[str]] = mapped_column(Text)
    github_org:            Mapped[Optional[str]] = mapped_column(Text)
    github_pat:            Mapped[Optional[str]] = mapped_column(Text)
    github_ssh_key_name:   Mapped[str]           = mapped_column(Text, nullable=False, default="productfactory-deploy")
    slack_webhook_url:     Mapped[Optional[str]] = mapped_column(Text)
    github_webhook_secret: Mapped[Optional[str]] = mapped_column(Text)
    max_sessions_per_day:  Mapped[Optional[int]] = mapped_column(Integer)

    # ── Poller settings ───────────────────────────────────────────────────────
    poll_interval:              Mapped[Optional[int]] = mapped_column(Integer)  # default 60s
    session_timeout_minutes:    Mapped[Optional[int]] = mapped_column(Integer)  # default 90
    stale_threshold_minutes:    Mapped[Optional[int]] = mapped_column(Integer)  # default 45
    auth_check_timeout:         Mapped[Optional[int]] = mapped_column(Integer)  # default 30s
    stuck_feature_timeout_hours:Mapped[Optional[float]] = mapped_column(Float)  # default 0.75h (45 min)
    max_features_per_run:       Mapped[Optional[int]] = mapped_column(Integer)  # default 1
    max_features_per_sprint:    Mapped[Optional[int]] = mapped_column(Integer)  # default 5
    max_fix_attempts:           Mapped[Optional[int]] = mapped_column(Integer)  # default 5
    brownfield_file_threshold:  Mapped[Optional[int]] = mapped_column(Integer)  # default 10
    recommender_pending_threshold: Mapped[Optional[int]] = mapped_column(Integer)  # default 15
    auto_merge_enabled:            Mapped[Optional[bool]] = mapped_column(Boolean)  # default False

    # ── Agent / Ollama settings ───────────────────────────────────────────────
    agent_backend:     Mapped[Optional[str]] = mapped_column(Text)   # "claude" | "ollama"
    ollama_host:       Mapped[Optional[str]] = mapped_column(Text)   # http://host.docker.internal:11434 (local) or https://ollama.com (cloud)
    ollama_api_key:    Mapped[Optional[str]] = mapped_column(Text)   # required for Ollama Cloud, ignored locally
    designer_model:    Mapped[Optional[str]] = mapped_column(Text)   # legacy: 'designer'/'reviewer' personas
    coder_model:       Mapped[Optional[str]] = mapped_column(Text)   # legacy: everything else
    ollama_model_map:  Mapped[Optional[dict]] = mapped_column(JSONB) # explicit persona → ollama model
    ollama_timeout:    Mapped[Optional[int]] = mapped_column(Integer) # default 300s
    bash_timeout:      Mapped[Optional[int]] = mapped_column(Integer) # default 180s
    max_turns:         Mapped[Optional[int]] = mapped_column(Integer) # default 80
    claude_model:            Mapped[Optional[str]] = mapped_column(Text)   # legacy single-model fallback
    claude_model_heavy:      Mapped[Optional[str]] = mapped_column(Text)   # coder/reviewer/designer/etc  (default Sonnet)
    claude_model_light:      Mapped[Optional[str]] = mapped_column(Text)   # planner/documenter/etc       (default Haiku)
    claude_model_map:        Mapped[Optional[dict]] = mapped_column(JSONB) # explicit per-persona override
    claude_credentials_dir:  Mapped[Optional[str]] = mapped_column(Text)   # default C:/Users/digvi/.claude
    ssh_keys_dir:            Mapped[Optional[str]] = mapped_column(Text)   # default: SSH_DIR env var

    # ── Supervisor (Phase 1 — rule-based detectors) ──────────────────────────
    # Global kill switch + per-detector toggles. NULL means "use default".
    supervisor_dry_run_only:           Mapped[Optional[bool]] = mapped_column(Boolean)  # default False
    supervisor_false_success_enabled:  Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_dirty_pr_enabled:       Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_dirty_pr_min_age_min:   Mapped[Optional[int]]  = mapped_column(Integer)  # default 60
    supervisor_dirty_pr_idle_min:      Mapped[Optional[int]]  = mapped_column(Integer)  # default 30
    supervisor_auto_plan_enabled:      Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_auto_plan_min_unsprinted: Mapped[Optional[int]] = mapped_column(Integer) # default 3
    supervisor_merge_stall_enabled:    Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_merge_stall_min_min:    Mapped[Optional[int]]  = mapped_column(Integer)  # default 60
    supervisor_overlap_pr_enabled:     Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    # Phase-1 supervisor — additional detectors added in migration 038
    supervisor_kill_recovery_enabled:        Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_orphan_approved_enabled:      Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_orphan_approved_min_age_hours: Mapped[Optional[int]] = mapped_column(Integer)  # default 24
    supervisor_orphan_approved_threshold:    Mapped[Optional[int]]  = mapped_column(Integer)  # default 1
    supervisor_rapid_flap_enabled:           Mapped[Optional[bool]] = mapped_column(Boolean)  # default True
    supervisor_rapid_flap_window_hours:      Mapped[Optional[int]]  = mapped_column(Integer)  # default 1
    supervisor_rapid_flap_min_transitions:   Mapped[Optional[int]]  = mapped_column(Integer)  # default 5

    updated_at:        Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # ── Poller distributed lock ───────────────────────────────────────────────
    # Atomically acquired by poller on startup; heartbeat refreshed every 15s.
    # If heartbeat_at is NULL or > 30s old the lock is considered stale/free.
    poller_pid:          Mapped[Optional[int]]      = mapped_column(Integer)
    poller_host:         Mapped[Optional[str]]      = mapped_column(Text)
    poller_locked_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    poller_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class PMUser(Base):
    """
    PM accounts. If any rows exist, they override env-var Basic Auth entirely.
    Passwords stored as passlib bcrypt hashes.
    """
    __tablename__ = "pm_users"

    id:            Mapped[int]     = mapped_column(Integer, primary_key=True)
    name:          Mapped[str]     = mapped_column(Text, nullable=False)
    username:      Mapped[str]     = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str]     = mapped_column(Text, nullable=False)
    created_at:    Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        CheckConstraint(f"level IN {ALERT_LEVELS}", name="ck_alerts_level"),
        CheckConstraint("retry_count >= 0", name="ck_alerts_retry_count"),
    )

    id:          Mapped[int]           = mapped_column(Integer, primary_key=True)
    product_id:  Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("products.id", ondelete="SET NULL"))
    level:       Mapped[str]           = mapped_column(Text, nullable=False)
    message:     Mapped[str]           = mapped_column(Text, nullable=False)
    delivered:   Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)
    retry_count: Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())


# ── JIRA-like tracking models ──────────────────────────────────────────────────

class FeatureComment(Base):
    """Per-feature discussion thread. Written by PM, poller, or agents."""
    __tablename__ = "feature_comments"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True)
    feature_id: Mapped[int]           = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True)
    author:     Mapped[str]           = mapped_column(Text, nullable=False, default="pm")
    body:       Mapped[str]           = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    feature: Mapped["Feature"] = relationship("Feature", back_populates="comments")


class FeatureChangelog(Base):
    """Field-level audit trail — auto-populated on every PATCH /api/features/{id}."""
    __tablename__ = "feature_changelog"

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True)
    feature_id: Mapped[int]           = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True)
    field:      Mapped[str]           = mapped_column(Text, nullable=False)
    old_value:  Mapped[Optional[str]] = mapped_column(Text)
    new_value:  Mapped[Optional[str]] = mapped_column(Text)
    changed_by: Mapped[str]           = mapped_column(Text, nullable=False, default="poller")
    changed_at: Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())

    feature: Mapped["Feature"] = relationship("Feature", back_populates="changelog")


class Label(Base):
    """User-defined label per product. Re-usable across features."""
    __tablename__ = "labels"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_labels_product_name"),
    )

    id:         Mapped[int]  = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int]  = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    name:       Mapped[str]  = mapped_column(Text, nullable=False)
    color:      Mapped[str]  = mapped_column(Text, nullable=False, default="#6366f1")

    features: Mapped[List["FeatureLabel"]] = relationship("FeatureLabel", back_populates="label", cascade="all, delete")


class FeatureLabel(Base):
    """Many-to-many join table: features ↔ labels."""
    __tablename__ = "feature_labels"
    __table_args__ = (
        {"extend_existing": True},
    )

    feature_id: Mapped[int] = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), primary_key=True)
    label_id:   Mapped[int] = mapped_column(Integer, ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True)

    feature: Mapped["Feature"] = relationship("Feature", back_populates="labels")
    label:   Mapped["Label"]   = relationship("Label", back_populates="features")


class Phase(Base):
    """Top-level planning container grouping sprints under a named phase."""
    __tablename__ = "phases"
    __table_args__ = (
        CheckConstraint("status IN ('planned', 'active', 'completed')", name="ck_phases_status"),
    )

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int]           = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    name:       Mapped[str]           = mapped_column(Text, nullable=False)
    goal:       Mapped[Optional[str]] = mapped_column(Text)
    order:      Mapped[int]           = mapped_column(Integer, nullable=False, default=0)
    status:     Mapped[str]           = mapped_column(Text, nullable=False, default="planned")

    sprints: Mapped[list["Sprint"]] = relationship("Sprint", back_populates="phase", order_by="Sprint.id")


class Sprint(Base):
    """Time-boxed batch of features per product, optionally grouped under a Phase."""
    __tablename__ = "sprints"
    __table_args__ = (
        CheckConstraint("status IN ('planned', 'active', 'completed')", name="ck_sprints_status"),
    )

    id:         Mapped[int]           = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int]           = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    phase_id:   Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("phases.id", ondelete="SET NULL"), nullable=True, index=True)
    name:       Mapped[str]           = mapped_column(Text, nullable=False)
    goal:       Mapped[Optional[str]] = mapped_column(Text)
    start_date: Mapped[Optional[date]] = mapped_column(Date)
    end_date:   Mapped[Optional[date]] = mapped_column(Date)
    status:         Mapped[str]            = mapped_column(Text, nullable=False, default="active")
    release_notes:  Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    dod_status:     Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=dict)
    retro_doc_path: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
    completed_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    branch_name:    Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
    pr_number:      Mapped[Optional[int]]  = mapped_column(Integer, nullable=True)
    pr_url:         Mapped[Optional[str]]  = mapped_column(String(500), nullable=True)
    # "normal" = standard delivery sprint (counts toward DoD, capacity, etc.)
    # "blocked" = per-product holding pen for features that exhausted
    #             max_fix_attempts. Excluded from active-sprint selection,
    #             DoD gates, sprint cap, sprint-PR provisioning. PM-only.
    kind:           Mapped[str]            = mapped_column(String(16), nullable=False, default="normal")

    phase: Mapped[Optional["Phase"]] = relationship("Phase", back_populates="sprints")


class FeatureLink(Base):
    """Directed relationship edge between two features."""
    __tablename__ = "feature_links"
    __table_args__ = (
        UniqueConstraint("source_id", "target_id", "link_type", name="uq_feature_links"),
        CheckConstraint(
            "link_type IN ('blocks', 'is_blocked_by', 'relates_to', 'duplicates')",
            name="ck_feature_links_type",
        ),
    )

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    source_id:  Mapped[int]      = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id:  Mapped[int]      = mapped_column(Integer, ForeignKey("features.id", ondelete="CASCADE"), nullable=False, index=True)
    link_type:  Mapped[str]      = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    source: Mapped["Feature"] = relationship("Feature", foreign_keys=[source_id], back_populates="links_out")
    target: Mapped["Feature"] = relationship("Feature", foreign_keys=[target_id], back_populates="links_in")


class SupervisorAction(Base):
    """One row per supervisor-detector firing.

    The Phase-1 supervisor (orchestrator/supervisor.py) is a set of rule-based
    detectors that catch stuck-state patterns the deterministic orchestrator
    misses. Every firing — whether it actually mutated state or just ran in
    dry-run — writes one row here so PMs can audit what the system has been
    doing automatically. Surfaced in the UI under each product's Corrections
    tab and the global /admin/supervisor page.
    """
    __tablename__ = "supervisor_actions"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    detector:    Mapped[str]      = mapped_column(String(40), nullable=False)
    product_id:  Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    target_type: Mapped[str]      = mapped_column(String(20), nullable=False)
    target_id:   Mapped[str]      = mapped_column(String(100), nullable=False)
    action:      Mapped[str]      = mapped_column(String(40), nullable=False)
    reason:      Mapped[str]      = mapped_column(Text, nullable=False)
    dry_run:     Mapped[bool]     = mapped_column(Boolean, nullable=False, default=False)
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
