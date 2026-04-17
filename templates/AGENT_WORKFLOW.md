# Agent Workflow — {PRODUCT_NAME}

> **⚠️ PERSONA CHECK — READ THIS FIRST**
>
> Check your `AGENT_PERSONA` environment variable:
> ```
> echo $AGENT_PERSONA
> ```
> - **`designer`** → **STOP. Do NOT follow this file.** Your instructions are in the `-p` prompt you were launched with. Follow those exclusively.
> - **`reviewer`** → **STOP. Do NOT follow this file.** Your instructions are in the `-p` prompt you were launched with. Follow those exclusively.
> - **`coder`** (or empty) → continue reading below. This workflow is for you.
>
> This file is the **Coder** standing operating procedure only.
> Edit this file to change Coder behaviour without touching the poller.

---

## 1. Startup (every session, no exceptions)

Read these files in order before doing anything else:

1. `README.md` — product vision, goals, out-of-scope list, known constraints
2. `ARCHITECTURE.md` — existing patterns. Hard cap ~800 tokens. Never introduce a new pattern without PM approval.
3. `CLAUDE.md` — runtime, test command, folder layout, key rules. Hard cap 1,500 tokens.
4. `progress.md` — check YAML front matter:

```yaml
---
workflow_version: 2
resume_step: <number>     # which step to resume at (see step map below)
resume_feature_id: <id>   # which feature was in progress
---
```

**Resume step map:**

| resume_step | Meaning — start here |
|-------------|----------------------|
| 2 | Batch plan written, now claim first feature |
| 3 | Feature claimed (Implementing), now write code |
| 4 | Code written, now run tests |
| 5 | Tests passed, now commit + push + open PR |
| 6 | PR open, now update session summary |
| 7 | Batch done, now run competitor research |

If `resume_step` is present → **RESUME mode**: skip to that step for `resume_feature_id`.
If absent or file missing → **FRESH mode**: start a new batch from step 2.

After reading context:
- If on a feature branch: `git fetch origin && git rebase origin/main`

---

## 2. Batch planning (FRESH mode only)

Your batch is **pre-assigned** — features are listed in the `-p` prompt you were launched with.
Do NOT call `GET /api/features/approved`. Use the assigned list from the prompt directly.

- Sort assigned features by `priority ASC`, then by `depends_on` (dependencies first)
- Take max **{MAX_BATCH_SIZE}** feature(s) — never exceed this
- Write `Temp/batch_{date}_plan.md` — one paragraph per feature: what, why, approach
- Initialise `progress.md`:

```markdown
---
workflow_version: 2
resume_step: 2
resume_feature_id: {first_feature_id}
---
# Progress — {PRODUCT_NAME} | Session {SESSION_UID}
Started: {datetime} UTC
Last heartbeat: {datetime} UTC

## Batch
- [ ] {feature_1_name} (id={id})
- [ ] {feature_2_name} (id={id})
```

- **Commit + push `progress.md` to main** — this is heartbeat #0

---

## 3. Per-feature loop (repeat for each feature in batch)

### Step 3 — Branch

The poller has already set your features to `Implementing` before launching this session.
Do NOT call `PATCH /api/features/{id}` to claim — go straight to creating the branch:

```
git checkout -b feature/{feature_name}
```

**If an open PR already exists for this feature** (re-queued due to merge conflicts):
```
gh pr close <pr_number> --comment "Closing to rebase and reopen — merge conflict detected."
git push origin --delete feature/{feature_name} 2>/dev/null || true
git checkout -b feature/{feature_name}
```

- Update `progress.md`: `resume_step: 3, resume_feature_id: {id}` → commit + push (heartbeat)

### Step 4 — Implement

- Write implementation to `{SOURCE_PATH}/{feature_name}.{ext}`
- Follow every pattern in `ARCHITECTURE.md` exactly
- **Brownfield only:** write to `{NEW_FEATURE_SOURCE}` path. Do NOT touch existing source files unless the feature description explicitly requires it. Log any existing-file touch in `progress.md` under "Touched existing files".
- Update `progress.md`: `resume_step: 4` → commit + push (heartbeat)

### Step 5 — Test + audit

Write tests to `{TEST_PATH}/test_{feature_name}.{ext}`. Required:

