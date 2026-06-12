"""
Wave-5 blocked-feature triage (2026-06-12): flap reason enrichment,
stale-design-doc invalidation, tool_missing terminal routing, the
capabilities manifest, and the diagnose-first escalation router.
"""

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.capabilities import AGENT_IMAGE_TOOLS, is_obtainable_tool  # noqa: E402
from orchestrator.pipelines.post_coder import _detect_missing_tool  # noqa: E402
from orchestrator import docker_runner  # noqa: E402
from orchestrator import supervisor  # noqa: E402


# ── capabilities manifest ────────────────────────────────────────────────────


class TestCapabilitiesManifest:
    def test_docker_is_not_obtainable(self):
        # Deliberate per the security model — the basis of tool_missing routing.
        assert not is_obtainable_tool("docker")

    def test_baked_tools_are_obtainable(self):
        for tool in ("ruff", "pre-commit", "pytest", "npx", "actionlint", "hadolint"):
            assert is_obtainable_tool(tool), tool

    def test_per_project_runners_are_obtainable(self):
        # jest/vitest arrive via npm ci — missing means env hiccup, not terminal.
        assert is_obtainable_tool("jest") and is_obtainable_tool("vitest")


# ── _detect_missing_tool ─────────────────────────────────────────────────────


class TestDetectMissingTool:
    def test_sh_command_not_found(self):
        assert _detect_missing_tool("sh: docker: command not found") == "docker"

    def test_bare_command_not_found(self):
        out = "docker: command not found\nFAILED tests/test_docker_probe.py"
        assert _detect_missing_tool(out) == "docker"

    def test_path_assertion_shape(self):
        out = "AssertionError: 'ruff' not on PATH"
        assert _detect_missing_tool(out) == "ruff"

    def test_filenotfound_shape(self):
        out = "FileNotFoundError: [Errno 2] No such file or directory: 'kubectl'"
        assert _detect_missing_tool(out) == "kubectl"

    def test_regular_failure_not_matched(self):
        assert _detect_missing_tool("AssertionError: expected 3 == 4") is None

    def test_empty(self):
        assert _detect_missing_tool("") is None


# ── stale design-doc invalidation ────────────────────────────────────────────


class TestStaleDocInvalidation:
    def test_deletes_both_filename_forms(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "story_042.md").write_text("stale zero-padded", encoding="utf-8")
        (docs / "story_43.md").write_text("stale un-padded", encoding="utf-8")
        (docs / "story_999.md").write_text("unrelated — stays", encoding="utf-8")

        n = docker_runner._invalidate_stale_design_docs(
            str(tmp_path), [{"id": 42}, {"id": 43}])
        assert n == 2
        assert not (docs / "story_042.md").exists()
        assert not (docs / "story_43.md").exists()
        assert (docs / "story_999.md").exists()

    def test_missing_docs_dir_is_noop(self, tmp_path):
        assert docker_runner._invalidate_stale_design_docs(
            str(tmp_path), [{"id": 1}]) == 0

    def test_non_int_ids_skipped(self, tmp_path):
        assert docker_runner._invalidate_stale_design_docs(
            str(tmp_path), [{"id": None}, {"name": "x"}]) == 0


# ── escalation: addendum contract + diagnosis router ─────────────────────────


class _FakeClient:
    def __init__(self, comments_by_fid):
        self.comments_by_fid = comments_by_fid
        self.patches = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, **kw):
        fid = int(path.split("/")[3])
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = self.comments_by_fid.get(fid, [])
        return r

    def patch(self, path, json=None, **kw):
        self.patches.append((path, json))
        return MagicMock(status_code=200)


