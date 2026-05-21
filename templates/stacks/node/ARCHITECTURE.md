# {PRODUCT_NAME} — Architecture

Hard cap: ~1500 tokens (raised from 800 — this doc is now machine-read by ProductFactory's pre-coder context builder, lint guard, and architect persona).
Self-trim when updating — keep only what the next session needs.
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

## ENTRY POINTS

The single source for "where the app starts." If a session needs to register a route or wire a new entry, edit the canonical file listed here. Do not create `server2.ts`, `app_v2.ts`, or sibling variants.

| Concern | Canonical file | Notes |
|---|---|---|
| App factory | _(populate — e.g. `src/server.ts:createApp`)_ | Only place that mounts routes. |
| Production entry | _(populate — e.g. `src/server.ts` via `node dist/server.js`)_ | |
| CLI entry | _(populate if applicable — e.g. `src/cli.ts`)_ | |

## MODULES

The source-of-truth registry. Pre-coder context reads this section; when your feature touches a listed concern, USE the canonical module — do not create a parallel `*Repository.ts` / `*Store.ts` / `*Service.ts` alongside it.

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| _(populated as features land)_ | | | |

Example rows (replace as the product grows):
- `User persistence` | `src/users/userStore.ts` | `User`, `createUser`, `getUserById` | Single store; do not create `userRepository.ts` etc.
- `Authentication` | `src/auth/verifyAuth.ts` | `verifyAuth(req) → User` | Throws `Unauthorized`; do not hand-roll per-route.
- `HTTP client` | `src/lib/httpClient.ts` | `getClient()` | Configures retries/timeouts; do not call raw `fetch()`.

## RULES

Machine-checkable invariants. The post-coder lint guard refuses commits that violate these.

- Every state-changing route handler (`POST`, `PUT`, `PATCH`, `DELETE` in `src/pages/api/` or `app/api/`) MUST call one of `verifyAuth`, `requireAuth`, `withAuth`, `getServerSession` — or the file's first line must contain `// PUBLIC_ROUTE: <reason>`.
- No `catch { /* empty */ }` / `catch (e) { }` in non-test code. Log, rethrow, or handle — never silently swallow.
- No `eval()`, `Function()`, or `vm.runInNewContext()` on data derived from request input.
- No hardcoded fallback secrets in `jwt.sign` / `jwt.verify` / `crypto.createHmac` calls. If the secret env var is unset, return 503.
- `package.json` `"type": "module"` ⇒ files use `import`/`export`; no `require()` or `module.exports` in `src/`.
- No `dangerouslySetInnerHTML` on user-derived strings without `DOMPurify.sanitize`.

## REFERENCE PATTERNS

Copy-pasteable canonical code. Use verbatim. If you need a variant, propose updates to this section first.

### Auth check at the top of every state-changing route
```typescript
import { verifyAuth } from '@/lib/auth/verifyAuth';

export default async function handler(req, res) {
  let user;
  try { user = await verifyAuth(req); }
  catch (e) { return res.status(401).json({ error: e.message }); }
  // ... handler body with user.id available
}
```

### Error response (never leak `error.message` from caught exceptions)
```typescript
return res.status(400).json({
  error: { code: 'INVALID_INPUT', message: 'Field "x" is required' }
});
// NEVER: res.status(400).json({ error: err.message })  // leaks stack/PII
```

### Async/await with timeout
```typescript
const ctrl = new AbortController();
const t = setTimeout(() => ctrl.abort(), 5000);
try {
  const res = await fetch(url, { signal: ctrl.signal });
  // ...
} finally { clearTimeout(t); }
```

## CONFIG GATES

Quality bars that the post-coder lint guard verifies. Authoritative source: `quality_gates.json` (installed alongside this file).

| File | Setting | Required | Rationale |
|---|---|---|---|
| `package.json` | `scripts.test` | (must invoke real runner — e.g. `jest`) | Never `echo` / `exit 0` / `true`. |
| `jest.config.cjs` | `coverageThreshold.global.lines` | ≥ 70 | |
| `jest.config.cjs` | `coverageThreshold.global.branches` | ≥ 60 | |

Override path: PM edits `product.config.quality_gates_override` — never edit `jest.config.cjs` to bypass.

## Patterns in use

- **Error handling:** throw typed errors (`class AppError extends Error { constructor(public code: string, message: string) }`). Never swallow errors without logging.
- **Logging:** `import { logger } from '../lib/logger'`. Structured JSON output via `pino` or `winston`.
- **Config:** `import { config } from '../lib/config'` — validated at startup with `zod`. Missing required vars throw at boot, not at runtime.
- **DB access:** repository pattern — `class UserRepository` wraps all queries. No raw SQL in feature modules.
- **HTTP:** `fetch` (Node 18+) or `axios`. Always set timeout. Return typed response objects.
- **Auth:** JWT decoded in middleware; feature handlers receive `{ userId: string }` context, not raw tokens.
- **Async:** async/await throughout. No `.then()/.catch()` chains.
- **Testing:** `describe` / `it` blocks. `beforeEach` for setup. Mock external dependencies with `jest.mock()` or `vi.mock()`.

## DEPRECATED

Files / modules / paths slated for removal. The agent-debris detector refuses commits that re-introduce items listed here. The architect persona uses this as its TODO queue.

- _(populated as cruft is identified — e.g. "`src/legacy/oldAuth.ts` — superseded by `src/auth/verifyAuth.ts`, delete by 2026-Q3")_

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
