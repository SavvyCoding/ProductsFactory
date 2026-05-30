"""
Tests for the Option-1 drift-detector spike (orchestrator/drift_detectors.py).

Each detector is pure (no PM API calls; takes a working_dir + features
list, returns Finding dataclasses). Tests use tmp_path for the working
dir and feature dicts shaped like the PM API's /api/products/{id}/features
response.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

# post_coder reads PM_API_URL at import time — set it before any deep import.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.drift_detectors import (  # noqa: E402
    Finding,
    detect_design_doc_mismatch,
    detect_duplicate_ddl,
    detect_placeholder_template_content,
    detect_shell_artifact_files,
    file_corrective_chores,
    post_findings,
    run_all,
    run_chore_detectors,
)


def _feature(fid: int, **kw) -> dict:
    base = {"id": fid, "name": f"feat-{fid}", "status": "Designed"}
    base.update(kw)
    return base


# ── detect_shell_artifact_files ────────────────────────────────────────────


class TestShellArtifactFiles:
    def test_catches_equals_leader(self, tmp_path):
        (tmp_path / "=3.0,").write_text("")
        out = detect_shell_artifact_files(tmp_path, [_feature(1)])
        assert len(out) == 1
        assert out[0].category == "shell_artifact"
        assert out[0].target_id == "=3.0,"
        assert out[0].feature_id == 1

    def test_catches_other_leaders(self, tmp_path):
        # Only `=` and `&` are valid Windows filename leaders; `<>|` are
        # reserved by the OS and can't be testable on this platform.
        # The detector treats all five identically (single `in _SHELL_ARTIFACT_LEADERS`
        # membership check) so the cross-leader behaviour is verified for the
        # portable chars and trusted-by-code-inspection for the others.
        for name in ("=foo", "&bar"):
            (tmp_path / name).write_text("")
        out = detect_shell_artifact_files(tmp_path, [_feature(1)])
        names = sorted(f.target_id for f in out)
        assert names == ["&bar", "=foo"]

    def test_ignores_normal_files(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask\n")
        (tmp_path / "README.md").write_text("# project\n")
        out = detect_shell_artifact_files(tmp_path, [_feature(1)])
        assert out == []

    def test_no_features_no_findings(self, tmp_path):
        # No anchor feature → can't attribute findings, skip.
        (tmp_path / "=3.0,").write_text("")
        out = detect_shell_artifact_files(tmp_path, [])
        assert out == []

    def test_missing_working_dir(self, tmp_path):
        out = detect_shell_artifact_files(tmp_path / "missing", [_feature(1)])
        assert out == []

    def test_skips_directories(self, tmp_path):
        # A directory named "=weird" is rare but not legitimate to flag —
        # the detector targets files.
        (tmp_path / "=somedir").mkdir()
        out = detect_shell_artifact_files(tmp_path, [_feature(1)])
        assert out == []


# ── detect_design_doc_mismatch ─────────────────────────────────────────────


class TestDesignDocMismatch:
    def test_flags_path_without_file(self, tmp_path):
        feats = [_feature(995, design_doc_path="docs/story_995.md")]
        out = detect_design_doc_mismatch(tmp_path, feats)
        assert len(out) == 1
        assert out[0].category == "design_doc_missing"
        assert out[0].feature_id == 995

    def test_passes_when_file_exists(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "story_993.md").write_text("# design")
        feats = [_feature(993, design_doc_path="docs/story_993.md")]
        out = detect_design_doc_mismatch(tmp_path, feats)
        assert out == []

    def test_ignores_features_without_design_doc_path(self, tmp_path):
        feats = [_feature(1)]   # no design_doc_path
        out = detect_design_doc_mismatch(tmp_path, feats)
        assert out == []

    def test_mixed_results(self, tmp_path):
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "story_1.md").write_text("# ok")
        feats = [
            _feature(1, design_doc_path="docs/story_1.md"),  # exists
            _feature(2, design_doc_path="docs/story_2.md"),  # missing
            _feature(3),                                      # no path
        ]
        out = detect_design_doc_mismatch(tmp_path, feats)
        assert len(out) == 1
        assert out[0].feature_id == 2


# ── detect_placeholder_template_content ────────────────────────────────────


_ARCH_WITH_OLD_PLACEHOLDER = """\
# X — Architecture

