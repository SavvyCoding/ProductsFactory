"""
Tests for the architect phantom-DEPRECATED prune (Fix A, 2026-05-29).

The architect persona carries DEPRECATED entries forward forever with no
existence check — calc3's `SRC/` entry named a directory that never existed
(a case-fold artifact of the Windows bind mount) and the architect even
"updated" its file count to track src/. _prune_phantom_deprecated removes
DEPRECATED entries whose concrete path doesn't exist, using an exact-case
check (os.path.exists is case-insensitive on the bind mount — the very bug).
"""
import os

# post_maintenance reads PM_API_URL at import time.
os.environ.setdefault("PM_API_URL", "http://pm-api:8080")

from orchestrator.pipelines.post_maintenance import (  # noqa: E402
    _path_exists_exact_case,
    _prune_phantom_deprecated,
)


def _arch(deprecated_bullets: str) -> str:
    return (
        "# X — Architecture\n\n"
        "## MODULES\n\n| Concern | Canonical module | Owns | Notes |\n"
        "|---|---|---|---|\n| Calc | `src/calculate.py` | calc() | core |\n\n"
        "## DEPRECATED\n\n"
        f"{deprecated_bullets}\n\n"
        "## Naming conventions\n\n- snake_case\n"
    )


def _deprecated_lines(text: str) -> list[str]:
    out, in_sec = [], False
    for line in text.splitlines():
        if line.startswith("## DEPRECATED"):
            in_sec = True
            continue
        if in_sec and line.startswith("## "):
            break
        if in_sec and line.strip().startswith("- "):
            out.append(line.strip())
    return out


class TestPathExistsExactCase:
    def test_exact_match(self, tmp_path):
        (tmp_path / "src").mkdir()
        assert _path_exists_exact_case(str(tmp_path), "src") is True

    def test_wrong_case_is_false(self, tmp_path):
        # Only lowercase src/ exists; SRC must read as absent even on a
        # case-insensitive filesystem (listdir returns the real name "src").
        (tmp_path / "src").mkdir()
        assert _path_exists_exact_case(str(tmp_path), "SRC") is False

    def test_missing_is_false(self, tmp_path):
        assert _path_exists_exact_case(str(tmp_path), "nope") is False

    def test_nested_path(self, tmp_path):
        (tmp_path / "src" / "auth").mkdir(parents=True)
        (tmp_path / "src" / "auth" / "verify.py").write_text("x")
        assert _path_exists_exact_case(str(tmp_path), "src/auth/verify.py") is True
        assert _path_exists_exact_case(str(tmp_path), "src/Auth/verify.py") is False


class TestPrunePhantomDeprecated:
    def test_removes_phantom_keeps_real(self, tmp_path):
        # src/ exists (lowercase); SRC/ does not. A real deprecated file exists.
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "old_module.py").write_text("# legacy")
        (tmp_path / "ARCHITECTURE.md").write_text(_arch(
            "- `SRC/` — complete duplicate of `src/` (8 files). Delete.\n"
            "- `src/old_module.py` — superseded by src/calculate.py; remove.\n"
            "- `*.bak` — agent-debris backups; never commit.\n"
            "- `_(populated by the architect persona as features land)_`\n"
        ), encoding="utf-8")

        removed = _prune_phantom_deprecated(str(tmp_path))
        assert removed == 1

        lines = _deprecated_lines((tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8"))
        joined = "\n".join(lines)
        assert "`SRC/`" not in joined, "phantom SRC/ must be pruned"
        assert "`src/old_module.py`" in joined, "real deprecated file must be kept"
        assert "`*.bak`" in joined, "glob pattern must be left untouched"
        assert "_(populated" in joined, "renderer placeholder must be left untouched"

    def test_no_phantom_no_change(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "old.py").write_text("x")
        (tmp_path / "ARCHITECTURE.md").write_text(_arch(
            "- `src/old.py` — remove after migration.\n"
        ), encoding="utf-8")
        before = (tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8")

        removed = _prune_phantom_deprecated(str(tmp_path))
        assert removed == 0
        assert (tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8") == before

    def test_header_preserved(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "ARCHITECTURE.md").write_text(_arch(
            "- `SRC/` — phantom.\n"
        ), encoding="utf-8")
        _prune_phantom_deprecated(str(tmp_path))
        text = (tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8")
        # All section headers survive (section-integrity check depends on this).
        for h in ("## MODULES", "## DEPRECATED", "## Naming conventions"):
            assert h in text

    def test_no_deprecated_section_is_noop(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text(
            "# X\n\n## MODULES\n\n- nothing\n", encoding="utf-8")
        assert _prune_phantom_deprecated(str(tmp_path)) == 0

    def test_missing_file_is_noop(self, tmp_path):
        assert _prune_phantom_deprecated(str(tmp_path)) == 0
