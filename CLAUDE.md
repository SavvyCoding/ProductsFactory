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

### Windows Service Deployment

The poller runs persistently on the Windows host via two options:
- **Task Scheduler** (recommended): `deploy/windows/install_task.ps1` registers it as a system task that auto-starts on login
- **Startup folder**: `deploy/install.sh` places a shortcut in the Windows startup folder

The wrapper script `deploy/windows/start_poller.ps1` loads `.env`, then runs the poller in a crash-restart loop. The agent Docker image is built with `deploy/docker/build.sh`, which also creates the `productfactory-net` external bridge network.

## Architecture

### Orchestration Loop (poller.py)

On startup the poller acquires a **distributed lock** via `POST /api/poller/lock` (stored in `system_config` — 409 if another live poller holds it). A background thread refreshes the heartbeat every 15 s (`POST /api/poller/heartbeat`); TTL is 30 s. Lock released via `DELETE /api/poller/lock` on clean exit (also registered with `atexit`).

Every `POLL_INTERVAL` seconds (default 60s), the poller:
1. Auth-checks Claude: `claude -p ping` (real API call, validates OAuth tokens are mounted)
2. Auto-discovers new products in `PRODUCTS_BASE_DIR` via `setup_product.py`
3. Kills stale Docker containers via heartbeat check (progress.md not pushed in >45 min)
4. Checks globally for reviewer work (Reviewing+PR) — runs reviewer session first if found
5. Checks for `run_trainer_now` products — runs Product Trainer if flagged
6. Otherwise, selects next product: `run_now=True` products have priority, then `ORDER BY last_run_at ASC` (round-robin)
7. Determines persona (designer vs coder) via `/api/features/next-for-persona`
8. Coder: skips if ≥3 open PRs; syncs merged PRs from GitHub → Pushed
9. Launches `docker run --rm productfactory-agent claude -p {prompt} -e AGENT_PERSONA={persona}`
10. A background thread live-polls `session_result.json` every 30 s while the container runs, applying DB updates in real-time
11. After Docker exits: final reconcile of `session_result.json`; if `auto_merge_enabled` (system_config), high-confidence reviewed PRs are merged automatically before the file is deleted (see Agent Contract below)

   Alternatively, when `USE_OLLAMA=1` is set, step 8 instead runs `orchestrator/ollama_agent.py` inside the container — a self-contained tool-use loop against Ollama's OpenAI-compatible API (no Claude API key required). Model selection: `DESIGNER_MODEL` (default `gemma3:27b`) for designer/reviewer, `CODER_MODEL` (default `qwen3-coder:30b`) for coder.

### Greenfield Scaffolding

When `setup_product.py` discovers a new product directory with fewer than `BROWNFIELD_FILE_THRESHOLD` (default: 10) source files, it is classified as **greenfield**. `greenfield_scaffold.py` then:
1. Creates a GitHub repo via the GitHub API (using `system_config.github_pat`)
2. Generates a per-product Ed25519 SSH deploy key (`id_ed25519_{product_name}`) in `SSH_DIR`
3. Uploads the public key to GitHub as a deploy key with write access
4. Initializes the local git repo, commits templates, and pushes to GitHub
5. Stores the repo URL in `product.github_repo`

For brownfield products (existing repos), `setup_product.py` only installs templates and registers in the DB. Deploy keys fall back to `id_ed25519_productfactory` if no per-product key exists.

### Multi-Agent Personas

Eleven personas run as separate Docker sessions, in priority order:

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

**On-demand / special triggers**:
- **Product Trainer** (`product_trainer.md`): Generates a showcase MP4 of shipped features; triggered by `product.run_trainer_now = True` (set via the PM website). Runs immediately when the flag is set, bypassing round-robin scheduling.
- **Analysis Run** (`analysis_run.md`): Brownfield codebase analysis; triggered via `POST /product/{id}/trigger_analysis` from the PM website.

Scheduling state for maintenance personas is stored in `product.config` as `last_{persona}_at` (ISO timestamp). The agent writes its own completion timestamp via `PATCH /api/products/{id}`.

### Product Lifecycle

```
registered → discovered → ready → [running] → (repeats)
```

