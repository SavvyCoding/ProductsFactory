"""Compare two evals/results/*.json reports — baseline vs candidate.

Usage:
  # baseline (current master)
  python -m evals.runner evals/results/baseline.json

  # edit a persona prompt
  vim orchestrator/prompts/coder.md

  # candidate
  python -m evals.runner evals/results/candidate.json

  # diff
  python -m evals.compare evals/results/baseline.json evals/results/candidate.json

Exits non-zero if the candidate has any regression (a scenario that
passed in baseline but fails in candidate, OR an overall score drop
> tolerance). Designed for CI use: gate a prompt-change PR on this.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _index(report: dict) -> dict[str, dict]:
    return {s["scenario_id"]: s for s in report.get("scenarios", [])}


def compare(baseline_path: Path, candidate_path: Path,
            score_tolerance_pct: float = 1.0) -> int:
    """Return 0 if candidate ≥ baseline, 1 if regression detected.

    Regression rules:
      - any scenario that PASSED in baseline and FAILS in candidate
      - overall score_pct drop > score_tolerance_pct
    Improvements (any scenario that failed in baseline and passes now)
    are highlighted but never block.
    """
    base = _load(baseline_path)
    cand = _load(candidate_path)
    base_by_id = _index(base)
    cand_by_id = _index(cand)

    all_ids = sorted(set(base_by_id) | set(cand_by_id))
    regressed: list[str] = []
    improved: list[str] = []
    unchanged: list[str] = []

    print(f"{'scenario':<45} {'baseline':>12} {'candidate':>12} {'delta':>8}")
    print("-" * 80)
    for sid in all_ids:
        b = base_by_id.get(sid)
        c = cand_by_id.get(sid)
        if b is None:
            print(f"{sid:<45} {'(new)':>12} "
                  f"{'PASS' if c['passed'] else 'FAIL':>12} {'+new':>8}")
            continue
        if c is None:
            print(f"{sid:<45} "
                  f"{'PASS' if b['passed'] else 'FAIL':>12} "
                  f"{'(missing)':>12} {'-':>8}")
            regressed.append(f"{sid}: missing from candidate")
            continue
        b_pct = b.get("score_pct", 0.0)
        c_pct = c.get("score_pct", 0.0)
        delta = c_pct - b_pct
        status = "PASS" if c["passed"] else "FAIL"
        sign = "+" if delta >= 0 else ""
        print(f"{sid:<45} "
              f"{(b_pct):>10.1f}% "
              f"{(c_pct):>10.1f}% "
              f"{sign}{delta:>6.1f}")
        if b["passed"] and not c["passed"]:
            regressed.append(f"{sid}: pass→fail (score {b_pct:.1f}% → {c_pct:.1f}%)")
        elif not b["passed"] and c["passed"]:
            improved.append(f"{sid}: fail→pass (score {b_pct:.1f}% → {c_pct:.1f}%)")
        else:
            unchanged.append(sid)

    base_summary = base.get("summary", {})
    cand_summary = cand.get("summary", {})
    b_total = base_summary.get("score_pct", 0.0)
    c_total = cand_summary.get("score_pct", 0.0)
    delta_total = c_total - b_total

    print("-" * 80)
    print(f"{'OVERALL':<45} {b_total:>10.1f}% {c_total:>10.1f}% "
          f"{'+' if delta_total >= 0 else ''}{delta_total:>6.1f}")
    print()
    print(f"Regressed:   {len(regressed)}")
    for r in regressed:
        print(f"  ❌ {r}")
    print(f"Improved:    {len(improved)}")
    for i in improved:
        print(f"  ✅ {i}")
    print(f"Unchanged:   {len(unchanged)}")
    print()

    score_regressed = delta_total < -score_tolerance_pct
    if score_regressed:
        print(f"❌ overall score dropped {-delta_total:.1f}% "
              f"(tolerance: {score_tolerance_pct}%)")

    if regressed or score_regressed:
        return 1
    return 0


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    baseline = Path(sys.argv[1])
    candidate = Path(sys.argv[2])
    tolerance = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    if not baseline.is_file():
        print(f"baseline not found: {baseline}", file=sys.stderr)
        return 2
    if not candidate.is_file():
        print(f"candidate not found: {candidate}", file=sys.stderr)
        return 2
    return compare(baseline, candidate, score_tolerance_pct=tolerance)


if __name__ == "__main__":
    raise SystemExit(main())
