# ProductFactory — persona simplification plan

Captured 2026-05-06 after the all-day pipeline debug session. Constraint:
**24/7 autonomous SDLC is non-negotiable**. Goal: same autonomy, fewer
moving parts.

## Current state (post 2026-05-06 fixes)

Per-feature critical path: **planner → product_planner → designer →
coder → qa_tester → security_auditor → reviewer → retro** = 7 LLM
sessions. Each handoff is a state-machine seam. Today's bug session
proved how expensive those seams are: 7 fix commits required to make
the pipeline actually ship a feature end-to-end (rank tables, log
levels, post-coder direct PATCH, post-doc PATCH, husky bypass,
reviewer prompt, `{reviewer_feedback}` bridge).

## Target state

Per-feature critical path: **designer → coder → reviewer** = 3 LLM
sessions. Plus occasional `planner` (when product needs new ideas).
Plus a templated retro generator (no LLM).

That's **6 personas → 4 personas, 7 sessions → 3 sessions**, ~50%
fewer LLM rounds, ~70% fewer tokens per feature, half the prompt
files to maintain.

## The merges

### Merge 1 — `qa_tester` + `security_auditor` + `reviewer` → `reviewer`

All three already do the same shape of work: read the PR diff,
produce structured findings. They differ only in focus area. One
agent with a numbered prompt covering all three concerns is cheaper,
faster, and removes two handoff seams.

- **Reviewer prompt** gets three sections: (a) functional correctness
  vs `story_<id>.md` acceptance criteria, (b) test coverage and
  un-skipped tests, (c) security review (info disclosure, auth,
  secrets, injection).
- Posts **one `POST /api/features/<id>/comments` per concern category
  raised**, prefixed with the section name.
- Emits a single `review_outcome` (`approved` / `changes_requested`).
- **DoD gates collapse:** `qa_passed` and `security_clean` go away.
  The reviewer's `review_outcome` is now the only quality gate.
  Auto-merge sweep simplifies to "all features Reviewed AND
  approved → merge."
- **`security_clean=False` filing of bug features still works** — the
  merged reviewer continues to file bug features for things that
  shouldn't block the current sprint but need triage. Same `bug-routing`
  log we already have.

### Merge 2 — `planner` + `product_planner` → `planner`

Code-level audit (2026-05-06, post-pipeline-debug) revealed the
actual duplication: `product_planner` and `designer` share the SAME
`_fetch_assigned_features` candidate filter (`status == "Approved"
AND not design_doc_path`) and both write near-identical per-feature
docs to `/workspace/docs/` — just with different filenames
(`story_<NNN>.md` vs `feature_<NNN>_design.md`). `planner` is a
DIFFERENT role: it analyses the product and generates feature ideas
via the PM API; it writes no files, and runs only when the product
needs new ideas.

So the corrected merge:

- **KEEP `planner`** — distinct role, no overlap.
- **MERGE `product_planner` → `designer`.** Delete `product_planner.md`,
  use designer's prompt as the canonical per-feature doc writer.
  Remove `product_planner` from persona dispatch in
  `cycle/persona.py` (replaces it with `designer`).
  Update `_fetch_assigned_features` to drop the
  `("designer", "product_planner")` tuple to just `("designer",)`.
- Existing `docs/story_<NNN>.md` files stay valid — designer's
  prompt should accept either filename pattern when reading
  back, or do a one-time rename pass.

(The original Phase-1 draft proposed `planner + product_planner`
based on names; the actual runtime overlap was between
`product_planner` and `designer`.)

### Keep — `designer`

Standalone designer survives. Justification:

- Tight acceptance-criteria handoff to coder (`story_<id>.md`).
- Smaller models specialize better with focused prompts.
- Recovery checkpoint — design doc on `origin/main` survives a
  rolled-back coder cycle.
- Reviewer cites the design doc directly when grading work.
- Removing it is more work than keeping it.
- Tradeoff accepted: ~25% extra lead time per feature for
  determinism + recoverability.

### Replace — `retrospective` agent → templated generator

A static markdown template fills in: sprint metadata, commit log,
reviewer comment summaries, fix_attempts ratchet, DoD outcomes. No
LLM. Saves ~$0.50/sprint and a session slot. The retro stays in the
DB / docs/ as before; just generated mechanically.

## What stays unchanged

- The post-coder / post-doc git-ceremony pipelines (orchestration,
  not personas). All of today's fixes apply regardless of persona
  count.
- `coder` persona (it's the actual work).
- The auto-merge sweep, fix_attempts ratchet, blocked-sprint route,
  rank guard. These protect against agent regressions, not persona
  handoffs — they remain useful.
- The session_result.json contract (now hardened against
  cross-session pollution).
- Sprint-PR mode, the DB schema, the dashboard.

## Migration path

Sequence by ROI / risk:

1. **Merge planner + product_planner** (lowest risk, smallest blast
   radius — single prompt rewrite, persona dispatch already routes
   both the same way).
2. **Merge qa_tester + security_auditor + reviewer** (highest ROI,
   moderate prompt-engineering work; needs careful testing of the
   tri-section reviewer prompt against real PR diffs).
3. **Replace retrospective with templater** (low risk, isolated;
   delete the persona prompt + replace orchestrator's retro launcher
   with a template-fill function).
4. **Drop DoD gates that the merged reviewer subsumes** (`qa_passed`,
   `security_clean`) — DB migration + auto-merge sweep simplification.
5. **Reuse all existing infra:** `{reviewer_feedback}` bridge,
   `_format_assigned_features`, `_fetch_recent_review_comments`,
   `post_coder` / `post_doc` direct-PATCH paths, rank tables — none
   of these need to change.

## Estimated effort

- Phase 1 (planner merge): ~2 hours, one prompt file consolidation.
- Phase 2 (reviewer merge): ~6-8 hours including prompt engineering
  + test cycle on the next live sprint.
- Phase 3 (retro templater): ~3 hours.
- Phase 4 (DoD gate cleanup): ~3 hours including migration.

**Total ~2 working days** to land the simplification. Compared to
~14 hours debug today across 7 fix commits, the ROI is good — the
merges remove the seams that cause those bugs.

## Why we're keeping autonomy

User constraint reaffirmed 2026-05-06: 24/7 autonomous is
non-negotiable. This plan preserves autonomy fully. The simpler
"add a human PR review" approach (Tier 2 in earlier discussion)
would cut more code but breaks the autonomy requirement. Not pursuing.

## Today's fix commits worth preserving

All on `OrchestratorRefactor` branch:

- `df898e8` — 5 keystone bugs (rank tables sync, log levels, file IO
  robust delete, reset_stuck audit trail, post-coder direct PATCH)
- `1e1c273` — post-coder stash `-u` + `checkout -B`
- `4286822` — post-coder `--no-verify` (husky bypass)
- `78ad9fc` — post-doc stash `-u` + `checkout -B` + `--no-verify`
- `a970446` — post-doc direct PATCH → Designed
- `857d51e` — reviewer prompt: harness restrictions + PM API comments
- `96fd49a` — `{reviewer_feedback}` bridge: comments → coder prompt

These should be merged to `master` before starting the persona
simplification work, OR the simplification can branch from
`OrchestratorRefactor`. Either way they're load-bearing for the
target architecture.
