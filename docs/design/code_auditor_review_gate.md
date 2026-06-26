# Code Auditor — Phase-Boundary Review Gate

**Status:** Comment-only soak SHIPPED 2026-06-24 · **filing promotion Increment 1 SHIPPED 2026-06-25** (behind `CODE_AUDITOR_FILING_ENABLED`, default OFF) · verifier + blocker-gate (Increment 2) and semantic dedup + migration (Increment 3) deferred · **Scope:** orchestrator (website/migration at Increment 3)

## Implementation status

**Shipped — comment-only soak, behind `CODE_AUDITOR_ENABLED` (default OFF):**
- Read-only `code_auditor` persona (§4), **3 semantic dimensions** — security · correctness · tests (§7); architecture/dedup deferred to the `architect` persona.
- Trigger: phase-boundary checkpoint + architect-run cadence (`CODE_AUDIT_ARCHITECT_RUNS`, default 1 during soak) (§5).
- Output: dashboard **alerts** (`POST /api/alerts`); files no bugs, no preemption.
- Tracked in `product.config` counters — **no migration**.

**The filing promotion ships in three increments** so each is independently soak-able:

- **Increment 1 — filing (SHIPPED 2026-06-25, behind `CODE_AUDITOR_FILING_ENABLED`, default OFF).** When ON, the audit files each surviving finding as a `bug` feature (`POST /api/features`) instead of an alert, **severity-routed by priority** (Critical → 1, High → 5, Medium → 20, Low → 40). Findings land **`status=Pending`** (FeatureCreate default), so the PM triages before any coder session runs — a false positive costs one rejection, never a wasted session. **No stop-the-line / preemption / migration yet**; Critical bugs at priority 1 naturally sort to the front of phase-ordered dispatch (a *soft* stop-the-line). The output sink is a deterministic builder-side switch (`{code_auditor_output_steps}` → filing block vs alert block in `orchestrator/prompts/__init__.py`), not an agent branch, so a local model can't do the wrong half. Regression: `tests/test_code_auditor_filing.py`.
- **Increment 2 — verifier session + deterministic blocker gate (§8, §10):** an independent verifier re-checks each Critical/High before it can preempt; passing findings graduate to auto-`Approved` + the current gating phase (hard stop-the-line); `open Critical/High == 0` governs phase advance. Medium/Low → standing **Hardening lane**.
- **Increment 3 — semantic dedup + `phases.review_state` migration (§9, §10):** embed-and-cosine dedup against open + recently-closed code-review bugs (prompt-level name dedup is the Increment-1 stopgap); the migration backs the blocker gate's persisted state.

## 1. Motivation

The per-PR `reviewer` and the deterministic drift detectors are *structurally* unable to catch a class of defects that only a **whole-product, semantic** pass finds. The 2026-06-23 IndianFoodTruck review surfaced ~10 of them on shipped `main`:

- **Cross-cutting accretion** the per-PR reviewer can't see: 4 parallel Stripe modules accreted across many PRs; a committed `menu.db`; dual `payment_intent_id` / `stripe_payment_intent_id` columns.
- **Semantic correctness/security** the pattern-matching drift detectors don't reason about: IDOR on `orders/[id].ts`, email-spoofed order ownership, the payment-intent-never-persisted bug spanning `submitOrder` + webhook + schema.

The per-PR reviewer sees **one session diff**; the drift detectors are **pattern matchers**. Nothing runs a scheduled, multi-dimensional, semantic review over the whole product. `security_auditor` is the closest persona but is (a) on-demand only and (b) security-only.

## 2. Goals / Non-goals

**Goals**
- A scheduled, whole-product review at **phase boundaries** that files verified findings as bugs.
- Stop-the-line on **Critical/High** only; non-blockers accumulate in a separate lane.
- A **deterministic** exit criterion (auditable, not emergent).
- Bounded cost + bounded noise (churn floor, dedup, caps, soak).

