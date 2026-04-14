"""
Pydantic schemas — request/response shapes for the REST API.
Separate from ORM models so the API contract is explicit.
"""

from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, field_validator

# Valid status values — kept in sync with models.py constants
PM_ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    # PM can make these status changes via the website.
    # Note: agent endpoints (PATCH /api/features/{id}) bypass this table entirely —
    # agents have unrestricted status authority so they can drive the full pipeline.
    "Pending":  ["Approved", "Rejected", "Deferred"],
    "Approved": ["Pending"],
    "Blocked":  ["Approved", "Rejected"],
    "Pushed":   ["Reverted"],
    "Rejected": ["Approved", "Pending"],
    "Reverted": ["Pending"],
    "Deferred": ["Approved", "Pending"],
}


# ── Products ─────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    working_dir:  str
    name:         Optional[str]       = None
    type:         Optional[str]       = None
    tech_stack:   Optional[List[str]] = None
    status:       Optional[str]       = None
    github_repo:  Optional[str]       = None
    config:       Optional[dict]      = None


class ProductUpdate(BaseModel):
    name:            Optional[str]       = None
    github_repo:     Optional[str]       = None
    tech_stack:      Optional[List[str]] = None
    type:            Optional[str]       = None
    status:          Optional[str]       = None
    analysis_status: Optional[str]       = None
    config:          Optional[dict]      = None
    last_run_at:     Optional[datetime]  = None
    run_now:         Optional[bool]      = None
    custom_prompt:   Optional[str]       = None


class ProductOut(BaseModel):
    id:                 int
    working_dir:        str
    name:               Optional[str]
    github_repo:        Optional[str]
    tech_stack:         Optional[List[str]]
    type:               str
    status:             str
    analysis_status:    str
    config:             Optional[dict]
    last_run_at:        Optional[datetime]
    run_now:            bool = False
    custom_prompt:      Optional[str] = None
    quiet_hours_start:  Optional[int] = None
    quiet_hours_end:    Optional[int] = None
    daily_session_cap:      Optional[int] = None
    max_features_per_run:   Optional[int] = None
    created_at:             datetime
    updated_at:         datetime

    model_config = {"from_attributes": True}


# ── Features ─────────────────────────────────────────────────────────────────

class FeatureCreate(BaseModel):
    product_id:   int
    name:         str
    description:  Optional[str] = None
    priority:     int = 50
    depends_on:   Optional[int] = None
    source:       str = "pm"
    feature_type: str = "feature"
    skip_design:  bool = False

    @field_validator("priority")
    @classmethod
    def priority_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("priority must be between 1 and 100")
        return v

    @field_validator("source")
    @classmethod
    def source_valid(cls, v: str) -> str:
        if v not in ("pm", "ai"):
            raise ValueError("source must be 'pm' or 'ai'")
        return v

    @field_validator("feature_type")
    @classmethod
    def feature_type_valid(cls, v: str) -> str:
        if v not in ("feature", "bug", "chore"):
            raise ValueError("feature_type must be 'feature', 'bug', or 'chore'")
        return v


class FeatureUpdate(BaseModel):
    """Used by Claude agents to update feature status during implementation."""
    status:          Optional[str]  = None
    feature_type:    Optional[str]  = None
    fix_attempts:    Optional[int]  = None
    branch_name:     Optional[str]  = None
    pr_url:          Optional[str]  = None
    pr_number:       Optional[int]  = None
    blocked_reason:  Optional[str]  = None
    design_doc:      Optional[str]  = None
    design_doc_path: Optional[str]  = None
    review_outcome:  Optional[str]  = None
    review_notes:    Optional[str]  = None
    session_uid:     Optional[str]  = None  # review authorship — stored in feature_reviews, not on feature
    skip_design:     Optional[bool] = None  # PM may override design requirement


class FeatureStatusUpdate(BaseModel):
    """Used by PM (website form) — only allows PM-permitted transitions."""
    status: str

    @field_validator("status")
    @classmethod
    def status_is_pm_target(cls, v: str) -> str:
        all_targets = {s for targets in PM_ALLOWED_TRANSITIONS.values() for s in targets}
        if v not in all_targets:
            raise ValueError(f"'{v}' is not a valid PM-controlled status")
        return v


