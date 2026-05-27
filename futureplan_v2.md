# futureplan v2 — Feature/Story relabel (SUPERSEDED)

> ⚠️ **SUPERSEDED 2026-05-26 by migration 043 (`feat/phases-features-flat`).**
>
> This document describes the relabel-without-schema-change plan that
> preceded the actual flat-model migration. The migration that landed
> took a different, more aggressive approach: it *dropped* the `sprints`
> table entirely and made `features` rows the user-facing unit, with
> phases as pure UI groupings (no DoD, no status). The vocabulary in
> this doc — "a Feature is a `sprints` row" — is therefore historical
> only.
>
> Kept for reference because the **reasoning** about the sizing cap
> (~ "the gate that prevents #224"), the **planner output spec**, and
> the **out-of-scope** discussion remain useful when thinking about
> how the current planner prompt evolved. See `CLAUDE.md` →
> "Phases & Features" and `orchestrator/INVARIANTS.md` Vocabulary for
> the current model.

---

This is the canonical contract for the next architectural step. Read this
**before** changing any code in the planner / cap / UI / persona layers
covered by Phases 0–6.

## Why

Sprint 105 on MySalesforce shipped end-to-end (4 features auto-merged via PR #2)
proving the persona pipeline works. Sprint 106 lost 2 of 5 features (#224 and
#246) to `fix_attempts ≥ 5` auto-block — root cause was scope-per-session, not
pipeline reliability. Each "feature" the planner produced (Contacts CRUD,
Background Job Queue) was 4–6 dev-days of work; the coder couldn't fit it into
its 200-turn / 90-min session budget so each rework chipped at the surface
without addressing systemic issues (auth middleware, missing endpoints).

The fix is **smaller units of work**, but the system already does most of the
right things at the wrong level of abstraction. Sprint 105 shipped 5 things as
one PR — that's exactly the granularity humans want from a "feature". So
**relabel without restructuring**:

| Today's code term | Tomorrow's domain term | Size |
|---|---|---|
| `phases` table | Phase / Epic | Optional grouping |
| `sprints` table | **Feature** | The user-facing chunk: "Contact Management" |
| `features` table | **Story** | ≤1 dev-day, fits one coder session |

No schema migration. The existing sprint-PR merge mechanic is exactly the
"feature ships when all stories approved" mechanic we want.

## Domain ↔ code mapping (canonical)

When PMs and external users say… | The DB / code says…
---|---
"Feature" / "user-want" | a `sprints` row
"Story" / "implementation chunk" | a `features` row
"Phase" or "Epic" | a `phases` row
"Feature is shipping" | sprint PR is merging
"All stories approved → ship feature" | all sprint features Reviewed/approved → auto-merge fires
"Stories per feature" | features per sprint (capped by `max_features_per_sprint`)

Engineers reading code will keep seeing `sprint`/`feature`. Domain experts
(PMs, external observers) will see `Feature`/`Story`. The gap is documented
in `orchestrator/INVARIANTS.md` Vocabulary section.

## Hard size cap (the gate that prevents #224)

A `features` row (= Story in domain) is rejected at creation if either:

- **AC bullet count > 4** in `description` (parsed as `^- ` or `^* ` lines)
- **`files_to_create` count > 6** if the field is structured

Cap enforced at `POST /api/features` and at planner-output validation. Reject
returns 422 with the violation message; planner retries with stricter
decomposition. No way to bypass except `changed_by="pm"` (manual override
escape-hatch).

Why these numbers: typical "fits a coder session" is ~3 file additions +
~3 modifications + ≤4 acceptance criteria. Generous but enforces an upper
bound. Tunable via `system_config.max_ac_per_story` /
`max_files_per_story` (added in Phase 1).

## Planner output spec (Phase 2)

Today the planner emits a flat list of features. Tomorrow it emits one
"Feature" (= sprint) per response, containing N stories. JSON-structured for
deterministic parsing:

```json
{
  "feature_name": "Contact Management",
  "feature_goal": "Users can create, read, update, soft-delete contacts and accounts",
  "stories": [
    {
      "name": "List endpoint for /api/contacts",
      "description": "GET /api/contacts returns paginated list with filters.\n- Returns 200 with array under data\n- Supports ?limit&offset query params\n- Returns 401 when unauthenticated",
      "feature_type": "feature",
      "priority": 70,
      "files_to_create": ["src/app/api/contacts/route.ts"],
      "depends_on": null
    },
    { "name": "Detail endpoint for /api/contacts/:id", ... },
    { "name": "Auth middleware on contact routes", "depends_on": "List endpoint for /api/contacts" }
  ]
}
```

The orchestrator parses this, creates a sprint row + N feature rows in one
transaction. Each feature row gets `sprint_id` set at creation time.

## What stays the same

- Designer pipeline (one design doc per `feature` row = one per Story).
- Coder/reviewer/post_coder/post_doc behavior.
- Auto-merge sweep — already gates per-sprint (= per-Feature).
- Sprint PR mechanic — already squashes per-sprint (= per-Feature).
- `phases` grouping — Phase contains multiple Features.

## What changes (Phases 0–6 scope)

- Phase 0 (this doc + INVARIANTS): vocabulary nailed down.
- Phase 1: size cap on `features` rows.
- Phase 2: planner rewrite to emit feature+stories tree atomically.
- Phase 3: UI strings flip Sprint→Feature, Feature→Story.
- Phase 4: persona prompts flip "feature"→"story" where they mean the work unit.
- Phase 5: end-to-end validation on a fresh greenfield.
- Phase 6: cutover for new products via `product.config.story_mode = true`.

## Out of scope

- Schema rename (`sprints`→`features`, etc.) — too costly for the win, and
  the vocabulary gap is tolerable when documented.
- Cross-feature story deferral ("story 1b ships in next Feature") — current
  one-Feature-at-a-time model has no need; revisit if it becomes a constraint.
- Renaming `sprint/N` branches to `feature/N` — internal-only identifier.
- Backfilling existing products' features into the new model — grandfather
  them; the new model only applies to new sprints.

## Open questions

- Should the planner produce one Feature per call, or fill the entire phase's
  worth of Features in one shot? Phase 2 implements one-per-call (matches
  current planner cadence); revisit if planner LLM is too slow.
- If a Story hits `fix_attempts=5` auto-block, does the parent Feature also
  block, or does the Feature ship with the bad Story marked as Deferred?
  Phase 2 keeps current behavior (sprint-PR merges with all non-Blocked
  features included). Revisit after Phase 5 data.
- `max_features_per_sprint` (= max_stories_per_feature) currently 5. Is that
  right for the new model? Probably leave unchanged; adjust based on Phase 5
  observations.
