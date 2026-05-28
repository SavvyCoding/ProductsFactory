# {PRODUCT_NAME} — Architecture

Hard cap: ~1500 tokens (raised from 800 — this doc is now machine-read by ProductFactory's pre-coder context builder, lint guard, and architect persona).
Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
src/
  {feature_name}.py        ← one module per feature
tests/
  test_{feature_name}.py   ← mirrors src/ structure
Results/
  {feature_name}_results.json
```

## ENTRY POINTS

The single source for "where the app starts." If a session needs to register a route, add a CLI command, or wire a new entry, edit the canonical file listed here. Do not create `main_v2.py`, `main_complete.py`, `app.py`, or sibling variants.

| Concern | Canonical file | Notes |
|---|---|---|
| App factory | _(populate — e.g. `src/main.py:create_app`)_ | Only place that registers routes. |
| WSGI / ASGI entry | _(populate — e.g. `src/main.py` via `gunicorn src.main:create_app()`)_ | Production server entrypoint. |
| CLI entry | _(populate if applicable — e.g. `src/cli.py`)_ | |

## MODULES

The source-of-truth registry. Pre-coder context reads this section; when your feature touches a listed concern, USE the canonical module — do not create a parallel `*Repository.py` / `*Store.py` / `*Service.py` alongside it.

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| _(populated by the architect persona as features land — do not edit by hand)_ | | | |

## RULES

Machine-checkable invariants. The post-coder lint guard refuses commits that violate these.

- Every state-changing API handler (`POST`, `PUT`, `PATCH`, `DELETE`) MUST call `verify_auth(request)` unless the first line of the file contains `# PUBLIC_ROUTE: <reason>`.
- No bare `except: pass` / `except Exception: pass` in non-test code. Catch the specific exception class you need; let others propagate.
- No `eval()`, `exec()`, or `pickle.loads()` on data derived from request input.
- No hardcoded fallback secrets in `jwt.encode` / `jwt.decode` / `crypto.create_hmac` calls. If the secret env var is unset, return 503 — never substitute a constant.
- DB connections must be closed via `with` context manager OR a `finally:` block in the same function.
- Files in `src/` MUST use Python imports — no `module.exports` / CommonJS / `require()` (this is a Python project).
- No `sys.modules.get('main')` lookups baked into source for test monkey-patching. Use dependency injection via function parameters.

## REFERENCE PATTERNS

Copy-pasteable canonical code. Use verbatim. If you need a variant, propose updates to this section first.

### Auth check at the top of every state-changing route
```python
from src.auth.verify import verify_auth, Unauthorized

@app.route("/api/v1/resource", methods=["POST"])
def create_resource():
    try:
        user = verify_auth(request)
    except Unauthorized as e:
        return jsonify({"error": str(e)}), 401
    # ... handler body, with user.id available
```

### Error response (never leak tracebacks)
```python
return jsonify({"error": {"code": "INVALID_INPUT", "message": "x is required"}}), 400
# NEVER: return jsonify({"error": str(exc)}), 400
```

### DB connection lifecycle
```python
with get_db_connection() as conn:
    cur = conn.cursor()
    cur.execute("SELECT ... WHERE id = %s", (user_id,))   # parameterized, not f-string
    rows = cur.fetchall()
# conn closes via context manager — no leak in any branch
```

## CONFIG GATES

Quality bars that the post-coder lint guard verifies. Authoritative source: `quality_gates.json` (installed alongside this file).

| File | Setting | Required | Rationale |
|---|---|---|---|
| `pytest.ini` | `addopts --cov-fail-under` | ≥ 70 | Template default. Lower only with PM approval. |
| `pytest.ini` | `testpaths` | `tests` | Single canonical test directory. Adding other dirs requires PM approval — do NOT include `TestCases` or other parallel test roots. |

Override path: PM edits `product.config.quality_gates_override` — never edit `pytest.ini` directly to bypass.

## Patterns in use

- **Error handling:** raise typed exceptions (`ValueError`, `RuntimeError`, custom `AppError`). Never swallow exceptions silently.
- **Logging:** `import logging; log = logging.getLogger(__name__)`. JSON format in production.
- **Config:** required env vars via `os.environ["KEY"]` (raises `KeyError` if missing — intentional). Optional vars via `os.environ.get("KEY", default)`.
- **DB access:** repository pattern — `class UserRepository` encapsulates all queries. Never write raw SQL in feature modules.
- **Auth:** JWT verified in middleware before reaching feature code. Feature modules receive `user_id: int`, not raw tokens.
- **HTTP client:** `httpx.Client` (sync) or `httpx.AsyncClient` (async). Always set `timeout=`.
- **Type hints:** all function signatures annotated. `from __future__ import annotations` at top of each file.
- **Testing:** pytest fixtures for shared state. `conftest.py` at `tests/` root. Rolled-back DB transactions per test.

## DEPRECATED

Files / modules / paths slated for removal. The agent-debris detector refuses commits that re-introduce items listed here. The architect persona uses this as its TODO queue.

- _(populated as cruft is identified — e.g. "`src/main.py.backup` — older snapshot, delete in next cleanup")_

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
