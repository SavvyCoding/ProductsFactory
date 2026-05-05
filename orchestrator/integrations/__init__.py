# Integrations subpackage — adapters for external systems (Docker, Git, GitHub,
# the PM API, the Claude CLI, Ollama).
#
# Phase 2 of OrchestratorRefactor:
#   docker_cli.py — _chmod_workspace_via_alpine (host-side throwaway container)
#   git_ops.py    — single safe_run helper + workspace git utilities
#   pm_api.py     — single httpx.Client wrapper for /api/* calls
