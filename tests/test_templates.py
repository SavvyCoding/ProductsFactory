"""
Phase 3 — Template renderer tests.
All tests use a temporary directory (no real product repo needed).

Run: pytest tests/test_templates.py -v
"""

import pytest
from pathlib import Path

from templates.renderer import (
    select_stack,
    build_context,
    install_templates,
    get_stack_defaults,
    KNOWN_STACKS,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def product_dir(tmp_path) -> Path:
    """A temporary directory acting as the product working directory."""
    return tmp_path


def make_product(working_dir: Path, **kwargs) -> dict:
    return {
        "id": 1,
        "working_dir": str(working_dir),
        "name": "Test Product",
        "tech_stack": kwargs.pop("tech_stack", ["python"]),
        "type": kwargs.pop("type", "greenfield"),
        "config": kwargs.pop("config", {}),
        **kwargs,
    }


PM_API = "https://test-vm:8080"


# ══════════════════════════════════════════════════════════════════════════════
# select_stack
# ══════════════════════════════════════════════════════════════════════════════

class TestSelectStack:
    def test_python_detected(self):
        assert select_stack(["python"]) == "python"

    def test_node_detected(self):
        assert select_stack(["node"]) == "node"

    def test_go_detected(self):
        assert select_stack(["go"]) == "go"

    def test_first_known_wins(self):
        # python listed first → python wins even though node also present
        assert select_stack(["python", "node"]) == "python"

    def test_unknown_stack_falls_back_to_default(self):
        assert select_stack(["rust"]) == "default"

    def test_empty_stack_falls_back_to_default(self):
        assert select_stack([]) == "default"

    def test_none_stack_falls_back_to_default(self):
        assert select_stack(None) == "default"

    def test_case_insensitive(self):
        assert select_stack(["Python"]) == "python"
        assert select_stack(["NODE"]) == "node"


# ══════════════════════════════════════════════════════════════════════════════
# build_context
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildContext:
    def test_contains_required_keys(self, product_dir):
        p = make_product(product_dir)
        ctx = build_context(p, PM_API, "python")
        for key in ("PRODUCT_NAME", "PRODUCT_ID", "PM_API_URL", "MAX_BATCH_SIZE",
                     "TEST_COMMAND", "AUDIT_COMMAND", "SOURCE_PATH", "TEST_PATH"):
            assert key in ctx, f"Missing key: {key}"

    def test_product_name_used(self, product_dir):
        p = make_product(product_dir)
        ctx = build_context(p, PM_API, "python")
        assert ctx["PRODUCT_NAME"] == "Test Product"

    def test_pm_api_url_set(self, product_dir):
        p = make_product(product_dir)
        ctx = build_context(p, PM_API, "python")
        assert ctx["PM_API_URL"] == PM_API

    def test_max_batch_size_from_config(self, product_dir):
        p = make_product(product_dir, config={"max_batch_size": 5})
        ctx = build_context(p, PM_API, "python")
        assert ctx["MAX_BATCH_SIZE"] == "5"

    def test_max_batch_size_default(self, product_dir):
        p = make_product(product_dir, config={})
        ctx = build_context(p, PM_API, "python")
        assert ctx["MAX_BATCH_SIZE"] == "3"

    def test_runtime_version_override_python(self, product_dir):
        p = make_product(product_dir, config={"runtime_version": {"python": "3.12"}})
        ctx = build_context(p, PM_API, "python")
        assert "3.12" in ctx["RUNTIME"]
        assert ctx["PYTHON_VERSION"] == "3.12"

    def test_test_command_override_from_config(self, product_dir):
        custom_cmd = "python -m pytest my_tests/"
        p = make_product(product_dir, config={"test_command": custom_cmd})
        ctx = build_context(p, PM_API, "python")
        assert ctx["TEST_COMMAND"] == custom_cmd

    def test_audit_command_override_from_config(self, product_dir):
        p = make_product(product_dir, config={"audit_command": "safety check"})
        ctx = build_context(p, PM_API, "python")
        assert ctx["AUDIT_COMMAND"] == "safety check"

    def test_name_falls_back_to_folder_name(self, product_dir):
        p = make_product(product_dir)
        p["name"] = None
        ctx = build_context(p, PM_API, "python")
        assert ctx["PRODUCT_NAME"] == product_dir.name

    def test_node_stack_defaults(self, product_dir):
        p = make_product(product_dir, tech_stack=["node"])
        ctx = build_context(p, PM_API, "node")
        assert "npm" in ctx["AUDIT_COMMAND"]
        assert ctx["SOURCE_PATH"] == "src"

    def test_go_stack_defaults(self, product_dir):
        p = make_product(product_dir, tech_stack=["go"])
        ctx = build_context(p, PM_API, "go")
        assert "go test" in ctx["TEST_COMMAND"]
        assert "govulncheck" in ctx["AUDIT_COMMAND"]


# ══════════════════════════════════════════════════════════════════════════════
# install_templates — positive cases
# ══════════════════════════════════════════════════════════════════════════════

class TestInstallTemplatesPositive:
    def test_writes_three_md_files(self, product_dir):
        p = make_product(product_dir)
        written = install_templates(p, PM_API)
        md_files = [f for f in written if f.endswith(".md")]
        names = {Path(f).name for f in md_files}
        assert "AGENT_WORKFLOW.md" in names
        assert "CLAUDE.md" in names
        assert "ARCHITECTURE.md" in names
        assert "CONTRIBUTING.md" in names

    def test_contributing_md_describes_sprint_pr_flow(self, product_dir):
        """Pre-filled CONTRIBUTING.md must describe the sprint-PR-mode flow,
        not generic fork→feature-branch→PR-against-main boilerplate, so a
        later 'Developer Documentation' feature finds an existing file the
        coder agent can leave alone instead of regenerating with contradictory
        instructions."""
        p = make_product(product_dir)
        install_templates(p, PM_API)
        contributing = (product_dir / "CONTRIBUTING.md").read_text(encoding='utf-8')
        # Mentions the sprint-branch concept (the actual workflow).
        assert "sprint/" in contributing
        # Does NOT advise the standard fork-and-PR-to-main flow.
        assert "fork" in contributing.lower()  # only as a "Do not fork" warning
        assert "Do not fork" in contributing
        # Resolves the product name placeholder.
        assert "Test Product" in contributing
        assert "{PRODUCT_NAME}" not in contributing

    def test_placeholders_resolved(self, product_dir):
        p = make_product(product_dir)
        install_templates(p, PM_API)
        agent_wf = (product_dir / "AGENT_WORKFLOW.md").read_text(encoding='utf-8')
        assert "Test Product" in agent_wf
        assert PM_API in agent_wf
        assert "{PRODUCT_NAME}" not in agent_wf   # all resolved
        assert "{PM_API_URL}" not in agent_wf

    def test_features_md_created(self, product_dir):
        p = make_product(product_dir)
        install_templates(p, PM_API)
        assert (product_dir / "features.md").exists()

    def test_greenfield_directories_created(self, product_dir):
        p = make_product(product_dir, type="greenfield")
        install_templates(p, PM_API)
        assert (product_dir / "src").is_dir()
        assert (product_dir / "tests").is_dir()
        assert (product_dir / "Results").is_dir()
        assert (product_dir / "Temp").is_dir()

    def test_brownfield_directories_not_created(self, product_dir):
        p = make_product(product_dir, type="brownfield")
        install_templates(p, PM_API)
        assert not (product_dir / "src").exists()

    def test_python_claude_md_contains_pytest(self, product_dir):
        p = make_product(product_dir, tech_stack=["python"])
        install_templates(p, PM_API)
        claude_md = (product_dir / "CLAUDE.md").read_text(encoding='utf-8')
        assert "pytest" in claude_md
        assert "pip-audit" in claude_md

    def test_node_claude_md_contains_npm(self, product_dir):
        p = make_product(product_dir, tech_stack=["node"])
        install_templates(p, PM_API)
        claude_md = (product_dir / "CLAUDE.md").read_text(encoding='utf-8')
        assert "npm" in claude_md

    def test_go_claude_md_contains_go_test(self, product_dir):
        p = make_product(product_dir, tech_stack=["go"])
        install_templates(p, PM_API)
        claude_md = (product_dir / "CLAUDE.md").read_text(encoding='utf-8')
        assert "go test" in claude_md

    def test_max_batch_size_in_agent_workflow(self, product_dir):
        p = make_product(product_dir, config={"max_batch_size": 2})
        install_templates(p, PM_API)
        content = (product_dir / "AGENT_WORKFLOW.md").read_text(encoding='utf-8')
        assert "2" in content
        assert "{MAX_BATCH_SIZE}" not in content

    def test_returns_list_of_written_files(self, product_dir):
        p = make_product(product_dir)
        written = install_templates(p, PM_API)
        assert isinstance(written, list)
        assert len(written) > 0


# ══════════════════════════════════════════════════════════════════════════════
# install_templates — negative cases
# ══════════════════════════════════════════════════════════════════════════════

class TestInstallTemplatesNegative:
    def test_raises_if_working_dir_missing(self, tmp_path):
        p = make_product(tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            install_templates(p, PM_API)

    def test_does_not_overwrite_existing_by_default(self, product_dir):
        # Write custom content first
        existing = product_dir / "AGENT_WORKFLOW.md"
        existing.write_text("# My custom workflow\n")
        p = make_product(product_dir)
        install_templates(p, PM_API)
        assert existing.read_text(encoding='utf-8') == "# My custom workflow\n"

    def test_force_overwrites_existing(self, product_dir):
        existing = product_dir / "CLAUDE.md"
        existing.write_text("# old content\n")
        p = make_product(product_dir)
        install_templates(p, PM_API, force=True)
        assert existing.read_text(encoding='utf-8') != "# old content\n"
        assert "Test Product" in existing.read_text(encoding='utf-8')

    def test_idempotent_second_install(self, product_dir):
        p = make_product(product_dir)
        written_first  = install_templates(p, PM_API)
        written_second = install_templates(p, PM_API)
        # Second run writes nothing (files already exist)
        md_written_second = [f for f in written_second if f.endswith(".md")]
        assert md_written_second == []


# ══════════════════════════════════════════════════════════════════════════════
# install_templates — edge cases
# ══════════════════════════════════════════════════════════════════════════════

class TestInstallTemplatesEdge:
    def test_unknown_stack_uses_default_template(self, product_dir):
        p = make_product(product_dir, tech_stack=["rust"])
        install_templates(p, PM_API)
        # default CLAUDE.md should still be written
        assert (product_dir / "CLAUDE.md").exists()

    def test_empty_tech_stack_uses_default(self, product_dir):
        p = make_product(product_dir, tech_stack=[])
        install_templates(p, PM_API)
        assert (product_dir / "CLAUDE.md").exists()

    def test_product_name_with_special_chars(self, product_dir):
        p = make_product(product_dir)
        p["name"] = "My App & Co."
        install_templates(p, PM_API)
        content = (product_dir / "AGENT_WORKFLOW.md").read_text(encoding='utf-8')
        assert "My App & Co." in content

    def test_brownfield_config_overrides_test_command(self, product_dir):
        custom_cmd = "pytest apps/ -v --tb=short"
        p = make_product(product_dir, type="brownfield",
                         config={"test_command": custom_cmd, "max_batch_size": 2})
        install_templates(p, PM_API)
        claude_md = (product_dir / "CLAUDE.md").read_text(encoding='utf-8')
        assert custom_cmd in claude_md

    def test_all_known_stacks_have_templates(self):
        from templates.renderer import STACKS_DIR
        for stack in KNOWN_STACKS:
            assert (STACKS_DIR / stack / "CLAUDE.md").exists(), f"Missing CLAUDE.md for {stack}"
            assert (STACKS_DIR / stack / "ARCHITECTURE.md").exists(), f"Missing ARCHITECTURE.md for {stack}"

    def test_default_stack_has_templates(self):
        from templates.renderer import STACKS_DIR
        assert (STACKS_DIR / "default" / "CLAUDE.md").exists()
        assert (STACKS_DIR / "default" / "ARCHITECTURE.md").exists()
