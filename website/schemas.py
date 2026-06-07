"""
Pydantic schemas — request/response shapes for the REST API.
Separate from ORM models so the API contract is explicit.
"""

from datetime import datetime, date
from typing import Optional, List
from pydantic import BaseModel, field_validator, model_validator

# Valid status values — kept in sync with models.py constants
PM_ALLOWED_TRANSITIONS: dict[str, list[str]] = {
    # PM can make these status changes via the website.
    # Note: agent endpoints (PATCH /api/features/{id}) bypass this table entirely —
    # agents have unrestricted status authority so they can drive the full pipeline.
    # Pending is the "park for later consideration" state; PM can pull a feature
    # back from any active sprint state into Pending (the handler also clears
    # sprint_id and review_outcome so it's a clean slate).
    "Pending":     ["Approved", "Rejected", "Deferred"],
    "Approved":    ["Pending"],
    "Designed":    ["Pending"],
    "Implementing":["Pending"],
    "Reviewing":   ["Pending"],
    "Reviewed":    ["Pending"],
    "Blocked":     ["Approved", "Rejected", "Pending"],
    "Pushed":      ["Reverted"],
    "Rejected":    ["Approved", "Pending"],
    "Reverted":    ["Pending"],
    "Deferred":    ["Approved", "Pending"],
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
    run_trainer_now: Optional[bool]      = None
    run_persona_now: Optional[str]       = None
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
    run_trainer_now:    bool = False
    run_persona_now:    Optional[str] = None
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
    phase_id:     Optional[int] = None
    parent_id:    Optional[int] = None  # sizing-gate splits point children back
    story_points: Optional[int] = None
    due_date:     Optional[date] = None
    status:       str = "Pending"

    @field_validator("name")
    @classmethod
    def name_substantive(cls, v: str) -> str:
        # Reject stub/placeholder names that the designer can't work with.
        # Canonical 2026-06-01 incident: 6 of 13 Blocked features on
        # DocumentSign (1110, 1111, 1151, 1164, 1165, 1172) were stubs
        # with name in {"test","x","DESIGNER_ASSIGN"} and null/trivial
        # description — they polluted the backlog, burned designer cycles,
        # and each got rejected with `Insufficient spec — cannot design
        # from empty story`. Reject at the API boundary instead.
        s = (v or "").strip()
        if len(s) < 3:
            raise ValueError(
                f"name must be at least 3 characters (got {len(s)}); "
                f"give the feature a descriptive title"
            )
        _STUB_NAMES = {
            "test", "tests", "x", "y", "todo", "tbd", "draft",
            "placeholder", "designer_assign", "designer-assign",
            "fixme", "wip", "temp", "tmp", "asdf", "foo", "bar",
            "feature", "bug", "chore",  # the type, not a name
        }
        if s.lower() in _STUB_NAMES:
            raise ValueError(
                f"name {v!r} looks like a stub/placeholder — give the "
                f"feature a real, descriptive name. To file a quick-note "
                f"task, use a feature_comments POST on an existing feature."
            )
        return v

    @model_validator(mode="after")
    def description_substantive(self) -> "FeatureCreate":
        # Description is required for any creatable feature. Stub features
        # with null/trivial descriptions cannot be designed and waste
        # designer cycles. Reconciler-filed chores already pass multi-
        # paragraph descriptions; PMs creating real features have something
        # to say. The 10-char minimum filters single-word "yes"/"fix it"
        # without being noisy on terse genuine descriptions.
        #
        # Uses model_validator (not field_validator) so the check runs even
        # when `description` is omitted from the request body and falls
        # through to the Optional[str]=None default — field_validators
        # skip default values in Pydantic v2, but the API still needs to
        # reject "no description provided" the same as "description: ''".
        v = self.description
        if v is None or not v.strip():
            raise ValueError(
                "description is required — the designer cannot author a "
                "design doc from an empty story. Include at least one "
                "sentence describing the desired behavior."
            )
        if len(v.strip()) < 10:
            raise ValueError(
                f"description must be at least 10 characters (got "
                f"{len(v.strip())}); the designer needs enough context "
                f"to author a design doc"
            )
        return self

    @field_validator("priority")
    @classmethod
    def priority_range(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError("priority must be between 1 and 100")
        return v

    @field_validator("status")
    @classmethod
    def status_allowed(cls, v: str) -> str:
        if v not in ("Pending", "Approved"):
            raise ValueError("status must be 'Pending' or 'Approved'")
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
    last_changes_signature: Optional[str] = None  # supervisor.detect_repeated_review_feedback
    repeated_changes_count: Optional[int] = None  # ditto
    session_uid:     Optional[str]  = None  # review authorship — stored in feature_reviews, not on feature
    phase_id:        Optional[int]  = None  # reassign to a different phase
    parent_id:       Optional[int]  = None  # sizing-gate split tree
    merge_notes:     Optional[str]  = None  # per-feature release-notes draft
    expected_version: Optional[int] = None  # optimistic lock — if provided, update is rejected on mismatch
    # Caller-supplied attribution: who/what is making this change. Read by
    # the rank-guard handler in main.py to allow trusted internal callers
    # (rollback, kill_recovery, supervisor, post-doc:rollback, pm) to bypass
    # the IV.1 rank-downgrade rule. Without this field declared here, Pydantic
    # silently strips it from the PATCH body and every legitimate downgrade
    # got rejected with 422 — incident 2026-05-06 left 5 features stuck after
    # an Ollama exit=2 storm because every rollback was actually a silent 422.
    # Also written to feature_changelog as the "changed_by" attribution.
    changed_by:      Optional[str]  = None


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
    version:         int = 0
    design_doc:      Optional[str] = None
    design_doc_path: Optional[str] = None
    review_outcome:  Optional[str] = None
    review_notes:    Optional[str] = None
    last_changes_signature: Optional[str] = None
    repeated_changes_count: int = 0
    phase_id:        Optional[int] = None
    parent_id:       Optional[int] = None
    merge_notes:     Optional[str] = None
    story_points:    Optional[int] = None
    due_date:        Optional[date] = None
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
    status:       Optional[str] = None   # FSM default handled server-side


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
    status:             Optional[str]      = None   # FSM transition on close
    kill_reason:        Optional[str]      = None


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




# ── LLM Recommendations ───────────────────────────────────────────────────────

class RecommendRequest(BaseModel):
    vision:          str
    preferred_stack: str = "python"


class ArticulateRequest(BaseModel):
    vision:          str
    preferred_stack: str = "python"


class RecommendStackRequest(BaseModel):
    """Wizard step 3: given a finalised vision, recommend a tech stack."""
    vision: str


class RecommendUITemplateRequest(BaseModel):
    """Wizard step 4: given vision + chosen stack, recommend a UI template."""
    vision:   str
    stack_id: str


# ── Misc ─────────────────────────────────────────────────────────────────────

class ResetStuckResult(BaseModel):
    reset_count: int


# ── JIRA-like tracking schemas ─────────────────────────────────────────────────

class FeatureCommentCreate(BaseModel):
    author: str = "pm"
    body:   str


class FeatureCommentOut(BaseModel):
    id:         int
    feature_id: int
    author:     str
    body:       str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChangelogEntryOut(BaseModel):
    id:         int
    feature_id: int
    field:      str
    old_value:  Optional[str]
    new_value:  Optional[str]
    changed_by: str
    changed_at: datetime

    model_config = {"from_attributes": True}


class LabelCreate(BaseModel):
    product_id: int
    name:       str
    color:      str = "#6366f1"


class LabelOut(BaseModel):
    id:         int
    product_id: int
    name:       str
    color:      str

    model_config = {"from_attributes": True}


class FeatureLabelAdd(BaseModel):
    label_id: int


class PhaseCreate(BaseModel):
    product_id: int
    name:       str
    goal:       Optional[str] = None
    order:      int = 0


class PhaseUpdate(BaseModel):
    name:       Optional[str] = None
    goal:       Optional[str] = None
    order:      Optional[int] = None
    # Human-in-loop gate (migration 045). PM PATCHes 'approved' to unlock the
    # next phase. Replaces the prior orphan `status` field (which had no
    # backing column). 'open'/'awaiting_review' are normally set by the
    # detector, not the PM, but are accepted here for manual override.
    gate_state: Optional[str] = None


class PhaseOut(BaseModel):
    id:         int
    product_id: int
    name:       str
    goal:       Optional[str]
    order:      int
    gate_state: str = "open"
    report:     Optional[dict] = None

    model_config = {"from_attributes": True}


class FeatureLinkCreate(BaseModel):
    target_id: int
    link_type: str

    @field_validator("link_type")
    @classmethod
    def link_type_valid(cls, v: str) -> str:
        if v not in ("blocks", "is_blocked_by", "relates_to", "duplicates"):
            raise ValueError("link_type must be blocks, is_blocked_by, relates_to, or duplicates")
        return v


class FeatureLinkOut(BaseModel):
    id:         int
    source_id:  int
    target_id:  int
    link_type:  str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Internal /api/* request bodies (Phase #9) ────────────────────────────────
# Pydantic shapes for endpoints that previously took body: dict — replaces
# silent no-op-on-typo behavior with a 422 on malformed payloads.


class SessionKillRequest(BaseModel):
    reason: str = "watchdog"


class SupervisorActionRequest(BaseModel):
    detector:    str
    target_type: str
    target_id:   str
    action:      str
    reason:      str
    product_id:  Optional[int]  = None
    dry_run:     bool           = False


class SessionLogAppendRequest(BaseModel):
    lines: List[str]
