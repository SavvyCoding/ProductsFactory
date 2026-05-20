# Poller Invariants

This document is the behavioral spec for the orchestrator. Every invariant listed here is something the current code enforces — sourced from reading `deploy/orchestrator/{orchestrate,tools}.py`, `docker_runner.py`, `supervisor.py`, and `github_client.py`. If a future refactor (or rewrite) breaks any of these without an explicit decision to change the behavior, that's a regression.

## Vocabulary — domain ↔ code mapping

The user-facing PM dashboard, persona prompts to LLMs, and external observers
use **Feature** and **Story** as the primary unit terms. The DB schema and
internal code use the legacy names **sprint** and **feature**. This is a
deliberate, documented gap — see `futureplan_v2.md` Phase 0:

| Domain term (UI, prompts, PM-speak) | Code / DB term | Role |
|---|---|---|
| **Feature** | `sprints` row | The user-facing chunk of work ("Contact Management"). Ships as one PR. |
| **Story** | `features` row | An implementation chunk ≤1 dev-day, fits one coder session. |
| Phase / Epic | `phases` row | Optional grouping of Features. |

Two consequences when reading code:
1. Anywhere code refers to "the sprint", the domain meaning is "the Feature being shipped".
2. Anywhere code refers to "a feature row", the domain meaning is "a Story within a Feature".

Branch names (`sprint/N`), API URLs (`/api/sprints/...`), and the `_sprint_branch` / `_sprint_pr_*` product fields keep the legacy names — those are internal-only identifiers; renaming would churn webhooks and external integrations for no real win. Engineers should mentally translate when reading.

---

> **Single orchestrator entry point** as of 2026-05-19: `deploy/orchestrator/orchestrate.py` calls `tools.run_cycle` (in `deploy/orchestrator/tools.py`) every ~60 s inside the `pf-orchestrator` container. The legacy host-mode `orchestrator/poller.py` + `orchestrator/dispatch.py` were retired in PR `88d8031`; the helper modules they relied on (`orchestrator/cycle/{selection,locks,loop_detector}.py`, `orchestrator/log_context.py`, `orchestrator/metrics.py`) were removed in the follow-up PR that landed this doc rewrite.

Each invariant is tagged with **why** (the failure mode it guards against) and **how** (the function or module that enforces it). Citations are by name, not line number — line numbers drift on every refactor, and a stale citation is worse than no citation. If a citation's function gets renamed or moved, that's exactly the kind of regression this document is supposed to catch.

> **Status legend**: ✅ enforced today · ⚠ partially enforced · ❌ documented intent but currently violated (these are the fix targets).

---

## I. Distributed lock & process safety

**I.1 ✅ At most one orchestrator per database.**
- *How*: `bootstrap.sh` `POST`s `/api/poller/lock` on container startup; the website handler uses a single Postgres `UPDATE WHERE` — atomic. On 409 the container exits and Docker's restart policy will retry (eventually getting through after the lock TTL expires).
- *Why*: Two orchestrators running against the same DB will both pick the same product, launch duplicate Docker containers for it, and race on `session_result.json` writes. Caused real corruption pre-lock.

**I.2 ✅ A crashed orchestrator's lock self-heals within 30 seconds.**
- *How*: `tools.poller_heartbeat` refreshes every cycle (~60 s default; cycle period in `orchestrate.CYCLE_SECONDS`); lock TTL is 30 s. The website handler clears stale rows whose `updated_at` is older than the TTL before issuing the next lock.
- *Why*: Hard crashes (OOM, SIGKILL, host reboot) leave the lock held. Without TTL, the next orchestrator waits indefinitely or — worse — the operator manually clears it and double-runs.

**I.3 ✅ An orchestrator whose lock was stolen mid-cycle exits, doesn't keep working.**
- *How*: `tools.poller_heartbeat` returns a non-success on 404 from `/api/poller/heartbeat`; the cycle loop in `orchestrate.py` detects the failed heartbeat and exits. Docker's restart policy re-launches the container, which then hits the I.1 lock contest and waits its turn.
- *Why*: A stolen lock means another orchestrator is already running; continuing would produce the duplicate-container scenario from I.1.

