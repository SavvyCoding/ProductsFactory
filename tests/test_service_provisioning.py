"""
Service provisioning (Phases A-C, 2026-06-12): orchestrator/services.py,
the infra-story executor, the service_missing triage, and the infra
exclusion at both selection points.
"""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator import services  # noqa: E402
from orchestrator.pipelines.post_coder import _detect_missing_service  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "orchestrator"))
import tools as orch_tools  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ── catalog + pure helpers ───────────────────────────────────────────────────


class TestCatalogAndEnv:
    def test_catalog_entries_have_required_keys(self):
        for name, entry in services.SERVICE_CATALOG.items():
            for key in ("image", "port", "env_var", "url_template", "run_env", "ready"):
                assert key in entry, f"{name} missing {key}"
            # Images must be pinned (tag present) — the allowlist is the
            # security boundary.
            assert ":" in entry["image"], f"{name} image is not tag-pinned"

    def test_declared_services_filters_to_catalog(self):
        product = {"config": {"services": ["redis", "memcached", 42, "postgres"]}}
        assert services.declared_services(product) == ["redis", "postgres"]

    def test_declared_services_handles_absent_config(self):
        assert services.declared_services({}) == []
        assert services.declared_services({"config": None}) == []
        assert services.declared_services({"config": {"services": "redis"}}) == []

    def test_session_service_env_builds_urls(self):
        product = {"config": {"services": ["redis"]}}
        env = services.session_service_env(product, "abc12345")
        assert env == {"REDIS_URL": "redis://pf-svc-abc12345-redis:6379/0"}

    def test_session_service_env_empty_when_none_declared(self):
        assert services.session_service_env({"config": {}}, "abc") == {}


# ── ensure / teardown / reap (mocked docker) ─────────────────────────────────


class TestEnsureTeardownReap:
    def test_ensure_starts_and_returns_env(self):
        product = {"config": {"services": ["redis"]}, "name": "P"}
        with patch("orchestrator.services.subprocess.run") as run:
            # docker run -d → ok; docker exec ready → ok
            run.side_effect = [_completed(0, stdout="cid"), _completed(0, stdout="PONG")]
            env = services.ensure_session_services(product, "u1")
        assert env == {"REDIS_URL": "redis://pf-svc-u1-redis:6379/0"}
        started = run.call_args_list[0][0][0]
        assert started[:4] == ["docker", "run", "-d", "--rm"]
        assert "redis:7-alpine" in started
        assert "--network" in started and "productfactory-net" in started

    def test_ensure_skips_service_on_start_failure(self):
        product = {"config": {"services": ["redis"]}, "name": "P"}
        with patch("orchestrator.services.subprocess.run") as run:
            run.return_value = _completed(1, stderr="pull access denied")
            env = services.ensure_session_services(product, "u1")
        assert env == {}

    def test_ensure_removes_container_when_never_ready(self):
        product = {"config": {"services": ["redis"]}, "name": "P"}
        with patch("orchestrator.services.subprocess.run") as run, \
             patch("orchestrator.services._READY_DEADLINE_S", 0):
            run.side_effect = [_completed(0), _completed(0)]  # run -d, rm -f
            env = services.ensure_session_services(product, "u1")
        assert env == {}
        rm_call = run.call_args_list[-1][0][0]
        assert rm_call[:3] == ["docker", "rm", "-f"]

    def test_teardown_removes_session_containers(self):
        with patch("orchestrator.services.subprocess.run") as run:
            run.side_effect = [_completed(0, stdout="aaa\nbbb\n"), _completed(0)]
            services.teardown_session_services("u1")
        ps_call = run.call_args_list[0][0][0]
        assert "name=pf-svc-u1-" in ps_call
        rm_call = run.call_args_list[1][0][0]
        assert rm_call[:3] == ["docker", "rm", "-f"] and "aaa" in rm_call

    def test_reap_spares_active_and_kills_orphans(self):
        with patch("orchestrator.services.subprocess.run") as run:
            run.side_effect = [
                _completed(0, stdout="pf-svc-live1-redis\npf-svc-dead2-redis\n"),
                _completed(0),  # rm for the orphan
            ]
            reaped = services.reap_orphan_services({"live1"})
        assert reaped == 1
        rm_call = run.call_args_list[1][0][0]
        assert "pf-svc-dead2-redis" in rm_call


# ── _detect_missing_service (Phase C pure function) ──────────────────────────


class TestDetectMissingService:
    def test_dogtinder_redis_shape_detected(self):
        out = ("FAILED tests/test_redis_availability.py::test_redis_server_ping "
               "- AssertionError: redis-cli PING failed: Could not connect to "
               "Redis at 127.0.0.1:6379: Connection refused")
        assert _detect_missing_service(out) == "redis"

    def test_postgres_detected(self):
        out = ("sqlalchemy.exc.OperationalError: connection to server at "
               "\"localhost\" (127.0.0.1), port 5432 failed: Connection refused"
               "\nIs the postgres server running?")
        # port appears as ':5432' form required — build matching output
        out2 = "could not connect to postgres at localhost:5432: Connection refused"
        assert _detect_missing_service(out2) == "postgres"

    def test_generic_econnrefused_not_flagged(self):
        # App-under-test not started — coder's bug, not a service gap.
        out = "httpx.ConnectError: Connection refused connecting to localhost:8000"
        assert _detect_missing_service(out) is None

    def test_service_name_without_port_not_flagged(self):
        out = "redis tests failed: assertion error in scoring"
        assert _detect_missing_service(out) is None

    def test_empty_output(self):
        assert _detect_missing_service("") is None


