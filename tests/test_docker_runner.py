"""
Integration tests for orchestrator/docker_runner.py sandbox hardening.

These tests intercept `subprocess.Popen` so no actual `docker run` is executed,
then assert that every required isolation flag is present in the constructed
command. This prevents silent regressions of the Phase 1 sandbox changes
(pids-limit, cap-drop, no-new-privileges, read-only rootfs, tmpfs mounts,
gh_token file mount instead of env var).

The fixture builds a product dict whose working_dir is a real `tmp_path` so
`Path(working_dir, _agent_dir).mkdir(exist_ok=True)` in run_claude_in_docker
succeeds without network or DB access.
"""

from __future__ import annotations

import os
# docker_runner.py reads PM_API_URL at import time — set it before any import below.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def product(tmp_path: Path) -> dict:
    return {
        "id": "42",
        "name": "Sandbox Test Product",
        "working_dir": str(tmp_path),
    }


def _run_and_capture(monkeypatch, docker_runner, product, tmp_path, *,
                     backend: str = "claude", persona: str = "coder") -> list[str]:
    """Run run_claude_in_docker with all external dependencies mocked out.

    Returns the flat list of argv tokens passed to subprocess.Popen.
    """
    monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
    monkeypatch.setattr(docker_runner, "PM_API_URL", "http://pm-api:8080")
    monkeypatch.setattr(docker_runner, "SSH_DIR", tmp_path)
    monkeypatch.setattr(docker_runner, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(docker_runner, "AGENT_BACKEND", backend)
    # Stub out heavy helpers that hit PM API or filesystem state we don't care about here.
    monkeypatch.setattr(docker_runner, "_get_system_config_sync", lambda: {})
    monkeypatch.setattr(docker_runner, "_get_gh_token", lambda: "ghp_testtokenXYZ")
    monkeypatch.setattr(docker_runner, "install_templates", lambda *a, **kw: None)
    monkeypatch.setattr(docker_runner, "build_prompt", lambda *a, **kw: "test prompt")
    # _fetch_assigned_features returns (features, sprint_name, sprint_dict) — 3-tuple.
    # Pre-#10, this stub returned a 2-tuple and every test in this module crashed
    # on `(features, sprint_name, active_sprint) = _fetch_assigned_features(...)`
    # at docker_runner.py:1431 with ValueError.
    monkeypatch.setattr(docker_runner, "_fetch_assigned_features",
                        lambda *a, **kw: ([], None, None))
    monkeypatch.setattr(docker_runner, "_format_assigned_features", lambda *a, **kw: "")
    monkeypatch.setattr(docker_runner, "_write_sprint_features_md", lambda *a, **kw: None)
    monkeypatch.setattr(docker_runner, "_reset_workspace", lambda *a, **kw: None)
    # Silence log buffer DELETE
    monkeypatch.setattr("httpx.delete", lambda *a, **kw: MagicMock(status_code=200))

    # httpx.Client context manager returning canned responses for session create/patch/get.
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"id": 99}
    fake_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.post.return_value = fake_resp
    fake_client.patch.return_value = MagicMock(status_code=200)
    fake_client.get.return_value = MagicMock(json=MagicMock(return_value={}), status_code=200)
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("httpx.Client", lambda *a, **kw: fake_client)

    # subprocess.run stubs — docker-check (active session), git ops, docker exec, docker kill.
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: MagicMock(returncode=0, stdout="", stderr=""))

    captured: list[str] = []
    fake_proc = MagicMock()
    fake_proc.stdout = iter([])
    fake_proc.returncode = 0
    fake_proc.wait = MagicMock(return_value=0)

    def fake_popen(cmd, **kwargs):
        captured.extend(cmd)
        return fake_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    docker_runner.run_claude_in_docker(product, persona=persona)
    return captured


def _flag_value(cmd: list[str], flag: str) -> str | None:
    """Return the value immediately following `flag` in the argv list, or None."""
    try:
        idx = cmd.index(flag)
    except ValueError:
        return None
    return cmd[idx + 1] if idx + 1 < len(cmd) else None


