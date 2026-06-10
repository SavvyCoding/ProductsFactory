"""
Wave-3 structural fixes (2026-06-10): Guard 20 (net test-file deletion),
detect_secret_sentinel, and fresh_env_check's classification/filing logic.
"""

import os
import subprocess as _sp
from unittest.mock import MagicMock, patch

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.drift_detectors import detect_secret_sentinel  # noqa: E402
from orchestrator.pipelines.post_coder import _post_coder_lint_check  # noqa: E402
from orchestrator import fresh_env_check  # noqa: E402


# ── shared git helpers (same conventions as test_post_coder_deps_guard) ─────


def _git(cwd, *args, check=True):
    return _sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _make_run(working_dir):
    def _run(cmd, **kw):
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True,
                       timeout=timeout, **kw)
    return _run


def _init_repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    return tmp_path


def _write(repo, rel, content):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def _commit_all(repo, message="commit"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _g20_violation(violations):
    for v in violations:
        if "test file(s) deleted" in v:
            return v
    return None


# ── Guard 20: net test-file deletion ────────────────────────────────────────


class TestGuard20TestDeletion:
    def test_pure_test_deletion_bounced(self, tmp_path):
        # Canonical MyCalc1 shape: rework deletes the test file outright.
        repo = _init_repo(tmp_path)
        _write(repo, "src/calc.py", "def add(a, b): return a + b\n")
        _write(repo, "tests/test_calc.py", "def test_add(): assert True\n")
        _commit_all(repo, "initial")
        (repo / "tests/test_calc.py").unlink()
        _write(repo, "src/calc.py", "def add(a, b): return a + b  # rework\n")
        _commit_all(repo, "rework deletes the test")

        v = _g20_violation(_post_coder_lint_check(str(repo), _make_run(str(repo))))
        assert v is not None
        assert "tests/test_calc.py" in v

    def test_ts_test_deletion_bounced(self, tmp_path):
        # The Guard-17 gap: non-Python stacks had no deletion protection.
        repo = _init_repo(tmp_path)
        _write(repo, "src/Calculator.tsx", "export const C = 1;\n")
        _write(repo, "tests/Calculator.test.tsx", "it('renders', () => {});\n")
        _commit_all(repo, "initial")
        (repo / "tests/Calculator.test.tsx").unlink()
        _write(repo, "src/Calculator.tsx", "export const C = 2;\n")
        _commit_all(repo, "rework deletes the test")

        v = _g20_violation(_post_coder_lint_check(str(repo), _make_run(str(repo))))
        assert v is not None

    def test_rename_replace_passes(self, tmp_path):
        # D + A in the same commit (rename / split) keeps coverage — pass.
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_old_name.py", "def test_x(): assert 1\n")
        _commit_all(repo, "initial")
        (repo / "tests/test_old_name.py").unlink()
        _write(repo, "tests/test_new_name.py", "def test_x(): assert 1\n")
        _commit_all(repo, "rename test")

        assert _g20_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None

    def test_deprecated_listed_deletion_passes(self, tmp_path):
        # The sanctioned removal queue: ARCHITECTURE.md DEPRECATED lists it.
        repo = _init_repo(tmp_path)
        _write(repo, "ARCHITECTURE.md",
               "## DEPRECATED\n- `tests/test_grep_antipattern.py` "
               "(source-grep antipattern — replace with behaviour assertion)\n")
        _write(repo, "tests/test_grep_antipattern.py", "def test_g(): pass\n")
        _commit_all(repo, "initial")
        (repo / "tests/test_grep_antipattern.py").unlink()
        _write(repo, "src/real.py", "x = 1\n")
        _commit_all(repo, "remove deprecated test")

        assert _g20_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None

    def test_non_test_deletion_not_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/old_module.py", "x = 1\n")
        _write(repo, "src/keeper.py", "y = 1\n")
        _commit_all(repo, "initial")
        (repo / "src/old_module.py").unlink()
        _write(repo, "src/keeper.py", "y = 2\n")
        _commit_all(repo, "delete a source module")

        assert _g20_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None


# ── detect_secret_sentinel ──────────────────────────────────────────────────


def _feature(fid: int, **kw) -> dict:
    base = {"id": fid, "name": f"feat-{fid}", "status": "Designed"}
    base.update(kw)
    return base


def _w(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestDetectSecretSentinel:
    def test_missing_env_sentinel_flagged(self, tmp_path):
        # Canonical Mytracking evasion shape.
        _w(tmp_path / "src" / "auth" / "deps.py", (
            "def _get_env_var(name):\n"
            "    return os.environ.get(name) or f'<MISSING_ENV_VAR_{name}>'\n"
        ))
        out = detect_secret_sentinel(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        assert out[0].category == "secret_sentinel"
        assert out[0].severity == "high"

    def test_placeholder_secret_literal_flagged(self, tmp_path):
        _w(tmp_path / "src" / "seed.py",
           'password_hash = "placeholder-secret-hash"\n')
        assert len(detect_secret_sentinel(tmp_path, [_feature(1)])) == 1

    def test_environ_get_secret_default_flagged(self, tmp_path):
        _w(tmp_path / "src" / "config.py",
           'JWT = os.environ.get("JWT_SECRET", "dev-fallback")\n')
        assert len(detect_secret_sentinel(tmp_path, [_feature(1)])) == 1

    def test_test_files_excluded(self, tmp_path):
        # Fixed test secrets are legitimate.
        _w(tmp_path / "tests" / "test_auth.py",
           'os.environ["JWT_SECRET"] = "dummy-secret"\n')
        assert detect_secret_sentinel(tmp_path, [_feature(1)]) == []

    def test_fail_closed_code_passes(self, tmp_path):
        _w(tmp_path / "src" / "auth.py", (
            "secret = os.environ.get('JWT_SECRET')\n"
            "if not secret:\n"
            "    raise RuntimeError('JWT_SECRET must be set')\n"
        ))
        assert detect_secret_sentinel(tmp_path, [_feature(1)]) == []


# ── fresh_env_check ─────────────────────────────────────────────────────────


class TestFreshEnvCheck:
    def test_skips_without_requirements(self, tmp_path):
        res = fresh_env_check._run_fresh_install(str(tmp_path))
        assert res["status"] == "skipped"

    def test_detect_skips_when_disabled(self):
        with patch.dict(os.environ, {"FRESH_ENV_CHECK_ENABLED": "0"}):
            res = fresh_env_check.detect_broken_fresh_install(
                {"id": 1, "working_dir": "/x", "name": "p"})
        assert res == {"status": "disabled", "filed_chore": False}

    def test_passed_files_nothing(self):
        with patch.object(fresh_env_check, "_run_fresh_install",
                          return_value={"status": "passed", "output": ""}):
            res = fresh_env_check.detect_broken_fresh_install(
                {"id": 1, "working_dir": "/x", "name": "p"},
                pm_client=MagicMock())
        assert res == {"status": "passed", "filed_chore": False}

    def test_install_failed_files_chore_and_alert(self):
        client = MagicMock()
        with patch.object(fresh_env_check, "_run_fresh_install",
                          return_value={"status": "install_failed",
                                        "output": "ERROR: no matching distribution"}), \
             patch("orchestrator.drift_detectors.file_corrective_chores",
                   return_value=1) as fcc:
            res = fresh_env_check.detect_broken_fresh_install(
                {"id": 7, "working_dir": "/x", "name": "p"}, pm_client=client)
        assert res == {"status": "install_failed", "filed_chore": True}
        finding = fcc.call_args[0][0][0]
        assert finding.category == "broken_fresh_install"
        assert finding.dedupe_key == "fresh_install:7"
        # Alert posted on the filing transition.
        client.post.assert_called_once()
        assert "/api/alerts" in client.post.call_args[0][0]

    def test_collect_failed_dry_run_files_nothing(self):
        with patch.object(fresh_env_check, "_run_fresh_install",
                          return_value={"status": "collect_failed", "output": "x"}):
            res = fresh_env_check.detect_broken_fresh_install(
                {"id": 7, "working_dir": "/x", "name": "p"},
                pm_client=MagicMock(), dry_run=True)
        assert res == {"status": "collect_failed", "filed_chore": False}

    def test_no_new_chore_no_alert(self):
        # Dedupe path: file_corrective_chores returns 0 (chore already open).
        client = MagicMock()
        with patch.object(fresh_env_check, "_run_fresh_install",
                          return_value={"status": "install_failed", "output": "x"}), \
             patch("orchestrator.drift_detectors.file_corrective_chores",
                   return_value=0):
            res = fresh_env_check.detect_broken_fresh_install(
                {"id": 7, "working_dir": "/x", "name": "p"}, pm_client=client)
        assert res == {"status": "install_failed", "filed_chore": False}
        client.post.assert_not_called()
