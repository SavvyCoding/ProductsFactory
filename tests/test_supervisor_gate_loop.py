"""Tests for the gate-loop circuit-breaker
(orchestrator.supervisor.detect_repeated_gate_rejection + _gate_signature).

The deterministic post-coder gates (lint-guard / test-check / verify-check) had
no repeated-signature detector, so a feature looping on the SAME gate reason
only exited via the fix_attempts cap — which reset_stuck/contention can keep
artificially low (DogTinder #1873: 16 rejections, looped ~8h on a Guard-17
false positive). This detector routes such a feature to the diagnose-first
escalation early by bumping fix_attempts to the escalation threshold.

Two layers: pure signature tests, then detector tests via httpx.MockTransport.
"""
from __future__ import annotations

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import httpx
import pytest

from orchestrator import supervisor


# ── Layer 1: pure _gate_signature tests ─────────────────────────────────────

LINT_BODY = (
    "❌ lint-guard auto-reject (2 violation(s) on commit before review):\n"
    "- removed top-level symbol still referenced elsewhere (deletion-safety): "
    "`count_metric` (removed from debug_test.py) still referenced in "
    "tests/test_prometheus_metrics.py"
)

TESTCHECK_BODY_A = (
    "❌ post-coder test-check auto-reject (tests failed):\n"
    "**All currently-failing tests (2):**\n"
    "- `[src.lib.push]`\n"
    "- `[src.lib.celery_app]`\n"
    "```\ncollected 335 items\ntests/test_admin.py ...  0.42s\n```"
)
# Same failing items, different volatile pytest tail — must hash identically.
TESTCHECK_BODY_A2 = (
    "❌ post-coder test-check auto-reject (tests failed):\n"
    "**All currently-failing tests (2):**\n"
    "- `[src.lib.push]`\n"
    "- `[src.lib.celery_app]`\n"
    "```\ncollected 335 items\ntests/test_admin.py ...  1.07s (different timing)\n```"
)
TESTCHECK_BODY_B = (
    "❌ post-coder test-check auto-reject (tests failed):\n"
    "- `[src.api.dogs]`\n"
)


class TestGateSignature:
    def test_same_items_match(self):
        assert supervisor._gate_signature(TESTCHECK_BODY_A) == \
               supervisor._gate_signature(TESTCHECK_BODY_A)

    def test_ignores_volatile_pytest_output(self):
        # Same failing items, different durations/tail → same signature.
        assert supervisor._gate_signature(TESTCHECK_BODY_A) == \
               supervisor._gate_signature(TESTCHECK_BODY_A2)

    def test_different_items_differ(self):
        assert supervisor._gate_signature(TESTCHECK_BODY_A) != \
               supervisor._gate_signature(TESTCHECK_BODY_B)

    def test_lint_vs_test_differ(self):
        assert supervisor._gate_signature(LINT_BODY) != \
               supervisor._gate_signature(TESTCHECK_BODY_A)

    def test_empty_returns_none(self):
        assert supervisor._gate_signature(None) is None
        assert supervisor._gate_signature("") is None


# ── Layer 2: detector via httpx.MockTransport ───────────────────────────────

def _mock(*, feature, comments, patches, posts,
          cfg=None):
    cfg = cfg if cfg is not None else {
        "supervisor_gate_loop_enabled":   True,
        "supervisor_gate_loop_threshold": 3,
        "supervisor_gate_loop_window":    6,
        "supervisor_dry_run_only":        False,
    }
    fid = feature["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        path, method = request.url.path, request.method
        if method == "GET" and path == f"/api/features/{fid}":
            return httpx.Response(200, json=feature)
        if method == "GET" and path == f"/api/features/{fid}/comments":
            return httpx.Response(200, json=comments)
        if method == "GET" and path == "/api/system-config":
            return httpx.Response(200, json=cfg)
        if method == "PATCH" and path == f"/api/features/{fid}":
            patches.append(_json.loads(request.content) if request.content else {})
            return httpx.Response(200, json={**feature, **(_json.loads(request.content) if request.content else {})})
        if method == "POST" and path == f"/api/features/{fid}/comments":
            posts.append(_json.loads(request.content) if request.content else {})
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/api/supervisor/actions":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unmocked {method} {path}"})

    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    state = {"transport": None}
    real = httpx.Client

    def fake(*a, **k):
        k["transport"] = state["transport"]
        return real(*a, **k)
    monkeypatch.setattr(supervisor.httpx, "Client", fake)
    return lambda t: state.__setitem__("transport", t)


def _gate_comment(body):
    return {"author": "post-coder:test-check", "body": body}


class TestDetectRepeatedGateRejection:
    def test_escalates_on_3_identical(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(3)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700, product_id=8)

        assert res["action"] == "escalated"
        assert res["repeated"] == 3
        # bumped fix_attempts to the escalation threshold (default 4)
        assert len(patches) == 1 and patches[0]["fix_attempts"] == 4
        # left a gate-loop audit comment
        assert posts and posts[0]["author"] == "gate-loop"

    def test_noop_below_threshold(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(2)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "no-op"
        assert patches == [] and posts == []

    def test_noop_when_signatures_differ(self, patch_client):
        # 3 gate rejections but all different → max repeat 1 < threshold.
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A),
                    _gate_comment(TESTCHECK_BODY_B),
                    _gate_comment(LINT_BODY)]
        comments[2]["author"] = "lint-guard"
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_already_at_threshold(self, patch_client):
        # fix_attempts already at escalation threshold → escalation owns it.
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 4}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(5)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_not_active(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Approved", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(5)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_disabled(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(5)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts,
                           cfg={"supervisor_gate_loop_enabled": False,
                                "supervisor_dry_run_only": False}))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "no-op"
        assert patches == []

    def test_dry_run_audits_but_no_patch(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing", "fix_attempts": 1}
        comments = [_gate_comment(TESTCHECK_BODY_A) for _ in range(3)]
        patches, posts = [], []
        patch_client(_mock(feature=feature, comments=comments, patches=patches, posts=posts,
                           cfg={"supervisor_gate_loop_enabled": True,
                                "supervisor_gate_loop_threshold": 3,
                                "supervisor_gate_loop_window": 6,
                                "supervisor_dry_run_only": True}))

        res = supervisor.detect_repeated_gate_rejection(feature_id=700)
        assert res["action"] == "would_escalate"
        assert patches == [] and posts == []
