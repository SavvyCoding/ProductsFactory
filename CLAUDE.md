# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Repo Is

ProductFactory is a 24/7 autonomous development system. It orchestrates Claude Code agents inside isolated Docker containers to implement features across multiple product repos, monitored by a FastAPI PM dashboard.

**Three independent subsystems:**
- **Orchestrator** (`orchestrator/` + `deploy/orchestrator/`) — long-running process inside the `pf-orchestrator` container. The cycle loop is `deploy/orchestrator/orchestrate.py` calling `tools.run_cycle` every ~60s. **Two layers of cycle logic**: `deploy/orchestrator/tools.py` holds the ~700-line `run_cycle` body (Priority 0 trainer/run_persona_now → Priority 1 reviewer preempt → Priority 2 round-robin + launch dispatch), and delegates per-product persona choice to `orchestrator/cycle/persona._decide_action`. See module docstrings (`orchestrator/auto_merge.py`, `reconcile.py`, `supervisor.py`, `docker_runner.py`) and `orchestrator/INVARIANTS.md` for the behavioral contract. (The host-mode `orchestrator/poller.py` and `orchestrator/dispatch.py` were retired 2026-05-18.)
- **PM Website** (`website/`) — FastAPI dashboard + REST API for managing products and features
- **Agent Image** (`deploy/docker/Dockerfile`) — Docker image Claude runs inside per product session

