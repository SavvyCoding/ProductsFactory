"""
ProductFactory PM Website — FastAPI application.

HTML pages (require Basic Auth):
  GET  /                               — dashboard: product list + register tabs
  GET  /admin                          — admin: system config + PM management
  GET  /product/{id}                   — product detail: kanban + feature form
  GET  /product/{id}/progress          — progress.md viewer (fetched from GitHub)

  POST /product/register               — brownfield: register existing repo
  POST /product/register/greenfield    — greenfield: scaffold new product
  POST /product/{id}/features          — PM adds a feature
  POST /product/{id}/features/{fid}/status  — PM changes feature status
  POST /product/{id}/pause             — pause product
  POST /product/{id}/resume            — resume product
  POST /product/{id}/trigger_analysis  — trigger brownfield Analysis Run
  POST /admin/settings                 — save system configuration
  POST /admin/pms                      — add PM account
  POST /admin/pms/{id}/delete          — remove PM account

REST API (used by poller — no auth on poller-only routes):
  GET  /api/products                   — list all products
  POST /api/products                   — register product (JSON)
  GET  /api/products/next              — next product for poller (fair queue)
  PATCH /api/products/{id}             — update discovered fields
  GET  /api/features/approved          — approved features for product (batch planning)
  POST /api/features                   — create feature (PM or AI)
  PATCH /api/features/{id}             — update feature status (Claude)
  GET  /api/features/{id}/reviews      — full review history for a feature
  GET  /api/features/{id}/comments     — feature comments (pm, poller, agents)
  POST /api/features/{id}/comments     — add comment
  GET  /api/features/{id}/changelog    — field-level audit trail
  GET  /api/features/{id}/labels       — labels on a feature
  POST /api/features/{id}/labels       — apply label to feature
  DELETE /api/features/{id}/labels/{id} — remove label
  POST /api/features/{id}/links        — link two features
  GET  /api/features/{id}/links        — list feature links
  DELETE /api/features/{id}/links/{id} — remove link
  GET  /api/features/search            — full-text search (q, product_id)
  GET  /api/features/overdue           — features past due_date
  PATCH /api/features/{id}/pm-status   — PM status change with transition validation
  DELETE /api/features/{id}            — delete a Rejected feature (PM only)
  POST /api/features/reset_stuck       — reset Implementing→Approved if >2h (poller)
  POST /api/labels                     — create label
  GET  /api/products/{id}/labels       — list labels for product
  POST /api/sprints                    — create sprint
  GET  /api/products/{id}/sprints      — list sprints
  GET  /api/products/{id}/sprints/active — active sprint
  PATCH /api/sprints/{id}              — update sprint
  GET  /api/products/{id}/open_pr_count — PR count gate (poller)
  GET  /api/system-config              — system config for poller (no auth)
  POST /api/recommend/features         — LLM-generated feature suggestions
  GET  /api/alerts/unread              — unread alerts for nav badge
  POST /api/sessions                   — record session start (poller)
  PATCH /api/sessions/{id}             — record session end (poller)

Start: uvicorn website.main:app --host 0.0.0.0 --port 8080
"""

import asyncio
import hashlib
import hmac
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import mistune
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import bcrypt as _bcrypt_lib
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from website.database import get_db
from website.models import (
    Product, Feature, FeatureReview, Session as DBSession, Alert, SystemConfig, PMUser,
    FeatureComment, FeatureChangelog, Label, FeatureLabel, Phase, Sprint, FeatureLink,
)
from website.auth import require_auth
from website import schemas
from website.github import fetch_progress_md, fetch_architecture_md, count_open_prs, list_open_prs, merge_pr, close_pr
from website.schemas import PM_ALLOWED_TRANSITIONS

app = FastAPI(title="ProductFactory PM", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="website/static"), name="static")
templates = Jinja2Templates(directory="website/templates")

# ── Video file serving ────────────────────────────────────────────────────────
# Products root on the host is mounted read-only at /workspace inside the container.
# PRODUCTS_BASE_DIR env var holds the host-side path (e.g. C:/Users/you/Products).
# We map product.working_dir → /workspace/{relative_part} to locate output/ videos.
_HOST_PRODUCTS_BASE     = os.environ.get("PRODUCTS_BASE_DIR", "").rstrip("/\\").replace("\\", "/")
STUCK_FEATURE_HOURS     = int(os.environ.get("STUCK_FEATURE_TIMEOUT_HOURS", "2"))
SESSION_LOG_MAXLEN      = int(os.environ.get("SESSION_LOG_MAXLEN", "1000"))
SESSION_LOG_WARN_AT     = int(SESSION_LOG_MAXLEN * 0.9)  # warn when buffer is 90% full

# Claude model used for PM-facing LLM features (feature recommendations, vision articulation)
_RECOMMENDATION_MODEL = "claude-haiku-4-5-20251001"


async def _llm_call(prompt: str, max_tokens: int = 3000) -> str:
    """Call Claude — tries CLI first (OAuth subscription), falls back to SDK (API key)."""
    import subprocess, shutil

    # 1) Try claude CLI (uses OAuth tokens from ~/.claude)
    claude_bin = shutil.which("claude")
    if claude_bin:
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [claude_bin, "-p", prompt, "--output-format", "text"],
                    capture_output=True, text=True, timeout=120,
                ),
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception:
            pass  # fall through to SDK

    # 2) Fall back to Anthropic SDK with API key
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=501,
            detail="Neither Claude CLI nor ANTHROPIC_API_KEY is available",
        )
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=_RECOMMENDATION_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()
_CONTAINER_WORKSPACE = Path("/workspace")


def _output_dir_for_product(working_dir: str) -> Path:
    """Map a product's host working_dir to its output/ dir visible inside the container."""
    norm = working_dir.replace("\\", "/").rstrip("/")
    if _HOST_PRODUCTS_BASE and norm.lower().startswith(_HOST_PRODUCTS_BASE.lower()):
        rel = norm[len(_HOST_PRODUCTS_BASE):].lstrip("/")
        return _CONTAINER_WORKSPACE / rel / "output"
    # Fallback: direct path (works when running outside Docker)
    return Path(working_dir) / "output"

_md = mistune.create_markdown(plugins=["table"])
def _hash_password(pw: str) -> str:
    return _bcrypt_lib.hashpw(pw.encode(), _bcrypt_lib.gensalt()).decode()

# ── In-memory session log store ───────────────────────────────────────────────
# product_id → deque of log lines (capped at 1000)
_session_logs: dict[int, deque] = defaultdict(lambda: deque(maxlen=SESSION_LOG_MAXLEN))
# product_id → list of asyncio.Queue (one per SSE subscriber)
_session_subscribers: dict[int, list] = defaultdict(list)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

async def _get_product_or_404(product_id: int, db: AsyncSession) -> Product:
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


async def _get_feature_or_404(feature_id: int, db: AsyncSession) -> Feature:
    feature = await db.get(Feature, feature_id)
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    return feature


async def _unread_alert_count(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).where(Alert.delivered == False)  # noqa: E712
    )
    return result.scalar_one()


async def _get_system_config(db: AsyncSession) -> SystemConfig | None:
    return await db.get(SystemConfig, 1)


_CFG_DEFAULTS = {
    # Poller
    "poll_interval":               60,
    "session_timeout_minutes":     90,
    "stale_threshold_minutes":     45,
    "auth_check_timeout":          30,
    "max_open_prs":                1,
    "pr_gate_sleep":               300,
    "stuck_feature_timeout_hours": 0.75,  # 45 minutes — matches stale session threshold
    "max_features_per_run":        5,
    "brownfield_file_threshold":   10,
    "auto_merge_enabled":            False,
    # Agent / Ollama
    "agent_backend":    "claude",
    "ollama_host":      "http://host.docker.internal:11434",
    "designer_model":   "gemma3:27b",
    "coder_model":      "qwen3-coder:30b",
    "ollama_timeout":   300,
    "bash_timeout":     180,
    "max_turns":        80,
    # Claude profile
    "claude_model":           "claude-sonnet-4-6",
    "claude_credentials_dir": "C:/Users/digvi/.claude",
    "ssh_keys_dir":           "",
}


def _cfg(config: SystemConfig | None, key: str):
    """Return DB value if set, else env var, else built-in default."""
    db_val = getattr(config, key, None) if config else None
    if db_val is not None:
        return db_val
    env_key = key.upper()
    env_val = os.environ.get(env_key)
    if env_val is not None:
        default = _CFG_DEFAULTS.get(key)
        try:
            return type(default)(env_val) if default is not None else env_val
        except (ValueError, TypeError):
            return env_val
    return _CFG_DEFAULTS.get(key)


def _config_as_dict(config: SystemConfig | None) -> dict:
    base = {
        "products_root_dir":     (config.products_root_dir  if config else "") or "",
        "github_org":            (config.github_org          if config else "") or "",
        "github_pat":            (config.github_pat          if config else "") or "",
        "github_ssh_key_name":   (config.github_ssh_key_name if config else "") or "productfactory-deploy",
        "slack_webhook_url":     (config.slack_webhook_url   if config else "") or "",
        "github_webhook_secret": (config.github_webhook_secret if config else "") or "",
        "max_sessions_per_day":  (config.max_sessions_per_day  if config else "") or "",
    }
    # Merge all operational settings with their effective values (DB → env → default)
    for key in _CFG_DEFAULTS:
        base[key] = _cfg(config, key)
    return base


