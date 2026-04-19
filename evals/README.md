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

For a canned fixture (product + feature), we actually run the persona against
the backend (Ollama for local, Claude API for CI). We then apply judge rubrics
to the output (did it produce a design doc? valid JSON? reasonable scope?).

Enable by setting:
- `RUN_LIVE_EVALS=1`
- `OLLAMA_HOST` (local) or `ANTHROPIC_API_KEY` (CI)

Tier-2 evals are skipped by default so `pytest` stays fast.

## When to add an eval

Every time you:
- Change a persona prompt in a way that might affect behaviour
- Discover a failure mode in a session (model hallucinated, produced invalid JSON, skipped the review)
- Add a new persona

Add a tier-1 invariant that would have caught the regression, and a tier-2
fixture if it's worth the eval cost.
