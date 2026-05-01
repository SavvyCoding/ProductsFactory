# Poller Architecture

This document covers the orchestrator subsystem: `orchestrator/poller.py`, `orchestrator/docker_runner.py`, `orchestrator/supervisor.py`, and supporting modules. For repo-wide context (data model, DoD, REST API, deployment) see the root `CLAUDE.md`.

## Orchestration Loop (poller.py)

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
12. After Docker exits: final reconcile of `session_result.json`; if `auto_merge_enabled` (system_config), reviewed PRs are merged automatically before the file is deleted (see Agent Contract below)

   Alternatively, when `USE_OLLAMA=1` is set, step 10 instead runs `orchestrator/ollama_agent.py` inside the container — a self-contained tool-use loop against Ollama's OpenAI-compatible API (no Claude API key required). Model selection: `DESIGNER_MODEL` (default `gemma3:27b`) for designer/reviewer, `CODER_MODEL` (default `qwen3-coder:30b`) for coder.

## Multi-Agent Personas

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

## Stuck Feature Self-Healing

Four layers prevent features from getting stuck:

1. **`_apply_session_entry` (real-time)**: When an agent writes a `Reviewing` entry without `pr_number`, the poller extracts it from `pr_url` automatically. Reviewer `Reviewing` entries are filtered out entirely (reviewers must not set features back to `Reviewing`).
2. **`reconcile_in_flight_prs` (every cycle)**: Checks ALL in-flight features (Implementing/Reviewing/Reviewed) with a PR reference against GitHub. Merged → Pushed immediately. Closed-unmerged → reset to Approved.
3. **`reconcile_merged_prs` (every cycle)**: Batch-reconciles the 20 most recently closed PRs. Handles both merged and closed-unmerged PRs. Parses `pr_number` from `pr_url` as fallback.
4. **`reset_stuck` (time-gated)**: Features stuck in agent states (Implementing/Designing/Reviewing) for longer than `stuck_feature_timeout_hours` (default 0.75h / 45 min, configurable as float) are reset to their prior ready state.

## Blocked Sprint Quarantine Route

Each `features.fix_attempts` is bumped on every changes-requested rework cycle, false-success detection, and killed-session recovery. When it crosses `system_config.max_fix_attempts` (default 5), `orchestrator/github_client.py` routes the feature to a per-product **Blocked sprint** (`sprints.kind = "blocked"`) for PM triage instead of letting it cycle indefinitely.

Blocked sprints are excluded from active-sprint selection, DoD gates, sprint capacity caps, and sprint-PR provisioning — they are purely a holding pen surfaced on the PM dashboard. The feature columns `fix_attempts`, `blocked_reason` and the sprint column `kind` are the persistence layer; migration `036_blocked_sprint.py` introduced them.

## Phase-1 Supervisor

`orchestrator/supervisor.py` is a rule-based safety net (no LLM) that catches stuck-state patterns the deterministic orchestrator misses. It is wired into three hook points:
- **Post-coder pipeline** (`docker_runner.py` after every coder session) — runs `detect_false_success` (exit 0 but no PR) and `detect_kill_recovery` (non-zero exit) to bump `fix_attempts` so the Blocked-sprint route triggers faster instead of waiting for `reset_stuck`.
- **Per-product reconcile sweep** (`deploy/orchestrator/tools.py::reconcile_prs`) — runs `detect_dirty_prs`, `detect_overlapping_prs`, `detect_orphan_approved`, `detect_rapid_flap`.
- **No-work cycle hook** — runs `detect_auto_plan` (active sprint dead + ≥N unsprinted Approved → call plan-sprints) and `detect_merge_stall` (sprint all-Reviewed but PR not merging for ≥1h → alert).

Every detector firing — even in dry-run — writes one row to the `supervisor_actions` audit table (`detector`, `target_type`, `target_id`, `action`, `reason`, `dry_run`, `created_at`) so the PM dashboard can show what the system has been doing automatically. Detector toggles + thresholds live in `system_config` (`supervisor_*_enabled`, `supervisor_*_min_*`, etc.) — flip them without restarting; the orchestrator picks them up next cycle. `supervisor_dry_run_only=True` is a global kill switch that lets all detectors log but skip mutations. Migrations `037_supervisor_actions.py` (audit table + 10 flags) and `038_supervisor_orphan_flap.py` (orphan_approved + rapid_flap flags) own the schema.

## Session FSM

`sessions.status` is the canonical session lifecycle state — never parse docker output or file mtimes when a session's state is in question, consult this column. Statuses: `pending` (DB row created, docker not yet started) → `starting` (docker run launched) → `running` (container live) → `wrapping` (agent exited, harvester applying results) → `ended` (clean exit_code=0) | `killed` (watchdog/timeout/SIGKILL) | `orphaned` (DB says running but no container — reconciler recovers).

Three actors drive transitions: the **watchdog** (kills stale sessions past `expected_deadline`, sets `killed` + `kill_reason`), the **reconciler** (recovers `orphaned` sessions when no container exists), and the **harvester** (applies session_result.json results in the `wrapping` phase). Every transition is appended to `session_events` (table) for audit. Related columns: `heartbeat_at`, `expected_deadline`, `kill_reason`, `log` (last N lines, capped by `SESSION_LOG_MAXLEN`).

## Agent Contract (session_result.json)

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
- When `auto_merge_enabled` is set in `system_config`, reviewer sessions trigger automatic GitHub PR merges before the final reconcile runs. PRs with conflicts are closed instead; the feature is set back to `Implementing` (clears `pr_number`).

## Conventions specific to the orchestrator

- **Sync HTTP**: `orchestrator/` runs on the Windows host with standard `httpx` (sync) calls to the PM API — async is reserved for the website.
- **`last_run_at` advances on every cycle visit**: orchestrator bumps `last_run_at` after `determine_next_action` runs for a product, regardless of whether it produced a launch — so products that resolve to `action=exit` (no actionable work, PR-gated, etc.) still cycle through the round-robin instead of starving every other product. The post-success bump from `launch_session` (on Docker exit code 0) still happens; the cycle-visit bump is defensive coverage for the no-launch paths.
- **Thread safety**: `orchestrator/alerts.py` uses a `threading.Lock` for the webhook fail counter; the poller spawns daemon threads for log streaming and live-polling `session_result.json`.