# ══════════════════════════════════════════════════════════════════════════════
# HTML PAGES — PM views (all require Basic Auth)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_pm: str = Depends(require_auth),
):
    """Main dashboard — product list with Greenfield/Brownfield register tabs."""
    result = await db.execute(select(Product).order_by(Product.name))
    products = result.scalars().all()
    alert_count = await _unread_alert_count(db)
    config = await _get_system_config(db)

    # Feature counts per product for progress bars
    _ACTIVE_STATUSES = {"Approved", "Designing", "Designed", "Implementing", "Reviewing", "Reviewed"}
    counts_result = await db.execute(
        select(Feature.product_id, Feature.status, func.count().label("cnt"))
        .group_by(Feature.product_id, Feature.status)
    )
    feature_counts: dict[int, dict] = {}
    for row in counts_result:
        fc = feature_counts.setdefault(row.product_id, {"pushed": 0, "active": 0, "blocked": 0, "pending": 0, "total": 0})
        fc["total"] += row.cnt
        if row.status == "Pushed":
            fc["pushed"] += row.cnt
        elif row.status == "Blocked":
            fc["blocked"] += row.cnt
        elif row.status in _ACTIVE_STATUSES:
            fc["active"] += row.cnt
        elif row.status == "Pending":
            fc["pending"] += row.cnt

    # Active phase + sprint per product for dashboard cards
    phases_result = await db.execute(
        select(Phase).where(Phase.status == "active")
    )
    active_phases: dict[int, Phase] = {p.product_id: p for p in phases_result.scalars().all()}

    sprints_result = await db.execute(
        select(Sprint).where(Sprint.status == "active")
    )
    active_sprints: dict[int, Sprint] = {s.product_id: s for s in sprints_result.scalars().all()}

    # Last session persona per product
    sessions_result = await db.execute(
        select(DBSession.product_id, DBSession.persona, DBSession.started_at)
        .distinct(DBSession.product_id)
        .order_by(DBSession.product_id, DBSession.started_at.desc())
    )
    last_persona: dict[int, str] = {row.product_id: row.persona for row in sessions_result}

    return templates.TemplateResponse("index.html", {
        "request": request,
        "products": products,
        "alert_count": alert_count,
        "current_pm": current_pm,
        "products_root_dir": config.products_root_dir if config else None,
        "feature_counts": feature_counts,
        "active_phases": active_phases,
        "active_sprints": active_sprints,
        "last_persona": last_persona,
    })


@app.get("/api/products/running-count")
async def running_count(db: AsyncSession = Depends(get_db)):
    """Count products currently running — used by nav live indicator."""
    result = await db.execute(select(func.count()).select_from(Product).where(Product.status == "running"))
    count = result.scalar() or 0
    return {"count": count}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    saved: bool = False,
    db: AsyncSession = Depends(get_db),
    current_pm: str = Depends(require_auth),
):
    """Admin page — system config and PM account management."""
    alert_count = await _unread_alert_count(db)
    config = await _get_system_config(db)
    pms_result = await db.execute(select(PMUser).order_by(PMUser.created_at))
    pms = pms_result.scalars().all()
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "alert_count": alert_count,
        "current_pm": current_pm,
        "saved": saved,
        "config": _config_as_dict(config),
        "pms": pms,
    })


@app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(
    product_id: int, request: Request,
    db: AsyncSession = Depends(get_db),
    current_pm: str = Depends(require_auth),
):
    """Product detail — feature kanban + add-feature form."""
    product = await _get_product_or_404(product_id, db)
    feat_result = await db.execute(
        select(Feature)
        .where(Feature.product_id == product_id)
        .order_by(Feature.priority, Feature.created_at)
    )
    features = feat_result.scalars().all()
    sess_result = await db.execute(
        select(DBSession)
        .where(DBSession.product_id == product_id)
        .where(
            # Exclude ghost sessions: failed AND ended within 10s (container startup errors)
            ~(
                (DBSession.exit_code != 0) &
                (DBSession.ended_at != None) &
                (func.extract("epoch", DBSession.ended_at - DBSession.started_at) < 10)
            )
        )
        .order_by(DBSession.started_at.desc())
        .limit(50)
    )
    sessions = sess_result.scalars().all()
    alert_count = await _unread_alert_count(db)
    _sys_cfg = await _get_system_config(db)
    _gh_pat = _sys_cfg.github_pat if _sys_cfg else None
    open_prs_list = list_open_prs(product.github_repo or "", token=_gh_pat) if product.github_repo else []
    open_prs = len(open_prs_list)

    # Phase + Sprint + label data for the Sprints tab
    phase_result = await db.execute(
        select(Phase).where(Phase.product_id == product_id).order_by(Phase.order, Phase.id)
    )
    phases = phase_result.scalars().all()

    sprint_result = await db.execute(
        select(Sprint).where(Sprint.product_id == product_id).order_by(Sprint.phase_id.nulls_last(), Sprint.id)
    )
    sprints = sprint_result.scalars().all()
    active_sprint = next((s for s in sprints if s.status == "active"), None)

    label_result = await db.execute(
        select(Label).where(Label.product_id == product_id).order_by(Label.name)
    )
    labels = label_result.scalars().all()

    tab = request.query_params.get("tab", "board")
    return templates.TemplateResponse("product.html", {
        "request": request,
        "product": product,
        "features": features,
        "sessions": sessions,
        "open_prs": open_prs,
        "open_prs_list": open_prs_list,
        "pm_transitions": PM_ALLOWED_TRANSITIONS,
        "alert_count": alert_count,
        "current_pm": current_pm,
        "active_tab": tab,
        "max_features_default": _cfg(await _get_system_config(db), "max_features_per_run"),
        "phases": phases,
        "sprints": sprints,
        "active_sprint": active_sprint,
        "labels": labels,
    })


@app.get("/product/{product_id}/architecture", response_class=HTMLResponse)
async def architecture_view(
    product_id: int, request: Request,
    db: AsyncSession = Depends(get_db),
    current_pm: str = Depends(require_auth),
):
    """Renders ARCHITECTURE.md from GitHub."""
    product = await _get_product_or_404(product_id, db)
    alert_count = await _unread_alert_count(db)
    _sys_cfg = await _get_system_config(db)
    _gh_pat = _sys_cfg.github_pat if _sys_cfg else None
    raw_md = fetch_architecture_md(product.github_repo or "", token=_gh_pat) if product.github_repo else None
    html_content = _md(raw_md) if raw_md else None
    return templates.TemplateResponse("progress.html", {
        "request": request,
        "product": product,
        "content": html_content,
        "alert_count": alert_count,
        "current_pm": current_pm,
        "doc_title": "ARCHITECTURE.md",
    })


@app.get("/product/{product_id}/progress", response_class=HTMLResponse)
async def progress_view(
    product_id: int, request: Request,
    db: AsyncSession = Depends(get_db),
    current_pm: str = Depends(require_auth),
):
    """Fetches progress.md from GitHub and renders as HTML."""
    product = await _get_product_or_404(product_id, db)
    alert_count = await _unread_alert_count(db)
    _sys_cfg = await _get_system_config(db)
    _gh_pat = _sys_cfg.github_pat if _sys_cfg else None
    raw_md = fetch_progress_md(product.github_repo or "", token=_gh_pat) if product.github_repo else None
    html_content = _md(raw_md) if raw_md else None

    return templates.TemplateResponse("progress.html", {
        "request": request,
        "product": product,
        "content": html_content,
        "alert_count": alert_count,
        "current_pm": current_pm,
    })


