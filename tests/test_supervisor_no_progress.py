"""Tests for the no-progress / hang-loop token-burn guard
(orchestrator.supervisor.detect_no_progress_sessions + _session_minutes).

A coder session that runs long and pushes nothing produces no bounce comment,
so gate/cap/flap detectors stay silent and fix_attempts never reaches the cap —
the feature can burn sessions indefinitely (DogTinder #1873: ~4M input tokens
per 60–90min hang, zero progress). This detector keys on session telemetry
(duration + features_pushed) and blocks the targeted feature for triage.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import httpx
import pytest

from orchestrator import supervisor

UTC = timezone.utc
_BASE = datetime(2026, 6, 22, 10, 0, tzinfo=UTC)


def _sess(sid, persona="coder", pushed=0, attempted=1, minutes=90, tokens=4_000_000):
    start = _BASE
    end = start + timedelta(minutes=minutes)
    return {
        "id": sid, "persona": persona,
        "features_pushed": pushed, "features_attempted": attempted,
        "started_at": start.isoformat(), "ended_at": end.isoformat(),
        "tokens_input": tokens,
    }


# ── Layer 1: pure duration helper ───────────────────────────────────────────

class TestSessionMinutes:
    def test_computes_minutes(self):
        assert abs(supervisor._session_minutes(_sess(1, minutes=90)) - 90) < 0.01

    def test_missing_start_is_zero(self):
        assert supervisor._session_minutes({"started_at": None}) == 0.0


# ── Layer 2: detector via httpx.MockTransport ───────────────────────────────

def _mock(*, product_id, sessions, feature, patches, posts, changelog_fid=700, cfg=None):
    cfg = cfg if cfg is not None else {
        "supervisor_no_progress_enabled": True,
        "supervisor_no_progress_min_minutes": 45,
        "supervisor_no_progress_threshold": 3,
        "supervisor_no_progress_window": 8,
        "supervisor_dry_run_only": False,
    }
    fid = feature["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        path, method = request.url.path, request.method
        if method == "GET" and path == f"/api/products/{product_id}/sessions":
            return httpx.Response(200, json=sessions)
        if method == "GET" and path.startswith("/api/sessions/"):
            return httpx.Response(200, json={"changelog": [{"feature_id": changelog_fid, "field": "status"}]})
        if method == "GET" and path == f"/api/features/{fid}":
            return httpx.Response(200, json=feature)
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


class TestDetectNoProgress:
    def test_blocks_on_3_dead_sessions(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=90) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))

        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "blocked"
        assert 700 in res["blocked"]
        assert len(patches) == 1 and patches[0]["status"] == "Blocked"
        assert "no_progress" in patches[0]["blocked_reason"] or "hang" in patches[0]["blocked_reason"]
        assert posts and posts[0]["author"] == "no-progress"

    def test_noop_below_threshold(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=90) for i in (511, 512)]  # only 2
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_short_sessions(self, patch_client):
        # 3 sessions but all short (5 min) — normal quick bounces, not hangs.
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=5) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_sessions_pushed(self, patch_client):
        # Long sessions but they PUSHED — productive, not dead.
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=90, pushed=1) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_feature_not_active(self, patch_client):
        # Dead sessions, but the feature already shipped (Pushed) — skip.
        feature = {"id": 700, "product_id": 8, "status": "Pushed"}
        sessions = [_sess(i, minutes=90) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_designer_sessions(self, patch_client):
        # Long no-push sessions but they're designer, not coder — ignore.
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, persona="designer", minutes=90) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_noop_when_disabled(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=90) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts,
                           cfg={"supervisor_no_progress_enabled": False,
                                "supervisor_dry_run_only": False}))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "no-op"
        assert patches == []

    def test_dry_run_audits_no_patch(self, patch_client):
        feature = {"id": 700, "product_id": 8, "status": "Implementing"}
        sessions = [_sess(i, minutes=90) for i in (511, 512, 513)]
        patches, posts = [], []
        patch_client(_mock(product_id=8, sessions=sessions, feature=feature,
                           patches=patches, posts=posts,
                           cfg={"supervisor_no_progress_enabled": True,
                                "supervisor_no_progress_min_minutes": 45,
                                "supervisor_no_progress_threshold": 3,
                                "supervisor_no_progress_window": 8,
                                "supervisor_dry_run_only": True}))
        res = supervisor.detect_no_progress_sessions(product_id=8)
        assert res["action"] == "would_block"
        assert patches == [] and posts == []
