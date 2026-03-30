# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Keep this file concise. Remove sections that don't apply.

---

## Platform

- Runtime: {RUNTIME}  (e.g. Python 3.11 / Node 20)
- OS target: {OS}
- Working directory: /workspace

## Test command

```
{TEST_COMMAND}
# e.g. pytest tests/ -v --json-report --cov --cov-fail-under=70
```

## Audit command

```
{AUDIT_COMMAND}
# e.g. pip-audit  |  npm audit --audit-level=high
```

## Folder layout

```
{SOURCE_PATH}/         ← implementation files
{TEST_PATH}/           ← test files
Results/               ← test result JSON files
Temp/                  ← scratch files (never committed)
```

## Key rules

- Follow ARCHITECTURE.md patterns exactly
- No new dependencies without checking existing lock file first
- All secrets via environment variables — never hardcode
- {ADDITIONAL_RULES}
