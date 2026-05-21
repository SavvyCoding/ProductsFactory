# {PRODUCT_NAME} — Architecture

Hard cap: ~1500 tokens (raised from 800 — this doc is now machine-read by ProductFactory's pre-coder context builder, lint guard, and architect persona).
Self-trim when updating — keep only what the next session needs.
Last updated: {DATE} by session {SESSION_UID}

---

## Directory structure

```
internal/
  {feature_name}/
    {feature_name}.go         ← implementation + exported interface
    {feature_name}_test.go    ← table-driven tests
cmd/
  main.go
go.mod / go.sum
```

## ENTRY POINTS

| Concern | Canonical file | Notes |
|---|---|---|
| Binary entry | `cmd/{binary_name}/main.go` | One main.go per binary. Do not create `main2.go`. |
| Server bootstrap | _(populate — e.g. `internal/server/server.go:New`)_ | Only place that registers handlers. |
| Background worker | _(populate if applicable)_ | |

## MODULES

The source-of-truth registry. Pre-coder context reads this section; when your feature touches a listed concern, USE the canonical package — do not create a parallel `*_repository.go` / `*_store.go` alongside it.

| Concern | Canonical package | Owns | Notes |
|---|---|---|---|
| _(populated as features land)_ | | | |

Example rows:
- `User persistence` | `internal/users` | `User`, `UserStore`, `NewUserStore` | Single store interface; concrete impl in same package.
- `Authentication` | `internal/auth` | `VerifyAuth(ctx, *http.Request) (User, error)` | Sentinel error `ErrUnauthorized`.

## RULES

Machine-checkable invariants. The post-coder lint guard refuses commits that violate these.

- Every state-changing handler MUST call `auth.VerifyAuth(r.Context(), r)` unless the file's first line contains `// PUBLIC_ROUTE: <reason>`.
- No `_ = err` / discarded errors in production code. Either handle or wrap with `fmt.Errorf("context: %w", err)`.
- `context.Context` MUST be the first parameter on every IO-touching function.
- No hardcoded fallback secrets in `jwt.Sign` / `hmac.New` calls. If env var unset, return 503.
- No `os/exec` invocations with user-derived input without `exec.Command` argv form (no shell=true equivalent).
- All `sql.DB.Query` / `Exec` calls MUST use parameterized arguments (`?` placeholders) — no `fmt.Sprintf` into SQL.

## REFERENCE PATTERNS

Copy-pasteable canonical code.

### Auth check at the top of every state-changing handler
```go
func createResource(w http.ResponseWriter, r *http.Request) {
    user, err := auth.VerifyAuth(r.Context(), r)
    if err != nil {
        http.Error(w, err.Error(), http.StatusUnauthorized)
        return
    }
    _ = user.ID
    // ... handler body
}
```

### Error response (never leak internal errors)
```go
http.Error(w, "invalid input", http.StatusBadRequest)
slog.Error("createResource failed", "err", err, "user_id", user.ID)
// NEVER: http.Error(w, err.Error(), http.StatusBadRequest)  // leaks internals
```

### DB connection lifecycle
```go
ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
defer cancel()
rows, err := db.QueryContext(ctx, "SELECT ... WHERE id = ?", userID)
if err != nil { return err }
defer rows.Close()
```

## CONFIG GATES

Quality bars that the post-coder lint guard verifies.

| File | Setting | Required | Rationale |
|---|---|---|---|
| `go.mod` | `go` version | ≥ 1.21 | `slog` stdlib, generics |
| Test coverage (via `go test -cover`) | line coverage | ≥ 70 | Verified by CI; agent must not lower threshold |

## Patterns in use

- **Error handling:** return `(T, error)`. Wrap with `fmt.Errorf("context: %w", err)`. Define sentinel errors as `var ErrNotFound = errors.New("not found")`.
- **Logging:** `slog` (stdlib, Go 1.21+) with JSON handler. `log := slog.With("feature", "login")`.
- **Config:** `os.Getenv("KEY")` validated at startup in `cmd/main.go`. Fail fast with `log.Fatal` if required vars missing.
- **DB access:** repository interface + concrete implementation. `type UserRepository interface { FindByID(ctx, id) (*User, error) }`.
- **HTTP:** `net/http` stdlib. Handler functions take `(w http.ResponseWriter, r *http.Request)`. Use `r.Context()` for cancellation.
- **Context:** always propagate `context.Context` as first argument to any IO-touching function.
- **Testing:** table-driven tests with `t.Run(tc.name, ...)`. Use `testify/assert` for assertions. No global state in tests.

## DEPRECATED

Files / packages slated for removal. The agent-debris detector refuses commits that re-introduce items listed here.

- _(populated as cruft is identified)_

## Naming conventions

- Packages: `lowercase` single word
- Exported symbols: `PascalCase`
- Unexported symbols: `camelCase`
- Interfaces: noun or noun+`er` (`Repository`, `Storer`, `Handler`)
- Test functions: `TestFunctionName_Scenario` — e.g. `TestUserLogin_InvalidPassword`
- Files: `snake_case.go`

## Do not change without PM approval

_(add locked packages here with reasons)_

## Pre-existing test failures (brownfield only)

_(list known failures excluded from the baseline gate)_
