"""Tests for orchestrator.supervisor.detect_repeated_review_feedback.

Two layers:
  1. Pure tests for the signature helpers (no I/O). These pin the
     fingerprint stability — e.g. minor wording shifts must produce the
     same hash; semantic changes must produce a different one.
  2. Integration tests for the detector itself via httpx.MockTransport.
     We swap out httpx.Client so no real PM API is contacted; the test
     just verifies the right PATCHes happen for the no-prior, increment,
     reset, and threshold-cross cases.
"""
from __future__ import annotations

import os

# Match conftest's PM_API_URL convention so import-time os.environ access
# doesn't blow up in isolation.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

import httpx
import pytest

from orchestrator import supervisor


# ── Layer 1: pure signature tests ────────────────────────────────────────────


class TestSignatureFromComments:
    def test_two_reviewers_same_complaint_match(self):
        """Different reviewers, slightly different wording, same root issue
        — signatures must match. This is the whole point of the detector."""
        a = [
            {"author": "reviewer", "body": "❌ tests: e2e timeout at file responsive.spec.ts:103. [s1]"},
        ]
        b = [
            {"author": "reviewer", "body": "❌ tests: e2e timeout at file responsive.spec.ts:103. [s2]"},
        ]
        assert supervisor._signature_from_comments(a) == supervisor._signature_from_comments(b)

    def test_different_section_changes_signature(self):
        """A functional finding and a tests finding must hash differently
        — otherwise we'd block on unrelated issues."""
        a = [{"author": "reviewer", "body": "❌ functional: foo bar baz"}]
        b = [{"author": "reviewer", "body": "❌ tests: foo bar baz"}]
        assert supervisor._signature_from_comments(a) != supervisor._signature_from_comments(b)

    def test_different_body_changes_signature(self):
        """Same section but different finding must hash differently."""
        a = [{"author": "reviewer", "body": "❌ tests: e2e timeout on responsive.spec.ts"}]
        b = [{"author": "reviewer", "body": "❌ tests: snapshot mismatch on dark-mode.spec.ts"}]
        assert supervisor._signature_from_comments(a) != supervisor._signature_from_comments(b)

    def test_approval_comments_ignored(self):
        """`✅` comments don't carry feedback to repeat — must not be
        included in the signature."""
        only_approve = [{"author": "reviewer", "body": "✅ Commit a1b2c3: LGTM"}]
        assert supervisor._signature_from_comments(only_approve) is None

    def test_non_reviewer_comments_ignored(self):
        """Coder/PM comments must not pollute the reviewer fingerprint."""
        comments = [
            {"author": "coder", "body": "❌ tests: this should be ignored"},
            {"author": "pm",    "body": "❌ tests: also ignored"},
        ]
        assert supervisor._signature_from_comments(comments) is None

    def test_session_uid_suffix_does_not_affect_hash(self):
        """The `[<session_uid>]` tag at the end of every reviewer comment
        is per-session — it must not change the signature."""
        a = [{"author": "reviewer", "body": "❌ functional: divide-by-zero crash. [reviewer-aaaa]"}]
        b = [{"author": "reviewer", "body": "❌ functional: divide-by-zero crash. [reviewer-bbbb]"}]
        # Both should clamp to the same first-60-chars-after-section payload
        assert supervisor._signature_from_comments(a) == supervisor._signature_from_comments(b)

    def test_order_independent(self):
        """Two reviewers may post the functional and tests findings in
        different order — same set of findings ⇒ same signature."""
        a = [
            {"author": "reviewer", "body": "❌ functional: alpha"},
            {"author": "reviewer", "body": "❌ tests: beta"},
        ]
        b = [
            {"author": "reviewer", "body": "❌ tests: beta"},
            {"author": "reviewer", "body": "❌ functional: alpha"},
        ]
        assert supervisor._signature_from_comments(a) == supervisor._signature_from_comments(b)

    def test_empty_list_returns_none(self):
        assert supervisor._signature_from_comments([]) is None
        assert supervisor._signature_from_comments(None) is None  # type: ignore[arg-type]


