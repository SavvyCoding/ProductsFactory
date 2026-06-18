"""
Wave-10 robust sizing (2026-06-13): the depends_on dispatch-gate keystone
and the over-coupling design-doc advisory.
"""

import os

os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.cycle.dependencies import (  # noqa: E402
    dependency_blocked_feature_ids,
    dangling_dependency_repairs,
)


# ── keystone: depends_on dispatch gate ───────────────────────────────────────


class TestDependencyGate:
    def test_no_depends_on_blocks_nothing(self):
        feats = [{"id": 1, "status": "Designed"},
                 {"id": 2, "status": "Approved"}]
        assert dependency_blocked_feature_ids(feats) == set()

    def test_blocks_when_predecessor_not_pushed(self):
        feats = [
            {"id": 1, "status": "Implementing"},          # predecessor in flight
            {"id": 2, "status": "Designed", "depends_on": 1},
        ]
        assert dependency_blocked_feature_ids(feats) == {2}

    def test_unblocks_when_predecessor_pushed(self):
        feats = [
            {"id": 1, "status": "Pushed"},
            {"id": 2, "status": "Designed", "depends_on": 1},
        ]
        assert dependency_blocked_feature_ids(feats) == set()

    def test_chain_blocks_transitively_until_each_ships(self):
        # slice1 Pushed, slice2 in-flight, slice3 depends on slice2 → slice3
        # blocked, slice2 unblocked (its dep shipped).
        feats = [
            {"id": 1, "status": "Pushed"},
            {"id": 2, "status": "Implementing", "depends_on": 1},
            {"id": 3, "status": "Designed", "depends_on": 2},
        ]
        assert dependency_blocked_feature_ids(feats) == {3}

    def test_dangling_ref_fails_open(self):
        # depends_on points at a feature not present (deleted; FK SET NULL
        # normally nulls it, but a stale id must not deadlock the dependent).
        feats = [{"id": 2, "status": "Designed", "depends_on": 999}]
        assert dependency_blocked_feature_ids(feats) == set()

    def test_self_reference_ignored(self):
        feats = [{"id": 5, "status": "Designed", "depends_on": 5}]
        assert dependency_blocked_feature_ids(feats) == set()

    def test_depends_on_infra_story_resolves(self):
        # The infra predecessor is in the raw list; until it's Pushed the
        # dependent is held (fixes the previously-decorative infra depends_on).
        feats = [
            {"id": 1, "status": "Approved", "feature_type": "infra"},
            {"id": 2, "status": "Designed", "depends_on": 1},
        ]
        assert dependency_blocked_feature_ids(feats) == {2}
        feats[0]["status"] = "Pushed"
        assert dependency_blocked_feature_ids(feats) == set()

    def test_terminally_failed_dep_keeps_dependent_blocked(self):
        # A Rejected predecessor means the chain is genuinely broken — the
        # dependent stays blocked (surfaces as a stall) rather than shipping
        # onto a foundation that never landed.
        feats = [
            {"id": 1, "status": "Rejected"},
            {"id": 2, "status": "Designed", "depends_on": 1},
        ]
        assert dependency_blocked_feature_ids(feats) == {2}

    def test_none_and_empty_safe(self):
        assert dependency_blocked_feature_ids(None) == set()
        assert dependency_blocked_feature_ids([]) == set()


# ── Layer 1.2: over-coupling design-doc advisory (AC/file counting) ───────────


class TestOversizeAdvisory:
    def _doc(self, n_acs: int, n_files: int) -> str:
        acs = "\n".join(
            f"AC{i}. Some observable behavior.\n     Verify: `x`\n     Expected: `y`."
            for i in range(1, n_acs + 1)
        )
        files = "\n".join(f"- `src/file{i}.py` — purpose" for i in range(n_files))
        return (
            "# Feature Design: X\n\n## Acceptance Criteria (must be ≤4)\n"
            f"{acs}\n\n## Implementation Plan\n\n### Files to create (must be ≤6)\n"
            f"{files}\n\n### Files to modify\n- `src/app.py` — wire it\n"
        )

    def _run_advisory(self, tmp_path, doc_text):
        """Drive _post_doc_oversize_advisory against a staged doc, capturing
        any posted comments."""
        from unittest.mock import MagicMock, patch
        from orchestrator.pipelines import post_doc

        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "story_077.md").write_text(doc_text, encoding="utf-8")

        def _run(cmd, **kw):
            r = MagicMock()
            r.returncode = 0
            r.stdout = "docs/story_077.md\n"
            return r

        posts = []

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def post(self, path, json=None, **kw):
                posts.append((path, json))
                return MagicMock(status_code=201)

        with patch("orchestrator.pipelines.post_doc.httpx.Client", return_value=_C()):
            n = post_doc._post_doc_oversize_advisory(
                str(tmp_path), _run, [{"id": 77}], {"name": "P"})
        return n, posts

    def test_within_caps_no_advisory(self, tmp_path):
        n, posts = self._run_advisory(tmp_path, self._doc(4, 6))
        assert n == 0 and posts == []

    def test_too_many_acs_triggers_advisory(self, tmp_path):
        n, posts = self._run_advisory(tmp_path, self._doc(8, 5))
        assert n == 1
        assert posts[0][0] == "/api/features/77/comments"
        assert posts[0][1]["author"] == "sizing-advisor"
        assert "VERTICAL" in posts[0][1]["body"]

    def test_too_many_files_triggers_advisory(self, tmp_path):
        n, posts = self._run_advisory(tmp_path, self._doc(3, 9))
        assert n == 1
        assert "9 files" in posts[0][1]["body"]

    def test_advisory_never_bounces(self, tmp_path):
        # The advisory returns a count only — it has no path to PATCH a
        # feature status. (Belt-and-suspenders: assert no patch surface.)
        n, posts = self._run_advisory(tmp_path, self._doc(8, 8))
        assert n == 1
        # only a comment POST, no status mutation
        assert all(p[0].endswith("/comments") for p in posts)


