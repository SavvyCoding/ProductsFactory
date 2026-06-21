"""Scope-guard partial-commit tests for post_doc.

The designer may only commit docs/story_*.md (+ documented append targets).
Historically one out-of-scope path bounced the WHOLE commit, which deadlocked
spec_defect escalations: the designer was routed in to FIX a story doc, but its
`git add -A` commit also swept an out-of-scope file (ARCHITECTURE.md), so the
corrected doc never landed and the feature looped forever (DogTinder
#1872/#1881/#1892, 2026-06-21).

`_scan_post_doc_scope` now returns the out-of-scope PATHS so the pipeline can
DROP them (`git reset HEAD -- <paths>`) and still commit the in-scope design
doc. These tests pin the scanner + the drop mechanic against a real git repo.
"""
import os
import subprocess

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_doc import (  # noqa: E402
    _scan_post_doc_scope,
    _post_doc_allowlist_check,
)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _mk_repo(tmp_path, baseline_files=None):
    """Init a git repo with an initial commit. `baseline_files` is a dict of
    {relpath: content} committed as HEAD so later modifications register as M."""
    repo = tmp_path
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base")
    for rel, content in (baseline_files or {}).items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _run_factory(repo):
    def _run(cmd, **kw):
        kw.pop("timeout", None)
        return subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    return _run


def test_in_scope_only_is_clean(tmp_path):
    repo = _mk_repo(tmp_path)
    (repo / "docs").mkdir()
    (repo / "docs" / "story_001.md").write_text("# design")
    _git(repo, "add", "-A")
    assert _scan_post_doc_scope(str(repo), _run_factory(repo)) == []
    assert _post_doc_allowlist_check(str(repo), _run_factory(repo)) == []


def test_new_architecture_md_is_allowed_on_add(tmp_path):
    # A (newly-added) ARCHITECTURE.md is greenfield renderer output — allowed.
    repo = _mk_repo(tmp_path)
    (repo / "ARCHITECTURE.md").write_text("# arch")
    (repo / "docs").mkdir()
    (repo / "docs" / "story_001.md").write_text("# design")
    _git(repo, "add", "-A")
    assert _scan_post_doc_scope(str(repo), _run_factory(repo)) == []


def test_out_of_scope_modifications_flagged_not_in_scope(tmp_path):
    repo = _mk_repo(tmp_path, {"ARCHITECTURE.md": "arch", "src/main.py": "x=1\n"})
    (repo / "ARCHITECTURE.md").write_text("arch changed")   # M, out of scope
    (repo / "src" / "main.py").write_text("x=2\n")          # M, out of scope
    (repo / "docs").mkdir()
    (repo / "docs" / "story_007.md").write_text("# d")       # A, in scope
    _git(repo, "add", "-A")
    scan = _scan_post_doc_scope(str(repo), _run_factory(repo))
    assert sorted(d["path"] for d in scan) == ["ARCHITECTURE.md", "src/main.py"]
    # message wrapper agrees on count
    assert len(_post_doc_allowlist_check(str(repo), _run_factory(repo))) == 2


def test_dropping_out_of_scope_keeps_in_scope_staged(tmp_path):
    """The fix mechanic: after `git reset HEAD -- <oos>`, the in-scope design
    doc is still staged and the scan is clean — so the commit proceeds with
    only the doc fix (loop broken)."""
    repo = _mk_repo(tmp_path, {"src/main.py": "x=1\n"})
    (repo / "src" / "main.py").write_text("x=2\n")           # out of scope
    (repo / "docs").mkdir()
    (repo / "docs" / "story_007.md").write_text("# corrected design")
    _git(repo, "add", "-A")

    oos = [d["path"] for d in _scan_post_doc_scope(str(repo), _run_factory(repo))]
    assert oos == ["src/main.py"]

    _git(repo, "reset", "-q", "HEAD", "--", *oos)

    staged = _git(repo, "diff", "--cached", "--name-only").stdout.split()
    assert "docs/story_007.md" in staged      # in-scope doc still ships
    assert "src/main.py" not in staged        # out-of-scope dropped
    # scan now clean → pipeline would commit the doc
    assert _scan_post_doc_scope(str(repo), _run_factory(repo)) == []


def test_only_out_of_scope_leaves_nothing_staged(tmp_path):
    """When the designer staged ONLY out-of-scope edits, dropping them leaves
    nothing staged → the pipeline genuinely bounces (MyDocusign #608 class)."""
    repo = _mk_repo(tmp_path, {"ARCHITECTURE.md": "arch"})
    (repo / "ARCHITECTURE.md").write_text("mangled")          # M, out of scope
    _git(repo, "add", "-A")
    oos = [d["path"] for d in _scan_post_doc_scope(str(repo), _run_factory(repo))]
    assert oos == ["ARCHITECTURE.md"]
    _git(repo, "reset", "-q", "HEAD", "--", *oos)
    # nothing staged left
    assert _git(repo, "diff", "--cached", "--quiet").returncode == 0
