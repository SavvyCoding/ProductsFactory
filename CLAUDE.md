# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

ProductFactory is a 24/7 autonomous development system. It orchestrates Claude Code agents inside isolated Docker containers to implement features across multiple product repos, monitored by a FastAPI PM dashboard.

**Three independent subsystems:**
- **Orchestrator** (`orchestrator/`) — long-running host process. See its module docstrings (`orchestrator/poller.py`, `dispatch.py`, `auto_merge.py`, `reconcile.py`, `supervisor.py`) and `orchestrator/INVARIANTS.md` for the behavioral contract.
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

# Generate ProductFactory showcase video (requires Pillow, edge-tts, moviepy, playwright)
python scripts/build_pf_video.py
```

## Architecture

### Greenfield Scaffolding

When `setup_product.py` discovers a new product directory with fewer than `BROWNFIELD_FILE_THRESHOLD` (default: 10) source files, it is classified as **greenfield**. `greenfield_scaffold.py` then:
1. Creates a GitHub repo via the GitHub API (using `system_config.github_pat`)
2. Generates a per-product Ed25519 SSH deploy key (`id_ed25519_{product_name}`) in `SSH_DIR`
3. Uploads the public key to GitHub as a deploy key with write access
4. Initializes the local git repo, commits templates, and pushes to GitHub
5. Stores the repo URL in `product.github_repo`

For brownfield products (existing repos), `setup_product.py` only installs templates and registers in the DB. Deploy keys fall back to `id_ed25519_productfactory` if no per-product key exists.

### Product Lifecycle

```
registered → discovered → ready → [running] → (repeats)
```

Feature states: `Pending → Approved → [Designing → Designed →] Implementing → Reviewing → [Reviewed →] Pushed` (also: `Deferred`, `Blocked`, `Rejected`, `Reverted`)

**Status transition authority**: PMs (via the website) are restricted to a whitelist in `website/schemas.py` (`PM_ALLOWED_TRANSITIONS`). Agents calling the internal REST API bypass this gate entirely and can move features to any valid state.

### Sprint Lifecycle

**Phases** (optional) group sprints for large products. Sprints contain features and have a **Definition of Done (DoD)** with 5 gates:

1. **all_features_done** — All sprint features are Pushed/Deferred/Rejected (auto-computed)
2. **no_open_prs** — No open PRs against sprint features (auto-computed)
3. **qa_passed** — QA Tester calls `POST /api/sprints/{id}/sign-off` with `gate=qa_passed`
4. **security_clean** — Security Auditor calls sign-off with `gate=security_clean`
5. **retro_done** — Retrospective agent calls sign-off with `gate=retro_done` + `retro_doc_path`

When all gates pass (`POST /api/sprints/{id}/check-dod`): sprint marked `completed`, `completed_at` set, release notes auto-generated into `sprints.release_notes`, and the next planned sprint in the phase is activated. A phase is marked completed when it has no more planned/active sprints.

`POST /api/products/{id}/plan-sprints` — LLM auto-plans phases + sprints from Approved features.

### Per-Product Configuration

`product.config` (JSONB column) stores per-product runtime state and overrides:
- `last_{persona}_at` — ISO timestamp used to gate scheduled maintenance personas
- `quiet_hours_start` / `quiet_hours_end` — Hour of day (0–23) to suppress sessions
- `daily_session_cap` — Max sessions per day for this product
- `max_features_per_run` — Per-product override for the global `MAX_FEATURES_PER_RUN`
- `sprint_pr_mode` — When true, sprint activation calls `orchestrator.sprint_pr.provision_sprint_pr` to cut a `sprint/<id>` branch + draft PR on GitHub and populate `sprints.branch_name/pr_number/pr_url`. The coder/reviewer/qa_tester/security_auditor pipelines all assume this PR is the only PR for the product: coder commits stack onto the sprint branch, reviewer posts per-commit comments on the sprint PR, auto-merge merges the sprint PR once every sprint feature is merge-eligible, and sprint completion squash-merges the PR before activating the next sprint (halting on conflict — see Phase 6.3). With `sprint_pr_mode=false` and an active sprint, the coder pipeline marks features Blocked rather than opening fresh per-feature PRs (the per-feature `gh pr create` path was removed in Phase 6.2). **Default true for new products** as of Phase 6.4 (set via `_seed_product_config` in `website/main.py`); existing products keep their setting and must be migrated explicitly via PATCH on `product.config`.

Additionally, a `product_config.json` file in the product working directory (read by `setup_product.py` on discovery) can seed:
- `preferred_stack` — Selects which `templates/stacks/` variant to install
- `vision` — High-level product description passed to agents
- `suggested_features` — Initial feature list auto-created on discovery

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `alerts`, `feature_reviews`, `sprints`, `phases`
- JIRA-like tracking tables: `feature_comments` (per-feature discussion), `feature_changelog` (auto-tracked field-level audit trail), `labels` + `feature_labels` (product-scoped color tags, many-to-many), `feature_links` (directed relationships: blocks/is_blocked_by/relates_to/duplicates)
- Notable columns: `features.skip_design`, `features.design_doc`, `features.design_doc_path`, `features.review_outcome`, `features.review_notes`, `features.feature_type` (`feature | bug | chore`), `features.due_date`, `features.story_points`, `features.fix_attempts`, `features.blocked_reason`; `sprints.dod_status` (JSONB — gate booleans + agent notes), `sprints.phase_id`, `sprints.release_notes`, `sprints.completed_at`, `sprints.retro_doc_path`, `sprints.kind` (`normal | blocked`)
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation; requires `TEST_DATABASE_URL`

**Feature tracking REST endpoints** (agents and PM can call these):
- Comments: `POST/GET /api/features/{id}/comments` — author field: `pm`, `poller`, or persona name
- Changelog: `GET /api/features/{id}/changelog` — auto-populated on every status/priority/sprint mutation; no manual writes needed
- Labels: `POST /api/labels`, `GET /api/products/{id}/labels`, `POST/DELETE /api/features/{id}/labels/{label_id}`
- Links: `POST/GET /api/features/{id}/links`, `DELETE /api/features/{id}/links/{link_id}`
- Search: `GET /api/features/search?q=...&product_id=...` (PostgreSQL tsvector full-text)
- Overdue: `GET /api/features/overdue` (past due_date, not Pushed/Rejected/Deferred)
- Sync: `POST /api/products/{id}/sync-features` — reads `features.md` and reconciles statuses into DB; useful after a DB volume wipe

### Docker Network Model

Two networks serve different purposes:
- **`pf-internal`** — compose-internal only (PostgreSQL ↔ PM website); never exposed outside compose
- **`productfactory-net`** — external bridge created by `deploy/docker/build.sh`; both the PM website container and agent containers attach to this, so agents can reach `http://pm-api:8080`

