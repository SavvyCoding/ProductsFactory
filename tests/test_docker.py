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
def product():
    return {
        "id": "abc123",
        "name": "My Test Product",
        "working_dir": "/home/user/products/my_test_product",
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

    def test_claude_dir_mounted_readonly(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)

        docker_runner.run_claude_in_docker(product)

        # Find the ~/.claude mount — must end with :ro
        claude_mounts = [
            arg for arg in captured_cmd
            if "/root/.claude" in arg
        ]
        assert len(claude_mounts) >= 1
        assert all(m.endswith(":ro") for m in claude_mounts), \
            f"~/.claude mount must be :ro, got: {claude_mounts}"

    def test_deploy_key_mounted_as_file_not_dir(self, product, env_vars, tmp_path, monkeypatch):
        """Verify only the deploy key FILE is mounted, not the entire .ssh dir.
        Mounting the whole dir would overwrite known_hosts baked into the image."""
        from orchestrator import docker_runner

        # Create a per-product deploy key
        key_file = tmp_path / "id_ed25519_my_test_product"
        key_file.write_text("fake-key-content")

        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)

        # The SSH mount must go to /root/.ssh/id_ed25519 (specific file), not /root/.ssh/
        ssh_mounts = [arg for arg in captured_cmd if "/root/.ssh" in arg]
        assert len(ssh_mounts) >= 1
        for m in ssh_mounts:
            assert "/root/.ssh/id_ed25519" in m, f"Must mount specific file, got: {m}"
            assert m.endswith(":ro"), f"Deploy key must be :ro, got: {m}"
            assert not m.split(":")[1].endswith("/.ssh/"), f"Must not mount entire .ssh dir: {m}"

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
        claude_mounts = [a for a in captured_cmd if "/root/.claude" in a]
        assert claude_mounts == [], "Ollama backend must not mount Claude OAuth dir"

    def test_persona_env_var_injected(self, product, env_vars, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product, persona="designer")
        assert "AGENT_PERSONA=designer" in captured_cmd


# ── Deploy key selection tests ────────────────────────────────────────────────

class TestDeployKeySelection:
    def test_per_product_key_used_when_exists(self, tmp_path):
        from orchestrator import docker_runner
        key = tmp_path / "id_ed25519_my_test_product"
        key.write_text("key")

        product = {"name": "My Test Product", "id": "1", "working_dir": str(tmp_path)}
        result = docker_runner._get_deploy_key_path.__wrapped__(product) \
            if hasattr(docker_runner._get_deploy_key_path, "__wrapped__") \
            else _call_get_deploy_key(docker_runner, product, tmp_path)
        assert result == key

    def test_falls_back_to_default_key(self, tmp_path):
        from orchestrator import docker_runner
        default_key = tmp_path / "id_ed25519_productfactory"
        default_key.write_text("key")

        product = {"name": "Unknown Product", "id": "2", "working_dir": str(tmp_path)}
        result = _call_get_deploy_key(docker_runner, product, tmp_path)
        assert result == default_key

    def test_returns_none_when_no_key_found(self, tmp_path):
        from orchestrator import docker_runner
        product = {"name": "No Key Product", "id": "3", "working_dir": str(tmp_path)}
        result = _call_get_deploy_key(docker_runner, product, tmp_path)
        assert result is None

    def test_name_slug_normalizes_spaces_and_hyphens(self, tmp_path):
        from orchestrator import docker_runner
        key = tmp_path / "id_ed25519_my_cool_product"
        key.write_text("key")

        # Spaces and hyphens both → underscores
        product = {"name": "My-Cool Product", "id": "4", "working_dir": str(tmp_path)}
        result = _call_get_deploy_key(docker_runner, product, tmp_path)
        assert result == key

    def test_no_ssh_mount_when_no_key(self, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        product = {"name": "No Key", "id": "5", "working_dir": str(tmp_path)}
        captured_cmd, _ = _setup_runner(monkeypatch, docker_runner, tmp_path)
        docker_runner.run_claude_in_docker(product)

        # No SSH volume mount at all (known_hosts from image is the only .ssh entry)
        ssh_mounts = [arg for arg in captured_cmd if "/root/.ssh/id_ed25519" in arg]
        assert ssh_mounts == [], f"Expected no SSH key mount, got: {ssh_mounts}"


def _call_get_deploy_key(docker_runner_module, product, ssh_dir):
    """Helper: call _get_deploy_key_path with a patched SSH_DIR."""
    original = docker_runner_module.SSH_DIR
    docker_runner_module.SSH_DIR = ssh_dir
    try:
        return docker_runner_module._get_deploy_key_path(product)
    finally:
        docker_runner_module.SSH_DIR = original


# ── Session lock guard tests ──────────────────────────────────────────────────

class TestSessionLockGuard:
    def test_skips_launch_when_lock_exists(self, tmp_path, monkeypatch):
        from orchestrator import docker_runner
        lock = tmp_path / "session.lock"
        lock.write_text("locked")

        product = {"name": "Locked Product", "id": "6", "working_dir": str(tmp_path)}

        popen_called = []
        monkeypatch.setenv("PM_API_URL", "http://pm-api:8080")
        monkeypatch.setattr(docker_runner, "PM_API_URL", "http://pm-api:8080")
        monkeypatch.setattr(docker_runner, "SSH_DIR", tmp_path)
        monkeypatch.setattr(docker_runner, "CLAUDE_DIR", tmp_path)
        monkeypatch.setattr(docker_runner, "AGENT_BACKEND", "claude")
        fake_client = MagicMock()
        fake_client.__enter__ = MagicMock(return_value=fake_client)
        fake_client.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr("httpx.Client", lambda *a, **kw: fake_client)
        monkeypatch.setattr("subprocess.Popen", lambda cmd, **kw: popen_called.append(cmd))

        result = docker_runner.run_claude_in_docker(product)
        assert result == 1
        assert popen_called == [], "Docker must not be launched when session.lock exists"

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