**I.4 ✅ Lock release is best-effort and never raises.**
- *How*: Container exit naturally drops the heartbeat; the lock's TTL handles cleanup. No explicit release call (the previous host-mode `atexit` path is gone with `poller.py`).
- *Why*: A failing release on shutdown must not prevent the process from exiting — TTL cleanup will recover.

---

## II. Round-robin fairness across products

**II.1 ✅ Each cycle visits one product max for an agent session.**
- *How*: `tools.run_cycle` resolves to exactly one `launch_session` call (or `action=exit`) per call. `orchestrate.py`'s main loop calls `run_cycle` once per `CYCLE_SECONDS` tick.
- *Why*: Concurrent agent sessions per cycle would exhaust Claude API rate limits, Docker host resources, and the GitHub PR cap. The system is built around serial per-cycle work.

**II.2 ✅ `last_run_at` advances on every cycle visit, not only on session launch.**
- *How*: `tools._bump_product_last_run(product_id)` is called from `tools.run_cycle` on every Priority-2 (round-robin) resolution — including the `action=exit` branches. Independent of whether `launch_session` actually created a container.
- *Why*: Without this, products that resolve to `action=exit` (no actionable work, PR-gated, etc.) keep getting picked by the round-robin and starve every other product. Real incident: pre-fix, a single PR-gated product blocked the loop for hours.

**II.3 ✅ `run_now=True` jumps the queue.**
- *How*: The website's `GET /api/products/next` handler sorts `run_now=True` first, then `last_run_at ASC`. `tools.run_cycle` Priority 2 just consumes that ordering.
- *Why*: PMs need an "act now" button for urgent work. Without priority override, PM requests would wait for the round-robin.

**II.4 ✅ `run_trainer_now` bypasses persona selection entirely.**
- *How*: `tools.run_cycle` Priority 0 scans `ready` products for `run_trainer_now` (and `run_persona_now`) before reviewer preemption or round-robin and forces `persona = "product_trainer"` (or the queued maintenance persona). The flag is cleared up front; on a non-error launch deferral (`already_active`/`already_launching`), it is restored so the next cycle retries.
- *Why*: Showcase video generation is on-demand; running it through the normal sprint-aware flow would queue behind feature delivery. The restore-on-deferral protects against the request being silently dropped when a coder happens to be running.

**II.5 ✅ Reviewer work preempts everything except trainer.**
- *How*: `tools.run_cycle` Priority 1 calls `/api/features/next-for-persona?persona=reviewer`. If a product has a `Reviewing` feature with a PR, the reviewer session launches before round-robin.
- *Why*: Reviewing is the bottleneck of the delivery pipeline. If reviewer falls behind, session PRs keep growing and merge-time conflicts compound.

---

## III. Auth and external system gating

**III.1 ⚠ Auth-failed cycles skip all work, don't fail loud.**
- *How*: Previously enforced by `poller.claude_auth_healthy` (host-mode poller). The containerized path does not currently run a per-cycle Claude CLI auth probe — agent containers acquire their OAuth credential at launch time via the `~/.claude` mount. A persistent auth lapse currently surfaces as repeated failed `claude` invocations inside the agent container, which bump `fix_attempts` until the feature routes to Blocked (VI.2).
- *Why*: Mass spurious failures across products on auth lapse would create false "feature broken" alerts. The `fix_attempts → Blocked` budget catches this eventually, but the loss of the upfront probe means symptoms surface as ~5 failed agent sessions instead of a single skip. Tracked as follow-up; not currently load-bearing.

**III.2 ✅ PM API unreachable → retry with backoff, then skip cycle.**
- *How*: `tools.run_cycle` fetches via `_pm("GET", "/api/products")`; on transport failure the cycle returns `action=exit` and the next tick retries. Docker's container restart policy provides the outer retry envelope if the orchestrator itself crashes on a malformed response.
- *Why*: PM API restarts should not crash the orchestrator. The orchestrator is designed to outlive the website.

