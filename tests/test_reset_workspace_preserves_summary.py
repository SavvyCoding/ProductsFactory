"""
Regression test for the 2026-05-28 session-continuity bug.

_reset_workspace runs `git clean -fdx` before each session. session_summary.md
is gitignored (a per-session artefact), so without an explicit exclude the
clean wiped it — and it was wiped BEFORE docker_runner._read_session_summary
read it to build the next session's {prev_session_summary}. Net effect: every
session got an empty prior-summary.

This test sets up a real temp git repo with a gitignored session_summary.md
plus a gitignored junk file, monkeypatches _reset_workspace's network/docker
side-effects to no-ops, runs the real reset, and asserts session_summary.md
survives the clean while the junk file is removed.
"""
import os
import subprocess as _sp

import pytest

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.integrations import git_ops  # noqa: E402


def _git(cwd, *args):
    return _sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Real git repo on main with a committed .gitignore.
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("session_summary.md\n*.pyc\n")
    (tmp_path / "README.md").write_text("# product\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")

    # Neutralize the side-effect helpers so we exercise only the
    # checkout/reset/clean cascade against the real repo.
    monkeypatch.setattr(git_ops, "_chmod_workspace_via_alpine", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "_remove_stale_git_lock", lambda *a, **k: None)
    monkeypatch.setattr(git_ops, "_enforce_https_origin", lambda *a, **k: None)
    # No origin remote in the test; fetch is a no-op. reset --hard origin/main
    # then fails-and-logs (no remote ref), but the cascade continues to clean.
    monkeypatch.setattr(
        git_ops, "git_fetch_authenticated",
        lambda *a, **k: _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
    )
    return tmp_path


def test_session_summary_survives_reset(repo):
    # Gitignored continuity doc + gitignored junk.
    (repo / "session_summary.md").write_text("prior session notes")
    (repo / "junk.pyc").write_text("bytecode")

    git_ops._reset_workspace(str(repo), "test-product")

    # session_summary.md is excluded from the clean → survives.
    assert (repo / "session_summary.md").is_file(), \
        "session_summary.md must survive the workspace reset (continuity depends on it)"
    assert (repo / "session_summary.md").read_text() == "prior session notes"

    # An un-excluded gitignored file is removed by `git clean -fdx`.
    assert not (repo / "junk.pyc").exists(), \
        "git clean -fdx should still remove non-excluded ignored files"


def test_excluded_artifact_dirs_also_survive(repo):
    # The pre-existing excludes (output/, Results/, Temp/) still hold.
    for d in ("output", "Results", "Temp"):
        (repo / d).mkdir()
        (repo / d / "artifact.txt").write_text("keep me")

    git_ops._reset_workspace(str(repo), "test-product")

    for d in ("output", "Results", "Temp"):
        assert (repo / d / "artifact.txt").is_file(), f"{d}/ should survive the clean"


def test_reset_prunes_orphaned_worktree_holding_main(repo, tmp_path):
    """Regression for the 2026-06-21 DogTinder /tmp/maincheck wedge.

    A post-coder baseline worktree that checked out `main` and whose directory
    later vanished (container recreated mid-pipeline) leaves orphaned metadata
    in .git/worktrees/ that still HOLDS `main`. Every `git checkout -f main` in
    _reset_workspace then fails rc=128 ("'main' is already used by worktree at
    ...") — silently breaking the whole reset, so the workspace never gets
    cleaned and cross-feature state accumulates (fuelled the #1872/#1881/#1892
    loops). _reset_workspace now `git worktree prune`s first.
    """
    import shutil

    # Leave `main` free by moving the primary checkout onto a session branch.
    _git(repo, "checkout", "-b", "coder/abc123")
    # A worktree checks out `main`, then its dir disappears -> prunable orphan
    # that still holds the branch.
    wt = tmp_path / "orphan_wt"
    _git(repo, "worktree", "add", str(wt), "main")
    shutil.rmtree(wt)

    # Pre-condition: with the orphan present, a plain checkout of main fails.
    pre = _sp.run(["git", "checkout", "-f", "main"], cwd=repo,
                  capture_output=True, text=True)
    assert pre.returncode != 0 and "already used by worktree" in (pre.stderr or ""), \
        "test setup must reproduce the locked-main condition"

    # The fix: reset prunes the orphan, then successfully lands on main.
    git_ops._reset_workspace(str(repo), "test-product")

    head = _sp.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo,
                   capture_output=True, text=True).stdout.strip()
    assert head == "main", \
        f"reset must land on main after pruning the orphan worktree (got {head!r})"
    wl = _sp.run(["git", "worktree", "list"], cwd=repo,
                 capture_output=True, text=True).stdout
    assert "orphan_wt" not in wl, "orphaned worktree entry should be pruned"
