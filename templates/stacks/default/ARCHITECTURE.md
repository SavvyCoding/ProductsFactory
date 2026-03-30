# {PRODUCT_NAME} — Architecture

Hard cap: ~800 tokens. Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
SRC/
  {feature_name}.{ext}         ← one file per feature
TestCases/
  test_{feature_name}.{ext}    ← mirrors SRC
Results/
```

## Key modules

| Module | Responsibility |
|--------|----------------|
| _(populated after first session)_ | |

## Patterns in use

- **Error handling:** _(fill in — e.g. exceptions, error return values, Result type)_
- **Logging:** _(fill in — e.g. structured JSON, log levels)_
- **Config:** _(fill in — e.g. env vars, config files)_
- **DB access:** _(fill in — e.g. repository pattern, ORM, raw queries)_
- **Auth:** _(fill in — e.g. JWT, session, API key)_

## Naming conventions

- _(fill in for this stack)_

## Do not change without PM approval

_(add locked modules here with reasons)_

## Pre-existing test failures (brownfield only)

_(list known failures excluded from the baseline gate)_
