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
        assert calls["cmd"][-1] == "echo hi"          # nothing prepended

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

    def test_outer_timeout_exceeds_inner(self, tmp_path, monkeypatch):
        (tmp_path / "requirements.txt").write_text("flask\n")
        calls = _capture(monkeypatch)
        pc._container_test_run(str(tmp_path))(["pytest", "-q"], timeout=300)
        assert calls["kw"]["timeout"] == 300 + 120     # install headroom over inner cap
