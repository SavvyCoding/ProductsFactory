# Agent quality evals

Regression harness for ProductFactory persona prompts. The goal is to detect
quality drops *before* a bad prompt rollout burns 50 sessions worth of compute.

There are two tiers of eval, selected at runtime:

## Tier 1 — contract checks (always on, fast, no LLM required)

For each persona we assert a set of **structural invariants** on the built
prompt: it must mention the required output format, must carry the
non-negotiable constraints (e.g. security auditor MUST NOT modify code), must
reference the right working-dir paths, etc.

These catch the most common prompt-drift regression: a refactor that silently
drops a security instruction.

Run:
```bash
pytest evals/ -v
```

## Tier 2 — live LLM evals (optional, slower)

For each scenario (JSON file under `evals/scenarios/`), we build the persona
prompt, call the backend, and apply deterministic scorers to the output.
Designed so a prompt edit's effect is measurable rather than guessed.

### One-shot run

```bash
RUN_LIVE_EVALS=1 OLLAMA_HOST=https://ollama.com OLLAMA_API_KEY=... \
  pytest evals/ -v
```

Or via the runner directly (writes a JSON report consumable by `compare.py`):

```bash
python -m evals.runner evals/results/latest.json
```

### Baseline vs candidate comparison (the main workflow)

```bash
# 1. Run on current master to establish baseline.
python -m evals.runner evals/results/baseline.json

# 2. Edit a persona prompt.
vim orchestrator/prompts/brownfield.md

# 3. Run again on the candidate prompt.
python -m evals.runner evals/results/candidate.json

# 4. Compare. Exits non-zero if the candidate has any pass→fail
#    regression, or if overall score dropped >1% (tolerance configurable).
python -m evals.compare evals/results/baseline.json evals/results/candidate.json
```

A CI pre-merge gate could run `compare.py` on every prompt-touching PR.

### Backends

Selected by `EVAL_BACKEND` (default: `ollama` if `OLLAMA_HOST` is set, else `stub`).

- **stub** — replays each scenario's `stub_response` field. Token-free; lets the
  harness be self-tested in CI. Used by `tests/test_eval_harness.py`.
- **ollama** — POSTs to the Ollama HTTP API at `OLLAMA_HOST`, model from
  `EVAL_OLLAMA_MODEL` (default `qwen3-coder:30b`). Matches the orchestrator's
  production path.

### Adding a scenario

1. Create `evals/scenarios/<id>.json`. Required fields:
   - `id`: unique string, also the test name in pytest
   - `description`: one-line human summary
   - `persona`: persona name (or `null` for the coder, which is type-routed via
     `product_overrides.type` — all coder routing now lands on `brownfield.md`;
     the separate greenfield template was retired to a pointer stub 2026-06-11)
   - `product_overrides`: dict merged over `SAMPLE_PRODUCT`
   - `assigned_features`: list of feature dicts
   - `stub_response`: the response the StubBackend should replay (must
     satisfy the scenario's own checks; the runner's unit tests verify this)
   - `checks`: list of `{scorer, args, weight}` — scorers in `evals/scoring.py`
2. Run `pytest tests/test_eval_harness.py::TestRunnerEndToEnd::test_seed_scenarios_all_pass_with_stub`
   to confirm the new scenario's checks pass on its own stub response.

### Adding a scorer

Add a function to `evals/scoring.py` returning `ScoreResult`. Register it in
`SCORERS`. Add a test in `tests/test_eval_harness.py` covering both the pass
and fail paths.

## When to add an eval

Every time you:
- Change a persona prompt in a way that might affect behaviour
- Discover a failure mode in a session (model hallucinated, produced invalid JSON, skipped the review)
- Add a new persona

Add a tier-1 invariant that would have caught the regression, and a tier-2
scenario + scorer if the failure mode needs behavioural detection.
