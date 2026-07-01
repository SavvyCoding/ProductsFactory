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
                if path.endswith("/sessions"):          # Fix 1a: no active session
                    return MagicMock(status_code=200, json=MagicMock(return_value=[]))
                if "/api/features/" in path:            # Fix 1c re-verify: still eligible
                    _fid = int(path.rstrip("/").split("/")[-1])
                    _m = next((f for f in feats_payload if f["id"] == _fid), {})
                    return MagicMock(status_code=200, json=MagicMock(return_value=_m))
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
                if path.endswith("/sessions"):          # Fix 1a: no active session
                    return MagicMock(status_code=200, json=MagicMock(return_value=[]))
                if "/api/features/" in path:            # Fix 1c re-verify: still eligible
                    _fid = int(path.rstrip("/").split("/")[-1])
                    _m = next((f for f in feats_payload if f["id"] == _fid), {})
                    return MagicMock(status_code=200, json=MagicMock(return_value=_m))
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


class TestSweepRaceGuards:
    """Fix 1 (2026-06-29): the per-cycle sweep must not race a live reviewer
    finalize, and must re-confirm eligibility at the irreversible merge point.
    Root incident: HCS/IFT features #2172/#2173 squash-merged with
    review_outcome=changes_requested because the sweep caught a transient
    Reviewed+approved state during finalize."""

    def _run(self, sessions, fresh_feature):
        _set_pm_url()
        from orchestrator import auto_merge

        product = {"id": 9, "name": "P", "github_repo": "https://github.com/o/r.git"}
        sys_cfg = {"auto_merge_enabled": True}
        feats_payload = [{"id": 1, "status": "Reviewed", "pr_number": 201,
                          "review_outcome": "approved"}]

        class _Client:
            def __init__(self): self.patches = []
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def get(self, path, **kw):
                if path.endswith("/sessions"):
                    return MagicMock(status_code=200, json=MagicMock(return_value=sessions))
                if path.endswith("/features"):
                    return MagicMock(status_code=200, json=MagicMock(return_value=feats_payload))
                if "/api/features/" in path:
                    return MagicMock(status_code=200, json=MagicMock(return_value=fresh_feature))
                return MagicMock(status_code=404)
            def patch(self, path, json=None, **kw):
                self.patches.append((path, json or {}))
                return MagicMock(status_code=200, json=MagicMock(return_value={}))
            def post(self, *a, **kw):
                return MagicMock(status_code=201, json=MagicMock(return_value={"id": 99}))

        holder = {}
        def _mk(*a, **kw):
            c = _Client(); holder["c"] = c; return c

        merge_calls = []
        with patch.object(auto_merge, "_try_merge_pr",
                          side_effect=lambda *a: (merge_calls.append(a) or (200, "merged"))), \
             patch.object(auto_merge.httpx, "Client", side_effect=_mk), \
             patch("orchestrator.github_client._get_auth_token", return_value="tok"):
            counters = auto_merge.sweep_product(product, sys_cfg)
        pushed = [p for p in holder["c"].patches if p[1].get("status") == "Pushed"]
        return counters, merge_calls, pushed

    def test_defers_when_reviewer_session_live(self):
        # Fix 1a: a wrapping reviewer session → defer the whole sweep, no merge.
        counters, merge_calls, pushed = self._run(
            sessions=[{"persona": "reviewer", "status": "wrapping"}],
            fresh_feature={"id": 1, "status": "Reviewed", "pr_number": 201,
                           "review_outcome": "approved"})
        assert counters["skipped"] == 1
        assert counters["merged"] == 0
        assert merge_calls == []
        assert pushed == []

    def test_skips_feature_flipped_since_snapshot(self):
        # Fix 1c: snapshot said approved, but re-verify shows it flipped to
        # changes_requested/Implementing → must NOT merge.
        counters, merge_calls, pushed = self._run(
            sessions=[],
            fresh_feature={"id": 1, "status": "Implementing", "pr_number": 201,
                           "review_outcome": "changes_requested"})
        assert merge_calls == [], "must not merge a feature that flipped since the snapshot"
        assert counters["merged"] == 0
        assert counters["skipped"] >= 1
        assert pushed == []

    def test_merges_normally_when_quiescent_and_still_approved(self):
        # No active reviewer session + re-verify still approved → merges.
        counters, merge_calls, pushed = self._run(
            sessions=[{"persona": "coder", "status": "ended"}],
            fresh_feature={"id": 1, "status": "Reviewed", "pr_number": 201,
                           "review_outcome": "approved"})
        assert counters["merged"] == 1
        assert len(merge_calls) == 1
        assert len(pushed) == 1


class TestSupersededReviewerOutcomes:
    """Fix 2 (2026-06-29): conflicting reviewer entries for one feature collapse
    to the final verdict, removing the transient Reviewed+approved the sweep
    could race."""

    def _idx(self, entries, persona="reviewer"):
        import json
        from orchestrator.session.result_io import _superseded_reviewer_outcome_indices
        return _superseded_reviewer_outcome_indices([json.dumps(e) for e in entries], persona)

    def test_approved_then_changes_requested_collapses(self):
        sup = self._idx([
            {"id": 2173, "status": "Reviewed", "review_outcome": "approved"},
            {"id": 2173, "status": "Implementing", "review_outcome": "changes_requested"},
        ])
        assert sup == {0}, "earlier approved entry must be superseded by the later verdict"

    def test_single_outcome_not_superseded(self):
        assert self._idx([{"id": 1, "review_outcome": "approved"}]) == set()

    def test_distinct_features_independent(self):
        assert self._idx([
            {"id": 1, "review_outcome": "approved"},
            {"id": 2, "review_outcome": "changes_requested"},
        ]) == set()

    def test_non_reviewer_persona_collapses_nothing(self):
        assert self._idx([
            {"id": 1, "review_outcome": "approved"},
            {"id": 1, "review_outcome": "changes_requested"},
        ], persona="coder") == set()
