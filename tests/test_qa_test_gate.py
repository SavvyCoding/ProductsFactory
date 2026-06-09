"""QA/Tester gate — deterministic test execution in a throwaway agent-image
container (no LLM). Replaces in-orchestrator-process test execution, which
broke for every non-Python stack because the orchestrator lacks node/npm/go
(canonical: MyCalc1 #1370 'npm not found' → env_broken cascade).

These tests assert `_container_test_run` builds the correct `docker run` argv
(stack-appropriate clean install prepended, workspace mounted, hardening flags)
without actually launching docker.
"""
import os
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import orchestrator.pipelines.post_coder as pc


def _capture(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(pc._sp, "run", fake_run)
    return calls


class TestAllFailingTests:
    """Runner-agnostic parsing of the FULL failing-test set (widened rework
    feedback) — so the coder fixes every failure, not just the first."""

    def test_vitest_lists_all_failing_files(self):
        out = (
            " ✓ tests/engine.test.ts (6 tests) 2ms\n"
            " ❯ tests/Calculator.test.tsx (0 test)\n"
            " ❯ tests/scaffolding.test.ts (4 tests | 2 failed) 7346ms\n"
        )
        got = pc._all_failing_tests(out)
        assert "tests/Calculator.test.tsx" in got
        assert "tests/scaffolding.test.ts" in got
        assert "tests/engine.test.ts" not in got        # passing file excluded

    def test_pytest_lists_all_failed_ids(self):
        out = "FAILED tests/test_a.py::test_x\nFAILED tests/test_b.py::test_y\n"
        got = pc._all_failing_tests(out)
        assert got == ["tests/test_a.py::test_x", "tests/test_b.py::test_y"]

    def test_go_failures(self):
        assert "TestFoo" in pc._all_failing_tests("--- FAIL: TestFoo (0.01s)\n")

    def test_strips_ansi_and_dedupes(self):
        out = "\x1b[31mFAILED tests/x.py::t\x1b[0m\nFAILED tests/x.py::t\n"
        assert pc._all_failing_tests(out) == ["tests/x.py::t"]

    def test_empty_on_no_failures(self):
        assert pc._all_failing_tests("all good\n") == []
        assert pc._all_failing_tests("") == []


class TestVerifyCheckInContainer:
    """The verify-check (per-AC Verify recipes) must run in the agent container
    too — the recipes invoke stack tools (npx/tsc/node) the orchestrator lacks
    (the MyCalc1 #1370 `npx: command not found` false-bounce)."""

    def test_recipe_runs_in_agent_container(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
        calls = _capture(monkeypatch)
        res = pc._run_verify("npx tsc --noEmit", cwd=str(tmp_path), timeout=60)
        cmd = calls["cmd"]
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert "productfactory-agent" in cmd
        assert any(a.endswith(":/workspace") for a in cmd)
        script = cmd[-1]
        assert "npx tsc --noEmit" in script        # the recipe (via bash -c)
        assert "timeout 60s" in script             # hard timeout backstop
        assert ">/dev/null 2>&1" in script         # install silenced
        assert res["skipped"] is False

    def test_server_required_recipe_still_skipped_without_container(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        res = pc._run_verify("curl -s http://localhost:8000/health", cwd=str(tmp_path), timeout=30)
        assert res["skipped"] is True              # pre-detected, no docker run
        assert "cmd" not in calls                  # never launched a container


class TestEnvBrokenClassification:
    """A pip install failure from a CODER-declared bad dependency (unresolvable
    package) must be a real bounce (fix_attempts++), not env_broken — only
    genuine infra/transient failures stay env_broken (testingcalc #1423)."""

    def _check(self, tmp_path, install_stderr):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        (tmp_path / "requirements.txt").write_text("somepkg\n")

        def fake_run(cmd, **kw):
            class _R:
                returncode = 1
                stdout = ""
                stderr = install_stderr
            return _R()

        return pc._post_coder_test_check(str(tmp_path), fake_run, "test")

    def test_unresolvable_package_is_real_bounce(self, tmp_path):
        res = self._check(tmp_path,
            "ERROR: Could not find a version that satisfies the requirement bogus==9.9")
        assert res["env_broken"] is False            # coder's fault → fix_attempts++
        assert res["passed"] is False
        assert "requirements.txt" in res["first_failure"]

    def test_no_matching_distribution_is_real_bounce(self, tmp_path):
        res = self._check(tmp_path,
            "ERROR: No matching distribution found for nonexistent-pkg-xyz")
        assert res["env_broken"] is False

    def test_transient_network_stays_env_broken(self, tmp_path):
        res = self._check(tmp_path,
            "WARNING: Retrying (Retry(total=4)) ... Connection broken: Read timed out")
        assert res["env_broken"] is True             # infra/transient → no bump
        assert res["passed"] is False


class TestContainerTestRun:
    def test_npm_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test", "--silent"], timeout=300)
        cmd = calls["cmd"]
        assert cmd[:3] == ["docker", "run", "--rm"]
        assert "productfactory-agent" in cmd          # default agent image
        assert any(a.endswith(":/workspace") for a in cmd)
        script = cmd[-1]
        assert script.startswith("npm ci") and "npm test --silent" in script

    def test_pip_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"], timeout=300)
        script = calls["cmd"][-1]
        assert "pip install" in script and "requirements.txt" in script and "pytest -q" in script

    def test_go_install_prefixed(self, tmp_path, monkeypatch):
        (tmp_path / "go.mod").write_text("module x\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["go", "test", "./..."], timeout=300)
        script = calls["cmd"][-1]
        assert script.startswith("go mod download") and "go test ./..." in script

    def test_no_markers_no_install_prefix(self, tmp_path, monkeypatch):
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["echo", "hi"])
        # No install prefix, but the hard-timeout backstop still wraps the cmd.
        assert calls["cmd"][-1] == "timeout 300s echo hi"

    def test_ci_env_and_timeout_backstop(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}')
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test"], timeout=300)
        cmd = calls["cmd"]
        # CI=true disables watch mode (the MyCalc1 vitest hang); timeout is the
        # backstop that kills a stuck runner instead of hanging the pipeline.
        assert "CI=true" in cmd
        assert "timeout 300s npm test" in cmd[-1]

    def test_npm_takes_precedence_over_requirements(self, tmp_path, monkeypatch):
        (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["npm", "test"])
        assert calls["cmd"][-1].startswith("npm ci")

    def test_security_and_resource_flags_present(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"])
        cmd = calls["cmd"]
        for flag in ("--cap-drop", "--security-opt", "--network", "--pids-limit", "--memory"):
            assert flag in cmd, f"missing hardening flag {flag}"

    def test_per_product_download_cache_mounted(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"])
        cmd = calls["cmd"]
        assert any(a.endswith(":/cache") for a in cmd)          # cache bind-mount
        assert "PIP_CACHE_DIR=/cache/pip" in cmd
        assert "npm_config_cache=/cache/npm" in cmd
        # Cache lives outside the repo (sibling .pf-cache), never under the workspace.
        assert (tmp_path.parent / ".pf-cache" / tmp_path.name).exists()

    def test_outer_timeout_exceeds_inner(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"], timeout=300)
        assert calls["kw"]["timeout"] == 300 + 120     # install headroom over inner cap
