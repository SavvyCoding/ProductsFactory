# {PRODUCT_NAME} — Architecture

Hard cap: ~800 tokens. Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
SRC/
  {feature_name}.py        ← one module per feature
TestCases/
  test_{feature_name}.py   ← mirrors SRC structure
Results/
  {feature_name}_results.json
```

## Key modules

| Module | Responsibility |
|--------|----------------|
| _(populated after first session)_ | |

## Patterns in use

- **Error handling:** raise typed exceptions (`ValueError`, `RuntimeError`, custom `AppError`). Never swallow exceptions silently.
- **Logging:** `import logging; log = logging.getLogger(__name__)`. JSON format in production.
- **Config:** required env vars via `os.environ["KEY"]` (raises `KeyError` if missing — intentional). Optional vars via `os.environ.get("KEY", default)`.
- **DB access:** repository pattern — `class UserRepository` encapsulates all queries. Never write raw SQL in feature modules.
- **Auth:** JWT verified in middleware before reaching feature code. Feature modules receive `user_id: int`, not raw tokens.
- **HTTP client:** `httpx.Client` (sync) or `httpx.AsyncClient` (async). Always set `timeout=`.
- **Type hints:** all function signatures annotated. `from __future__ import annotations` at top of each file.
- **Testing:** pytest fixtures for shared state. `conftest.py` at `TestCases/` root. Rolled-back DB transactions per test.

## Naming conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions / methods: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Tests: `test_{module}_{scenario}()` — e.g. `test_user_login_positive()`

## Do not change without PM approval

_(add locked modules here with reasons)_

## Pre-existing test failures (brownfield only)

_(list known failures excluded from the baseline gate)_
