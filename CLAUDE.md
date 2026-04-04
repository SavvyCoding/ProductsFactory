# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

ProductFactory is a 24/7 autonomous development system. It orchestrates Claude Code agents inside isolated Docker containers to implement features across multiple product repos, monitored by a FastAPI PM dashboard.

**Three independent subsystems:**
- **Orchestrator** (`orchestrator/`) — Windows poller that runs on the host, picks products, launches Docker containers
- **PM Website** (`website/`) — FastAPI dashboard + REST API for managing products and features
- **Agent Image** (`deploy/docker/Dockerfile`) — Docker image Claude runs inside per product session

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Start services (PostgreSQL + PM website)
docker-compose up -d

# Run migrations
alembic upgrade head

# Run tests (requires DATABASE_URL or TEST_DATABASE_URL env var)
pytest tests/ -v

# Run a single test file
pytest tests/test_poller.py -v

# Run a single test
pytest tests/test_website.py::test_list_products -v

# Run with coverage
pytest tests/ --cov=orchestrator,website --cov-fail-under=70

# Start the poller (Windows host, after .env is configured)
python orchestrator/poller.py

# Start PM website locally (outside Docker)
uvicorn website.main:app --host 0.0.0.0 --port 8080
```

## Architecture

### Orchestration Loop (poller.py)

Every `POLL_INTERVAL` seconds (default 60s), the poller:
1. Auth-checks Claude: `claude -p ping` (real API call, validates OAuth tokens are mounted)
2. Auto-discovers new products in `PRODUCTS_BASE_DIR` via `setup_product.py`
3. Kills stale Docker containers via heartbeat check (progress.md not pushed in >45 min)
4. Selects next product: `ORDER BY last_run_at ASC` (round-robin, null = never run = highest priority)
5. Skips products with ≥3 open PRs (waits for PM to merge)
6. Syncs merged PRs from GitHub → marks features as "Pushed" in DB
7. Launches `docker run --rm productfactory-agent claude -p {prompt}` with workspace, OAuth, and SSH deploy key mounted

### Product Lifecycle

```
registered → discovered → ready → [running] → (repeats)
```

Feature states: `Pending → Approved → Implementing → Pushed`

The poller only picks features with status `Approved`. Claude claims them (`Implementing`), implements, pushes a branch, opens a PR, then the poller reconciles merged PRs back to `Pushed`.

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `alerts`

### Templates

`templates/renderer.py` installs 3 files into every product repo on discovery (idempotent — skips if already present):
- `AGENT_WORKFLOW.md` — Claude's standing operating procedure (startup checklist, batch planning, per-feature loop)
- `CLAUDE.md` — stack-specific config (test command, folder layout); selected from `templates/stacks/{python|node|go|default}/`
- `ARCHITECTURE.md` — architecture doc template the PM fills in

### Auth & Security

- PM website uses HTTP Basic Auth (`secrets.compare_digest` — timing-safe)
- REST API (`/api/...`) has no auth (internal use by the poller)
- OAuth tokens (`~/.claude`) mounted read-only into agent containers
- SSH deploy key mounted read-only; not the full `~/.ssh` directory
- Agent containers run on an isolated bridge network, not `--network host`, no `--privileged`

## Key Conventions

- **Async-first in website**: use `async def` + `await` for all DB and HTTP calls in `website/`
- **Sync in orchestrator**: `orchestrator/` runs on the Windows host with standard `httpx` (sync) calls to the PM API
- **No hardcoded config**: everything from env vars (`.env.example` is the canonical reference)
- **Idempotent operations**: `setup_product.py` discovery is safe to run multiple times; templates only written if missing
- **last_run_at only updated on success**: poller sets `last_run_at` only when Docker exits with code 0, ensuring failed runs don't advance the round-robin pointer

## Environment Variables

See `.env.example` for all variables. Critical ones:
- `DATABASE_URL` — PostgreSQL URL (asyncpg driver for website, psycopg2 for Alembic)
- `PM_API_URL` — PM website internal URL (poller → website REST API)
- `AGENT_IMAGE` — Docker image name (default: `productfactory-agent`)
- `CLAUDE_DIR` / `SSH_DIR` — Host paths for OAuth tokens and deploy key mounts
- `SESSION_TIMEOUT_MINUTES` — Kill Docker container after N minutes (default: 90)
- `STALE_THRESHOLD_MINUTES` — Alert if progress.md not pushed in N minutes (default: 45)
