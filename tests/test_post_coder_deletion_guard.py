"""
Tests for Guard 17 (AST-diff deletion safety) in _post_coder_lint_check.

Strategy: build a real temp git repo, drive real `git` commands, and call the
real `_post_coder_lint_check`. No mocking — the guard's correctness depends on
git plumbing + AST parsing, both of which are awkward to fake.

Each test sets up two commits:
  - HEAD~1: initial state
  - HEAD:   the "coder commit" we're guarding against
Then calls _post_coder_lint_check and inspects the returned violations list
for the deletion-safety message.
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
    """Mirrors the _run closure inside _run_post_coder_pipeline."""
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


def _write_and_commit(repo, files: dict[str, str], message: str):
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _violation(violations):
    """Return the deletion-safety violation if present, else None."""
    for v in violations:
        if "deletion-safety" in v:
            return v
    return None


class TestDeletionGuard:
    def test_deletion_with_surviving_caller_fails(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def compute_total(x):\n    return x + 1\n",
            "b.py": "from a import compute_total\nprint(compute_total(1))\n",
        }, "initial")
        _write_and_commit(repo, {
            "a.py": "def something_else():\n    return 0\n",
        }, "coder removes compute_total but b.py still calls it")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None, f"expected deletion-safety violation, got: {violations}"
        assert "compute_total" in v
        assert "b.py" in v

    def test_deletion_with_co_modified_caller_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def compute_total(x):\n    return x + 1\n",
            "b.py": "from a import compute_total\nprint(compute_total(1))\n",
        }, "initial")
        # Coder removes compute_total AND updates the caller in the same commit.
        _write_and_commit(repo, {
            "a.py": "def something_else():\n    return 0\n",
            "b.py": "print('no more call')\n",
        }, "coder removes compute_total and updates caller")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"expected no deletion-safety violation when caller is co-modified, "
            f"got: {violations}"
        )

    def test_private_helper_deletion_not_flagged(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def _helper(x):\n    return x\n",
            "b.py": "from a import _helper\nprint(_helper(1))\n",
        }, "initial")
        # _helper is private (leading underscore) — guard should ignore even
        # though b.py still imports it. Module-internal convention.
        _write_and_commit(repo, {
            "a.py": "def public_thing():\n    return 1\n",
        }, "coder removes _helper")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"private helpers must not trigger deletion-safety, got: {violations}"
        )

    def test_rename_with_callers_updated_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def compute_total(x):\n    return x + 1\n",
            "b.py": "from a import compute_total\nprint(compute_total(1))\n",
        }, "initial")
        # Rename compute_total -> calculate_total in both files (coder did it
        # consistently). Guard sees compute_total removed from a.py, but b.py
        # is co-modified so it's excluded from the surviving-caller search.
        _write_and_commit(repo, {
            "a.py": "def calculate_total(x):\n    return x + 1\n",
            "b.py": "from a import calculate_total\nprint(calculate_total(1))\n",
        }, "rename compute_total -> calculate_total everywhere")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None, (
            f"consistent rename across both files must pass, got: {violations}"
        )

    def test_rename_without_caller_update_fails(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def compute_total(x):\n    return x + 1\n",
            "b.py": "from a import compute_total\nprint(compute_total(1))\n",
        }, "initial")
        # Coder renames in a.py but forgets b.py — classic accidental
        # break that the guard exists to catch.
        _write_and_commit(repo, {
            "a.py": "def calculate_total(x):\n    return x + 1\n",
        }, "rename in a.py only")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None, (
            f"expected violation when rename leaves stale callers, "
            f"got: {violations}"
        )
        assert "compute_total" in v

    def test_class_deletion_with_surviving_caller_fails(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "class Widget:\n    def use(self):\n        return 1\n",
            "b.py": "from a import Widget\nWidget().use()\n",
        }, "initial")
        _write_and_commit(repo, {
            "a.py": "class Sprocket:\n    pass\n",
        }, "remove Widget class")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None, f"class deletion must be caught, got: {violations}"
        assert "Widget" in v

    def test_entire_file_deletion_flags_surviving_callers(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "a.py": "def compute_total(x):\n    return x + 1\n",
            "b.py": "from a import compute_total\nprint(compute_total(1))\n",
        }, "initial")
        # Delete a.py entirely — all its public symbols are now "removed".
        (repo / "a.py").unlink()
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "remove a.py")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        v = _violation(violations)
        assert v is not None, f"file deletion must flag surviving callers, got: {violations}"
        assert "compute_total" in v

    def test_no_python_files_skips_guard(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write_and_commit(repo, {
            "README.md": "# product\n",
        }, "initial")
        _write_and_commit(repo, {
            "README.md": "# product (updated)\n",
        }, "doc change")

        violations = _post_coder_lint_check(str(repo), _make_run(str(repo)))
        assert _violation(violations) is None
