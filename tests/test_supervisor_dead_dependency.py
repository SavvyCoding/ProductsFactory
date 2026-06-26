"""
detect_dead_dependency (2026-06-25): a feature waiting for dispatch (Approved/
Designed) whose depends_on target is terminal-non-Pushed (Rejected/Reverted/
Deferred) is frozen forever — the dependency gate only releases on Pushed. Block
the dependent for PM review instead of leaving it silently stuck.

Canonical: HomeChoreService #1786 (Designed, depends_on the Rejected #1785).
The dep target's status is read from the same features payload — no extra calls.
"""

import os

import httpx
import pytest

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator import supervisor


def _mock(*, patches, cfg=None):
    cfg = cfg if cfg is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        path, method = request.url.path, request.method
        if method == "GET" and path == "/api/system-config":
            return httpx.Response(200, json=cfg)
        if method == "GET" and path.endswith("/supervisor-actions"):
            return httpx.Response(200, json=[])
        if method == "PATCH" and path.startswith("/api/features/"):
            patches.append({"path": path, **(_json.loads(request.content) if request.content else {})})
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


def _f(fid, status, depends_on=None):
    return {"id": fid, "product_id": 33, "status": status, "depends_on": depends_on}


class TestDeadDependency:
    def test_blocks_designed_depending_on_rejected(self, patch_client):
        feats = [_f(1786, "Designed", depends_on=1785), _f(1785, "Rejected")]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 1
        assert len(patches) == 1
        assert patches[0]["path"] == "/api/features/1786"
        assert patches[0]["status"] == "Blocked"
        assert "dead_dependency" in patches[0]["blocked_reason"]
        assert "#1785" in patches[0]["blocked_reason"]

    @pytest.mark.parametrize("dep_status", ["Rejected", "Reverted", "Deferred"])
    def test_all_terminal_dep_statuses_trigger(self, patch_client, dep_status):
        feats = [_f(2, "Approved", depends_on=1), _f(1, dep_status)]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 1 and patches[0]["status"] == "Blocked"

    def test_pushed_dep_is_fine(self, patch_client):
        feats = [_f(2, "Designed", depends_on=1), _f(1, "Pushed")]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 0 and patches == []

    def test_active_dep_is_fine(self, patch_client):
        # dep still in flight (Implementing) → not terminal → leave it
        feats = [_f(2, "Designed", depends_on=1), _f(1, "Implementing")]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 0 and patches == []

    def test_in_session_dependent_is_not_disrupted(self, patch_client):
        # dependent is mid-session (Implementing) → don't yank it even if dep is dead
        feats = [_f(2, "Implementing", depends_on=1), _f(1, "Rejected")]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 0 and patches == []

    def test_no_dependency_noop(self, patch_client):
        feats = [_f(2, "Approved", depends_on=None), _f(1, "Rejected")]
        patches = []
        patch_client(_mock(patches=patches))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 0 and patches == []

    def test_dry_run_records_but_no_patch(self, patch_client):
        feats = [_f(1786, "Designed", depends_on=1785), _f(1785, "Rejected")]
        patches = []
        patch_client(_mock(patches=patches, cfg={"supervisor_dry_run_only": True}))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 1 and patches == []

    def test_disabled_flag_noop(self, patch_client):
        feats = [_f(1786, "Designed", depends_on=1785), _f(1785, "Rejected")]
        patches = []
        patch_client(_mock(patches=patches, cfg={"supervisor_dead_dependency_enabled": False}))
        routed = supervisor.detect_dead_dependency(product_id=33, features=feats)
        assert routed == 0 and patches == []
