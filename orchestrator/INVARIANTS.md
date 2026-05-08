# Poller Invariants

This document is the behavioral spec for the orchestrator. Every invariant listed here is something the current code enforces — sourced from reading `poller.py`, `docker_runner.py`, `supervisor.py`, `github_client.py`, and `deploy/orchestrator/tools.py`. If a future refactor (or rewrite) breaks any of these without an explicit decision to change the behavior, that's a regression.

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

> **Two orchestrator implementations co-exist today**: `orchestrator/poller.py` (the legacy host-mode entry point invoked by `deploy/windows/start_poller.ps1`) and `deploy/orchestrator/{orchestrate,tools}.py` (the containerized entry point used by `pf-orchestrator`, currently the deployed path). The Phase 1-4 modules in `orchestrator/` (`auto_merge`, `dispatch`, `reconcile`) are wired into BOTH paths where applicable. The `dispatch.py` priority-list cascade is only used by the legacy path; `tools.py.determine_next_action` keeps its own decision tree. Consolidating the two entry points is on the future-work list.

Each invariant is tagged with **why** (the failure mode it guards against) and **how** (the function or module that enforces it). Citations are by name, not line number — line numbers drift on every refactor, and a stale citation is worse than no citation. If a citation's function gets renamed or moved, that's exactly the kind of regression this document is supposed to catch.

> **Status legend**: ✅ enforced today · ⚠ partially enforced · ❌ documented intent but currently violated (these are the fix targets).

---

## I. Distributed lock & process safety

**I.1 ✅ At most one poller per database.**
- *How*: `poller._acquire_db_lock` calls `POST /api/poller/lock`; the website handler uses a single Postgres `UPDATE WHERE` — atomic. 409 returned if another live poller holds it.
- *Why*: Two pollers running against the same DB will both pick the same product, launch duplicate Docker containers for it, and race on `session_result.json` writes. Caused real corruption pre-lock.

**I.2 ✅ A crashed poller's lock self-heals within 30 seconds.**
- *How*: `poller._heartbeat_loop` refreshes every 15 s; lock TTL is 30 s. Same-host stale-PID recovery in `_acquire_db_lock` probes the holder PID with `os.kill(pid, 0)` and force-unlocks if dead.
- *Why*: Hard crashes (OOM, SIGKILL, host reboot) leave the lock held. Without TTL + PID probe, the next poller waits indefinitely or — worse — the user manually clears it and double-runs.

**I.3 ✅ A poller whose lock was stolen mid-cycle exits, doesn't keep working.**
- *How*: `poller._heartbeat_loop` treats 404 from `/api/poller/heartbeat` as theft and sets `_hb_lock_stolen`. `poller.main` checks the flag at top of each cycle and breaks.
- *Why*: A stolen lock means another poller is already running; continuing would produce the duplicate-container scenario from I.1.

**I.4 ✅ Lock release is best-effort and never raises.**
- *How*: `poller._release_db_lock` wraps in `try/except: pass`, registered with `atexit`.
- *Why*: A failing release on shutdown must not prevent the process from exiting — TTL cleanup will recover.

---

## II. Round-robin fairness across products

**II.1 ✅ Each cycle visits one product max for an agent session.**
- *How*: `poller.main` calls `get_next_product` once per cycle, launches at most one Docker session.
- *Why*: Concurrent agent sessions per cycle would exhaust Claude API rate limits, Docker host resources, and the GitHub PR cap. The system is built around serial per-cycle work.

**II.2 ✅ `last_run_at` advances on every cycle visit, not only on session launch.**
- *How*: Cycle-visit bump after `determine_next_action` runs for a product, regardless of whether it produced a launch (`poller.main` loop). The post-success bump from `launch_session` on Docker exit code 0 is independent.
- *Why*: Without this, products that resolve to `action=exit` (no actionable work, PR-gated, etc.) keep getting picked by the round-robin and starve every other product. Real incident: pre-fix, a single PR-gated product blocked the loop for hours.

**II.3 ✅ `run_now=True` jumps the queue.**
- *How*: `poller.get_next_product` sorts `run_now=True` first, then `last_run_at ASC`.
- *Why*: PMs need an "act now" button for urgent work. Without priority override, PM requests would wait for the round-robin.

**II.4 ✅ `run_trainer_now` bypasses persona selection entirely.**
- *How*: `poller.main` checks `run_trainer_now` before round-robin and forces `persona = "product_trainer"`.
- *Why*: Showcase video generation is on-demand; running it through the normal sprint-aware flow would queue behind feature delivery.

**II.5 ✅ Reviewer work preempts everything except trainer.**
- *How*: `poller.main` calls `get_next_reviewer_product` *before* round-robin; if any product has `Reviewing` features with PRs, it runs reviewer first.
- *Why*: Reviewing is the bottleneck of the delivery pipeline. If reviewer falls behind, the sprint PR keeps growing and merge-time conflicts compound.

---

## III. Auth and external system gating

**III.1 ✅ Auth-failed cycles skip all work, don't fail loud.**
- *How*: `poller.claude_auth_healthy` runs at top of each cycle in `poller.main`; on fail, alert + `continue`.
- *Why*: Mass spurious failures across products on auth lapse would create false "feature broken" alerts. Better to halt and alert humans.

