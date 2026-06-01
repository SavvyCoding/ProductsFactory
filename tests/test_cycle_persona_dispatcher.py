"""Tests for orchestrator.cycle.persona._decide_action — specifically the
designer/coder balancing rule (cycle DD 2026-06-01).

The dispatcher's old "designer always before fresh-coder" rule starved the
coder whenever the designer was faster than the coder (canonical 2026-06-01
incident: 44 Designed features waited while 9 Approved features kept the
designer running every cycle for ~1.5h, zero merges). The flipped order
("coder always before designer") starves the designer instead. The fix:
balance by queue depth — pick whichever queue is deeper.
"""
from __future__ import annotations

import os

# persona module reads PM_API_URL at import time defensively; match the
# project convention.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from unittest.mock import MagicMock
from types import SimpleNamespace

from orchestrator.cycle.persona import _decide_action


def _client_for_features(features: list[dict], sys_cfg: dict | None = None):
    """Build a MagicMock httpx client that returns the given features list
    for /api/products/{id}/features and an empty sys_cfg by default."""
    client = MagicMock()
    sys_cfg = sys_cfg or {}

    def _get(url, *a, **kw):
        if "/features" in url:
            return SimpleNamespace(
                is_success=True, status_code=200, json=lambda: features,
            )
        if "/system-config" in url:
            return SimpleNamespace(
                is_success=True, status_code=200, json=lambda: sys_cfg,
            )
        return SimpleNamespace(
            is_success=False, status_code=404, json=lambda: {},
        )

    client.get.side_effect = _get
    return client


def _feature(fid, status, **kw):
    base = {
        "id": fid,
        "status": status,
        "phase_id": 1,  # avoid the auto-plan branch
        "design_doc_path": None,
        "pr_number": None,
        "review_outcome": None,
    }
    base.update(kw)
    return base


class TestDispatcherBalance:
    def test_deeper_coder_queue_runs_coder(self):
        """44 Designed features + 9 Approved-no-design: coder should win.
        This is the canonical cycle DD starvation case."""
        features = (
            # 44 first-pass-codeable
            [_feature(1000 + i, "Designed", design_doc_path=f"docs/{1000+i}.md")
             for i in range(44)]
            # 9 designer queue
            + [_feature(2000 + i, "Approved", design_doc_path=None)
               for i in range(9)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "coder", (
            f"coder should win — coder backlog (44) > designer backlog (9), "
            f"got {result}"
        )
        assert "44" in result["reason"] and "9" in result["reason"]

    def test_deeper_designer_queue_runs_designer(self):
        """41 Approved-no-design + 5 Designed: designer should win.
        This is the canonical cycle H starvation case (opposite shape)."""
        features = (
            [_feature(1000 + i, "Approved", design_doc_path=None)
             for i in range(41)]
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(5)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "designer", (
            f"designer should win — designer backlog (41) > coder backlog (5), "
            f"got {result}"
        )
        assert "41" in result["reason"]

    def test_equal_queues_tiebreak_to_coder(self):
        """Equal queue depths: tie goes to coder (PRs reach Reviewing
        and the system's ultimate throughput metric is merges)."""
        features = (
            [_feature(1000 + i, "Approved", design_doc_path=None)
             for i in range(5)]
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(5)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["action"] == "launch_session"
        assert result["persona"] == "coder", (
            f"equal queues should tie-break to coder, got {result}"
        )

    def test_only_designer_queue_runs_designer(self):
        """Only Approved-no-design features: designer runs (no coder work)."""
        features = [_feature(1000 + i, "Approved", design_doc_path=None)
                    for i in range(3)]
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "designer"

    def test_only_coder_queue_runs_coder(self):
        """Only Designed features: coder runs (no designer work)."""
        features = [_feature(1000 + i, "Designed",
                             design_doc_path=f"docs/{1000+i}.md")
                    for i in range(3)]
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder"

    def test_rework_coder_preempts_both(self):
        """An Implementing+changes_requested feature outranks both
        balancing branches — rework is most time-sensitive and runs
        before any first-pass / design work."""
        features = (
            [_feature(1, "Implementing", review_outcome="changes_requested")]
            # Big designer queue that would otherwise win
            + [_feature(1000 + i, "Approved", design_doc_path=None)
               for i in range(20)]
            # Big coder queue
            + [_feature(2000 + i, "Designed", design_doc_path=f"docs/{2000+i}.md")
               for i in range(30)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "coder"
        assert "rework" in result["reason"].lower()

    def test_reviewer_preempts_everything(self):
        """A Reviewing feature with a PR outranks every other branch.
        Open PRs MUST be reviewed before more work piles up."""
        features = (
            [_feature(1, "Reviewing", pr_number=42)]
            # Designer queue
            + [_feature(1000 + i, "Approved", design_doc_path=None)
               for i in range(20)]
        )
        client = _client_for_features(features)
        result = _decide_action(product_id=25, client=client)
        assert result["persona"] == "reviewer"
