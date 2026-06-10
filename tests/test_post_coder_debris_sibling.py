"""
Tests for the Guard 13a sibling-required retune (2026-06-09).

`_v[0-9]+` and `_complete` filename patterns also match legitimate naming
(`api_v2.py` REST versioning, `mark_complete.py` handlers) — and 13a is the
AUTO-RM category, so an unconditional match deletes legit files. The retune
counts them as debris only when a bare-stem sibling exists in the same
directory (`main.py` alongside `main_complete.py`).

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


def _debris_violation(violations):
    for v in violations:
        if "agent-debris" in v:
            return v
    return None


class TestDebrisSiblingRequired:
    def test_v2_without_bare_sibling_passes(self, tmp_path):
        # api_v2.py with no api.py — REST versioning, NOT debris.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"src/__init__.py": ""}, "initial")
        _write_and_commit(repo, {
            "src/api_v2.py": "def handler():\n    return 2\n",
        }, "add v2 api module")
        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _debris_violation(violations) is None

    def test_v2_with_bare_sibling_flagged(self, tmp_path):
        # main.py + main_v2.py — the rework-copy debris pattern.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "src/main.py": "def main():\n    pass\n",
        }, "initial")
        _write_and_commit(repo, {
            "src/main_v2.py": "def main():\n    pass  # rewrite\n",
        }, "agent rework copy")
        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _debris_violation(violations)
        assert v is not None
        assert "main_v2.py" in v

    def test_complete_handler_without_sibling_passes(self, tmp_path):
        # mark_complete.py with no mark.py — verb_noun handler, NOT debris.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"src/__init__.py": ""}, "initial")
        _write_and_commit(repo, {
            "src/mark_complete.py": "def mark_complete(task):\n    task.done = True\n",
        }, "add completion handler")
        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _debris_violation(violations) is None

    def test_complete_with_bare_sibling_flagged(self, tmp_path):
        # main.py + main_complete.py — canonical Calculator debris.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "src/main.py": "def main():\n    pass\n",
        }, "initial")
        _write_and_commit(repo, {
            "src/main_complete.py": "def main():\n    pass\n",
        }, "agent debris copy")
        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _debris_violation(violations)
        assert v is not None
        assert "main_complete.py" in v

    def test_unconditional_patterns_still_fire(self, tmp_path):
        # .bak stays unconditional — no sibling requirement.
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {"src/__init__.py": ""}, "initial")
        _write_and_commit(repo, {
            "src/main.py.bak": "old content\n",
        }, "agent left a backup")
        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _debris_violation(violations)
        assert v is not None
        assert "main.py.bak" in v
