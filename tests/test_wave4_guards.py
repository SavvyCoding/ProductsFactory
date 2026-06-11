"""
Wave-4 structural guards (2026-06-11, motivated by the DogTinder review):
  - Guard 5b: literal fallback inside secret-getter functions (AST)
  - Guard 13f: test_*.py at repo root when tests/ exists
  - Guard 21: CORS wildcard origins + credentials on added lines
  - Guard 22: zero-assertion test files (newly added)
"""

import os
import subprocess as _sp

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_coder import _post_coder_lint_check  # noqa: E402


# ── shared git helpers (same conventions as test_wave3_guards) ──────────────


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


def _lint(repo):
    return _post_coder_lint_check(str(repo), _make_run(str(repo)))


def _find(violations, needle):
    return next((v for v in violations if needle in v), None)


# ── Guard 5b: literal fallback inside secret-getter functions ───────────────


class TestGuard5bSecretGetterFallback:
    def test_dogtinder_shape_bounced(self, tmp_path):
        # Canonical DogTinder crypto.py:_get_key — env read + literal return.
        repo = _init_repo(tmp_path)
        _write(repo, "src/lib/crypto.py", (
            "import os\n"
            "def _get_key():\n"
            "    key_b64 = os.environ.get('ENCRYPTION_KEY')\n"
            "    if key_b64 is None:\n"
            "        return b'a' * 32\n"
            "    return key_b64.encode()\n"
        ))
        _commit_all(repo, "add crypto helper")

        v = _find(_lint(repo), "secret-getter function returns a literal fallback")
        assert v is not None
        assert "src/lib/crypto.py::_get_key" in v

    def test_plain_string_fallback_bounced(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/auth.py", (
            "import os\n"
            "def resolve_jwt_secret():\n"
            "    s = os.getenv('JWT_SECRET')\n"
            "    if not s:\n"
            "        return 'dev-secret-do-not-use'\n"
            "    return s\n"
        ))
        _commit_all(repo, "add auth helper")

        assert _find(_lint(repo),
                     "secret-getter function returns a literal fallback") is not None

    def test_fail_closed_getter_passes(self, tmp_path):
        # Correct shape: raises when the env var is missing.
        repo = _init_repo(tmp_path)
        _write(repo, "src/lib/crypto.py", (
            "import os\n"
            "def _get_key():\n"
            "    key_b64 = os.environ.get('ENCRYPTION_KEY')\n"
            "    if key_b64 is None:\n"
            "        raise RuntimeError('ENCRYPTION_KEY is required')\n"
            "    return key_b64.encode()\n"
        ))
        _commit_all(repo, "add fail-closed crypto helper")

        assert _find(_lint(repo),
                     "secret-getter function returns a literal fallback") is None

    def test_literal_without_env_read_passes(self, tmp_path):
        # get_token_type() returns "Bearer" but never reads the environment —
        # not a secret fallback.
        repo = _init_repo(tmp_path)
        _write(repo, "src/auth.py", (
            "def get_token_type():\n"
            "    return 'Bearer'\n"
        ))
        _commit_all(repo, "add token type helper")

        assert _find(_lint(repo),
                     "secret-getter function returns a literal fallback") is None

    def test_test_files_exempt(self, tmp_path):
        # Literal keys in tests/ are fixture data, not production fallbacks.
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_crypto.py", (
            "import os\n"
            "def fake_key():\n"
            "    os.environ.get('X')\n"
            "    return b'k' * 32\n"
            "def test_k():\n"
            "    assert fake_key()\n"
        ))
        _commit_all(repo, "add crypto test")

        assert _find(_lint(repo),
                     "secret-getter function returns a literal fallback") is None


# ── Guard 13f: test file at repo root ────────────────────────────────────────


