# {PRODUCT_NAME} — Architecture

Hard cap: ~1500 tokens (raised from 800 — this doc is now machine-read by ProductFactory's pre-coder context builder, lint guard, and architect persona).
Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
src/
  {feature_name}.{ext}         ← one file per feature
tests/
  test_{feature_name}.{ext}    ← mirrors src
Results/
```

## ENTRY POINTS

The single source for "where the app starts." If a session needs to register a route or wire a new entry, edit the canonical file listed here. Do not create `main_v2`, `app_complete`, or sibling variants.

| Concern | Canonical file | Notes |
|---|---|---|
| Application bootstrap | _(fill in for this stack)_ | Only place that registers routes / commands. |
| Production server entry | _(fill in)_ | |
| CLI entry | _(fill in if applicable)_ | |

## MODULES

The source-of-truth registry. Pre-coder context reads this section; when your feature touches a listed concern, USE the canonical module — do not create a parallel implementation alongside it.

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| _(populated as features land)_ | | | |

## RULES

Machine-checkable invariants. The post-coder lint guard refuses commits that violate these.

- Every state-changing API handler MUST call the project's canonical auth check unless its first line contains `# PUBLIC_ROUTE: <reason>` (or stack-equivalent comment).
- No empty `catch`/`except: pass` in non-test code.
- No `eval()` / `exec()` / dynamic-code execution on data derived from request input.
- No hardcoded fallback secrets in token-signing functions. If the secret env var is unset, return 503.
- DB connections must be closed via context manager / defer / try-finally.
- File mutations that share concern with existing canonical modules MUST extend them, not parallel them.

## REFERENCE PATTERNS

Copy-pasteable canonical code. Fill these in for the stack at first feature land.

### Auth check
```
_(populate with stack-specific snippet)_
```

### Error response (never leak internals)
```
_(populate with stack-specific snippet)_
```

### DB connection lifecycle
```
_(populate with stack-specific snippet)_
```

## CONFIG GATES

Quality bars that the post-coder lint guard verifies. Authoritative source: `quality_gates.json` (installed alongside this file).

| File | Setting | Required | Rationale |
|---|---|---|---|
| _(fill in for this stack)_ | | | |

## Patterns in use

- **Error handling:** _(fill in — e.g. exceptions, error return values, Result type)_
- **Logging:** _(fill in — e.g. structured JSON, log levels)_
- **Config:** _(fill in — e.g. env vars, config files)_
- **DB access:** _(fill in — e.g. repository pattern, ORM, raw queries)_
- **Auth:** _(fill in — e.g. JWT, session, API key)_

## DEPRECATED

Files / modules slated for removal. The agent-debris detector refuses commits that re-introduce items listed here.

- _(populated as cruft is identified)_

## Naming conventions

- _(fill in for this stack)_

## Do not change without PM approval

_(add locked modules here with reasons)_

## Pre-existing test failures (brownfield only)

_(list known failures excluded from the baseline gate)_
