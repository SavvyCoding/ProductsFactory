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

import re
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("renderer")

TEMPLATES_DIR = Path(__file__).parent
STACKS_DIR    = TEMPLATES_DIR / "stacks"
KNOWN_STACKS  = {"python", "node", "go"}

# Framework/ecosystem names PMs actually type → the stack template that
# serves them. select_stack consults this AFTER the bare-name and
# strip-qualifier checks fail. Canonical incident: MyCalc1 2026-06
# (`preferred_stack: "nextjs"`) matched nothing → got the `default`
# templates → generic .gitignore with no `.next/` entry → 40 build-cache
# files (9.16MB webpack pack.gz) committed and auto-merged, .git grew to
# 93MB. One unmapped string silently degraded every downstream gate
# (.gitignore is RO-mounted, so the agent couldn't repair it either).
STACK_SYNONYMS: dict[str, str] = {
    # node ecosystem
    "nextjs": "node", "next": "node", "react": "node", "vue": "node",
    "nuxt": "node", "svelte": "node", "angular": "node", "express": "node",
    "nestjs": "node", "vite": "node", "remix": "node", "astro": "node",
    "typescript": "node", "javascript": "node", "ts": "node", "js": "node",
    "nodejs": "node", "deno": "node", "bun": "node",
    # python ecosystem
    "fastapi": "python", "flask": "python", "django": "python",
    "py": "python", "python3": "python",
    # go ecosystem
    "golang": "go",
}

# Placeholder defaults — overridden by product config and stack-specific values
STACK_DEFAULTS: dict[str, dict] = {
    "python": {
        "PYTHON_VERSION": "3.11",
        "RUNTIME":        "Python 3.11",
        # 2026-05-22: aligned with the default/node stacks — lowercase
        # src/tests instead of Salesforce-style uppercase SRC/TestCases.
        # The 2026-05-07 default-stack migration (see "default" block below)
        # left this entry alone, which caused python products to drift onto
        # SRC/TestCases while every other stack used src/tests. On a
        # case-sensitive Linux container, an agent that wrote both
        # variants (e.g. SRC/main.py from the template + src/api.py because
        # most FastAPI tutorials use lowercase) ended up with parallel
        # directories and broken imports. MyDocusign 2026-05-22 had both
        # `tests/` AND `TestCases/` for this reason. Existing python
        # products need a one-time `git mv SRC src && git mv TestCases tests`
        # migration; new products will get the right layout immediately.
        "TEST_COMMAND":   "pytest tests/ -v --json-report --json-report-file=Results/{feature_name}_results.json --cov=src --cov-fail-under=70",
        "AUDIT_COMMAND":  "pip-audit",
        "SOURCE_PATH":    "src",
        "TEST_PATH":      "tests",
        "NEW_FEATURE_SOURCE": "src",
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
    """Returns the best matching stack name, or 'default'.

    Accepts both bare stack names (`python`, `node`, `go`) and
    framework-qualified ones (`python_fastapi`, `node_react`, `go_chi`, etc.).
    The PM web UI lets operators pick framework-specific options like
    `python_fastapi`; before this normalisation those were silently dropping
    to the `default` template because select_stack only matched against the
    bare-language KNOWN_STACKS set.

    Real incident: MyDocusign 2026-05-20 had tech_stack=["python_fastapi"],
    select_stack returned "default", products got the generic
    `default/.gitignore` (missing `.coverage`, `*.db`) and empty
    `default/quality_gates.json`. Cascade of debris committed + no
    enforced coverage gate.

    Normalisation strategy: try the bare value first (for backward
    compatibility), then strip a single framework qualifier on `_`/`-`
    and retry against KNOWN_STACKS, then consult STACK_SYNONYMS for
    framework/ecosystem names (`nextjs`→node, `fastapi`→python, ...).

    Falling through to "default" is logged at WARNING with the full
    tech_stack value — the default templates have TODO test commands,
    empty quality gates, and a framework-blind .gitignore, so a silent
    fall-through degrades every downstream gate for the product's whole
    life (see STACK_SYNONYMS docstring for the MyCalc1 incident).
    """
    for tech in (tech_stack or []):
        normalised = tech.lower()
        if normalised in KNOWN_STACKS:
            return normalised
        # Framework-qualified like `python_fastapi` / `node-react` →
        # try the bare leading token.
        base = re.split(r"[_\-]", normalised, maxsplit=1)[0]
        if base in KNOWN_STACKS:
            return base
        # Framework/ecosystem synonym (`nextjs`, `fastapi`, `golang`, ...)
        # — check both the full value and the leading token.
        for candidate in (normalised, base):
            if candidate in STACK_SYNONYMS:
                return STACK_SYNONYMS[candidate]
    if tech_stack:
        log.warning(
            "select_stack: no known stack or synonym for tech_stack=%r — "
            "falling back to 'default' templates (TODO test command, empty "
            "quality gates, framework-blind .gitignore). Add a synonym to "
            "templates/renderer.STACK_SYNONYMS if this is a real stack.",
            tech_stack,
        )
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

    # 3c2. pytest.ini — stack-specific. Ships only when the stack has one
    # (currently python). RO-mounted via _PM_CURATED_RO_FILES so the coder
    # can't overwrite it with a half-broken hand-rolled version. Canonical
    # 2026-05-26 SmokeTest incident: coder kept writing pytest.ini with
    # mismatched `pythonpath` vs the import style in test files
    # (`from src.X` vs `from X`), causing ModuleNotFoundError on every
    # collection. Pre-shipping a canonical one with `pythonpath = .` plus
    # `from src.X` import style eliminates that whole class of failure.
    stack_pytest_ini = STACKS_DIR / stack / "pytest.ini"
    if stack_pytest_ini.exists():
        written += _write_file(
            working_dir / "pytest.ini",
            stack_pytest_ini,
            context, force,
        )

    # 3c3. requirements.txt — stack-specific. Ships only when the stack has
    # one (currently python). Pre-seeded with the test toolchain (pytest,
    # pytest-cov) so a fresh greenfield product can ship its first test
    # commit without tripping Guard 18 (deps-coherence) on `import pytest`.
    # Canonical 2026-05-27 Calculator incident: every coder session imported
    # pytest in tests/, no requirements.txt existed, Guard 18 fired, the
    # agent's rework removed the import instead of adding the dep, the
    # bounce repeated, supervisor's flap detector blocked all three
    # features. The coder agent is expected to APPEND runtime deps here;
    # the pre-seeded test deps stay.
    stack_requirements = STACKS_DIR / stack / "requirements.txt"
    if stack_requirements.exists():
        written += _write_file(
            working_dir / "requirements.txt",
            stack_requirements,
            context, force,
        )

    # 3d. check_deletion_safety.py — stack-agnostic. Standalone helper the
    # coder runs pre-commit (Step 5b in AGENT_WORKFLOW.md) to catch removed
    # top-level Python symbols that other files still reference. Mirrors
    # the orchestrator's post-coder Guard 17. Stdlib-only, runs from the
    # product's working directory. Listed in _PM_CURATED_RO_FILES so the
    # coder can't tamper with it.
    helper_script = TEMPLATES_DIR / "check_deletion_safety.py"
    if helper_script.exists():
        written += _write_file(
            working_dir / "check_deletion_safety.py",
            helper_script,
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


