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
    detect_placeholder_template_content,
    detect_shell_artifact_files,
    post_findings,
    run_all,
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
        client = MagicMock()
        client.post.return_value = SimpleNamespace(status_code=201)
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
        client = MagicMock()
        client.post.return_value = SimpleNamespace(status_code=500)
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
        client = MagicMock()
        # First call raises, second succeeds.
        client.post.side_effect = [
            RuntimeError("network"),
            SimpleNamespace(status_code=201),
        ]
        posted = post_findings(findings, client, product_name="t")
        assert posted == 1
        assert client.post.call_count == 2


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