class TestSandboxHardening:
    """Phase 1 hardening (C1/C2/H1/C3) — every flag must be present on every run."""

    def test_pids_limit_prevents_fork_bomb(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert "--pids-limit" in cmd, "missing --pids-limit — container can fork-bomb the host"
        value = _flag_value(cmd, "--pids-limit")
        assert value is not None and int(value) <= 1024, \
            f"--pids-limit too high or invalid: {value}"

    def test_cap_drop_all(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert "--cap-drop" in cmd, "missing --cap-drop — container runs with default Linux caps"
        assert _flag_value(cmd, "--cap-drop") == "ALL"

    def test_no_new_privileges(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert "--security-opt" in cmd, "missing --security-opt no-new-privileges"
        # no-new-privileges may appear in any of the --security-opt slots
        nnp_present = any(a == "no-new-privileges:true" for a in cmd)
        assert nnp_present, "--security-opt no-new-privileges:true not set"

    def test_read_only_rootfs(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert "--read-only" in cmd, "rootfs must be read-only so agent can't persist changes"

    def test_required_tmpfs_mounts_present(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        # Collect every value that follows a --tmpfs flag
        tmpfs_values: list[str] = []
        for i, tok in enumerate(cmd):
            if tok == "--tmpfs" and i + 1 < len(cmd):
                tmpfs_values.append(cmd[i + 1])
        required_paths = ["/tmp", "/run", "/home/agent/.cache", "/home/agent/.npm"]
        for req in required_paths:
            assert any(v.startswith(req + ":") for v in tmpfs_values), \
                f"missing tmpfs mount for {req} — writes would fail with --read-only"

    def test_memory_and_cpu_limits_still_set(self, product, tmp_path, monkeypatch):
        """Regression guard — sandbox hardening must not remove pre-existing resource caps."""
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert _flag_value(cmd, "--memory") == "4g"
        assert _flag_value(cmd, "--cpus") == "2"

    def test_bridge_network_not_host(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert _flag_value(cmd, "--network") == "productfactory-net"
        # --network host would be a catastrophic regression
        assert "host" not in (cmd[cmd.index("--network") + 1:cmd.index("--network") + 2])

    def test_no_privileged_flag(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        assert "--privileged" not in cmd


class TestGhTokenFileMount:
    """Phase 1 C5 — GH_TOKEN must travel via file mount, never as -e env var."""

    def test_gh_token_not_in_env_vars(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        # No `-e GH_TOKEN=...` env-var injection (would leak via `docker inspect`).
        gh_env_tokens = [t for t in cmd if isinstance(t, str) and t.startswith("GH_TOKEN=")]
        assert gh_env_tokens == [], \
            f"GH_TOKEN must not be passed as env var; found {gh_env_tokens}"

    def test_gh_token_mounted_at_run_secrets(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        # Look for a -v mount whose target is /run/secrets/gh_token:ro
        mounts = [cmd[i + 1] for i, t in enumerate(cmd)
                  if t == "-v" and i + 1 < len(cmd)]
        assert any(m.endswith(":/run/secrets/gh_token:ro") for m in mounts), \
            f"GH_TOKEN mount missing or wrong target; mounts={mounts}"

    def test_agent_cmd_wrapped_for_gh_token(self, product, tmp_path, monkeypatch):
        """When gh_token is set, agent_cmd must be wrapped in `sh -c ...` that loads the
        token into env before exec'ing the real agent. Otherwise `gh` CLI won't see it."""
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path)
        # The last few argv elements form the agent_cmd. Expect sh -c '...' wrapper.
        assert "sh" in cmd and "-c" in cmd, \
            "agent_cmd must be wrapped in sh -c to source GH_TOKEN from file"
        # Confirm the wrapper references the mount path.
        joined = " ".join(cmd)
        assert "/run/secrets/gh_token" in joined, \
            "sh wrapper must reference /run/secrets/gh_token"


class TestOllamaBackendSandbox:
    """All sandbox hardening flags must apply to the ollama backend too."""

    def test_ollama_sandbox_flags_present(self, product, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        cmd = _run_and_capture(monkeypatch, docker_runner, product, tmp_path, backend="ollama")
        for flag in ("--pids-limit", "--cap-drop", "--read-only", "--security-opt"):
            assert flag in cmd, f"ollama backend missing hardening flag {flag}"
