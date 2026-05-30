"""Tier-2 eval runner.

Reads scenario JSONs from `evals/scenarios/`, builds the persona prompt for
each, calls the configured backend, applies the scenario's scorers, and
writes a per-scenario result. Used by `evals/test_live_evals.py` (pytest
integration) AND by `evals/compare.py` (which runs the full suite directly
to produce a JSON report).

The runner deliberately keeps zero state across scenarios — each one is
self-contained, with the working-dir, product fixture, and assigned
features all fully specified in its JSON. This guarantees that re-running
the suite against a different prompt version produces comparable results.

Stub vs live:
  - StubBackend is the default in unit tests and lets the harness itself
    be tested without spending tokens. Each scenario carries a
    `stub_response` field — the StubBackend matches the scenario's id
    against the prompt's session_uid (which the runner sets to the
    scenario id) and returns the stub.
  - OllamaBackend / future backends ignore `stub_response` and actually
    call the model.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from evals.backends import Backend, StubBackend, make_backend_from_env
from evals.scoring import SCORERS, ScoreResult
from evals.fixtures import SAMPLE_PRODUCT
from orchestrator.prompts import build_prompt


SCENARIO_DIR = Path(__file__).parent / "scenarios"


@dataclass
class ScenarioResult:
    """Aggregated outcome of running one scenario against the backend."""
    scenario_id: str
    persona: str | None
    passed: bool
    total_score: float
    max_score: float
    checks: list[dict[str, Any]] = field(default_factory=list)
    output_length: int = 0
    backend: str = ""
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_id":  self.scenario_id,
            "persona":      self.persona,
            "passed":       self.passed,
            "total_score":  round(self.total_score, 3),
            "max_score":    round(self.max_score, 3),
            "score_pct":    round(100 * self.total_score / self.max_score, 1)
                            if self.max_score else 0.0,
            "checks":       self.checks,
            "output_length": self.output_length,
            "backend":      self.backend,
            "error":        self.error,
        }


def load_scenarios() -> list[dict[str, Any]]:
    """Return every scenario JSON in SCENARIO_DIR, sorted by filename."""
    scenarios = []
    for p in sorted(SCENARIO_DIR.glob("*.json")):
        try:
            scenarios.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:
            raise RuntimeError(f"failed to parse {p}: {e}") from e
    return scenarios


def _build_product(scenario: dict) -> dict:
    """Combine SAMPLE_PRODUCT defaults with scenario overrides + assigned
    features. Returns the dict the prompt builder consumes."""
    product = dict(SAMPLE_PRODUCT)
    product.update(scenario.get("product_overrides", {}))
    assigned = scenario.get("assigned_features", [])
    if assigned:
        product["_assigned_features"] = assigned
        product["_assigned_features_md"] = "\n".join(
            f"- #{f['id']} {f.get('name', '?')} ({f.get('status', '?')})"
            for f in assigned
        )
    return product


def _build_backend(scenario: dict, backend: Backend | None) -> Backend:
    """If the caller passed a backend, use it. Otherwise:
      - if the scenario has a stub_response, return a StubBackend that
        always emits it. Lets unit tests run without env config.
      - else fall through to make_backend_from_env.
    """
    if backend is not None:
        return backend
    stub = scenario.get("stub_response")
    if stub is not None:
        return StubBackend(rules=[("", stub)], default=stub)
    return make_backend_from_env()


def run_scenario(scenario: dict, *, backend: Backend | None = None) -> ScenarioResult:
    """Build the prompt for one scenario, call the backend, score the output.

    Never raises — backend errors become ScenarioResult.error and the
    scenario fails. The runner is a long-lived process; one bad scenario
    must not abort the suite.
    """
    sid = scenario["id"]
    persona = scenario.get("persona")
    product = _build_product(scenario)
    b = _build_backend(scenario, backend)

    try:
        prompt = build_prompt(product, sid, persona=persona)
    except Exception as e:
        return ScenarioResult(
            scenario_id=sid, persona=persona, passed=False,
            total_score=0.0, max_score=1.0, backend=b.name,
            error=f"prompt build failed: {e}",
        )

    try:
        output = b.call(prompt)
    except Exception as e:
        return ScenarioResult(
            scenario_id=sid, persona=persona, passed=False,
            total_score=0.0, max_score=1.0, backend=b.name,
            error=f"backend call failed: {e}",
        )

    check_results = []
    total = 0.0
    max_total = 0.0
    all_passed = True
    for ch in scenario.get("checks", []):
        scorer_name = ch["scorer"]
        scorer = SCORERS.get(scorer_name)
        if scorer is None:
            check_results.append({
                "scorer": scorer_name, "passed": False, "score": 0.0,
                "weight": 0.0,
                "detail": f"unknown scorer {scorer_name!r}",
            })
            all_passed = False
            continue
        weight = float(ch.get("weight", 1.0))
        try:
            r: ScoreResult = scorer(output, **ch.get("args", {}))
        except Exception as e:
            check_results.append({
                "scorer": scorer_name, "passed": False, "score": 0.0,
                "weight": weight,
                "detail": f"scorer raised: {e}",
            })
            all_passed = False
            max_total += weight
            continue
        check_results.append({
            "scorer": scorer_name, "passed": r.passed, "score": r.score,
            "weight": weight, "detail": r.detail, "data": r.data,
        })
        total += r.score * weight
        max_total += weight
        if not r.passed:
            all_passed = False

    return ScenarioResult(
        scenario_id=sid, persona=persona, passed=all_passed,
        total_score=total, max_score=max_total,
        checks=check_results, output_length=len(output),
        backend=b.name,
    )


def run_all(backend: Backend | None = None) -> list[ScenarioResult]:
    """Run every scenario in SCENARIO_DIR with the given backend (or default).
    Returns one ScenarioResult per scenario. Order matches load_scenarios."""
    return [run_scenario(s, backend=backend) for s in load_scenarios()]


def write_report(results: list[ScenarioResult], path: str | Path) -> None:
    """Write a JSON report at `path`. The schema is consumed by
    compare.py, so keep keys stable across runs.
    """
    payload = {
        "version": 1,
        "scenarios": [r.as_dict() for r in results],
        "summary": {
            "total":    len(results),
            "passed":   sum(1 for r in results if r.passed),
            "failed":   sum(1 for r in results if not r.passed),
            "errors":   sum(1 for r in results if r.error),
            "score_pct": round(
                100 * sum(r.total_score for r in results)
                    / max(0.001, sum(r.max_score for r in results)),
                1,
            ),
        },
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def main() -> int:
    """CLI: `python -m evals.runner [report_path]`. Returns 0 if all
    scenarios passed, 1 otherwise."""
    import sys
    out_path = sys.argv[1] if len(sys.argv) > 1 else "evals/results/latest.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    results = run_all()
    write_report(results, out_path)
    summary = json.loads(Path(out_path).read_text(encoding="utf-8"))["summary"]
    print(f"[evals] {summary['passed']}/{summary['total']} passed "
          f"({summary['score_pct']}%); wrote {out_path}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
