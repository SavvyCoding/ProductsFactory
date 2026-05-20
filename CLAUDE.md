# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

ProductFactory is a 24/7 autonomous development system. It orchestrates Claude Code agents inside isolated Docker containers to implement features across multiple product repos, monitored by a FastAPI PM dashboard.

**Three independent subsystems:**
- **Orchestrator** (`orchestrator/` + `deploy/orchestrator/`) — long-running process inside the `pf-orchestrator` container. The cycle loop is `deploy/orchestrator/orchestrate.py` calling `tools.run_cycle` every ~60s; per-cycle decisions live in `orchestrator/cycle/persona.py`. See module docstrings (`orchestrator/auto_merge.py`, `reconcile.py`, `supervisor.py`, `docker_runner.py`) and `orchestrator/INVARIANTS.md` for the behavioral contract. (The host-mode `orchestrator/poller.py` and `orchestrator/dispatch.py` were retired 2026-05-18.)
- **PM Website** (`website/`) — FastAPI dashboard + REST API for managing products and features
- **Agent Image** (`deploy/docker/Dockerfile`) — Docker image Claude runs inside per product session

**Orchestrator subpackage layout** (not obvious from a top-level `ls`):
- `orchestrator/pipelines/` — per-persona post-session pipelines: `post_coder.py` (cut session branch, run lint guards + test execution, commit, push, open PR), `post_doc.py` (designer commits to main), `post_maintenance.py`, `auto_merge_reviewer.py` (per-reviewer-session squash-merge of approved session PRs)
- `orchestrator/session/` — the session FSM. `state_machine.py` defines lifecycle states (incl. the "wrapping" state wired up in `04c4282`); `reconciler.py` reconciles DB session rows against Docker reality; `result_io.py` reads/writes `session_result.json`; `context_builder.py` (Phase 7) builds the pre-coder "related code" context from ARCHITECTURE.md MODULES/ENTRY POINTS/DEPRECATED to stop parallel-module drift. The agent loop is hardened against shape drift / hallucinated tool calls here (`d1bf9a9`).
- `orchestrator/cycle/` — cycle-level helpers: `selection.py` (which product/persona this cycle), `persona.py` (gating per maintenance persona), `locks.py` (per-product mutex), `loop_detector.py` (catches planner spirals like the `kimi-k2.6` pattern).
- `orchestrator/integrations/` — outbound integrations: `github_app.py` (JWT sign + installation-token minting, single chokepoint for all git auth), `github.py`, `git_ops.py` (authenticated push via one-shot credential helper, no token in `.git/config`), `docker_cli.py`.
- `orchestrator/infra/` — `redaction.py` (token redaction in logs).

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Start services (PostgreSQL + PM website)
docker-compose up -d

# Run migrations
alembic upgrade head

# Run tests
# REQUIRED: TEST_DATABASE_URL must point at a DB whose name contains 'test'.
# There is NO fallback to DATABASE_URL — pointing tests at production wiped
# it twice (2026-05-03, 2026-05-05) before the fallback was removed.
# One-time test-DB setup:
#   docker exec ProductsFactoryDB createdb -U productfactory productfactory_test
#   DATABASE_URL=postgresql://...:.../productfactory_test alembic upgrade head
TEST_DATABASE_URL=postgresql://productfactory:PASSWORD@localhost:5432/productfactory_test \
  pytest tests/ -v

# Run a single test file
pytest tests/test_orchestrator_helpers.py -v

# Run a single test
pytest tests/test_website.py::test_list_products -v

# Run with coverage
pytest tests/ --cov=orchestrator,website --cov-fail-under=70

# Start the orchestrator (container; production path)
docker compose --profile orchestrator up -d

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

### 1-PR merge flow (session PR → main)

When `product.config.sprint_pr_mode = true` (default for new products) — the flag name is legacy; under this model it just means "open a session PR per coder run":

1. **Coder session**: agent edits files on the default branch; `orchestrator/pipelines/post_coder.py` cuts a fresh `coder/<session_uid>` branch off the default branch's tip (stash → checkout `-B` → stash-pop; **stash-pop conflicts block the assigned features** with a clear reason rather than force-resolving), commits with `[feature-N]` tags, pushes, and opens a **session PR** (`coder/<uid>` → `main`). The feature row's `pr_number`/`pr_url`/`branch_name` point at the session PR/branch.
2. **Reviewer session**: scoped to a single open session PR (`docker_runner._fetch_assigned_features` groups Reviewing features by `pr_number` and picks the oldest). Reviewer reviews `git log origin/main..{session_branch}` and writes per-feature decisions to `session_result.json`.
3. **Auto-merge** (per-reviewer-session in `auto_merge_reviewer.py` + per-cycle sweep in `auto_merge.py`) squash-merges every Reviewed+approved session PR directly to `main`.
4. **Sprint completion**: planning bucket only. When DoD's `all_features_done` gate passes (all features in the sprint are Pushed/Deferred/Rejected), `_do_complete_sprint` marks the sprint completed, generates release notes from features already Pushed, and activates the next sprint. No PR is merged at sprint completion — features have already shipped to `main` individually through their session PRs.