**Non-goals**
- Not a replacement for the per-PR reviewer or drift detectors (it's the periodic whole-product layer above them).
- Not a code-*writing* persona — read-only; it files findings, the normal coder fixes them.

## 3. Overview

A new **read-only `code_auditor` persona** runs at a phase boundary, **after every 2 architect runs** (so it rides a fresh `architect` pass). It reviews the product *with whole-product context*, across **three semantic dimensions** (security · correctness · tests — architecture/drift is the architect's job), self-verifying each finding before reporting. In the **comment-only soak** it raises findings as dashboard alerts. At the **filing promotion**: Critical/High findings file as top-priority `bug` features into the current gating phase (stop-the-line); Medium/Low file into a standing **Hardening lane**; a **deterministic blocker gate** (`open Critical/High == 0`) governs phase advance; **semantic dedup** prevents re-filing.

## 4. The persona

- New prompt `orchestrator/prompts/code_auditor.md`.
- **Read-only**: add `code_auditor` to `_READONLY_PERSONAS` in `ollama_agent.py` (write_file + git-mutating bash blocked at the tool layer, same as `security_auditor`). It skips the commit pipeline.
- **Not** in `_MAINTENANCE_PERSONAS`, **not** on-demand. New category: **phase-triggered**.
- Reviews `reviewed_sha..HEAD` (the phase's churn) **with whole-product context injected** (ARCHITECTURE.md MODULES + file tree) so cross-cutting accretion is visible even on a delta-focused review.

## 5. Trigger — phase-boundary checkpoint + 2-architect-run cadence (runs for ALL products)

`_run_phase_review_gate(product, features)` in `deploy/orchestrator/tools.py`, called in the per-product loop **after the architect scheduler** (so a cycle that queues the architect lets it go first; the audit follows in a later cycle). Runs for ALL products — **not** gated behind `human_gate_phases`, like `reap_empty_phases`.

```
if not code_auditor_enabled(product): return            # opt-in, default OFF
if product.run_persona_now: return                      # don't stomp a queued session
if no settled phase with >=1 Pushed: return             # phase-boundary checkpoint
if architect_run_count - architect_runs_at_last_audit < CODE_AUDIT_ARCHITECT_RUNS: return  # env, default 1 soak / 2 steady
queue run_persona_now = "code_auditor"; architect_runs_at_last_audit = architect_run_count
```

**Why architect-run count, not LOC/feature churn:** the architect runs every ~3 features Pushed (`_check_architect_due`, default N=3), refreshing `ARCHITECTURE.md` and filing drift chores each time. Riding 1-audit-per-2-architect-runs (≈ every 6 features) means the semantic pass *always* reads the freshest structural map and dedups against architect's just-filed chores. `architect_run_count` is incremented at the architect-launch chokepoint (`_check_architect_due`, where the cadence counters already advance at queue time); the audit tracks `architect_runs_at_last_audit`. **Intentional tight coupling:** if the architect were disabled the audit would not fire. Small phases batch naturally (2 runs span several phases). No migration — both counters live in `product.config`.

## 6. State machine & schema

Migration adds to `phases`:
- `review_state` — `none → reviewing → (clean | blocked)` (CHECK-constrained, mirrors `gate_state`).
- `reviewed_sha` — last-reviewed commit; the churn-floor + re-review-dedup anchor.

`reviewing → clean` when the run finds no Critical/High (or all are later closed); `reviewing → blocked` while open Critical/High code-review bugs exist for the phase.

## 7. Review mechanism

One `code_auditor` session, structured across **three SEMANTIC dimensions**:
**security · correctness · tests.** Architecture/duplication is **out of scope** — the
`architect` persona (every ~3 features) and the deterministic drift detectors already own
structural drift and log it; the auditor dedups against their output rather than re-deriving it.

- For each candidate finding, the prompt requires a **refute-before-file** step (state why it might be a false positive; file only if it survives) — the single-session analog of adversarial verification.
- Each finding carries: `dimension`, `severity` (Critical/High/Medium/Low), `file:line`, `essence` (1-line), `fix`, and a `dedupe_key = "<normalized-file>:<finding-class>"`.
- **Upgrade path — now REQUIRED for filing (per the soak triage, not optional):** a dedicated **verifier session** that independently (a) confirms each Critical/High is real before it can preempt **and (b) normalizes its severity**. The soak showed the *same* bug drawing different severities across runs (High↔Critical↔Medium↔Low), and severity is exactly what drives the stop-the-line-vs-Hardening-lane split — so the verifier must pin severity (independent re-rate, or max/median over N runs, or a deterministic rubric), not just confirm reality. Self-critique alone is insufficient: both hard FPs across the soak were self-critiqued findings (a reachability-trace High, and a "correct-Stripe-pattern" Medium).

## 8. Filing & severity routing

| Severity | Lane | Mechanics |
|---|---|---|
| **Critical / High** | **Current gating phase**, top priority | `feature_type='bug'`, priority below the phase's current min, labels `['code-review','blocker']`, **cap 3 / phase**. Stop-the-line. |
| **Medium / Low** | **Hardening lane** | `feature_type='bug'`, labels `['code-review','tech-debt']`, **cap 8 / run**. Non-blocking. |

**Hardening lane** = a standing per-product phase `"Hardening & Tech Debt"`, created lazily, placed **last in phase order** so phase-ordered dispatch (`(phase_order, priority)` ASC) works it only when the feature phases drain — present, worked, never blocking. Add a **ceiling** (e.g. `max_open_tech_debt = 30`/product): at the cap, file only net-new higher-severity items and drop the rest with a `log()` (no silent truncation).

Reuses the `security_auditor` PM-API filing machinery (read-only persona files `bug` features, caps, labels).

## 9. Semantic dedup (borrowed from ruflo's vector memory, right-sized)

Exact `(file:class)` keys miss findings that are the **same bug phrased differently** or that moved to a new line/file. ruflo solves redundant work with an HNSW vector store; at our scale (tens of code-review bugs/product) **HNSW is overkill** — brute-force cosine is correct and simpler.

- **Embed** each finding's text (`title + normalized-location + essence`) at file time. Embeddings via the agent image's SDK (`anthropic`/`openai`) or a local `ollama` embedding model (`nomic-embed-text`); chosen by config, same pattern as `DESIGNER_MODEL`/`CODER_MODEL`.
- **Store** in a new table `code_review_findings(feature_id, product_id, phase_id, dedupe_key, severity, embedding float8[], status, created_at)` — embedding as a plain array column (**pgvector is not installed**; not needed at this N).
- **Dedup check before filing** (two-pass):
  1. Cheap exact `(file:class)` match against open code-review bugs → skip.
  2. **Semantic**: pull embeddings of *open* + *recently-closed (30d)* code-review bugs for the product (small N), brute-force cosine:
     - `max_sim(open) ≥ τ_dup (~0.88)` → **skip** (duplicate of an open bug).
     - `max_sim(closed) ≥ τ_regress (~0.90)` → file as **regression/reopened** (the fix didn't hold), escalate one severity band.
     - else → **new** finding.

This keeps the every-phase loop from re-filing the same 10 bugs, and turns "we shipped a fix that regressed" into an explicit signal.

**Soak-confirmed essential (2026-06-24):** with no dedup, the cadence=1 *second* round re-raised the first round's findings as fresh alerts (nothing was fixed in between, since the soak files nothing). Dedup is therefore a **hard prerequisite for filing**, not an optimization — without it every architect run refiles the standing backlog.

## 10. Deterministic blocker gate (our differentiator vs swarm systems)

ruflo and peers have **no explicit exit criteria** — quality is emergent from trajectory/SONA learning (per their own docs). ProductFactory's identity is *deterministic* gates, so the exit criterion here is an **integer, not a judgment**:

```
phase_blockers_open(phase) =
  count(features f
        where f.feature_type='bug'
          and {'code-review','blocker'} ⊆ labels(f)
          and f belongs to / targets phase
          and f.status not in ('Pushed','Rejected','Deferred'))
```

**Advance rule:** a phase may move past review only when `phase_blockers_open == 0` (`review_state='clean'`), or it was churn-floor-skipped (`review_state` stayed `none`). **No LLM sits in the gate decision** — the auditor *produces* findings; the *gate* is a COUNT, auditable in one SQL query.

- **Human-gate products** (`human_gate_phases=true`): the phase-gate report includes `review_state`; the phase cannot reach `awaiting_review`/`approved` while `review_state='blocked'`.
- **Autonomous products** (`human_gate_phases=false`, e.g. IndianFoodTruck): no latch exists, so the blocker bugs (top priority, current phase) **preempt the next coder session** via phase-ordered dispatch. When the count hits 0, `review_state → 'clean'` and normal work resumes. Same stop-the-line outcome, no human in the loop.

This is the thing we do that the swarm crowd doesn't: a hard, auditable "Critical/High = 0 before advance."

## 11. Rollout — soak first (drift-detector / Guard-17 protocol)

Ship **comment-only**: findings post as dashboard **alerts** (`POST /api/alerts`, `[code-audit]` prefix) — no bug filing, no preemption. Promote to filing once the soak's precision is validated. Gated by `CODE_AUDITOR_ENABLED` (global) + `product.config.code_auditor` (per-product opt-out). Soak cadence is env-tunable via `CODE_AUDIT_ARCHITECT_RUNS` (default **1** = one audit per architect run, for max precision-sample density; steady-state target 2).

**First-run triage (2026-06-24).** First 2 audits (IndianFoodTruck, HomeChoreService) raised 6 findings each. Adversarial verification against the code: **10/12 hard TP (83 %), 11/12 real (92 %), 1 hard FP**. Both IFT High findings independently re-found the manual-review bugs (payment-never-persisted, admin-page-404). The single hard FP was a **High** — a "state unreachable via the API" claim refuted only by a non-obvious `assign → webhook` reachability path that the in-prompt self-critique missed. That is the **empirical case for gating the Critical/High stop-the-line behind an INDEPENDENT verifier session** at the promotion (§7 upgrade path), not self-critique; Medium/Low (Hardening lane, non-blocking) can stay self-critique. One stray `[code-audit][probe]` line — negligible noise (suppress in the prompt).

**Second round (2026-06-24, cadence=1).** A second audit of the same two products raised 14 findings; triaging just the **8 net-new** ones → **5 TP, 2 PARTIAL, 1 FP**. Two key learnings, now hardened into requirements above:
1. **Recall grows across passes** — round 2 found High-value TPs round 1 missed (a payout **double-pay**, a **2FA-secret-rotation** bypass, the `orders/[id].ts` IDOR). One pass is not exhaustive; the architect-cadence repetition is a feature.
2. **Severity is non-deterministic across runs** for the *same* bug (High↔Critical↔Medium↔Low). Both this round's non-TPs were *overstated severity/impact*, not phantom bugs. → the verifier session must **normalize severity** (§7), and **dedup** must collapse the re-raises (§9). Aggregate over both rounds: 20 unique findings, **15 TP / 3 PARTIAL / 2 FP (~75 % hard-TP, ~90 % real)**.

## 12. Prior art (and what we borrow / differentiate)

| System | Review approach | Exit criteria |
|---|---|---|
| **ruflo** (ruvnet) | Hook-routed verifier agents (`jujutsu` diff-risk, `testgen`), queen+consensus swarm, HNSW vector memory | **None explicit** (emergent/SONA) |
| **Roo Code Boomerang** | Orchestrator → per-subtask modes (architect/code/test/debug) | per-subtask, inline |
| **Devin / OpenHands / SWE-agent** | Per-task agentic loop, verification = test suite + self-critique | tests pass |

**All review inline/per-task; none does a scheduled whole-product audit at a phase boundary** — that's our distinctive bet, and it's justified because inline review *structurally* can't see cross-cutting accretion.

- **Borrow:** semantic-memory dedup (§9, right-sized to brute-force), specialized verifier agents + consensus (our dimensions + refute-before-file / verifier-session upgrade).
- **Differentiate:** the **deterministic blocker gate** (§10) — ruflo's admitted gap is exactly our strength.

## 13. Integration points

**Shipped (soak):**
- `orchestrator/prompts/code_auditor.md` — new read-only prompt (3 semantic dimensions, refute-before-file, raises alerts).
- `orchestrator/prompts/__init__.py` — `code_auditor` → its template in `build_prompt`.
- `orchestrator/ollama_agent.py` — `code_auditor` in `_READONLY_PERSONAS`.
- `deploy/orchestrator/tools.py` — `code_auditor` in `_ONDEMAND_PERSONAS`; `_code_auditor_enabled()`, `_select_phase_for_review()`, `_run_phase_review_gate()` + call site after the architect scheduler; `architect_run_count` increment in `_check_architect_due`.
- `tests/test_code_auditor_gate.py` — 16 tests.

**At the filing promotion:**
- `orchestrator/code_review/` — `dedup.py` (embed + brute-force cosine), `filing.py` (severity routing, caps, PM-API).
- `deploy/orchestrator/tools.py` — fold `phase_blockers_open()==0` into the phase-advance / dispatch logic.
- `db/migrations/versions/NNN_*.py` — `phases.review_state`; `code_review_findings` table.
- Reuse: `security_auditor` filing machinery; drift-detector dedup + soak protocol.

## 14. Open decisions

1. **Cadence** — RESOLVED: every N architect runs since last audit, env-tunable `CODE_AUDIT_ARCHITECT_RUNS` (default **1 during the soak** for precision-sample density; steady-state target **2** ≈ every 6 features). Replaces the LOC/feature churn floor — feature count was the wrong metric, and the architect already provides a fresh-context heartbeat to ride. **Verifier-session** for Critical/High is now RECOMMENDED (not optional) per the first-run triage — the lone hard FP was a High that self-critique missed.
2. **Scope** — RESOLVED: 3 semantic dimensions only; architecture deferred to the architect (fires every ~3 features, not the stale "~50").
3. **Verify rigor for filing v1** — self-critique in-prompt (cheap, 1 session) vs a dedicated verifier session per Critical/High (robust, +1 session/phase). Lean self-critique.
4. **Embedding backend** (promotion) — `ollama` (`nomic-embed-text`, free, matches prod) vs API SDK. Lean ollama; `τ_dup ≈ 0.88`, `τ_regress ≈ 0.90`, tune during soak.

## 15. Risks & mitigations

| Risk | Mitigation |
|---|---|
| False positives stop the line | refute-before-file + Critical/High-only preemption + comment-only soak; verifier-session upgrade if needed |
| Re-find loop (refile every phase) | two-pass dedup (§9): exact key + semantic cosine |
| Hardening lane grows unbounded | per-product ceiling + no-silent-truncation `log()` |
| Cost of full sweeps | phase-boundary cadence + churn floor + delta-focused review |
| Coupling shipped-phase bugs into active phase | only blockers go to the active phase; Medium/Low → Hardening lane |
