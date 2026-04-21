"""
Tool registration for the ProductFactory Hermes plugin.

Registers every tool declared in deploy/hermes/skills/orchestrate.md with the
Hermes registry. Called by Hermes on plugin load.

NOTE: The exact Hermes registry API surface may require small tweaks once we
run against real Hermes. The pattern below follows Nous's documented
`registry.register(name, handler, schema)` approach.
"""

from __future__ import annotations

from . import tools


def register_all(registry) -> None:
    """Entry point Hermes calls at plugin load time."""

    # ---- PM API ---------------------------------------------------------
    registry.register(
        name="pm_api",
        handler=tools.pm_api,
        schema={
            "name": "pm_api",
            "description": "Generic PM REST API passthrough. Use for any endpoint not covered by a dedicated tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                    "path":   {"type": "string", "description": "Path starting with /api/..."},
                    "body":   {"type": "object", "description": "Optional JSON body"},
                },
                "required": ["method", "path"],
            },
        },
    )

    registry.register(
        name="get_products",
        handler=tools.get_products,
        schema={
            "name": "get_products",
            "description": "Fetch all products. Returns list of product dicts.",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    registry.register(
        name="get_active_sprint",
        handler=tools.get_active_sprint,
        schema={
            "name": "get_active_sprint",
            "description": "Get the active sprint for a product, or null if none.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    )

    registry.register(
        name="get_sprints",
        handler=tools.get_sprints,
        schema={
            "name": "get_sprints",
            "description": "Get all sprints (any status) for a product.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    )

    registry.register(
        name="get_features",
        handler=tools.get_features,
        schema={
            "name": "get_features",
            "description": "Get all features for a product.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    )

    registry.register(
        name="get_system_config",
        handler=tools.get_system_config,
        schema={
            "name": "get_system_config",
            "description": "Fetch global system config (auto_merge_enabled, github_pat, max_open_prs, etc).",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    registry.register(
        name="set_feature_status",
        handler=tools.set_feature_status,
        schema={
            "name": "set_feature_status",
            "description": "PATCH a feature's status (and optionally pr_number).",
            "parameters": {
                "type": "object",
                "properties": {
                    "feature_id": {"type": "integer"},
                    "status":     {"type": "string"},
                    "pr_number":  {"type": "integer", "description": "Optional PR number"},
                },
                "required": ["feature_id", "status"],
            },
        },
    )

    registry.register(
        name="reset_stuck_features",
        handler=tools.reset_stuck_features,
        schema={
            "name": "reset_stuck_features",
            "description": "Trigger the PM API's time-gated reset for features stuck in agent states.",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    registry.register(
        name="poller_heartbeat",
        handler=tools.poller_heartbeat,
        schema={
            "name": "poller_heartbeat",
            "description": "Refresh the distributed poller lock. Returns 200 (ok) or 409 (lock stolen - EXIT).",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    registry.register(
        name="alert",
        handler=tools.alert,
        schema={
            "name": "alert",
            "description": "Send an alert webhook. Severity: info | warning | error | critical.",
            "parameters": {
                "type": "object",
                "properties": {
                    "severity":     {"type": "string"},
                    "message":      {"type": "string"},
                    "product_name": {"type": "string"},
                },
                "required": ["severity", "message"],
            },
        },
    )

    # ---- Docker / session management ------------------------------------
    registry.register(
        name="launch_session",
        handler=tools.launch_session,
        schema={
            "name": "launch_session",
            "description": "Spawn the agent Docker container and wait for it to finish. Returns exit_code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "persona":    {"type": "string", "description": "designer|coder|reviewer|retrospective|planner|product_planner|product_trainer|documenter|analytics|refactorer|devops|recommender"},
                },
                "required": ["product_id", "persona"],
            },
        },
    )

    registry.register(
        name="kill_stale_container",
        handler=tools.kill_stale_container,
        schema={
            "name": "kill_stale_container",
            "description": "docker kill any pf-{product_id}-* container.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": "integer"}},
                "required": ["product_id"],
            },
        },
    )

    registry.register(
        name="check_stale_sessions",
        handler=tools.check_stale_sessions,
        schema={
            "name": "check_stale_sessions",
            "description": "For each ready product, kill runaway agent containers whose progress.md is stale.",
            "parameters": {"type": "object", "properties": {}},
        },
    )

    # ---- PR reconciliation ----------------------------------------------
    registry.register(
        name="reconcile_prs",
        handler=tools.reconcile_prs,
        schema={
            "name": "reconcile_prs",
            "description": "Reconcile merged+closed PRs for one product. Pass null to run global reset_stuck only.",
            "parameters": {
                "type": "object",
                "properties": {"product_id": {"type": ["integer", "null"]}},
                "required": ["product_id"],
            },
        },
    )

    # ---- GitHub ---------------------------------------------------------
    registry.register(
        name="github_list_prs",
        handler=tools.github_list_prs,
        schema={
            "name": "github_list_prs",
            "description": "List PRs on a product's GitHub repo by state.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "state":      {"type": "string", "enum": ["open", "closed", "all"]},
                },
                "required": ["product_id"],
            },
        },
    )

    registry.register(
        name="github_merge_pr",
        handler=tools.github_merge_pr,
        schema={
            "name": "github_merge_pr",
            "description": "Squash-merge a PR on a product's GitHub repo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "integer"},
                    "pr_number":  {"type": "integer"},
                },
                "required": ["product_id", "pr_number"],
            },
        },
    )
