# ProductsFactory

**A 24/7 autonomous software-development system.** ProductsFactory orchestrates Claude Code agents inside isolated Docker containers to design, implement, review, and ship features across multiple product repositories — supervised by a FastAPI project-management dashboard.

It runs a continuous cycle loop: pick a product, choose a persona (designer, coder, reviewer, …), launch a sandboxed agent session, run deterministic quality gates on the result, open a pull request, review it, and auto-merge — with a layer of rule-based supervisors that detect and recover from cascades (stuck features, review ping-pong, false success, environment failures).

> ⚠️ **Status: experimental / research.** This is an ambitious automation system that spends real compute and pushes real code to real repositories. Run it against repos you own, in a sandbox, with the cost and safety controls described below. Read [`SECURITY.md`](SECURITY.md) before exposing it to anything you care about.

---

## How it works

```
              ┌─────────────────────────────────────────────┐
              │  Orchestrator (pf-orchestrator container)    │
              │  cycle loop ~every 60s:                      │
              │   0. trainer / on-demand persona             │
              │   1. reviewer preempt                        │
              │   2. round-robin product → launch session    │
              └───────────────┬─────────────────────────────┘
                              │ launches one sandboxed agent per cycle
                              ▼
        ┌──────────────────────────────────────────────────┐
        │  Agent container (non-root, no docker socket)     │
        │  Claude Code runs a persona session on a product  │
        │  → edits files, writes session_result.json        │
        └───────────────┬──────────────────────────────────┘
                        │ post-session pipeline (deterministic gates)
                        ▼
        lint guards · drift detectors · test run · verify recipes
                        │ pass → commit, push, open PR
                        ▼
        ┌──────────────────────────────────────────────────┐
        │  PM Website (FastAPI + Postgres)                  │
        │  products · features · phases · sessions · alerts │
        │  REST API + dashboard the agents and humans share │
        └──────────────────────────────────────────────────┘
```

### Three independent subsystems

| Subsystem | Path | Role |
|---|---|---|
| **Orchestrator** | `orchestrator/` + `deploy/orchestrator/` | Long-running cycle loop that selects products/personas, launches agent containers, and runs the post-session quality pipeline. |
| **PM Website** | `website/` | FastAPI dashboard + REST API for managing products, features, phases, and sessions. Async SQLAlchemy over PostgreSQL. |
| **Agent Image** | `deploy/docker/` | The hardened Docker image each Claude agent session runs inside (non-root, capability-dropped, isolated network). |

### Key concepts

- **Personas** — each session runs as one role: `designer` (writes design docs), `coder` (implements a feature), `reviewer` (reviews the session PR), plus scheduled maintenance personas (`architect`, `documenter`, `analytics`, `refactorer`, …). Prompts live in `orchestrator/prompts/`.
- **One PR per feature** — every coder session opens its own PR straight to `main`; a reviewer session approves it; auto-merge squash-merges it. No long-lived integration branches.
- **Deterministic quality gates** — before any PR is opened, the result passes static lint guards, AST-based drift detectors, a real test run, and designer-authored verification recipes (all in `orchestrator/pipelines/post_coder.py`). The reviewer never sees the patterns these catch.
- **Supervisors** — rule-based detectors (`orchestrator/supervisor.py`) catch cascades (repeated review feedback, false success, stuck PRs) and route features to `Blocked` with an auditable reason instead of looping forever.
- **Phase gate** — an optional human-in-the-loop checkpoint that freezes later phases until a PM approves the current one.

For the full behavioral contract, see [`orchestrator/INVARIANTS.md`](orchestrator/INVARIANTS.md). For an architecture deep-dive aimed at contributors (and AI agents working in this repo), see [`CLAUDE.md`](CLAUDE.md).

---

## Quick start

### Prerequisites

- **Docker** + Docker Compose
- **Python 3.12**
- A **PostgreSQL** instance (the compose file provides one)
- An LLM backend — either:
  - **Claude** via the `claude` CLI (OAuth tokens mounted into agent containers), or
  - **Ollama** for a fully local, no-API-key path (`AGENT_BACKEND=ollama`)
- A **GitHub App** (for git push / PR automation) if you want the full ship-a-PR loop

### Run it

