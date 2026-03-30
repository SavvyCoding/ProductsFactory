# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Remove sections that don't apply to this product.

---

## Platform

- Runtime: Python {PYTHON_VERSION}
- OS: Linux (Docker container)
- Working directory: /workspace

## Folder layout

```
SRC/               ← implementation files  ({feature_name}.py)
TestCases/         ← test files            (test_{feature_name}.py)
Results/           ← test output JSON      ({feature_name}_results.json)
Temp/              ← scratch only          (never committed)
```

## Test command

```bash
pytest TestCases/ -v \
  --json-report --json-report-file=Results/{feature_name}_results.json \
  --cov=SRC --cov-report=term-missing --cov-fail-under=70
```

## Audit command

```bash
pip-audit
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
- One module per feature file: `SRC/{feature_name}.py`
- One test file per feature: `TestCases/test_{feature_name}.py`