Pre-checkout (`docker_runner.py`): reviewer lands on the session branch under review; coder/designer stay on the default branch (`_reset_workspace` left them on `main`/`master`). Designer commits design docs straight to `main` (`post_doc.py`).

Rework path: when reviewer rejects features in a session PR, `post_coder.py` detects on the next coder cycle that the rejected features share an open PR (the original session PR) and force-pushes fresh commits to its branch, preserving the reviewer's comment thread.

Retired with the 1-PR model (2026-05-15): the sprint integration branch (`sprint/<id>`) and sprint PR; `provision_sprint_pr` / `merge_sprint_pr`; `_maybe_provision_sprint_pr` / `_attempt_merge_completed_sprint_pr`; `reconcile_sprint_pr_state`; supervisor's `_check_sprint_provisioned` / `_fix_sprint_provisioned`; `check_open_pr_invariant`.

### Post-coder quality pipeline

After the coder agent exits, `_run_post_coder_pipeline` (`orchestrator/pipelines/post_coder.py`) runs a sequence of gates BEFORE pushing the branch / opening the PR. Failing any gate either bounces the features back to `Implementing` with `changes_requested` or, for infra failures, rolls them back to `Approved`/`Designed` without bumping `fix_attempts`. The gates are deliberately deterministic (regex + AST + subprocess) so the reviewer never sees the patterns they catch:

- **`_post_coder_lint_check`** (Phases 2–4) — 14 guards. Notable ones: hardcoded secret fallbacks in token/crypto calls (Guard 5); state-changing API routes (`POST/PUT/PATCH/DELETE`) without an auth check, opt-out via `// PUBLIC_ROUTE:` or `# PUBLIC_ROUTE:` annotation (Guard 6); agent-debris filenames — `*.bak`, `*_old_*`, `*_v\d+_*`, `*_complete_*`, `temp_fixed*`, files under `Temp/` or `temp_storage/`, `test_X_qa.py` siblings of `test_X.py` (Guard 13); config-as-gate integrity — refuses commits that lower bars declared in `quality_gates.json` by editing `pytest.ini` / `package.json` directly (Guard 14).
- **`_post_coder_test_check`** (Phase 5) — runs the stack's test command and classifies the outcome four ways:
  - `passed`: continue to push.
  - `env_broken` (jest missing, ENOENT on node_modules, ModuleNotFoundError pytest): roll back to `Approved`/`Designed`, send operator alert, **do NOT bump `fix_attempts`** (infra failures must not auto-Block features after 5 attempts).
  - `collection_errors > 0`: pytest collect-only errors → bounce to `Implementing` with the first failing import quoted.
  - test failures: bounce to `Implementing` with the first failure quoted.

### Supervisor cascade detectors

`orchestrator/supervisor.py` runs after each reviewer session. Two complementary detectors for stuck rework loops:
- `detect_repeated_review_feedback` — **convergent cascade**: same comment signature across N consecutive cycles (reviewer flags the same issue, coder keeps missing it).
- `detect_divergent_review_feedback` (Phase 6) — **divergent cascade**: reviewer flags a *different* issue each cycle while ignoring earlier ones. The 25-comment cumulative-feedback context (`_fetch_recent_review_comments` + `_format_reviewer_feedback`, limit raised 6→25 on 2026-05-07) is injected into the rework coder prompt via `{reviewer_feedback}` to make the running checklist explicit.

### Maintenance personas

`_MAINTENANCE_PERSONAS` in `orchestrator/docker_runner.py` is the canonical set of scheduled personas (gated per-cycle in `orchestrator/cycle/persona.py`): `documenter`, `analytics`, `recommender`, `devops`, `refactorer`, `product_trainer`, and `architect` (Phase 8 — quantitative drift detection: counts documented vs registered endpoints, detects parallel-module drift, verifies `CONFIG GATES` values match `quality_gates.json`, files chore features with `priority=25` + labels `["architecture","drift"]`; capped at 3 features per session).

### Greenfield Scaffolding

When `setup_product.py` discovers a new product directory with fewer than `BROWNFIELD_FILE_THRESHOLD` (default: 10) source files, it is classified as **greenfield**. `greenfield_scaffold.py` then:
1. Creates a private GitHub repo via `POST /orgs/{org}/repos` using a fresh GitHub App installation token (no PAT).
2. Initializes the local repo with `git init -b main`, sets the HTTPS origin (no token embedded).
3. Writes `README.md` + `product_config.json`, commits, and pushes via `git_ops.git_push_authenticated` — the installation token is supplied through a one-shot credential helper and is never persisted to `.git/config`.
4. Creates AI-suggested features as `Pending` and flips the product to `registered` so the next cycle picks up discovery.

