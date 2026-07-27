"""Designer-side AC-recipe self-check (2026-07-27) — statically validate the
`Verify:` recipes in a staged design doc before they can reach a coder."""
import os
from unittest.mock import MagicMock

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines import post_doc


def _fake_run(staged_doc="docs/story_777.md", bad_marker="UNCLOSED"):
    """Fakes git-diff (returns one staged doc) and bash -n (fails syntax on any
    command containing bad_marker, else passes)."""
    def run(cmd, **kw):
        r = MagicMock()
        r.stdout = ""
        r.stderr = ""
        r.returncode = 0
        if cmd[0] == "git" and "diff" in cmd:
            r.stdout = staged_doc + "\n"
        elif cmd[0] == "bash" and cmd[1] == "-n":
            if bad_marker in cmd[3]:
                r.returncode = 2
                r.stderr = "bash: -c: line 1: unexpected EOF while looking for matching `\"'"
        return r
    return run


def _write_doc(tmp_path, body):
    doc = tmp_path / "docs" / "story_777.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(body, encoding="utf-8")


def test_flags_invalid_shell_syntax(tmp_path):
    _write_doc(tmp_path, (
        "AC1. Good.\n"
        "     Verify: `echo hello`\n"
        "     Expected: hello\n"
        "AC2. Broken (UNCLOSED bracket).\n"
        "     Verify: `echo UNCLOSED{`\n"
        "     Expected: whatever\n"
    ))
    v = post_doc._post_doc_ac_recipe_check(str(tmp_path), _fake_run(), [{"id": 777}], "P")
    assert len(v) == 1
    assert "AC2" in v[0] and "syntax" in v[0].lower()


def test_flags_malformed_expected_json(tmp_path):
    _write_doc(tmp_path, (
        "AC1. Bad JSON expected.\n"
        "     Verify: `echo x`\n"
        "     Expected: `{\"a\": 1, }garbage`\n"
    ))
    v = post_doc._post_doc_ac_recipe_check(str(tmp_path), _fake_run(), [{"id": 777}], "P")
    assert len(v) == 1
    assert "Expected block looks like JSON" in v[0]


def test_clean_doc_passes(tmp_path):
    _write_doc(tmp_path, (
        "AC1. All good.\n"
        "     Verify: `echo hello world`\n"
        "     Expected: hello world\n"
        "AC2. Valid JSON expected.\n"
        "     Verify: `echo json`\n"
        "     Expected: `{\"ok\": true}`\n"
    ))
    v = post_doc._post_doc_ac_recipe_check(str(tmp_path), _fake_run(), [{"id": 777}], "P")
    assert v == []


def test_no_staged_docs_is_clean(tmp_path):
    def run(cmd, **kw):
        r = MagicMock(); r.stdout = ""; r.stderr = ""; r.returncode = 0
        return r
    v = post_doc._post_doc_ac_recipe_check(str(tmp_path), run, [{"id": 777}], "P")
    assert v == []