Feature states: `Pending → Approved → [Designing → Designed →] Implementing → Reviewing → [Reviewed →] Pushed` (also: `Deferred`, `Blocked`, `Rejected`, `Reverted`)

**Status transition authority**: PMs (via the website) are restricted to a whitelist in `website/schemas.py` (`PM_ALLOWED_TRANSITIONS`). Agents calling the internal REST API bypass this gate entirely and can move features to any valid state.

### Agent Contract (session_result.json)

Agents write `{working_dir}/session_result.json` **incrementally** as they complete each feature — one JSON object per line (newline-delimited). The poller applies updates in two passes:
1. **Live poll** (background thread, every 30 s while container runs) — applies new lines in real-time
2. **Final reconcile** (after container exits) — re-applies all entries idempotently, then deletes the file

Features with no entry in `session_result.json` and no open PR are **rolled back to `Approved`** to avoid stuck states — this handles crashes mid-session.

Each line format:
```json
{"id": 42, "status": "Reviewing", "pr_number": 7}
```

When `auto_merge_enabled` is set in `system_config`, reviewer sessions with high-confidence approvals trigger automatic GitHub PR merges before the final reconcile runs. PRs with conflicts are closed instead; the feature is set back to `Implementing` (clears `pr_number`).

### Per-Product Configuration

`product.config` (JSONB column) stores per-product runtime state and overrides:
- `last_{persona}_at` — ISO timestamp used to gate scheduled maintenance personas
- `quiet_hours_start` / `quiet_hours_end` — Hour of day (0–23) to suppress sessions
- `daily_session_cap` — Max sessions per day for this product
- `max_features_per_run` — Per-product override for the global `MAX_FEATURES_PER_RUN`

Additionally, a `product_config.json` file in the product working directory (read by `setup_product.py` on discovery) can seed:
- `preferred_stack` — Selects which `templates/stacks/` variant to install
- `vision` — High-level product description passed to agents
- `suggested_features` — Initial feature list auto-created on discovery

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `alerts`, `feature_reviews`
- Notable columns: `features.skip_design`, `features.design_doc`, `features.design_doc_path`, `features.review_outcome`, `features.review_notes`, `features.feature_type` (`feature | bug | chore`); `sessions.persona`, `sessions.container_id`; `system_config.poller_pid/host/locked_at/heartbeat_at`
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation; requires `TEST_DATABASE_URL`

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

### Auth & Security

- PM website uses HTTP Basic Auth (`secrets.compare_digest` — timing-safe); falls back to env-var credentials if no `pm_users` rows exist
- REST API (`/api/...`) has no auth (internal use by the poller)
- OAuth tokens (`~/.claude`) mounted read-only into agent containers
- SSH deploy keys in `SSH_DIR`: per-product key `id_ed25519_{product_name}` with fallback to `id_ed25519_productfactory`; mounted read-only (not the full `~/.ssh` directory)
- Agent containers run on an isolated bridge network, not `--network host`, no `--privileged`
- GitHub PAT stored in `system_config.github_pat` (DB), not an env var — fetched fresh each API call so live updates take effect without restarting the poller

## Key Conventions

- **Async-first in website**: use `async def` + `await` for all DB and HTTP calls in `website/`
- **Sync in orchestrator**: `orchestrator/` runs on the Windows host with standard `httpx` (sync) calls to the PM API
- **No hardcoded config**: everything from env vars (`.env.example` is the canonical reference)
- **Idempotent operations**: `setup_product.py` discovery is safe to run multiple times; templates only written if missing
- **last_run_at only updated on success**: poller sets `last_run_at` only when Docker exits with code 0, ensuring failed runs don't advance the round-robin pointer
- **Model changes require a migration**: add the column to `website/models.py` AND create a new `db/migrations/versions/NNN_*.py` file — Alembic does not auto-generate these

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
- `MAX_OPEN_PRS` — Coder skips the product if open PR count meets or exceeds this (default: 3)
- `OLLAMA_HOST` — Ollama base URL (default: `http://host.docker.internal:11434` inside Docker, `http://localhost:11434` for local runs)
- `DESIGNER_MODEL` / `CODER_MODEL` — Ollama model names (defaults: `gemma3:27b` / `qwen3-coder:30b`)
- `MAX_TURNS` — Hard cap on Ollama agent turns per session (default: 80)
