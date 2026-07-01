"""
Regression test for the 2026-06-25 root-cause fix: `session_summary.md` must NOT
be in `_CODER_DENYLIST`.

The coder is told (brownfield.md / AGENT_WORKFLOW.md) to write its
`## AC<N> verification:` evidence blocks into session_summary.md, and the reviewer
(reviewer.md) requires them on the committed branch. The 2026-05-21 MyDocusign
audit blanket-added session_summary.md to the denylist, which silently STRIPPED
the coder's own evidence from every commit → the branch never got the blocks →
eternal "AC verification blocks missing" reject loop (HomeChoreService).
"""

import os
import subprocess

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_coder import _coder_stage_with_denylist, _CODER_DENYLIST


def _git_runner(repo):
    def _run(cmd, **kw):
        kw.pop("cwd", None)
        return subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True)
    return _run


def test_session_summary_not_in_denylist():
    # The coder must be able to commit its own AC-evidence file.
    assert "session_summary.md" not in _CODER_DENYLIST
    # PM-curated / other-persona files the coder genuinely must not touch stay denied.
    for f in ("CLAUDE.md", "ARCHITECTURE.md", "quality_gates.json", "product_memory.md"):
        assert f in _CODER_DENYLIST


def test_stager_keeps_session_summary_strips_pm_curated(tmp_path):
    run = _git_runner(tmp_path)
    run(["git", "init", "-q", "-b", "main"])
    run(["git", "config", "user.email", "t@t.t"])
    run(["git", "config", "user.name", "t"])
    # session_summary.md + a PM-curated file start TRACKED (committed) — mirrors prod
    (tmp_path / "session_summary.md").write_text("# session\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("curated\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("x = 1\n", encoding="utf-8")
    run(["git", "add", "-A"])
    run(["git", "commit", "-qm", "init"])
    # coder modifies all three (appending an AC block to session_summary.md)
    (tmp_path / "session_summary.md").write_text(
        "# session\n## AC1 verification:\nOK\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("curated\nhacked\n", encoding="utf-8")
    (tmp_path / "src.py").write_text("x = 2\n", encoding="utf-8")

    _staged_count, stripped = _coder_stage_with_denylist(str(tmp_path), run, "t")

    # session_summary.md survives the stager; CLAUDE.md is stripped
    assert not any("session_summary.md" in s for s in stripped), stripped
    assert any("CLAUDE.md" in s for s in stripped), stripped
    # and it's actually staged → it WILL be committed and reach the reviewed branch
    cached = run(["git", "diff", "--cached", "--name-only"]).stdout
    assert "session_summary.md" in cached
    assert "src.py" in cached
    assert "CLAUDE.md" not in cached
