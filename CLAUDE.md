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

# Generate ProductFactory showcase video (requires Pillow, edge-tts, moviepy, playwright)
python scripts/build_pf_video.py
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
7. Determines persona via `/api/features/next-for-persona`; sprint-aware: checks for retrospective (completed sprint, no retro) → product_planner (active sprint, Approved features) → designer → coder → reviewer
8. Coder: skips if ≥ MAX_OPEN_PRS open PRs; syncs merged/closed PRs from GitHub
9. Reconciles in-flight PRs against GitHub (`reconcile_in_flight_prs`) — self-heals stuck features every cycle
10. Launches `docker run --rm productfactory-agent claude -p {prompt} -e AGENT_PERSONA={persona}`
11. A background thread live-polls `session_result.json` every 30 s while the container runs, applying DB updates in real-time
12. After Docker exits: final reconcile of `session_result.json`; if `auto_merge_enabled` (system_config), high-confidence reviewed PRs are merged automatically before the file is deleted (see Agent Contract below)

   Alternatively, when `USE_OLLAMA=1` is set, step 10 instead runs `orchestrator/ollama_agent.py` inside the container — a self-contained tool-use loop against Ollama's OpenAI-compatible API (no Claude API key required). Model selection: `DESIGNER_MODEL` (default `gemma3:27b`) for designer/reviewer, `CODER_MODEL` (default `qwen3-coder:30b`) for coder.

### Stuck Feature Self-Healing

Four layers prevent features from getting stuck:

1. **`_apply_session_entry` (real-time)**: When an agent writes a `Reviewing` entry without `pr_number`, the poller extracts it from `pr_url` automatically. Reviewer `Reviewing` entries are filtered out entirely (reviewers must not set features back to `Reviewing`).
2. **`reconcile_in_flight_prs` (every cycle)**: Checks ALL in-flight features (Implementing/Reviewing/Reviewed) with a PR reference against GitHub. Merged → Pushed immediately. Closed-unmerged → reset to Approved.
3. **`reconcile_merged_prs` (every cycle)**: Batch-reconciles the 20 most recently closed PRs. Handles both merged and closed-unmerged PRs. Parses `pr_number` from `pr_url` as fallback.
4. **`reset_stuck` (time-gated)**: Features stuck in agent states (Implementing/Designing/Reviewing) for longer than `stuck_feature_timeout_hours` (default 0.75h / 45 min, configurable as float) are reset to their prior ready state.

### Blocked Sprint Quarantine Route

Each `features.fix_attempts` is bumped on every changes-requested rework cycle, false-success detection, and killed-session recovery. When it crosses `system_config.max_fix_attempts` (default 5), `orchestrator/github_client.py` routes the feature to a per-product **Blocked sprint** (`sprints.kind = "blocked"`) for PM triage instead of letting it cycle indefinitely.

Blocked sprints are excluded from active-sprint selection, DoD gates, sprint capacity caps, and sprint-PR provisioning — they are purely a holding pen surfaced on the PM dashboard. The feature columns `fix_attempts`, `blocked_reason` and the sprint column `kind` are the persistence layer; migration `036_blocked_sprint.py` introduced them.

### Phase-1 Supervisor

`orchestrator/supervisor.py` is a rule-based safety net (no LLM) that catches stuck-state patterns the deterministic orchestrator misses. It is wired into three hook points:
- **Post-coder pipeline** (`docker_runner.py` after every coder session) — runs `detect_false_success` (exit 0 but no PR) and `detect_kill_recovery` (non-zero exit) to bump `fix_attempts` so the Blocked-sprint route triggers faster instead of waiting for `reset_stuck`.
- **Per-product reconcile sweep** (`deploy/orchestrator/tools.py::reconcile_prs`) — runs `detect_dirty_prs`, `detect_overlapping_prs`, `detect_orphan_approved`, `detect_rapid_flap`.
- **No-work cycle hook** — runs `detect_auto_plan` (active sprint dead + ≥N unsprinted Approved → call plan-sprints) and `detect_merge_stall` (sprint all-Reviewed but PR not merging for ≥1h → alert).