Agent containers run as non-root user `agent` (UID 1001), with no `--privileged` flag and no host network access. The PM website is reachable from agents via `--add-host pm-api:host-gateway` set in `docker_runner.py`.

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

### Video Generation

Two video builders exist:
- **`orchestrator/video_builder.py`** — Per-product showcase video (called only from `scripts/test_run.py` during local dev). Generates slides with Pillow + TTS narration. Output: `output/product_video_<timestamp>.mp4`
- **`scripts/build_pf_video.py`** — ProductFactory itself showcase video with web-quality slides rendered by Playwright/Chromium + Edge TTS narration. Output: `output/productfactory_story_<timestamp>.mp4`. Run manually.

### REST API Route Ordering

FastAPI evaluates routes in definition order. The parameterized `GET /api/features/{feature_id}` must be defined **after** all literal routes like `/api/features/next-for-persona`, `/api/features/approved`, `/api/features/count`, and `/api/features/reset_stuck` — otherwise it shadows them (FastAPI tries to parse e.g. `"next-for-persona"` as an integer and returns 422).

### Auth & Security

- PM website uses HTTP Basic Auth (`secrets.compare_digest` — timing-safe); falls back to env-var credentials if no `pm_users` rows exist
- REST API (`/api/...`) has no auth (internal-only)
- OAuth tokens (`~/.claude`) mounted read-only into agent containers
- SSH deploy keys in `SSH_DIR`: per-product key `id_ed25519_{product_name}` with fallback to `id_ed25519_productfactory`; mounted read-only (not the full `~/.ssh` directory)
- Agent containers run on an isolated bridge network, not `--network host`, no `--privileged`
- GitHub PAT stored in `system_config.github_pat` (DB), not an env var — fetched fresh each call so live updates take effect at runtime

