"""
ProductFactory plugin for Hermes agent.

Registers the orchestration tools that deploy/hermes/skills/orchestrate.md
references. Handlers are thin wrappers around existing orchestrator/*
modules (docker_runner, heartbeat, github_client) — no logic is reimplemented.

Entry point:
    def register(ctx: PluginContext) -> None

Called once by Hermes at plugin-load time. We delegate to toolsets.register_all
for the actual registry.register_tool() calls.
"""

from __future__ import annotations

import logging

from . import tools

log = logging.getLogger("hermes.productfactory")


# Every tool in the plugin shares this toolset tag. Hermes's orchestrate.md
# skill is read as a system prompt; all registered tools become available.
TOOLSET = "productfactory"


def _register_tool(ctx, name: str, description: str, schema: dict, handler) -> None:
    ctx.register_tool(
        name=name,
        toolset=TOOLSET,
        schema={
            "name": name,
            "description": description,
            "parameters": schema,
        },
        handler=handler,
        description=description,
    )


def register(ctx) -> None:
    """Plugin entry point — Hermes calls this with a PluginContext."""

    # --- PM API --------------------------------------------------------
    _register_tool(ctx, "pm_api",
        "Generic PM REST API passthrough. Use for any endpoint not covered by a dedicated tool.",
        {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                "path":   {"type": "string", "description": "Path starting with /api/..."},
                "body":   {"type": "object", "description": "Optional JSON body"},
            },
            "required": ["method", "path"],
        },
        tools.pm_api,
    )

    _register_tool(ctx, "get_products",
        "Fetch all products (GET /api/products). Returns list of product dicts.",
        {"type": "object", "properties": {}},
        tools.get_products,
    )

    _register_tool(ctx, "get_active_sprint",
        "Get the active sprint for a product, or null if none.",
        {"type": "object",
         "properties": {"product_id": {"type": "integer"}},
         "required": ["product_id"]},
        tools.get_active_sprint,
    )

    _register_tool(ctx, "get_sprints",
        "Get all sprints (any status) for a product.",
        {"type": "object",
         "properties": {"product_id": {"type": "integer"}},
         "required": ["product_id"]},
        tools.get_sprints,
    )

    _register_tool(ctx, "get_features",
        "Get all features for a product.",
        {"type": "object",
         "properties": {"product_id": {"type": "integer"}},
         "required": ["product_id"]},
        tools.get_features,
    )

    _register_tool(ctx, "get_system_config",
        "Fetch global system config (auto_merge_enabled, github_pat, max_open_prs, etc).",
        {"type": "object", "properties": {}},
        tools.get_system_config,
    )

    _register_tool(ctx, "set_feature_status",
        "PATCH a feature's status (and optionally pr_number).",
        {"type": "object",
         "properties": {
             "feature_id": {"type": "integer"},
             "status":     {"type": "string"},
             "pr_number":  {"type": "integer"},
         },
         "required": ["feature_id", "status"]},
        tools.set_feature_status,
    )

    _register_tool(ctx, "reset_stuck_features",
        "Trigger the PM API's time-gated reset for features stuck in agent states.",
        {"type": "object", "properties": {}},
        tools.reset_stuck_features,
    )

    _register_tool(ctx, "poller_heartbeat",
        "Refresh the distributed poller lock. Returns 200 (ok) or 409 (lock stolen - EXIT immediately).",
        {"type": "object", "properties": {}},
        tools.poller_heartbeat,
    )

    _register_tool(ctx, "alert",
        "Send an alert webhook. Severity: info | warning | error | critical.",
        {"type": "object",
         "properties": {
             "severity":     {"type": "string"},
             "message":      {"type": "string"},
             "product_name": {"type": "string"},
         },
         "required": ["severity", "message"]},
        tools.alert,
    )

    # --- Docker / session ----------------------------------------------
    _register_tool(ctx, "launch_session",
        "Spawn the agent Docker container and wait for it to finish. Returns exit_code.",
        {"type": "object",
         "properties": {
             "product_id": {"type": "integer"},
             "persona":    {"type": "string"},
         },
         "required": ["product_id", "persona"]},
        tools.launch_session,
    )

    _register_tool(ctx, "kill_stale_container",
        "docker kill any pf-{product_id}-* container.",
        {"type": "object",
         "properties": {"product_id": {"type": "integer"}},
         "required": ["product_id"]},
        tools.kill_stale_container,
    )

    _register_tool(ctx, "check_stale_sessions",
        "For each ready product, kill runaway agent containers whose progress.md is stale.",
        {"type": "object", "properties": {}},
        tools.check_stale_sessions,
    )

    # --- PR reconciliation ---------------------------------------------
    _register_tool(ctx, "reconcile_prs",
        "Reconcile merged+closed PRs for one product. Pass null product_id to run reset_stuck only.",
        {"type": "object",
         "properties": {"product_id": {"type": ["integer", "null"]}},
         "required": ["product_id"]},
        tools.reconcile_prs,
    )

    # --- GitHub --------------------------------------------------------
    _register_tool(ctx, "github_list_prs",
        "List PRs on a product's GitHub repo by state.",
        {"type": "object",
         "properties": {
             "product_id": {"type": "integer"},
             "state":      {"type": "string", "enum": ["open", "closed", "all"]},
         },
         "required": ["product_id"]},
        tools.github_list_prs,
    )

    _register_tool(ctx, "github_merge_pr",
        "Squash-merge a PR on a product's GitHub repo.",
        {"type": "object",
         "properties": {
             "product_id": {"type": "integer"},
             "pr_number":  {"type": "integer"},
         },
         "required": ["product_id", "pr_number"]},
        tools.github_merge_pr,
    )

    log.info("productfactory plugin registered 15 tools in toolset %r", TOOLSET)