Every detector firing — even in dry-run — writes one row to the `supervisor_actions` audit table (`detector`, `target_type`, `target_id`, `action`, `reason`, `dry_run`, `created_at`) so the PM dashboard can show what the system has been doing automatically. Detector toggles + thresholds live in `system_config` (`supervisor_*_enabled`, `supervisor_*_min_*`, etc.) — flip them without restarting; the orchestrator picks them up next cycle. `supervisor_dry_run_only=True` is a global kill switch that lets all detectors log but skip mutations. Migrations `037_supervisor_actions.py` (audit table + 10 flags) and `038_supervisor_orphan_flap.py` (orphan_approved + rapid_flap flags) own the schema.

### Session FSM

`sessions.status` is the canonical session lifecycle state — never parse docker output or file mtimes when a session's state is in question, consult this column. Statuses: `pending` (DB row created, docker not yet started) → `starting` (docker run launched) → `running` (container live) → `wrapping` (agent exited, harvester applying results) → `ended` (clean exit_code=0) | `killed` (watchdog/timeout/SIGKILL) | `orphaned` (DB says running but no container — reconciler recovers).

Three actors drive transitions: the **watchdog** (kills stale sessions past `expected_deadline`, sets `killed` + `kill_reason`), the **reconciler** (recovers `orphaned` sessions when no container exists), and the **harvester** (applies session_result.json results in the `wrapping` phase). Every transition is appended to `session_events` (table) for audit. Related columns: `heartbeat_at`, `expected_deadline`, `kill_reason`, `log` (last N lines, capped by `SESSION_LOG_MAXLEN`).

### Greenfield Scaffolding

When `setup_product.py` discovers a new product directory with fewer than `BROWNFIELD_FILE_THRESHOLD` (default: 10) source files, it is classified as **greenfield**. `greenfield_scaffold.py` then:
1. Creates a GitHub repo via the GitHub API (using `system_config.github_pat`)
2. Generates a per-product Ed25519 SSH deploy key (`id_ed25519_{product_name}`) in `SSH_DIR`
3. Uploads the public key to GitHub as a deploy key with write access
4. Initializes the local git repo, commits templates, and pushes to GitHub
5. Stores the repo URL in `product.github_repo`

For brownfield products (existing repos), `setup_product.py` only installs templates and registers in the DB. Deploy keys fall back to `id_ed25519_productfactory` if no per-product key exists.

### Multi-Agent Personas

Thirteen personas run as separate Docker sessions, in priority order:

**Sprint-driven** (checked first when a sprint is active):
- **Product Planner** (`product_planner.md`): Writes detailed user story docs (`docs/story_{NNN}.md`) for `Approved` features in the active sprint; sets `Designed`. Triggered when an active sprint has Approved features with no design doc.
- **Retrospective** (`retrospective.md`): Runs after sprint completion — writes `docs/retro_sprint_{ID}.md`, files chore features for action items, calls `POST /api/sprints/{id}/sign-off` with `gate=retro_done`. Triggered when a sprint is `completed` but has no `retro_doc_path`.

**Feature delivery pipeline** (triggered by feature backlog state):
- **Designer** (`designer.md`): Picks `Approved` features (skip_design=False), writes `docs/feature_NNN_design.md`, sets `Designed`
- **Coder** (`greenfield.md` / `brownfield.md`): Picks `Designed` or `skip_design Approved` features, implements, opens PR, sets `Reviewing`
- **Reviewer** (`reviewer.md`): Picks `Reviewing` features with PR numbers, reviews diff, approves or requests changes, sets `Reviewed`

**Post-coder chain** (run automatically in `docker_runner.py` after every successful coder session):
- **QA Tester** (`qa_tester.md`): Adds automated tests to the open PR branch, commits and pushes; calls sign-off `gate=qa_passed` on the sprint
- **Security Auditor** (`security_auditor.md`): Audits PR diff for OWASP issues, files `bug` features for any found; calls sign-off `gate=security_clean` on the sprint
- **Recommender** (`recommender.md`): Searches competitors, POSTs new feature ideas with `source: "ai"` (gated by pending feature count)

