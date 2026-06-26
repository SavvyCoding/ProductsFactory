"""
Tests for the node_modules-tmpfs container mount (Fix B) and the exit-124
infra classifier, both added 2026-06-23 after IndianFoodTruck #1644.

Root cause recap: on Docker Desktop for Windows /workspace is an NTFS bind,
and `npm ci` extracting ~1200 packages into /workspace/node_modules over the
9p/virtiofs layer took ~1 minute — eating the gate `timeout` budget so heavy
Verify recipes (AC3/AC4 JSDOM renders on #1644) were SIGTERM'd with exit 124
and bounced as phantom "AC mismatches" the coder could not fix.

Fix B moves node_modules onto a fast per-run tmpfs; the classifier reclassifies
exit 124 as infra (verify-check → skipped; test-check → env_broken) so a mount
timeout never bumps fix_attempts.

These are pure / mock-based (no Docker), so they run on every platform.
"""

import os
import types
import pytest

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines import post_coder as pc  # noqa: E402
from orchestrator.pipelines.post_coder import (  # noqa: E402
    _agent_container_base,
    _run_verify,
    _post_coder_test_check,
)


def _fake_completed(returncode, stdout="", stderr=""):
    """Stand-in for subprocess.CompletedProcess."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── Fix B: node_modules tmpfs ────────────────────────────────────────────────
class TestNodeModulesTmpfs:
    def test_node_product_gets_node_modules_tmpfs_and_memory_bump(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
        prefix, _install = _agent_container_base(str(tmp_path))
        joined = " ".join(prefix)
        # tmpfs overlays node_modules, world-writable (non-root agent) + exec
        # (native .node addons must dlopen).
        assert "--tmpfs" in prefix
        assert "/workspace/node_modules:rw,exec,mode=1777,size=3g" in prefix
        # tmpfs pages count against the cgroup → node gets the headroom bump.
        assert "6g" in prefix and "4g" not in prefix
        # mounted at /workspace/node_modules, i.e. UNDER the bind, not replacing it
        assert ":/workspace" in joined

    def test_non_node_product_unchanged(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        prefix, _install = _agent_container_base(str(tmp_path))
        assert "--tmpfs" not in prefix
        assert "4g" in prefix  # default memory, no bump


# ── exit-124 classifier: verify-check side ───────────────────────────────────
class TestVerifyExit124IsSkip:
    def test_inner_timeout_124_classified_as_skip(self, tmp_path, monkeypatch):
        # Simulate the container exiting 124 (inner GNU `timeout` SIGTERM).
        monkeypatch.setattr(
            pc._sp, "run",
            lambda *a, **k: _fake_completed(124, stdout="added 1198 packages in 1m"),
        )
        r = _run_verify("npx tsx -e 'heavy()'", cwd=str(tmp_path), timeout=30)
        assert r["exit_code"] == 124
        assert r["skipped"] is True
        assert "timed out" in r["skip_reason"]
        # Preserves the 'timed out' stderr marker the legacy test relies on.
        assert "timed out" in r["stderr"]

    def test_outer_subprocess_timeout_classified_as_skip(self, tmp_path, monkeypatch):
        def _boom(*a, **k):
            raise pc._sp.TimeoutExpired(cmd="docker", timeout=210)
        monkeypatch.setattr(pc._sp, "run", _boom)
        r = _run_verify("npx tsx -e 'heavy()'", cwd=str(tmp_path), timeout=30)
        assert r["exit_code"] == 124
        assert r["skipped"] is True

    def test_genuine_nonzero_still_not_skipped(self, tmp_path, monkeypatch):
        # A normal failing recipe (exit 1) must STILL be a real result, not a skip.
        monkeypatch.setattr(
            pc._sp, "run", lambda *a, **k: _fake_completed(1, stdout="boom"),
        )
        r = _run_verify("false", cwd=str(tmp_path), timeout=30)
        assert r["exit_code"] == 1
        assert r["skipped"] is False


# ── exit-124 classifier: test-check side ─────────────────────────────────────
class TestTestCheckExit124IsEnvBroken:
    def test_node_test_timeout_124_is_env_broken_no_bump(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name":"x","scripts":{"test":"jest"}}', encoding="utf-8")

        # _post_coder_test_check takes the container-run callable as a param;
        # stub it to mimic the test command being SIGTERM'd at exit 124.
        def _run(cmd, **kw):
            return _fake_completed(124, stdout="added 1198 packages in 1m")

        res = _post_coder_test_check(str(tmp_path), _run, product_name="t", timeout=30)
        assert res["env_broken"] is True
        assert res["passed"] is False
        assert "timed out" in res["first_failure"]

    def test_node_real_test_failure_still_bounces(self, tmp_path):
        (tmp_path / "package.json").write_text(
            '{"name":"x","scripts":{"test":"jest"}}', encoding="utf-8")

        def _run(cmd, **kw):
            return _fake_completed(1, stdout="Tests: 1 failed, 2 passed")

        res = _post_coder_test_check(str(tmp_path), _run, product_name="t", timeout=30)
        assert res["env_broken"] is False
        assert res["passed"] is False