## MODULES

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| Calc engine | `src/calculate.py` | calculate() | core |

Example rows (replace as the product grows):
- `User persistence` | `src/users/user_store.py` | ...
- `Authentication` | `src/auth/verify.py` | ...

## RULES
- whatever
"""

_ARCH_WITH_NEW_PLACEHOLDER_AND_REAL_ROWS = """\
# X — Architecture

## MODULES

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| Calc engine | `src/calculate.py` | calculate() | core |
| _(populated by the architect persona as features land — do not edit by hand)_ | | | |

## RULES
- whatever
"""

_ARCH_WITH_NEW_PLACEHOLDER_ONLY = """\
# X — Architecture

## MODULES

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| _(populated by the architect persona as features land — do not edit by hand)_ | | | |

## RULES
- whatever
"""

_ARCH_CLEAN = """\
# X — Architecture

## MODULES

| Concern | Canonical module | Owns | Notes |
|---|---|---|---|
| Calc engine | `src/calculate.py` | calculate() | core |
| History | `src/history.py` | log/get/delete | sqlite |

## RULES
- whatever
"""


class TestPlaceholderTemplateContent:
    def test_flags_old_example_rows_header(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text(_ARCH_WITH_OLD_PLACEHOLDER, encoding="utf-8")
        out = detect_placeholder_template_content(tmp_path, [_feature(1)])
        assert len(out) == 1
        assert out[0].category == "placeholder_template"
        assert out[0].target_id == "ARCHITECTURE.md"

    def test_flags_new_placeholder_when_real_rows_exist(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text(
            _ARCH_WITH_NEW_PLACEHOLDER_AND_REAL_ROWS, encoding="utf-8",
        )
        out = detect_placeholder_template_content(tmp_path, [_feature(1)])
        assert len(out) == 1
        # Should be the low-severity finding about placeholder + real rows
        assert out[0].severity == "low"
        assert "populated row" in out[0].detail.lower() or "row(s) and" in out[0].detail.lower()

    def test_does_not_flag_placeholder_alone(self, tmp_path):
        # Fresh greenfield — only the placeholder, no real rows. That's
        # the intended initial state; no finding.
        (tmp_path / "ARCHITECTURE.md").write_text(_ARCH_WITH_NEW_PLACEHOLDER_ONLY, encoding="utf-8")
        out = detect_placeholder_template_content(tmp_path, [_feature(1)])
        assert out == []

    def test_clean_arch_no_findings(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text(_ARCH_CLEAN, encoding="utf-8")
        out = detect_placeholder_template_content(tmp_path, [_feature(1)])
        assert out == []

    def test_no_arch_file_no_findings(self, tmp_path):
        out = detect_placeholder_template_content(tmp_path, [_feature(1)])
        assert out == []


# ── orchestration ──────────────────────────────────────────────────────────


class TestRunAll:
    def test_concatenates_all_detector_outputs(self, tmp_path):
        # Set up findings from BOTH detector 1 and detector 3.
        (tmp_path / "=4").write_text("")
        (tmp_path / "ARCHITECTURE.md").write_text(_ARCH_WITH_OLD_PLACEHOLDER, encoding="utf-8")
        out = run_all(tmp_path, [_feature(7)])
        cats = sorted(f.category for f in out)
        assert cats == ["placeholder_template", "shell_artifact"]
        assert all(f.feature_id == 7 for f in out)

    def test_one_detector_raising_does_not_kill_others(self, tmp_path, monkeypatch):
        # Make detector 2 explode; detector 1 still produces a finding.
        from orchestrator import drift_detectors as dd
        def _boom(*a, **kw): raise RuntimeError("boom")
        monkeypatch.setattr(dd, "detect_design_doc_mismatch", _boom)
        monkeypatch.setattr(dd, "_DETECTORS", (
            dd.detect_shell_artifact_files,
            _boom,
            dd.detect_placeholder_template_content,
        ))
        (tmp_path / "=4").write_text("")
        out = dd.run_all(tmp_path, [_feature(1)])
        assert any(f.category == "shell_artifact" for f in out)


def _mk_client(get_return=None, post_return=None, post_side_effect=None):
    """Build a MagicMock httpx-like client where GET returns recent
    comments (empty list by default → no dedupe matches) and POST returns
    a 2xx by default."""
    client = MagicMock()
    if get_return is None:
        client.get.return_value = SimpleNamespace(
            status_code=200, json=lambda: [],
        )
    else:
        client.get.return_value = get_return
    if post_side_effect is not None:
        client.post.side_effect = post_side_effect
    else:
        client.post.return_value = (
            post_return if post_return is not None
            else SimpleNamespace(status_code=201)
        )
    return client


class TestPostFindings:
    def test_posts_each_finding_as_comment(self):
        findings = [
            Finding(category="shell_artifact", severity="medium",
                    target_type="file", target_id="=3.0,",
                    feature_id=42, detail="d1", fix_hint="h1"),
            Finding(category="design_doc_missing", severity="high",
                    target_type="feature", target_id="43",
                    feature_id=43, detail="d2", fix_hint="h2"),
        ]
        client = _mk_client()
        posted = post_findings(findings, client, product_name="t")
        assert posted == 2
        assert client.post.call_count == 2
        # Each call POSTed to /api/features/{fid}/comments
        called_paths = [c.args[0] for c in client.post.call_args_list]
        assert called_paths == [
            "/api/features/42/comments",
            "/api/features/43/comments",
        ]
        # Author tag is "drift-scanner"
        for c in client.post.call_args_list:
            assert c.kwargs["json"]["author"] == "drift-scanner"

    def test_non_2xx_response_is_logged_not_raised(self):
        findings = [
            Finding(category="x", severity="low", target_type="file",
                    target_id="t", feature_id=1, detail="d", fix_hint="h"),
        ]
        client = _mk_client(post_return=SimpleNamespace(status_code=500))
        # Should not raise, should not count as posted.
        posted = post_findings(findings, client, product_name="t")
        assert posted == 0

    def test_exception_in_post_does_not_kill_loop(self):
        findings = [
            Finding(category="x", severity="low", target_type="file",
                    target_id="a", feature_id=1, detail="d", fix_hint="h"),
            Finding(category="x", severity="low", target_type="file",
                    target_id="b", feature_id=2, detail="d", fix_hint="h"),
        ]
        client = _mk_client(post_side_effect=[
            RuntimeError("network"),
            SimpleNamespace(status_code=201),
        ])
        posted = post_findings(findings, client, product_name="t")
        assert posted == 1
        assert client.post.call_count == 2


class TestDedupe:
    """Re-running the same detectors must not double-post comments."""

    def test_skips_finding_whose_body_already_posted(self):
        f = Finding(category="shell_artifact", severity="medium",
                    target_type="file", target_id="=3.0,",
                    feature_id=42, detail="d", fix_hint="h")
        # The GET on /api/features/42/comments returns one drift-scanner
        # comment whose body exactly matches what we'd post.
        client = _mk_client(get_return=SimpleNamespace(
            status_code=200,
            json=lambda: [{"author": "drift-scanner", "body": f.as_comment_body()}],
        ))
        posted = post_findings([f], client, product_name="t")
        assert posted == 0
        assert client.post.call_count == 0  # dedupe hit, no POST

    def test_does_not_dedupe_other_authors(self):
        f = Finding(category="x", severity="low", target_type="file",
                    target_id="t", feature_id=1, detail="d", fix_hint="h")
        # The feature has a comment with the same body but a different
        # author — drift-scanner should still post.
        client = _mk_client(get_return=SimpleNamespace(
            status_code=200,
            json=lambda: [{"author": "reviewer", "body": f.as_comment_body()}],
        ))
        posted = post_findings([f], client, product_name="t")
        assert posted == 1

    def test_dedupes_within_same_cycle(self):
        # Two findings with identical bodies in the same call — second
        # should be deduped from the in-cycle cache.
        f1 = Finding(category="x", severity="low", target_type="file",
                     target_id="t", feature_id=1, detail="d", fix_hint="h")
        f2 = Finding(category="x", severity="low", target_type="file",
                     target_id="t", feature_id=1, detail="d", fix_hint="h")
        client = _mk_client()
        posted = post_findings([f1, f2], client, product_name="t")
        assert posted == 1   # second skipped
        assert client.post.call_count == 1

    def test_caches_get_per_feature(self):
        # Two findings on the same feature_id should only GET once.
        f1 = Finding(category="a", severity="low", target_type="file",
                     target_id="x", feature_id=5, detail="d", fix_hint="h")
        f2 = Finding(category="b", severity="low", target_type="file",
                     target_id="y", feature_id=5, detail="d", fix_hint="h")
        client = _mk_client()
        post_findings([f1, f2], client, product_name="t")
        # exactly one GET, two POSTs
        assert client.get.call_count == 1
        assert client.post.call_count == 2

    def test_get_failure_does_not_block_post(self):
        # If the GET for existing comments fails (network / 5xx), we
        # default to "no existing comments" and post anyway.
        f = Finding(category="x", severity="low", target_type="file",
                    target_id="t", feature_id=1, detail="d", fix_hint="h")
        client = _mk_client(get_return=SimpleNamespace(
            status_code=500, json=lambda: None,
        ))
        posted = post_findings([f], client, product_name="t")
        assert posted == 1


class TestFindingRender:
    def test_as_comment_body_includes_all_fields(self):
        f = Finding(
            category="shell_artifact", severity="medium",
            target_type="file", target_id="=3.0,",
            feature_id=99, detail="junk file", fix_hint="delete it",
        )
        body = f.as_comment_body()
        assert "drift-scanner" in body
        assert "shell_artifact" in body
        assert "medium" in body
        assert "=3.0," in body
        assert "junk file" in body
        assert "delete it" in body


# ── Finding worklist fields ─────────────────────────────────────────────────


class TestFindingWorklistFields:
    def test_dedupe_key_defaults_to_category_target(self):
        f = Finding(
            category="duplicate_ddl", severity="high", target_type="code",
            target_id="calculations", feature_id=0, detail="d", fix_hint="h",
        )
        assert f.dedupe_key == "duplicate_ddl:calculations"
        assert f.occurrences == []
        assert f.product_id is None

    def test_explicit_dedupe_key_preserved(self):
        f = Finding(
            category="x", severity="low", target_type="doc", target_id="t",
            feature_id=1, detail="d", fix_hint="h", dedupe_key="custom:key",
        )
        assert f.dedupe_key == "custom:key"

    def test_existing_detectors_construct_unchanged(self, tmp_path):
        # Back-compat: the original comment-mode detectors don't pass the
        # new fields and must still work.
        (tmp_path / "=3.0,").write_text("")
        out = detect_shell_artifact_files(tmp_path, [_feature(1, product_id=24)])
        assert out and out[0].occurrences == [] and out[0].dedupe_key


# ── detect_duplicate_ddl ────────────────────────────────────────────────────


class TestDetectDuplicateDDL:
    def _write(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_flags_multiple_sites(self, tmp_path):
        ddl = 'conn.execute("CREATE TABLE IF NOT EXISTS calculations (id INTEGER)")\n'
        self._write(tmp_path / "src" / "history.py", ddl * 2)   # 2 in one file
        self._write(tmp_path / "src" / "stats.py", ddl)         # 1 in another
        out = detect_duplicate_ddl(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        f = out[0]
        assert f.category == "duplicate_ddl"
        assert f.severity == "high"
        assert f.target_id == "calculations"
        assert len(f.occurrences) == 3
        assert f.product_id == 24
        assert f.dedupe_key == "duplicate_ddl:calculations"

    def test_single_site_clean(self, tmp_path):
        self._write(
            tmp_path / "src" / "history.py",
            'conn.execute("CREATE TABLE calculations (id INTEGER)")\n',
        )
        assert detect_duplicate_ddl(tmp_path, [_feature(1, product_id=24)]) == []

    def test_migrations_excluded(self, tmp_path):
        ddl = 'op.execute("CREATE TABLE calculations (id INTEGER)")\n'
        # Two CREATE TABLEs but both under migrations/ → legitimate, not flagged.
        self._write(tmp_path / "migrations" / "0001.py", ddl)
        self._write(tmp_path / "migrations" / "0002.py", ddl)
        assert detect_duplicate_ddl(tmp_path, [_feature(1, product_id=24)]) == []

    def test_comments_ignored(self, tmp_path):
        body = (
            'conn.execute("CREATE TABLE calculations (id INTEGER)")\n'
            "# CREATE TABLE calculations -- this is just a comment\n"
        )
        # Only one real occurrence (the comment is skipped) → not flagged.
        self._write(tmp_path / "src" / "history.py", body)
        assert detect_duplicate_ddl(tmp_path, [_feature(1, product_id=24)]) == []

    def test_run_chore_detectors_includes_it(self, tmp_path):
        ddl = 'conn.execute("CREATE TABLE t (id INTEGER)")\n'
        self._write(tmp_path / "src" / "a.py", ddl)
        self._write(tmp_path / "src" / "b.py", ddl)
        out = run_chore_detectors(tmp_path, [_feature(1, product_id=24)])
        assert any(f.category == "duplicate_ddl" for f in out)


# ── file_corrective_chores ──────────────────────────────────────────────────


def _high(table="calculations", n=3, pid=24, key=None):
    return Finding(
        category="duplicate_ddl", severity="high", target_type="code",
        target_id=table, feature_id=0, detail="dup", fix_hint="consolidate",
        occurrences=[f"src/f{i}.py:{i}" for i in range(n)], product_id=pid,
        dedupe_key=key or "",
    )


def _client(get_features=None, post_status=201):
    """MagicMock pm_client. get → product features list; post → status."""
    c = MagicMock()
    c.get.return_value = SimpleNamespace(
        status_code=200, json=lambda: (get_features or []),
    )
    c.post.return_value = SimpleNamespace(status_code=post_status)
    return c


def _posts(client):
    return [
        call.kwargs["json"]
        for call in client.post.call_args_list
        if call.args and call.args[0] == "/api/features"
    ]


class TestFileCorrectiveChores:
    def test_files_high_finding_with_correct_fields(self):
        client = _client(get_features=[])
        filed = file_corrective_chores([_high()], client, product_id=24)
        assert filed == 1
        body = _posts(client)[0]
        assert body["product_id"] == 24
        assert body["feature_type"] == "chore"
        assert body["status"] == "Approved"
        assert body["source"] == "ai"
        # priority MUST be 99 — the orchestrator's session-launcher path
        # (_fetch_assigned_features) sorts by -priority DESC, so the
        # HIGHEST number is picked first. Standard features land at
        # priority 50 (recommender) or 70 (planner); 99 beats both and
        # leaves 100 as headroom for explicit "PM urgent." Pre-2026-05-30
        # this was priority=1 (mistaken assumption of ASC ordering) and
        # chores sorted to the BACK of the designer queue.
        assert body["priority"] == 99
        assert "<!-- reconciler-key: duplicate_ddl:calculations -->" in body["description"]
        assert "src/f0.py:0" in body["description"]

    def test_skips_low_and_medium(self):
        client = _client(get_features=[])
        low = Finding("c", "low", "doc", "t", 1, "d", "h")
        med = Finding("c", "medium", "doc", "t", 1, "d", "h")
        assert file_corrective_chores([low, med], client, product_id=24) == 0
        assert _posts(client) == []

    def test_dedupes_against_open_chore(self):
        existing = [{
            "feature_type": "chore",
            "status": "Implementing",
            "description": "...\n<!-- reconciler-key: duplicate_ddl:calculations -->",
        }]
        client = _client(get_features=existing)
        assert file_corrective_chores([_high()], client, product_id=24) == 0
        assert _posts(client) == []

    def test_closed_chore_does_not_dedupe(self):
        # A Pushed chore for the same key means the drift was fixed and has
        # regressed — re-file it.
        existing = [{
            "feature_type": "chore",
            "status": "Pushed",
            "description": "<!-- reconciler-key: duplicate_ddl:calculations -->",
        }]
        client = _client(get_features=existing)
        assert file_corrective_chores([_high()], client, product_id=24) == 1

    def test_respects_cap(self):
        client = _client(get_features=[])
        findings = [_high(table=f"t{i}", n=i + 2) for i in range(5)]
        assert file_corrective_chores(findings, client, product_id=24, max_chores=2) == 2
        assert len(_posts(client)) == 2

    def test_no_product_id_files_nothing(self):
        client = _client(get_features=[])
        assert file_corrective_chores([_high(pid=None)], client, product_id=None) == 0

    def test_post_failure_does_not_raise(self):
        client = _client(get_features=[], post_status=500)
        assert file_corrective_chores([_high()], client, product_id=24) == 0

    def test_many_sites_body_has_no_ac_bullets(self):
        # Regression (calc3 2026-05-29): the /api/features story-sizing guard
        # counts lines starting with `- ` or `* ` as acceptance criteria and
        # 422s at >4. A dedup chore lists every site, so >4-site dups (the
        # worst ones) were rejected. Locations must be a code fence, not bullets.
        import re as _re
        client = _client(get_features=[])
        file_corrective_chores([_high(table="calculations", n=11)], client, product_id=24)
        body = _posts(client)[0]["description"]
        ac_bullets = [ln for ln in body.splitlines() if _re.match(r"^\s*[-*] ", ln)]
        assert ac_bullets == [], f"chore body must carry no AC bullets, found: {ac_bullets}"
        assert "src/f0.py:0" in body, "locations must still be present (in a code fence)"


# ── detect_god_file ─────────────────────────────────────────────────────────

from orchestrator.drift_detectors import (  # noqa: E402
    detect_god_file,
    detect_public_route_blanket_with_auth,
    detect_mixed_error_envelopes,
)


def _write(p, body):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


class TestDetectGodFile:
    def _routes(self, n):
        # Generate n route handlers in a single file.
        return "\n".join(
            f"@app.get('/r{i}')\ndef h{i}(): return 'ok'\n" for i in range(n)
        )

    def test_flags_file_above_threshold(self, tmp_path):
        _write(tmp_path / "src" / "main.py", self._routes(9))   # > 8
        out = detect_god_file(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        assert out[0].category == "god_file"
        assert out[0].severity == "high"
        assert out[0].target_id == "src/main.py"
        assert "9" in out[0].occurrences[0]

    def test_under_threshold_no_finding(self, tmp_path):
        _write(tmp_path / "src" / "main.py", self._routes(8))   # at threshold
        assert detect_god_file(tmp_path, [_feature(1, product_id=24)]) == []

    def test_recognises_flask_and_fastapi(self, tmp_path):
        # mix of `app.route`, `app.post`, `router.get` — all count.
        body = (
            "@app.route('/a')\ndef a(): pass\n"
            "@app.post('/b')\ndef b(): pass\n"
            "@app.delete('/c')\ndef c(): pass\n"
            "@router.get('/d')\ndef d(): pass\n"
            "@router.put('/e')\ndef e(): pass\n"
            "@router.patch('/f')\ndef f(): pass\n"
            "@app.get('/g')\ndef g(): pass\n"
            "@app.post('/h')\ndef h(): pass\n"
            "@app.delete('/i')\ndef i(): pass\n"
        )
        _write(tmp_path / "src" / "api.py", body)
        out = detect_god_file(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        assert "src/api.py" in out[0].target_id

    def test_tests_dir_excluded(self, tmp_path):
        _write(tmp_path / "tests" / "test_x.py", self._routes(15))
        assert detect_god_file(tmp_path, [_feature(1, product_id=24)]) == []


# ── detect_public_route_blanket_with_auth ───────────────────────────────────


class TestDetectPublicRouteBlanketWithAuth:
    def test_flags_blanket_with_authed_route(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "# PUBLIC_ROUTE: arithmetic endpoints have no user state\n"
            "from src.auth import verify_auth\n"
            "@app.delete('/api/x')\ndef x():\n    user = verify_auth(request)\n"
        ))
        out = detect_public_route_blanket_with_auth(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        assert out[0].category == "public_route_blanket_with_auth"
        assert out[0].severity == "high"
        assert out[0].target_id == "src/main.py"

    def test_blanket_alone_no_auth_no_finding(self, tmp_path):
        # Genuinely public file — annotation correct, no contradiction.
        _write(tmp_path / "src" / "public.py", (
            "# PUBLIC_ROUTE: weather widget\n"
            "@app.get('/weather')\ndef w(): pass\n"
        ))
        assert detect_public_route_blanket_with_auth(tmp_path, [_feature(1, product_id=24)]) == []

    def test_auth_alone_no_blanket_no_finding(self, tmp_path):
        # Per-route auth without a file-level blanket — correct pattern.
        _write(tmp_path / "src" / "main.py", (
            "from src.auth import verify_auth\n"
            "@app.delete('/api/x')\ndef x():\n    verify_auth(request)\n"
        ))
        assert detect_public_route_blanket_with_auth(tmp_path, [_feature(1, product_id=24)]) == []

    def test_blanket_only_in_first_nonempty_line(self, tmp_path):
        # A PUBLIC_ROUTE token buried mid-file is NOT a blanket; don't flag.
        _write(tmp_path / "src" / "main.py", (
            '"""docstring"""\n'
            "import x\n"
            "# PUBLIC_ROUTE: this is a comment somewhere, not a blanket\n"
            "from src.auth import verify_auth\n"
            "@app.delete('/api/x')\ndef x(): verify_auth(request)\n"
        ))
        assert detect_public_route_blanket_with_auth(tmp_path, [_feature(1, product_id=24)]) == []


# ── detect_mixed_error_envelopes ────────────────────────────────────────────


class TestDetectMixedErrorEnvelopes:
    def test_flags_mixed_shapes(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "def a():\n"
            "    return jsonify({'error': {'code': 'INVALID', 'message': 'bad'}}), 400\n"
            "def b():\n"
            "    return jsonify({'error': str(e)}), 401\n"
        ))
        out = detect_mixed_error_envelopes(tmp_path, [_feature(1, product_id=24)])
        assert len(out) == 1
        assert out[0].category == "mixed_error_envelopes"
        assert out[0].severity == "high"
        # detail contains both counts
        assert "structured=" in out[0].occurrences[0]
        assert "raw=" in out[0].occurrences[0]

    def test_only_structured_no_finding(self, tmp_path):
        _write(tmp_path / "src" / "main.py", (
            "return jsonify({'error': {'code': 'X', 'message': 'y'}}), 400\n"
        ))
        assert detect_mixed_error_envelopes(tmp_path, [_feature(1, product_id=24)]) == []

    def test_only_raw_no_finding(self, tmp_path):
        # Raw only (still bad but a different finding class — let
        # `detect_mixed_error_envelopes` stay narrow on MIXED state).
        _write(tmp_path / "src" / "main.py", "return jsonify({'error': str(e)}), 500\n")
        assert detect_mixed_error_envelopes(tmp_path, [_feature(1, product_id=24)]) == []

    def test_no_errors_no_finding(self, tmp_path):
        _write(tmp_path / "src" / "calculate.py", "def calc(a, b): return a + b\n")
        assert detect_mixed_error_envelopes(tmp_path, [_feature(1, product_id=24)]) == []


# ── detect_architect_review_pending ────────────────────────────────────────

from orchestrator.drift_detectors import (  # noqa: E402
    detect_architect_review_pending,
)


def _arch_review(title_n: int, title: str, section: str = "RULES",
                 doc_says: str = "old contract", code_does: str = "new contract",
                 proposed: str = "rewrite to match code") -> str:
    """One finding section in the architect's mandated `### N.` shape."""
    return (
        f"### {title_n}. {title}\n"
        f"- **Section:** {section}\n"
        f"- **Doc says:** {doc_says}\n"
        f"- **Code does:** {code_does}\n"
        f"- **Proposed change:** {proposed}\n"
    )