class TestSignatureFromReviewNotes:
    def test_identical_notes_match(self):
        a = "Tests fail at line 42 of foo.spec.ts"
        b = "Tests fail at line 42 of foo.spec.ts"
        assert supervisor._signature_from_review_notes(a) == supervisor._signature_from_review_notes(b)

    def test_different_notes_differ(self):
        a = "Tests fail at line 42 of foo.spec.ts"
        b = "Tests fail at line 99 of bar.spec.ts"
        assert supervisor._signature_from_review_notes(a) != supervisor._signature_from_review_notes(b)

    def test_empty_returns_none(self):
        assert supervisor._signature_from_review_notes(None) is None
        assert supervisor._signature_from_review_notes("") is None
        assert supervisor._signature_from_review_notes("   ") is None


# ── Layer 2: detector integration tests via httpx.MockTransport ──────────────


def _build_mock_client(*, feature: dict, comments: list[dict],
                       captured_patches: list[dict] | None = None,
                       captured_routes: list[dict] | None = None,
                       sysconfig: dict | None = None):
    """Returns a function that monkeypatches supervisor.httpx.Client to a
    mocked-transport client. The mocked endpoints:
      - GET  /api/features/<id>           → returns `feature`
      - GET  /api/features/<id>/comments  → returns `comments`
      - GET  /api/system-config           → returns `sysconfig` (defaults
                                            below — repeated_feedback enabled)
      - PATCH /api/features/<id>          → captures into captured_patches
      - POST /api/products/<id>/sprints/blocked/route → captures into captured_routes
      - POST /api/supervisor/actions      → no-op, returns 200
    Anything else returns 404 so unexpected calls fail loudly.
    """
    sysconfig = sysconfig if sysconfig is not None else {
        "supervisor_repeated_feedback_enabled":   True,
        "supervisor_repeated_feedback_threshold": 2,
        "supervisor_dry_run_only":                False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if method == "GET" and path == f"/api/features/{feature['id']}":
            return httpx.Response(200, json=feature)
        if method == "GET" and path == f"/api/features/{feature['id']}/comments":
            return httpx.Response(200, json=comments)
        if method == "GET" and path == "/api/system-config":
            return httpx.Response(200, json=sysconfig)
        if method == "PATCH" and path == f"/api/features/{feature['id']}":
            import json as _json
            body = _json.loads(request.content) if request.content else {}
            if captured_patches is not None:
                captured_patches.append(body)
            return httpx.Response(200, json={**feature, **body})
        if method == "POST" and path.endswith("/sprints/blocked/route"):
            import json as _json
            body = _json.loads(request.content) if request.content else {}
            if captured_routes is not None:
                captured_routes.append({"path": path, "body": body})
            return httpx.Response(200, json={"ok": True})
        if method == "POST" and path == "/api/supervisor/actions":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unmocked: {method} {path}"})

    return httpx.MockTransport(handler)


@pytest.fixture
def patch_client(monkeypatch):
    """Patch supervisor.httpx.Client so any `with httpx.Client(...) as c:`
    inside the supervisor module uses our injected MockTransport."""

    state = {"transport": None}

    def install(transport: httpx.MockTransport):
        state["transport"] = transport

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = state["transport"]
        return real_client(*args, **kwargs)

    monkeypatch.setattr(supervisor.httpx, "Client", fake_client)
    return install


class TestDetectRepeatedReviewFeedback:
    def test_no_prior_signature_stores_baseline(self, patch_client):
        """First changes_requested cycle: no prior signature → store
        baseline, count stays at 0, no block, no route to Blocked sprint."""
        feature = {
            "id": 100, "product_id": 8, "review_notes": None,
            "last_changes_signature": None, "repeated_changes_count": 0,
        }
        comments = [{"author": "reviewer", "body": "❌ tests: e2e timeout"}]
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=comments,
            captured_patches=patches, captured_routes=routes,
        ))

        result = supervisor.detect_repeated_review_feedback(feature_id=100)

        assert result["action"] == "stored"
        assert result["repeated"] == 0
        assert result["signature"] is not None
        # One PATCH that stores the baseline; no route to Blocked sprint.
        assert len(patches) == 1
        assert patches[0]["last_changes_signature"] == result["signature"]
        assert patches[0]["repeated_changes_count"] == 0
        assert "status" not in patches[0]      # no block
        assert "pr_number" not in patches[0]   # no detach
        assert routes == []

    def test_same_signature_increments_below_threshold(self, patch_client):
        """Second matching cycle: signature matches prior → counter goes
        0→1 (below threshold=2) → no block."""
        sig = "deadbeef" * 5
        feature = {
            "id": 100, "product_id": 8, "review_notes": None,
            "last_changes_signature": sig, "repeated_changes_count": 0,
        }
        comments = [{"author": "reviewer", "body": "❌ tests: e2e timeout"}]
        # We need the comments to hash to `sig` for the match — so we
        # compute the real signature first, then plant THAT in the feature.
        real_sig = supervisor._signature_from_comments(comments)
        feature["last_changes_signature"] = real_sig
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=comments,
            captured_patches=patches, captured_routes=routes,
        ))

        result = supervisor.detect_repeated_review_feedback(feature_id=100)

        assert result["action"] == "incremented"
        assert result["repeated"] == 1
        assert len(patches) == 1
        assert patches[0]["repeated_changes_count"] == 1
        assert "status" not in patches[0]
        assert routes == []

    def test_threshold_met_blocks_and_routes(self, patch_client):
        """Same signature reaches threshold (count goes 1→2 with default
        threshold=2): feature is PATCHed to status=Blocked, pr_number
        cleared. Under the phases→features flat model (migration 043)
        there is no Blocked sprint — a single PATCH carries the full
        transition; no sprint-route call is made."""
        comments = [
            {"author": "reviewer", "body": "❌ tests: e2e timeout"},
            {"author": "reviewer", "body": "❌ functional: divide-by-zero crash"},
        ]
        sig = supervisor._signature_from_comments(comments)
        feature = {
            "id": 100, "product_id": 8,
            "review_notes": "tests still failing on the same 240px viewport",
            "last_changes_signature": sig, "repeated_changes_count": 1,
            "pr_number": 6,
        }
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=comments,
            captured_patches=patches, captured_routes=routes,
        ))

        result = supervisor.detect_repeated_review_feedback(
            feature_id=100,
            review_notes=feature["review_notes"],
        )

        assert result["action"] == "blocked"
        assert result["repeated"] == 2
        # The PATCH should mark Blocked and clear pr_number.
        assert len(patches) == 1
        body = patches[0]
        assert body["status"] == "Blocked"
        assert body["pr_number"] is None
        assert "Auto-blocked" in body["blocked_reason"]
        assert body["repeated_changes_count"] == 2
        # Phases→features flat model: no sprint route call.
        assert routes == []

    def test_different_signature_resets_counter(self, patch_client):
        """The coder addressed something — signature differs from prior →
        counter resets to 0 (no block, even if the prior count was high)."""
        comments_now = [{"author": "reviewer", "body": "❌ functional: brand new issue"}]
        feature = {
            "id": 100, "product_id": 8, "review_notes": None,
            "last_changes_signature": "old-sig-from-different-issue",
            "repeated_changes_count": 1,  # was about to block, but issue changed
        }
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=comments_now,
            captured_patches=patches, captured_routes=routes,
        ))

        result = supervisor.detect_repeated_review_feedback(feature_id=100)

        assert result["action"] == "stored"
        assert result["repeated"] == 0
        assert len(patches) == 1
        assert patches[0]["last_changes_signature"] == result["signature"]
        assert patches[0]["repeated_changes_count"] == 0
        # No status mutation, no routing.
        assert "status" not in patches[0]
        assert routes == []

    def test_disabled_in_config_is_noop(self, patch_client):
        """When `supervisor_repeated_feedback_enabled=False`, the detector
        must not contact the PM API at all — return immediately."""
        feature = {"id": 100, "product_id": 8}
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=[],
            captured_patches=patches, captured_routes=routes,
            sysconfig={
                "supervisor_repeated_feedback_enabled":   False,
                "supervisor_repeated_feedback_threshold": 2,
                "supervisor_dry_run_only":                False,
            },
        ))

        result = supervisor.detect_repeated_review_feedback(feature_id=100)
        assert result["action"] == "no-op"
        assert result["repeated"] == 0
        assert patches == []
        assert routes == []

    def test_dry_run_audits_but_does_not_patch_repeated(self, patch_client):
        """Dry-run path: detector still computes the signature and emits
        an audit row, but never PATCHes the feature or routes the sprint."""
        comments = [{"author": "reviewer", "body": "❌ tests: timeout"}]
        sig = supervisor._signature_from_comments(comments)
        feature = {
            "id": 100, "product_id": 8, "review_notes": None,
            "last_changes_signature": sig, "repeated_changes_count": 1,
        }
        patches, routes = [], []
        patch_client(_build_mock_client(
            feature=feature, comments=comments,
            captured_patches=patches, captured_routes=routes,
            sysconfig={
                "supervisor_repeated_feedback_enabled":   True,
                "supervisor_repeated_feedback_threshold": 2,
                "supervisor_dry_run_only":                True,
            },
        ))

        result = supervisor.detect_repeated_review_feedback(feature_id=100)
        assert result["action"] == "blocked"        # would-have-blocked
        assert "dry-run" in result["reason"].lower()
        assert patches == []   # but no PATCH actually fired
        assert routes == []    # no Blocked-sprint route either


