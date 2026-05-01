# Poller Invariants

This document is the behavioral spec for the orchestrator. Every invariant listed here is something the current code enforces — sourced from reading `poller.py`, `docker_runner.py`, `supervisor.py`, and `github_client.py`. If a future refactor (or rewrite) breaks any of these without an explicit decision to change the behavior, that's a regression.

Each invariant is tagged with **why** (the failure mode it guards against) and **how** (file:line citation for the current enforcement). The "why" is the part that documentation alone cannot recover — it has to be captured before the code changes.

> **Status legend**: ✅ enforced today · ⚠ partially enforced · ❌ documented intent but currently violated (these are the fix targets).

---

## I. Distributed lock & process safety

**I.1 ✅ At most one poller per database.**
- *How*: `_acquire_db_lock` (poller.py:869) calls `POST /api/poller/lock` which uses a single Postgres `UPDATE WHERE` — atomic. 409 returned if another live poller holds it.
- *Why*: Two pollers running against the same DB will both pick the same product, launch duplicate Docker containers for it, and race on `session_result.json` writes. Caused real corruption pre-lock.

**I.2 ✅ A crashed poller's lock self-heals within 30 seconds.**
- *How*: Heartbeat thread `_heartbeat_loop` (poller.py:935) refreshes every 15 s; lock TTL is 30 s. Same-host stale-PID recovery in `_acquire_db_lock` (poller.py:892) probes the holder PID with `os.kill(pid, 0)` and force-unlocks if dead.
- *Why*: Hard crashes (OOM, SIGKILL, host reboot) leave the lock held. Without TTL + PID probe, the next poller waits indefinitely or — worse — the user manually clears it and double-runs.

**I.3 ✅ A poller whose lock was stolen mid-cycle exits, doesn't keep working.**
- *How*: `_heartbeat_loop` checks for 404 from `/api/poller/heartbeat` (poller.py:945) and sets `_hb_lock_stolen`. Main loop checks the flag at top of each cycle (poller.py:1184) and breaks.
- *Why*: A stolen lock means another poller is already running; continuing would produce the duplicate-container scenario from I.1.

**I.4 ✅ Lock release is best-effort and never raises.**
- *How*: `_release_db_lock` (poller.py:922) wraps in `try/except: pass`, registered with `atexit`.
- *Why*: A failing release on shutdown must not prevent the process from exiting — TTL cleanup will recover.

---

## II. Round-robin fairness across products

**II.1 ✅ Each cycle visits one product max for an agent session.**
- *How*: Main loop (poller.py:1273) calls `get_next_product` once, launches at most one Docker session per cycle.
- *Why*: Concurrent agent sessions per cycle would exhaust Claude API rate limits, Docker host resources, and the GitHub PR cap. The system is built around serial per-cycle work.

**II.2 ✅ `last_run_at` advances on every cycle visit, not only on session launch.**
- *How*: Documented in `POLLER.md` ("Conventions specific to the orchestrator"). Implementation: cycle-visit bump after `determine_next_action` regardless of action outcome.
- *Why*: Without this, products that resolve to `action=exit` (no actionable work, PR-gated, etc.) keep getting picked by the round-robin and starve every other product. Real incident: pre-fix, a single PR-gated product blocked the loop for hours.

**II.3 ✅ `run_now=True` jumps the queue.**
- *How*: `get_next_product` (poller.py:172) sorts `run_now=True` first, then `last_run_at ASC`.
- *Why*: PMs need an "act now" button for urgent work. Without priority override, PM requests would wait for the round-robin.

**II.4 ✅ `run_trainer_now` bypasses persona selection entirely.**
- *How*: Main loop (poller.py:1257) checks `run_trainer_now` before round-robin and forces `persona = "product_trainer"`.
- *Why*: Showcase video generation is on-demand; running it through the normal sprint-aware flow would queue behind feature delivery.

**II.5 ✅ Reviewer work preempts everything except trainer.**
- *How*: Main loop (poller.py:1255-1266) calls `get_next_reviewer_product` *before* round-robin; if any product has `Reviewing` features with PRs, it runs reviewer first.
- *Why*: Reviewing is the bottleneck of the delivery pipeline. If reviewer falls behind, PRs pile up against `MAX_OPEN_PRS` and coder gets gated everywhere.