# ── dangling-dependency repair sweep ─────────────────────────────────────────


class TestDanglingDependencyRepair:
    def test_no_dead_deps_yields_no_repairs(self):
        feats = [
            {"id": 1, "status": "Pushed", "depends_on": None, "parent_id": None},
            {"id": 2, "status": "Approved", "depends_on": 1, "parent_id": None},
        ]
        assert dangling_dependency_repairs(feats) == []

    def test_rehomes_to_live_replacement_child(self):
        # The IndianFoodTruck #1633 case: dep #1632 was Rejected and re-split
        # into live children #1634/#1635; re-home to the highest-id live child.
        feats = [
            {"id": 1632, "status": "Rejected", "depends_on": None, "parent_id": 1540},
            {"id": 1634, "status": "Blocked",  "depends_on": None, "parent_id": 1632},
            {"id": 1635, "status": "Designed", "depends_on": 1634, "parent_id": 1632},
            {"id": 1633, "status": "Designed", "depends_on": 1632, "parent_id": 1540},
        ]
        repairs = dangling_dependency_repairs(feats)
        assert len(repairs) == 1
        r = repairs[0]
        assert r["feature_id"] == 1633
        assert r["old_dep"] == 1632
        assert r["new_dep"] == 1635          # highest-id live child of the dead #1632

    def test_reverted_target_also_repaired(self):
        feats = [
            {"id": 10, "status": "Reverted", "depends_on": None, "parent_id": None},
            {"id": 11, "status": "Approved", "depends_on": 10, "parent_id": None},
        ]
        repairs = dangling_dependency_repairs(feats)
        assert len(repairs) == 1
        assert repairs[0]["new_dep"] is None  # no live child → flag for PM, don't clear

    def test_skips_dead_children_when_picking_replacement(self):
        # A dead target whose only children are ALSO dead → no auto re-home.
        feats = [
            {"id": 20, "status": "Rejected", "depends_on": None, "parent_id": None},
            {"id": 21, "status": "Rejected", "depends_on": None, "parent_id": 20},
            {"id": 22, "status": "Approved", "depends_on": 20, "parent_id": None},
        ]
        repairs = dangling_dependency_repairs(feats)
        assert repairs[0]["new_dep"] is None

    def test_live_dep_is_not_flagged(self):
        # depends_on a Blocked (not dead) feature → gate holds it, but it is
        # NOT a dangling-repair case (Blocked can still recover to Pushed).
        feats = [
            {"id": 30, "status": "Blocked",  "depends_on": None, "parent_id": None},
            {"id": 31, "status": "Approved", "depends_on": 30, "parent_id": None},
        ]
        assert dangling_dependency_repairs(feats) == []


# ── source-side post_doc re-home step ────────────────────────────────────────


class TestPostDocRehome:
    def _mock_client(self, feats):
        from unittest.mock import MagicMock
        c = MagicMock()
        c.get.return_value = MagicMock(json=lambda: feats)
        c.patch.return_value = MagicMock(status_code=200)
        c.post.return_value = MagicMock(status_code=201)
        return c

    _FEATS = [
        {"id": 1632, "status": "Rejected", "parent_id": 1540, "depends_on": None},
        {"id": 1635, "status": "Designed", "parent_id": 1632, "depends_on": None},
        {"id": 1633, "status": "Designed", "parent_id": 1540, "depends_on": 1632},
    ]

    def test_soak_mode_applies_nothing(self, monkeypatch):
        from orchestrator.pipelines import post_doc
        monkeypatch.delenv("DEPENDENCY_REHOME_ENABLED", raising=False)
        c = self._mock_client(self._FEATS)
        n = post_doc._post_doc_rehome_replaced_dependents({"id": 32}, "designer", "P", c)
        assert n == 0
        c.patch.assert_not_called()      # soak: detect + log, never mutate

    def test_enabled_rehomes_to_live_child(self, monkeypatch):
        from orchestrator.pipelines import post_doc
        monkeypatch.setenv("DEPENDENCY_REHOME_ENABLED", "1")
        c = self._mock_client(self._FEATS)
        n = post_doc._post_doc_rehome_replaced_dependents({"id": 32}, "designer", "P", c)
        assert n == 1
        call = c.patch.call_args
        assert "/api/features/1633" in call.args[0]
        assert call.kwargs["json"]["depends_on"] == 1635   # re-homed to live child
