# Spec: Agent Environment Provisioning Strategy

**Status:** Future consideration — **plan only, not implemented.**
**Date:** 2026-06-09
**Supersedes:** the earlier per-product-image drafts discussed in-session (v1 builder, v2 staged, "phase-0.5 predictive extraction"). Those over-engineered a divergence problem that has **not been observed**; this document is the rethought, evidence-driven strategy.

---

## Problem

AI-built products occasionally need **system binaries** (apt-level: psql, chromium, pandoc, tesseract, …) or **language packages** the agent environment lacks, causing the coder to Block or gates to fail. How should the factory provision environments — without manual per-product Dockerfile surgery — while staying simple and evidence-driven?

## Core strategy (one line)

**Grow one shared base; diverge only on genuine conflict; when you must diverge, the product owns its environment-as-code.** Build provisioning automation only when *evidence* (not speculation) shows it's needed.

## Principles

1. **Environment is a function of code** → declare it as code in the repo and build from the declaration.
2. **The proven model is language-deps-as-code** (`requirements.txt` / `package.json` / `go.mod`, installed at container start). *Extend* that model; don't invent a parallel one.
3. **Additive need ≠ conflicting need.** Additions (a product wants psql) are satisfied by *one growing shared base* — cheap, shared, no divergence. Only a genuine **conflict** (product A needs lib X v1, product B needs v2; incompatible system libs) forces per-product divergence.
4. **No provisioning automation ahead of evidence.** As of 2026-06-09, *zero* observed cases of a product needing a binary that doesn't belong in the shared base. Every real failure was a shared-base or prompt-convention bug (pyenv-rehash false `env_broken`, missing-common-tool Blocks, `npx not found`), all fixed at the base/prompt/orchestrator level.
5. **Fail safe** — never silently run a session missing a *declared* tool.

## The model (tiers)

| Need | Mechanism | Owner | Status |
|---|---|---|---|
| **Language deps** (pip/npm/go) | declared in repo, installed at container start | product repo | ✅ DONE — the proven model |
| **System binaries, additive** | grow the **single shared base image** (curated; the common set is small and finite) | factory base Dockerfile | ✅ base current (chromium, psql, sqlite3, redis-cli, graphviz, pandoc, poppler, wkhtmltopdf, tesseract, black/flake8/mypy, ffmpeg, …); +1 edit & rebuild when a new common tool appears |
| **System binaries, *conflicting*** (rare) | **product owns its environment-as-code** — a product `Dockerfile`/`Aptfile` the agent maintains, built per-product (the **escape hatch**) | product repo | 🔲 NOT built — only when a real conflict is observed |
| **Demand signal** | the coder's existing **Block-with-reason** (`"runtime tool X unavailable in agent image"`) | existing | ✅ exists; optionally surface a count on the dashboard |

## Decision gates

- **Default (additive):** a needed system binary → add to the **shared base** (operator edit + one rebuild; serves all products). This handles ~everything.
- **Divergence gate (build the escape hatch ONLY when):** two or more products need **conflicting** system environments (incompatible versions/libs), demonstrated by real data — **not** mere "needs an extra tool." Adding a tool is not a conflict.

## What we are explicitly NOT building (and why)

- **Factory-layered per-product images** — carried real defects: `FROM …:latest` staleness, base-bump thundering-herd rebuilds, fallback-to-base silently masking a missing *declared* tool, TOCTOU build races, GC breaking running sessions, allowlist-as-a-human-bottleneck. Over-engineered for a divergence that doesn't exist.
- **Allowlist + hash-cache + per-product GC machinery** — only meaningful with per-product builds.
- **Predictive LLM dependency extraction at the phase gate** — appealing, but premature: the Block-with-reason already signals demand, and the prediction is a heads-up (can't see transitive libs/fonts or designer omissions), not a guarantee.
- **Runtime `apt` in the agent container** — the non-root, `--cap-drop ALL` container can't, and per-session apt is slow.
- **A dedicated build worker/queue** — premature for a single-host factory.

## Escape-hatch design (sketch — implement ONLY when the divergence gate trips)

If genuine conflict is observed, implement **"product owns its environment"** (environment-as-code extended to system deps), preferred over factory-layered images because it matches standard devcontainer practice and keeps the environment authored where the code lives:

- The product repo carries an environment declaration (a `Dockerfile` or an `Aptfile` alongside the existing `requirements*.txt`).
- The **agent maintains it** — adds system deps as features need them, exactly as it maintains `requirements.txt`.
- The system builds the product's declared image with the v2 correctness fixes applied **here only**:
  - **digest-pinned base** (`FROM …@sha256:…`, not `:latest`) + a throttled base-bump→rebuild policy;
  - **defer-don't-fallback** (a manifest'd product whose image isn't warm skips the cycle, never runs on base missing a declared tool);
  - **lock wraps `image_exists()`→`build()`** (no TOCTOU);
  - **GC-safe pruning** (only when no container references the tag);
  - **package-name validation** (`^[a-z0-9][a-z0-9.+-]*$`) before it enters the generated Dockerfile;
  - **out-of-band build** via the host daemon socket (cycle loop never blocks), build logs captured + surfaced on failure;
  - **kill-switch** flag (documented: dependent products Block when off).
- **Security:** build-time root / runtime non-root (unchanged); allowlist + validation; accepted residual = allowlisted apt packages still run maintainer postinstall scripts as root at build and pull from upstream mirrors.

## Cheap thing worth doing now (optional, low risk)

Surface a **count of coder "runtime tool unavailable" Blocks** (by binary & product) on the dashboard — a few lines reading the existing `blocked_reason`, no new architecture — so the shared base stays ahead of demand. This is the proactive complement to the (already-existing) reactive Block signal.

## Net

Current state **suffices**: a generous shared base + language-deps-as-code + the Block-with-reason demand signal. The architecture decision (keep growing the base vs. product-owned environment-as-code) is **deferred until real evidence of conflict**, and when that arrives the answer is product-owned env, not factory-layered per-product images.

**Meta-lesson recorded:** don't build provisioning automation ahead of evidence; extend the model that already works (deps-as-code) rather than inventing parallel machinery.
