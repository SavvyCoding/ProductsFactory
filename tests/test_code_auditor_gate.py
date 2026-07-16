"""
Tests for the phase-boundary code-auditor trigger (2026-06-24, comment-only soak).

Covers flag resolution, the settled-phase checkpoint, and the architect-run
cadence gate (fire after _CODE_AUDIT_ARCHITECT_RUNS architect runs since the
last audit) — tight coupling so the audit always rides a fresh architect pass.
The launch path itself (run_persona_now → Priority-0) reuses the
architect/security_auditor mechanism and is exercised by the run_cycle tests.
"""

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
        "orchestrator_runtime", "deploy/orchestrator/tools.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orchestrator_runtime"] = mod
    spec.loader.exec_module(mod)
    return mod


def _ph(pid, order): return {"id": pid, "order": order, "name": f"P{pid}"}
def _ft(fid, phase_id, status): return {"id": fid, "phase_id": phase_id, "status": status}


# ── fake PM client (context manager yielding get/patch) ──────────────────────
class _FakeResp:
    def __init__(self, data): self._d = data; self.is_success = True
    def json(self): return self._d


class _FakeClient:
    def __init__(self, phases, features):
        self.phases, self.features, self.patched = phases, features, None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url):
        return _FakeResp(self.phases if url.endswith("/phases") else self.features)
    def patch(self, url, json=None):
        self.patched = json
        return _FakeResp({})


# ── flag resolution (ALWAYS ON per product; global env kill switch only) ──────
class TestCodeAuditorEnabled:
    def test_on_by_default_when_env_unset(self, tools, monkeypatch):
        monkeypatch.delenv("CODE_AUDITOR_ENABLED", raising=False)
        assert tools._code_auditor_enabled({"id": 1, "config": {}}) is True

    def test_env_falsy_kills_globally(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "off")
        assert tools._code_auditor_enabled({"id": 1, "config": {}}) is False
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "0")
        assert tools._code_auditor_enabled({"id": 1, "config": {}}) is False

    def test_per_product_config_optout_is_ignored(self, tools, monkeypatch):
        # The per-product opt-out was removed 2026-07-16 — a stale config bool
        # must NOT disable the always-on audit.
        monkeypatch.delenv("CODE_AUDITOR_ENABLED", raising=False)
        assert tools._code_auditor_enabled({"id": 1, "config": {"code_auditor": False}}) is True


# ── security_auditor SCHEDULING flag (ALWAYS ON; global env kill switch only) ──
class TestSecurityAuditorScheduledEnabled:
    def test_on_by_default_when_env_unset(self, tools, monkeypatch):
        monkeypatch.delenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", raising=False)
        assert tools._security_auditor_scheduled_enabled({"id": 1, "config": {}}) is True

    def test_env_falsy_kills_globally(self, tools, monkeypatch):
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "off")
        assert tools._security_auditor_scheduled_enabled({"id": 1, "config": {}}) is False

    def test_per_product_config_optout_is_ignored(self, tools, monkeypatch):
        monkeypatch.delenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", raising=False)
        assert tools._security_auditor_scheduled_enabled(
            {"id": 1, "config": {"security_auditor_scheduled": False}}) is True


# ── settled-phase checkpoint (pure) ──────────────────────────────────────────
class TestSelectPhaseForReview:
    def test_picks_lowest_order_settled(self, tools):
        phases = [_ph(20, 1), _ph(10, 0)]
        features = [_ft(1, 10, "Pushed"), _ft(2, 20, "Pushed")]
        assert tools._select_phase_for_review(phases, features)["id"] == 10

    def test_single_feature_phase_is_eligible(self, tools):
        # The size floor is NOT here anymore — it's the cumulative cadence gate.
        phases = [_ph(10, 0)]
        assert tools._select_phase_for_review(phases, [_ft(1, 10, "Pushed")])["id"] == 10

    def test_skips_active_phase(self, tools):
        phases = [_ph(10, 0)]
        features = [_ft(1, 10, "Pushed"), _ft(2, 10, "Implementing")]
        assert tools._select_phase_for_review(phases, features) is None

    def test_skips_phase_without_pushed(self, tools):
        phases = [_ph(10, 0)]
        features = [_ft(1, 10, "Blocked"), _ft(2, 10, "Rejected")]
        assert tools._select_phase_for_review(phases, features) is None

    def test_empty_inputs_return_none(self, tools):
        assert tools._select_phase_for_review([], []) is None
        assert tools._select_phase_for_review(None, None) is None


