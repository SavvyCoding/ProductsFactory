# Hermes Orchestrator

Hermes replaces the Windows-hosted Python poller with a Docker-based agent
(github.com/nousresearch/hermes-agent) that drives the ProductFactory sprint
pipeline using an LLM-reasoned orchestration loop.

## How it works

1. `docker compose --profile hermes up -d hermes` starts the Hermes gateway.
2. On first boot, `bootstrap.sh` registers a cron job: `every 60s`.
3. Every 60s, Hermes spawns a fresh session that reads `skills/orchestrate.md`
   and calls the tools registered by `plugins/productfactory/`.
4. The session decides which product + persona to run, calls `launch_session`
   which wraps `orchestrator/docker_runner.py:run_claude_in_docker`, waits for
   the agent container to exit, and then exits itself.

All deterministic mechanics (feature claiming, git reset, live session-result
polling, post-coder QA/security chain) still live in the existing Python
modules — Hermes just replaces the outer `while True:` loop.

## Layout

```
deploy/hermes/
├── README.md                              ← this file
├── config.yaml                            ← Hermes runtime config (LLM, plugins)
├── bootstrap.sh                           ← ENTRYPOINT: register cron + run gateway
├── skills/
│   └── orchestrate.md                     ← The "brain" — poll cycle as a skill
└── plugins/
    └── productfactory/
        ├── __init__.py
        ├── tools.py                       ← Tool handlers (wrap orchestrator/*)
        └── toolsets.py                    ← registry.register(...) per tool
```

## First-time setup

```bash
# 1. Build the Hermes image (also builds the agent image if not cached)
bash deploy/docker/build.sh --hermes

# 2. Set ANTHROPIC_API_KEY + PRODUCTS_BASE_DIR in .env (see .env.example)

# 3. Start Hermes alongside pm-api + postgres
docker compose --profile hermes up -d

# 4. Watch it run
docker logs -f pf-hermes
```

## Expected log pattern (first 60s)

```
[bootstrap] Registering orchestration cron job...
[bootstrap] Acquiring poller lock...
{hermes gateway boot messages}
{cron fires once} → tool: poller_heartbeat → 200
                 → tool: check_stale_sessions
                 → tool: reset_stuck_features
                 → tool: get_products
                 → tool: pm_api GET /api/features/next-for-persona?persona=reviewer
                 → tool: pm_api GET /api/products/next
                 → decision: persona=coder, product=Calculator
                 → tool: github_list_prs (PR gate)
                 → tool: launch_session (blocks ~2–30 min)
                 → cycle exits
```

## Invariants preserved from the Python poller

- DB distributed lock (PM API `/api/poller/lock` + heartbeat)
- Single agent container per cycle
- Stuck feature rollback on zero-progress exit (lives in `docker_runner.py`)
- Post-coder chain: qa_tester → security_auditor → recommender

## Rollback

Hermes is gated behind the `hermes` compose profile. To return to the Windows
poller: `docker compose stop hermes` and restart `start_poller.ps1`.