# ── Layer 3: in-line normalizer (state_machine._apply_session_entry) ─────────


class TestStateMachineHybridNormalizer:
    """The in-line guard that catches Reviewer prompt violations BEFORE
    they land in the DB. Key behavior: `Reviewed + changes_requested` and
    `Reviewing + changes_requested` get rewritten to
    `Implementing + changes_requested`. Anything else passes through."""

    def _apply(self, captured: list[dict], entry: dict) -> bool:
        from orchestrator.session import state_machine

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            path = request.url.path
            method = request.method
            if method == "GET" and path.startswith("/api/features/"):
                # Pretend the current DB state is "Reviewing" so the
                # rank-guard accepts the (Reviewing, Implementing) backward.
                return httpx.Response(200, json={"status": "Reviewing"})
            if method == "PATCH" and path.startswith("/api/features/"):
                captured.append(_json.loads(request.content) if request.content else {})
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with httpx.Client(base_url="http://pm-api:8080",
                          transport=httpx.MockTransport(handler)) as client:
            return state_machine._apply_session_entry(client, entry)

    def test_reviewed_plus_changes_requested_rewritten(self):
        captured: list[dict] = []
        ok = self._apply(captured, {
            "id": 374,
            "status": "Reviewed",
            "review_outcome": "changes_requested",
        })
        assert ok is True
        assert len(captured) == 1
        # The PATCH that actually goes to the API has status=Implementing,
        # not Reviewed — that's the normalization.
        assert captured[0]["status"] == "Implementing"
        assert captured[0]["review_outcome"] == "changes_requested"

    def test_reviewing_plus_changes_requested_rewritten(self):
        captured: list[dict] = []
        ok = self._apply(captured, {
            "id": 377,
            "status": "Reviewing",
            "review_outcome": "changes_requested",
        })
        assert ok is True
        assert captured[0]["status"] == "Implementing"
        assert captured[0]["review_outcome"] == "changes_requested"

    def test_legitimate_reviewed_plus_approved_passes_through(self):
        """The valid combo `Reviewed + approved` must NOT be touched —
        it's how the reviewer signals approval."""
        captured: list[dict] = []
        # For this case we need the DB to claim status=Implemented or
        # something below Reviewed so the rank-guard accepts the forward write.
        from orchestrator.session import state_machine

        def handler(request: httpx.Request) -> httpx.Response:
            import json as _json
            path = request.url.path
            method = request.method
            if method == "GET" and path.startswith("/api/features/"):
                return httpx.Response(200, json={"status": "Reviewing"})
            if method == "PATCH" and path.startswith("/api/features/"):
                captured.append(_json.loads(request.content) if request.content else {})
                return httpx.Response(200, json={})
            return httpx.Response(404)

        with httpx.Client(base_url="http://pm-api:8080",
                          transport=httpx.MockTransport(handler)) as client:
            ok = state_machine._apply_session_entry(client, {
                "id": 100,
                "status": "Reviewed",
                "review_outcome": "approved",
                "pr_number": 5,
            })
        assert ok is True
        assert captured[0]["status"] == "Reviewed"  # untouched
        assert captured[0]["review_outcome"] == "approved"

    def test_implementing_plus_changes_requested_passes_through(self):
        """The legitimate rework write — must NOT be touched."""
        captured: list[dict] = []
        ok = self._apply(captured, {
            "id": 200,
            "status": "Implementing",
            "review_outcome": "changes_requested",
        })
        assert ok is True
        assert captured[0]["status"] == "Implementing"