Migration note (2026-05-14): per-product Ed25519 SSH deploy keys, `~/.ssh/config` Host blocks, and PAT-based auth are all retired. `greenfield_scaffold.scaffold_greenfield(...)` still takes an `ssh_dir` kwarg for callsite compatibility but ignores it. For brownfield products, `setup_product.py` only installs templates and registers in the DB.

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
- `sprint_pr_mode` — Legacy flag name; under the **1-PR (session-PR) model** introduced on the `feat/pr-per-session` branch (see ARCHITECTURE → 1-PR merge flow above), this flag toggles "open a session PR per coder run". When true, every coder session cuts a `coder/<session_uid>` branch off the default branch and opens its own session PR (head=`coder/<uid>`, base=`main`); the reviewer reviews the session PR, and the per-reviewer / per-cycle auto-merge squash-merges approved session PRs directly to `main`. Sprints are planning buckets — no sprint branch, no sprint PR. With `sprint_pr_mode=false`, the coder pipeline bare-branches without opening a PR (dead path; the per-feature `gh pr create` was removed in Phase 6.2, and the bare-branch path warns + bails). **Default true for new products** as of Phase 6.4 (set via `_seed_product_config` in `website/main.py`); existing products keep their setting and must be migrated explicitly via PATCH on `product.config`.

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
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation. **`TEST_DATABASE_URL` is required and must contain `test` in the database name.** No fallback to `DATABASE_URL`; conftest aborts the session on a banned name (`productfactory`, `postgres`) or any name without a `test` substring. Engine teardown does NOT call `drop_all` — per-test rollback is the only cleanup. Set up the test DB once with `createdb productfactory_test` then `alembic upgrade head` against it.

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

`templates/renderer.py` installs the following files into every product repo on discovery (idempotent — skips if already present):
- `AGENT_WORKFLOW.md` — Claude's standing operating procedure (startup checklist, batch planning, per-feature loop)
- `CLAUDE.md` — stack-specific config (test command, folder layout); selected from `templates/stacks/{python|node|go|default}/`
- `ARCHITECTURE.md` — **structured architecture doc** with six machine-readable sections consumed by other subsystems (Phase 1):
  - `ENTRY POINTS` — canonical app/server/CLI entry files (prevents "5 versions of main.py")
  - `MODULES` — source-of-truth registry of canonical modules per concern; read by `orchestrator/session/context_builder.py` to tell the coder "userStore.py already exists, use it"
  - `RULES` — machine-checkable invariants the post-coder lint guard (`_post_coder_lint_check`, Phase 2) refuses commits against
  - `REFERENCE PATTERNS` — copy-pasteable canonical snippets (auth check, error response, DB lifecycle)
  - `CONFIG GATES` — quality bars cross-checked by the config-integrity guard (Phase 4) against `quality_gates.json`
  - `DEPRECATED` — removal queue; the agent-debris detector (Phase 3) refuses commits that re-introduce listed items
- `quality_gates.json` — per-product quality bars (e.g. `pytest --cov-fail-under`, eslint max-warnings) declared once at discovery. Phase 4's `_post_coder_lint_check` Guard 14 refuses commits that lower these gates by editing `pytest.ini` / `package.json` directly instead of clearing the bar with code.

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
- **Git auth: GitHub App only.** All git push, PR API calls, and repo creation go through short-lived installation access tokens minted by `orchestrator/integrations/github_app.py` (JWT signed with the App's PEM → POST `/app/installations/{id}/access_tokens` → 60-min token, cached and refreshed when < 5 min remains). App config lives in `system_config` (`github_app_id`, `github_app_private_key`, `github_app_installation_id`, `github_org`) — read fresh each call so operators can rotate without restarting the poller. The PM website was migrated off direct `github_pat` reads in `6d16deb`; do not propose PAT fallbacks. SSH deploy keys (per-product `id_ed25519_{name}` and `id_ed25519_productfactory`) are retired.
- Agent containers run as non-root user `agent` (UID 1001) on an isolated bridge network, no `--network host`, no `--privileged`

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
- `deploy/docker/startup.sh` — Runs inside the pm-api container before uvicorn. Sanity-checks DB state (alembic_version presence + core table presence). Refuses to start (exit 2) if alembic_version is populated but all core tables are missing — this used to auto-clear via a "self-heal" that triggered on a single false negative on 2026-05-02 and DROPped the entire schema. Operators must now manually `DELETE FROM alembic_version` to opt into a destructive re-init.

## Environment Variables

See `.env.example` for all variables. Critical ones:
- `DATABASE_URL` — PostgreSQL URL (asyncpg driver for website, psycopg2 for Alembic)
- `PM_API_URL` — PM website internal URL (poller → website REST API)
- `AGENT_IMAGE` — Docker image name (default: `productfactory-agent`)
- `AGENT_BACKEND` — Set to `ollama` to use local Ollama instead of Claude CLI
- `CLAUDE_DIR` — Host path for Claude OAuth token mount into agent containers (`SSH_DIR` is legacy; SSH-based git auth is retired — see Auth & Security)
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
- `system_config.github_app_id` / `github_app_private_key` / `github_app_installation_id` / `github_org` — GitHub App credentials (DB-stored, hot-reloadable). All git auth flows through these.
- `SESSION_LOG_MAXLEN` — Max in-memory log lines buffered per session in the PM website (default: 1000)
- `PRODUCTS_BASE_DIR` — Root directory where product repos live; also used for video serving in docker-compose
