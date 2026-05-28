"""
Tests for Guard 12 (DB connection lifecycle heuristic) in _post_coder_lint_check.

Regression for the 2026-05-28 calc3 #1022 infinite-loop incident: the coder
wrote correct code using `with sqlite3.connect(...) as conn:` everywhere, but
the heuristic's `has_with` regex only recognized helper names ending in
`_connection`/`_conn`, so the stdlib pattern read as a leak. The remedy hint
then pointed at a `get_db_connection()` helper that didn't exist → the coder
could never satisfy it → ~12 lint bounces in a tight loop.

Same temp-git-repo strategy as test_post_coder_deps_guard.py.
"""

import os
import subprocess as _sp

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


def _leak_violation(violations):
    for v in violations:
        if "DB connection leak" in v:
            return v
    return None


# requirements.txt present so Guard 18 stays quiet on the psycopg2 case; sqlite3
# is stdlib so it never needs a declaration.
_REQS = "psycopg2-binary>=2.9\n"


class TestDBLifecycleGuard:
    def test_with_sqlite3_connect_not_flagged(self, tmp_path):
        # THE calc3 #1022 case: correct `with sqlite3.connect()` + a raise.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"requirements.txt": _REQS, "src/__init__.py": ""}, "init")
        _write_and_commit(repo, {
            "src/users.py": (
                "import sqlite3\n"
                "def get_user(uid):\n"
                "    with sqlite3.connect('db') as conn:\n"
                "        row = conn.execute('SELECT 1 WHERE id=?', (uid,)).fetchone()\n"
                "        if row is None:\n"
                "            raise ValueError('not found')\n"
                "        return row\n"
            ),
        }, "correct with-block usage + a raise")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _leak_violation(violations) is None, (
            f"`with sqlite3.connect()` must not be flagged as a leak, got: {violations}"
        )

    def test_with_psycopg2_connect_not_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"requirements.txt": _REQS, "src/__init__.py": ""}, "init")
        _write_and_commit(repo, {
            "src/store.py": (
                "import psycopg2\n"
                "def f():\n"
                "    with psycopg2.connect('dsn') as conn:\n"
                "        if not conn:\n"
                "            raise RuntimeError('x')\n"
            ),
        }, "psycopg2 with-block + raise")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _leak_violation(violations) is None, (
            f"`with psycopg2.connect()` must not be flagged, got: {violations}"
        )

    def test_with_closing_not_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"requirements.txt": _REQS, "src/__init__.py": ""}, "init")
        _write_and_commit(repo, {
            "src/store.py": (
                "import sqlite3\n"
                "from contextlib import closing\n"
                "def f():\n"
                "    with closing(sqlite3.connect('db')) as conn:\n"
                "        if True:\n"
                "            raise ValueError('x')\n"
            ),
        }, "contextlib.closing + raise")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _leak_violation(violations) is None, (
            f"`with closing(...)` must not be flagged, got: {violations}"
        )

    def test_genuine_leak_still_flagged(self, tmp_path):
        # No `with`, no `finally: close()`, opens a connection and raises → leak.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"requirements.txt": _REQS, "src/__init__.py": ""}, "init")
        _write_and_commit(repo, {
            "src/leak.py": (
                "import sqlite3\n"
                "def f(uid):\n"
                "    conn = sqlite3.connect('db')\n"
                "    row = conn.execute('SELECT 1').fetchone()\n"
                "    if row is None:\n"
                "        raise ValueError('boom')  # conn leaks on this path\n"
                "    return row\n"
            ),
        }, "genuine leak: bare connect + raise")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _leak_violation(violations) is not None, (
            f"a real leak (bare connect + raise, no with/finally) must still "
            f"be flagged, got: {violations}"
        )

    def test_finally_close_not_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"requirements.txt": _REQS, "src/__init__.py": ""}, "init")
        _write_and_commit(repo, {
            "src/store.py": (
                "import sqlite3\n"
                "def f():\n"
                "    conn = sqlite3.connect('db')\n"
                "    try:\n"
                "        raise ValueError('x')\n"
                "    finally:\n"
                "        conn.close()\n"
            ),
        }, "explicit finally: close")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _leak_violation(violations) is None, (
            f"`finally: conn.close()` must not be flagged, got: {violations}"
        )
