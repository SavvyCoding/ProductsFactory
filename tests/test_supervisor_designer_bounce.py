"""
detect_designer_bounce (2026-06-25): a designer-pool feature (Approved, no
design_doc_path) repeatedly claimed (→Designing) and rolled back to Approved
with NO design doc keeps starving the queue — the designer finds nothing to
design (moot / already-shipped duplicate). rapid_flap misses it (slow, <10
transitions/h). Block after N Designing→Approved rollbacks in the window.

Canonical: HomeChoreService #1954 (a code_auditor finding already fixed by its
sibling #1949 on the same function).
"""

import os
from datetime import datetime, timezone, timedelta

import httpx
import pytest

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator import supervisor


def _ts(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _rollback(hours_ago: float) -> dict:
    return {"field": "status", "old_value": "Designing",
            "new_value": "Approved", "changed_at": _ts(hours_ago)}


def _mock(*, feature, changelog, patches, cfg=None):
    fid = feature["id"]
    cfg = cfg if cfg is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/system-config":
            return httpx.Response(200, json=cfg)
        if method == "GET" and path.endswith("/supervisor-actions"):
            return httpx.Response(200, json=[])  # no recent action → not deduped
        if method == "GET" and path == f"/api/features/{fid}/changelog":
            return httpx.Response(200, json=changelog)
        if method == "PATCH" and path == f"/api/features/{fid}":
            patches.append(_json.loads(request.content) if request.content else {})
            return httpx.Response(200, json={**feature})
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


def _feature(**kw):
    base = {"id": 1954, "product_id": 33, "status": "Approved",
            "design_doc_path": None, "feature_type": "bug", "priority": 40}
    base.update(kw)
    return base


class TestDesignerBounce:
    def test_blocks_after_3_rollbacks(self, patch_client):
        feature = _feature()
        changelog = [_rollback(0.1), _rollback(1), _rollback(2)]  # 3 in-window
        patches = []
        patch_client(_mock(feature=feature, changelog=changelog, patches=patches))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])

        assert routed == 1
        assert len(patches) == 1
        assert patches[0]["status"] == "Blocked"
        assert "designer_bounce" in patches[0]["blocked_reason"]
        assert patches[0]["changed_by"] == "supervisor"

    def test_noop_below_threshold(self, patch_client):
        feature = _feature()
        changelog = [_rollback(0.1), _rollback(1)]  # only 2
        patches = []
        patch_client(_mock(feature=feature, changelog=changelog, patches=patches))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        assert routed == 0 and patches == []

    def test_rollbacks_outside_window_dont_count(self, patch_client):
        feature = _feature()
        # 1 recent + 2 well outside the 24h window → only 1 counts → no block
        changelog = [_rollback(0.1), _rollback(30), _rollback(40)]
        patches = []
        patch_client(_mock(feature=feature, changelog=changelog, patches=patches))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        assert routed == 0 and patches == []

    def test_skips_feature_with_design_doc(self, patch_client):
        # has a design doc → not in the designer pool → never inspected/blocked
        feature = _feature(design_doc_path="docs/story_1954.md")
        patches = []
        patch_client(_mock(feature=feature, changelog=[_rollback(0.1)] * 5, patches=patches))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        assert routed == 0 and patches == []

    def test_skips_non_approved(self, patch_client):
        feature = _feature(status="Implementing")
        patches = []
        patch_client(_mock(feature=feature, changelog=[_rollback(0.1)] * 5, patches=patches))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        assert routed == 0 and patches == []

    def test_dry_run_records_but_no_patch(self, patch_client):
        feature = _feature()
        changelog = [_rollback(0.1), _rollback(1), _rollback(2)]
        patches = []
        patch_client(_mock(feature=feature, changelog=changelog, patches=patches,
                           cfg={"supervisor_dry_run_only": True}))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        # counted as routed, but no Block PATCH issued
        assert routed == 1 and patches == []

    def test_disabled_flag_noop(self, patch_client):
        feature = _feature()
        changelog = [_rollback(0.1), _rollback(1), _rollback(2)]
        patches = []
        patch_client(_mock(feature=feature, changelog=changelog, patches=patches,
                           cfg={"supervisor_designer_bounce_enabled": False}))

        routed = supervisor.detect_designer_bounce(product_id=33, features=[feature])
        assert routed == 0 and patches == []