# ══════════════════════════════════════════════════════════════════════════════
# HTML FORM ENDPOINTS — PM actions
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/product/register")
async def register_product_form(
    working_dir: str = Form(...),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Brownfield: PM registers an existing repo."""
    existing = await db.execute(
        select(Product).where(Product.working_dir == working_dir)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Product already registered")
    product = Product(working_dir=working_dir)
    db.add(product)
    await db.flush()
    return RedirectResponse(f"/product/{product.id}", status_code=303)


@app.post("/product/register/greenfield")
async def register_greenfield_form(
    product_name: str = Form(...),
    github_repo_name: str = Form(...),
    vision: str = Form(...),
    preferred_stack: str = Form(...),
    suggested_features: str = Form("[]"),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """
    Greenfield: store the request in DB as 'greenfield_pending'.
    The poller picks this up on the next cycle and does the actual scaffolding
    (create GitHub repo, generate SSH key, git init, write scaffold files).
    """
    config = await _get_system_config(db)
    if not config or not config.products_root_dir or not config.github_org or not config.github_pat:
        raise HTTPException(
            status_code=422,
            detail="Admin configuration incomplete. Set products root dir, GitHub org, and PAT first.",
        )

    working_dir = str(Path(config.products_root_dir) / github_repo_name)

    existing = await db.execute(select(Product).where(Product.working_dir == working_dir))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"A product already exists at {working_dir}")

    try:
        features_list = json.loads(suggested_features)
    except (json.JSONDecodeError, ValueError):
        features_list = []

    product = Product(
        working_dir=working_dir,
        name=product_name.strip(),
        type="greenfield",
        status="greenfield_pending",
        config={
            "vision": vision.strip(),
            "preferred_stack": preferred_stack,
            "github_repo_name": github_repo_name.strip(),
            "suggested_features": features_list,
        },
    )
    db.add(product)
    await db.flush()
    return RedirectResponse(f"/product/{product.id}", status_code=303)


@app.post("/product/{product_id}/features")
async def add_feature_form(
    product_id: int,
    name: str = Form(...),
    description: str = Form(""),
    priority: int = Form(50),
    feature_type: str = Form("feature"),
    skip_design: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM adds a feature via the product detail form."""
    await _get_product_or_404(product_id, db)
    if feature_type not in ("feature", "bug", "chore"):
        feature_type = "feature"
    feature = Feature(
        product_id=product_id,
        name=name.strip(),
        description=description.strip() or None,
        priority=max(1, min(100, priority)),
        source="pm",
        feature_type=feature_type,
        skip_design=(skip_design == "true"),
    )
    db.add(feature)
    await db.flush()
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/features/{feature_id}/status")
async def change_feature_status_form(
    product_id: int, feature_id: int,
    status: str = Form(...),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM changes feature status via the kanban dropdown."""
    feature = await _get_feature_or_404(feature_id, db)
    allowed = PM_ALLOWED_TRANSITIONS.get(feature.status, [])
    if status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot transition {feature.status!r} → {status!r}",
        )
    feature.status = status
    if status == "Approved" and feature.fix_attempts > 0:
        feature.fix_attempts = 0
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/approve")
async def approve_product(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    product = await _get_product_or_404(product_id, db)
    if product.status == "discovered":
        product.status = "ready"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/pause")
async def pause_product(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    product = await _get_product_or_404(product_id, db)
    if product.status == "ready":
        product.status = "paused"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/resume")
async def resume_product(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    product = await _get_product_or_404(product_id, db)
    if product.status == "paused":
        product.status = "ready"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/trigger_analysis")
async def trigger_analysis(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM triggers one-time Analysis Run for a brownfield product."""
    product = await _get_product_or_404(product_id, db)
    if product.type != "brownfield":
        raise HTTPException(status_code=422, detail="Analysis Run is for brownfield products only")
    if product.analysis_status == "running":
        raise HTTPException(status_code=409, detail="Analysis Run already in progress")
    product.analysis_status = "running"
    product.status = "ready"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.post("/admin/settings")
async def admin_save_settings(
    products_root_dir: str = Form(""),
    github_org: str = Form(""),
    github_pat: str = Form(""),
    github_ssh_key_name: str = Form("productfactory-deploy"),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save system configuration (upsert single row id=1)."""
    config = await db.get(SystemConfig, 1)
    if not config:
        config = SystemConfig(id=1)
        db.add(config)

    config.products_root_dir = products_root_dir.strip() or None
    config.github_org = github_org.strip() or None
    # Only update PAT if a non-empty value was submitted
    if github_pat.strip():
        config.github_pat = github_pat.strip()
    config.github_ssh_key_name = github_ssh_key_name.strip() or "productfactory-deploy"
    await db.flush()
    return RedirectResponse("/admin?saved=true", status_code=303)


# Valid (inclusive) ranges for poller numeric settings — prevents misconfiguration
# that would tight-loop the poller or block it from ever running.
_POLLER_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "poll_interval":               (5,   3600),
    "session_timeout_minutes":     (5,    480),
    "stale_threshold_minutes":     (5,    120),
    "auth_check_timeout":          (5,    120),
    "max_open_prs":                (1,     20),
    "pr_gate_sleep":               (60,  3600),
    "stuck_feature_timeout_hours": (0.25,  48),
    "max_features_per_run":        (1,     10),
    "brownfield_file_threshold":   (1,    100),
    "ollama_timeout":              (30,  1800),
    "bash_timeout":                (10,   600),
    "max_turns":                   (5,    500),
}


@app.post("/admin/settings/poller")
async def admin_save_poller_settings(
    request: Request,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save poller & agent operational settings."""
    form = await request.form()

    def _int(key: str) -> int | None:
        v = form.get(key, "").strip()
        if not v:
            return None
        try:
            val = int(v)
        except ValueError:
            return None
        bounds = _POLLER_INT_BOUNDS.get(key)
        if bounds:
            lo, hi = bounds
            val = max(lo, min(hi, val))
        return val

    def _float(key: str) -> float | None:
        v = form.get(key, "").strip()
        if not v:
            return None
        try:
            val = float(v)
        except ValueError:
            return None
        bounds = _POLLER_INT_BOUNDS.get(key)
        if bounds:
            lo, hi = bounds
            val = max(float(lo), min(float(hi), val))
        return val

    def _str(key: str) -> str | None:
        v = form.get(key, "").strip()
        return v or None

    config = await db.get(SystemConfig, 1)
    if not config:
        config = SystemConfig(id=1)
        db.add(config)

    config.poll_interval               = _int("poll_interval")
    config.session_timeout_minutes     = _int("session_timeout_minutes")
    config.stale_threshold_minutes     = _int("stale_threshold_minutes")
    config.auth_check_timeout          = _int("auth_check_timeout")
    config.max_open_prs                = _int("max_open_prs")
    config.pr_gate_sleep               = _int("pr_gate_sleep")
    config.stuck_feature_timeout_hours = _float("stuck_feature_timeout_hours")
    config.max_features_per_run        = _int("max_features_per_run")
    config.brownfield_file_threshold       = _int("brownfield_file_threshold")
    config.auto_merge_enabled              = form.get("auto_merge_enabled") == "1"
    config.agent_backend               = _str("agent_backend")
    config.ollama_host                 = _str("ollama_host")
    config.designer_model              = _str("designer_model")
    config.coder_model                 = _str("coder_model")
    config.ollama_timeout              = _int("ollama_timeout")
    config.bash_timeout                = _int("bash_timeout")
    config.max_turns                   = _int("max_turns")
    config.claude_model                = _str("claude_model")
    config.claude_credentials_dir      = _str("claude_credentials_dir")
    config.ssh_keys_dir = _str("ssh_keys_dir")
    await db.flush()
    return RedirectResponse("/admin?saved=true", status_code=303)


@app.post("/admin/pms")
async def admin_create_pm(
    name: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Add a new PM account."""
    existing = await db.execute(select(PMUser).where(PMUser.username == username.strip()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Username '{username}' already exists")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    user = PMUser(
        name=name.strip(),
        username=username.strip().lower(),
        password_hash=_hash_password(password),
    )
    db.add(user)
    await db.flush()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/pms/{user_id}/delete")
async def admin_delete_pm(
    user_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Remove a PM account."""
    user = await db.get(PMUser, user_id)
    if user:
        await db.delete(user)
        await db.flush()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/settings/notifications")
async def admin_save_notifications(
    slack_webhook_url: str = Form(""),
    github_webhook_secret: str = Form(""),
    max_sessions_per_day: str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save notification / scheduling config."""
    config = await db.get(SystemConfig, 1)
    if not config:
        config = SystemConfig(id=1)
        db.add(config)
    config.slack_webhook_url = slack_webhook_url.strip() or None
    if github_webhook_secret.strip():
        config.github_webhook_secret = github_webhook_secret.strip()
    config.max_sessions_per_day = int(max_sessions_per_day) if max_sessions_per_day.strip().isdigit() else None
    await db.flush()
    return RedirectResponse("/admin?saved=true", status_code=303)


@app.post("/product/{product_id}/run-now")
async def run_now(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Flag this product to be picked up on the next poller cycle immediately."""
    product = await _get_product_or_404(product_id, db)
    product.run_now = True
    await db.flush()
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/run-trainer")
async def run_trainer(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Queue a Product Trainer (showcase video) session on the next poller cycle."""
    product = await _get_product_or_404(product_id, db)
    product.run_trainer_now = True
    await db.flush()
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/schedule")
async def save_schedule(
    product_id: int,
    quiet_hours_start: str = Form(""),
    quiet_hours_end: str = Form(""),
    daily_session_cap: str = Form(""),
    max_features_per_run: str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save per-product scheduling settings."""
    product = await _get_product_or_404(product_id, db)
    product.quiet_hours_start    = int(quiet_hours_start)    if quiet_hours_start.strip().isdigit()    else None
    product.quiet_hours_end      = int(quiet_hours_end)      if quiet_hours_end.strip().isdigit()      else None
    product.daily_session_cap    = int(daily_session_cap)    if daily_session_cap.strip().isdigit()    else None
    product.max_features_per_run = int(max_features_per_run) if max_features_per_run.strip().isdigit() else None
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=settings", status_code=303)


@app.post("/product/{product_id}/sprints/create")
async def create_sprint_form(
    product_id: int,
    name:       str = Form(...),
    goal:       str = Form(""),
    start_date: str = Form(""),
    end_date:   str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Create a sprint from the Sprints tab form."""
    from datetime import date as _date
    await _get_product_or_404(product_id, db)
    sprint = Sprint(
        product_id=product_id,
        name=name.strip(),
        goal=goal.strip() or None,
        start_date=_date.fromisoformat(start_date) if start_date else None,
        end_date=_date.fromisoformat(end_date) if end_date else None,
        status="active",
    )
    db.add(sprint)
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=sprints", status_code=303)


@app.get("/api/sprints/{sprint_id}/dod")
async def api_sprint_dod(sprint_id: int, db: AsyncSession = Depends(get_db)):
    """
    Evaluate the Definition of Done for a sprint.
    Returns the current gate states — used by poller and UI.
    """
    sprint = await db.get(Sprint, sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    dod = await _evaluate_dod(sprint_id, sprint.product_id, db)
    return dod


@app.post("/api/sprints/{sprint_id}/check-dod")
async def api_check_dod(sprint_id: int, db: AsyncSession = Depends(get_db)):
    """
    Poller calls this every cycle. Evaluates gates; if all pass, auto-completes sprint.
    Does NOT modify any gate state — only triggers completion.
    """
    sprint = await db.get(Sprint, sprint_id)
    if not sprint or sprint.status == "completed":
        return {"action": "skipped"}
    dod = await _evaluate_dod(sprint_id, sprint.product_id, db)
    all_pass = (
        dod["all_features_done"]
        and dod["no_open_prs"]
        and dod["qa_passed"]
        and dod["security_clean"]
    )
    if all_pass:
        await _do_complete_sprint(sprint, sprint.product_id, db)
        return {"action": "auto_completed", "dod": dod}
    return {"action": "gates_pending", "dod": dod}


async def _evaluate_dod(sprint_id: int, product_id: int, db: AsyncSession) -> dict:
    """
    Compute DoD gate states for a sprint:
      all_features_done — all sprint features are Pushed or Deferred
      no_open_prs       — no sprint features have an open PR
      qa_passed         — QA agent has signed off (stored in dod_status)
      security_clean    — Security agent has signed off (stored in dod_status)
      retro_done        — Retrospective agent has completed (stored in dod_status)
    """
    sprint = await db.get(Sprint, sprint_id)
    if not sprint:
        return {}

    # Persisted sign-offs (set by agents)
    persisted = sprint.dod_status or {}

    # Compute live gates
    feat_result = await db.execute(
        select(Feature).where(Feature.sprint_id == sprint_id)
    )
    sprint_features = feat_result.scalars().all()

    terminal = {"Pushed", "Deferred", "Rejected"}
    all_features_done = all(f.status in terminal for f in sprint_features) if sprint_features else False
    no_open_prs = all(f.pr_number is None or f.status == "Pushed" for f in sprint_features)

    return {
        "all_features_done": all_features_done,
        "no_open_prs":       no_open_prs,
        "qa_passed":         bool(persisted.get("qa_passed")),
        "security_clean":    bool(persisted.get("security_clean")),
        "retro_done":        bool(persisted.get("retro_done")),
        "feature_count":     len(sprint_features),
        "features_terminal": sum(1 for f in sprint_features if f.status in terminal),
    }


async def _do_complete_sprint(sprint: Sprint, product_id: int, db: AsyncSession) -> None:
    """Mark sprint completed, generate release notes, activate next sprint."""
    from datetime import datetime as _dt, timezone as _tz
    sprint.status = "completed"
    sprint.completed_at = _dt.now(_tz.utc)
    await db.flush()

    # Generate release notes
    try:
        notes = await _generate_sprint_release_notes(sprint.id, product_id, db)
        if notes:
            sprint.release_notes = notes
            await db.flush()
    except Exception:
        pass

    # Activate the next sprint in the same phase, or next phase's first sprint
    await _activate_next_sprint(sprint, product_id, db)


async def _activate_next_sprint(completed: Sprint, product_id: int, db: AsyncSession) -> None:
    """Find and activate the next planned sprint after the completed one."""
    # Same phase first
    if completed.phase_id:
        next_result = await db.execute(
            select(Sprint).where(
                Sprint.product_id == product_id,
                Sprint.phase_id == completed.phase_id,
                Sprint.status == "planned",
                Sprint.id > completed.id,
            ).order_by(Sprint.id).limit(1)
        )
        nxt = next_result.scalar_one_or_none()
        if nxt:
            nxt.status = "active"
            await db.flush()
            return

        # No more sprints in this phase — check if phase should be completed
        phase = await db.get(Phase, completed.phase_id)
        if phase:
            rem_result = await db.execute(
                select(func.count()).where(
                    Sprint.phase_id == completed.phase_id,
                    Sprint.status.in_(["planned", "active"]),
                )
            )
            if (rem_result.scalar() or 0) == 0:
                phase.status = "completed"
                await db.flush()

    # Move to next phase's first planned sprint
    if completed.phase_id:
        phase = await db.get(Phase, completed.phase_id)
        if phase:
            next_phase_result = await db.execute(
                select(Phase).where(
                    Phase.product_id == product_id,
                    Phase.status == "planned",
                    Phase.order > phase.order,
                ).order_by(Phase.order).limit(1)
            )
            next_phase = next_phase_result.scalar_one_or_none()
            if next_phase:
                next_phase.status = "active"
                await db.flush()
                first_sprint_result = await db.execute(
                    select(Sprint).where(
                        Sprint.phase_id == next_phase.id,
                        Sprint.status == "planned",
                    ).order_by(Sprint.id).limit(1)
                )
                first = first_sprint_result.scalar_one_or_none()
                if first:
                    first.status = "active"
                    await db.flush()


@app.post("/product/{product_id}/sprints/{sprint_id}/complete")
async def complete_sprint_form(
    product_id: int, sprint_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM override — complete sprint regardless of DoD gate state."""
    sprint = await db.get(Sprint, sprint_id)
    if sprint and sprint.product_id == product_id and sprint.status != "completed":
        await _do_complete_sprint(sprint, product_id, db)
    return RedirectResponse(f"/product/{product_id}?tab=sprints", status_code=303)


async def _generate_sprint_release_notes(sprint_id: int, product_id: int, db: AsyncSession) -> str | None:
    """Call Claude to write release notes for all Pushed features in a sprint."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    sprint = await db.get(Sprint, sprint_id)
    if not sprint:
        return None
    product = await db.get(Product, product_id)

    feat_result = await db.execute(
        select(Feature).where(
            Feature.sprint_id == sprint_id,
            Feature.status == "Pushed",
        ).order_by(Feature.priority.desc())
    )
    pushed = feat_result.scalars().all()
    if not pushed:
        return None

    feature_list = "\n".join(f"- {f.name}: {f.description or ''}" for f in pushed)
    try:
        return await _llm_call(
            f"Write concise release notes for {product.name if product else 'this product'} "
            f"— {sprint.name}.\n\n"
            f"Shipped features:\n{feature_list}\n\n"
            "Format as Markdown with:\n"
            "- A one-sentence summary of what this release delivers\n"
            "- A '## What's New' section with bullet points grouped by theme\n"
            "- Keep it short and user-facing — no internal jargon\n"
            "Return only the Markdown, nothing else.",
            max_tokens=800,
        )
    except Exception:
        return None


@app.post("/product/{product_id}/plan-sprints")
async def plan_sprints_form(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Trigger LLM sprint auto-planning (form post → redirect to sprints tab)."""
    # Re-use the API endpoint logic by calling it directly
    try:
        result = await api_plan_sprints(product_id=product_id, db=db, _=_)
        n = result.get("sprints_created", 0)
        return RedirectResponse(
            f"/product/{product_id}?tab=sprints&msg=Created+{n}+sprint(s)", status_code=303
        )
    except HTTPException as exc:
        return RedirectResponse(
            f"/product/{product_id}?tab=sprints&err={exc.detail}", status_code=303
        )


@app.post("/product/{product_id}/features/{feature_id}/assign-sprint")
async def assign_sprint_form(
    product_id: int, feature_id: int,
    sprint_id: str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Assign or remove a feature from a sprint."""
    feature = await _get_feature_or_404(feature_id, db)
    feature.sprint_id = int(sprint_id) if sprint_id.isdigit() else None
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=sprints", status_code=303)


@app.post("/product/{product_id}/prompt")
async def save_custom_prompt(
    product_id: int,
    custom_prompt: str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save a custom prompt override for this product's agent."""
    product = await _get_product_or_404(product_id, db)
    product.custom_prompt = custom_prompt.strip() or None
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=settings", status_code=303)


@app.post("/product/{product_id}/merge-pr/{pr_number}")
async def merge_pr_action(
    product_id: int, pr_number: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Merge a GitHub PR via the API."""
    product = await _get_product_or_404(product_id, db)
    config = await _get_system_config(db)
    token = config.github_pat if config else None
    if not token:
        raise HTTPException(422, "GitHub PAT not configured — set it in Admin → Settings")
    if not token:
        raise HTTPException(422, "GitHub PAT not configured — set it in Admin")
    ok, err = merge_pr(product.github_repo or "", pr_number, token)
    if not ok:
        # Not mergeable = conflicts; close PR on GitHub, re-queue feature to Implementing
        if "not mergeable" in err.lower() or "405" in err:
            close_pr(
                product.github_repo or "", pr_number, token,
                reason="Closing due to merge conflicts — ProductFactory will rebase and reopen.",
            )
            result = await db.execute(
                select(Feature).where(Feature.product_id == product_id, Feature.pr_number == pr_number)
            )
            feature = result.scalar_one_or_none()
            if feature:
                feature.status = "Implementing"
                feature.pr_number = None
                feature.review_outcome = None
                feature.review_notes = f"Merge failed: PR has conflicts — coder must rebase. ({err})"
                await db.commit()
            raise HTTPException(409, f"PR has merge conflicts — closed PR #{pr_number} and re-queued for coder to rebase.")
        raise HTTPException(502, f"GitHub merge failed: {err}")
    # Mark matching feature as Pushed
    result = await db.execute(
        select(Feature).where(Feature.product_id == product_id, Feature.pr_number == pr_number)
    )
    feature = result.scalar_one_or_none()
    if feature:
        feature.status = "Pushed"
        await db.commit()
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/features/bulk-approve")
async def bulk_approve(
    product_id: int,
    feature_ids: str = Form(...),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Approve multiple Pending features at once."""
    ids = [int(x) for x in feature_ids.split(",") if x.strip().isdigit()]
    result = await db.execute(
        select(Feature).where(Feature.product_id == product_id, Feature.id.in_(ids), Feature.status == "Pending")
    )
    for f in result.scalars().all():
        f.status = "Approved"
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=board&phase=Approved", status_code=303)


@app.post("/product/{product_id}/features/bulk-reject")
async def bulk_reject(
    product_id: int,
    feature_ids: str = Form(...),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Reject multiple Pending features at once."""
    ids = [int(x) for x in feature_ids.split(",") if x.strip().isdigit()]
    result = await db.execute(
        select(Feature).where(Feature.product_id == product_id, Feature.id.in_(ids), Feature.status == "Pending")
    )
    for f in result.scalars().all():
        f.status = "Rejected"
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=board&phase=Pending", status_code=303)


@app.post("/product/{product_id}/features/bulk-approve-all")
async def bulk_approve_all(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Approve ALL Pending features for a product in one click."""
    result = await db.execute(
        select(Feature).where(Feature.product_id == product_id, Feature.status == "Pending")
    )
    for f in result.scalars().all():
        f.status = "Approved"
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=board&phase=Approved", status_code=303)


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Products
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products", response_model=list[schemas.ProductOut])
async def api_list_products(db: AsyncSession = Depends(get_db)):
    """Poller fetches all products for discovery loop."""
    result = await db.execute(select(Product))
    return result.scalars().all()


@app.post("/api/products", response_model=schemas.ProductOut, status_code=201)
async def api_create_product(
    body: schemas.ProductCreate,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """JSON endpoint for PM or tooling to register a product."""
    product = Product(working_dir=body.working_dir)
    if body.name:         product.name        = body.name
    if body.type:         product.type        = body.type
    if body.tech_stack:   product.tech_stack  = body.tech_stack
    if body.status:       product.status      = body.status
    if body.github_repo:  product.github_repo = body.github_repo
    if body.config:       product.config      = body.config
    db.add(product)
    await db.flush()
    return product


@app.get("/api/products/next", response_model=schemas.ProductOut | None)
async def api_next_product(db: AsyncSession = Depends(get_db)):
    """
    Returns the next product for the poller to process.
    Selects: status=ready, ordered by last_run_at ASC NULLS FIRST.
    Includes products with Approved/Designed features (designer/coder work)
    AND products with no actionable features at all (planner will generate new ones).
    """
    # Products with actionable features (designer/coder work)
    has_actionable = Product.id.in_(
        select(Feature.product_id)
        .where(Feature.status.in_(["Approved", "Designed"]))
        .distinct()
    )
    # Products with NO in-flight features at all (needs planner)
    has_no_actionable = ~Product.id.in_(
        select(Feature.product_id)
        .where(Feature.status.in_(["Pending", "Approved", "Designed", "Implementing", "Reviewing"]))
        .distinct()
    )
    result = await db.execute(
        select(Product)
        .where(Product.status == "ready")
        .where(has_actionable | has_no_actionable)
        .order_by(Product.last_run_at.asc().nullsfirst())
        .limit(1)
    )
    return result.scalar_one_or_none()


@app.get("/api/products/{product_id}", response_model=schemas.ProductOut)
async def api_get_product(product_id: int, db: AsyncSession = Depends(get_db)):
    """Fetch a single product by ID."""
    return await _get_product_or_404(product_id, db)


@app.patch("/api/products/{product_id}", response_model=schemas.ProductOut)
async def api_update_product(
    product_id: int, body: schemas.ProductUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Poller writes discovered fields (name, github_repo, tech_stack, config, last_run_at)."""
    product = await _get_product_or_404(product_id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    return product


@app.post("/api/products/{product_id}/sync-features")
async def api_sync_features(product_id: int, db: AsyncSession = Depends(get_db)):
    """Deprecated: DB is the single source of truth for feature status."""
    return {"changes": 0, "details": [], "message": "Disabled — DB is source of truth for feature status"}


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Features
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products/{product_id}/features", response_model=list[schemas.FeatureOut])
async def api_product_features(product_id: int, db: AsyncSession = Depends(get_db)):
    """Fetch all features for a product (used by poller for PR reconciliation)."""
    result = await db.execute(
        select(Feature)
        .where(Feature.product_id == product_id)
        .order_by(Feature.priority, Feature.created_at)
    )
    return result.scalars().all()


@app.get("/api/features/count")
async def api_feature_count(product_id: int, status: str | None = None, db: AsyncSession = Depends(get_db)):
    """Return {"count": N} for features matching product_id and optional status filter."""
    q = select(func.count()).select_from(Feature).where(Feature.product_id == product_id)
    if status:
        q = q.where(Feature.status == status)
    result = await db.execute(q)
    return {"count": result.scalar()}


@app.get("/api/features/approved", response_model=list[schemas.FeatureOut])
async def api_approved_features(product_id: int, db: AsyncSession = Depends(get_db)):
    """Poller fetches features ready for coder: Approved (with skip_design) + Designed."""
    result = await db.execute(
        select(Feature)
        .where(
            Feature.product_id == product_id,
            Feature.status.in_(["Approved", "Designed"]),
        )
        .where(
            (Feature.status == "Designed") |
            ((Feature.status == "Approved") & (Feature.skip_design == True))
        )
        .order_by(Feature.priority, Feature.created_at)
    )
    return result.scalars().all()


@app.post("/api/features", response_model=schemas.FeatureOut, status_code=201)
async def api_create_feature(body: schemas.FeatureCreate, db: AsyncSession = Depends(get_db)):
    """PM or Claude (AI-recommended) creates a new feature."""
    await _get_product_or_404(body.product_id, db)
    feature = Feature(**body.model_dump())
    db.add(feature)
    await db.flush()
    return feature


@app.patch("/api/features/{feature_id}", response_model=schemas.FeatureOut)
async def api_update_feature(
    feature_id: int, body: schemas.FeatureUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Claude updates feature status during implementation."""
    feature = await _get_feature_or_404(feature_id, db)
    updates = body.model_dump(exclude_unset=True)

    # Optimistic locking: if the caller supplies expected_version, reject the update
    # if the DB version has already been incremented by a concurrent write.
    expected_version = updates.pop("expected_version", None)
    if expected_version is not None and feature.version != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "expected": expected_version,
                "actual": feature.version,
                "message": f"Feature #{feature_id} was modified concurrently — re-fetch and retry.",
            },
        )

    # Increment version on every write so callers can detect concurrent updates.
    feature.version = (feature.version or 0) + 1

    # Auto-record changelog for tracked fields before applying the update
    _CHANGELOG_FIELDS = frozenset({"status", "priority", "pr_number", "blocked_reason", "sprint_id", "story_points", "due_date"})
    changed_by = updates.pop("changed_by", "agent")
    for field in _CHANGELOG_FIELDS:
        if field in updates:
            old_val = getattr(feature, field, None)
            new_val = updates[field]
            if str(old_val) != str(new_val):
                db.add(FeatureChangelog(
                    feature_id=feature_id,
                    field=field,
                    old_value=str(old_val) if old_val is not None else None,
                    new_value=str(new_val) if new_val is not None else None,
                    changed_by=changed_by,
                ))

    # session_uid is review metadata — not a column on features
    feature_fields = {k: v for k, v in updates.items() if k not in ("session_uid",)}
    for field, value in feature_fields.items():
        setattr(feature, field, value)
    # Auto-record review history whenever the reviewer sets an outcome
    if "review_outcome" in updates and updates["review_outcome"]:
        db.add(FeatureReview(
            feature_id=feature_id,
            review_outcome=updates["review_outcome"],
            review_notes=updates.get("review_notes"),
            session_uid=updates.get("session_uid"),
        ))
    return feature


@app.get("/api/features/{feature_id}/reviews", response_model=list[schemas.FeatureReviewOut])
async def api_feature_reviews(feature_id: int, db: AsyncSession = Depends(get_db)):
    """Full review history for a feature, newest first."""
    await _get_feature_or_404(feature_id, db)
    result = await db.execute(
        select(FeatureReview)
        .where(FeatureReview.feature_id == feature_id)
        .order_by(FeatureReview.created_at.desc())
    )
    return result.scalars().all()


@app.patch("/api/features/{feature_id}/pm-status", response_model=schemas.FeatureOut)
async def api_pm_status_update(
    feature_id: int, body: schemas.FeatureStatusUpdate,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM changes feature status — only PM-permitted transitions allowed."""
    feature = await _get_feature_or_404(feature_id, db)
    allowed = PM_ALLOWED_TRANSITIONS.get(feature.status, [])
    if body.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"Transition {feature.status!r} → {body.status!r} not allowed for PM",
        )
    old_status = feature.status
    feature.status = body.status
    if body.status == "Approved" and feature.fix_attempts > 0:
        feature.fix_attempts = 0
    if old_status != body.status:
        db.add(FeatureChangelog(
            feature_id=feature_id,
            field="status",
            old_value=old_status,
            new_value=body.status,
            changed_by="pm",
        ))
    return feature


@app.delete("/api/features/{feature_id}", status_code=204)
async def api_delete_feature(
    feature_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM deletes a Rejected feature. Only Rejected features may be deleted."""
    feature = await _get_feature_or_404(feature_id, db)
    if feature.status != "Rejected":
        raise HTTPException(
            status_code=422,
            detail=f"Only Rejected features can be deleted (current status: {feature.status!r})",
        )
    await db.delete(feature)


@app.post("/api/features/reset_stuck", response_model=schemas.ResetStuckResult)
async def api_reset_stuck(db: AsyncSession = Depends(get_db)):
    """
    Poller calls this each cycle.
    Resets features stuck in in-progress agent states for >2h back to their prior ready state.
    """
    sys_cfg = await _get_system_config(db)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_cfg(sys_cfg, "stuck_feature_timeout_hours"))
    result = await db.execute(
        select(Feature).where(
            Feature.status.in_(["Implementing", "Designing", "Reviewing"]),
            Feature.updated_at < cutoff,
        )
    )
    stuck = result.scalars().all()
    for f in stuck:
        if f.status == "Implementing":
            # Reset to Designed if a design doc was written, otherwise back to Approved
            f.status = "Designed" if f.design_doc_path else "Approved"
        elif f.status == "Designing":
            f.status = "Approved"
        elif f.status == "Reviewing":
            f.status = "Implementing"  # Coder will re-open or reviewer will re-pick
    await db.flush()
    return {"reset_count": len(stuck)}


@app.get("/api/features/next-for-persona", response_model=schemas.FeatureOut | None)
async def api_next_feature_for_persona(
    persona: str,
    product_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the next feature for a given persona to work on.
    - designer: Approved features where skip_design=False
    - coder:    Designed features OR Approved features where skip_design=True
    - reviewer: Reviewing features that have a PR number
    Optional product_id filter scopes to a single product.
    Returns null if nothing to do.
    """
    q = select(Feature).order_by(Feature.priority, Feature.created_at).limit(1)

    if persona == "designer":
        q = q.where(Feature.status == "Approved", Feature.skip_design == False)
    elif persona == "coder":
        q = q.where(
            Feature.status.in_(["Designed", "Approved"]),
            # For Approved features, only pick them if skip_design is True
            # (Designed features are always ready for coder)
        ).where(
            (Feature.status == "Designed") |
            ((Feature.status == "Approved") & (Feature.skip_design == True))
        )
    elif persona == "reviewer":
        q = q.where(Feature.status == "Reviewing", Feature.pr_number.isnot(None))
    else:
        # Maintenance personas (qa_tester, security_auditor, documenter, etc.)
        # discover their own work — return null so the agent handles the no-work case.
        return None

    if product_id is not None:
        q = q.where(Feature.product_id == product_id)

    result = await db.execute(q)
    return result.scalar_one_or_none()


@app.get("/api/features/search", response_model=list[schemas.FeatureOut])
async def api_search_features(
    q: str,
    product_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Full-text search features by name and description (PostgreSQL tsvector GIN index)."""
    query = select(Feature).where(
        text("to_tsvector('english', coalesce(features.name,'') || ' ' || coalesce(features.description,'')) @@ plainto_tsquery('english', :q)")
    ).params(q=q).order_by(Feature.priority, Feature.created_at)
    if product_id is not None:
        query = query.where(Feature.product_id == product_id)
    result = await db.execute(query)
    return result.scalars().all()


@app.get("/api/features/overdue", response_model=list[schemas.FeatureOut])
async def api_overdue_features(product_id: Optional[int] = None, db: AsyncSession = Depends(get_db)):
    """Return features with a due_date in the past that are not yet Pushed, Rejected, or Deferred."""
    from datetime import date as _date
    today = _date.today()
    q = select(Feature).where(
        Feature.due_date < today,
        Feature.status.notin_(["Pushed", "Rejected", "Deferred"]),
    )
    if product_id is not None:
        q = q.where(Feature.product_id == product_id)
    result = await db.execute(q)
    return result.scalars().all()


@app.get("/api/features/{feature_id}", response_model=schemas.FeatureOut)
async def api_get_feature(feature_id: int, db: AsyncSession = Depends(get_db)):
    """Fetch a single feature by ID (used by auto-merge to resolve pr_number)."""
    return await _get_feature_or_404(feature_id, db)


# ══════════════════════════════════════════════════════════════════════════════
# REST API — System Config (no auth — internal use by poller)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/system-config")
async def api_get_system_config(db: AsyncSession = Depends(get_db)):
    """Poller reads system config for greenfield scaffolding and GH_TOKEN injection."""
    config = await _get_system_config(db)
    return _config_as_dict(config)


# ══════════════════════════════════════════════════════════════════════════════
# REST API — LLM Recommendations
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/recommend/features")
async def recommend_features(
    body: schemas.RecommendRequest,
    _: str = Depends(require_auth),
):
    """Call Claude to suggest initial features based on product vision."""
    try:
        raw = await _llm_call(
            f"You are a senior product manager generating a comprehensive product backlog.\n\n"
            f"Product vision: {body.vision}\n"
            f"Tech stack: {body.preferred_stack}\n\n"
            "Generate a COMPLETE product backlog of 25-35 features covering ALL layers of the product:\n"
            "- Core functionality (the main user-facing features)\n"
            "- Authentication & user management\n"
            "- Security hardening\n"
            "- Performance & caching\n"
            "- API & integrations\n"
            "- Admin & ops tooling\n"
            "- Observability (logging, metrics, health checks)\n"
            "- Testing infrastructure\n"
            "- Developer experience (CI/CD, linting, docs)\n\n"
            "For each feature include: name (4-8 words), description (1-2 sentences), "
            "feature_type (feature/bug/chore), priority (1-100 where 100=critical), "
            "skip_design (true for chores/infra, false for user-facing features).\n\n"
            "Return ONLY a JSON array, no other text:\n"
            '[{"name": "...", "description": "...", "feature_type": "feature", '
            '"priority": 70, "skip_design": false}]',
            max_tokens=4000,
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        features = json.loads(raw.strip())
        return {"features": features}
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="LLM returned malformed JSON — try again")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")


@app.post("/api/articulate/vision")
async def articulate_vision(
    body: schemas.ArticulateRequest,
    _: str = Depends(require_auth),
):
    """Refine and articulate a rough product vision into a clear, structured statement."""
    try:
        result = await _llm_call(
            f"You are a senior product manager helping articulate a product vision.\n\n"
            f"Raw vision input: {body.vision}\n"
            f"Tech stack: {body.preferred_stack}\n\n"
            "Rewrite the vision as a concise, well-structured product vision statement. "
            "Cover: the problem being solved, the target user, the core value proposition, "
            "and what success looks like. "
            "Write in plain English — no bullet points, no headings, 3-5 sentences. "
            "Return only the articulated vision text, nothing else.",
            max_tokens=600,
        )
        return {"vision": result}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Comments, Changelog, Labels, Sprints, Links, Search
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/features/{feature_id}/comments", response_model=schemas.FeatureCommentOut, status_code=201)
async def api_add_comment(feature_id: int, body: schemas.FeatureCommentCreate, db: AsyncSession = Depends(get_db)):
    """Add a comment to a feature. Author can be 'pm', 'poller', or a persona name."""
    await _get_feature_or_404(feature_id, db)
    comment = FeatureComment(feature_id=feature_id, author=body.author, body=body.body)
    db.add(comment)
    await db.flush()
    return comment


@app.get("/api/features/{feature_id}/comments", response_model=list[schemas.FeatureCommentOut])
async def api_list_comments(feature_id: int, db: AsyncSession = Depends(get_db)):
    """List all comments for a feature, newest first."""
    await _get_feature_or_404(feature_id, db)
    result = await db.execute(
        select(FeatureComment)
        .where(FeatureComment.feature_id == feature_id)
        .order_by(FeatureComment.created_at.desc())
    )
    return result.scalars().all()


@app.get("/api/features/{feature_id}/changelog", response_model=list[schemas.ChangelogEntryOut])
async def api_feature_changelog(feature_id: int, db: AsyncSession = Depends(get_db)):
    """Full field-level audit trail for a feature, newest first."""
    await _get_feature_or_404(feature_id, db)
    result = await db.execute(
        select(FeatureChangelog)
        .where(FeatureChangelog.feature_id == feature_id)
        .order_by(FeatureChangelog.changed_at.desc())
    )
    return result.scalars().all()


@app.post("/api/labels", response_model=schemas.LabelOut, status_code=201)
async def api_create_label(body: schemas.LabelCreate, db: AsyncSession = Depends(get_db)):
    """Create a label for a product. Name must be unique within the product."""
    await _get_product_or_404(body.product_id, db)
    label = Label(product_id=body.product_id, name=body.name.lower().strip(), color=body.color)
    db.add(label)
    try:
        await db.flush()
    except Exception:
        raise HTTPException(status_code=409, detail=f"Label '{body.name}' already exists for this product")
    return label


@app.get("/api/products/{product_id}/labels", response_model=list[schemas.LabelOut])
async def api_list_labels(product_id: int, db: AsyncSession = Depends(get_db)):
    """List all labels for a product."""
    await _get_product_or_404(product_id, db)
    result = await db.execute(
        select(Label).where(Label.product_id == product_id).order_by(Label.name)
    )
    return result.scalars().all()


@app.post("/api/features/{feature_id}/labels", status_code=201)
async def api_add_label_to_feature(
    feature_id: int, body: schemas.FeatureLabelAdd, db: AsyncSession = Depends(get_db)
):
    """Apply a label to a feature."""
    await _get_feature_or_404(feature_id, db)
    label = await db.get(Label, body.label_id)
    if not label:
        raise HTTPException(status_code=404, detail="Label not found")
    assoc = FeatureLabel(feature_id=feature_id, label_id=body.label_id)
    db.add(assoc)
    try:
        await db.flush()
    except Exception:
        pass  # Already applied — idempotent
    return {"feature_id": feature_id, "label_id": body.label_id}


@app.delete("/api/features/{feature_id}/labels/{label_id}", status_code=204)
async def api_remove_label_from_feature(feature_id: int, label_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a label from a feature."""
    result = await db.execute(
        select(FeatureLabel).where(
            FeatureLabel.feature_id == feature_id,
            FeatureLabel.label_id == label_id,
        )
    )
    assoc = result.scalar_one_or_none()
    if assoc:
        await db.delete(assoc)


@app.get("/api/features/{feature_id}/labels", response_model=list[schemas.LabelOut])
async def api_feature_labels(feature_id: int, db: AsyncSession = Depends(get_db)):
    """List all labels applied to a feature."""
    await _get_feature_or_404(feature_id, db)
    result = await db.execute(
        select(Label)
        .join(FeatureLabel, FeatureLabel.label_id == Label.id)
        .where(FeatureLabel.feature_id == feature_id)
        .order_by(Label.name)
    )
    return result.scalars().all()


@app.post("/api/phases", response_model=schemas.PhaseOut, status_code=201)
async def api_create_phase(body: schemas.PhaseCreate, db: AsyncSession = Depends(get_db)):
    """Create a phase for a product."""
    await _get_product_or_404(body.product_id, db)
    phase = Phase(**body.model_dump())
    db.add(phase)
    await db.flush()
    return phase


@app.get("/api/products/{product_id}/phases", response_model=list[schemas.PhaseOut])
async def api_list_phases(product_id: int, db: AsyncSession = Depends(get_db)):
    """List all phases for a product ordered by phase order."""
    await _get_product_or_404(product_id, db)
    result = await db.execute(
        select(Phase)
        .where(Phase.product_id == product_id)
        .order_by(Phase.order, Phase.id)
    )
    return result.scalars().all()


@app.patch("/api/phases/{phase_id}", response_model=schemas.PhaseOut)
async def api_update_phase(phase_id: int, body: schemas.PhaseUpdate, db: AsyncSession = Depends(get_db)):
    """Update phase fields."""
    phase = await db.get(Phase, phase_id)
    if not phase:
        raise HTTPException(status_code=404, detail="Phase not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(phase, field, value)
    await db.flush()
    return phase


@app.post("/api/sprints", response_model=schemas.SprintOut, status_code=201)
async def api_create_sprint(body: schemas.SprintCreate, db: AsyncSession = Depends(get_db)):
    """Create a sprint for a product."""
    await _get_product_or_404(body.product_id, db)
    sprint = Sprint(**body.model_dump())
    db.add(sprint)
    await db.flush()
    return sprint


@app.get("/api/products/{product_id}/sprints", response_model=list[schemas.SprintOut])
async def api_list_sprints(product_id: int, db: AsyncSession = Depends(get_db)):
    """List all sprints for a product (active first, then by id)."""
    await _get_product_or_404(product_id, db)
    result = await db.execute(
        select(Sprint)
        .where(Sprint.product_id == product_id)
        .order_by(Sprint.status == "active", Sprint.id.desc())
    )
    return result.scalars().all()


@app.get("/api/products/{product_id}/sprints/active", response_model=schemas.SprintOut | None)
async def api_active_sprint(product_id: int, db: AsyncSession = Depends(get_db)):
    """Return the active sprint for a product, or null if none."""
    result = await db.execute(
        select(Sprint).where(Sprint.product_id == product_id, Sprint.status == "active").limit(1)
    )
    return result.scalar_one_or_none()


@app.patch("/api/sprints/{sprint_id}", response_model=schemas.SprintOut)
async def api_update_sprint(sprint_id: int, body: schemas.SprintUpdate, db: AsyncSession = Depends(get_db)):
    """Update sprint fields (name, goal, dates, status)."""
    sprint = await db.get(Sprint, sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(sprint, field, value)
    await db.flush()
    return sprint


@app.post("/api/sprints/{sprint_id}/sign-off")
async def api_sprint_sign_off(
    sprint_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """
    Agent sign-off endpoint — merges gate values into sprint.dod_status.
    Body: {"gate": "qa_passed"|"security_clean"|"retro_done", "value": true/false,
           "notes": "optional notes", "retro_doc_path": "optional path"}
    After each sign-off, check if all gates pass → auto-complete sprint.
    """
    sprint = await db.get(Sprint, sprint_id)
    if not sprint:
        raise HTTPException(status_code=404, detail="Sprint not found")
    if sprint.status == "completed":
        return {"status": "already_completed"}

    gate  = body.get("gate")
    value = body.get("value", True)
    valid_gates = {"qa_passed", "security_clean", "retro_done"}
    if gate not in valid_gates:
        raise HTTPException(status_code=400, detail=f"gate must be one of {valid_gates}")

    current = dict(sprint.dod_status or {})
    current[gate] = bool(value)
    if body.get("notes"):
        current[f"{gate}_notes"] = body["notes"]
    if gate == "retro_done" and body.get("retro_doc_path"):
        sprint.retro_doc_path = body["retro_doc_path"]
    sprint.dod_status = current
    await db.flush()

    # Re-evaluate all gates — if all pass, auto-complete sprint
    dod = await _evaluate_dod(sprint_id, sprint.product_id, db)
    all_pass = (
        dod["all_features_done"]
        and dod["no_open_prs"]
        and dod["qa_passed"]
        and dod["security_clean"]
    )
    auto_completed = False
    if all_pass and sprint.status != "completed":
        await _do_complete_sprint(sprint, sprint.product_id, db)
        auto_completed = True

    return {"dod": dod, "auto_completed": auto_completed}


@app.post("/api/products/{product_id}/plan-sprints")
async def api_plan_sprints(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """
    LLM auto-plans phasewise implementation from all Approved features.
    Generates phases (e.g. Foundation, Core, Enhancements) each with 1-3 sprints.
    Creates Phase + Sprint rows and assigns feature.sprint_id for each.
    """
    product = await db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    # Read max features per sprint from DB config
    _sys_cfg = await _get_system_config(db)
    max_per_sprint = _cfg(_sys_cfg, "max_features_per_run") or 5

    # Fetch all non-terminal features (everything except Pushed/Rejected/Reverted/Deferred)
    terminal_statuses = ("Pushed", "Rejected", "Reverted", "Deferred")
    feat_result = await db.execute(
        select(Feature).where(
            Feature.product_id == product_id,
            Feature.status.notin_(terminal_statuses),
        ).order_by(Feature.priority.desc())
    )
    approved = feat_result.scalars().all()
    if not approved:
        raise HTTPException(status_code=400, detail="No features to plan")

    features_list = [
        {"id": f.id, "name": f.name, "description": f.description or "",
         "feature_type": f.feature_type, "priority": f.priority}
        for f in approved
    ]

    try:
        raw = await _llm_call(
            f"You are a senior product manager doing phasewise implementation planning for: {product.name}\n"
            f"Vision: {getattr(product, 'vision', None) or (product.config or {}).get('vision') or 'Not specified'}\n\n"
            f"Approved features to plan ({len(features_list)} total):\n"
            f"{json.dumps(features_list, indent=2)}\n\n"
            "Organise these features into PHASES, where each phase has 1-3 SPRINTS.\n\n"
            "Phase structure (use exactly these phase names or similar):\n"
            "- Phase 1: Foundation — infrastructure, auth, CI/CD, core data models, dev tooling\n"
            "- Phase 2: Core Product — the main user-facing value, primary workflows\n"
            "- Phase 3: Growth & Polish — integrations, analytics, UX improvements, API\n"
            "- Phase 4: Scale & Ops — performance, observability, security hardening (if enough features)\n\n"
            "Rules:\n"
            "- Every feature must be in exactly one sprint\n"
            "- Sprints within a phase are sequential (Sprint 1 → Sprint 2 → Sprint 3)\n"
            f"- Each sprint should have at most {max_per_sprint} features — all features in a sprint are planned and implemented together in one session\n"
            "- Respect dependencies: foundational work (auth, DB schema) goes in Phase 1\n"
            "- Only create phases that have features to put in them\n\n"
            "Return ONLY a JSON array of phases, no other text:\n"
            '[\n'
            '  {\n'
            '    "phase_name": "Phase 1: Foundation",\n'
            '    "phase_goal": "Set up infrastructure and core data models",\n'
            '    "sprints": [\n'
            '      {"sprint_name": "Sprint 1", "sprint_goal": "...", "feature_ids": [1, 2, 3]},\n'
            '      {"sprint_name": "Sprint 2", "sprint_goal": "...", "feature_ids": [4, 5]}\n'
            '    ]\n'
            '  }\n'
            ']',
            max_tokens=3000,
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        phase_plan = json.loads(raw.strip())
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="LLM returned malformed JSON — try again")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    # Delete existing phases, sprints, and clear feature sprint assignments
    await db.execute(
        Feature.__table__.update()
        .where(Feature.product_id == product_id, Feature.sprint_id.isnot(None))
        .values(sprint_id=None)
    )
    await db.execute(Sprint.__table__.delete().where(Sprint.product_id == product_id))
    await db.execute(Phase.__table__.delete().where(Phase.product_id == product_id))
    await db.flush()

    # Create phases → sprints → assign features
    phases_created = 0
    sprints_created = 0
    features_assigned = 0
    first_sprint_overall = True

    for phase_idx, ph in enumerate(phase_plan):
        phase = Phase(
            product_id=product_id,
            name=ph.get("phase_name", f"Phase {phase_idx + 1}"),
            goal=ph.get("phase_goal", ""),
            order=phase_idx,
            status="active" if phase_idx == 0 else "planned",
        )
        db.add(phase)
        await db.flush()
        phases_created += 1

        for sprint_idx, sp in enumerate(ph.get("sprints", [])):
            sprint = Sprint(
                product_id=product_id,
                phase_id=phase.id,
                name=sp.get("sprint_name", f"Sprint {sprints_created + 1}"),
                goal=sp.get("sprint_goal", ""),
                status="active" if first_sprint_overall else "planned",
            )
            db.add(sprint)
            await db.flush()
            sprints_created += 1
            first_sprint_overall = False

            for fid in sp.get("feature_ids", []):
                feat_row = await db.execute(
                    select(Feature).where(Feature.id == fid, Feature.product_id == product_id)
                )
                feat = feat_row.scalar_one_or_none()
                if feat:
                    feat.sprint_id = sprint.id
                    features_assigned += 1

    return {"phases_created": phases_created, "sprints_created": sprints_created, "features_assigned": features_assigned}


@app.post("/api/features/{feature_id}/links", response_model=schemas.FeatureLinkOut, status_code=201)
async def api_add_feature_link(
    feature_id: int, body: schemas.FeatureLinkCreate, db: AsyncSession = Depends(get_db)
):
    """Link two features with a typed relationship (blocks, relates_to, duplicates)."""
    await _get_feature_or_404(feature_id, db)
    await _get_feature_or_404(body.target_id, db)
    link = FeatureLink(source_id=feature_id, target_id=body.target_id, link_type=body.link_type)
    db.add(link)
    try:
        await db.flush()
    except Exception:
        raise HTTPException(status_code=409, detail="Link already exists")
    return link


@app.get("/api/features/{feature_id}/links", response_model=list[schemas.FeatureLinkOut])
async def api_feature_links(feature_id: int, db: AsyncSession = Depends(get_db)):
    """List all links for a feature (both outgoing and incoming)."""
    await _get_feature_or_404(feature_id, db)
    result = await db.execute(
        select(FeatureLink).where(
            (FeatureLink.source_id == feature_id) | (FeatureLink.target_id == feature_id)
        )
    )
    return result.scalars().all()


@app.delete("/api/features/{feature_id}/links/{link_id}", status_code=204)
async def api_remove_feature_link(feature_id: int, link_id: int, db: AsyncSession = Depends(get_db)):
    """Remove a feature link."""
    link = await db.get(FeatureLink, link_id)
    if link and (link.source_id == feature_id or link.target_id == feature_id):
        await db.delete(link)


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Sessions
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/sessions", response_model=schemas.SessionOut, status_code=201)
async def api_start_session(body: schemas.SessionCreate, db: AsyncSession = Depends(get_db)):
    session = DBSession(**body.model_dump())
    db.add(session)
    await db.flush()
    return session


@app.get("/api/sessions/active")
async def api_active_session(product_id: int | None = None, db: AsyncSession = Depends(get_db)):
    """
    Returns the currently running session(s) — ended_at IS NULL.
    Poller calls this before launching a container to avoid duplicates.
    If product_id is given, returns the active session for that product (or null).
    Without product_id, returns all active sessions.
    """
    q = select(DBSession).where(DBSession.ended_at.is_(None))
    if product_id is not None:
        q = q.where(DBSession.product_id == product_id)
    result = await db.execute(q)
    sessions = result.scalars().all()
    if product_id is not None:
        return sessions[0] if sessions else None
    return sessions


@app.patch("/api/sessions/{session_id}", response_model=schemas.SessionOut)
async def api_end_session(
    session_id: int, body: schemas.SessionEnd,
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(DBSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(session, field, value)
    return session


@app.get("/api/sessions/{session_id}")
async def api_get_session(session_id: int, db: AsyncSession = Depends(get_db)):
    session = await db.get(DBSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Features updated during this session's time window (best-effort activity tracking)
    end_bound = session.ended_at or func.now()
    features_result = await db.execute(
        select(Feature.id, Feature.name, Feature.status, Feature.pr_url, Feature.pr_number,
               Feature.updated_at, Feature.feature_type, Feature.review_outcome)
        .where(
            Feature.product_id == session.product_id,
            Feature.updated_at >= session.started_at,
            Feature.updated_at <= end_bound,
        )
        .order_by(Feature.updated_at)
    )
    activities = [
        {
            "id": row.id, "name": row.name, "status": row.status,
            "feature_type": row.feature_type, "pr_url": row.pr_url,
            "pr_number": row.pr_number, "review_outcome": row.review_outcome,
            "updated_at": row.updated_at.isoformat(),
        }
        for row in features_result
    ]

    # Code reviews done in this session (via session_uid on feature_reviews)
    reviews_result = await db.execute(
        select(FeatureReview, Feature.name)
        .join(Feature, FeatureReview.feature_id == Feature.id)
        .where(FeatureReview.session_uid == session.session_uid)
        .order_by(FeatureReview.created_at)
    )
    reviews = [
        {"feature_name": row.name, "outcome": row.FeatureReview.review_outcome,
         "notes": row.FeatureReview.review_notes, "created_at": row.FeatureReview.created_at.isoformat()}
        for row in reviews_result
    ]

    dur = None
    if session.ended_at:
        dur = int((session.ended_at - session.started_at).total_seconds())
    return {
        "id": session.id,
        "session_uid": session.session_uid,
        "persona": session.persona,
        "started_at": session.started_at.isoformat(),
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration_seconds": dur,
        "exit_code": session.exit_code,
        "features_attempted": session.features_attempted,
        "features_pushed": session.features_pushed,
        "tokens_input": session.tokens_input,
        "tokens_output": session.tokens_output,
        "cost_usd": float(session.cost_usd) if session.cost_usd else None,
        "notes": session.notes,
        "activities": activities,
        "reviews": reviews,
    }


@app.patch("/api/products/{product_id}/last-session/persona")
async def api_set_last_session_persona(product_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    """Poller calls this after container exits to record the persona on the most recent session."""
    body = await request.json()
    persona = body.get("persona")
    if not persona:
        raise HTTPException(status_code=422, detail="persona required")
    result = await db.execute(
        select(DBSession)
        .where(DBSession.product_id == product_id)
        .order_by(DBSession.started_at.desc())
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session:
        session.persona = persona
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Session log streaming
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/products/{product_id}/session/log")
async def api_append_session_log(product_id: int, request: Request):
    """Called by poller to push agent stdout lines. No auth — internal network only."""
    body = await request.json()
    lines: list[str] = body.get("lines", [])
    buf = _session_logs[product_id]
    if lines and len(buf) >= SESSION_LOG_WARN_AT:
        log.warning(
            f"Session log buffer near capacity for product {product_id} "
            f"({len(buf)}/{SESSION_LOG_MAXLEN}) — oldest lines will be dropped"
        )
    for line in lines:
        buf.append(line)
        for q in list(_session_subscribers[product_id]):
            await q.put(line)
    return {"ok": True}


@app.get("/api/products/{product_id}/session/stream")
async def api_stream_session_logs(
    product_id: int, _: str = Depends(require_auth),
):
    """SSE endpoint — streams agent log lines to the browser in real-time."""
    queue: asyncio.Queue = asyncio.Queue()
    _session_subscribers[product_id].append(queue)

    async def generate():
        try:
            # Replay buffered lines first
            for line in list(_session_logs[product_id]):
                yield f"data: {json.dumps(line)}\n\n"
            # Stream new lines as they arrive
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"data: {json.dumps(line)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _session_subscribers[product_id].remove(queue)

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/products/{product_id}/session/message")
async def api_session_message(
    product_id: int, request: Request,
    _: str = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """PM sends a message to the running agent. Stored in product config for poller to inject."""
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(400, "message required")
    product = await _get_product_or_404(product_id, db)
    config = dict(product.config or {})
    queue = config.get("pm_messages", [])
    queue.append({"text": message, "sent_at": datetime.now(timezone.utc).isoformat()})
    config["pm_messages"] = queue
    product.config = config
    await db.commit()
    # Echo the message into the log stream so PM sees it immediately
    entry = f"[PM] {message}"
    _session_logs[product_id].append(entry)
    for q in list(_session_subscribers[product_id]):
        await q.put(entry)
    return {"ok": True}


@app.delete("/api/products/{product_id}/session/log")
async def api_clear_session_log(product_id: int):
    """Called by poller when a new session starts — clears the old log buffer."""
    _session_logs[product_id].clear()
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Poller Distributed Lock
# ══════════════════════════════════════════════════════════════════════════════
#
# Three-endpoint protocol:
#   POST   /api/poller/lock       — atomic acquire (409 if another poller is live)
#   POST   /api/poller/heartbeat  — refresh TTL every 15s while running
#   DELETE /api/poller/lock       — release on clean exit
#
# The lock row lives on system_config id=1. A lock is considered free when
# poller_heartbeat_at IS NULL or is older than 30s (dead poller TTL).

_LOCK_TTL_SECONDS = 30


@app.post("/api/poller/lock")
async def api_acquire_poller_lock(
    body: schemas.PollerLockRequest,
    db:   AsyncSession = Depends(get_db),
):
    """
    Atomically acquire the poller lock.
    Returns 200 + lock info on success, 409 + current holder on conflict.
    Safe against concurrent callers: a single UPDATE WHERE is atomic in PostgreSQL.
    """
    # Ensure the singleton config row exists (idempotent)
    config = await db.get(SystemConfig, 1)
    if not config:
        config = SystemConfig(id=1)
        db.add(config)
        await db.flush()

    now    = datetime.now(timezone.utc)
    stale  = now - timedelta(seconds=_LOCK_TTL_SECONDS)

    result = await db.execute(
        text("""
            UPDATE system_config
               SET poller_pid          = :pid,
                   poller_host         = :host,
                   poller_locked_at    = :now,
                   poller_heartbeat_at = :now
             WHERE id = 1
               AND (poller_heartbeat_at IS NULL OR poller_heartbeat_at < :stale)
            RETURNING poller_pid, poller_host, poller_locked_at, poller_heartbeat_at
        """),
        {"pid": body.pid, "host": body.host, "now": now, "stale": stale},
    )
    row = result.fetchone()

    if row:
        return {
            "pid":          row.poller_pid,
            "host":         row.poller_host,
            "locked_at":    row.poller_locked_at.isoformat(),
            "heartbeat_at": row.poller_heartbeat_at.isoformat(),
        }

    # Lock is held — include current holder in 409 so caller can log it
    config = await db.get(SystemConfig, 1)
    raise HTTPException(
        status_code=409,
        detail={
            "error":        "lock_held",
            "holder_pid":   config.poller_pid  if config else None,
            "holder_host":  config.poller_host if config else None,
            "heartbeat_at": config.poller_heartbeat_at.isoformat()
                            if config and config.poller_heartbeat_at else None,
        },
    )


@app.post("/api/poller/heartbeat")
async def api_poller_heartbeat(
    body: schemas.PollerHeartbeatRequest,
    db:   AsyncSession = Depends(get_db),
):
    """Refresh heartbeat. Returns 404 if this pid+host no longer holds the lock."""
    now = datetime.now(timezone.utc)
    result = await db.execute(
        text("""
            UPDATE system_config
               SET poller_heartbeat_at = :now
             WHERE id = 1
               AND poller_pid  = :pid
               AND poller_host = :host
            RETURNING id
        """),
        {"now": now, "pid": body.pid, "host": body.host},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Lock not held by this pid/host")
    return {"ok": True, "heartbeat_at": now.isoformat()}


@app.delete("/api/poller/lock")
async def api_release_poller_lock(
    body: schemas.PollerHeartbeatRequest,
    db:   AsyncSession = Depends(get_db),
):
    """Release the lock. No-op if this pid+host no longer holds it."""
    result = await db.execute(
        text("""
            UPDATE system_config
               SET poller_pid          = NULL,
                   poller_host         = NULL,
                   poller_locked_at    = NULL,
                   poller_heartbeat_at = NULL
             WHERE id = 1
               AND poller_pid  = :pid
               AND poller_host = :host
            RETURNING id
        """),
        {"pid": body.pid, "host": body.host},
    )
    if not result.fetchone():
        raise HTTPException(status_code=404, detail="Lock not held by this pid/host")
    return {"ok": True}


@app.post("/api/poller/force-unlock")
async def api_force_unlock_poller(db: AsyncSession = Depends(get_db)):
    """Force-clear a stale poller lock regardless of pid/host. Admin use only."""
    await db.execute(
        text("""
            UPDATE system_config
               SET poller_pid          = NULL,
                   poller_host         = NULL,
                   poller_locked_at    = NULL,
                   poller_heartbeat_at = NULL
             WHERE id = 1
        """)
    )
    return {"ok": True, "message": "Poller lock forcefully cleared"}


# REST API — Misc
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products/{product_id}/open_pr_count")
async def api_open_pr_count(product_id: int, db: AsyncSession = Depends(get_db)):
    """Poller PR count gate. Fetches live from GitHub."""
    product = await _get_product_or_404(product_id, db)
    _cfg = await _get_system_config(db)
    _pat = _cfg.github_pat if _cfg else None
    count = count_open_prs(product.github_repo, token=_pat) if product.github_repo else 0
    return {"product_id": product_id, "count": count}


@app.get("/api/alerts/unread", response_model=list[schemas.AlertOut])
async def api_unread_alerts(
    limit: int = 20,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Returns unread alerts for the PM website nav badge."""
    result = await db.execute(
        select(Alert)
        .where(Alert.delivered == False)  # noqa: E712
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    alerts = result.scalars().all()
    for a in alerts:
        a.delivered = True
    return alerts


@app.get("/api/products/{product_id}/videos")
async def api_list_videos(
    product_id: int,
    db: AsyncSession = Depends(get_db),
):
    """List MP4 videos in the product's output/ folder, newest first."""
    product = await _get_product_or_404(product_id, db)
    output_dir = _output_dir_for_product(product.working_dir)
    if not output_dir.exists():
        return []
    videos = sorted(output_dir.glob("product_video_*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "filename": v.name,
            "size_mb": round(v.stat().st_size / 1_048_576, 1),
            "url": f"/product/{product_id}/output/{v.name}",
        }
        for v in videos
    ]


@app.get("/product/{product_id}/output/{filename}")
async def serve_product_video(
    product_id: int,
    filename: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    """Serve a video file from the product's output/ folder (supports range requests)."""
    if not filename.endswith(".mp4") or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    product = await _get_product_or_404(product_id, db)
    file_path = _output_dir_for_product(product.working_dir) / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(str(file_path), media_type="video/mp4")


@app.delete("/api/products/{product_id}/sessions", status_code=200)
async def api_clear_sessions(
    product_id: int,
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Delete all session history for a product."""
    await _get_product_or_404(product_id, db)
    result = await db.execute(
        select(DBSession).where(DBSession.product_id == product_id)
    )
    sessions = result.scalars().all()
    for s in sessions:
        await db.delete(s)
    return {"deleted": len(sessions)}


@app.get("/api/products/{product_id}/sessions", response_model=list[schemas.SessionOut])
async def api_product_sessions(
    product_id: int, limit: int = 20,
    db: AsyncSession = Depends(get_db),
):
    """Return session history for a product."""
    result = await db.execute(
        select(DBSession)
        .where(DBSession.product_id == product_id)
        .order_by(DBSession.started_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


@app.post("/api/webhooks/github")
async def github_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """
    GitHub webhook listener — processes pull_request closed+merged events.
    Validates HMAC-SHA256 signature if github_webhook_secret is configured.
    """
    config = await db.get(SystemConfig, 1)
    secret = config.github_webhook_secret if config else None

    body = await request.body()

    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            raise HTTPException(401, "Invalid webhook signature")
    else:
        log.warning(
            "GitHub webhook received without signature validation — "
            "configure github_webhook_secret in Admin → Notifications for security"
        )

    event = request.headers.get("X-GitHub-Event", "")
    if event != "pull_request":
        return {"ok": True, "skipped": True}

    payload = await request.json()
    action = payload.get("action")
    pr = payload.get("pull_request", {})
    if action != "closed" or not pr.get("merged"):
        return {"ok": True, "skipped": True}

    pr_number = pr.get("number")
    pr_url    = pr.get("html_url", "")
    branch    = pr.get("head", {}).get("ref", "")

    # Find feature by branch name or PR number
    result = await db.execute(
        select(Feature).where(
            Feature.pr_number == pr_number,
            Feature.status.in_(["Pushed", "Implementing"]),
        )
    )
    feature = result.scalar_one_or_none()
    if not feature:
        result = await db.execute(
            select(Feature).where(Feature.branch_name == branch, Feature.status.in_(["Pushed", "Implementing"]))
        )
        feature = result.scalar_one_or_none()

    if feature:
        feature.status = "Pushed"  # already pushed, just confirm
        log_line = f"[webhook] PR #{pr_number} merged → feature '{feature.name}' confirmed Pushed"
        _session_logs[feature.product_id].append(log_line)

    return {"ok": True}
