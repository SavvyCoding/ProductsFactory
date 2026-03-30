# {PRODUCT_NAME} — Claude Configuration

Hard cap: 1,500 tokens. Remove sections that don't apply to this product.

---

## Platform

- Runtime: Go {GO_VERSION}
- OS: Linux (Docker container)
- Working directory: /workspace
- Module path: _(read from go.mod)_

## Folder layout

```
internal/
  {feature_name}/
    {feature_name}.go         ← implementation
    {feature_name}_test.go    ← tests (same package, _test suffix)
cmd/
  main.go                     ← entry point (do not modify unless required)
Results/
  {feature_name}_results.json
Temp/                         ← scratch only (never committed)
```

## Test command

```bash
go test ./internal/{feature_name}/... -v -count=1 \
  -coverprofile=Results/{feature_name}_coverage.out \
  -covermode=atomic 2>&1 | tee Results/{feature_name}_results.json
go tool cover -func=Results/{feature_name}_coverage.out | grep total
# Fail if total coverage < 70%
```

## Audit command

```bash
govulncheck ./...
# Run after every `go get`. Block commit if any HIGH found.
```

## Dependencies

```bash
# Add a dependency:
go get <module>@<version>
# go.sum is updated automatically — never edit it manually.
# NEVER delete go.sum and regenerate on a brownfield product.
```

## Key rules

- Always handle errors explicitly — never `_` an error return without a comment
- No `panic()` in library code — return errors instead
- All public functions and types must have doc comments
- Use context propagation: first argument of any IO function must be `ctx context.Context`
- No `init()` functions — use explicit initialisation
- Follow patterns in ARCHITECTURE.md exactly
