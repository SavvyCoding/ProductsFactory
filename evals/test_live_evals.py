"""Pytest integration for Tier-2 evals.

Skipped by default — set RUN_LIVE_EVALS=1 to enable. When skipped, the
file is still imported (catches stray syntax errors in the runner / scorers).

Why pytest at all when we have `python -m evals.runner`? Two reasons:
  1. Pytest is the common CI verb across the repo. Adding `pytest evals/`
     to a CI step is a one-line change; calling our runner directly is more
     work for the same outcome.
  2. Pytest's per-scenario failure isolation surfaces WHICH scenario failed
     instead of a single aggregate exit code. Easier to debug.

For comparison runs (baseline vs candidate), use the runner directly so
the JSON report shape is stable:
  python -m evals.runner evals/results/baseline.json
  # ... edit prompt ...
  python -m evals.runner evals/results/candidate.json
  python -m evals.compare evals/results/baseline.json evals/results/candidate.json
"""
from __future__ import annotations

import os
import pytest

# Skip the whole module unless explicitly enabled. Tier 1 is always on
# (test_prompt_contracts.py); Tier 2 costs LLM tokens.
if not os.environ.get("RUN_LIVE_EVALS"):
    pytest.skip(
        "Tier-2 evals skipped — set RUN_LIVE_EVALS=1 to enable. "
        "Configure backend via EVAL_BACKEND={stub,ollama} + OLLAMA_HOST.",
        allow_module_level=True,
    )

from evals.runner import load_scenarios, run_scenario


@pytest.mark.parametrize(
    "scenario", load_scenarios(),
    ids=lambda s: s["id"],
)
def test_scenario_passes(scenario: dict) -> None:
    """One pytest case per JSON scenario file. Each must pass all its checks.

    On failure, the assertion message includes the per-check details so the
    operator doesn't need to dig through pytest's full traceback to see
    which scorer flagged what.
    """
    result = run_scenario(scenario)
    if result.error:
        pytest.fail(f"{scenario['id']}: {result.error}")
    if not result.passed:
        failed_checks = [
            f"  - {c['scorer']}: {c['detail']}"
            for c in result.checks if not c["passed"]
        ]
        pytest.fail(
            f"{scenario['id']} scored {result.total_score:.2f}/{result.max_score:.2f} "
            f"({100 * result.total_score / max(0.001, result.max_score):.1f}%). "
            f"Failed checks:\n" + "\n".join(failed_checks)
        )
