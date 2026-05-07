# Contributing to {PRODUCT_NAME}

This repository is primarily maintained by an automated agent pipeline (ProductFactory). The git workflow here is **sprint-based**, not feature-branch-per-PR — keep that in mind when contributing.

## How work flows

Each sprint has:
- One long-lived branch named `sprint/<sprint_id>` (e.g. `sprint/105`).
- One pull request from that branch into `main`, opened when the sprint starts and squash-merged when the sprint completes.

Agents commit feature work directly onto the active `sprint/<id>` branch. When the sprint's Definition of Done passes, the entire sprint PR is squash-merged into `main` as a single deliverable.

## Human contributions

If you want to contribute by hand:

1. Find the active sprint branch:
   ```
   git fetch origin
   git branch -r | grep sprint/
   ```
2. Branch from the active sprint:
   ```
   git checkout -b your-name/<short-description> origin/sprint/<id>
   ```
3. Commit and push:
   ```
   git push origin your-name/<short-description>
   ```
4. Open a PR against `sprint/<id>` (NOT against `main`). It will ride along when the sprint PR squash-merges.

If no sprint is active, branch off `main` and PR to `main` directly.

**Do not fork.** The repo uses a deploy-key push model; PRs come from branches in this repo, not from forks.

## Tests, lint, audit

- Run the project's test command (see `CLAUDE.md` for the exact command and source/test paths).
- An automated reviewer agent inspects every commit on `sprint/<id>` against acceptance criteria, test coverage of the changed code, and security guards (no hardcoded secrets, no `error.message` returned to HTTP responses, no `.skip` / `.todo` tests).
- There is no externally enforced coverage threshold gate — write meaningful tests; the reviewer will flag obvious gaps.

## Commit messages

Use conventional-commit prefixes when natural (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`). Agent commits include a `[feature-<id>]` or `[<persona>-<session_uid>]` tag — humans are not required to add one.

## Secrets

Never commit secrets, credentials, or `.env` files. The reviewer agent will reject any commit that introduces them.

## Questions

Open an issue. Sprint scope, priorities, and acceptance criteria are tracked outside this repo by the ProductFactory dashboard; the GitHub issue tracker is for things that need human judgement (architecture pivots, ambiguous requirements, infra problems).
