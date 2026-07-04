# Coder Escalation Tiers (diagnostician-gated model ladder)

**Status:** Draft / plan (branch `UI-UX-enhancements`, 2026-07-02)
**Supersedes:** the single global premium-escalation config (migration 047, `docs/blocked_escalation_plan.md`)
**Scope note:** replaces two earlier drafts — a per-persona × 4-tier × 5-platform ladder
(too broad) and a flat-column 3-tab copy (correct-but-shallow). This version keeps the
simplicity of "coder-only tiers" but fixes the two correctness landmines and subordinates
the ladder to the diagnostician. See "History of this design" at the end.

## One-paragraph summary

Give the coder a configurable **model ladder** — a first model, then up to three stronger
escalation models — but **only climb it for features the diagnose-first diagnostician has
already judged `fixable`**. Repeated coder failure is usually a spec/env defect, not a
model-capability gap (the wave-5→9 finding); so the ladder is *subordinate* to the
existing diagnostician, not a competing "throw a bigger model at it" theory. Each tier
names a `(backend, model, #attempts)`. Any tier may be Ollama (free) or a paid API
(`claude-api`/`openai`); paid tiers are bounded by a daily-USD cap. Coder-only. **No master
gate — the ladder is the coder's sole routing path** (the legacy migration-047 premium tier
is retired); behavior is preserved without config because an empty ladder runs a single
default tier built from the current `coder_model`.

> **Status (2026-07-02): Slices A + B shipped locally on branch `UI-UX-enhancements`** —
> migration 049, resolver, routing, diagnostician gate, increment/Block state machine,
> daily-cap enforcement, legacy-path removal, and the **"🧠 Coder Models" admin tab** are
> implemented and tested (full suite green, 997 passed). Only **live validation over ≥20
> shipped features** remains before push. Nothing pushed.

## Assumed backend mode

**In scope: Ollama / API production mode** (the backend-agnostic `orchestrator/agent_loop.py`
path — `AGENT_BACKEND=ollama`, plus the `AGENT_API_*` premium path). **Out of scope:
Claude-CLI mode** (`AGENT_BACKEND=claude`) — the CLI can't tool-gate, so the tier
machinery does not apply there and the coder falls back to the legacy single path.

## Core design: the ladder is gated by the diagnostician

The wave-9 diagnose-first flow already exists and is coder-only. We **reuse it as the gate**
for entering the ladder — no new decision layer:

```
Tier 0 (First Attempt) exhausts its attempt budget
        │
        ▼
  diagnose-first diagnostician runs ONCE (read-only, existing)  ──►  emits ESCALATION-DIAGNOSIS
        │
   ┌────┴───────────────┬─────────────────────┐
 spec_defect        env_impossible          fixable
   │                     │                      │
 → designer           → Blocked          → ENTER the ladder:
 (existing route)   (existing, +diagnosis) tier 1's model on the next (writeable) coder
                                           session, with the diagnosis in {reviewer_feedback};
                                           climb tier 1 → 2 → 3 automatically on continued
                                           failure; Blocked on last-enabled-tier exhaustion.
```

Consequences:
- **Frontier dollars are spent only on `fixable` features.** `spec_defect` / `env_impossible`
  features never reach a paid tier — they route exactly as they do today.