---

## III. Auth and external system gating

**III.1 ✅ Auth-failed cycles skip all work, don't fail loud.**
- *How*: `claude_auth_healthy` (poller.py:131) at top of cycle (poller.py:1192); on fail, alert + `continue`.
- *Why*: Mass spurious failures across products on auth lapse would create false "feature broken" alerts. Better to halt and alert humans.

**III.2 ✅ PM API unreachable → retry with backoff, then skip cycle.**
- *How*: Main loop (poller.py:1200-1213) retries 3× with `2**attempt` backoff; on final failure, alert + `continue`.
- *Why*: PM API restarts should not crash the poller. The poller is designed to outlive the website.

**III.3 ✅ Coder skips a product when ≥ `MAX_OPEN_PRS` open PRs exist.**
- *How*: Documented in CLAUDE.md ("Coder: skips if ≥ MAX_OPEN_PRS"); enforced in determine_persona path before launching coder.
- *Why*: Unbounded open PRs against a single repo overwhelm reviewers and create merge-conflict storms.

---

## IV. Feature state machine — safety constraints

**IV.1 ✅ A feature's status never silently downgrades.**
- *How*: `_apply_session_entry` (docker_runner.py:274-292) ranks statuses (`Pending=0 → Pushed=7`) and rejects entries whose target rank is lower than the current feature's rank.
- *Why*: Without rank-checking, a stale agent message ("Designed") can clobber a freshly-Reviewed feature, making the system regress. Real incident.

**IV.2 ✅ Two backward transitions ARE allowed: `Reviewing → Implementing` and `Reviewed → Implementing`.**
- *How*: `_ALLOWED_BACKWARD` exception set (docker_runner.py:279).
- *Why*: Reviewer requesting changes IS a backward move. Without the allowlist, the reviewer's `changes_requested` PATCH gets silently dropped (Implementing rank 4 < Reviewing rank 5) and the feature loops in Reviewing forever.

**IV.3 ✅ A `Reviewed` entry without `review_outcome` is rejected.**
- *How*: `_apply_session_entry` (docker_runner.py:263).
- *Why*: Auto-merge can't decide whether to merge without `review_outcome=approved`. Without rejection, malformed reviewer output silently strands features.

**IV.4 ✅ Reviewer entries with `status=Reviewing` are filtered out.**
- *How*: `_reconcile_session_result` filter (docker_runner.py:387).
- *Why*: Reviewers must only write `Reviewed` or `Implementing`. Without the filter, a confused reviewer would loop the feature.

**IV.5 ✅ Coder and reviewer entries with `status=Pushed` are filtered out.**
- *How*: `_reconcile_session_result` filter (docker_runner.py:389).
- *Why*: Only auto-merge or reconcile-against-GitHub can move a feature to Pushed. Without the filter, a coder could falsely declare success and bypass the merge gate.

**IV.6 ✅ `Reviewing` entry without `pr_number` falls back to extracting from `pr_url`.**
- *How*: `_apply_session_entry` (docker_runner.py:243-246).
- *Why*: Some reviewers wrote `pr_url` but not `pr_number`. Without the fallback, the feature would sit in Reviewing without a PR reference, never reconcilable.

---

## V. Stuck feature recovery (multiple layers)

**V.1 ✅ A feature in an agent state (Designing/Implementing/Reviewing) for >`stuck_feature_timeout_hours` is reset to its prior ready state.**
- *How*: `reset_stuck_features` (poller.py:213) calls `/api/features/reset_stuck` every cycle (poller.py:1234). Default 0.75h.
- *Why*: Crashed agents leave features pinned in agent states. Without this, a single crash poisons that feature forever.

