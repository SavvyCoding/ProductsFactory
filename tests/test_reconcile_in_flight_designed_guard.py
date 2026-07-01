"""
Regression test for the in-flight PR reconciler laundering hole
(orchestrator/github_client.py::reconcile_in_flight_prs), fixed 2026-06-28.

A post-coder gate (env_broken, a test-run timeout, a lint/test bounce) rolls a
feature back to Designed while leaving its already-opened PR open. The reconciler
used to advance ANY Implementing/Designed feature with an open PR to Reviewing —
so a gate-bounced Designed feature was laundered straight to a merge
(HomeChoreService 2026-06-27: every feature shipped 0/1). The fix advances ONLY
Implementing (the genuine crash-after-PR-open heal), never Designed. Designed
stays eligible for merged/closed reconciliation.

Mock-based: PM API via httpx.MockTransport, GitHub via monkeypatched seams.
"""

import os
import json
import types
import pytest
import httpx

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator import github_client as gh  # noqa: E402


OPEN_PR = {"state": "open", "merged_at": None}
MERGED_PR = {"state": "closed", "merged_at": "2026-06-28T00:00:00Z"}


def _feature(fid, status, **extra):
    f = {"id": fid, "status": status, "pr_number": 187,
         "review_outcome": None, "fix_attempts": 0}
    f.update(extra)
    return f


def _run_reconcile(monkeypatch, *, feature, pr_state):
    """Run reconcile_in_flight_prs for one feature against a stubbed PR state.
    Returns the list of PATCH bodies sent to the PM API."""
    pid = 1
    patches = []

    # GitHub seams — no network.
    monkeypatch.setattr(gh, "_parse_repo_slug", lambda *_a, **_k: "org/repo")
    monkeypatch.setattr(gh, "_github_headers", lambda: {})
    monkeypatch.setattr(
        gh, "_gh_get",
        lambda url, headers, params=None, timeout=15:
            types.SimpleNamespace(status_code=200, json=lambda: pr_state),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == f"/api/products/{pid}/features":
            return httpx.Response(200, json=[feature])
        if method == "PATCH" and path == f"/api/features/{feature['id']}":
            patches.append(json.loads(request.content) if request.content else {})
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unmocked {method} {path}"})

    real = httpx.Client
    monkeypatch.setattr(
        gh.httpx, "Client",
        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(handler)}),
    )
    gh.reconcile_in_flight_prs(
        {"id": pid, "github_repo": "https://github.com/org/repo"})
    return patches


def _advanced_to_reviewing(patches):
    return [b for b in patches if b.get("status") == "Reviewing"]


class TestReconcileInFlightDesignedGuard:
    def test_designed_with_open_pr_is_not_advanced(self, monkeypatch):
        # THE FIX: a gate-bounced Designed feature with an open PR must NOT be
        # auto-advanced to Reviewing (that laundered the gate to a merge).
        patches = _run_reconcile(
            monkeypatch, feature=_feature(50, "Designed"), pr_state=OPEN_PR)
        assert _advanced_to_reviewing(patches) == [], \
            f"Designed+open-PR was laundered to Reviewing: {patches}"

    def test_implementing_with_open_pr_still_heals(self, monkeypatch):
        # Preserved: the genuine crash-after-PR-open case (a coder always sits at
        # Implementing when post-coder opens the PR).
        patches = _run_reconcile(
            monkeypatch, feature=_feature(51, "Implementing"), pr_state=OPEN_PR)
        assert _advanced_to_reviewing(patches), \
            f"Implementing+open-PR was not healed to Reviewing: {patches}"

    def test_open_pr_with_review_outcome_never_advances(self, monkeypatch):
        # Once a reviewer decision exists, the reviewer's status is authoritative
        # — even Implementing is not re-advanced.
        patches = _run_reconcile(
            monkeypatch,
            feature=_feature(52, "Implementing", review_outcome="changes_requested"),
            pr_state=OPEN_PR)
        assert _advanced_to_reviewing(patches) == []

    def test_designed_with_merged_pr_still_reconciles_to_pushed(self, monkeypatch):
        # Designed stays in IN_FLIGHT for merged/closed reconciliation — only the
        # OPEN-pr advance was removed. A merged PR on a Designed feature → Pushed.
        patches = _run_reconcile(
            monkeypatch, feature=_feature(53, "Designed"), pr_state=MERGED_PR)
        assert [b for b in patches if b.get("status") == "Pushed"], \
            f"merged PR on a Designed feature should reconcile to Pushed: {patches}"
