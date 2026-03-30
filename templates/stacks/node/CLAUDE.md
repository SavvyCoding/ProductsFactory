# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Remove sections that don't apply to this product.

---

## Platform

- Runtime: Node.js {NODE_VERSION} · TypeScript {TS_VERSION}
- Package manager: {PACKAGE_MANAGER} (npm | yarn | pnpm)
- OS: Linux (Docker container)
- Working directory: /workspace

## Folder layout

```
src/
  {feature_name}/
    index.ts           ← implementation
    {feature_name}.types.ts  ← interfaces / types (if needed)
tests/
  {feature_name}.test.ts     ← test file
Results/
  {feature_name}_results.json
Temp/                  ← scratch only (never committed)
```

## Test command

```bash
{PACKAGE_MANAGER} test -- \
  --testPathPattern=tests/{feature_name} \
  --coverage --coverageThreshold='{"global":{"lines":70,"functions":70}}' \
  --json --outputFile=Results/{feature_name}_results.json
```

_(Adjust if using vitest: `vitest run tests/{feature_name}.test.ts --coverage`)_

## Audit command

```bash
npm audit --audit-level=high
# Run after every package install. Block commit if any HIGH or CRITICAL found.
```

## Dependencies

```bash
# Add a runtime dependency:
{PACKAGE_MANAGER} add <pkg>

# Add a dev dependency:
{PACKAGE_MANAGER} add -D <pkg>

# NEVER delete and regenerate package-lock.json / yarn.lock on a brownfield product.
```

## Key rules

- TypeScript strict mode — no `any` without a comment explaining why
- No `console.log` in production code — use the established logger (see ARCHITECTURE.md)
- All secrets via `process.env.KEY` — validate at startup, fail fast if missing
- Async/await only — no raw Promises unless wrapping a callback API
- Follow patterns in ARCHITECTURE.md exactly
- Export one primary function or class per feature file
