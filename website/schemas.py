"""
Pydantic schemas — request/response shapes for the REST API.
Separate from ORM models so the API contract is explicit.
"""

from datetime import datetime
from typing import Optional, List, Literal
from pydantic import BaseModel, field_validator

# Valid status values — kept in sync with models.py constants
PM_ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    # PM can make these status changes via the website
    "Pending":  ["Approved", "Rejected"],
    "Approved": ["Pending"],
    "Blocked":  ["Approved", "Rejected"],
    "Pushed":   ["Reverted"],
    "Rejected": ["Pending"],
    "Reverted": ["Pending"],
}


# ── Products ─────────────────────────────────────────────────────────────────

class ProductCreate(BaseModel):
    working_dir: str


class ProductUpdate(BaseModel):
    name:            Optional[str]       = None
    github_repo:     Optional[str]       = None
    tech_stack:      Optional[List[str]] = None
    type:            Optional[str]       = None
    status:          Optional[str]       = None
    analysis_status: Optional[str]       = None
    config:          Optional[dict]      = None
    last_run_at:     Optional[datetime]  = None


class ProductOut(BaseModel):
    id:              int
    working_dir:     str
    name:            Optional[str]
    github_repo:     Optional[str]
    tech_stack:      Optional[List[str]]
    type:            str
    status:          str
    analysis_status: str
    config:          Optional[dict]
    last_run_at:     Optional[datetime]
    created_at:      datetime
    updated_at:      datetime

    model_config = {"from_attributes": True}


# ── Features ─────────────────────────────────────────────────────────────────

class FeatureCreate(BaseModel):
    product_id:  int
    name:        str
    description: Optional[str] = None
    priority:    int = 50
    depends_on:  Optional[int] = None
    source:      str = "pm"

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


class FeatureUpdate(BaseModel):
    """Used by Claude (poller) to update feature status during implementation."""
    status:         Optional[str] = None
    fix_attempts:   Optional[int] = None
    branch_name:    Optional[str] = None
    pr_url:         Optional[str] = None
    pr_number:      Optional[int] = None
    blocked_reason: Optional[str] = None


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
    id:             int
    product_id:     int
    name:           str
    description:    Optional[str]
    status:         str
    priority:       int
    depends_on:     Optional[int]
    fix_attempts:   int
    source:         str
    branch_name:    Optional[str]
    pr_url:         Optional[str]
    pr_number:      Optional[int]
    blocked_reason: Optional[str]
    created_at:     datetime
    updated_at:     datetime

    model_config = {"from_attributes": True}


# ── Sessions ─────────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    product_id:  int
    session_uid: str


class SessionEnd(BaseModel):
    ended_at:           Optional[datetime] = None
    exit_code:          Optional[int]      = None
    container_id:       Optional[str]      = None
    features_attempted: Optional[int]      = None
    features_pushed:    Optional[int]      = None
    notes:              Optional[str]      = None


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

    model_config = {"from_attributes": True}


# ── Alerts ───────────────────────────────────────────────────────────────────

class AlertOut(BaseModel):
    id:          int
    product_id:  Optional[int]
    level:       str
    message:     str
    delivered:   bool
    created_at:  datetime

    model_config = {"from_attributes": True}


# ── Misc ─────────────────────────────────────────────────────────────────────

class ResetStuckResult(BaseModel):
    reset_count: int
