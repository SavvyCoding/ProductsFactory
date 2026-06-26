"""
Architect cadence counts FEATURE work only (2026-06-25).

`_check_architect_due` must count only feature_type='feature' Pushed features
toward the every-N-pushed trigger — NOT bugs/chores/infra. Two reasons:
  - bug-fixes/chores are corrective and don't reshape architecture, so 3 in a
    row shouldn't trigger a drift review;
  - the code_auditor's cadence rides on architect_run_count, so counting the
    bugs IT files would form a self-reinforcing audit→bug→fix→audit loop.

The same fix covers the code_auditor (it only fires after architect fires).
"""

import os
import sys
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "orchestrator"))
import tools as orch_tools  # noqa: E402


class _Resp:
    def __init__(self, data): self._d = data; self.is_success = True
    def json(self): return self._d


class _FakeClient:
    """Captures PATCH calls; returns empty lists for any GET (sessions etc.)."""
    def __init__(self, patches): self.patches = patches
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url): return _Resp([])
    def patch(self, url, json=None): self.patches.append((url, json)); return _Resp({})


def _pushed(ftype, n):
    return [{"status": "Pushed", "feature_type": ftype} for _ in range(n)]


def _run(features):
    """Call _check_architect_due with a recent last_architect_at (so only the
    pushed-delta can trigger, not the 7-day / first-run fallbacks). Returns
    (architect_queued: bool, baseline_written)."""
    captured = []
    product = {
        "id": 1,
        "config": {
            "last_architect_at": datetime.now(timezone.utc).isoformat(),
            "features_pushed_at_last_architect": 0,
        },
    }
    with patch.object(orch_tools, "_pm_client", lambda: _FakeClient(captured)):
        orch_tools._check_architect_due(product, sys_cfg={}, features=features)
    queued = product.get("run_persona_now") == "architect"
    baseline = None
    for _url, body in captured:
        if body and "config" in body:
            baseline = body["config"].get("features_pushed_at_last_architect")
    return queued, baseline


def test_three_pushed_bugs_do_not_trigger():
    queued, _ = _run(_pushed("bug", 3))
    assert queued is False


def test_three_pushed_chores_do_not_trigger():
    queued, _ = _run(_pushed("chore", 5))
    assert queued is False


def test_three_pushed_features_trigger():
    queued, baseline = _run(_pushed("feature", 3))
    assert queued is True
    # baseline written is the feature-only count (3), not inflated by other types
    assert baseline == 3


def test_mixed_counts_only_features():
    # 2 features + 6 bugs/chores Pushed, threshold 3 → delta is 2 → no trigger
    feats = _pushed("feature", 2) + _pushed("bug", 4) + _pushed("chore", 2)
    queued, _ = _run(feats)
    assert queued is False


def test_null_feature_type_counts_as_feature():
    # legacy rows with no feature_type are real features → still counted
    feats = [{"status": "Pushed"} for _ in range(3)]
    queued, _ = _run(feats)
    assert queued is True
