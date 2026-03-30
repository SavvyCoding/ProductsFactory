# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Fill in the blanks below based on the actual tech stack.

---

## Platform

- Runtime: {RUNTIME}
- OS: Linux (Docker container)
- Working directory: /workspace

## Folder layout

```
SRC/               ← implementation files
TestCases/         ← test files
Results/           ← test output JSON
Temp/              ← scratch only (never committed)
```

## Test command

```bash
# TODO: fill in the actual test command for this stack
{TEST_COMMAND}
# Must exit non-zero on failure. Must produce Results/{feature}_results.json.
# Must enforce ≥70% coverage or equivalent.
```

## Audit command

```bash
# TODO: fill in the security audit command for this stack
{AUDIT_COMMAND}
# Run after every package install. Block commit if HIGH+ vulnerabilities found.
```

## Dependencies

```bash
# TODO: document how to add a dependency for this stack
# NEVER regenerate the lock file from scratch on a brownfield product.
```

## Key rules

- Follow patterns in ARCHITECTURE.md exactly
- All secrets via environment variables — never hardcode
- No new architectural patterns without PM approval
- One source file per feature, one test file per feature