**III.3 ❌ More than 1 open PR per product is anomalous and surfaces an alert.**
- *How*: Previously enforced by `orchestrator.sprint_pr.check_open_pr_invariant`, called from `poller.main`. Both `sprint_pr.py` and the legacy poller are retired (the 1-PR session-PR model on 2026-05-15 changed the semantics — sprint PRs no longer exist, so "one open PR per product" is the wrong invariant). Currently unenforced; not re-implemented because the new model produces 0–N concurrent session PRs (one per coder session) as normal behavior. Tracked as follow-up; the correct successor invariant would alert on PRs older than a threshold without a Reviewing feature pointed at them.
- *Why*: Stale orphan PRs (manually opened, abandoned mid-session, unmerged across an orchestrator restart) need a human look. The 1-PR refactor preserved this need but removed the previous implementation; new shape pending.

---

## IV. Feature state machine — safety constraints

**IV.1 ✅ A feature's status never silently downgrades.**
- *How*: `docker_runner._apply_session_entry` ranks statuses (`_PROGRESS_RANK`: Pending=0 → Pushed=7) and rejects entries whose target rank is lower than the current feature's rank.
- *Why*: Without rank-checking, a stale agent message ("Designed") can clobber a freshly-Reviewed feature, making the system regress. Real incident.

**IV.2 ✅ Two backward transitions ARE allowed: `Reviewing → Implementing` and `Reviewed → Implementing`.**
- *How*: `_ALLOWED_BACKWARD` exception set in `docker_runner._apply_session_entry`.
- *Why*: Reviewer requesting changes IS a backward move. Without the allowlist, the reviewer's `changes_requested` PATCH gets silently dropped (Implementing rank 4 < Reviewing rank 5) and the feature loops in Reviewing forever.

**IV.3 ✅ A `Reviewed` entry without `review_outcome` is rejected.**
- *How*: `docker_runner._apply_session_entry` rejection branch.
- *Why*: Auto-merge can't decide whether to merge without `review_outcome=approved`. Without rejection, malformed reviewer output silently strands features.

**IV.4 ✅ Reviewer entries with `status=Reviewing` are filtered out.**
- *How*: `docker_runner._reconcile_session_result` filter (`_is_blocked` predicate).
- *Why*: Reviewers must only write `Reviewed` or `Implementing`. Without the filter, a confused reviewer would loop the feature.

**IV.5 ✅ Coder and reviewer entries with `status=Pushed` are filtered out.**
- *How*: `docker_runner._reconcile_session_result` filter (same `_is_blocked` predicate as IV.4).
- *Why*: Only auto-merge or reconcile-against-GitHub can move a feature to Pushed. Without the filter, a coder could falsely declare success and bypass the merge gate.

**IV.6 ✅ `Reviewing` entry without `pr_number` falls back to extracting from `pr_url`.**
- *How*: `docker_runner._apply_session_entry` regex `r"/pull/(\d+)"` against `pr_url`.
- *Why*: Some reviewers wrote `pr_url` but not `pr_number`. Without the fallback, the feature would sit in Reviewing without a PR reference, never reconcilable.

---

## V. Stuck feature recovery (multiple layers)

**V.1 ✅ A feature in an agent state (Designing/Implementing/Reviewing) for >`stuck_feature_timeout_hours` is reset to its prior ready state.**
- *How*: `tools.reset_stuck_features` (called from `tools.run_cycle` step 1) `POST`s `/api/features/reset_stuck` every cycle. Default 0.75h.
- *Why*: Crashed agents leave features pinned in agent states. Without this, a single crash poisons that feature forever.

