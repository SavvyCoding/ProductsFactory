"""
Template renderer — instantiates ProductFactory templates into a product repo.

Called by setup_product.py after discovery.
Writes AGENT_WORKFLOW.md, CONTRIBUTING.md, CLAUDE.md, and ARCHITECTURE.md
into the product's working directory. Never overwrites existing files
unless force=True.

Stack selection priority:
  1. First entry in product["tech_stack"] that matches a known stack
  2. "default" if no known stack matches
"""

import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("renderer")

TEMPLATES_DIR = Path(__file__).parent
STACKS_DIR    = TEMPLATES_DIR / "stacks"
KNOWN_STACKS  = {"python", "node", "go"}

# Placeholder defaults — overridden by product config and stack-specific values
STACK_DEFAULTS: dict[str, dict] = {
    "python": {
        "PYTHON_VERSION": "3.11",
        "RUNTIME":        "Python 3.11",
        "TEST_COMMAND":   "pytest TestCases/ -v --json-report --json-report-file=Results/{feature_name}_results.json --cov=SRC --cov-fail-under=70",
        "AUDIT_COMMAND":  "pip-audit",
        "SOURCE_PATH":    "SRC",
        "TEST_PATH":      "TestCases",
        "NEW_FEATURE_SOURCE": "SRC",
    },
    "node": {
        "NODE_VERSION":   "20",
        "TS_VERSION":     "5",
        "PACKAGE_MANAGER": "npm",
        "RUNTIME":        "Node.js 20 / TypeScript 5",
        "TEST_COMMAND":   "npm test -- --coverage --coverageThreshold='{\"global\":{\"lines\":70}}'",
        "AUDIT_COMMAND":  "npm audit --audit-level=high",
        "SOURCE_PATH":    "src",
        "TEST_PATH":      "tests",
        "NEW_FEATURE_SOURCE": "src",
    },
    "go": {
        "GO_VERSION":     "1.22",
        "RUNTIME":        "Go 1.22",
        "TEST_COMMAND":   "go test ./... -v -coverprofile=Results/coverage.out && go tool cover -func=Results/coverage.out",
        "AUDIT_COMMAND":  "govulncheck ./...",
        "SOURCE_PATH":    "internal",
        "TEST_PATH":      "internal",
        "NEW_FEATURE_SOURCE": "internal",
    },
    "default": {
        "RUNTIME":        "See CLAUDE.md",
        "TEST_COMMAND":   "# TODO: configure test command",
        "AUDIT_COMMAND":  "# TODO: configure audit command",
        # 2026-05-07: switched default from "SRC"/"TestCases" (Salesforce-style
        # uppercase) to lowercase "src"/"tests" — matches the Node stack's
        # convention and avoids the case-mismatch confusion seen on MySalesforce
        # feature #224 where reviewer comments oscillated between
        # `SRC/app/api/...` and `src/app/api/...` paths and the coder created
        # files in one casing while imports/tests resolved against the other
        # (Windows is case-insensitive on disk but Node's module resolver and
        # tsconfig are case-sensitive).
        "SOURCE_PATH":    "src",
        "TEST_PATH":      "tests",
        "NEW_FEATURE_SOURCE": "src",
    },
}


def select_stack(tech_stack: list[str] | None) -> str:
    """Returns the best matching stack name, or 'default'."""
    for tech in (tech_stack or []):
        if tech.lower() in KNOWN_STACKS:
            return tech.lower()
    return "default"


def build_context(product: dict, pm_api_url: str, stack: str) -> dict:
    """
    Merges stack defaults + product config + runtime values into a flat
    substitution dict. Every {PLACEHOLDER} in templates maps to a key here.
    """
    config   = product.get("config") or {}
    defaults = STACK_DEFAULTS.get(stack, STACK_DEFAULTS["default"]).copy()

    # Runtime version override from product_config.json
    runtime_ver = config.get("runtime_version", {})
    if stack == "python" and "python" in runtime_ver:
        v = runtime_ver["python"]
        defaults["PYTHON_VERSION"] = v
        defaults["RUNTIME"] = f"Python {v}"
    elif stack == "node" and "node" in runtime_ver:
        v = runtime_ver["node"]
        defaults["NODE_VERSION"] = v
        defaults["RUNTIME"] = f"Node.js {v}"
    elif stack == "go" and "go" in runtime_ver:
        v = runtime_ver["go"]
        defaults["GO_VERSION"] = v
        defaults["RUNTIME"] = f"Go {v}"

    # Product-level overrides from product_config.json
    if config.get("test_command"):
        defaults["TEST_COMMAND"] = config["test_command"]
    if config.get("audit_command"):
        defaults["AUDIT_COMMAND"] = config["audit_command"]
    if config.get("new_feature_source"):
        defaults["NEW_FEATURE_SOURCE"] = config["new_feature_source"]
    if config.get("new_feature_tests"):
        defaults["NEW_FEATURE_TESTS"] = config["new_feature_tests"]

    return {
        **defaults,
        "PRODUCT_NAME":    product.get("name") or Path(product["working_dir"]).name,
        "PRODUCT_ID":      str(product.get("id", "")),
        "PM_API_URL":      pm_api_url,
        "MAX_BATCH_SIZE":  str(config.get("max_batch_size", 3)),
        "DATE":            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "SESSION_UID":     "renderer",
    }


