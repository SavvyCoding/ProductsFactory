"""
SQLAlchemy ORM models — mirrors db/schema.sql exactly.
Add columns here + create an Alembic migration to apply to live DB.
"""

from datetime import datetime
from typing import Optional, List
from sqlalchemy import (
    Integer, String, Text, DateTime, Boolean, ARRAY,
    ForeignKey, func, CheckConstraint, event, Numeric
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from website.database import Base


PRODUCT_STATUSES   = ('registered', 'discovering', 'discovered', 'ready', 'paused', 'error',
                      'greenfield_pending')
PRODUCT_TYPES      = ('greenfield', 'brownfield')
ANALYSIS_STATUSES  = ('pending', 'running', 'done')
FEATURE_STATUSES   = ('Pending', 'Approved',
                      'Designing', 'Designed',
                      'Implementing', 'Implemented',
                      'Reviewing', 'Reviewed',
                      'Testing', 'Committed', 'Pushed',
                      'Blocked', 'Rejected', 'Reverted')
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
    type:            Mapped[str]           = mapped_column(Text, nullable=False, default="greenfield")
    status:          Mapped[str]           = mapped_column(Text, nullable=False, default="registered")
    analysis_status: Mapped[str]           = mapped_column(Text, nullable=False, default="pending")
    config:             Mapped[Optional[dict]] = mapped_column(JSONB)
    last_run_at:        Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    run_now:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    custom_prompt:      Mapped[Optional[str]]  = mapped_column(Text)
    quiet_hours_start:  Mapped[Optional[int]]  = mapped_column(Integer)
    quiet_hours_end:    Mapped[Optional[int]]  = mapped_column(Integer)
    daily_session_cap:  Mapped[Optional[int]]  = mapped_column(Integer)
    created_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    features: Mapped[List["Feature"]] = relationship("Feature", back_populates="product", cascade="all, delete")
    sessions:  Mapped[List["Session"]]  = relationship("Session",  back_populates="product", cascade="all, delete")


class Feature(Base):
    __tablename__ = "features"
    __table_args__ = (
        CheckConstraint(f"status IN {FEATURE_STATUSES}", name="ck_features_status"),
        CheckConstraint("priority BETWEEN 1 AND 100", name="ck_features_priority"),
        CheckConstraint("fix_attempts >= 0", name="ck_features_fix_attempts"),
        CheckConstraint("source IN ('pm', 'ai')", name="ck_features_source"),
    )

    id:             Mapped[int]            = mapped_column(Integer, primary_key=True)
    product_id:     Mapped[int]            = mapped_column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    name:           Mapped[str]            = mapped_column(Text, nullable=False)
    description:    Mapped[Optional[str]]  = mapped_column(Text)
    status:         Mapped[str]            = mapped_column(Text, nullable=False, default="Pending")
    priority:       Mapped[int]            = mapped_column(Integer, nullable=False, default=50)
    depends_on:     Mapped[Optional[int]]  = mapped_column(Integer, ForeignKey("features.id"))
    fix_attempts:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    source:         Mapped[str]            = mapped_column(Text, nullable=False, default="pm")
    branch_name:    Mapped[Optional[str]]  = mapped_column(Text)
    pr_url:         Mapped[Optional[str]]  = mapped_column(Text)
    pr_number:      Mapped[Optional[int]]  = mapped_column(Integer)
    blocked_reason: Mapped[Optional[str]]  = mapped_column(Text)
    skip_design:    Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    design_doc:     Mapped[Optional[str]]  = mapped_column(Text)
    design_doc_path: Mapped[Optional[str]] = mapped_column(Text)
    review_outcome: Mapped[Optional[str]]  = mapped_column(String(32))
    review_notes:   Mapped[Optional[str]]  = mapped_column(Text)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    product: Mapped["Product"] = relationship("Product", back_populates="features")


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

    product: Mapped["Product"] = relationship("Product", back_populates="sessions")


class SystemConfig(Base):
    """
    Global system configuration — single row (id=1 always).
    Upserted via POST /admin/settings. Never create multiple rows.
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
    updated_at:            Mapped[datetime]       = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


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