**Scheduled maintenance** (run by `determine_persona()` when no feature work exists, on a schedule stored in `product.config`):
- **Documenter** (`documenter.md`): Updates README, CHANGELOG, ARCHITECTURE; every 3 days
- **Analytics** (`analytics.md`): Analyses velocity/backlog health, files high-value features; every 7 days
- **Refactorer** (`refactorer.md`): Identifies tech debt, creates `chore` features; every 7 days
- **DevOps** (`devops.md`): Audits Dockerfile/CI/deps, creates infra `chore` features; every 14 days

**Backlog generation** (last resort when nothing else to do):
- **Planner** (`planner.md`): Reads codebase context, creates new `Pending` features for PM approval

**On-demand / special triggers**:
- **Product Trainer** (`product_trainer.md`): Generates a showcase MP4 of shipped features; triggered by `product.run_trainer_now = True` (set via the PM website). Runs immediately when the flag is set, bypassing round-robin scheduling. Flag auto-cleared after launch.
- **Analysis Run** (`analysis_run.md`): Brownfield codebase analysis; triggered via `POST /product/{id}/trigger_analysis` from the PM website.

Scheduling state for maintenance personas is stored in `product.config` as `last_{persona}_at` (ISO timestamp). The agent writes its own completion timestamp via `PATCH /api/products/{id}`.

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

### Agent Contract (session_result.json)

Agents write `{working_dir}/session_result.json` **incrementally** as they complete each feature — one JSON object per line (newline-delimited). The poller applies updates in two passes:
1. **Live poll** (background thread, every 30 s while container runs) — applies new lines in real-time
2. **Final reconcile** (after container exits) — re-applies all entries idempotently, then deletes the file

Features with no entry in `session_result.json` and no open PR are **rolled back to `Approved`** to avoid stuck states — this handles crashes mid-session.

Each line format:
```json
{"id": 42, "status": "Reviewing", "pr_number": 7}
```

Important behaviours:
- The poller unwraps nested `{"features": [...]}` format if an agent writes it, but this is non-standard
- Reviewer entries with `status: "Reviewing"` are filtered out (reviewers must only write `Reviewed` or `Implementing`)
- `Reviewing` entries without `pr_number` trigger a fallback: `pr_number` is extracted from `pr_url` if present
- When `auto_merge_enabled` is set in `system_config`, reviewer sessions with high-confidence approvals trigger automatic GitHub PR merges before the final reconcile runs. PRs with conflicts are closed instead; the feature is set back to `Implementing` (clears `pr_number`).

### Per-Product Configuration

`product.config` (JSONB column) stores per-product runtime state and overrides:
- `last_{persona}_at` — ISO timestamp used to gate scheduled maintenance personas
- `quiet_hours_start` / `quiet_hours_end` — Hour of day (0–23) to suppress sessions
- `daily_session_cap` — Max sessions per day for this product
- `max_features_per_run` — Per-product override for the global `MAX_FEATURES_PER_RUN`
- `sprint_pr_mode` — When true, sprint activation provisions a `sprint/<id>` branch + draft PR on GitHub, populating `sprints.branch_name/pr_number/pr_url`. The coder/reviewer/auto-merge wiring to consume that PR is staged work — the flag is currently observed only by sprint activation. Default off.