# ── infra-story executor ─────────────────────────────────────────────────────


class _FakePMClient:
    def __init__(self, product_payload):
        self.product_payload = product_payload
        self.patches = []
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, **kw):
        r = MagicMock()
        r.is_success = True
        r.json.return_value = self.product_payload
        return r

    def patch(self, path, json=None, **kw):
        self.patches.append((path, json))
        return MagicMock(is_success=True, status_code=200)

    def post(self, path, json=None, **kw):
        self.posts.append((path, json))
        return MagicMock(is_success=True, status_code=200)


class TestInfraExecutor:
    def _product(self):
        return {"id": 7, "name": "P", "config": {}}

    def test_executes_recognized_service(self):
        fake = _FakePMClient({"id": 7, "config": {}})
        feats = [{"id": 101, "feature_type": "infra", "status": "Approved",
                  "name": "Provision redis service", "description": ""}]
        with patch.object(orch_tools, "_pm_client", return_value=fake), \
             patch("orchestrator.services.ensure_session_services",
                   return_value={"REDIS_URL": "redis://x:6379/0"}) as ens, \
             patch("orchestrator.services.teardown_session_services") as tear:
            n = orch_tools._execute_infra_stories(self._product(), feats)
        assert n == 1
        ens.assert_called_once()
        tear.assert_called_once()
        cfg_patch = next(j for p, j in fake.patches if p == "/api/products/7")
        assert cfg_patch["config"]["services"] == ["redis"]
        feat_patch = next(j for p, j in fake.patches if p == "/api/features/101")
        assert feat_patch["status"] == "Pushed"
        assert "redis:7-alpine" in feat_patch["merge_notes"]

    def test_unrecognized_service_blocked(self):
        fake = _FakePMClient({"id": 7, "config": {}})
        feats = [{"id": 102, "feature_type": "infra", "status": "Approved",
                  "name": "Provision kafka cluster", "description": ""}]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._execute_infra_stories(self._product(), feats)
        assert n == 0
        feat_patch = next(j for p, j in fake.patches if p == "/api/features/102")
        assert feat_patch["status"] == "Blocked"
        assert "no catalog service" in feat_patch["blocked_reason"]

    def test_smoke_failure_leaves_approved_and_alerts(self):
        fake = _FakePMClient({"id": 7, "config": {}})
        feats = [{"id": 103, "feature_type": "infra", "status": "Approved",
                  "name": "Provision redis service", "description": ""}]
        with patch.object(orch_tools, "_pm_client", return_value=fake), \
             patch("orchestrator.services.ensure_session_services", return_value={}), \
             patch("orchestrator.services.teardown_session_services"):
            n = orch_tools._execute_infra_stories(self._product(), feats)
        assert n == 0
        assert not any(p == "/api/features/103" for p, _ in fake.patches)
        assert any(p == "/api/alerts" for p, _ in fake.posts)

    def test_non_infra_and_non_approved_ignored(self):
        fake = _FakePMClient({"id": 7, "config": {}})
        feats = [
            {"id": 1, "feature_type": "feature", "status": "Approved", "name": "redis thing"},
            {"id": 2, "feature_type": "infra", "status": "Pending", "name": "Provision redis"},
        ]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            assert orch_tools._execute_infra_stories(self._product(), feats) == 0
        assert fake.patches == []


# ── infra exclusion at the session selector ─────────────────────────────────


class TestInfraExcludedFromSelector:
    def test_fetch_assigned_features_skips_infra(self):
        from orchestrator import docker_runner

        infra = {"id": 1, "feature_type": "infra", "status": "Designed",
                 "name": "Provision redis service", "priority": 1, "phase_id": None}
        real = {"id": 2, "feature_type": "feature", "status": "Designed",
                "name": "Real story", "priority": 50, "phase_id": None,
                "design_doc_path": "docs/story_2.md"}

        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, path, **kw):
                r = MagicMock()
                r.is_success = True
                r.raise_for_status = lambda: None
                if path.endswith("/features"):
                    r.json.return_value = [infra, real]
                elif path.endswith("/phases"):
                    r.json.return_value = []
                else:
                    r.json.return_value = {"id": 7, "config": {}}
                return r

        with patch("orchestrator.docker_runner.httpx.Client", return_value=_C()):
            feats, _, _ = docker_runner._fetch_assigned_features(7, "coder")
        ids = [f["id"] for f in feats]
        assert 2 in ids and 1 not in ids


# ── blocked-feature re-processor (wave-8) ───────────────────────────────────