```bash
# 1. Clone and configure
git clone https://github.com/SavvyCoding/ProductsFactory.git
cd ProductsFactory
cp .env.example .env
#   → edit .env: set POSTGRES_PASSWORD, PM_PASSWORD, and your backend/credentials

# 2. Start Postgres + the PM website
docker compose up -d

# 3. Apply database migrations
alembic upgrade head

# 4. Open the dashboard
#   http://localhost:8080  (HTTP Basic auth: PM_USERNAME / PM_PASSWORD)

# 5. (Optional) start the autonomous orchestrator
docker compose --profile orchestrator up -d
```

To run the PM website locally without Docker:

```bash
pip install -r requirements.txt
uvicorn website.main:app --host 0.0.0.0 --port 8080
```

### Try one agent session without Docker or an API key

```bash
python scripts/test_run.py \
    --product-id 3 \
    --working-dir /path/to/a/product/repo \
    --feature "Add hello endpoint" \
    --desc "GET /hello returns {message: hello}" \
    --persona coder
```

This drives a single session through local Ollama, intercepting `git push` / `gh pr create` so nothing leaves your machine (pass `--allow-push` for real operations).

---

## Repository layout

```
orchestrator/        Cycle loop, personas, pipelines, drift detectors, supervisors
  cycle/             Per-product decision tree, phase gate, dependency gating
  pipelines/         Post-session gates (lint, test, push, PR, auto-merge)
  prompts/           Per-persona prompt templates
  session/           Session state machine + reconciliation
  integrations/      GitHub App auth, git ops, docker CLI
deploy/
  orchestrator/      The containerized cycle entry point (orchestrate.py, tools.py)
  docker/            Agent image Dockerfile + build scripts
website/             FastAPI dashboard + REST API + models
db/migrations/       Alembic migrations (source of truth for schema)
templates/           Files installed into each managed product repo on discovery
tests/               Pytest suite (real Postgres, per-test rollback)
evals/               Persona-prompt regression harness
scripts/             Operational + dev utilities
docs/specs/          Design specs
```

---

## Configuration

All configuration is environment-driven — see [`.env.example`](.env.example) for the complete, annotated list. The essentials:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `PM_USERNAME` / `PM_PASSWORD` | Dashboard HTTP Basic credentials |
| `AGENT_BACKEND` | `ollama` for local models; otherwise the Claude CLI path |
| `ANTHROPIC_API_KEY` | Optional — used for AI feature recommendations |
| `SESSION_TIMEOUT_MINUTES` | Hard cap on an agent session (default 90) |
| `MAX_FEATURES_PER_RUN` | Features attempted per session (default 1) |

Git automation uses a **GitHub App** (DB-stored, hot-reloadable credentials), not personal access tokens. See [`CLAUDE.md`](CLAUDE.md) → *Auth & Security*.

---

## Testing

Tests run against a **real PostgreSQL database** (each test in a rolled-back transaction), not mocks.

> 🛑 **`TEST_DATABASE_URL` is required and must contain `test` in the database name.** There is no fallback to `DATABASE_URL` — that fallback once wiped a production database.

```bash
# One-time test DB setup
docker exec <postgres-container> createdb -U productfactory productfactory_test
DATABASE_URL=postgresql://...:.../productfactory_test alembic upgrade head

# Run the suite
TEST_DATABASE_URL=postgresql://productfactory:PASSWORD@localhost:5432/productfactory_test \
  pytest tests/ -v

# Persona-prompt regression evals (run after editing orchestrator/prompts/)
pytest evals/ -v
```

---

## Security model

ProductsFactory runs untrusted-ish LLM output that edits code and touches git. The defenses, in brief:

- Agent containers run as a **non-root** user with `--cap-drop ALL`, **no `--privileged`**, no host network, and **no docker socket**.
- PM-curated files (e.g. `CLAUDE.md`, quality gates) are mounted **read-only** so agents physically cannot edit them.
- All git auth flows through short-lived **GitHub App installation tokens**; secrets are **redacted from logs** (`orchestrator/infra/redaction.py`).
- Deterministic gates and rule-based supervisors bound the blast radius of a misbehaving agent.

See [`SECURITY.md`](SECURITY.md) to report a vulnerability, and [`CLAUDE.md`](CLAUDE.md) → *Auth & Security* for the full picture.

---

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev setup, the test contract, and PR guidelines. Please also read the [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## License

Licensed under the [Apache License 2.0](LICENSE) — © 2026 Digvijay Parmar. See [NOTICE](NOTICE) for attribution.