- **The diagnostician still runs once per feature** (`_has_escalation_diagnosis` gate,
  unchanged). Once a feature is proven `fixable`, subsequent tier climbs are automatic
  (we've established a stronger model is worth trying) — no re-diagnosing, no extra cost.

## Tier state: a dedicated counter, NOT `fix_attempts`

**Add `features.escalation_step` (INTEGER, default 0)** — a *cumulative* counter of failed
coder attempts on the ladder. It is advanced *only* by a ladder attempt (after the
`fixable` gate) and is **never written by the blocked re-processor.** This is the mandatory
fix for the odometer problem:

> `fix_attempts` is an overloaded control signal — the re-processor writes `fix_attempts=0`
> on divergent-review / rapid-flap→Approved (`deploy/orchestrator/tools.py:1551, 1676`) and
> `=4` on the rapid-flap code-quality path (`:1688`); `env_broken` doesn't bump it. Keying
> tiers off it would reset an escalated feature to the cheapest model on the very retry it
> earned by failing. A dedicated counter decouples the ladder from all of that.

The active tier is derived by walking the cumulative `max_attempts` of each live tier
(`orchestrator/coder_tiers.resolve_coder_tier`) — e.g. tiers [minimax×2, glm×3, opus×2]
give step windows 0–1→minimax, 2–4→glm, 5–6→opus, 7+→exhausted. So `first_attempt`'s
`max_attempts` governs how many tier-0 failures precede the diagnostician (replacing the
hard-coded `ESCALATION_FIX_ATTEMPTS_THRESHOLD` for the coder); each subsequent tier's
`max_attempts` governs failures at that tier before the next climb. Exhausting the last
enabled tier →
**`Blocked`** (with a descriptive `blocked_reason`, e.g. "coder escalation ladder exhausted:
minimax→glm→opus all failed"). We use `Blocked`, not a separate terminal state, so
operators triage ladder-exhausted features in the **normal Blocked queue** they already
manage.

**Re-processor guard (important):** the blocked re-processor gives Blocked features one
automatic retry. A ladder-exhausted feature has `escalation_step` at max, so
`resolve_coder_tier` returns `EXHAUSTED` — the re-processor must therefore **skip features
whose `escalation_step` is already exhausted** (they've had the full ladder; a re-run would
immediately re-Block). Since `escalation_step` is never reset by the re-processor, this is
a stable, non-looping terminal: the feature stays Blocked until a human changes the spec or
the tiers.

## Storage: one JSON column

```
system_config.coder_tiers  (JSONB)  -- ordered list, 1–4 entries; NULL/empty ⇒ default tier
  [
    {"backend": "ollama",     "model": "minimax-m2",      "max_attempts": 2, "enabled": true},
    {"backend": "ollama",     "model": "glm-5.2",         "max_attempts": 3, "enabled": true},
    {"backend": "claude-api", "model": "claude-opus-4-8", "max_attempts": 2, "enabled": true}
  ]
```

A JSON list (not flat columns) so the **tier count is variable** — this restores the
"maximum escalation up to 3" requirement (First Attempt + up to 3 = 4 entries) that a
fixed-column copy silently capped at 2. The daily-USD cap reuses the existing
`blocked_escalation_daily_usd_cap` column. **No master-toggle column** — the ladder is
always the coder's routing path.

**Migration 049 is behavior-preserving without a gate:** when `coder_tiers` is empty the
runtime (`_resolve_coder_ladder` in `docker_runner`) builds a single default tier
`{ollama, <current coder_model>, <ESCALATION_FIX_ATTEMPTS_THRESHOLD>, enabled}` — so the
coder runs exactly today's model with the diagnostician at the same threshold. Adding tiers
via the UI turns on climbing; no enable step.

## Modeling your selection — a tier is a self-contained `(backend, model, #attempts)`

Nothing is privileged about tier 0. Each tier independently names which backend and model;
"First Attempt = Ollama" is only the default seed. This works because
`orchestrator/agent_loop.py` is backend-agnostic — the same tool loop runs on Ollama,
Claude API, or OpenAI. Tiers are an ordered list you climb, and may run
**expensive→cheap→expensive** in any order.

**Example — OpenAI as the first attempt:**

| Tier | backend | model | #attempts |
|------|---------|-------|-----------|
| First Attempt | `openai`     | gpt-5           | 2 |
| Escalation 1  | `ollama`     | glm-…           | 3 |
| Escalation 2  | `claude-api` | claude-opus-4-8 | 2 |

At tier 0, `run_claude_in_docker` resolves the tier → sees `openai` → injects
`AGENT_API_BACKEND=openai / AGENT_API_MODEL=gpt-5 / AGENT_API_KEY=<OpenAI key>` onto the
container — the same env path escalation uses today, fired at tier 0. The container runs
the standard tool loop on `OpenAIBackend` from turn one; no Ollama for that session.

Two consequences of a **paid** first attempt (both handled below):
1. Cost governance must cover tier 0 — see "Cost & the cap".
2. **The tier's backend supersedes the global `agent_backend` for the coder.** Once a coder
   tier names a backend, that wins for the coder; non-coder personas still follow
   `agent_backend`. Note the auth split this implies: `claude-api` (API key) is a different
   credential path than `claude` CLI (OAuth token) — a coder on `claude-api` and a reviewer
   on `claude` CLI bill separately.

## Cost & the cap

One `daily_usd_cap` (existing `blocked_escalation_daily_usd_cap`) bounds spend. **It sums
every session whose *resolved tier backend is paid* (`claude-api`/`openai`) — regardless of
tier index** (not just sessions tagged "escalation"). This is the change a paid first
attempt forces; today the cap only counts `is_escalation` sessions, which would leave a
paid tier-0 session uncapped. So the cost sum / `is_escalation` tag is redriven off
*"tier backend is non-Ollama"*, not *"tier > 0"*. An all-Ollama ladder is free and never
touches the cap. Cap trip mid-day → fall back to the lowest **Ollama** tier if the ladder
has one, else the feature waits for the next UTC day (logged either way).

**Honest caveat (recommended default):** the cap is **global, not per-product**. A *paid
first attempt* means every coder session, across every product, spends on turn one — so one
product's coder churn can drain the cap and degrade every other product's coder. Reserve
paid backends for *escalation tiers* (rare — only `fixable` stuck features) and keep First
Attempt on Ollama unless you deliberately want factory-wide paid coding. Paid First Attempt
stays supported, but it is opt-in and this trade-off is why.

## UI: one new tab, no reorg of existing tabs

Add a single **"🧠 Coder Models"** sub-tab in Admin → Agent. It holds:
- the **tier list** — rows for First Attempt + up to 3 escalation tiers, each: backend
  dropdown (`ollama`/`claude-api`/`openai`) · model field · #attempts · (escalations only)
  enable checkbox · add/remove-tier controls (≤4 total),
- the **daily-USD cap** field, labelled as guarding any paid tier.

Leave the existing `⚡ Ollama · ✦ Anthropic · ◯ OpenAI · 🚀 Escalation` credential sub-tabs
and **all non-coder persona model config exactly where they are** — this feature does not
touch them. Saved by the existing "💾 Save Agent settings" button. (Merging the credential
sub-tabs into a single "Platforms" tab is a separate cosmetic change — Slice C, optional.)

## Risks & how this design handles them

| # | Risk | Resolution |
|---|------|-----------|
| R1 | `fix_attempts` overloaded → escalated features reset to tier 0 | **Dedicated `escalation_step` counter** the reprocessor never writes. |
| R2 | Per-tier container launch (Ollama vs API env) | Contained 3-way branch on the resolved tier's backend; extends the existing `AGENT_API_*` path, now reachable from any tier. |
| R3 | Migration not behavior-preserving | No gate needed: empty `coder_tiers` ⇒ runtime default single Ollama tier from `coder_model` at the existing threshold ⇒ identical to today until the operator adds tiers. |
| R4 | Paid escalation on un-fixable features (money leak) | **Diagnostician `fixable` gate** — `spec_defect`/`env_impossible` never reach a paid tier. |
| R5 | "Up to 3 escalations" dropped to 2 | **JSON list** (1–4 entries) makes tier count variable; no migration to add a level. |
| R6 | Backend-mode ambiguity / `agent_backend` split-brain | Scope stated (Ollama/API only); tier backend explicitly supersedes `agent_backend` for the coder, auth split documented. |
| R7 | Global cap → cross-product starvation with paid First Attempt | Documented; recommended default is Ollama First Attempt + paid escalations only. |
| R8 | Kill switch for a paid First Attempt | Set the First Attempt tier's backend to `ollama` (the default single tier is always Ollama); there is no separate paid-by-default path to disable. |

Remaining open item — **cap-trip consistency:** a feature started on a paid tier that
falls back to Ollama mid-rework gets inconsistent code across attempts. Accepted for v1
(rare edge, and the fallback is logged); revisit if it bites.

## Work breakdown — slices (one PR each, engine-first)

### Slice A — routing engine + storage + state machine ✅ SHIPPED LOCALLY (2026-07-02)
- **Migration 049** (validated up/down on a scratch DB): `features.escalation_step` (INT
  default 0, cumulative counter); `system_config.coder_tiers` (JSONB). No toggle column.
- **`orchestrator/coder_tiers.py`** (pure, 25 unit tests): `resolve_coder_tier` (cumulative
  step→tier), `effective_tiers`, `is_paid_backend`, `total_attempts`, `is_last_tier`,
  `validate_coder_tiers`.
- **`docker_runner`**: `_resolve_coder_ladder` (always-on for coder + default tier from
  `coder_model`); diagnostician trigger driven by `escalation_step >= tier0_budget`; tier
  model/backend selection (Ollama override / `AGENT_API_*` for paid tiers); `is_escalation`
  redriven off "tier backend is paid"; `_advance_coder_ladder` increments `escalation_step`
  on a real coder-fault bounce (fix_attempts rose, not env_broken) and **Blocks** on
  exhaustion once a diagnosis exists; `escalation_step` added to the assigned-features slim.
- **`tools.py`**: `_reprocess_blocked_features` skips ladder-exhausted features; legacy
  `_escalate_blocked_features` (migration-047 premium driver) retired to a no-op.
- **`schemas.py`**: `escalation_step` added to `FeatureUpdate`.
- Full test suite green (988 passed, 12 skipped).
- **Live exit check (pending, on the product):** a `fixable` feature climbs
  Ollama→GLM→Opus and Blocks at exhaustion; a `spec_defect` feature routes to designer
  without touching a paid tier; a reprocessor-unblocked feature keeps its `escalation_step`;
  a paid tier session counts against the cap. Validate over ≥20 shipped features before push.

### Slice B — "🧠 Coder Models" tab UI ✅ SHIPPED LOCALLY (2026-07-02)
- **`admin.html`**: new sub-tab replacing the retired "🚀 Escalation" tab — 4 fixed tier rows
  (First Attempt + 3 escalations), each backend dropdown · model · #attempts · enable
  checkbox (First Attempt always on) + the daily-USD cap field (reuses
  `blocked_escalation_daily_usd_cap`).
- **`main.py`** (`admin_save_poller_settings`): parses `coder_tier_*_{i}` into `coder_tiers`
  (empty models omitted; validated via `validate_coder_tiers` → 422 on bad input; empty ⇒
  NULL/default). Legacy `blocked_escalation_*` save lines dropped (fields retired).
- **`admin.js`**: `AGENT_SUBS` `escalation`→`coder`; cache-buster bumped.
- **Cap enforcement (gap closed in routing):** `_daily_paid_cap_ok` + `_cheapest_ollama_model`
  in `docker_runner` — a paid tier only runs when the cap is set (>0) and today's spend is
  under it; otherwise it falls back to the cheapest Ollama tier (design's cap-trip behavior).
- Tests: 6 admin save/render round-trips + cap-helper units. Full suite green (997 passed).
- **Exit check (pending):** operator configures minimax→GLM→Opus in the tab and it drives a
  live session; the cap enforces on the paid tier.

### Slice C — (optional, cosmetic) merge credential sub-tabs into a "Platforms" tab
- Purely a UI consolidation of the existing Ollama/Anthropic/OpenAI credential fields; no
  routing change. Ship only if the extra tab feels cluttered after B.

### Cross-cutting
- Tests: `tests/test_coder_escalation_tiers.py` — tier resolution & boundaries, dedicated
  counter survives a reprocessor reset, diagnostician `fixable`-gate (spec_defect/env skip
  paid tiers), cap counts any non-Ollama tier, empty-ladder = default single tier = legacy behavior.
- Docs: update `CLAUDE.md` ("Diagnose-first escalation / Premium-model routing") and this
  file as each slice lands.

## History of this design (why it looks like this)

1. **v0 vision:** per-persona ladder, up to 3 escalations, any platform (minimax→GLM→Opus).
2. **v1 draft:** per-persona × ≤4 tiers × 5 platforms × live model discovery. Cut — too
   broad; escalation is coder-only in practice, and MiniMax/GLM ride Ollama (no new
   backends needed).
3. **v2 draft:** coder-only, flat columns, 3 tabs. Correct scope but shallow — flat columns
   capped escalations at 2, and it left the `fix_attempts` odometer and the "escalate an
   un-fixable feature" money leak unaddressed.
4. **This version:** coder-only ladder, **gated by the diagnostician's `fixable` verdict**,
   on a **dedicated tier counter**, stored as a **variable-length JSON list**, behind an
   **no master gate** (empty ladder = behavior-preserving default tier), with **one new tab**. Fixes v2's two landmines and restores
   the "up to 3" requirement.