Additionally, a `product_config.json` file in the product working directory (read by `setup_product.py` on discovery) can seed:
- `preferred_stack` — Selects which `templates/stacks/` variant to install
- `vision` — High-level product description passed to agents
- `suggested_features` — Initial feature list auto-created on discovery

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `session_events`, `alerts`, `feature_reviews`, `sprints`, `phases`, `supervisor_actions`
- JIRA-like tracking tables: `feature_comments` (per-feature discussion, author=pm/poller/persona), `feature_changelog` (auto-tracked field-level audit trail), `labels` + `feature_labels` (product-scoped color tags, many-to-many), `feature_links` (directed relationships: blocks/is_blocked_by/relates_to/duplicates)
- Notable columns: `features.skip_design`, `features.design_doc`, `features.design_doc_path`, `features.review_outcome`, `features.review_notes`, `features.feature_type` (`feature | bug | chore`), `features.due_date`, `features.story_points`, `features.fix_attempts`, `features.blocked_reason`; `sessions.persona`, `sessions.container_id`, `sessions.status` (FSM), `sessions.heartbeat_at`, `sessions.expected_deadline`, `sessions.kill_reason`; `system_config.poller_pid/host/locked_at/heartbeat_at`; `system_config.stuck_feature_timeout_hours` (Float, default 0.75h), `system_config.max_fix_attempts` (default 5), `system_config.supervisor_*` (per-detector flags + thresholds); `sprints.dod_status` (JSONB — gate booleans + agent notes), `sprints.phase_id`, `sprints.release_notes`, `sprints.completed_at`, `sprints.retro_doc_path`, `sprints.kind` (`normal | blocked`)
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation; requires `TEST_DATABASE_URL`

**Feature tracking REST endpoints** (agents and PM can call these):
- Comments: `POST/GET /api/features/{id}/comments` — author field: `pm`, `poller`, or persona name
- Changelog: `GET /api/features/{id}/changelog` — auto-populated on every status/priority/sprint mutation; no manual writes needed
- Labels: `POST /api/labels`, `GET /api/products/{id}/labels`, `POST/DELETE /api/features/{id}/labels/{label_id}`
- Links: `POST/GET /api/features/{id}/links`, `DELETE /api/features/{id}/links/{link_id}`
- Search: `GET /api/features/search?q=...&product_id=...` (PostgreSQL tsvector full-text)
- Overdue: `GET /api/features/overdue` (past due_date, not Pushed/Rejected/Deferred)
- Sync: `POST /api/products/{id}/sync-features` — reads `features.md` and reconciles statuses into DB; called on poller startup and useful after a DB volume wipe

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
- **last_run_at advances on every cycle visit**: orchestrator bumps `last_run_at` after `determine_next_action` runs for a product, regardless of whether it produced a launch — so products that resolve to `action=exit` (no actionable work, PR-gated, etc.) still cycle through the round-robin instead of starving every other product. The post-success bump from `launch_session` (on Docker exit code 0) still happens; the cycle-visit bump is defensive coverage for the no-launch paths.
- **Model changes require a migration**: add the column to `website/models.py` AND create a new `db/migrations/versions/NNN_*.py` file — Alembic does not auto-generate these
- **Route ordering matters**: in `website/main.py`, parameterized routes (`/api/features/{id}`) must come after all static routes at the same path prefix to avoid shadowing
- **Thread safety in orchestrator**: `orchestrator/alerts.py` uses a `threading.Lock` for the webhook fail counter; the poller spawns daemon threads for log streaming and live-polling `session_result.json`
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
- `MAX_OPEN_PRS` — Coder skips the product if open PR count meets or exceeds this (default: 3)
- `OLLAMA_HOST` — Ollama base URL (default: `http://host.docker.internal:11434` inside Docker, `http://localhost:11434` for local runs)
- `DESIGNER_MODEL` / `CODER_MODEL` — Ollama model names (defaults: `gemma3:27b` / `qwen3-coder:30b`)
- `MAX_TURNS` — Hard cap on Ollama agent turns per session (default: 80)
- `AUTH_CHECK_TIMEOUT` — Seconds for Claude CLI auth probe (default: 30)
- `PR_GATE_SLEEP` — Seconds to wait when PR gate is triggered (default: 300)
- `ANTHROPIC_API_KEY` — Optional; used for AI feature recommendations on the greenfield product form
- `SESSION_LOG_MAXLEN` — Max in-memory log lines buffered per session in the PM website (default: 1000)
- `PRODUCTS_BASE_DIR` — Root directory where product repos live; also used for video serving in docker-compose