def _render(template_text: str, context: dict) -> str:
    """Replaces all {KEY} placeholders with context values."""
    def replacer(match):
        key = match.group(1)
        return context.get(key, match.group(0))  # leave unknown placeholders as-is
    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", replacer, template_text)


def install_templates(
    product: dict,
    pm_api_url: str,
    force: bool = False,
) -> list[str]:
    """
    Writes AGENT_WORKFLOW.md, CLAUDE.md, ARCHITECTURE.md, and features.md
    into product["working_dir"].

    Returns a list of file paths that were written.
    Never overwrites an existing file unless force=True.
    """
    working_dir = Path(product["working_dir"])
    if not working_dir.exists():
        raise FileNotFoundError(f"Working directory does not exist: {working_dir}")

    stack   = select_stack(product.get("tech_stack"))
    context = build_context(product, pm_api_url, stack)
    written = []

    # 1. AGENT_WORKFLOW.md — stack-agnostic
    written += _write_file(
        working_dir / "AGENT_WORKFLOW.md",
        TEMPLATES_DIR / "AGENT_WORKFLOW.md",
        context, force,
    )

    # 1b. CONTRIBUTING.md — stack-agnostic. Pre-fills the human-contributor
    # guide with the sprint-PR-mode flow this repo actually uses, so that
    # later "Developer Documentation" features don't trigger the agent to
    # write generic CONTRIBUTING boilerplate (fork → feature-branch →
    # PR-against-main) that contradicts the orchestrator's actual
    # workflow.
    written += _write_file(
        working_dir / "CONTRIBUTING.md",
        TEMPLATES_DIR / "CONTRIBUTING.md",
        context, force,
    )

    # 2. CLAUDE.md — stack-specific
    written += _write_file(
        working_dir / "CLAUDE.md",
        STACKS_DIR / stack / "CLAUDE.md",
        context, force,
    )

    # 3. ARCHITECTURE.md — stack-specific
    written += _write_file(
        working_dir / "ARCHITECTURE.md",
        STACKS_DIR / stack / "ARCHITECTURE.md",
        context, force,
    )

    # 3b. .gitignore — stack-specific. Critical: without this, npm/pip/etc.
    # install pulls thousands of files into the working tree and the post-coder
    # `git stash` walks them all, blowing past the 120s timeout. Stack-specific
    # so we ship the right ignore list per product type.
    stack_gitignore = STACKS_DIR / stack / ".gitignore"
    if stack_gitignore.exists():
        written += _write_file(
            working_dir / ".gitignore",
            stack_gitignore,
            context, force,
        )

    # 3c. quality_gates.json — stack-specific. Machine-readable mirror of the
    # CONFIG GATES section in ARCHITECTURE.md. Consumed by the post-coder
    # _check_config_gates lint (Phase 4 of quality-specs). Skipped (passes
    # through unchanged) if the stack has no enforced gates.
    stack_gates = STACKS_DIR / stack / "quality_gates.json"
    if stack_gates.exists():
        written += _write_file(
            working_dir / "quality_gates.json",
            stack_gates,
            context, force,
        )

    # 4. features.md — no longer created (DB is single source of truth)

    # 5. Create required directories if they don't exist (greenfield only).
    # Use the stack's SOURCE_PATH/TEST_PATH so e.g. a Node project gets `src/`
    # + `tests/` (matching what npm/Next/jest expect), not the legacy
    # `SRC/TestCases/`. Pre-2026-05-07 this hardcoded "SRC"/"TestCases" for
    # every stack, so a Node greenfield ended up with BOTH `SRC/` (init) and
    # `src/` (agent-created) — case-only siblings on Windows that broke the
    # coder/reviewer feedback loop.
    if product.get("type", "greenfield") == "greenfield":
        src_dir  = context.get("SOURCE_PATH") or "src"
        test_dir = context.get("TEST_PATH")   or "tests"
        # Dedupe (e.g. go stack uses "internal" for both) and add Results/Temp
        # which are universal scratch dirs.
        scratch_dirs = ("Results", "Temp")
        for dir_name in tuple(dict.fromkeys((src_dir, test_dir, *scratch_dirs))):
            d = working_dir / dir_name
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                (d / ".gitkeep").touch()
                written.append(str(d / ".gitkeep"))

    return written


def _write_file(
    dest: Path,
    template_path: Path,
    context: dict,
    force: bool,
) -> list[str]:
    if not force and dest.exists():
        log.debug(f"Skipping (already exists): {dest}")
        return []
    if not template_path.exists():
        log.warning(f"Template not found: {template_path} — skipping")
        return []
    rendered = _render(template_path.read_text(encoding="utf-8"), context)
    dest.write_text(rendered, encoding="utf-8")
    log.info(f"Wrote: {dest}")
    return [str(dest)]


def get_stack_defaults(stack: str) -> dict:
    """Exposed for testing — returns defaults for a given stack."""
    return STACK_DEFAULTS.get(stack, STACK_DEFAULTS["default"]).copy()
