"""Tests for the phantom design_doc_path guard in
orchestrator.session.state_machine._apply_session_entry.

The designer agent can write `{"id": <fid>, "status": "Designed",
"design_doc_path": "docs/story_<fid>.md"}` to session_result.json. If the
file actually exists on disk, that's a valid Designed transition. If the
file is missing (designer wandered to a different feature, or hallucinated
the path), persisting design_doc_path leaves the DB with a stale value
that no file satisfies — the next coder cycle reads it, finds no spec,
and fails. The guard strips design_doc_path from the patch in that case;
the rest of the patch still applies so other state transitions land.

Canonical 2026-06-01 incident: DocumentSign feature 1178 had
design_doc_path=`docs/story_1178.md` in the DB but the file was missing
from the working tree (drift-scanner flagged it as design_doc_missing).
"""
from __future__ import annotations

import os

# state_machine imports at module-import time read os.environ defensively;
# match other tests' convention.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from pathlib import Path

import httpx

from orchestrator.session.state_machine import _apply_session_entry


def _client_capturing_patches(captured: list[dict]) -> httpx.Client:
    """Return an httpx.Client whose every PATCH appends the JSON body to
    `captured`. GETs return 404 so the rank-downgrade guard sees no
    current_status and proceeds with the patch (the rank check is not the
    behavior under test here)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            import json as _json
            body = _json.loads(request.content or b"{}")
            captured.append({"url": str(request.url), "body": body})
            return httpx.Response(200, json={"ok": True})
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(500)

    transport = httpx.MockTransport(handler)
    return httpx.Client(base_url="http://pm-api:8080", transport=transport)


class TestPhantomDesignDocPathGuard:
    def test_strips_phantom_path_when_file_missing(self, tmp_path):
        """Doc file absent → design_doc_path stripped from patch, rest
        of the patch still applies."""
        captured: list[dict] = []
        client = _client_capturing_patches(captured)
        try:
            entry = {
                "id": 1178,
                "status": "Designed",
                "design_doc_path": "docs/story_1178.md",
            }
            ok = _apply_session_entry(client, entry, working_dir=str(tmp_path))
        finally:
            client.close()
        assert ok is True, "patch should still apply (just without design_doc_path)"
        assert len(captured) == 1
        body = captured[0]["body"]
        assert "design_doc_path" not in body, (
            f"phantom design_doc_path should have been stripped, got body={body}"
        )
        assert body.get("status") == "Designed", (
            f"status should still be patched, got body={body}"
        )

    def test_keeps_path_when_file_exists(self, tmp_path):
        """Doc file present → design_doc_path passes through to the patch."""
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "story_1178.md").write_text("# Spec\n", encoding="utf-8")

        captured: list[dict] = []
        client = _client_capturing_patches(captured)
        try:
            entry = {
                "id": 1178,
                "status": "Designed",
                "design_doc_path": "docs/story_1178.md",
            }
            ok = _apply_session_entry(client, entry, working_dir=str(tmp_path))
        finally:
            client.close()
        assert ok is True
        assert len(captured) == 1
        body = captured[0]["body"]
        assert body.get("design_doc_path") == "docs/story_1178.md", (
            f"existing file's path should pass through, got body={body}"
        )
        assert body.get("status") == "Designed"

    def test_guard_disabled_without_working_dir(self, tmp_path):
        """When working_dir is omitted (legacy callers / tests), the guard
        is silent — design_doc_path passes through unchanged so we don't
        change behavior for callers that haven't opted in."""
        captured: list[dict] = []
        client = _client_capturing_patches(captured)
        try:
            entry = {
                "id": 1178,
                "status": "Designed",
                "design_doc_path": "docs/story_1178.md",
            }
            # No working_dir argument — back-compat path.
            ok = _apply_session_entry(client, entry)
        finally:
            client.close()
        assert ok is True
        body = captured[0]["body"]
        assert body.get("design_doc_path") == "docs/story_1178.md", (
            f"without working_dir, path must pass through unchanged, got body={body}"
        )

    def test_no_design_doc_path_in_entry_is_fine(self, tmp_path):
        """Entries that don't touch design_doc_path are unaffected by the
        guard — the guard only fires when patch_body actually carries it."""
        captured: list[dict] = []
        client = _client_capturing_patches(captured)
        try:
            entry = {"id": 1178, "status": "Implementing"}
            ok = _apply_session_entry(client, entry, working_dir=str(tmp_path))
        finally:
            client.close()
        assert ok is True
        body = captured[0]["body"]
        assert "design_doc_path" not in body
        assert body.get("status") == "Implementing"
