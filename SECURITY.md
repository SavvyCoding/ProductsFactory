# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, report them privately via one of:

- GitHub's [private vulnerability reporting](https://github.com/SavvyCoding/ProductsFactory/security/advisories/new)
  (Security → Report a vulnerability), or
- Email the maintainer: **digvijay.s.parmar@gmail.com**

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (proof-of-concept if possible)
- The affected component (orchestrator, PM website, agent image, …) and version/commit

You can expect an initial acknowledgement within a few days. We'll keep you updated
on remediation progress and coordinate disclosure timing with you.

## Scope

ProductsFactory executes LLM-generated code and automates git operations, so its
security posture matters. In-scope concerns include, but are not limited to:

- **Agent sandbox escape** — anything that lets an agent session break out of its
  container, reach the host, or gain the docker socket / elevated capabilities.
- **Auth bypass** on the PM website or its REST API.
- **Secret exposure** — credentials leaking into logs, PRs, prompts, or the database
  (the redaction layer is `orchestrator/infra/redaction.py`).
- **Privilege escalation** via the GitHub App token flow or git auth.
- **Quality-gate / read-only-mount bypass** that lets an agent modify protected,
  PM-curated files.

## Design controls (context for reporters)

- Agent containers run **non-root** with `--cap-drop ALL`, no `--privileged`, no host
  network, and **no docker socket**.
- PM-curated files are mounted **read-only** at the container level.
- Git auth uses **short-lived GitHub App installation tokens** (no long-lived PATs).
- Secrets are redacted from session logs.

## Operator responsibilities

If you deploy ProductsFactory, you are responsible for:

- Setting strong, unique values for `POSTGRES_PASSWORD`, `PM_PASSWORD`, and all
  credentials in `.env` (never commit `.env`).
- Pointing it only at repositories and databases you own.
- Keeping `TEST_DATABASE_URL` on a database whose name contains `test` (the suite
  refuses anything else — do not work around this guard).
- Reviewing the cost and rate-limit controls before running the orchestrator unattended.

## Supported versions

This is an experimental project; security fixes are applied to the latest `master`.
There are no long-term-support branches.