class _ReprocFake:
    def __init__(self, comments_by_fid=None):
        self.comments_by_fid = comments_by_fid or {}
        self.patches = []
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, **kw):
        r = MagicMock(is_success=True, status_code=200)
        if "/comments" in path:
            fid = int(path.split("/")[3])
            r.json.return_value = self.comments_by_fid.get(fid, [])
        else:
            r.json.return_value = {}
        return r

    def patch(self, path, json=None, **kw):
        self.patches.append((path, json))
        return MagicMock(is_success=True, status_code=200)

    def post(self, path, json=None, **kw):
        self.posts.append((path, json))
        return MagicMock(is_success=True, status_code=200)


def _blocked(fid, reason):
    return {"id": fid, "status": "Blocked", "blocked_reason": reason}


class TestBlockedReprocessor:
    def _product(self, **cfg):
        return {"id": 7, "name": "P", "config": cfg}

    def test_enabled_by_default(self):
        # Default ON for all products (2026-06-13): no env, no config.
        fake = _ReprocFake()
        feats = [_blocked(1, "Auto-blocked by supervisor.divergent_review_feedback: ...")]
        with patch.dict(os.environ, {}, clear=False), \
             patch.object(orch_tools, "_pm_client", return_value=fake):
            os.environ.pop("BLOCKED_REPROCESSOR_ENABLED", None)
            n = orch_tools._reprocess_blocked_features(self._product(), feats)
        assert n == 1

    def test_env_kill_switch_disables(self):
        # BLOCKED_REPROCESSOR_ENABLED set to a falsy value disables everywhere.
        fake = _ReprocFake()
        feats = [_blocked(1, "supervisor.divergent_review_feedback")]
        with patch.dict(os.environ, {"BLOCKED_REPROCESSOR_ENABLED": "0"}), \
             patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(self._product(), feats)
        assert n == 0
        assert fake.patches == []

    def test_divergent_unblocked_clean(self):
        fake = _ReprocFake()
        feats = [_blocked(1, "Auto-blocked by supervisor.divergent_review_feedback: ...")]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats)
        assert n == 1
        _, body = fake.patches[0]
        assert body["status"] == "Approved"
        assert body["design_doc_path"] is None
        assert body["fix_attempts"] == 0

    def test_code_quality_flap_sets_escalation_threshold(self):
        fake = _ReprocFake(comments_by_fid={
            1: [{"author": "lint-guard", "body": "x"},
                {"author": "post-coder:test-check", "body": "y"}]})
        feats = [_blocked(1, "Auto-blocked by supervisor.rapid_flap: cycled 10 times. "
                             "Recent bounce authors: 1x lint-guard, 1x post-coder:test-check.")]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats)
        assert n == 1
        _, body = fake.patches[0]
        # Implementing (NOT Approved — →Approved would zero fix_attempts and
        # defeat escalation) + changes_requested makes it coder-eligible.
        assert body["status"] == "Implementing"
        assert body["review_outcome"] == "changes_requested"
        assert body["fix_attempts"] == 4  # escalation threshold → diagnose-first next run
        assert "design_doc_path" not in body  # doc kept for code-quality retry

    def test_env_block_skipped(self):
        fake = _ReprocFake()
        feats = [_blocked(1, "Tests require a live redis service that is not declared "
                             "(service-missing). Not coder-fixable.")]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats)
        assert n == 0
        assert fake.patches == []

    def test_dedup_one_shot(self):
        fake = _ReprocFake(comments_by_fid={
            1: [{"author": "blocked-reprocessor-v2", "body": "already retried"}]})
        feats = [_blocked(1, "Auto-blocked by supervisor.divergent_review_feedback: ...")]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats)
        assert n == 0

    def test_per_cycle_cap(self):
        fake = _ReprocFake()
        feats = [_blocked(i, "supervisor.divergent_review_feedback") for i in range(1, 6)]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats, max_per_cycle=2)
        assert n == 2

    def test_env_flag_enables(self):
        fake = _ReprocFake()
        feats = [_blocked(1, "supervisor.divergent_review_feedback")]
        with patch.dict(os.environ, {"BLOCKED_REPROCESSOR_ENABLED": "1"}), \
             patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(self._product(), feats)
        assert n == 1

    def test_config_false_overrides_env_on(self):
        fake = _ReprocFake()
        feats = [_blocked(1, "supervisor.divergent_review_feedback")]
        with patch.dict(os.environ, {"BLOCKED_REPROCESSOR_ENABLED": "1"}), \
             patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=False), feats)
        assert n == 0  # explicit per-product opt-out wins

    def test_failed_unblock_does_not_spend_one_shot(self):
        # The v1 bug: a 422'd unblock still posted the dedup marker, falsely
        # spending the retry. A non-2xx PATCH must NOT mark or count.
        class _RejectFake(_ReprocFake):
            def patch(self, path, json=None, **kw):
                self.patches.append((path, json))
                return MagicMock(is_success=False, status_code=422)
        fake = _RejectFake()
        feats = [_blocked(1, "supervisor.divergent_review_feedback")]
        with patch.object(orch_tools, "_pm_client", return_value=fake):
            n = orch_tools._reprocess_blocked_features(
                self._product(blocked_reprocessor=True), feats)
        assert n == 0
        assert fake.posts == []  # no dedup marker posted
