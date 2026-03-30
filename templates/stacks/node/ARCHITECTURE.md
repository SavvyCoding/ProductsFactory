# {PRODUCT_NAME} — Architecture

Hard cap: ~800 tokens. Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
src/
  {feature_name}/
    index.ts                 ← feature entry point (exported function/class)
    {feature_name}.types.ts  ← interfaces and types
tests/
  {feature_name}.test.ts
Results/
```

## Key modules

| Module | Responsibility |
|--------|----------------|
| _(populated after first session)_ | |

## Patterns in use

- **Error handling:** throw typed errors (`class AppError extends Error { constructor(public code: string, message: string) }`). Never swallow errors without logging.
- **Logging:** `import { logger } from '../lib/logger'`. Structured JSON output via `pino` or `winston`.
- **Config:** `import { config } from '../lib/config'` — validated at startup with `zod`. Missing required vars throw at boot, not at runtime.
- **DB access:** repository pattern — `class UserRepository` wraps all queries. No raw SQL in feature modules.
- **HTTP:** `fetch` (Node 18+) or `axios`. Always set timeout. Return typed response objects.
- **Auth:** JWT decoded in middleware; feature handlers receive `{ userId: string }` context, not raw tokens.
- **Async:** async/await throughout. No `.then()/.catch()` chains.
- **Testing:** `describe` / `it` blocks. `beforeEach` for setup. Mock external dependencies with `jest.mock()` or `vi.mock()`.

## Naming conventions

- Files: `camelCase.ts` (modules), `PascalCase.ts` (classes/components)
- Functions: `camelCase`
- Types / interfaces: `PascalCase` with `I` prefix for interfaces: `IUserRepository`
- Constants: `UPPER_SNAKE_CASE`
- Tests: `it('should {do something} when {condition}', ...)`

## Do not change without PM approval

_(add locked modules here with reasons)_

## Pre-existing test failures (brownfield only)

_(list known failures excluded from the baseline gate)_