**V.2 ✅ A `claimed` feature with no PR after the session ends is rolled back.**
- *How*: `docker_runner._rollback_stuck_features` called post-session per persona-specific status set (e.g. coder → Implementing). Features WITH `pr_number` are skipped (they're already in Reviewing).
- *Why*: Session crash mid-claim must not leave the feature stranded for `stuck_feature_timeout` minutes.

**V.3 ✅ In-flight features get reconciled against GitHub every cycle.**
- *How*: `github_client.reconcile_in_flight_prs`, dispatched via `reconcile.reconcile_product` for every `ready` product. Merged → Pushed; closed-unmerged → reset (or Blocked-routed via VI.2).
- *Why*: Auto-merges happening outside the poller (manual GitHub merges, GitHub Actions, other PRs from human devs) would otherwise leave the DB out of sync.

**V.4 ✅ Closed PRs (last 20) get reconciled every cycle as a safety net.**
- *How*: `github_client.reconcile_merged_prs`, dispatched via `reconcile.reconcile_product` per product per cycle.
- *Why*: V.3 only catches features with `pr_number` set. Features whose `pr_number` was lost (DB volume wipe, agent crash before PATCH) need PR-side reconciliation to recover.

**V.5 ✅ A session_result.json with no entry for an assigned feature → that feature rolls back to Approved.**
- *How*: Enforced in `docker_runner._reconcile_session_result` + `docker_runner._rollback_stuck_features`. Agent contract documented as docstrings on `_read_session_result` and `_apply_session_entry`.
- *Why*: Crashed mid-session containers leave features claimed but uncommitted. Rollback is mandatory or they sit until V.1's timeout.

---

## VI. Fix-attempt budget & Blocked sprint route

**VI.1 ✅ `fix_attempts` is bumped on every changes-requested rework cycle, false-success detection, and killed-session recovery.**
- *How*: Bumps in (a) website on `Reviewing→Implementing` transition with `review_outcome=changes_requested`, (b) `supervisor.detect_false_success`, (c) `supervisor.detect_kill_recovery`, (d) `github_client.reconcile_in_flight_prs` on closed-unmerged PRs.
- *Why*: Without a counter, a feature that fails in a reproducible way recurs forever. The counter forces a finite budget.

**VI.2 ✅ When `fix_attempts ≥ max_fix_attempts` (default 5), the feature is routed to the per-product Blocked sprint.**
- *How*: `github_client.reconcile_in_flight_prs` calls `POST /api/products/{id}/sprints/blocked/route`. Sets `status=Blocked`, clears PR fields, sets `blocked_reason`.
- *Why*: Caps the loop. PMs see the stuck feature on the dashboard; the agent pipeline stops wasting cycles.

**VI.3 ✅ Blocked-sprint features are quarantined from agent writes.**
- *How*: PATCH guard in `website.main.api_update_feature` — non-PM PATCHes against features whose current sprint has `kind="blocked"` are rejected with 422 unless the patch changes `sprint_id` (the PM-driven exit path).
- *Why*: Without the guard, reviewers re-engage Blocked features as soon as they spot the `[feature-NN]` commit prefix on the sprint PR, and the loop resumes.

**VI.4 ✅ Blocked sprints are excluded from active-sprint selection, DoD gates, sprint capacity caps, and sprint-PR provisioning.**
- *How*: `kind="blocked"` filter in `website.main` — `_check_sprint_capacity`, `api_active_sprint`, `_get_or_create_blocked_sprint`, etc.
- *Why*: A holding pen must not affect delivery metrics — otherwise stuck features would eternally fail "all features done" and stall every sprint.

**VI.5 ✅ Features that exhaust `max_fix_attempts` get routed to the Blocked sprint regardless of whether they ever opened a PR.**
- *How*: `supervisor._route_to_blocked_if_at_cap` is called after every `fix_attempts` bump in `supervisor.detect_false_success` and `supervisor.detect_kill_recovery`. When the new value crosses `max_fix_attempts`, it POSTs to `/api/products/{id}/sprints/blocked/route` directly. Pairs with the existing route in `github_client.reconcile_in_flight_prs` which only fires for closed-unmerged PRs.
- *Why*: Without this, a feature whose coder is repeatedly killed before ever pushing a PR (e.g. Ollama agent stalls, container OOMs, watchdog timeouts) accumulates `fix_attempts` indefinitely without an escape route. The github_client's PR-state-based route never sees it because there's no PR to inspect. Real example: webcalculator bug 126 reached `fix_attempts=5` (= cap) via four kill_recovery bumps and stayed stuck in `status=Implementing` in the active sprint until a manual DB UPDATE moved it. VI.5 closes that gap so kill loops terminate at the cap as VI.2's contract intends.

---

## VII. Auto-merge

**VII.1 ✅ Auto-merge fires for any Reviewed+approved+pr_number feature with `auto_merge_enabled` set, regardless of sprint membership.**
- *How*: `auto_merge.sweep_all` (called from `tools.run_cycle` after the per-cycle reconcile pass) iterates every `ready` product per cycle. For each product it walks `(status=Reviewed AND pr_number AND review_outcome=approved)` features and attempts squash-merge against GitHub. The post-reviewer-session `docker_runner._auto_merge_approved` path is still in place as a belt-and-braces second layer for the reviewer-session-specific flow (it does PR `update-branch` before merge, which the sweep doesn't).
- *Why*: Approved PRs must move to Pushed within bounded time (1-2 cycles), or the sprint can't complete.
- *Historical context*: Pre-Phase-1 there were two paths, both bound to a session ever launching — when an active sprint had only Reviewed features (no Reviewing), neither path fired and PRs stranded. The webcalculator class of deadlock. Phase 1 added the per-cycle sweep to cut that dependency.

**VII.2 ✅ A 405 (not mergeable) response on auto-merge is logged and skipped.**
- *How*: `auto_merge.sweep_product` and `docker_runner._auto_merge_approved` both branch on `code == 405`, log warning, increment conflict counter, and continue.
- *Why*: PRs with conflicts must not crash the loop. Skip and let the PM resolve manually.

**VII.3 ✅ A 422 (already merged) response is treated as success.**
- *How*: `auto_merge.sweep_product` flips the feature to Pushed and adds the PR to `merged_pr_nums` so subsequent features pointing at the same PR auto-flip.
- *Why*: Concurrent merges (manual + auto) are normal. The DB must reflect reality, not the order of operations.

**VII.4 ✅ Auto-merge state changes are attributed to `changed_by="auto-merge"` in the changelog.**
- *How*: `auto_merge.sweep_product` includes `changed_by` in every Pushed PATCH so `feature_changelog` rows record the sweep as the author rather than the default "agent".
- *Why*: Auditability. Without explicit attribution, the audit trail can't distinguish merges done by the sweep from merges done by a reviewer session or by a human via the UI.

**VII.5 ✅ In sprint-PR mode, a sprint PR is merged ONLY when every feature in the sprint is merge-eligible.**
- *How*: `auto_merge.sweep_product` detects sprint-PR mode at sweep time by checking whether the feature's `pr_number` equals its sprint's `pr_number`. If yes, it iterates every feature in that sprint and verifies each is either terminal (Pushed/Deferred/Rejected/Reverted) or `Reviewed+approved` pointing at the same PR (`_is_merge_eligible` helper). If any feature is in a pre-shipping state (Pending/Approved/Designed/Implementing/Reviewing/Reviewed-changes-requested), the merge is held this cycle and the PR is added to `held_sprint_prs` so subsequent features pointing at the same PR don't re-check or re-call GitHub. Per-feature mode (default) never enters this branch — feature pr_number won't match sprint pr_number.
- *Why*: In sprint-PR mode the PR contains every feature in the sprint. Merging on the first `Reviewed+approved` feature would ship the whole sprint while later features are still being implemented or reviewed — a premature ship of incomplete work. The sprint PR's natural unit is the whole sprint, not the individual feature, so the merge gate must reflect that.
- *Detection is data-driven, not flag-driven*: nothing reads `config.sprint_pr_mode` at sweep time. The signal is the actual data shape (feature/sprint pr_number alignment), so the same code is correct for hybrid states (e.g. a product mid-migration with some sprint-PR-mode sprints and some per-feature-mode sprints).

---

## VIII. Sprint-aware persona selection

**VIII.1 ✅ When an active sprint exists, persona selection considers ONLY features in that sprint.**
- *How*: `dispatch.Context.sprint_features` filters `f.sprint_id == active_sprint.id`. Every active-sprint decision (`_decide_product_planner`, `_decide_coder`, `_decide_reviewer`) reads from this filtered list.
- *Why*: Without this, a product with 3 active sprints would see agents fighting for features across them. The active sprint defines current scope.

**VIII.2 ✅ Unsprinted security bugs are auto-routed into the active sprint when the `security_clean` gate is currently False.**
- *How*: `dispatch._decide_route_unsprinted_security_bugs` reads the live DoD breakdown from `GET /api/sprints/{id}/dod`. When `security_clean` is False and there are unsprinted bugs (`status in (Approved, Designed)` AND `sprint_id IS NULL`), it PATCHes them into the active sprint up to `max_features_per_sprint` capacity (excluding terminal features from the count, matching website's `_check_sprint_capacity`).
- *Why*: The legacy filter VIII.1 had no escape. Security bugs filed by `security_auditor` are typically unsprinted; they sat forever invisible to the coder.
- *Honest caveat*: This decision does NOT directly clear the gate. The website's `_evaluate_dod` recomputes `security_clean` from *sprint-bugs only* — unsprinted bugs are not part of the calculation. What this routing achieves: pulls the bugs out of invisibility so the coder can ship them; once shipped (terminal in the sprint), they count toward the gate via the recompute. Two-step unblock, not one.

**VIII.3 ✅ A sprint with all features terminal triggers retrospective before completion.**
- *How*: `dispatch._decide_complete_sprint` returns `"retrospective"` if all sprint features are in `TERMINAL = {Pushed, Deferred, Rejected, Reverted}` and `retro_doc_path` is unset.
- *Why*: Retro must run while sprint context is fresh, before the next sprint activates.

**VIII.4 ❌ A sprint with retro complete is force-completed via API.**
- *Status*: Both the dispatcher (`dispatch._decide_complete_sprint`) and the website endpoint (`POST /api/sprints/{id}/force-complete`) were retired 2026-05-19 with the dead-code sweep. The natural sprint-completion path now is the DoD check (`POST /api/sprints/{id}/check-dod`) which runs the full gate evaluation. Legacy sprints whose qa_passed/security_clean gates were retired (Phase 4, 2026-05-06) need the operator to call `/check-dod` to flip them to completed; the auto-force path is gone.
- *Why retired*: With the dispatcher and the legacy poller both deleted, the only caller of force-complete was gone, and the endpoint's purpose (bypass DoD on ancient sprints) was already moot once qa_passed/security_clean stopped blocking.

**VIII.5 ✅ Features in agent states with no active session are reset.**
- *How*: `dispatch._decide_reset_orphan_agents` checks `/api/sessions/active` per product; if no live session, resets `(Designing, Implementing, Reviewing)` features in the active sprint to their prior ready state (Designed if `design_doc_path` exists, else Approved).
- *Why*: Crashed sessions outside V.1's timeout window. Belt + braces.

---

## IX. Loop detection (withdrawn 2026-05-19)

The in-memory `_LoopDetector` that enforced IX.1–IX.3 lived only in the host-mode `poller.py`. The containerized orchestrator never had an equivalent and was the lock-holder for an extended period without the detector running, with no observed incidents traceable to its absence. IX.1–IX.3 were withdrawn with the deletion of `orchestrator/cycle/loop_detector.py`.

The active loop guard is now **Section VI**: `fix_attempts` is bumped on every changes-requested rework, false-success detection, killed-session recovery, and closed-unmerged PR; on reaching `max_fix_attempts` (default 5) the feature routes to the per-product Blocked sprint. That cap catches reviewer-coder ping-pongs in a strict-budget way that does not depend on in-memory state surviving container restarts.

If real-time persona-alternation detection ever becomes necessary again, the natural home is `tools.run_cycle` rather than back inside the orchestrator package — it needs a single-process scope, which the container model already enforces.

---

## X. Session FSM (sessions.status)

**X.1 ✅ Session lifecycle: `pending → starting → running → wrapping → ended | killed | orphaned`.**
- *How*: `SESSION_STATUSES` enum in `website.models`.
- *Why*: A canonical FSM column means watchdog/reconciler/harvester can be written as pure transitions without parsing docker output or file mtimes.

**X.2 ✅ Watchdog kills sessions past `expected_deadline`.**
- *How*: `heartbeat.check_stale_sessions`, called per cycle from `tools.check_stale_sessions` (invoked by `tools.run_cycle`).
- *Why*: Default 90 min cap. Without it, runaway agents burn API quota indefinitely.

**X.3 ✅ Orphaned sessions (DB says running but container missing) are recovered on orchestrator startup.**
- *How*: `orchestrate.startup_reconcile`, called once before the cycle loop begins. Compares live `pf-*` containers against `sessions` rows in `running`/`starting`/`pending`/`wrapping` state; closes any session whose container is not found.
- *Why*: Hard reboots leave session rows with `status=running` but no container. Without recovery, the DB shows phantom in-flight work.

**X.4 ✅ Every transition writes a `session_events` row.**
- *How*: `SessionEvent` model in `website.models` plus the website's session-update path.
- *Why*: Post-mortem of stuck sessions needs the full transition log; in-memory state alone isn't auditable.

---

## XI. Supervisor (rule-based safety net)

**XI.1 ✅ Every supervisor detector firing — even dry-run — writes one `supervisor_actions` row.**
- *How*: `supervisor._record_action`, called from every `detect_*` function before any state mutation.
- *Why*: PM dashboard surfaces what the system did automatically. Without the audit trail, "the supervisor fixed it" is unprovable.

**XI.2 ✅ Per-detector toggles in `system_config` take effect next cycle, no restart.**
- *How*: `supervisor._get_supervisor_config` re-reads `/api/system-config` on each detector call.
- *Why*: Detectors can misbehave; killing them requires fast iteration.

**XI.3 ✅ `supervisor_dry_run_only=True` is a global kill switch.**
- *How*: Every `detect_*` function in `supervisor` checks `cfg["supervisor_dry_run_only"]` and short-circuits mutations when set.
- *Why*: A bad detector deployment must be reversible without code change.

**XI.4 ✅ Detectors never raise into the orchestrator.**
- *How*: Each detector wraps its body in try/except. `_record_action` itself catches and discards exceptions so an audit-write failure can't break a detector.
- *Why*: A supervisor bug must not take down the orchestrator. Audit-layer crashes are silent by design.

---

## XII. Discovery and onboarding

**XII.1 ✅ Greenfield products are auto-discovered and scaffolded once.**
- *How*: `setup_product.discover_and_populate` + `greenfield_scaffold.scaffold_greenfield`, dispatched from `tools.run_cycle` via `tools._scaffold_greenfield_pending` for `status=greenfield_pending` and `status=registered` products.
- *Why*: PM should not need to manually `git init` or create a GitHub repo. (SSH-key generation was retired with the GitHub App migration on 2026-05-14 — all git auth now flows through installation tokens.)

**XII.2 ✅ Setup is idempotent — re-running discovery never breaks existing products.**
- *How*: Templates only written if missing; DB inserts use upsert semantics.
- *Why*: A poller restart re-runs discovery; non-idempotency would corrupt registered products.

**XII.3 ❌ `features.md` is reconciled into the DB on orchestrator startup.**
- *How*: Was previously enforced by `poller._startup_sync_features`. That call was a no-op stub by the time it was deleted in 2026-05-19, and the containerized orchestrator has no equivalent. After a DB volume wipe today, the operator must call `POST /api/products/{id}/sync-features` manually per product.
- *Why*: After a DB volume wipe, the feature backlog must be recoverable from the source-of-truth file in the product repo. The manual endpoint exists; the automatic-on-boot wiring does not. Tracked as follow-up.

---

## XIII. Failure-mode invariants (the "should never happen" list)

These are properties the system *must* hold. If you can construct a scenario where one fails, that's a bug — even if no current code path produces it.

- **❌ A feature is never silently lost between session_result.json and the DB.** *Currently violated when a session crashes mid-claim AND the live-poll thread has not yet seen the entry — V.5 catches the steady state but the transient window is unsafe.*
- **✅ A `Reviewed` feature is always either merged or PM-actioned within bounded cycles.** *Satisfied by VII.1 (per-cycle sweep) as of Phase 1 of PollerRevamp. Still depends on the feature having a valid `pr_number` — features with stale or wrong `pr_number` (e.g. webcalculator's feature 74 referencing a PR that covers different features) need data fixup, not orchestration.*
- **⚠ A product is never invisibly stuck.** *Today "No actionable work" is just a debug log line. Need a per-product `stuck_reason` surface — the next phase of work.*
- **❌ The four reconciliation layers + supervisor never produce conflicting writes.** *No transactional boundary between them; ordering is "cycle order = arbitrary." Hasn't bit yet but is a latent race.*

---

## How to use this document

1. **Refactoring**: every PR on `PollerRevamp` must list which invariants it touches. If an invariant moves to a different file, update the citation. If it's intentionally weakened, list it under "Removed invariants" in the PR description with rationale.
2. **Test spec**: each ✅ invariant should have at least one test asserting it. Today, most don't. Filling in the test coverage IS Phase 0 of the rewrite.
3. **Future failures**: when a new failure mode is discovered in production, add an invariant here *before* writing the fix. The invariant outlives the specific code path that originally enforced it.
