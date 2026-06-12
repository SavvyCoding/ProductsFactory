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
    except Unauthorized:
        return jsonify({"error": {"code": "UNAUTHORIZED", "message": "auth required"}}), 401
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

### Route organisation — one Blueprint per concern (never a god-file)
A single `src/main.py` holding every route turns each edit into a coordination problem and routinely loses unrelated handlers on rewrite. Split by concern: one file per Blueprint, `main.py` does app-factory only.
```python
# src/api/auth.py
from flask import Blueprint, jsonify, request
auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")

@auth_bp.post("/login")
def login(): ...

# src/api/history.py
history_bp = Blueprint("history", __name__, url_prefix="/api/history")

@history_bp.get("/")
def list_history(): ...

# src/main.py  ← app factory ONLY
from flask import Flask
from src.api.auth import auth_bp
from src.api.history import history_bp

def create_app():
    app = Flask(__name__)
    app.register_blueprint(auth_bp)
    app.register_blueprint(history_bp)
    return app
```
Soft cap: **≤ 8 routes per file**. Above that, split. The post-coder `detect_god_file` check files a chore on any file crossing the threshold.

### Test isolation — autouse DB fixture per test
Tests must not share DB state via the process-global path env var. Put this in `tests/conftest.py`:
```python
import pytest

@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Fresh, schema-initialised DB + JWT secret per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("CALC_DB_PATH", str(db_path))      # rename to your app's env var
    monkeypatch.setenv("JWT_SECRET", "test-secret-key-please-use-32-bytes-min!")
    # Call your product's schema-init functions here:
    from src.history import init_db
    init_db()
    # from src.auth.users import init_users_db
    # init_users_db()
    yield
```
Without this, a test calling a repository function directly (or one of two tests sharing the default DB path) leaks state into the next test — invisible in isolation, broken in the full suite. The post-coder test-check runs the FULL suite, so cross-test leakage poisons every feature's gate.

### External OAuth provider — mock the IdP, never call it
Tests and Verify recipes must NEVER hit a live identity provider (Google/Apple/GitHub OAuth). Mock the token + userinfo endpoints with `respx`:
```python
import respx, httpx

@respx.mock
def test_google_oauth_callback(client):
    respx.post("https://oauth2.googleapis.com/token").respond(
        json={"access_token": "fake-at", "id_token": "fake-idt", "token_type": "Bearer"})
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
        json={"sub": "g-12345", "email": "user@example.com", "email_verified": True})
    r = client.get("/api/auth/google/callback?code=fake-code&state=teststate")
    assert r.status_code == 200
    assert r.json()["user"]["email"] == "user@example.com"
```
The AC asserts YOUR callback logic (token exchange called, user row created, session issued) — never the provider's behaviour.

### Outbound webhook — capture with a test double, assert the payload
```python
import respx, httpx

@respx.mock
def test_webhook_fired_on_decline(client, auth_headers):
    route = respx.post("https://hooks.example.com/endpoint").respond(204)
    client.post("/api/v1/documents/9/decline", headers=auth_headers)
    assert route.called
    body = httpx.Request("POST", "x://x", content=route.calls[0].request.content)
    import json; payload = json.loads(route.calls[0].request.content)
    assert payload["event"] == "document.declined"
```
Delivery retries/signing are YOUR code under test; the receiving endpoint is always a double.

### Third-party HTTP API (maps, payments, push) — recorded-response mock
```python
import respx

@respx.mock
def test_geocode_address():
    respx.get(url__startswith="https://api.mapbox.com/geocoding/").respond(
        json={"features": [{"center": [-122.42, 37.78]}]})   # recorded real shape
    from src.lib.geo import geocode
    assert geocode("123 Main St") == (37.78, -122.42)
```
Keep one canned response per API in `tests/fixtures/` (recorded once from real docs/responses). The AC asserts your parsing/fallback logic. A missing API key must fail closed (503/raise) — never a literal fallback (lint Guard 5/5b refuses those).

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
