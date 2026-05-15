"""Tests for the two-tier (session-PR) merge behavior.

Two changes the two-tier model made to auto-merge:

1. `auto_merge_reviewer._auto_merge_approved` no longer short-circuits when
   `product._sprint_pr_mode` is True. Under the two-tier model the
   feature's `pr_number` is the **session** PR (base=sprint integration
   branch), not the sprint PR itself, so merging it is safe — it just
   accumulates one session's work into the sprint branch.

2. `auto_merge.sweep_product` no longer holds a sprint PR until every
   feature in the sprint is merge-eligible (`_is_merge_eligible` and the
   `held_sprint_prs` machinery are gone). Instead, it skips features
   whose `pr_number` matches a sprint's `pr_number` (legacy state from
   pre-two-tier products) and merges every other Reviewed+approved PR
   independently.
"""

from unittest.mock import MagicMock, patch

import os


def _set_pm_url() -> None:
    """Ensure PM_API_URL is set; the module reads it at import time."""
    os.environ.setdefault("PM_API_URL", "http://pm-api:8080")


class TestReviewerAutoMergeNoSprintShortCircuit:
    """The pre-two-tier defer-to-sweep skip is gone."""

    def test_runs_merge_under_sprint_pr_mode(self):
        _set_pm_url()
        from orchestrator.pipelines import auto_merge_reviewer

        product = {
            "id": 9,
            "name": "P",
            "github_repo": "https://github.com/o/r.git",
            "_sprint_pr_mode":   True,
            "_sprint_pr_number": 100,  # sprint integration PR
        }
        features = [{
            "id": 1, "review_outcome": "approved", "pr_number": 200,  # session PR
        }]

        pr_state = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=MagicMock(return_value={"state": "open", "merged_at": None}),
        )
        upd = MagicMock(status_code=422, text="up-to-date")
        merge_ok = MagicMock(
            status_code=200,
            headers={"content-type": "application/json"},
            json=MagicMock(return_value={"merged": True}),
        )

        with patch.object(auto_merge_reviewer, "_get_gh_token", return_value="tok"), \
             patch.object(auto_merge_reviewer.httpx, "get", return_value=pr_state), \
             patch.object(auto_merge_reviewer.httpx, "put", side_effect=[upd, merge_ok]):
            out = auto_merge_reviewer._auto_merge_approved(product, features)

        # Feature should be flipped to Pushed, not returned unchanged
        assert out[0]["status"] == "Pushed"
        assert out[0]["pr_number"] is None


class TestSweepSkipsSprintIntegrationPR:
    """sweep_product treats sprint integration PRs as off-limits."""

    def test_skips_feature_pointing_at_sprint_pr(self):
        _set_pm_url()
        from orchestrator import auto_merge

        product = {
            "id": 9,
            "name": "P",
            "github_repo": "https://github.com/o/r.git",
        }
        sys_cfg = {"auto_merge_enabled": True}

        # Feature points at PR #100 — same as the sprint integration PR.
        feats_payload = [
            {
                "id": 1, "name": "leg",
                "status": "Reviewed", "pr_number": 100,
                "review_outcome": "approved",
                "sprint_id": 175,
            },
        ]
        sprints_payload = [{"id": 175, "pr_number": 100}]

        feats_resp   = MagicMock(status_code=200, json=MagicMock(return_value=feats_payload))
        sprints_resp = MagicMock(status_code=200, json=MagicMock(return_value=sprints_payload))

        class _Client:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, path, **kw):
                if path.endswith("/features"):
                    return feats_resp
                if path.endswith("/sprints"):
                    return sprints_resp
                return MagicMock(status_code=404)
            def patch(self, *a, **kw):
                raise AssertionError("PATCH should not be called when feature points at sprint PR")
            def post(self, *a, **kw):
                return MagicMock(status_code=201, json=MagicMock(return_value={"id": 1}))

        with patch.object(auto_merge, "httpx") as mock_httpx, \
             patch("orchestrator.github_client._get_auth_token", return_value="tok"):
            mock_httpx.Client = lambda *a, **kw: _Client()
            counters = auto_merge.sweep_product(product, sys_cfg)

        assert counters["skipped"] == 1
        assert counters["merged"] == 0
        assert counters["checked"] == 1


class TestSweepMergesSessionPR:
    """A feature whose pr_number is NOT a sprint PR is merged normally."""

    def test_merges_session_pr(self):
        _set_pm_url()
        from orchestrator import auto_merge

        product = {
            "id": 9,
            "name": "P",
            "github_repo": "https://github.com/o/r.git",
        }
        sys_cfg = {"auto_merge_enabled": True}

        feats_payload = [
            {
                "id": 1, "name": "story",
                "status": "Reviewed", "pr_number": 201,  # session PR
                "review_outcome": "approved",
                "sprint_id": 175,
            },
        ]
        sprints_payload = [{"id": 175, "pr_number": 100}]  # sprint PR #100 ≠ 201

        feats_resp   = MagicMock(status_code=200, json=MagicMock(return_value=feats_payload))
        sprints_resp = MagicMock(status_code=200, json=MagicMock(return_value=sprints_payload))
        post_resp    = MagicMock(status_code=201, json=MagicMock(return_value={"id": 99}))
        patch_resp   = MagicMock(status_code=200, json=MagicMock(return_value={}))

        class _Client:
            def __init__(self):
                self.patches: list[tuple[str, dict]] = []
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, path, **kw):
                if path.endswith("/features"):
                    return feats_resp
                if path.endswith("/sprints"):
                    return sprints_resp
                return MagicMock(status_code=404)
            def patch(self, path, json=None, **kw):
                self.patches.append((path, json or {}))
                return patch_resp
            def post(self, *a, **kw):
                return post_resp

        client_holder = {}
        def _make_client(*a, **kw):
            c = _Client()
            client_holder["c"] = c
            return c

        with patch.object(auto_merge, "_try_merge_pr", return_value=(200, "merged")), \
             patch.object(auto_merge.httpx, "Client", side_effect=_make_client), \
             patch("orchestrator.github_client._get_auth_token", return_value="tok"):
            counters = auto_merge.sweep_product(product, sys_cfg)

        assert counters["merged"] == 1
        assert counters["skipped"] == 0
        c = client_holder["c"]
        # The feature should have been PATCHed to Pushed
        pushed = [p for p in c.patches if p[1].get("status") == "Pushed"]
        assert pushed, f"expected a Pushed PATCH, got {c.patches}"
