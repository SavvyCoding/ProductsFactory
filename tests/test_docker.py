"""
Tests for Docker agent image config and docker_runner.py security properties.
These are static/unit tests — no live Docker daemon required.

Verifies:
  - Dockerfile has all required layers and tools
  - Security properties (no --privileged, :ro mounts, isolated network)
  - docker_runner.py builds the correct docker command
  - deploy key selection logic (per-product, fallback, missing)
  - build.sh creates isolated network (not --network host)
  - Session timeout and lock guard behavior
"""

import subprocess
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Dockerfile content tests ──────────────────────────────────────────────────

DOCKERFILE_PATH = Path(__file__).parent.parent / "deploy" / "docker" / "Dockerfile"


@pytest.fixture(scope="module")
def dockerfile_text():
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


class TestDockerfileRuntimes:
    def test_base_image_ubuntu_24(self, dockerfile_text):
        assert "FROM ubuntu:24.04" in dockerfile_text

    def test_python_pyenv_installed(self, dockerfile_text):
        assert "pyenv" in dockerfile_text

    def test_python_312_installed(self, dockerfile_text):
        assert "3.12" in dockerfile_text

    def test_python_311_installed(self, dockerfile_text):
        assert "3.11" in dockerfile_text

    def test_python_312_is_default(self, dockerfile_text):
        assert "pyenv global 3.12" in dockerfile_text

    def test_node_20_installed(self, dockerfile_text):
        assert "setup_20.x" in dockerfile_text or "node_20" in dockerfile_text.lower() or "v20" in dockerfile_text

    def test_node_n_version_manager(self, dockerfile_text):
        # Must use `n`, not nvm (nvm needs shell init — doesn't work in Docker RUN)
        assert "npm install -g n" in dockerfile_text

    def test_node_18_also_installed(self, dockerfile_text):
        assert "n 18" in dockerfile_text

    def test_go_122_installed(self, dockerfile_text):
        assert "1.22" in dockerfile_text

    def test_go_env_vars_set(self, dockerfile_text):
        assert "GOROOT" in dockerfile_text
        assert "GOPATH" in dockerfile_text


class TestDockerfileSecurityTools:
    def test_pip_audit_installed(self, dockerfile_text):
        assert "pip-audit" in dockerfile_text

    def test_govulncheck_installed(self, dockerfile_text):
        assert "govulncheck" in dockerfile_text

    def test_github_cli_installed(self, dockerfile_text):
        assert "gh" in dockerfile_text
        assert "github.com/packages" in dockerfile_text or "cli.github.com" in dockerfile_text

    def test_claude_code_cli_installed(self, dockerfile_text):
        assert "@anthropic-ai/claude-code" in dockerfile_text


class TestDockerfilePythonTools:
    def test_pytest_installed(self, dockerfile_text):
        assert "pytest" in dockerfile_text

    def test_pytest_cov_installed(self, dockerfile_text):
        assert "pytest-cov" in dockerfile_text

    def test_pytest_json_report_installed(self, dockerfile_text):
        assert "pytest-json-report" in dockerfile_text

    def test_httpx_installed(self, dockerfile_text):
        assert "httpx" in dockerfile_text

    def test_pydantic_installed(self, dockerfile_text):
        assert "pydantic" in dockerfile_text

    def test_tools_installed_for_both_python_versions(self, dockerfile_text):
        # Tools must be installed in both pyenv environments
        assert dockerfile_text.count("pip-audit") >= 2


class TestDockerfileGitConfig:
    def test_git_user_name_set(self, dockerfile_text):
        assert "user.name" in dockerfile_text
        assert "ProductFactory" in dockerfile_text

    def test_git_user_email_set(self, dockerfile_text):
        assert "user.email" in dockerfile_text

    def test_safe_directory_set(self, dockerfile_text):
        assert "safe.directory" in dockerfile_text
        assert "/workspace" in dockerfile_text

    def test_default_branch_main(self, dockerfile_text):
        assert "defaultBranch" in dockerfile_text
        assert "main" in dockerfile_text


class TestDockerfileSSHKnownHosts:
    def test_github_in_known_hosts(self, dockerfile_text):
        assert "ssh-keyscan" in dockerfile_text
        assert "github.com" in dockerfile_text

    def test_gitlab_in_known_hosts(self, dockerfile_text):
        assert "gitlab.com" in dockerfile_text

    def test_bitbucket_in_known_hosts(self, dockerfile_text):
        assert "bitbucket.org" in dockerfile_text

    def test_known_hosts_permissions(self, dockerfile_text):
        assert "chmod 600" in dockerfile_text
        assert "known_hosts" in dockerfile_text


class TestDockerfileSecurity:
    def test_no_privileged_flag(self, dockerfile_text):
        assert "--privileged" not in dockerfile_text

    def test_no_network_host(self, dockerfile_text):
        # Dockerfile itself should not run containers with --network host
        # (--add-host with host-gateway is fine — that's for DNS resolution)
        assert "--network host" not in dockerfile_text

    def test_workdir_is_workspace(self, dockerfile_text):
        assert "WORKDIR /workspace" in dockerfile_text

    def test_verification_run_at_end(self, dockerfile_text):
        # Must verify all tools compile/run correctly at build time
        assert "claude --version" in dockerfile_text
        assert "go version" in dockerfile_text