```
test_{feature_name}_positive()   — happy path
test_{feature_name}_negative()   — bad input / error paths
test_{feature_name}_edge()       — boundary conditions
```

Run:
```
{TEST_COMMAND}
```

Save output → `Results/{feature_name}_results.json`

**Brownfield:** run the FULL test suite. The `baseline_tests.passed` count from `product_config.json` must not decrease.

**Fix loop:** on failure, fix the implementation and re-run. Max **3 attempts** total.

After 3 failures, append **one JSON line** to `/workspace/session_result.json`:
```
{"id": <feature_id>, "status": "Blocked", "blocked_reason": "<reason>"}
```
Write the reason to `progress.md`. Push. Move on to the next feature in the batch.

After tests pass, run the security audit:
```
{AUDIT_COMMAND}
```
If vulnerabilities found → fix before committing.

Write a **Verification Note** to `progress.md`:
> Re-read the original feature description. In 2–3 sentences confirm the implementation matches the spec — not just that tests pass.

Update `progress.md`: `resume_step: 5` → commit + push (heartbeat)

### Step 6 — Commit + push + PR

Stage only:
- `{SOURCE_PATH}/{feature_name}.*`
- `{TEST_PATH}/test_{feature_name}.*`
- `Results/{feature_name}_*`

**Never stage:** `Temp/` · `*.log` · `__pycache__` · `node_modules/` · `session.lock`

```
git fetch origin && git rebase origin/main
git commit -m "feat({feature_name}): {one_line_description} [ProductFactory]"
git push origin feature/{feature_name}
gh pr create --title "feat: {description}" --base main
```

**Push failure** → `PATCH status → Blocked`, write reason to `progress.md`, never exit 0 silently.

On success, append **one JSON line** to `/workspace/session_result.json`:
```
{"id": <feature_id>, "status": "Reviewing", "pr_number": <n>, "pr_url": "<url>"}
```
Use `echo '{"id":...}' >> /workspace/session_result.json`. The poller polls this file every 30 s and applies each new line to the DB in real-time. Do NOT call `PATCH /api/features/{id}`.

Append **Session State Summary** to `progress.md`:
> Key decisions made, patterns introduced, anything the next session must know about this feature.

Update `progress.md`: `resume_step: 6` → commit + push (heartbeat)

---

## 4. Session wrap-up

After all features in the batch are done:

- Update `progress.md`: session complete, clear `resume_step` (remove from front matter)
- Commit + push `progress.md` and `session_summary.md` to main
- Exit cleanly (exit 0)

> **Note:** Do NOT run competitor research or POST new features here. The recommender agent runs as a separate Docker session after this one completes — it has its own backlog-size gate.

---

## 5. Context management rules

| Rule | Threshold | Action |
|------|-----------|--------|
| Source files read | ≥ 15 | Finish current feature, exit cleanly — don't start next |
| Batch size | {MAX_BATCH_SIZE} features max | Never exceed this per session |
| ARCHITECTURE.md | ~800 tokens max | Self-trim when updating — keep only what next session needs |
| CLAUDE.md | 1,500 tokens max | PM is responsible for staying within cap |

---

## 6. Brownfield-specific rules

Only applies when `product_config.json` exists with `"type": "brownfield"`.

Read `product_config.json` before any implementation. It defines:
- `existing_source_paths` — do not write new features here
- `new_feature_source` — write ALL new code here
- `existing_test_paths` — do not modify
- `new_feature_tests` — write all new tests here
- `baseline_tests.passed` — this count must not decrease
- `test_command` / `audit_command` — use exactly as written
- `max_batch_size` — overrides the default above

**Dependency rule:** Add packages to the existing lock file. Never regenerate `requirements.txt`, `package-lock.json`, or `go.sum` from scratch.

---

## 7. Hard rules — never break these

- ❌ Push directly to `main` (Analysis Run is the only exception — one commit, one time)
- ❌ Write or update `features.md` (DB is the single source of truth for feature status)
- ❌ Exit 0 after a push failure
- ❌ Introduce a new architectural pattern without PM approval
- ❌ Stage `session.lock`, `Temp/`, `*.log`, `__pycache__/`, `node_modules/`
- ❌ Start more than {MAX_BATCH_SIZE} features in one session
- ❌ Touch existing source files in a brownfield product without an explicit requirement
- ❌ Regenerate a lock file from scratch
