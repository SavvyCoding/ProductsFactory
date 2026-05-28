"""
Regression test for the 2026-05-28 phantom-design_doc_path bug.

A designer assigned feature #X sometimes designs a DIFFERENT feature #Y
(judging it a prerequisite). post_doc's fallback used to mark the
ASSIGNED feature #X `Designed` with a conventional docs/story_X.md path
that was never written — stranding the #X coder with an empty spec.

_mark_assigned_features_designed now verifies the doc exists on disk
before marking Designed; if it doesn't, the feature is re-queued to
Approved (no phantom path). These tests pin that behaviour.
"""
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_doc import _mark_assigned_features_designed  # noqa: E402


def _client():
    c = MagicMock()
    c.patch.return_value = SimpleNamespace(status_code=200, raise_for_status=lambda: None)
    return c


def _patches(client):
    """Return list of (feature_id, json_body) from recorded PATCH calls."""
    out = []
    for call in client.patch.call_args_list:
        path = call.args[0]
        fid = int(path.rstrip("/").split("/")[-1])
        out.append((fid, call.kwargs["json"]))
    return out


def test_marks_designed_when_doc_exists(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "story_1006.md").write_text("# Feature Design: Calculation history")
    client = _client()
    _mark_assigned_features_designed(
        str(tmp_path),
        [{"id": 1006, "design_doc_path": "docs/story_1006.md"}],
        "designer", "Calc3", client,
    )
    patches = _patches(client)
    assert len(patches) == 1
    fid, body = patches[0]
    assert fid == 1006
    assert body["status"] == "Designed"
    assert body["design_doc_path"] == "docs/story_1006.md"


def test_requeues_when_doc_missing(tmp_path):
    # Assigned #1005 but the designer never wrote docs/story_1005.md
    # (it designed #1007 instead). #1005 must be re-queued, not fabricated.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "story_1007.md").write_text("# Feature Design: Health check endpoint")
    client = _client()
    _mark_assigned_features_designed(
        str(tmp_path),
        [{"id": 1005, "design_doc_path": "docs/story_1005.md"}],
        "designer", "Calc3", client,
    )
    patches = _patches(client)
    assert len(patches) == 1
    fid, body = patches[0]
    assert fid == 1005
    assert body["status"] == "Approved", "missing-doc feature must re-queue, not be marked Designed"
    assert body["design_doc_path"] is None, "must not record a phantom path"
    assert body["changed_by"] == "post-doc:rollback"


def test_conventional_path_when_no_design_doc_path_field(tmp_path):
    # design_doc_path unset → falls back to docs/story_{id}.md, which exists.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "story_42.md").write_text("# Feature Design: Thing")
    client = _client()
    _mark_assigned_features_designed(
        str(tmp_path), [{"id": 42}], "designer", "P", client,
    )
    fid, body = _patches(client)[0]
    assert fid == 42 and body["status"] == "Designed"
    assert body["design_doc_path"] == "docs/story_42.md"


def test_mixed_batch(tmp_path):
    # Two assigned features: one designed (doc present), one wandered (missing).
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "story_1.md").write_text("# Feature Design: A")
    client = _client()
    _mark_assigned_features_designed(
        str(tmp_path),
        [
            {"id": 1, "design_doc_path": "docs/story_1.md"},   # designed
            {"id": 2, "design_doc_path": "docs/story_2.md"},   # missing → re-queue
        ],
        "designer", "P", client,
    )
    by_id = {fid: body for fid, body in _patches(client)}
    assert by_id[1]["status"] == "Designed"
    assert by_id[2]["status"] == "Approved"
    assert by_id[2]["design_doc_path"] is None
