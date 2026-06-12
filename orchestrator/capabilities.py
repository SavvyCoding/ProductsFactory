"""Agent-image capability manifest (wave-5, 2026-06-12).

Single source of truth for which command-line tools agent/test/verify
containers can be EXPECTED to have. Mirrors deploy/docker/Dockerfile —
when a tool is added to or removed from the image, update this set in the
same commit.

Semantics: this is the "obtainable" set — tools baked into the image PLUS
runners products install per-project (jest/vitest arrive via `npm ci`).
A test failure reporting one of THESE missing is a transient/environment
hiccup → env_broken (no fix_attempts bump, operator alert, retry).
A failure reporting a tool NOT in this set is permanent by construction —
the sandbox will never provide it (canonical: `docker`, absent
deliberately per the security model; DogTinder #1518/19/20 ground to
Block over it) → terminal Block-for-redesign via the tool_missing branch
in pipelines/post_coder.py, mirroring the service_missing triage.

The designer prompt renders this list so stories aren't written against
absent tools in the first place.
"""

AGENT_IMAGE_TOOLS: frozenset[str] = frozenset({
    # shells / core
    "sh", "bash", "env", "timeout",
    # vcs / forge
    "git", "gh", "pre-commit",
    # network / data
    "curl", "wget", "jq",
    # python toolchain
    "python", "python3", "pip", "pip3", "pytest", "alembic",
    "ruff", "black", "flake8", "mypy", "uvicorn", "gunicorn",
    # node toolchain (jest/vitest/tsc arrive via npm ci per project)
    "node", "npm", "npx", "jest", "vitest", "tsc", "eslint",
    # go toolchain
    "go", "gofmt",
    # browser automation
    "playwright",
    # client CLIs for sidecar services (server side comes from
    # orchestrator/services.py provisioning, never from the image)
    "psql", "redis-cli",
    # meta-infrastructure static linters (wave-5: CI-YAML / Dockerfile
    # stories verify statically — see designer.md meta-infra contract)
    "actionlint", "hadolint",
})


def is_obtainable_tool(name: str) -> bool:
    """True when the agent environment can be expected to provide `name`
    (baked in, or per-project installable). Case-sensitive on purpose —
    CLI names are."""
    return name in AGENT_IMAGE_TOOLS
