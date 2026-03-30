# {PRODUCT_NAME} — Architecture

Hard cap: ~800 tokens. Self-trim when updating — keep only what the next session needs.
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

## Key packages

| Package | Responsibility |
|---------|----------------|
| _(populated after first session)_ | |

## Patterns in use

- **Error handling:** return `(T, error)`. Wrap with `fmt.Errorf("context: %w", err)`. Define sentinel errors as `var ErrNotFound = errors.New("not found")`.
- **Logging:** `slog` (stdlib, Go 1.21+) with JSON handler. `log := slog.With("feature", "login")`.
- **Config:** `os.Getenv("KEY")` validated at startup in `cmd/main.go`. Fail fast with `log.Fatal` if required vars missing.
- **DB access:** repository interface + concrete implementation. `type UserRepository interface { FindByID(ctx, id) (*User, error) }`.
- **HTTP:** `net/http` stdlib. Handler functions take `(w http.ResponseWriter, r *http.Request)`. Use `r.Context()` for cancellation.
- **Context:** always propagate `context.Context` as first argument to any IO-touching function.
- **Testing:** table-driven tests with `t.Run(tc.name, ...)`. Use `testify/assert` for assertions. No global state in tests.

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