class TestDetectArchitectReviewPending:
    def test_single_finding_parsed(self, tmp_path):
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-30.md",
            "# Architecture review — 2026-05-30\n\n## Findings\n\n"
            + _arch_review(1, "RULES — auth rule stale",
                           doc_says="verify_auth(request)",
                           code_does="Depends(get_current_user)",
                           proposed="Rewrite to FastAPI pattern"),
        )
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert len(out) == 1
        f = out[0]
        assert f.category == "architect_review_pending"
        assert f.severity == "high"
        assert f.target_type == "doc"
        assert f.feature_id == 99
        assert f.product_id == 25
        assert "RULES — auth rule stale" in f.detail
        assert "verify_auth(request)" in f.detail
        assert "Depends(get_current_user)" in f.detail
        assert f.fix_hint == "Rewrite to FastAPI pattern"
        # Detail must NOT contain leading `- ` bullets (would trip the
        # website story-sizing guard's >4-bullet rejection).
        for line in f.detail.split("\n"):
            assert not line.lstrip().startswith("- "), \
                f"detail line {line!r} has a bullet — would 422 the chore"

    def test_multiple_findings_each_get_own(self, tmp_path):
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-30.md",
            "# Architecture review\n\n## Findings\n\n"
            + _arch_review(1, "RULES — auth stale")
            + "\n"
            + _arch_review(2, "REFERENCE PATTERNS — all Flask",
                           section="REFERENCE PATTERNS",
                           doc_says="Flask idioms",
                           code_does="FastAPI",
                           proposed="Rewrite patterns")
            + "\n"
            + _arch_review(3, "CONFIG GATES — cov-fail-under was lowered",
                           section="CONFIG GATES",
                           doc_says="cov-fail-under=70",
                           code_does="cov-fail-under=0",
                           proposed="Restore to 70")
            + "\n## Recommendation\n\nDo all the above.\n",
        )
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert len(out) == 3
        titles = [f.target_id for f in out]
        assert "architecture_review_2026-05-30.md#1" in titles
        assert "architecture_review_2026-05-30.md#2" in titles
        assert "architecture_review_2026-05-30.md#3" in titles
        # Recommendation section must NOT have been absorbed into #3.
        f3 = next(f for f in out if f.target_id.endswith("#3"))
        assert "Do all the above" not in f3.detail
        assert "Restore to 70" == f3.fix_hint

    def test_resolved_file_skipped(self, tmp_path):
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-29.md",
            "# RESOLVED 2026-05-30: RULES auth stale — addressed in commit abc123.\n",
        )
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert out == []

    def test_resolved_marker_case_insensitive_and_after_blank(self, tmp_path):
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-29.md",
            "\n\n# Resolved 2026-05-30: contract drift addressed.\n",
        )
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert out == []

    def test_no_docs_dir_no_findings(self, tmp_path):
        # No `docs/` subdir at all — clean greenfield.
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert out == []

    def test_no_review_doc_no_findings(self, tmp_path):
        # `docs/` exists but only has story docs.
        _write(tmp_path / "docs" / "story_1.md", "# Story 1\n")
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert out == []

    def test_dedupe_key_is_title_only(self, tmp_path):
        """Filename + section number are NOT part of the dedupe key, so the
        same finding moved to a new dated review doc or renumbered keeps
        the same key and won't re-file. Material title edit IS a new key."""
        body_a = (
            "# Review\n\n## Findings\n\n"
            + _arch_review(3, "RULES — auth rule stale")
        )
        body_b = (
            "# Review\n\n## Findings\n\n"
            + _arch_review(1, "RULES — auth rule stale")  # renumbered
        )
        body_c = (
            "# Review\n\n## Findings\n\n"
            + _arch_review(1, "RULES — auth rule STILL stale")  # retitled
        )
        for name, body in [
            ("architecture_review_a.md", body_a),
            ("architecture_review_b.md", body_b),
            ("architecture_review_c.md", body_c),
        ]:
            (tmp_path / "docs").mkdir(exist_ok=True)
            (tmp_path / "docs" / name).write_text(body, encoding="utf-8")
            out = detect_architect_review_pending(
                tmp_path, [_feature(99, product_id=25)],
            )
            (tmp_path / "docs" / name).unlink()
            if name in ("architecture_review_a.md", "architecture_review_b.md"):
                assert out and out[0].dedupe_key == (
                    "architect_review:rules_auth_rule_stale"
                )
            else:
                # Retitled → different key.
                assert out and out[0].dedupe_key != (
                    "architect_review:rules_auth_rule_stale"
                )

    def test_finding_without_proposed_change_uses_body_excerpt(self, tmp_path):
        # If the architect deviates from the bullet schema (no "Proposed
        # change:" line), the detector falls back to the section body as
        # the fix_hint rather than skipping the finding outright.
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-30.md",
            "# Review\n\n## Findings\n\n"
            "### 1. Something is off in main.py\n"
            "It looks weird and we should probably fix it.\n",
        )
        out = detect_architect_review_pending(
            tmp_path, [_feature(99, product_id=25)],
        )
        assert len(out) == 1
        assert "It looks weird" in out[0].fix_hint

    def test_anchor_feature_id_zero_when_no_features(self, tmp_path):
        # No anchor feature: still emit findings (chore sink files
        # against the product, not a specific feature), with feature_id=0.
        _write(
            tmp_path / "docs" / "architecture_review_2026-05-30.md",
            "# Review\n\n" + _arch_review(1, "X"),
        )
        out = detect_architect_review_pending(tmp_path, [])
        assert len(out) == 1
        assert out[0].feature_id == 0
        assert out[0].product_id is None