## Key Conventions

- **Async-first in website**: use `async def` + `await` for all DB and HTTP calls in `website/`
- **No hardcoded config**: everything from env vars (`.env.example` is the canonical reference)
- **Idempotent operations**: `setup_product.py` discovery is safe to run multiple times; templates only written if missing
- **Model changes require a migration**: add the column to `website/models.py` AND create a new `db/migrations/versions/NNN_*.py` file — Alembic does not auto-generate these
- **Route ordering matters**: in `website/main.py`, parameterized routes (`/api/features/{id}`) must come after all static routes at the same path prefix to avoid shadowing
- **No linter/formatter configured**: there is no ruff, black, flake8, or eslint config — code style is enforced by convention only
- **Shared requirements file**: `requirements.txt` covers both website and orchestrator (no separate dev/test requirements)

## Utility Scripts

- `scripts/seed.py` — Seeds the database with sample data for local development (`--wipe-only` to reset without seeding, `--no-sessions` to skip session history)
- `scripts/backup_db.sh` — Backs up the PostgreSQL database
- `scripts/recover_db.py` — Restores a database from backup
- `scripts/test_calculator.py` — End-to-end integration test: creates a greenfield "Calculator" product and runs a full agent cycle (scaffold → discover → coder session → GitHub PR). Use `--dry-run` to stop after scaffolding, `--persona designer` to test other personas.
- `deploy/docker/test_image.sh` — Smoke test for the agent Docker image; verifies all required tools (git, gh, claude, etc.) are installed. Usage: `bash deploy/docker/test_image.sh [image-tag]`
- `deploy/docker/startup.sh` — Runs inside the pm-api container before uvicorn; self-heals stale `alembic_version` rows after a volume wipe so migrations can re-run from scratch

## Environment Variables

See `.env.example` for all variables. Critical ones:
- `DATABASE_URL` — PostgreSQL URL (asyncpg driver for website, psycopg2 for Alembic)
- `PM_API_URL` — PM website internal URL (poller → website REST API)
- `AGENT_IMAGE` — Docker image name (default: `productfactory-agent`)
- `AGENT_BACKEND` — Set to `ollama` to use local Ollama instead of Claude CLI
- `CLAUDE_DIR` / `SSH_DIR` — Host paths for OAuth tokens and deploy key mounts
- `SESSION_TIMEOUT_MINUTES` — Kill Docker container after N minutes (default: 90)
- `STALE_THRESHOLD_MINUTES` — Alert if progress.md not pushed in N minutes (default: 45)
- `BROWNFIELD_FILE_THRESHOLD` — Source file count above which a product is treated as brownfield (default: 10)
- `MAX_FEATURES_PER_RUN` — Max features an agent attempts per session (default: 1; per-product override in DB)
- `system_config.max_features_per_sprint` — Hard cap on features assignable to one sprint (default: 5). Enforced by all feature-to-sprint assignment endpoints; the LLM sprint planner clamps each sprint's plan at this value.
- `OLLAMA_HOST` — Ollama base URL (default: `http://host.docker.internal:11434` inside Docker, `http://localhost:11434` for local runs)
- `DESIGNER_MODEL` / `CODER_MODEL` — Ollama model names (defaults: `gemma3:27b` / `qwen3-coder:30b`)
- `MAX_TURNS` — Hard cap on Ollama agent turns per session (default: 80)
- `AUTH_CHECK_TIMEOUT` — Seconds for Claude CLI auth probe (default: 30)
- `ANTHROPIC_API_KEY` — Optional; used for AI feature recommendations on the greenfield product form
- `SESSION_LOG_MAXLEN` — Max in-memory log lines buffered per session in the PM website (default: 1000)
- `PRODUCTS_BASE_DIR` — Root directory where product repos live; also used for video serving in docker-compose
