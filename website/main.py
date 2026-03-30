"""
ProductFactory PM Website — FastAPI application.

HTML pages (require Basic Auth):
  GET  /                               — dashboard: product list
  GET  /product/{id}                   — product detail: kanban + feature form
  GET  /product/{id}/progress          — progress.md viewer (fetched from GitHub)

  POST /product/register               — HTML form: PM registers a product
  POST /product/{id}/features          — HTML form: PM adds a feature
  POST /product/{id}/features/{fid}/status  — HTML form: PM changes feature status
  POST /product/{id}/pause             — HTML form: pause product
  POST /product/{id}/resume            — HTML form: resume product
  POST /product/{id}/trigger_analysis  — HTML form: trigger brownfield Analysis Run

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
  GET  /api/alerts/unread              — unread alerts for nav badge
  POST /api/sessions                   — record session start (poller)
  PATCH /api/sessions/{id}             — record session end (poller)

Start: uvicorn website.main:app --host 0.0.0.0 --port 8080
"""

from datetime import datetime, timezone, timedelta

import mistune
from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from website.database import get_db
from website.models import Product, Feature, Session as DBSession, Alert
from website.auth import require_auth
from website import schemas
from website.github import fetch_progress_md, count_open_prs
from website.schemas import PM_ALLOWED_TRANSITIONS

app = FastAPI(title="ProductFactory PM", docs_url=None, redoc_url=None)

app.mount("/static", StaticFiles(directory="website/static"), name="static")
templates = Jinja2Templates(directory="website/templates")

_md = mistune.create_markdown(plugins=["table"])


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


# ══════════════════════════════════════════════════════════════════════════════
# HTML PAGES — PM views (all require Basic Auth)
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), _=Depends(require_auth)):
    """Main dashboard — product list with status badges."""
    result = await db.execute(select(Product).order_by(Product.name))
    products = result.scalars().all()
    alert_count = await _unread_alert_count(db)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "products": products,
        "alert_count": alert_count,
    })


@app.get("/product/{product_id}", response_class=HTMLResponse)
async def product_detail(
    product_id: int, request: Request,
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
):
    """Product detail — feature kanban + add-feature form."""
    product = await _get_product_or_404(product_id, db)
    result = await db.execute(
        select(Feature)
        .where(Feature.product_id == product_id)
        .order_by(Feature.priority, Feature.created_at)
    )
    features = result.scalars().all()
    alert_count = await _unread_alert_count(db)
    open_prs = count_open_prs(product.github_repo or "") if product.github_repo else 0
    return templates.TemplateResponse("product.html", {
        "request": request,
        "product": product,
        "features": features,
        "open_prs": open_prs,
        "pm_transitions": PM_ALLOWED_TRANSITIONS,
        "alert_count": alert_count,
    })


@app.get("/product/{product_id}/progress", response_class=HTMLResponse)
async def progress_view(
    product_id: int, request: Request,
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
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
    })


# ══════════════════════════════════════════════════════════════════════════════
# HTML FORM ENDPOINTS — PM actions (redirect back to UI after each action)
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/product/register")
async def register_product_form(
    working_dir: str = Form(...),
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
):
    """PM registers a product via the dashboard form."""
    existing = await db.execute(
        select(Product).where(Product.working_dir == working_dir)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Product already registered")
    product = Product(working_dir=working_dir)
    db.add(product)
    await db.flush()
    return RedirectResponse(f"/product/{product.id}", status_code=303)


@app.post("/product/{product_id}/features")
async def add_feature_form(
    product_id: int,
    name: str = Form(...),
    description: str = Form(""),
    priority: int = Form(50),
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
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
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
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
    # Re-approve: reset fix_attempts so Claude gets 3 fresh attempts
    if status == "Approved" and feature.fix_attempts > 0:
        feature.fix_attempts = 0
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/pause")
async def pause_product(
    product_id: int,
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
):
    product = await _get_product_or_404(product_id, db)
    if product.status == "ready":
        product.status = "paused"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/resume")
async def resume_product(
    product_id: int,
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
):
    product = await _get_product_or_404(product_id, db)
    if product.status == "paused":
        product.status = "ready"
    return RedirectResponse(f"/product/{product_id}", status_code=303)


@app.post("/product/{product_id}/trigger_analysis")
async def trigger_analysis(
    product_id: int,
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
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


# ══════════════════════════════════════════════════════════════════════════════
# REST API — Products
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products", response_model=list[schemas.ProductOut])
async def api_list_products(db: AsyncSession = Depends(get_db)):
    """Poller fetches all products for discovery loop."""
    result = await db.execute(select(Product))
    return result.scalars().all()


@app.post("/api/products", response_model=schemas.ProductOut, status_code=201)
async def api_create_product(body: schemas.ProductCreate, db: AsyncSession = Depends(get_db), _=Depends(require_auth)):
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
    db: AsyncSession = Depends(get_db), _=Depends(require_auth),
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
# REST API — Misc
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/api/products/{product_id}/open_pr_count")
async def api_open_pr_count(product_id: int, db: AsyncSession = Depends(get_db)):
    """Poller PR count gate. Fetches live from GitHub."""
    product = await _get_product_or_404(product_id, db)
    count = count_open_prs(product.github_repo or "") if product.github_repo else 0
    return {"product_id": product_id, "count": count}


@app.get("/api/alerts/unread", response_model=list[schemas.AlertOut])
async def api_unread_alerts(limit: int = 20, db: AsyncSession = Depends(get_db), _=Depends(require_auth)):
    """Returns unread alerts for the PM website nav badge."""
    result = await db.execute(
        select(Alert)
        .where(Alert.delivered == False)  # noqa: E712
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    alerts = result.scalars().all()
    # Mark as delivered
    for a in alerts:
        a.delivered = True
    return alerts