class FeatureOut(BaseModel):
    id:              int
    product_id:      int
    name:            str
    description:     Optional[str]
    status:          str
    feature_type:    str = "feature"
    priority:        int
    depends_on:      Optional[int]
    fix_attempts:    int
    source:          str
    branch_name:     Optional[str]
    pr_url:          Optional[str]
    pr_number:       Optional[int]
    blocked_reason:  Optional[str]
    skip_design:     bool = False
    design_doc:      Optional[str] = None
    design_doc_path: Optional[str] = None
    review_outcome:  Optional[str] = None
    review_notes:    Optional[str] = None
    created_at:      datetime
    updated_at:      datetime

    model_config = {"from_attributes": True}


# ── Feature Reviews ──────────────────────────────────────────────────────────

class FeatureReviewOut(BaseModel):
    id:             int
    feature_id:     int
    review_outcome: str
    review_notes:   Optional[str]
    session_uid:    Optional[str]
    created_at:     datetime

    model_config = {"from_attributes": True}


# ── Sessions ─────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    product_id:   int
    session_uid:  str
    container_id: Optional[str] = None   # set at launch time: pf-{id}-{uid}
    persona:      Optional[str] = None
    backend:      Optional[str] = None   # "claude" | "ollama"


class SessionEnd(BaseModel):
    ended_at:           Optional[datetime] = None
    exit_code:          Optional[int]      = None
    container_id:       Optional[str]      = None
    features_attempted: Optional[int]      = None
    features_pushed:    Optional[int]      = None
    notes:              Optional[str]      = None
    tokens_input:       Optional[int]      = None
    tokens_output:      Optional[int]      = None
    cost_usd:           Optional[float]    = None
    persona:            Optional[str]      = None


class SessionOut(BaseModel):
    id:                 int
    product_id:         int
    session_uid:        str
    container_id:       Optional[str]
    started_at:         datetime
    ended_at:           Optional[datetime]
    exit_code:          Optional[int]
    features_attempted: int
    features_pushed:    int
    tokens_input:       Optional[int]
    tokens_output:      Optional[int]
    cost_usd:           Optional[float]
    persona:            Optional[str]
    backend:            Optional[str]
    notes:              Optional[str]

    model_config = {"from_attributes": True}


# ── Poller Distributed Lock ──────────────────────────────────────────────────

class PollerLockRequest(BaseModel):
    pid:  int
    host: str

class PollerLockOut(BaseModel):
    pid:          int
    host:         str
    locked_at:    datetime
    heartbeat_at: datetime

class PollerHeartbeatRequest(BaseModel):
    pid:  int
    host: str


# ── Alerts ───────────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id:          int
    product_id:  Optional[int]
    level:       str
    message:     str
    delivered:   bool
    created_at:  datetime

    model_config = {"from_attributes": True}


# ── System Config ─────────────────────────────────────────────────────────────

class SystemConfigOut(BaseModel):
    products_root_dir:     Optional[str] = None
    github_org:            Optional[str] = None
    github_pat:            Optional[str] = None
    github_ssh_key_name:   str = "productfactory-deploy"
    slack_webhook_url:     Optional[str] = None
    github_webhook_secret: Optional[str] = None
    max_sessions_per_day:  Optional[int] = None

    model_config = {"from_attributes": True}


class ProductSchedule(BaseModel):
    quiet_hours_start:    Optional[int] = None
    quiet_hours_end:      Optional[int] = None
    daily_session_cap:    Optional[int] = None
    max_features_per_run: Optional[int] = None


class BulkApprove(BaseModel):
    feature_ids: List[int]


# ── PM Users ──────────────────────────────────────────────────────────────────

class PMUserCreate(BaseModel):
    name:     str
    username: str
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("password must be at least 8 characters")
        return v


class PMUserOut(BaseModel):
    id:         int
    name:       str
    username:   str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── LLM Recommendations ───────────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    vision:          str
    preferred_stack: str = "python"


class ArticulateRequest(BaseModel):
    vision:          str
    preferred_stack: str = "python"


# ── Misc ─────────────────────────────────────────────────────────────────────

class ResetStuckResult(BaseModel):
    reset_count: int