**Orchestrator subpackage layout** (not obvious from a top-level `ls`):
- `orchestrator/pipelines/` — per-persona post-session pipelines: `post_coder.py` (cut session branch, run lint guards + test execution, commit, push, open PR), `post_doc.py` (designer commits to main), `post_maintenance.py`, `auto_merge_reviewer.py` (per-reviewer-session squash-merge of approved session PRs)
- `orchestrator/session/` — the session FSM. `state_machine.py` defines lifecycle states (incl. the "wrapping" state wired up in `04c4282`); `reconciler.py` reconciles DB session rows against Docker reality; `result_io.py` reads/writes `session_result.json`; `context_builder.py` (Phase 7) builds the pre-coder "related code" context from ARCHITECTURE.md MODULES/ENTRY POINTS/DEPRECATED to stop parallel-module drift. The agent loop is hardened against shape drift / hallucinated tool calls here (`d1bf9a9`).
- `orchestrator/cycle/` — currently just `persona.py` (per-product deterministic decision tree, called by `deploy/orchestrator/tools.run_cycle`). The sibling helpers `selection.py` / `locks.py` / `loop_detector.py` were retired together with the host-mode poller (2026-05-18); product selection and per-product mutex now live in the website / `tools.py`.
- `orchestrator/prompts/` — per-persona prompt templates (`coder.md`, `designer.md`, `reviewer.md`, `architect.md`, `planner.md`, `product_trainer.md`, etc.) + a builder (`__init__.py`) that fills in product context and injects `Pattern:` lines harvested from `session_summary.md` (capped at 10 patterns / 200 chars each to bound prompt growth). Add a new persona prompt here; do **not** inline persona text into `docker_runner.py`.
- `orchestrator/integrations/` — outbound integrations: `github_app.py` (JWT sign + installation-token minting, single chokepoint for all git auth), `github.py`, `git_ops.py` (authenticated push via one-shot credential helper, no token in `.git/config`), `docker_cli.py`.
- `orchestrator/agent_loop.py` — backend-agnostic tool-use loop extracted from `ollama_agent.py`. A `Backend` protocol turns one chat turn into an assistant message; a `ToolDispatcher` callable returns `(result_text, is_done)`. New backends (Claude API, OpenAI-compatible gateways) plug in here instead of duplicating the message/tool plumbing.
- `orchestrator/drift_detectors.py` — **deterministic Phase-1 drift scanner**, runs after each post-coder cycle (wired in `pipelines/post_coder.py`). Two registries: `_DETECTORS` (comment path — findings post as `feature_comments` with `author="drift-scanner"`, deduped against the last 24h, and surface to the next coder via `{reviewer_feedback}` injection), and `_CHORE_DETECTORS` (chore path — high-severity findings get filed as Approved chore features when the `RECONCILER_CHORES_ENABLED` env flag is on; gated by `dedupe_key` so an open chore isn't re-filed). Current detectors: `shell_artifact_files`, `design_doc_missing`, `duplicate_ddl`, `god_file`, `public_route_blanket_with_auth`. Each is a pure function `(working_dir, features) -> list[Finding]`; add new ones to the appropriate registry and a sibling test in `tests/test_drift_detectors.py`. The architect persona is the slow/reasoning sibling — these detectors are the cheap layer that runs every cycle.
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

### CI

`.github/workflows/ci.yml` runs on every push/PR to `master`/`main`:
- `pytest tests/ -v --tb=short` against a Postgres service container (`TEST_DATABASE_URL=postgresql://productfactory:ci_test_password@localhost:5432/productfactory_test`). The job currently tolerates a known band of fixture-related failures from `/home/user/...` paths that don't resolve on Windows — don't add new failing tests under that umbrella.
- `pip-audit` (transitive CVE scan) and `bandit` (Python static security) — both `continue-on-error: true` for now; surface findings, don't block.
- Trivy CRITICAL/HIGH scan of the agent image (`exit-code: 0` — report-only until baseline is clean).

Local pytest is authoritative for development, but CI catches Windows-vs-Linux path/encoding regressions that don't surface on the host. "Green locally" ≠ "green in CI."

### Prompt regression evals (`evals/`)

`evals/` is a standalone harness that catches persona-prompt quality regressions *before* a bad rollout burns sessions of compute. **Run `pytest evals/ -v` after editing anything under `orchestrator/prompts/`.** Two tiers:
- **Tier 1 — contract checks** (`test_prompt_contracts.py`, always on, no LLM): structural invariants on each built prompt — required output format present, non-negotiable constraints intact (e.g. "security auditor MUST NOT modify code"), correct working-dir paths. Catches the common drift where a refactor silently drops a security instruction.
- **Tier 2 — live LLM evals** (`test_live_evals.py`, opt-in via `RUN_LIVE_EVALS=1`): builds the persona prompt for each scenario under `evals/scenarios/`, calls the backend, applies deterministic scorers (`scoring.py`).

Baseline-vs-candidate is the main workflow: `python -m evals.runner <out.json>` on master, edit a prompt, run again, then `python -m evals.compare baseline.json candidate.json` — exits non-zero on any pass→fail regression or >1% overall score drop (a CI pre-merge gate on prompt-touching PRs). Backend selected by `EVAL_BACKEND` (`stub` replays `stub_response` fields token-free for CI self-test; `ollama` matches the production path, model from `EVAL_OLLAMA_MODEL`).

## Architecture

### Session-PR merge flow (1-PR-per-feature → main)

Every coder session opens its own session PR direct to `main`. There is no integration branch and no batch merge — features ship one PR at a time. (Migration 043 retired the sprints layer; what used to be a sprint is now just a `phases` row that groups features for UI/release-notes purposes.)

1. **Coder session**: agent edits files on the default branch; `orchestrator/pipelines/post_coder.py` cuts a fresh `coder/<session_uid>` branch off the default branch's tip (stash → checkout `-B` → stash-pop; **stash-pop conflicts block the assigned features** with a clear reason rather than force-resolving), commits with `[feature-N]` tags, pushes, and opens a **session PR** (`coder/<uid>` → `main`). The feature row's `pr_number`/`pr_url`/`branch_name` point at the session PR/branch.
2. **Reviewer session**: scoped to a single open session PR (`docker_runner._fetch_assigned_features` groups Reviewing features by `pr_number` and picks the oldest). Reviewer reviews `git log origin/main..{session_branch}` and writes per-feature decisions to `session_result.json`.
3. **Auto-merge** (per-reviewer-session in `auto_merge_reviewer.py` + per-cycle sweep in `auto_merge.py`) squash-merges every Reviewed+approved session PR directly to `main`.
4. **Release notes**: each feature carries its own `merge_notes` (set by the coder on push); the release-notes endpoint (`GET /api/phases/{id}/release-notes`) collates the `merge_notes` of all Pushed features in a phase on demand. No sprint-completion ceremony, no DoD gates.

Pre-checkout (`docker_runner.py`): reviewer lands on the session branch under review; coder/designer stay on the default branch (`_reset_workspace` left them on `main`/`master`). Designer commits design docs straight to `main` (`post_doc.py`).

Rework path: when reviewer rejects features in a session PR, `post_coder.py` detects on the next coder cycle that the rejected features share an open PR (the original session PR) and force-pushes fresh commits to its branch, preserving the reviewer's comment thread.

Retired by migration 043 (2026-05-26): the entire sprints layer — `sprints` table, `sprint_pr_mode` config flag, sprint integration branch (`sprint/<id>`) and sprint PR; `provision_sprint_pr` / `merge_sprint_pr`; `_maybe_provision_sprint_pr` / `_attempt_merge_completed_sprint_pr`; `reconcile_sprint_pr_state`; supervisor's `_check_sprint_provisioned` / `_fix_sprint_provisioned`; `check_open_pr_invariant`; the DoD machinery (`/api/sprints/{id}/sign-off`, `/api/sprints/{id}/check-dod`); the Blocked-sprint holdpen (supervisor now PATCHes `status=Blocked` directly).

### Post-coder quality pipeline

After the coder agent exits, `_run_post_coder_pipeline` (`orchestrator/pipelines/post_coder.py`) runs a sequence of gates BEFORE pushing the branch / opening the PR. Failing any gate either bounces the features back to `Implementing` with `changes_requested` or, for infra failures, rolls them back to `Approved`/`Designed` without bumping `fix_attempts`. The gates are deliberately deterministic (regex + AST + subprocess) so the reviewer never sees the patterns they catch:

**Execution layer — container vs orchestrator.** A check runs in one of two places, by a single principle: *if it EXECUTES the product's code or stack toolchain, it runs in a throwaway agent-image container (which has node/npm/npx, python/pip, go); if it only STATICALLY ANALYZES source files, it runs in the orchestrator process* (which has only Python + git). Misplacing execution in the orchestrator was the root cause of the MyCalc1 #1370 cascade — the orchestrator has no `npm`/`npx`, so for Node products the test-check died with `npm not found` and the verify-check with `npx: command not found`, both false-bouncing real code. Classification:

| Check | Kind | Runs in |
|---|---|---|
| `_post_coder_lint_check` (18 guards) | static (regex/AST/git) | orchestrator |
| `drift_detectors` | static (regex/AST) | orchestrator |
| Guard 17 deletion-safety, Guard 18 deps-coherence | static (AST) | orchestrator |
| `_post_coder_test_check` | runs `pytest`/`npm test`/`go test` | **container** |
| `_post_coder_verify_check` (`_run_verify`) | runs designer AC `Verify:` recipes (`npx`/`tsc`/…) | **container** |

Both container gates share `_agent_container_base()` (toolchain + per-product `.pf-cache/` download cache + workspace mount + `--cap-drop ALL`/non-root hardening + `CI=true` + `timeout` backstop), run as `docker run … sh -lc`, are deterministic (verdict = exit code), and need no `/workspace` symlink band-aid (the container mounts `/workspace` natively). When adding a check that runs product/stack code, route it through `_agent_container_base`, not in-process `subprocess`.

- **`_post_coder_lint_check`** (Phases 2–4) — 18 guards (1–11, 13, 14, 12, 15–18 — numbering preserved historically; Guard 12 was reordered after 14). Notable ones: hardcoded secret fallbacks in token/crypto calls (Guard 5); state-changing API routes (`POST/PUT/PATCH/DELETE`) without an auth check, opt-out via `// PUBLIC_ROUTE:` or `# PUBLIC_ROUTE:` annotation (Guard 6); agent-debris filenames — `*.bak`, `*_old_*`, `*_v\d+_*`, `*_complete_*`, `temp_fixed*`, files under `Temp/` or `temp_storage/`, `test_X_qa.py` siblings of `test_X.py` (Guard 13); config-as-gate integrity — refuses commits that lower bars declared in `quality_gates.json` by editing `pytest.ini` / `package.json` directly (Guard 14); alembic-branch detection + revision-ID coherence (Guards 15–16); **AST-diff deletion safety** (Guard 17) — parses pre/post-commit Python ASTs, takes the set difference of public top-level `def`/`class`/module-level assignments, then word-greps surviving callers across tracked `.py` files (excluding co-modified files) and bounces on dangling refs (paired with the pre-commit self-review helper `templates/check_deletion_safety.py` invoked at `AGENT_WORKFLOW.md` Step 5b, RO-mounted so the agent can't tamper with it); **deps coherence** (Guard 18) — AST-walks imports in changed `.py` files and bounces when packages used in code are missing from `requirements.txt`, catching the failure mode where the pre-baked agent image hides missing deps inside the container but a fresh `pip install -r requirements.txt && pytest` fails on collect (canonical 2026-05-22 MyDocusign incident).
- **`_post_coder_test_check`** (Phase 5) — runs the stack's test command and classifies the outcome four ways:
  - `passed`: continue to push.
  - `env_broken` (jest missing, ENOENT on node_modules, ModuleNotFoundError pytest): roll back to `Approved`/`Designed`, send operator alert, **do NOT bump `fix_attempts`** (infra failures must not auto-Block features after 5 attempts). **Consecutive-`env_broken` cap** (2026-06-04 cycle GK, `post_coder.py` ~L3526): if any assigned feature already has ≥2 prior `post-coder:test-env` comments, this round is downgraded to a regular test-failure bounce (fix_attempts bumps, coder sees the real test output). Prevents a fixture-setup `AssertionError` that merely *looks* infra-shaped from looping coder→env_broken→rollback forever until the supervisor's `rapid_flap` detector Blocks the feature with a misleading reason.
  - `collection_errors > 0`: pytest collect-only errors → bounce to `Implementing` with the first failing import quoted.
  - test failures: bounce to `Implementing` with the first failure quoted.

### Supervisor cascade detectors

`orchestrator/supervisor.py` is a rule-based detector layer called from existing hook points (post-coder pipeline, reconcile sweep, `determine_next_action`). No LLM. Every firing writes a row to `supervisor_actions` so PMs can audit it. All detectors honor `supervisor_dry_run_only` (global kill) plus their own `system_config` enable flag — flip the flag, no restart needed.

Active detectors:
- `false_success` — coder session claimed `task_done` but staged no commits / left features unaddressed
- `dirty_pr_close` — PR with merge conflicts, idle, ≥1h old → close + reset features
- `auto_plan` — active phase dead, ≥N un-phased Approved features → kick off LLM phase planner
- `overlap_pr` — multiple open PRs cover the same feature IDs → close the older
- `detect_repeated_review_feedback` — **convergent cascade**: same comment signature across N consecutive cycles → Block the feature with `blocked_reason`
- `detect_divergent_review_feedback` (Phase 6) — **divergent cascade**: reviewer flags a *different* issue each cycle while ignoring earlier ones. The 25-comment cumulative-feedback context (`_fetch_recent_review_comments` + `_format_reviewer_feedback`, limit raised 6→25 on 2026-05-07) is injected into the rework coder prompt via `{reviewer_feedback}` so the running checklist is explicit.

### Session continuity (`session_summary.md`)

`session_summary.md` is the per-product cross-session continuity doc, replacing the retired `progress.md` (2026-05-28, commit `9fa045f`). `orchestrator/docker_runner.py::_read_session_summary` reads it before each session and passes the contents into the agent prompt as `{prev_session_summary}`; on session end it's committed by `integrations/git_ops.py` so the next session on the same product sees the freshest notes. Reads are tail-keep capped (commit `b58bc8d`) so the newest notes survive the prompt-size limit. Reviewer-authored `Pattern:` lines inside it are also harvested by `orchestrator/prompts/__init__._read_reviewer_patterns` and injected separately into coder/designer prompts as a checklist (capped at 10 / 200 chars). If you're tempted to add a new long-lived continuity file, extend this one instead.

### Maintenance personas

`_MAINTENANCE_PERSONAS` in `orchestrator/docker_runner.py` is the canonical set of scheduled personas (gated per-cycle in `orchestrator/cycle/persona.py`): `documenter`, `analytics`, `recommender`, `devops`, `refactorer`, `product_trainer`, and `architect` (Phase 8 — quantitative drift detection: counts documented vs registered endpoints, detects parallel-module drift, verifies `CONFIG GATES` values match `quality_gates.json`, files chore features with `priority=25` + labels `["architecture","drift"]`; capped at 3 features per session).

### Sidecar services & infra stories (2026-06-12)

Products whose tests need a LIVE service (redis, postgres) declare it in `product.config.services`; the orchestrator provisions one container per (session, service) — `pf-svc-{session_uid}-{name}` on `productfactory-net` — at launch, injects connection env vars (`REDIS_URL`, `DATABASE_URL`) into the agent AND post-coder gate containers, and tears down after session finalize. A per-cycle reaper removes orphans. **Agents never start services** (no docker socket by design); `orchestrator/services.py::SERVICE_CATALOG` is the allowlist — agent text can *name* a service, never supply an image. Canonical incident: DogTinder #1582 (coder vendored the entire Redis source tree because its sandbox offered no other path to a live-Redis AC).

Three moving parts:
- **`feature_type='infra'` stories** (migration 046): the designer files `Provision <service> service` via the API when a story needs an undeclared catalog service, and points the real story's `depends_on` at it. After PM approval, `deploy/orchestrator/tools.py::_execute_infra_stories` implements it deterministically (catalog lookup → smoke-provision → write `product.config.services` → mark Pushed with merge_notes) — no coder/designer session ever runs one; both selection points (`cycle/persona._decide_action`, `docker_runner._fetch_assigned_features`) exclude `infra`.
- **Fakes-first designer rule** (designer.md): in-memory fakes (fakeredis/sqlite/moto/respx) are the default; a live service is the exception that needs the infra story.
- **`service_missing` triage** (post_coder.py `_detect_missing_service`): a test failure that is connection-refused against a catalog service's default port is never coder-fixable. Declared service → env_broken semantics (provisioning hiccup; no fix_attempts bump). Undeclared → features Blocked with the precise fix (file infra story or use the fake) — no bump, no coder rework loop.

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

### Phases & Features

Under the flat phases→features model (migration 043), **phases are pure UI groupings** — no DoD, no completion gate, no cap. Features own their own lifecycle and ship as their own PR (see Session-PR merge flow above). A phase is just a `phases` row (`name`, `goal`, `order`) and a parent for some number of `features` rows via `features.phase_id`.

A "Feature" in product-speak (e.g. "Contact Management") is usually a `phases` row in the DB; each individual Story under it is a `features` row that ships as one PR. The designer's sizing gate splits an oversized Story into children via `features.parent_id`.

**Auto-planning**: `POST /api/products/{id}/plan-phases` (LLM call) groups unphased `Approved`/`Designed` features into phases by theme. **Eligibility filter**: only features with `status IN ('Approved', 'Designed') AND phase_id IS NULL` are passed to the planner. Pending (PM hasn't approved), Blocked, Designing/Implementing/Reviewing/Reviewed (in-flight) are excluded.

**Blocking**: when the supervisor's repeated-feedback detector trips, it PATCHes `features.status='Blocked'` directly with a `blocked_reason`; no Blocked-sprint holdpen exists. PMs unblock by transitioning out of `Blocked`; `blocked_reason` auto-clears.

**Release notes**: `GET /api/phases/{id}/release-notes` collates the `merge_notes` field of all Pushed features in the phase. Each coder session writes `features.merge_notes` on push (set in `post_coder.py`).

### Phase gate (human-in-loop, migration 045) — ON by default

**On by default (2026-06-09):** the gate engages unless `product.config.human_gate_phases` is explicitly `False`. (The three reads — `phase_gate.gated_out_feature_ids`, `tools._run_phase_gate_detector`, `persona._decide_action` — all default unset→on.) Separately, **phase-ordered dispatch is always on**: feature selection (`docker_runner._fetch_assigned_features` + `/api/features/next-for-persona`) sorts by `(phase_order, priority)` so foundational work is built first *regardless* of the gate — phases carry a build-order that bare `priority` ignored (canonical: testingcalc #1402, a phase-2 feature at priority 50 designed ahead of phase-0 Foundation features at priority 69–79). The gate adds the *hard freeze + human approval*; phase-ordered dispatch is the *soft* foundational-first ordering. A per-phase human checkpoint:

- **State machine** on `phases.gate_state`: `open → awaiting_review → approved`. `approved` is a one-way latch only a human sets (PM Approve button → `POST /product/{id}/phase/{id}/approve`, valid only from `awaiting_review`). The detector drives `open⇄awaiting_review` to track feature reality.
- **Detector** (`deploy/orchestrator/tools.py::_run_phase_gate_detector`, run per-product each cycle): a phase whose features are all *settled* (no `Pending/Approved/Designing/Designed/Implementing/Reviewing/Reviewed`) and has a real outcome (≥1 `Pushed`, or any `Blocked`/`Reverted`) → `POST /api/phases/{id}/report` (sets `awaiting_review`) + one dashboard `alerts` row on the transition. An `awaiting_review` phase that regains active work (PM un-blocked a feature) is reopened to `open` → re-settles → re-reports. This is the **rework loop**: resolve a blocker by moving it `Blocked → Approved`; the gate stays put until the PM clicks Approve.
- **Read side — TWO enforcement points** that share `orchestrator/cycle/phase_gate.py::gated_out_feature_ids` (features in phases ordered *after* the lowest-order non-`approved` phase; empty-set no-op when the flag is off):
  1. `orchestrator/cycle/persona.py::_decide_action` excludes frozen features from the coder/designer/rework pools — stops a session being *launched for* later-phase work.
  2. `orchestrator/docker_runner.py::_fetch_assigned_features` excludes frozen features from what a launched session *claims*. This selector runs **independently** of `_decide_action` (by status + priority), so it MUST gate too — otherwise a session launched for the current phase grabs a later-phase feature, and because both sort priority ASC a low-priority-number later-phase feature would even rank first. Both points must stay in sync; that's why the logic is shared, not duplicated.
  Reviewer and the planner are intentionally **not** gated. So only the current gating phase makes progress; approving it unlocks the next.
- **Report** (`website/main.py::_build_phase_report`): deterministic facts (stats, shipped, blockers) computed in Python — including a **forward-dependency walk** that follows `feature_links` (`blocks`/`is_blocked_by`) and the `depends_on` FK from each unresolved feature into *strictly later* phases to warn which downstream features will be blocked — plus a best-effort `_llm_call` narration (summary/code_quality/challenges/recommendations). LLM failure degrades to facts-only; the gate still advances.

**Historical planning docs in the repo root** (`futureplan.md`, `futureplan_v2.md`) are SUPERSEDED — `futureplan_v2.md`'s own header (line 3) marks it superseded by migration 043. Read them for *reasoning* (sizing-cap rationale, planner output spec) but **do not** treat them as live specs; the current model is documented here and in `orchestrator/INVARIANTS.md` Vocabulary.

### Per-Product Configuration

`product.config` (JSONB column) stores per-product runtime state and overrides:
- `last_{persona}_at` — ISO timestamp used to gate scheduled maintenance personas
- `quiet_hours_start` / `quiet_hours_end` — Hour of day (0–23) to suppress sessions
- `daily_session_cap` — Max sessions per day for this product
- `max_features_per_run` — Per-product override for the global `MAX_FEATURES_PER_RUN`
- `human_gate_phases` — Human-in-loop phase gate (migration 045), **default ON** (engages unless explicitly `False`). When on, the orchestrator freezes later phases until a PM approves the current one; set `False` for fully-autonomous. Phase-ordered dispatch (foundational-first) applies regardless. See **Phase gate** below.
(Under the flat phases→features model the legacy `sprint_pr_mode` toggle and its bare-branch False branch are retired. Every coder session opens its own session PR unconditionally.)

Additionally, a `product_config.json` file in the product working directory (read by `setup_product.py` on discovery) can seed:
- `preferred_stack` — Selects which `templates/stacks/` variant to install
- `vision` — High-level product description passed to agents
- `suggested_features` — Initial feature list auto-created on discovery

### Database

- **Website runtime** uses async SQLAlchemy + asyncpg
- **Alembic migrations** use psycopg2 (sync) — driver is swapped in `db/migrations/env.py`
- PostgreSQL runs in Docker (`docker-compose.yml`); PM website connects via `productfactory-net` bridge network
- Key tables: `products`, `features`, `sessions`, `alerts`, `feature_reviews`, `phases` (no `sprints` — dropped by migration 043)
- JIRA-like tracking tables: `feature_comments` (per-feature discussion), `feature_changelog` (auto-tracked field-level audit trail), `labels` + `feature_labels` (product-scoped color tags, many-to-many), `feature_links` (directed relationships: blocks/is_blocked_by/relates_to/duplicates)
- Notable columns on `features`: `skip_design`, `design_doc`, `design_doc_path`, `review_outcome`, `review_notes`, `feature_type` (`feature | bug | chore`), `due_date`, `story_points`, `fix_attempts`, `blocked_reason`, **`phase_id`** (FK→phases; SET NULL on phase delete), **`parent_id`** (self-FK for designer-sizing splits), **`merge_notes`** (free-text, written by the coder on push; collated by `/api/phases/{id}/release-notes`)
- `phases` columns: `name`, `goal`, `order`, plus the **opt-in human-in-loop gate** (migration 045): `gate_state` (`open`→`awaiting_review`→`approved`, CHECK-constrained) and `report` (JSONB phase-summary blob). Still no `completed_at`/DoD JSONB — the gate is a lightweight state latch + denormalized report, NOT a sprints/DoD resurrection. Inert unless `product.config.human_gate_phases` is set (see Phase gate below)
- Tests use real PostgreSQL (not mocks) — each test runs inside a rolled-back transaction for isolation. **`TEST_DATABASE_URL` is required and must contain `test` in the database name.** No fallback to `DATABASE_URL`; conftest aborts the session on a banned name (`productfactory`, `postgres`) or any name without a `test` substring. Engine teardown does NOT call `drop_all` — per-test rollback is the only cleanup. Set up the test DB once with `createdb productfactory_test` then `alembic upgrade head` against it.

**Feature tracking REST endpoints** (agents and PM can call these):
- Comments: `POST/GET /api/features/{id}/comments` — author field: `pm`, `poller`, or persona name
- Changelog: `GET /api/features/{id}/changelog` — auto-populated on every status/priority/phase mutation; no manual writes needed
- Labels: `POST /api/labels`, `GET /api/products/{id}/labels`, `POST/DELETE /api/features/{id}/labels/{label_id}`
- Links: `POST/GET /api/features/{id}/links`, `DELETE /api/features/{id}/links/{link_id}`
- Search: `GET /api/features/search?q=...&product_id=...` (PostgreSQL tsvector full-text)
- Overdue: `GET /api/features/overdue` (past due_date, not Pushed/Rejected/Deferred)
- Sync: `POST /api/products/{id}/sync-features` — **deprecated no-op stub**; returns a "Disabled — DB is source of truth" payload. Kept so old bookmarks don't 404. After a DB volume wipe, restore from `backups/` via `scripts/recover_db.py`.

**Phase endpoints** (added by migration 043):
- `GET /api/phases/{id}/features` — features in this phase
- `GET /api/products/{id}/feature-tree` — full features tree, parents-first (parent_id graph)
- `GET /api/phases/{id}/release-notes` — collated `merge_notes` of Pushed features
- `POST /api/products/{id}/plan-phases` — LLM auto-plans phases from unphased Approved/Designed features (targets ~5 dependency-ordered phases, foundational-first)
- `POST /api/phases/{id}/report` — (migration 045) generate/regenerate the phase gate report and move `gate_state` to `awaiting_review`; skips an `approved` phase
- `POST /api/alerts` — create a dashboard alert row (the `alerts` table; surfaced via `GET /api/alerts/unread`)

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
- `check_deletion_safety.py` — pre-commit self-review helper the agent runs at `AGENT_WORKFLOW.md` Step 5b before committing Python deletions. Same AST-diff + caller-grep algorithm Guard 17 runs post-push; forced-choice output ("restore the symbol OR update the caller") converts a vague "review" step into a concrete fix task. RO-mounted via `_PM_CURATED_RO_FILES` so the agent can't edit it to return clean.

**Stack path convention** (2026-05-21): all stacks — `default`, `node`, `python` — now use lowercase `src/` and `tests/` (previously `SRC/`/`TestCases/` for Salesforce-style python; that was the cause of MyDocusign accumulating parallel `tests/` AND `TestCases/` on a case-sensitive container). `templates/renderer.STACK_DEFAULTS["python"]` and all python stack template text were aligned in `1f0fcba`. Existing python products with `SRC/TestCases` on disk still need a one-time `git mv` migration — the template change only affects fresh greenfield product discovery and re-rendered templates.

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
- **Read-only bind mounts for PM-curated files** (added 2026-05-20 in `b389ab1`): `_PM_CURATED_RO_FILES` in `orchestrator/docker_runner.py` lists files that are always mounted `:ro` over the parent `/workspace` mount, so agent writes to them return EROFS at the syscall level — before any lint guard runs. Files: `CLAUDE.md`, `AGENT_WORKFLOW.md`, `CONTRIBUTING.md`, `quality_gates.json`, `product_config.json`, `.gitignore`, `check_deletion_safety.py` (added 2026-05-22 alongside Guard 17 so the coder can't edit the pre-commit deletion-safety helper to return clean). `ARCHITECTURE.md` is per-persona: RW for architect (the authorized writer per `post_maintenance.py`'s `_MAINTENANCE_ALLOWLISTS["architect"]`), RO for every other persona. The post-coder denylist + post-doc/post-maintenance allowlists remain as belt-and-suspenders but are no longer the primary defense. Each per-file mount is conditional on the host file existing (Docker silently creates a directory at the source path if absent, which would corrupt the working tree on greenfield first sessions). When adding a new PM-curated artifact, extend `_PM_CURATED_RO_FILES` rather than only updating the soft denylists.

## Key Conventions

- **Async-first in website**: use `async def` + `await` for all DB and HTTP calls in `website/`
- **No hardcoded config**: everything from env vars (`.env.example` is the canonical reference)
- **Idempotent operations**: `setup_product.py` discovery is safe to run multiple times; templates only written if missing
- **Model changes require a migration**: add the column to `website/models.py` AND create a new `db/migrations/versions/NNN_*.py` file — Alembic does not auto-generate these
- **Route ordering matters**: in `website/main.py`, parameterized routes (`/api/features/{id}`) must come after all static routes at the same path prefix to avoid shadowing
- **Feature priority is ASC — LOWER number = HIGHER rank** (unified 2026-06-04, cycle GM). The PM API (`website/main.py` `ORDER BY Feature.priority`) and the orchestrator's session launcher (`docker_runner._fetch_assigned_features`) both sort `priority ASC, id ASC`. The dispatcher previously used `-priority DESC` — the inverse — so a `priority=5` chore the dashboard showed as top-of-queue was outranked by `priority=60` work in the actual session. When adding any feature-selection query, sort ASC.
- **No linter/formatter configured**: there is no ruff, black, flake8, or eslint config — code style is enforced by convention only
- **Shared requirements file**: `requirements.txt` covers both website and orchestrator (no separate dev/test requirements)

## Utility Scripts

- `scripts/seed.py` — Seeds the database with sample data for local development (`--wipe-only` to reset without seeding, `--no-sessions` to skip session history)
- `scripts/backup_db.sh` — Backs up the PostgreSQL database
- `scripts/recover_db.py` — Restores a database from backup
- `scripts/test_calculator.py` — End-to-end integration test: creates a greenfield "Calculator" product and runs a full agent cycle (scaffold → discover → coder session → GitHub PR). Use `--dry-run` to stop after scaffolding, `--persona designer` to test other personas.
- `scripts/orchestrator_drain_check.py` — **Run before any orchestrator restart.** Refuses (exit 1) if any agent session is `running`/`wrapping`; restarting mid-session orphans the post-coder pipeline (lint/test/push/PR-open runs in the orchestrator process) and strands the feature in `Implementing` for ~1 hour until `reset_stuck` fires. Canonical 2026-05-20 #626 incident. Use `--wait <seconds>` to poll-and-drain, or `--force` to override. Typical recipe: `python scripts/orchestrator_drain_check.py --wait 600 && docker compose --profile orchestrator up -d --force-recreate orchestrator`.
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
- `STALE_THRESHOLD_MINUTES` — **Retired 2026-05-28.** Was the progress.md-push staleness threshold for `orchestrator/heartbeat.py` (deleted; never wired into the containerized cycle loop). Stale-session detection is now the per-cycle watchdog in `deploy/orchestrator/tools.py` (session-heartbeat freshness + `docker ps` presence). No code reads this var anymore.
- `BROWNFIELD_FILE_THRESHOLD` — Source file count above which a product is treated as brownfield (default: 10)
- `MAX_FEATURES_PER_RUN` — Max features an agent attempts per session (default: 1; per-product override in DB)
- `RECONCILER_CHORES_ENABLED` — Opt-in (`1`/`true`/`yes`/`on`) to enable the corrective-chore sink: high-severity findings from `orchestrator/drift_detectors.py::_CHORE_DETECTORS` (currently `duplicate_ddl`) get filed as Approved chore features instead of just posted as feature comments. Default OFF. **Per-product override**: `product.config.reconciler_chores` (boolean, settable from the product Settings → Workflow card) wins over the env flag when present; absent → env default. Resolution lives in `post_coder.py`'s drift block.
(Retired by migration 043: `system_config.max_features_per_sprint` and the per-sprint feature-count cap. Under the phases→features flat model, phases are unbounded UI groupings — features have their own per-PR sizing instead.)
- `OLLAMA_HOST` — Ollama base URL (default: `http://host.docker.internal:11434` inside Docker, `http://localhost:11434` for local runs)
- `DESIGNER_MODEL` / `CODER_MODEL` — Ollama model names (defaults: `gemma3:27b` / `qwen3-coder:30b`)
- `MAX_TURNS` — Hard cap on Ollama agent turns per session (default: 80)
- `AUTH_CHECK_TIMEOUT` — Seconds for Claude CLI auth probe (default: 30)
- `ANTHROPIC_API_KEY` — Optional; used for AI feature recommendations on the greenfield product form
- `system_config.github_app_id` / `github_app_private_key` / `github_app_installation_id` / `github_org` — GitHub App credentials (DB-stored, hot-reloadable). All git auth flows through these.
- `SESSION_LOG_MAXLEN` — Max in-memory log lines buffered per session in the PM website (default: 1000)
- `PRODUCTS_BASE_DIR` — Root directory where product repos live; also used for video serving in docker-compose