**III.2 ✅ PM API unreachable → retry with backoff, then skip cycle.**
- *How*: `poller.main` retries the per-cycle products fetch 3× with `2**attempt` backoff; on final failure, alert + `continue`.
- *Why*: PM API restarts should not crash the poller. The poller is designed to outlive the website.

**III.3 ✅ More than 1 open PR per product is anomalous and surfaces an alert.**
- *How*: `poller.main` runs `orchestrator.sprint_pr.check_open_pr_invariant` for every product after the per-cycle reconcile sweep. When `count_open_prs > 1` it calls `send_alert("warning", …)` once per product per process run (re-arms when the count drops back to ≤1).
- *Why*: In sprint-PR mode there should be exactly one open PR per product (the sprint PR). >1 means a stale orphan, a manually-opened PR, or an unmerged previous sprint PR — all worth a human look. Replaces the legacy `MAX_OPEN_PRS=3` coder gate (Phase 6.5): we no longer pause work, just alert. Pausing on the sprint PR's own existence would block every coder run forever.

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
- *How*: `poller.reset_stuck_features` calls `/api/features/reset_stuck` every cycle. Default 0.75h.
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
- *How*: `auto_merge.sweep_all` (called from `poller.main` step ⑥c) iterates every `ready` product per cycle. For each product it walks `(status=Reviewed AND pr_number AND review_outcome=approved)` features and attempts squash-merge against GitHub. The post-reviewer-session `docker_runner._auto_merge_approved` path is still in place as a belt-and-braces second layer for the reviewer-session-specific flow (it does PR `update-branch` before merge, which the sweep doesn't).
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

**VIII.4 ✅ A sprint with retro complete is force-completed via API.**
- *How*: `dispatch._decide_complete_sprint` calls `POST /api/sprints/{id}/force-complete` once retro is recorded.
- *Why*: DoD gates (qa_passed, security_clean) may be falsy for harmless reasons on legacy sprints; force-complete bypasses them once retro is in. Without this, ancient sprints never close.

**VIII.5 ✅ Features in agent states with no active session are reset.**
- *How*: `dispatch._decide_reset_orphan_agents` checks `/api/sessions/active` per product; if no live session, resets `(Designing, Implementing, Reviewing)` features in the active sprint to their prior ready state (Designed if `design_doc_path` exists, else Approved).
- *Why*: Crashed sessions outside V.1's timeout window. Belt + braces.

---

## IX. Loop detection (defensive in-memory)

**IX.1 ✅ Same persona 3× in a row triggers a loop alert (excluding expected repeaters).**
- *How*: `poller._LoopDetector.detect_loop`. Excluded set `_EXPECTED_REPEATS`: `planner, product_trainer, coder, reviewer, designer, qa_tester, security_auditor, retrospective, product_planner`.
- *Why*: Maintenance personas (documenter, analytics, refactorer, devops, recommender) shouldn't run twice in a row — that means scheduling is broken. Real incident: a config bug caused `documenter` to run every cycle.

**IX.2 ✅ Two-persona alternating loop (A-B-A-B) triggers an alert.**
- *How*: `poller._LoopDetector.detect_loop` 4-element history check.
- *Why*: A reviewer-coder ping-pong on the same feature without progress is a sign of a stuck PR or a broken contract. Catches before fix_attempts crosses threshold.

**IX.3 ✅ Loop alerts are rate-limited to 1 per 15 minutes per product.**
- *How*: `poller._LoopDetector.should_alert` cooldown dict.
- *Why*: Without rate-limit, a single loop generates dozens of duplicate Slack alerts.

---

## X. Session FSM (sessions.status)

**X.1 ✅ Session lifecycle: `pending → starting → running → wrapping → ended | killed | orphaned`.**
- *How*: `SESSION_STATUSES` enum in `website.models`.
- *Why*: A canonical FSM column means watchdog/reconciler/harvester can be written as pure transitions without parsing docker output or file mtimes.

**X.2 ✅ Watchdog kills sessions past `expected_deadline`.**
- *How*: `heartbeat.check_stale_sessions`, called from `poller.main` per cycle.
- *Why*: Default 90 min cap. Without it, runaway agents burn API quota indefinitely.

**X.3 ✅ Orphaned sessions (DB says running but container missing) are recovered on poller startup.**
- *How*: `poller._close_orphaned_sessions`, registered as a startup hook.
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
- *Why*: A supervisor bug must not take down the poller. Audit-layer crashes are silent by design.

---

## XII. Discovery and onboarding

**XII.1 ✅ Greenfield products are auto-discovered and scaffolded once.**
- *How*: `setup_product.discover_and_populate` + `greenfield_scaffold.scaffold_greenfield`, dispatched from `poller.main` for `status=greenfield_pending` and `status=registered` products.
- *Why*: PM should not need to manually `git init`, create a GitHub repo, or generate SSH keys.

**XII.2 ✅ Setup is idempotent — re-running discovery never breaks existing products.**
- *How*: Templates only written if missing; DB inserts use upsert semantics.
- *Why*: A poller restart re-runs discovery; non-idempotency would corrupt registered products.

**XII.3 ✅ `features.md` is reconciled into the DB on poller startup.**
- *How*: `poller._startup_sync_features`, called once at process boot.
- *Why*: After a DB volume wipe, the feature backlog must be recoverable from the source-of-truth file in the product repo.

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
