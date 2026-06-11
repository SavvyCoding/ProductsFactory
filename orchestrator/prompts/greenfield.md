# RETIRED (2026-06-11) — all coder sessions use brownfield.md

This template is no longer reachable: `orchestrator/prompts/__init__.py`
routes every coder path to `brownfield.md` — the persona="coder" branch
since 2026-06-01, and the legacy persona=None type-based fall-through
since 2026-06-11.

Why retired rather than maintained: every coder-quality structural fix of
2026-05-30..06-11 (HARD STOP no-skips, {hard_rules} injection, rework
branch persistence, early-exit session_result contract, runtime-tool
declare-or-Block, PUBLIC_ROUTE annotation) shipped to brownfield.md only,
and this file's drifted copy caused the canonical 2026-06-01 DocumentSign
incident — 6 hours of 0 Pushed because coders read the stale template.
brownfield.md's "preserve existing code" guidance applies vacuously to an
empty repo, so greenfield products lose nothing.

Looking for the greenfield *scaffolding* path? That is
`orchestrator/greenfield_scaffold.py` (repo creation + templates), which
never used this prompt.