class TestEscalation:
    def test_addendum_carries_the_contract(self):
        for token in ("ESCALATION-DIAGNOSIS:", "spec_defect", "env_impossible",
                      "fixable", "DIAGNOSE FIRST"):
            assert token in docker_runner._ESCALATION_ADDENDUM, token

    def test_spec_defect_routes_to_designer(self):
        fake = _FakeClient({7: [
            {"body": "ESCALATION-DIAGNOSIS: spec_defect — AC2 contradicts AC4",
             "created_at": "2026-06-12T01:00:00"},
        ]})
        with patch("orchestrator.docker_runner.httpx.Client", return_value=fake):
            docker_runner._route_escalation_diagnosis({"name": "P"}, [{"id": 7}])
        path, body = fake.patches[0]
        assert path == "/api/features/7"
        assert body["status"] == "Approved"
        assert body["design_doc_path"] is None
        assert body["changed_by"] == "supervisor"

    def test_env_impossible_blocks_with_diagnosis(self):
        fake = _FakeClient({8: [
            {"body": "ESCALATION-DIAGNOSIS: env_impossible — needs live LDAP",
             "created_at": "2026-06-12T01:00:00"},
        ]})
        with patch("orchestrator.docker_runner.httpx.Client", return_value=fake):
            docker_runner._route_escalation_diagnosis({"name": "P"}, [{"id": 8}])
        path, body = fake.patches[0]
        assert body["status"] == "Blocked"
        assert "live LDAP" in body["blocked_reason"]

    def test_fixable_and_no_marker_are_noops(self):
        fake = _FakeClient({
            9: [{"body": "ESCALATION-DIAGNOSIS: fixable — off-by-one in pagination",
                 "created_at": "2026-06-12T01:00:00"}],
            10: [{"body": "regular bounce comment", "created_at": "2026-06-12T01:00:00"}],
        })
        with patch("orchestrator.docker_runner.httpx.Client", return_value=fake):
            docker_runner._route_escalation_diagnosis(
                {"name": "P"}, [{"id": 9}, {"id": 10}])
        assert fake.patches == []

    def test_latest_marker_wins(self):
        fake = _FakeClient({11: [
            {"body": "ESCALATION-DIAGNOSIS: fixable — first guess",
             "created_at": "2026-06-12T01:00:00"},
            {"body": "ESCALATION-DIAGNOSIS: env_impossible — needs docker daemon",
             "created_at": "2026-06-12T02:00:00"},
        ]})
        with patch("orchestrator.docker_runner.httpx.Client", return_value=fake):
            docker_runner._route_escalation_diagnosis({"name": "P"}, [{"id": 11}])
        assert fake.patches[0][1]["status"] == "Blocked"


# ── flap detector: enriched reason + author signature ────────────────────────


class _FlapClient:
    def __init__(self, comments):
        self.comments = comments
        self.patches = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def get(self, path, **kw):
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = self.comments
        return r

    def patch(self, path, json=None, **kw):
        self.patches.append((path, json))
        return MagicMock(status_code=200)


class TestFlapReasonEnrichment:
    def _cfg(self):
        return {
            "supervisor_rapid_flap_enabled": True,
            "supervisor_dry_run_only": False,
            "supervisor_rapid_flap_window_hours": 1,
            "supervisor_rapid_flap_min_transitions": 5,
        }

    def test_blocked_reason_carries_counts_and_authors(self):
        fake = _FlapClient([
            {"author": "post-coder:test-env", "body": "x"},
            {"author": "post-coder:test-env", "body": "y"},
            {"author": "lint-guard", "body": "z"},
            {"author": "pm", "body": "operator note — excluded"},
        ])
        with patch.object(supervisor, "_get_supervisor_config", return_value=self._cfg()), \
             patch.object(supervisor, "_recent_action", return_value=False), \
             patch.object(supervisor, "_record_action"), \
             patch("orchestrator.supervisor.httpx.Client", return_value=fake):
            routed = supervisor.detect_rapid_flap(
                product_id=1,
                flapping_features=[{"feature_id": 55, "transitions": 8, "window_hours": 1}],
            )
        assert routed == 1
        _, body = fake.patches[0]
        reason = body["blocked_reason"]
        assert "cycled status 8 times" in reason          # per-feature counts, not the old constant
        assert "2x post-coder:test-env" in reason         # dominant bounce author named
        assert "1x lint-guard" in reason
        assert "pm" not in reason.replace("supervisor.rapid_flap", "")  # pm comments excluded

    def test_signature_failure_degrades_gracefully(self):
        fake = _FlapClient([])
        with patch.object(supervisor, "_get_supervisor_config", return_value=self._cfg()), \
             patch.object(supervisor, "_recent_action", return_value=False), \
             patch.object(supervisor, "_record_action"), \
             patch("orchestrator.supervisor.httpx.Client", return_value=fake):
            supervisor.detect_rapid_flap(
                product_id=1,
                flapping_features=[{"feature_id": 56, "transitions": 6, "window_hours": 1}],
            )
        _, body = fake.patches[0]
        assert "cycled status 6 times" in body["blocked_reason"]
