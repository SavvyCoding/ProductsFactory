"""
Tier-3 structural fixes (2026-06-11):

1. _validate_reviewer_outcome_consistency evaluates the TRAILING RUN of
   reviewer comments, not just the latest one — the reviewer prompt posts
   one comment per failing section plus a "✅ prior addressed"
   acknowledgement, so a legitimate changes_requested round routinely
   ENDS with a ✅ comment. The latest-only check mis-rejected those
   rounds; the forensic audit traced 5 of 7 divergent_review_feedback
   auto-Blocks to that loop.

2. Guard 4 (placeholder/TODO) scans only the commit's ADDED lines — a
   pre-existing placeholder in a file the commit merely touches must not
   bounce the feature (testingcalc #1418/#1465/#1466/#1467). TODO/FIXME
   is case-sensitive (todo.id identifiers in todo-app products are not
   markers).
"""

import os
import subprocess as _sp
from unittest.mock import MagicMock

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.session.result_io import (  # noqa: E402
    _validate_reviewer_outcome_consistency,
)
from orchestrator.pipelines.post_coder import _post_coder_lint_check  # noqa: E402


# ── validator: trailing-run sentiment ───────────────────────────────────────


def _client_with(comments):
    client = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = comments
    client.get.return_value = resp
    return client


def _c(author, body, ts):
    return {"author": author, "body": body, "created_at": ts}


class TestValidatorTrailingRun:
    def test_multi_comment_round_ending_with_ack_applies(self):
        # The canonical false-reject: ❌ section comment first, ✅ prior-
        # addressed acknowledgement LAST. changes_requested is consistent.
        comments = [
            _c("reviewer", "❌ Tests: hollow test in tests/test_x.py", "2026-06-11T01:00:00"),
            _c("reviewer", "✅ Prior comment about imports addressed in abc123.", "2026-06-11T01:00:30"),
        ]
        entry = {"id": 9, "review_outcome": "changes_requested", "status": "Implementing"}
        decision, _ = _validate_reviewer_outcome_consistency(
            entry, _client_with(comments), "[t]")
        assert decision == "apply"

    def test_pure_lgtm_with_changes_requested_still_rejected(self):
        # The original #1131 protection must survive: an all-positive run
        # with changes_requested JSON is a real mismatch.
        comments = [
            _c("reviewer", "✅ Commits abc + def: LGTM — functional/tests/security pass.", "2026-06-11T01:00:00"),
        ]
        entry = {"id": 9, "review_outcome": "changes_requested", "status": "Implementing"}
        client = _client_with(comments)
        decision, _ = _validate_reviewer_outcome_consistency(entry, client, "[t]")
        assert decision == "reject"
        client.post.assert_called_once()  # validation comment posted

    def test_approved_with_neg_anywhere_in_run_rejected(self):
        comments = [
            _c("reviewer", "✅ Functional pass.", "2026-06-11T01:00:00"),
            _c("reviewer", "❌ Security: raw error.message in response.", "2026-06-11T01:00:20"),
        ]
        entry = {"id": 9, "review_outcome": "approved", "status": "Reviewed"}
        decision, _ = _validate_reviewer_outcome_consistency(
            entry, _client_with(comments), "[t]")
        assert decision == "reject"

    def test_prior_round_sentiment_not_counted(self):
        # An old ✅ approval separated by a system comment belongs to a
        # PRIOR round — only the trailing reviewer run counts.
        comments = [
            _c("reviewer", "✅ LGTM (old round)", "2026-06-11T00:00:00"),
            _c("post-coder:test-check", "tests failed after merge of X", "2026-06-11T00:30:00"),
            _c("reviewer", "❌ Tests: regression in test_y.", "2026-06-11T01:00:00"),
        ]
        entry = {"id": 9, "review_outcome": "changes_requested", "status": "Implementing"}
        decision, _ = _validate_reviewer_outcome_consistency(
            entry, _client_with(comments), "[t]")
        assert decision == "apply"

    def test_approved_clean_run_applies(self):
        comments = [
            _c("reviewer", "✅ Commit abc: LGTM — all sections pass. nit: rename helper.", "2026-06-11T01:00:00"),
        ]
        entry = {"id": 9, "review_outcome": "approved", "status": "Reviewed"}
        decision, _ = _validate_reviewer_outcome_consistency(
            entry, _client_with(comments), "[t]")
        assert decision == "apply"


# ── Guard 4: diff-scoped placeholder detection ──────────────────────────────


def _git(cwd, *args):
    return _sp.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_run(working_dir):
    def _run(cmd, **kw):
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True,
                       timeout=timeout, **kw)
    return _run


def _init_repo(tmp_path):
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def _write(repo, rel, content):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit(repo, msg="c"):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)


def _g4_violation(violations):
    for v in violations:
        if "placeholder / TODO / NotImplementedError" in v:
            return v
    return None


class TestGuard4DiffScoped:
    def test_preexisting_placeholder_in_touched_file_passes(self, tmp_path):
        # Canonical testingcalc contagion: legacy `# placeholder` line in a
        # file the commit touches for unrelated reasons.
        repo = _init_repo(tmp_path)
        _write(repo, "src/oauth2.py",
               "def validate_state(s):\n"
               "    # placeholder until session storage lands\n"
               "    return True\n")
        _commit(repo, "initial")
        _write(repo, "src/oauth2.py",
               "import logging\n"  # unrelated added line
               "def validate_state(s):\n"
               "    # placeholder until session storage lands\n"
               "    return True\n")
        _commit(repo, "unrelated refactor touches the file")
        assert _g4_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None

    def test_added_placeholder_line_bounces(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/x.py", "def f():\n    return 1\n")
        _commit(repo, "initial")
        _write(repo, "src/x.py",
               "def f():\n    return 1\n"
               "def g():\n    raise NotImplementedError\n")
        _commit(repo, "ships a stub")
        v = _g4_violation(_post_coder_lint_check(str(repo), _make_run(str(repo))))
        assert v is not None
        assert "src/x.py" in v

    def test_lowercase_todo_identifier_passes(self, tmp_path):
        # todo-app code: `todo.id` etc. are identifiers, not markers.
        repo = _init_repo(tmp_path)
        _write(repo, "src/app.py", "x = 1\n")
        _commit(repo, "initial")
        _write(repo, "src/app.py",
               "def get_todo(todo_id):\n"
               "    todo = repo.find(todo_id)\n"
               "    return todo\n")
        _commit(repo, "todo feature")
        assert _g4_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None

    def test_uppercase_todo_marker_bounces(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/app.py", "x = 1\n")
        _commit(repo, "initial")
        _write(repo, "src/app.py", "x = 1\n# TODO: actually implement this\n")
        _commit(repo, "marker")
        assert _g4_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is not None

    def test_jsx_placeholder_attr_passes(self, tmp_path):
        repo = _init_repo(tmp_path)
        _write(repo, "src/Form.tsx", "export const F = 1;\n")
        _commit(repo, "initial")
        _write(repo, "src/Form.tsx",
               'export const F = () => <input placeholder="Enter name" />;\n')
        _commit(repo, "form input")
        assert _g4_violation(
            _post_coder_lint_check(str(repo), _make_run(str(repo)))) is None
