"""Independent audit-verifier gate (Fix 2b, 2026-07-27): queues the
`audit_verifier` persona when open audit findings haven't been verified."""
import os
import sys
import importlib.util

import pytest


@pytest.fixture(scope="module")
def tools():
    os.environ.setdefault("PM_API_URL", "http://pm-api:8080")
    os.environ.setdefault("PM_USERNAME", "test")
    os.environ.setdefault("PM_PASSWORD", "test")
    spec = importlib.util.spec_from_file_location(
        "orchestrator_runtime", "deploy/orchestrator/tools.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_runtime"] = mod
    spec.loader.exec_module(mod)
    return mod


def _bug(fid, status="Pending", name="Security: foo IDOR", source="ai"):
    return {"id": fid, "feature_type": "bug", "source": source, "name": name, "status": status}


class _FakeResp:
    def __init__(self, data): self._d = data; self.is_success = True
    def json(self): return self._d


class _FakeClient:
    def __init__(self, features, comments_by_fid=None):
        self.features = features
        self.comments_by_fid = comments_by_fid or {}
        self.patched = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url, params=None):
        if "/comments" in url:
            fid = int(url.split("/")[3])
            return _FakeResp(self.comments_by_fid.get(fid, []))
        return _FakeResp(self.features)
    def patch(self, url, json=None):
        self.patched = json
        return _FakeResp({})


class TestAuditVerifierEnabled:
    def test_on_by_default(self, tools, monkeypatch):
        monkeypatch.delenv("AUDIT_VERIFIER_ENABLED", raising=False)
        assert tools._audit_verifier_enabled({"id": 1, "config": {}}) is True

    def test_env_falsy_disables(self, tools, monkeypatch):
        monkeypatch.setenv("AUDIT_VERIFIER_ENABLED", "off")
        assert tools._audit_verifier_enabled({"id": 1, "config": {}}) is False

    def test_per_product_optout(self, tools, monkeypatch):
        monkeypatch.delenv("AUDIT_VERIFIER_ENABLED", raising=False)
        assert tools._audit_verifier_enabled({"id": 1, "config": {"audit_verifier": False}}) is False


class TestAuditVerifierGate:
    def test_queues_when_unverified_audit_bug_exists(self, tools, monkeypatch):
        feats = [_bug(10), _bug(12), {"id": 3, "feature_type": "feature", "status": "Approved"}]
        fake = _FakeClient(feats, comments_by_fid={12: []})  # newest #12, no verdict
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": {}}
        tools._run_audit_verifier_gate(product, features=feats)
        assert product["run_persona_now"] == "audit_verifier"
        assert fake.patched["run_persona_now"] == "audit_verifier"

    def test_quiet_when_newest_already_verified(self, tools, monkeypatch):
        feats = [_bug(10), _bug(12)]
        fake = _FakeClient(feats, comments_by_fid={
            12: [{"author": "audit-verifier", "body": "VERDICT: KEEP"}]})
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": {}}
        tools._run_audit_verifier_gate(product, features=feats)
        assert product.get("run_persona_now") is None
        assert fake.patched is None

    def test_no_audit_bugs_no_queue(self, tools, monkeypatch):
        feats = [{"id": 3, "feature_type": "feature", "status": "Approved"},
                 {"id": 4, "feature_type": "bug", "source": "pm", "name": "manual bug", "status": "Pending"}]
        fake = _FakeClient(feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": {}}
        tools._run_audit_verifier_gate(product, features=feats)
        assert product.get("run_persona_now") is None

    def test_does_not_stomp_queued_persona(self, tools, monkeypatch):
        monkeypatch.setattr(tools, "_pm_client", lambda: (_ for _ in ()).throw(
            AssertionError("must not touch API when a persona is queued")))
        product = {"id": 1, "config": {}, "run_persona_now": "security_auditor"}
        tools._run_audit_verifier_gate(product, features=[_bug(10)])
        assert product["run_persona_now"] == "security_auditor"

    def test_disabled_no_op(self, tools, monkeypatch):
        monkeypatch.setenv("AUDIT_VERIFIER_ENABLED", "0")
        monkeypatch.setattr(tools, "_pm_client", lambda: (_ for _ in ()).throw(
            AssertionError("must not touch API when disabled")))
        product = {"id": 1, "config": {}}
        tools._run_audit_verifier_gate(product, features=[_bug(10)])
        assert product.get("run_persona_now") is None
