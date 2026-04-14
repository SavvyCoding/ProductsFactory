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

# Local feature test run (no Docker, no Claude API — uses Ollama)
python scripts/test_run.py --product-id 3 --working-dir /path/to/product --persona coder

# Local test run with an ad-hoc feature
python scripts/test_run.py \
    --product-id 3 \
    --working-dir /path/to/product \
    --feature "Add hello endpoint" \
    --desc "GET /hello returns {message: hello}" \
    --persona coder
```

## Architecture

### Orchestration Loop (poller.py)

Every `POLL_INTERVAL` seconds (default 60s), the poller:
1. Auth-checks Claude: `claude -p ping` (real API call, validates OAuth tokens are mounted)
2. Auto-discovers new products in `PRODUCTS_BASE_DIR` via `setup_product.py`
3. Kills stale Docker containers via heartbeat check (progress.md not pushed in >45 min)
4. Checks globally for reviewer work (Reviewing+PR) — runs reviewer session first if found
5. Otherwise, selects next product: `ORDER BY last_run_at ASC` (round-robin)
6. Determines persona (designer vs coder) via `/api/features/next-for-persona`
7. Coder: skips if ≥3 open PRs; syncs merged PRs from GitHub → Pushed
8. Launches `docker run --rm productfactory-agent claude -p {prompt} -e AGENT_PERSONA={persona}`

   Alternatively, when `USE_OLLAMA=1` is set, step 8 instead runs `orchestrator/ollama_agent.py` inside the container — a self-contained tool-use loop against Ollama's OpenAI-compatible API (no Claude API key required). Model selection: `DESIGNER_MODEL` (default `gemma3:27b`) for designer/reviewer, `CODER_MODEL` (default `qwen3-coder:30b`) for coder.

### Multi-Agent Personas

Ten personas run as separate Docker sessions, in priority order:

**Feature delivery pipeline** (triggered by feature backlog state):
- **Designer** (`designer.md`): Picks `Approved` features (skip_design=False), writes `docs/feature_NNN_design.md`, sets `Designed`
- **Coder** (`greenfield.md` / `brownfield.md`): Picks `Designed` or `skip_design Approved` features, implements, opens PR, sets `Reviewing`
- **Reviewer** (`reviewer.md`): Picks `Reviewing` features with PR numbers, reviews diff, approves or requests changes, sets `Reviewed`

**Post-coder chain** (run automatically after every successful coder session):
- **QA Tester** (`qa_tester.md`): Adds automated tests to the open PR branch, commits and pushes
- **Security Auditor** (`security_auditor.md`): Audits PR diff for OWASP issues, files `bug` features for any found
- **Recommender** (`recommender.md`): Searches competitors, POSTs new feature ideas with `source: "ai"`

**Scheduled maintenance** (run by `determine_persona()` when no feature work exists, on a schedule stored in `product.config`):
- **Documenter** (`documenter.md`): Updates README, CHANGELOG, ARCHITECTURE; every 3 days
- **Analytics** (`analytics.md`): Analyses velocity/backlog health, files high-value features; every 7 days
- **Refactorer** (`refactorer.md`): Identifies tech debt, creates `chore` features; every 7 days
- **DevOps** (`devops.md`): Audits Dockerfile/CI/deps, creates infra `chore` features; every 14 days

**Backlog generation** (last resort when nothing else to do):
- **Planner** (`planner.md`): Reads codebase context, creates new `Pending` features for PM approval

Scheduling state for maintenance personas is stored in `product.config` as `last_{persona}_at` (ISO timestamp). The agent writes its own completion timestamp via `PATCH /api/products/{id}`.

### Product Lifecycle

```
registered → discovered → ready → [running] → (repeats)
```

Feature states: `Pending → Approved → [Designing → Designed →] Implementing → Reviewing → [Reviewed →] Pushed` (also: `Deferred`, `Blocked`, `Rejected`, `Reverted`)

New DB columns on `features`: `skip_design`, `design_doc`, `design_doc_path`, `review_outcome`, `review_notes`, `feature_type` (`feature | bug | chore`, default `feature`)
New DB column on `sessions`: `persona`
New table `feature_reviews`: full audit trail of code reviews (one row per review cycle, FK → features)

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `alerts`, `feature_reviews`
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation; requires `TEST_DATABASE_URL`

### Templates

`templates/renderer.py` installs 3 files into every product repo on discovery (idempotent — skips if already present):
- `AGENT_WORKFLOW.md` — Claude's standing operating procedure (startup checklist, batch planning, per-feature loop)
- `CLAUDE.md` — stack-specific config (test command, folder layout); selected from `templates/stacks/{python|node|go|default}/`
- `ARCHITECTURE.md` — architecture doc template the PM fills in

### Local Development Without Docker (Ollama)

`scripts/test_run.py` runs an agent session on the host without Docker or the Claude API:
- Calls `orchestrator/ollama_agent.py` directly (same tool-use loop used inside containers)
- Intercepts `git push` and `gh pr create` via shims in a temp dir — local commits happen, remote ops are no-ops (`--allow-push` flag enables real operations)
- Advances feature statuses in the PM API on start/finish, and rolls back on failure
- Tees all output to `{working_dir}/Results/test_run_{persona}_{ts}.log`
- After a successful coder run: generates a product showcase MP4 via `orchestrator/video_builder.py` (requires `Pillow`, `edge-tts`, `moviepy`/ffmpeg) and runs the recommender agent
- On Windows, `ollama_agent.py` auto-detects Git Bash to execute shell commands; Ollama is expected at `http://localhost:11434` (or `OLLAMA_HOST`)

`orchestrator/ollama_agent.py` also has a standalone mode: `python orchestrator/ollama_agent.py -p "prompt"`.

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
- `AGENT_BACKEND` — Set to `ollama` to use local Ollama instead of Claude CLI
- `CLAUDE_DIR` / `SSH_DIR` — Host paths for OAuth tokens and deploy key mounts
- `SESSION_TIMEOUT_MINUTES` — Kill Docker container after N minutes (default: 90)
- `STALE_THRESHOLD_MINUTES` — Alert if progress.md not pushed in N minutes (default: 45)
- `OLLAMA_HOST` — Ollama base URL (default: `http://host.docker.internal:11434` inside Docker, `http://localhost:11434` for local runs)
- `DESIGNER_MODEL` / `CODER_MODEL` — Ollama model names (defaults: `gemma3:27b` / `qwen3-coder:30b`)
- `MAX_TURNS` — Hard cap on Ollama agent turns per session (default: 80)
