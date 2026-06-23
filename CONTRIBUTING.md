# Contributing to ProductsFactory

Thanks for your interest in contributing! This guide covers how to set up a dev
environment, the conventions this codebase follows, and how to get a change merged.

By participating, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- For anything beyond a small fix, **open an issue first** to discuss the approach.
- This is an automation system that spends compute and pushes code. Test changes
  against repos and databases you own — never point it at anything you can't afford
  to have modified.

## Development setup

Requirements: **Python 3.12**, **Docker** + Docker Compose, and a PostgreSQL
instance (the compose file provides one).

```bash
# 1. Fork and clone
git clone https://github.com/<you>/ProductsFactory.git
cd ProductsFactory

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env        # set POSTGRES_PASSWORD, PM_PASSWORD, etc.

# 4. Start Postgres + the PM website
docker compose up -d
alembic upgrade head

# 5. Run the website locally (optional)
uvicorn website.main:app --host 0.0.0.0 --port 8080
```

## Running tests

Tests run against a **real PostgreSQL database** — each test inside a rolled-back
transaction. There are no mocks for the DB.

> 🛑 **`TEST_DATABASE_URL` is required and its database name MUST contain `test`.**
> There is no fallback to `DATABASE_URL`; the test harness aborts on an unsafe name.
> This guard exists because a missing guard once wiped a production database.

```bash
# One-time test DB setup
docker exec <postgres-container> createdb -U productfactory productfactory_test
DATABASE_URL=postgresql://...:.../productfactory_test alembic upgrade head

# Full suite
TEST_DATABASE_URL=postgresql://productfactory:PASSWORD@localhost:5432/productfactory_test \
  pytest tests/ -v

# A single file / test
pytest tests/test_website.py -v
pytest tests/test_website.py::test_list_products -v

# If you touched anything under orchestrator/prompts/, also run the prompt evals
pytest evals/ -v
```

CI runs the same suite on every push/PR against `master`/`main`. **"Green locally"
is not always "green in CI"** — CI also catches Windows-vs-Linux path/encoding
regressions.

## Conventions

These are enforced by convention (there is no auto-formatter configured), so please
match the surrounding code:

- **Async-first in `website/`** — use `async def` + `await` for all DB and HTTP calls.
- **No hardcoded config** — everything comes from environment variables; `.env.example`
  is the canonical reference. Add new vars there.
- **Schema changes require a migration** — add the column to `website/models.py` **and**
  create a new `db/migrations/versions/NNN_*.py`. Alembic does not auto-generate these.
- **Route ordering matters** — in `website/main.py`, parameterized routes
  (`/api/features/{id}`) must be defined *after* literal routes at the same prefix,
  or they shadow them.
- **Feature priority is ASC** — lower number = higher rank. Sort `priority ASC, id ASC`.
- **Idempotent setup** — discovery/template installation must be safe to re-run.

When in doubt, read [`CLAUDE.md`](CLAUDE.md) — it documents the architecture and the
non-obvious invariants in depth, and [`orchestrator/INVARIANTS.md`](orchestrator/INVARIANTS.md)
for the orchestrator's behavioral contract.

## Submitting a pull request

1. Create a branch off `master` (e.g. `fix/...`, `feat/...`, `docs/...`).
2. Keep the change focused; prefer the cross-cutting structural fix over a one-off patch.
3. Add or update tests. If you fix a bug, add a test that would have caught it.
4. Make sure `pytest tests/ -v` passes (plus `evals/` if you touched prompts).
5. Write a clear PR description: what changed, why, and which behavior/invariant it touches.
6. **Sign off your commits** (see below) and be responsive to review feedback.

## Sign-off (Developer Certificate of Origin)

This project uses the [Developer Certificate of Origin](https://developercertificate.org/)
(DCO) instead of a CLA. By signing off on a commit you certify that you wrote the
change (or otherwise have the right to submit it) and that it may be distributed
under this project's [Apache 2.0 license](LICENSE).

Add a sign-off line to every commit:

```bash
git commit -s -m "your message"
```

This appends a trailer using your `git` name and email:

```
Signed-off-by: Your Name <your.email@example.com>
```

Use a real name and a reachable email. To sign off a branch you already committed,
run `git rebase --signoff <base>`. PRs whose commits are not signed off may be asked
to amend before merge.

## Reporting bugs and requesting features

Use the GitHub issue templates. For **security vulnerabilities**, do **not** open a
public issue — follow [`SECURITY.md`](SECURITY.md) instead.
