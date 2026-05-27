"""Tests for the 1-PR (session-PR) merge behavior.

Two properties this file covers:

1. `auto_merge_reviewer._auto_merge_approved` does not short-circuit —
   under the 1-PR model the feature's `pr_number` is the **session** PR
   (base=main), so merging it ships one session's stories directly to
   main with no shared-PR coordination.

2. `auto_merge.sweep_product` does not hold back any approved PR — the
   pre-flat-model "wait for whole sprint" gate is gone, and there is no
   sprint integration PR to special-case.
"""

from unittest.mock import MagicMock, patch

import os


def _set_pm_url() -> None:
    """Ensure PM_API_URL is set; the module reads it at import time."""
    os.environ.setdefault("PM_API_URL", "http://pm-api:8080")


class TestReviewerAutoMergeNoSprintShortCircuit:
    """The pre-1-PR defer-to-sweep skip is gone."""

    def test_runs_merge_on_approved_session_pr(self):
        _set_pm_url()
        from orchestrator.pipelines import auto_merge_reviewer

        product = {
            "id": 9,
            "name": "P",
            "github_repo": "https://github.com/o/r.git",
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


class TestSweepMergesSessionPR:
    """Under 1-PR every approved session PR is merged independently."""

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

        feats_resp   = MagicMock(status_code=200, json=MagicMock(return_value=feats_payload))
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


class TestSweepMergesMultipleFeaturesOnOneSessionPR:
    """A session PR covers N features — one merge flips all of them to Pushed."""

    def test_shared_session_pr(self):
        _set_pm_url()
        from orchestrator import auto_merge

        product = {
            "id": 9,
            "name": "P",
            "github_repo": "https://github.com/o/r.git",
        }
        sys_cfg = {"auto_merge_enabled": True}

        feats_payload = [
            {"id": 1, "status": "Reviewed", "pr_number": 201,
             "review_outcome": "approved", "sprint_id": 175},
            {"id": 2, "status": "Reviewed", "pr_number": 201,
             "review_outcome": "approved", "sprint_id": 175},
        ]

        feats_resp = MagicMock(status_code=200, json=MagicMock(return_value=feats_payload))
        post_resp  = MagicMock(status_code=201, json=MagicMock(return_value={"id": 99}))
        patch_resp = MagicMock(status_code=200, json=MagicMock(return_value={}))

        class _Client:
            def __init__(self):
                self.patches: list[tuple[str, dict]] = []
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, path, **kw):
                if path.endswith("/features"):
                    return feats_resp
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

        # Only the FIRST feature should trigger a real merge call; the
        # second is handled via the merged_pr_nums short-circuit.
        merge_calls: list[int] = []
        def _fake_merge(slug, pr_num, tok):
            merge_calls.append(pr_num)
            return (200, "merged")

        with patch.object(auto_merge, "_try_merge_pr", side_effect=_fake_merge), \
             patch.object(auto_merge.httpx, "Client", side_effect=_make_client), \
             patch("orchestrator.github_client._get_auth_token", return_value="tok"):
            counters = auto_merge.sweep_product(product, sys_cfg)

        assert counters["merged"] == 2
        assert len(merge_calls) == 1, f"expected exactly 1 GitHub merge call, got {merge_calls}"
        pushed_paths = [p[0] for p in client_holder["c"].patches if p[1].get("status") == "Pushed"]
        assert len(pushed_paths) == 2
