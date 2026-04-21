"""
ProductFactory plugin for Hermes agent.

Exposes the orchestration tools (pm_api, launch_session, github_*, etc.) that
orchestrate.md references. Tool handlers are thin wrappers around the existing
orchestrator/ Python code — we reuse docker_runner.run_claude_in_docker,
heartbeat.check_stale_sessions, and the PM REST API rather than reimplementing
any logic.
"""

from .toolsets import register_all

__all__ = ["register_all"]
