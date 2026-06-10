"""
Tests for _verify_architect_edit_claims (post_maintenance, 2026-06-09).

Architect sessions report ARCHITECTURE.md edits ("3 DEPRECATED entries
added") in product_memory.md / review docs that were never persisted.
The verifier checks staged ADDED lines for such claims and, when
ARCHITECTURE.md is absent from the staged diff, appends a correction note
to product_memory.md so the false history is flagged at the source the
next session reads.

Same temp-git-repo strategy as test_post_coder_deps_guard.py.
"""

import os
import subprocess as _sp

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_maintenance import (  # noqa: E402
    _verify_architect_edit_claims,
)


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


def _commit_all(repo, message="seed"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


class TestVerifyArchitectEditClaims:
    def test_unbacked_deprecated_claim_annotated(self, tmp_path):
        # Canonical MyJira shape: memory says entries were added; the file
        # wasn't touched.
        repo = _init_repo(tmp_path)
        _write(repo, "product_memory.md", "# Memory\n")
        _write(repo, "ARCHITECTURE.md", "## DEPRECATED\n")
        _commit_all(repo)
        _write(repo, "product_memory.md",
               "# Memory\n- 3 DEPRECATED entries added: SRC/, init_db(), grep tests\n")
        _git(repo, "add", "--", "product_memory.md")

        n = _verify_architect_edit_claims(str(repo), _make_run(str(repo)))
        assert n == 1
        body = (repo / "product_memory.md").read_text(encoding="utf-8")
        assert "ORCHESTRATOR-NOTE" in body
        assert "UNVERIFIED" in body
        # The note itself is staged so it lands in the commit.
        staged = _git(repo, "diff", "--cached", "--", "product_memory.md").stdout
        assert "ORCHESTRATOR-NOTE" in staged

    def test_claim_backed_by_staged_architecture_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "product_memory.md", "# Memory\n")
        _write(repo, "ARCHITECTURE.md", "## DEPRECATED\n")
        _commit_all(repo)
        _write(repo, "product_memory.md",
               "# Memory\n- 1 DEPRECATED entry added: init_db()\n")
        _write(repo, "ARCHITECTURE.md",
               "## DEPRECATED\n- `init_db()` — use migrations\n")
        _git(repo, "add", "-A")

        n = _verify_architect_edit_claims(str(repo), _make_run(str(repo)))
        assert n == 0
        body = (repo / "product_memory.md").read_text(encoding="utf-8")
        assert "ORCHESTRATOR-NOTE" not in body

    def test_no_claims_no_annotation(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "product_memory.md", "# Memory\n")
        _commit_all(repo)
        _write(repo, "product_memory.md",
               "# Memory\n- reviewed module layout; no drift found\n")
        _git(repo, "add", "--", "product_memory.md")

        assert _verify_architect_edit_claims(str(repo), _make_run(str(repo))) == 0
        assert "ORCHESTRATOR-NOTE" not in (
            (repo / "product_memory.md").read_text(encoding="utf-8")
        )

    def test_claim_in_review_doc_annotated(self, tmp_path):
        # testingcalc shape: the claim lives in docs/architecture_review_*.md.
        repo = _init_repo(tmp_path)
        _write(repo, "product_memory.md", "# Memory\n")
        _commit_all(repo)
        _write(repo, "docs/architecture_review_2026-06-09.md",
               "Updated MODULES row added for src/db.py\n")
        _git(repo, "add", "--", "docs/architecture_review_2026-06-09.md")

        n = _verify_architect_edit_claims(str(repo), _make_run(str(repo)))
        assert n == 1
        body = (repo / "product_memory.md").read_text(encoding="utf-8")
        assert "ORCHESTRATOR-NOTE" in body

    def test_pre_existing_claims_not_rescanned(self, tmp_path):
        # Only ADDED lines count — a claim already committed long ago must
        # not re-trigger on an unrelated staged edit.
        repo = _init_repo(tmp_path)
        _write(repo, "product_memory.md",
               "# Memory\n- 3 DEPRECATED entries added (2026-06-01)\n")
        _commit_all(repo)
        _write(repo, "product_memory.md",
               "# Memory\n- 3 DEPRECATED entries added (2026-06-01)\n"
               "- velocity note: nothing to report\n")
        _git(repo, "add", "--", "product_memory.md")

        assert _verify_architect_edit_claims(str(repo), _make_run(str(repo))) == 0