class TestGuard13fRootTestFiles:
    def test_root_test_file_bounced(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_real.py", "def test_x(): assert 1\n")
        _write(repo, "test_simple.py", "print('Simple test')\n")
        _commit_all(repo, "debris at root")

        v = _find(_lint(repo), "test file at repo root")
        assert v is not None
        assert "test_simple.py" in v

    def test_root_test_without_tests_dir_passes(self, tmp_path):
        # Tiny projects that keep tests at the root are a stack convention,
        # not debris — only flag when a tests/ dir exists.
        repo = _init_repo(tmp_path)
        _write(repo, "test_app.py", "def test_x(): assert 1\n")
        _write(repo, "src/app.py", "X = 1\n")
        _commit_all(repo, "root-test convention")

        assert _find(_lint(repo), "test file at repo root") is None

    def test_exempt_marker_honoured(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_real.py", "def test_x(): assert 1\n")
        _write(repo, "test_tool.py",
               "# AGENT_DEBRIS_EXEMPT: standalone smoke entrypoint\n"
               "def test_x(): assert 1\n")
        _commit_all(repo, "exempted root test")

        assert _find(_lint(repo), "test file at repo root") is None


# ── Guard 21: CORS wildcard + credentials ────────────────────────────────────


class TestGuard21CorsMisconfig:
    def test_fastapi_wildcard_with_credentials_bounced(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/main.py", (
            "from fastapi import FastAPI\n"
            "from fastapi.middleware.cors import CORSMiddleware\n"
            "app = FastAPI()\n"
            "app.add_middleware(\n"
            "    CORSMiddleware,\n"
            "    allow_origins=['*'],\n"
            "    allow_credentials=True,\n"
            ")\n"
        ))
        _commit_all(repo, "add app with CORS")

        v = _find(_lint(repo), "CORS misconfiguration")
        assert v is not None
        assert "src/main.py" in v

    def test_pinned_origins_with_credentials_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/main.py", (
            "from fastapi import FastAPI\n"
            "from fastapi.middleware.cors import CORSMiddleware\n"
            "app = FastAPI()\n"
            "app.add_middleware(\n"
            "    CORSMiddleware,\n"
            "    allow_origins=['https://app.example.com'],\n"
            "    allow_credentials=True,\n"
            ")\n"
        ))
        _commit_all(repo, "add app with pinned CORS")

        assert _find(_lint(repo), "CORS misconfiguration") is None

    def test_wildcard_without_credentials_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/main.py", (
            "from fastapi import FastAPI\n"
            "from fastapi.middleware.cors import CORSMiddleware\n"
            "app = FastAPI()\n"
            "app.add_middleware(CORSMiddleware, allow_origins=['*'])\n"
        ))
        _commit_all(repo, "wildcard but no credentials")

        assert _find(_lint(repo), "CORS misconfiguration") is None

    def test_preexisting_misconfig_does_not_bounce_bystander(self, tmp_path):
        # The misconfig exists from a previous commit; this commit touches an
        # unrelated line in another file. Added-lines scoping must not fire.
        repo = _init_repo(tmp_path)
        _write(repo, "src/main.py", (
            "app.add_middleware(CORSMiddleware, allow_origins=['*'],\n"
            "                   allow_credentials=True)\n"
        ))
        _commit_all(repo, "legacy misconfig")
        _write(repo, "src/other.py", "Y = 2\n")
        _commit_all(repo, "unrelated change")

        assert _find(_lint(repo), "CORS misconfiguration") is None


# ── Guard 22: zero-assertion test files ──────────────────────────────────────


class TestGuard22ZeroAssertTests:
    def test_print_only_test_file_bounced(self, tmp_path):
        # Canonical DogTinder test_simple.py.
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_simple.py", "print('Simple test')\n")
        _commit_all(repo, "hollow test")

        v = _find(_lint(repo), "ZERO assertions")
        assert v is not None
        assert "tests/test_simple.py" in v

    def test_import_only_test_file_bounced(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_imports.py", (
            "from src.main import create_app\n"
            "from src.db import init_db\n"
        ))
        _write(repo, "src/main.py", "def create_app(): return 1\n")
        _write(repo, "src/db.py", "def init_db(): return 1\n")
        _commit_all(repo, "import-only test")

        assert _find(_lint(repo), "ZERO assertions") is not None

    def test_real_assertions_pass(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_calc.py", (
            "def test_add():\n"
            "    assert 1 + 1 == 2\n"
        ))
        _commit_all(repo, "real test")

        assert _find(_lint(repo), "ZERO assertions") is None

    def test_pytest_raises_counts_as_assertion(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_err.py", (
            "import pytest\n"
            "def test_boom():\n"
            "    with pytest.raises(ValueError):\n"
            "        raise ValueError('x')\n"
        ))
        _commit_all(repo, "raises-style test")

        assert _find(_lint(repo), "ZERO assertions") is None

    def test_unittest_style_counts_as_assertion(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_ut.py", (
            "import unittest\n"
            "class T(unittest.TestCase):\n"
            "    def test_x(self):\n"
            "        self.assertEqual(1, 1)\n"
        ))
        _commit_all(repo, "unittest-style test")

        assert _find(_lint(repo), "ZERO assertions") is None

    def test_modified_legacy_hollow_file_not_bounced(self, tmp_path):
        # Guard is scoped to ADDED files; modifying a pre-existing hollow
        # test must not bounce the bystander commit.
        repo = _init_repo(tmp_path)
        _write(repo, "tests/test_old.py", "print('legacy')\n")
        _commit_all(repo, "legacy hollow test")
        _write(repo, "tests/test_old.py", "print('legacy v2')\n")
        _commit_all(repo, "touch legacy file")

        assert _find(_lint(repo), "ZERO assertions") is None

    def test_js_expect_passes_and_hollow_js_bounced(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "tests/Calc.test.ts",
               "it('adds', () => { expect(1 + 1).toBe(2); });\n")
        _write(repo, "tests/Hollow.test.ts",
               "it('does nothing', () => { console.log('hi'); });\n")
        _commit_all(repo, "js tests")

        v = _find(_lint(repo), "ZERO assertions")
        assert v is not None
        assert "Hollow.test.ts" in v
        assert "Calc.test.ts" not in v
