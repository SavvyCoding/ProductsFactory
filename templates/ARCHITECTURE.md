# {PRODUCT_NAME} — Architecture

Hard cap: ~800 tokens. Self-trim this file when updating — keep only what's needed for the next session.
Last updated: {DATE} by {SESSION_UID}

---

## Directory structure

```
{SOURCE_PATH}/
  {example_module}.{ext}     ← {description}
{TEST_PATH}/
  test_{example_module}.{ext}
Results/
```

## Key modules

| Module | Responsibility |
|--------|---------------|
| {module} | {responsibility} |

## Patterns in use

- **Error handling:** {pattern}  (e.g. raise ValueError, return (result, error) tuple)
- **DB access:** {pattern}  (e.g. repository pattern via UserRepository)
- **Logging:** {pattern}  (e.g. structlog with JSON output)
- **Auth:** {pattern}  (e.g. JWT in Authorization header, verified in middleware)
- **Config:** {pattern}  (e.g. environment variables via python-dotenv)

## Naming conventions

- Files: `snake_case.py`
- Classes: `PascalCase`
- Functions: `snake_case`
- Tests: `test_{module}_{case}()`

## Do not change without PM approval

- {locked_module} — reason: {reason}

## Pre-existing test failures (brownfield only)

- {test_name}: {reason why it fails — excluded from baseline gate}