**V.2 ✅ A `claimed` feature with no PR after the session ends is rolled back.**
- *How*: `_rollback_stuck_features` (docker_runner.py:126) called post-session per persona-specific status set (e.g. coder → Implementing). Features WITH `pr_number` are skipped (they're already in Reviewing).
- *Why*: Session crash mid-claim must not leave the feature stranded for `stuck_feature_timeout` minutes.

**V.3 ✅ In-flight features get reconciled against GitHub every cycle.**
- *How*: `reconcile_in_flight_prs` (github_client.py:237) called for every `ready` product (poller.py:1252). Merged → Pushed; closed-unmerged → reset (or Blocked-routed).
- *Why*: Auto-merges happening outside the poller (manual GitHub merges, GitHub Actions, other PRs from human devs) would otherwise leave the DB out of sync.

**V.4 ✅ Closed PRs (last 20) get reconciled every cycle as a safety net.**
- *How*: `reconcile_merged_prs` called per product per cycle (poller.py:1251).
- *Why*: V.3 only catches features with `pr_number` set. Features whose `pr_number` was lost (DB volume wipe, agent crash before PATCH) need PR-side reconciliation to recover.

**V.5 ✅ A session_result.json with no entry for an assigned feature → that feature rolls back to Approved.**
- *How*: Documented in POLLER.md "Agent Contract"; enforced in `_reconcile_session_result` (docker_runner.py:368) + `_rollback_stuck_features`.
- *Why*: Crashed mid-session containers leave features claimed but uncommitted. Rollback is mandatory or they sit until V.1's timeout.

---

## VI. Fix-attempt budget & Blocked sprint route

**VI.1 ✅ `fix_attempts` is bumped on every changes-requested rework cycle, false-success detection, and killed-session recovery.**
- *How*: Bumps in (a) website on `Reviewing→Implementing` transition with `review_outcome=changes_requested`, (b) `supervisor.detect_false_success`, (c) `supervisor.detect_kill_recovery`, (d) `reconcile_in_flight_prs` on closed-unmerged PRs (github_client.py:342).
- *Why*: Without a counter, a feature that fails in a reproducible way recurs forever. The counter forces a finite budget.

**VI.2 ✅ When `fix_attempts ≥ max_fix_attempts` (default 5), the feature is routed to the per-product Blocked sprint.**
- *How*: `reconcile_in_flight_prs` (github_client.py:343) calls `POST /api/products/{id}/sprints/blocked/route`. Sets `status=Blocked`, clears PR fields, sets `blocked_reason`.
- *Why*: Caps the loop. PMs see the stuck feature on the dashboard; the agent pipeline stops wasting cycles.

**VI.3 ✅ Blocked-sprint features are quarantined from agent writes.**
- *How*: PATCH guard at website/main.py:1856-1878 — non-PM PATCHes that don't change `sprint_id` are rejected with 422.
- *Why*: Without the guard, reviewers re-engage Blocked features as soon as they spot the `[feature-NN]` commit prefix on the sprint PR, and the loop resumes.

**VI.4 ✅ Blocked sprints are excluded from active-sprint selection, DoD gates, sprint capacity caps, and sprint-PR provisioning.**
- *How*: `kind="blocked"` filter applied across website/main.py (multiple sites).
- *Why*: A holding pen must not affect delivery metrics — otherwise stuck features would eternally fail "all features done" and stall every sprint.

---

## VII. Auto-merge

**VII.1 ⚠ Auto-merge fires for any approved+open PR with `auto_merge_enabled` set.**
- *How (current, partial)*: Two paths today — `_auto_merge_approved` post-reviewer-session (docker_runner.py:414) and inline merge in `determine_persona` (poller.py:655-722).
- *Why*: Approved PRs must move to Pushed within bounded time (1-2 cycles), or the sprint can't complete.
- *Failure mode currently observed*: Both paths require `determine_persona` to either return `reviewer` or run inline merge. When the active sprint has only Reviewed features (no Reviewing), `determine_persona` returns `None` and falls through. The inline merge does fire if `auto_merge_enabled=True`, but only on the active-sprint Reviewed set — **Reviewed features in non-active sprints or unsprinted are never merged**. *(This is the webcalculator deadlock — see PHASE-1 fix in TODO list.)*
- *Target state*: Auto-merge becomes a per-cycle sweep over all `(status=Reviewed, pr_number IS NOT NULL)` features for any `ready` product, regardless of sprint. Independent of `determine_persona`.

**VII.2 ✅ A 405 (not mergeable) response on auto-merge is logged and skipped.**
- *How*: `determine_persona` inline merge (poller.py:689-690).
- *Why*: PRs with conflicts must not crash the loop. Skip and let the PM resolve manually.

**VII.3 ✅ A 422 (already merged) response is treated as success.**
- *How*: poller.py:691-694 patches the feature to Pushed.
- *Why*: Concurrent merges (manual + auto) are normal. The DB must reflect reality, not the order of operations.

---

## VIII. Sprint-aware persona selection

**VIII.1 ✅ When an active sprint exists, persona selection considers ONLY features in that sprint.**
- *How*: `determine_persona` (poller.py:601, 633-637, 640-643). Filter: `f.sprint_id == active_sprint.id`.
- *Why*: Without this, a product with 3 active sprints would see agents fighting for features across them. The active sprint defines current scope.

**VIII.2 ⚠ This means unsprinted Approved features are invisible to the coder.**
- *How (current)*: Filter at poller.py:601 has no escape.
- *Why this matters (failure mode)*: Security bugs filed as `Approved` but unsprinted (typical of `security_auditor` output) sit forever. They block the active sprint's `security_clean` DoD gate but cannot be picked up to fix them. **Webcalculator deadlock root cause.**
- *Target state*: When the active sprint's `security_clean` gate is failing AND there are unsprinted `feature_type=bug` features, those bugs become eligible for coder selection (or are auto-routed into the active sprint).

**VIII.3 ✅ A sprint with all features terminal triggers retrospective before completion.**
- *How*: `determine_persona` (poller.py:603-609) returns `"retrospective"` if all sprint features are in `{Pushed,Deferred,Rejected,Reverted}` and `retro_doc_path` is unset.
- *Why*: Retro must run while sprint context is fresh, before the next sprint activates.

**VIII.4 ✅ A sprint with retro complete is force-completed via API.**
- *How*: `determine_persona` (poller.py:611-622) calls `/api/sprints/{id}/force-complete`.
- *Why*: DoD gates (qa_passed, security_clean) may be falsy for harmless reasons on legacy sprints; force-complete bypasses them once retro is in. Without this, ancient sprints never close.

**VIII.5 ✅ Features in agent states with no active session are reset.**
- *How*: `determine_persona` (poller.py:731-742) — checks `/api/sessions/active` per product; if no live session, resets `(Designing,Implementing,Reviewing)` features in the active sprint to their prior ready state.
- *Why*: Crashed sessions outside V.1's timeout window. Belt + braces.

---

## IX. Loop detection (defensive in-memory)

**IX.1 ✅ Same persona 3× in a row triggers a loop alert (excluding expected repeaters).**
- *How*: `_LoopDetector` (poller.py:320). Excluded set: `planner, product_trainer, coder, reviewer, designer, qa_tester, security_auditor, retrospective, product_planner` (poller.py:340-345).
- *Why*: Maintenance personas (documenter, analytics, refactorer, devops, recommender) shouldn't run twice in a row — that means scheduling is broken. Real incident: a config bug caused `documenter` to run every cycle.

**IX.2 ✅ Two-persona alternating loop (A-B-A-B) triggers an alert.**
- *How*: `_LoopDetector.detect_loop` (poller.py:347-356).
- *Why*: A reviewer-coder ping-pong on the same feature without progress is a sign of a stuck PR or a broken contract. Catches before fix_attempts crosses threshold.

**IX.3 ✅ Loop alerts are rate-limited to 1 per 15 minutes per product.**
- *How*: `should_alert` cooldown (poller.py:363-369).
- *Why*: Without rate-limit, a single loop generates dozens of duplicate Slack alerts.

---

## X. Session FSM (sessions.status)

**X.1 ✅ Session lifecycle: `pending → starting → running → wrapping → ended | killed | orphaned`.**
- *How*: `SESSION_STATUSES` enum (website/models.py:156).
- *Why*: A canonical FSM column means watchdog/reconciler/harvester can be written as pure transitions without parsing docker output or file mtimes.

**X.2 ✅ Watchdog kills sessions past `expected_deadline`.**
- *How*: `check_stale_sessions` (poller.py:1231) per cycle.
- *Why*: Default 90 min cap. Without it, runaway agents burn API quota indefinitely.

**X.3 ✅ Orphaned sessions (DB says running but container missing) are recovered on poller startup.**
- *How*: `_close_orphaned_sessions` (poller.py:954) at startup.
- *Why*: Hard reboots leave session rows with `status=running` but no container. Without recovery, the DB shows phantom in-flight work.

**X.4 ✅ Every transition writes a `session_events` row.**
- *How*: SessionEvent table (website/models.py:167).
- *Why*: Post-mortem of stuck sessions needs the full transition log; in-memory state alone isn't auditable.

---

## XI. Supervisor (rule-based safety net)

**XI.1 ✅ Every supervisor detector firing — even dry-run — writes one `supervisor_actions` row.**
- *How*: `_record_action` (supervisor.py:41).
- *Why*: PM dashboard surfaces what the system did automatically. Without the audit trail, "the supervisor fixed it" is unprovable.

**XI.2 ✅ Per-detector toggles in `system_config` take effect next cycle, no restart.**
- *How*: `_get_supervisor_config` (supervisor.py:72) re-reads each call.
- *Why*: Detectors can misbehave; killing them requires fast iteration.

**XI.3 ✅ `supervisor_dry_run_only=True` is a global kill switch.**
- *How*: All detectors check this flag (e.g. supervisor.py:142).
- *Why*: A bad detector deployment must be reversible without code change.

**XI.4 ✅ Detectors never raise into the orchestrator.**
- *How*: Wrapped in try/except at every call site (`_record_action`, `detect_*`, supervisor.py:54-69).
- *Why*: A supervisor bug must not take down the poller. Audit-layer crashes are silent by design.

---

## XII. Discovery and onboarding

**XII.1 ✅ Greenfield products are auto-discovered and scaffolded once.**
- *How*: `setup_product.py` discovery + `greenfield_scaffold.py` triggered for `status=greenfield_pending` (poller.py:1217-1222).
- *Why*: PM should not need to manually `git init`, create a GitHub repo, or generate SSH keys.

**XII.2 ✅ Setup is idempotent — re-running discovery never breaks existing products.**
- *How*: Templates only written if missing; DB inserts use upsert semantics.
- *Why*: A poller restart re-runs discovery; non-idempotency would corrupt registered products.

**XII.3 ✅ `features.md` is reconciled into the DB on poller startup.**
- *How*: `_startup_sync_features` (poller.py:1003).
- *Why*: After a DB volume wipe, the feature backlog must be recoverable from the source-of-truth file in the product repo.

---

## XIII. Failure-mode invariants (the "should never happen" list)

These are properties the system *must* hold. If you can construct a scenario where one fails, that's a bug — even if no current code path produces it.

- **❌ A feature is never silently lost between session_result.json and the DB.** *Currently violated when a session crashes mid-claim AND the live-poll thread has not yet seen the entry — V.5 catches the steady state but the transient window is unsafe.*
- **⚠ A `Reviewed` feature is always either merged or PM-actioned within bounded cycles.** *Violated by VII.1 today; webcalculator is the canonical example.*
- **⚠ A product is never invisibly stuck.** *Today "No actionable work" is just a debug log line. Need a per-product `stuck_reason` surface.*
- **❌ The four reconciliation layers + supervisor never produce conflicting writes.** *No transactional boundary between them; ordering is "cycle order = arbitrary." Hasn't bit yet but is a latent race.*

---

## How to use this document

1. **Refactoring**: every PR on `PollerRevamp` must list which invariants it touches. If an invariant moves to a different file, update the citation. If it's intentionally weakened, list it under "Removed invariants" in the PR description with rationale.
2. **Test spec**: each ✅ invariant should have at least one test asserting it. Today, most don't. Filling in the test coverage IS Phase 0 of the rewrite.
3. **Future failures**: when a new failure mode is discovered in production, add an invariant here *before* writing the fix. The invariant outlives the specific code path that originally enforced it.