# ── docker_runner.py command construction tests ───────────────────────────────

RUNNER_MODULE = "orchestrator.docker_runner"


@pytest.fixture
def product(tmp_path):
    # Real, existing working dir (was a hardcoded '/home/user/...' path that
    # doesn't resolve off the original dev box — docker_runner then tried to
    # re-install templates into it and raised FileNotFoundError, failing the
    # whole class in CI). tmp_path is portable + writable so the template
    # re-install succeeds and the command-construction assertions still run.
    wd = tmp_path / "my_test_product"
    wd.mkdir()
    return {
        "id": "abc123",
        "name": "My Test Product",
        "working_dir": str(wd),
    }


@pytest.fixture
def env_vars(monkeypatch):
    monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")


def _setup_runner(monkeypatch, docker_runner, tmp_path):
    """Common setup for docker_runner tests: patch module-level vars + Popen."""
    monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
    monkeypatch.setattr(docker_runner, "PM_API_URL", "http://pm-api:8080")
    monkeypatch.setattr(docker_runner, "SSH_DIR", tmp_path)
    monkeypatch.setattr(docker_runner, "CLAUDE_DIR", tmp_path)
    monkeypatch.setattr(docker_runner, "AGENT_BACKEND", "claude")  # force claude backend

    # Silence the httpx DELETE call that clears old logs
    monkeypatch.setattr("httpx.delete", lambda *a, **kw: MagicMock(status_code=200))

    # Mock httpx.Client context manager (session create POST + session end PATCH + GH token GET)
    fake_session_resp = MagicMock()
    fake_session_resp.json.return_value = {"id": 99}
    fake_session_resp.raise_for_status = MagicMock()
    fake_client = MagicMock()
    fake_client.post.return_value = fake_session_resp
    fake_client.patch.return_value = MagicMock(status_code=200)
    fake_client.get.return_value = MagicMock(json=MagicMock(return_value={}))
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=False)
    monkeypatch.setattr("httpx.Client", lambda *a, **kw: fake_client)

    captured_cmd = []

    fake_proc = MagicMock()
    fake_proc.stdout = iter([])
    fake_proc.returncode = 0
    fake_proc.wait = MagicMock(return_value=0)

    def fake_popen(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return fake_proc

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    return captured_cmd, fake_proc


class TestDockerRunCommand:
    def test_uses_isolated_network_not_host(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)

        docker_runner.run_claude_in_docker(product)

        assert "--network" in captured_cmd
        net_idx = captured_cmd.index("--network")
        assert captured_cmd[net_idx + 1] == "productfactory-net"
        assert "host" not in captured_cmd

    def test_no_privileged_flag_in_command(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)

        docker_runner.run_claude_in_docker(product)
        assert "--privileged" not in captured_cmd

    def test_claude_credentials_never_exposed_writable(self, product, env_vars, tmp_path, monkeypatch):
        """Security: the host's real ~/.claude credentials are never bind-mounted
        writable into the container.

        The mount target moved from /root/.claude to /home/agent/.claude (the
        container now runs as non-root user `agent`, UID 1001). The runner
        stages a disposable copy of only the auth files and mounts THAT at
        /home/agent/.claude — read-write is safe there because it's a throwaway
        temp dir (source path prefix ``pf_claude_creds_``), so writes never reach
        the host. A read-only (:ro) direct mount of the originals is only the
        copy-failure fallback. Either way, the host's real credentials dir
        (CLAUDE_DIR) is never exposed writable inside the container.
        """
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)

        docker_runner.run_claude_in_docker(product)

        # Find the ~/.claude mount(s) at the current (non-root) target path.
        claude_mounts = [
            arg for arg in captured_cmd
            if "/home/agent/.claude" in arg
        ]
        assert len(claude_mounts) >= 1, f"expected a Claude creds mount, cmd={captured_cmd}"
        for m in claude_mounts:
            staged_copy = "pf_claude_creds_" in m   # disposable temp copy, not the originals
            read_only = m.endswith(":ro")
            assert staged_copy or read_only, \
                f"Claude creds mount must be a staged disposable copy or :ro, got: {m}"
            # The host's real credentials dir (CLAUDE_DIR == tmp_path here) must
            # never be bind-mounted writable into the container.
            assert str(tmp_path) not in m or read_only, \
                f"host CLAUDE_DIR must not be exposed writable in container: {m}"

    def test_no_ssh_deploy_key_mounted(self, product, env_vars, tmp_path, monkeypatch):
        """SSH deploy keys are retired: git auth is GitHub App installation
        tokens only (see CLAUDE.md Auth & Security). The runner must not mount
        any ~/.ssh deploy key into the container — even when a legacy per-product
        key file happens to exist on disk."""
        from orchestrator import docker_runner

        # A stale per-product deploy key must be ignored, not mounted.
        key_file = tmp_path / "id_ed25519_my_test_product"
        key_file.write_text("fake-key-content")

        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)

        ssh_mounts = [arg for arg in captured_cmd if "/root/.ssh" in arg or "/home/agent/.ssh" in arg]
        assert ssh_mounts == [], f"SSH deploy-key mounting is retired, got: {ssh_mounts}"

    def test_uses_rm_flag(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)
        assert "--rm" in captured_cmd

    def test_memory_and_cpu_limits_set(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)
        assert "--memory" in captured_cmd
        assert "--cpus" in captured_cmd

    def test_uses_add_host_for_pm_api(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)
        assert "--add-host" in captured_cmd

    def test_uses_dangerously_skip_permissions(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)
        assert "--dangerously-skip-permissions" in captured_cmd

    def test_ollama_backend_uses_python_agent(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        monkeypatch.setattr(docker_runner, "AGENT_BACKEND", "ollama")
        docker_runner.run_claude_in_docker(product, persona="coder")
        assert any("ollama_agent.py" in arg for arg in captured_cmd)
        assert not any(arg == "claude" for arg in captured_cmd)

    def test_ollama_backend_skips_claude_mount(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        monkeypatch.setattr(docker_runner, "AGENT_BACKEND", "ollama")
        docker_runner.run_claude_in_docker(product, persona="coder")
        claude_mounts = [a for a in captured_cmd if "/home/agent/.claude" in a]
        assert claude_mounts == [], "Ollama backend must not mount Claude OAuth dir"

    def test_persona_env_var_injected(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product, persona="designer")
        assert "AGENT_PERSONA=designer" in captured_cmd


# ── Session lock guard tests ──────────────────────────────────────────────────

class TestSessionLockGuard:
    # NOTE: the host-mode poller's file-based session.lock guard (orchestrator/
    # cycle/locks.py) was retired with the host-mode poller (2026-05-18).
    # Concurrency is now serialized by the DB active-session check + per-product
    # mutex in the website / tools.py, not a working_dir lock file. The old
    # test_skips_launch_when_lock_exists (asserted run_claude_in_docker returns 1
    # and never calls Popen when a session.lock exists) was removed because that
    # code path no longer exists — a stray session.lock file is ignored.

    def test_launches_when_no_lock(self, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        product = {"name": "Clean Product", "id": "7", "working_dir": str(tmp_path)}
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)

        result = docker_runner.run_claude_in_docker(product)
        assert result == 0
        assert len(captured_cmd) > 0


# ── Timeout behavior tests ────────────────────────────────────────────────────

class TestSessionTimeout:
    def test_returns_1_on_timeout(self, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        monkeypatch.setattr(docker_runner, "SESSION_TIMEOUT_SECONDS", 10)
        monkeypatch.setattr(docker_runner, "send_alert", lambda *a, **kw: None)

        product = {"name": "Timeout Product", "id": "8", "working_dir": str(tmp_path)}

        _, fake_proc = _setup_runner(monkeypatch, docker_runner, tmp_path)
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(["docker"], 10)
        monkeypatch.setattr("subprocess.run", lambda cmd, **kw: MagicMock(returncode=0))

        result = docker_runner.run_claude_in_docker(product)
        assert result == 1

    def test_kills_container_on_timeout(self, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        monkeypatch.setattr(docker_runner, "SESSION_TIMEOUT_SECONDS", 10)
        monkeypatch.setattr(docker_runner, "send_alert", lambda *a, **kw: None)

        product = {"name": "Timeout Product", "id": "9", "working_dir": str(tmp_path)}

        kill_calls = []
        _, fake_proc = _setup_runner(monkeypatch, docker_runner, tmp_path)
        fake_proc.wait.side_effect = subprocess.TimeoutExpired(["docker"], 10)

        def fake_kill(cmd, **kw):
            if "kill" in cmd:
                kill_calls.append(cmd)
            return MagicMock(returncode=0)

        monkeypatch.setattr("subprocess.run", fake_kill)
        docker_runner.run_claude_in_docker(product)
        assert any("kill" in str(c) for c in kill_calls), "Must kill container on timeout"


# ── build.sh content tests ────────────────────────────────────────────────────

BUILD_SH_PATH = Path(__file__).parent.parent / "deploy" / "docker" / "build.sh"


@pytest.fixture(scope="module")
def build_sh_text():
    return BUILD_SH_PATH.read_text(encoding="utf-8")


class TestBuildSh:
    def test_creates_productfactory_net(self, build_sh_text):
        assert "productfactory-net" in build_sh_text

    def test_uses_bridge_driver_not_host(self, build_sh_text):
        assert "--driver bridge" in build_sh_text
        # Must not use host networking for the Docker network
        assert "--driver host" not in build_sh_text

    def test_network_creation_is_idempotent(self, build_sh_text):
        # Must check if network exists before creating
        assert "docker network inspect" in build_sh_text

    def test_supports_no_cache_flag(self, build_sh_text):
        assert "--no-cache" in build_sh_text

    def test_supports_test_flag(self, build_sh_text):
        assert "--test" in build_sh_text

    def test_tags_image_with_date(self, build_sh_text):
        assert "date" in build_sh_text
