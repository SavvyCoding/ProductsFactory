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
  PATCH /api/features/{id}/pm-status   — PM status change with transition validation
  POST /api/features/reset_stuck       — reset Implementing→Approved if >2h (poller)
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

import mistune
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from passlib.context import CryptContext
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from website.database import get_db
from website.models import Product, Feature, Session as DBSession, Alert, SystemConfig, PMUser
from website.auth import require_auth
from website import schemas
from website.github import fetch_progress_md, fetch_architecture_md, count_open_prs, list_open_prs, merge_pr
from website.schemas import PM_ALLOWED_TRANSITIONS

app = FastAPI(title="ProductFactory PM", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="website/static"), name="static")
templates = Jinja2Templates(directory="website/templates")

_md = mistune.create_markdown(plugins=["table"])
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── In-memory session log store ───────────────────────────────────────────────
# product_id → deque of log lines (capped at 1000)
_session_logs: dict[int, deque] = defaultdict(lambda: deque(maxlen=1000))
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


def _config_as_dict(config: SystemConfig | None) -> dict:
    if not config:
        return {
            "products_root_dir": "", "github_org": "", "github_pat": "",
            "github_ssh_key_name": "productfactory-deploy",
            "slack_webhook_url": "", "github_webhook_secret": "", "max_sessions_per_day": "",
        }
    return {
        "products_root_dir":     config.products_root_dir or "",
        "github_org":            config.github_org or "",
        "github_pat":            config.github_pat or "",
        "github_ssh_key_name":   config.github_ssh_key_name or "productfactory-deploy",
        "slack_webhook_url":     config.slack_webhook_url or "",
        "github_webhook_secret": config.github_webhook_secret or "",
        "max_sessions_per_day":  config.max_sessions_per_day or "",
    }


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
    return templates.TemplateResponse("index.html", {
        "request": request,
        "products": products,
        "alert_count": alert_count,
        "current_pm": current_pm,
        "products_root_dir": config.products_root_dir if config else None,
    })


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
        .order_by(DBSession.started_at.desc())
        .limit(20)
    )
    sessions = sess_result.scalars().all()
    alert_count = await _unread_alert_count(db)
    open_prs_list = list_open_prs(product.github_repo or "") if product.github_repo else []
    open_prs = len(open_prs_list)
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
    raw_md = fetch_architecture_md(product.github_repo or "") if product.github_repo else None
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

    raw_md = fetch_progress_md(product.github_repo or "") if product.github_repo else None
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
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """PM adds a feature via the product detail form."""
    await _get_product_or_404(product_id, db)
    feature = Feature(
        product_id=product_id,
        name=name.strip(),
        description=description.strip() or None,
        priority=max(1, min(100, priority)),
        source="pm",
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
        password_hash=pwd_context.hash(password),
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


@app.post("/product/{product_id}/schedule")
async def save_schedule(
    product_id: int,
    quiet_hours_start: str = Form(""),
    quiet_hours_end: str = Form(""),
    daily_session_cap: str = Form(""),
    db: AsyncSession = Depends(get_db), _: str = Depends(require_auth),
):
    """Save per-product scheduling settings."""
    product = await _get_product_or_404(product_id, db)
    product.quiet_hours_start = int(quiet_hours_start) if quiet_hours_start.strip().isdigit() else None
    product.quiet_hours_end   = int(quiet_hours_end)   if quiet_hours_end.strip().isdigit()   else None
    product.daily_session_cap = int(daily_session_cap) if daily_session_cap.strip().isdigit() else None
    await db.flush()
    return RedirectResponse(f"/product/{product_id}?tab=settings", status_code=303)


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
    token = (config.github_pat if config else None) or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise HTTPException(422, "GitHub PAT not configured — set it in Admin")
    ok = merge_pr(product.github_repo or "", pr_number, token)
    if not ok:
        raise HTTPException(502, "GitHub merge failed — check PAT permissions or PR state")
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
    return RedirectResponse(f"/product/{product_id}", status_code=303)


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
    db.add(product)
    await db.flush()
    return product


@app.get("/api/products/next", response_model=schemas.ProductOut | None)
async def api_next_product(db: AsyncSession = Depends(get_db)):
    """
    Returns the next product for the poller to process.
    Selects: status=ready, has ≥1 Approved feature, ordered by last_run_at ASC NULLS FIRST.
    """
    result = await db.execute(
        select(Product)
        .where(Product.status == "ready")
        .where(
            Product.id.in_(
                select(Feature.product_id)
                .where(Feature.status == "Approved")
                .distinct()
            )
        )
        .order_by(Product.last_run_at.asc().nullsfirst())
        .limit(1)
    )
    return result.scalar_one_or_none()


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


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Features
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/features/approved", response_model=list[schemas.FeatureOut])
async def api_approved_features(product_id: int, db: AsyncSession = Depends(get_db)):
    """Poller fetches the approved feature batch for a product."""
    result = await db.execute(
        select(Feature)
        .where(Feature.product_id == product_id, Feature.status == "Approved")
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
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(feature, field, value)
    return feature


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
    feature.status = body.status
    if body.status == "Approved" and feature.fix_attempts > 0:
        feature.fix_attempts = 0
    return feature


@app.post("/api/features/reset_stuck", response_model=schemas.ResetStuckResult)
async def api_reset_stuck(db: AsyncSession = Depends(get_db)):
    """
    Poller calls this each cycle.
    Resets features stuck in 'Implementing' for >2h back to 'Approved'.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    result = await db.execute(
        select(Feature).where(
            Feature.status == "Implementing",
            Feature.updated_at < cutoff,
        )
    )
    stuck = result.scalars().all()
    for f in stuck:
        f.status = "Approved"
    await db.flush()
    return {"reset_count": len(stuck)}


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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=501, detail="ANTHROPIC_API_KEY not configured")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=900,
            messages=[{
                "role": "user",
                "content": (
                    f"You are helping a PM plan an MVP for a new software product.\n\n"
                    f"Product vision: {body.vision}\n"
                    f"Tech stack: {body.preferred_stack}\n\n"
                    "List 6-8 concrete MVP features for a v0.1 release. "
                    "Focus on core functionality only — no nice-to-haves.\n"
                    "Return ONLY a JSON array, no other text:\n"
                    '[{"name": "Feature name (3-7 words)", '
                    '"description": "One sentence: what it does and why it matters."}]'
                ),
            }],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fences if Claude wrapped the JSON
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
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=501, detail="ANTHROPIC_API_KEY not configured")

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    f"You are a senior product manager helping articulate a product vision.\n\n"
                    f"Raw vision input: {body.vision}\n"
                    f"Tech stack: {body.preferred_stack}\n\n"
                    "Rewrite the vision as a concise, well-structured product vision statement. "
                    "Cover: the problem being solved, the target user, the core value proposition, "
                    "and what success looks like. "
                    "Write in plain English — no bullet points, no headings, 3-5 sentences. "
                    "Return only the articulated vision text, nothing else."
                ),
            }],
        )
        return {"vision": message.content[0].text.strip()}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Sessions
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/sessions", response_model=schemas.SessionOut, status_code=201)
async def api_start_session(body: schemas.SessionCreate, db: AsyncSession = Depends(get_db)):
    session = DBSession(**body.model_dump())
    db.add(session)
    await db.flush()
    return session


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


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Session log streaming
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/api/products/{product_id}/session/log")
async def api_append_session_log(product_id: int, request: Request):
    """Called by poller to push agent stdout lines. No auth — internal network only."""
    body = await request.json()
    lines: list[str] = body.get("lines", [])
    for line in lines:
        _session_logs[product_id].append(line)
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
# REST API — Misc
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products/{product_id}/open_pr_count")
async def api_open_pr_count(product_id: int, db: AsyncSession = Depends(get_db)):
    """Poller PR count gate. Fetches live from GitHub."""
    product = await _get_product_or_404(product_id, db)
    count = count_open_prs(product.github_repo or "") if product.github_repo else 0
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
