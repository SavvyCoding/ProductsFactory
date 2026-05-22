"""
Tests for Guard 18 (deps coherence) in _post_coder_lint_check.

Same temp-git-repo strategy as test_post_coder_deletion_guard.py — drives
real `git` commands and calls the real _post_coder_lint_check. Each test
sets up two commits:
  - HEAD~1: initial state (with requirements.txt + at least one tracked file)
  - HEAD:   the coder commit we're guarding against
Then asserts on whether the deps-coherence violation appears.
"""

import os
import subprocess as _sp

import pytest

# post_coder reads PM_API_URL at import time — set it before importing.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_coder import _post_coder_lint_check  # noqa: E402


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


def _write_and_commit(repo, files: dict, message: str):
    for rel, content in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _violation(violations):
    """Return the deps-coherence violation if present, else None."""
    for v in violations:
        if "deps-coherence" in v:
            return v
    return None


class TestDepsGuard:
    def test_undeclared_import_fails(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "sqlalchemy>=2.0\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/api.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        }, "add fastapi endpoint without declaring dep")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None, f"expected deps-coherence violation, got: {violations}"
        assert "fastapi" in v

    def test_declared_import_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "fastapi>=0.110\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/api.py": "from fastapi import FastAPI\napp = FastAPI()\n",
        }, "add fastapi endpoint, declared")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"expected no deps-coherence violation, got: {violations}"
        )

    def test_stdlib_import_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/util.py": "import os\nimport sys\nimport json\nfrom pathlib import Path\n",
        }, "stdlib-only utility")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"stdlib must never trigger deps-coherence, got: {violations}"
        )

    def test_first_party_import_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "\n",
            "src/__init__.py": "",
            "src/models.py": "class User: pass\n",
        }, "initial")
        _write_and_commit(repo, {
            "src/api.py": "from src.models import User\nu = User()\n",
        }, "internal cross-module import")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"first-party imports must not be flagged, got: {violations}"
        )

    def test_pypi_name_mapping_yaml(self, tmp_path):
        # `import yaml` resolves to the `pyyaml` pypi package.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "pyyaml>=6.0\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/cfg.py": "import yaml\nyaml.safe_load('a: 1')\n",
        }, "add yaml usage")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"`import yaml` with pyyaml declared must pass, got: {violations}"
        )

    def test_pypi_name_mapping_jwt(self, tmp_path):
        # `import jwt` → pypi: PyJWT.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "PyJWT>=2.8\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/auth.py": "import jwt\njwt.encode({}, 'k')\n",
        }, "add jwt usage")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"`import jwt` with PyJWT declared must pass (PEP-503 canonical), "
            f"got: {violations}"
        )

    def test_dev_requirements_count(self, tmp_path):
        # A dep only in requirements-dev.txt should still count as declared.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "\n",
            "requirements-dev.txt": "pytest>=7.0\n",
            "tests/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "tests/test_foo.py": "import pytest\ndef test_x(): assert 1\n",
        }, "add a test")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"dev-deps must count toward declared set, got: {violations}"
        )

    def test_no_requirements_skips_guard(self, tmp_path):
        # No requirements.txt at all = non-Python (or pre-managed) project; skip.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "package.json": '{"name":"x"}\n',
            "src/index.js": "console.log('x');\n",
        }, "initial")
        _write_and_commit(repo, {
            # Even a Python file with an undeclared import should not trigger
            # the guard when no requirements.txt exists.
            "scripts/oneoff.py": "import requests\nrequests.get('x')\n",
        }, "add a stray python script with undeclared import")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"no requirements.txt → guard must be silent, got: {violations}"
        )

    def test_passlib_with_extra_marker(self, tmp_path):
        # `passlib[bcrypt]>=1.7` should be parsed as `passlib`.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "passlib[bcrypt]>=1.7,<2.0\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/auth.py": "from passlib.hash import bcrypt\nbcrypt.hash('x')\n",
        }, "use passlib")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"`passlib[bcrypt]` extras spec must be parsed as `passlib`, "
            f"got: {violations}"
        )

    def test_multiple_undeclared_listed_in_message(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "requirements.txt": "\n",
            "src/__init__.py": "",
        }, "initial")
        _write_and_commit(repo, {
            "src/api.py": (
                "from fastapi import FastAPI\n"
                "from celery import Celery\n"
                "import httpx\n"
            ),
        }, "add three undeclared imports")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None
        for pkg in ("fastapi", "celery", "httpx"):
            assert pkg in v, f"expected {pkg} in violation message, got: {v}"
