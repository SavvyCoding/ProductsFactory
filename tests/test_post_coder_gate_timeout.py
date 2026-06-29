"""
Tests for the self-calibrating post-coder test-gate timeout
(orchestrator/pipelines/post_coder.py), added 2026-06-28.

Why it exists: a single fixed gate timeout (300s) can't serve a factory whose
suites grow without bound. HomeChoreService (2026-06-27) crossed a static 300s
once its suite passed ~490 tests; the resulting exit-124 was (then) classified
env_broken and every feature shipped 0/1 via the PR-laundering path. The budget
now self-calibrates off observed green-run wall-time, with an explicit
per-product `config.test_gate_timeout` override as the operator stopgap.

Pure / mock-based (no Docker, no DB) — runs on every platform.
"""

import os
import json
import pytest
import httpx

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines import post_coder as pc  # noqa: E402


# ── _percentile (pure) ───────────────────────────────────────────────────────
class TestPercentile:
    def test_empty_is_zero(self):
        assert pc._percentile([], 95) == 0.0

    def test_filters_nonpositive_none_and_bool(self):
        # 0, negatives, None and bool are not valid samples.
        assert pc._percentile([0, -5, None, True, 10, 20], 95) == 20.0

    def test_nearest_rank_p95(self):
        assert pc._percentile(list(range(1, 101)), 95) == 95.0

    def test_unsorted_input_ok(self):
        assert pc._percentile([30, 10, 20], 100) == 30.0

    def test_single_sample(self):
        assert pc._percentile([42.0], 95) == 42.0


# ── _resolve_test_gate_timeout (pure) ────────────────────────────────────────
class TestResolveTestGateTimeout:
    def test_no_history_is_floor(self):
        assert pc._resolve_test_gate_timeout({"config": {}}) == pc.TEST_GATE_TIMEOUT_FLOOR

    def test_missing_config_is_floor(self):
        assert pc._resolve_test_gate_timeout({}) == pc.TEST_GATE_TIMEOUT_FLOOR

    def test_explicit_override_wins_over_history(self):
        p = {"config": {"test_gate_timeout": 900, "test_gate_runtimes": [50] * 10}}
        assert pc._resolve_test_gate_timeout(p) == 900

    def test_bool_override_is_ignored(self):
        # True is an int subclass — must not be treated as a 1s override.
        p = {"config": {"test_gate_timeout": True}}
        assert pc._resolve_test_gate_timeout(p) == pc.TEST_GATE_TIMEOUT_FLOOR

    def test_fast_suite_clamps_to_floor(self):
        # p95 ~40s * MARGIN < FLOOR → floor.
        p = {"config": {"test_gate_runtimes": [35, 40, 38, 41, 39, 37, 42, 40, 36, 40]}}
        assert pc._resolve_test_gate_timeout(p) == pc.TEST_GATE_TIMEOUT_FLOOR

    def test_growing_suite_scales_between_bounds(self):
        # The HomeChoreService case: p95 ~290s * 2.5 ≈ 725, inside (floor, ceiling).
        p = {"config": {"test_gate_runtimes":
                        [250, 270, 280, 290, 260, 275, 285, 290, 265, 280]}}
        out = pc._resolve_test_gate_timeout(p)
        assert pc.TEST_GATE_TIMEOUT_FLOOR < out < pc.TEST_GATE_TIMEOUT_CEILING

    def test_huge_suite_clamps_to_ceiling(self):
        p = {"config": {"test_gate_runtimes": [900] * 10}}
        assert pc._resolve_test_gate_timeout(p) == pc.TEST_GATE_TIMEOUT_CEILING

    def test_budget_uses_prior_runs_not_current(self):
        # A budget derived from history of ~120s green runs would NOT cover a
        # one-off 10000s run — that run trips its own historical budget (a real
        # perf-regression signal), which is the point.
        p = {"config": {"test_gate_runtimes": [120] * 10}}
        assert pc._resolve_test_gate_timeout(p) < 10000


# ── _record_test_gate_runtime (mocked PM API) ────────────────────────────────
@pytest.fixture
def patch_pc_client(monkeypatch):
    real = httpx.Client

    def install(transport):
        monkeypatch.setattr(
            pc.httpx, "Client",
            lambda *a, **k: real(*a, **{**k, "transport": transport}),
        )
    return install


def _product_transport(pid, *, server_cfg, captured):
    def handler(request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "GET" and path == f"/api/products/{pid}":
            return httpx.Response(200, json={"id": pid, "config": server_cfg})
        if method == "PATCH" and path == f"/api/products/{pid}":
            body = json.loads(request.content) if request.content else {}
            captured["config"] = body.get("config")
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unmocked {method} {path}"})
    return httpx.MockTransport(handler)


class TestRecordTestGateRuntime:
    def test_first_sample_from_empty(self, patch_pc_client):
        captured = {}
        patch_pc_client(_product_transport(7, server_cfg={}, captured=captured))
        pc._record_test_gate_runtime({"id": 7, "config": {}}, 88.0)
        assert captured["config"]["test_gate_runtimes"] == [88.0]

    def test_appends_and_keeps_rolling_window(self, patch_pc_client):
        captured = {}
        # Re-reads the FRESH server config (not the passed-in product.config).
        server_cfg = {"test_gate_runtimes": [float(i) for i in range(10)]}  # 0..9
        patch_pc_client(_product_transport(7, server_cfg=server_cfg, captured=captured))
        pc._record_test_gate_runtime({"id": 7, "config": {"test_gate_runtimes": []}}, 123.4)
        runtimes = captured["config"]["test_gate_runtimes"]
        assert len(runtimes) == pc.TEST_GATE_RUNTIME_SAMPLES   # capped at N
        assert runtimes[-1] == 123.4                            # newest appended
        assert 0.0 not in runtimes                              # oldest dropped

    def test_records_even_under_override(self, patch_pc_client):
        # The override wins for the BUDGET, but history is still recorded so
        # dropping the override later hands off to the auto-budget instead of
        # resetting to FLOOR (the bootstrap chicken-and-egg).
        captured = {}
        patch_pc_client(_product_transport(
            7, server_cfg={"test_gate_timeout": 900, "test_gate_runtimes": [400.0]},
            captured=captured))
        pc._record_test_gate_runtime({"id": 7, "config": {}}, 410.0)
        assert captured["config"]["test_gate_runtimes"] == [400.0, 410.0]
        # The override itself is preserved through the merge.
        assert captured["config"]["test_gate_timeout"] == 900

    @pytest.mark.parametrize("bad", [0, -1, None, True, False])
    def test_noop_on_invalid_duration(self, patch_pc_client, bad):
        captured = {}
        patch_pc_client(_product_transport(7, server_cfg={}, captured=captured))
        pc._record_test_gate_runtime({"id": 7, "config": {}}, bad)
        assert "config" not in captured

    def test_noop_without_product_id(self, patch_pc_client):
        captured = {}
        patch_pc_client(_product_transport(7, server_cfg={}, captured=captured))
        pc._record_test_gate_runtime({"config": {}}, 50.0)
        assert "config" not in captured
