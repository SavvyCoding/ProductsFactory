# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Remove sections that don't apply to this product.

---

## Platform

- Runtime: Python {PYTHON_VERSION}
- OS: Linux (Docker container)
- Working directory: /workspace

## Folder layout

```
src/               ← implementation files  ({feature_name}.py)
tests/             ← test files            (test_{feature_name}.py)
Results/           ← test output JSON      ({feature_name}_results.json)
Temp/              ← scratch only          (never committed)
```

## Test command

```bash
{TEST_COMMAND}
```

## Audit command

```bash
{AUDIT_COMMAND}
# Run after every `pip install`. Block commit if any HIGH or CRITICAL found.
```

## Dependencies

```bash
# Add a dependency:
pip install <pkg>            # then add to requirements.txt with pinned version
pip freeze | grep <pkg> >> requirements.txt

# NEVER regenerate requirements.txt from scratch on a brownfield product.
```

## Key rules

- All functions must have type annotations
- Raise specific exceptions (never bare `except:` or `except Exception:` without re-raise)
- No hardcoded secrets — use `os.environ["KEY"]` (raise, not `.get()` for required vars)
- Follow patterns in ARCHITECTURE.md exactly — same error handling, same naming, same structure
- One module per feature file: `src/{feature_name}.py`
- One test file per feature: `tests/test_{feature_name}.py`

## Imports and test config

`pytest.ini` is **pre-shipped and read-only** — do NOT write to it. It already configures `pythonpath = .` and `testpaths = tests`, so:

- **Import style in tests**: `from src.{feature_name} import X` (not `from {feature_name} import X`, not `import sys; sys.path.insert(...)`)
- **Do NOT create `conftest.py` to add paths** — `pythonpath = .` in `pytest.ini` handles it
- **Do NOT create your own `pytest.ini`** — it's RO-mounted; your edit will be silently dropped
- `src/__init__.py` should exist (empty is fine) so `src` is an importable package