# ── cumulative cadence gate ──────────────────────────────────────────────────
class TestPhaseReviewGateCadence:
    def test_fires_at_cadence(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "0")  # isolate code_auditor
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        phases = [_ph(10, 0)]
        feats = [_ft(i, 10, "Pushed") for i in range(2)]      # settled phase exists
        fake = _FakeClient(phases, feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        # exactly N architect runs since the last audit (0) → fire, riding the fresh pass
        product = {"id": 1, "config": {"architect_run_count": N}}
        tools._run_phase_review_gate(product, features=feats)
        assert product["run_persona_now"] == "code_auditor"
        assert fake.patched["config"]["architect_runs_at_last_audit"] == N

    def test_waits_below_cadence(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "0")  # isolate code_auditor
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        phases = [_ph(10, 0)]
        feats = [_ft(i, 10, "Pushed") for i in range(2)]
        fake = _FakeClient(phases, feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": {"architect_run_count": N - 1}}  # one short → wait
        tools._run_phase_review_gate(product, features=feats)
        assert product.get("run_persona_now") is None
        assert fake.patched is None

    def test_counts_runs_since_last_audit(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "0")  # isolate code_auditor
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        phases = [_ph(10, 0)]
        feats = [_ft(i, 10, "Pushed") for i in range(2)]
        fake = _FakeClient(phases, feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        # audited at run 10; only N-1 architect runs since → still below cadence
        product = {"id": 1, "config": {"architect_run_count": 10 + (N - 1),
                                       "architect_runs_at_last_audit": 10}}
        tools._run_phase_review_gate(product, features=feats)
        assert product.get("run_persona_now") is None
        assert fake.patched is None

    def test_no_settled_phase_never_fires(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "0")  # isolate code_auditor
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        phases = [_ph(10, 0)]
        feats = [_ft(i, 10, "Implementing") for i in range(8)]  # plenty, but not settled
        fake = _FakeClient(phases, feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": {"architect_run_count": N + 99}}
        tools._run_phase_review_gate(product, features=feats)
        assert product.get("run_persona_now") is None
        assert fake.patched is None


# ── two disjoint auditors ride the same gate (security + code) ───────────────
class TestPhaseReviewGateBothAuditors:
    def _setup(self, tools, monkeypatch, config):
        phases = [_ph(10, 0)]
        feats = [_ft(i, 10, "Pushed") for i in range(2)]
        fake = _FakeClient(phases, feats)
        monkeypatch.setattr(tools, "_pm_client", lambda: fake)
        product = {"id": 1, "config": config}
        tools._run_phase_review_gate(product, features=feats)
        return product, fake

    def test_security_alone_fires_security(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "0")  # only security enabled
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "1")
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        product, fake = self._setup(tools, monkeypatch, {"architect_run_count": N})
        assert product["run_persona_now"] == "security_auditor"
        assert fake.patched["config"]["architect_runs_at_last_security_audit"] == N
        # code_auditor counter untouched
        assert "architect_runs_at_last_audit" not in fake.patched["config"]

    def test_both_due_security_wins_this_cycle(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "1")
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        product, fake = self._setup(tools, monkeypatch, {"architect_run_count": N})
        # serialized: security fires now, code_auditor counter left due for next cycle
        assert product["run_persona_now"] == "security_auditor"
        assert fake.patched["config"]["architect_runs_at_last_security_audit"] == N
        assert "architect_runs_at_last_audit" not in fake.patched["config"]

    def test_security_current_falls_through_to_code(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "1")
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        # security already audited at the current architect run → not due; code is
        product, fake = self._setup(tools, monkeypatch, {
            "architect_run_count": N,
            "architect_runs_at_last_security_audit": N,
        })
        assert product["run_persona_now"] == "code_auditor"
        assert fake.patched["config"]["architect_runs_at_last_audit"] == N

    def test_both_below_cadence_waits(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "1")
        N = tools._CODE_AUDIT_ARCHITECT_RUNS
        product, fake = self._setup(tools, monkeypatch, {"architect_run_count": N - 1})
        assert product.get("run_persona_now") is None
        assert fake.patched is None


# ── disabled → no-op (never queues, never calls the API) ─────────────────────
class TestPhaseReviewGateNoopWhenDisabled:
    def test_disabled_does_not_call_api(self, tools, monkeypatch):
        # Both auditors are always-on by default now, so "disabled" means BOTH
        # global env kill switches are set falsy.
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "0")
        monkeypatch.setenv("SECURITY_AUDITOR_SCHEDULED_ENABLED", "0")
        monkeypatch.setattr(tools, "_pm_client", lambda: (_ for _ in ()).throw(
            AssertionError("_pm_client must not be called when disabled")))
        product = {"id": 1, "config": {}}
        tools._run_phase_review_gate(product, features=[])
        assert product.get("run_persona_now") is None

    def test_does_not_stomp_queued_persona(self, tools, monkeypatch):
        monkeypatch.setenv("CODE_AUDITOR_ENABLED", "1")
        monkeypatch.setattr(tools, "_pm_client", lambda: (_ for _ in ()).throw(
            AssertionError("must not touch API when a persona is already queued")))
        product = {"id": 1, "config": {}, "run_persona_now": "architect"}
        tools._run_phase_review_gate(product, features=[])
        assert product["run_persona_now"] == "architect"
