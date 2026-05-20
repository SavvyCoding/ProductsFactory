"""
Path B post-session pipeline for the on-demand maintenance personas
(documenter, analytics, recommender, devops, refactorer).

Same delegation as post_coder / post_doc: agents are responsible for *editing
files*; this pipeline owns the git ceremony (`add`, `commit`, `push` to
`origin/main`). Before this lived, the maintenance prompts asked the agent
to run `git commit && git push` themselves — and Ollama models (kimi-k2.6
in particular) routinely called `task_done` after writing the files but
*before* running the git block, leaving the work in the local working tree
where the next `_cleanup_workspace_post_session` reset would discard it.
Real incident: documenter session 2316 (e72365da, 2026-05-12) — kimi spent
24.7 min and 524k tokens producing a high-quality README/ARCHITECTURE
rewrite, then ended via task_done without pushing; the changes were
silently dropped on the next workspace reset.

Always pushes to `origin/main`. These personas are sprint-independent —
they document, analyse, or audit the *whole product* — so there is no
sprint_branch to target. No feature DB writes either (the agent already
creates Pending features via the PM API where applicable; see
recommender/devops/refactorer prompts).

Idempotent: if `git status --porcelain` shows no changes (which is the
expected steady-state for recommender/devops/refactorer, which only POST
to the PM API and don't touch the file tree), the pipeline returns early.
On any push failure the local commit is left in place; the next
`_cleanup_workspace_post_session` discards it, and the next on-demand run
redoes the work.
"""

import logging
import os
import subprocess as _sp

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


# Per-persona write allowlists for maintenance commits. Same shape as
# post_doc's allowlist but persona-keyed because architect needs to write
# ARCHITECTURE.md (the whole point of putting it on this persona is so the
# document stays current) while every other maintenance persona must not.
# Paths are fnmatch globs against forward-slashed relative paths.
#
# Architect specifically gains write access to ARCHITECTURE.md as the *only*
# persona authorized to maintain it (designer is blocked by post_doc's
# allowlist; coders write through post_coder which is feature-scoped).
# Architect's prompt scopes WHAT it may edit inside ARCHITECTURE.md (MODULES
# and DEPRECATED rows only -- no section rewrites); this allowlist enforces
# the PATH boundary, the prompt enforces the in-file scope.
_MAINTENANCE_ALLOWLISTS: dict[str, tuple[str, ...]] = {
    "architect": (
        "ARCHITECTURE.md",
        "docs/architecture_review_*.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "documenter": (
        "README.md",
        "docs/*.md",
        "docs/**/*.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "analytics": (
        "docs/analytics_*.md",
        "docs/*_analytics.md",
        "product_memory.md",
        "session_summary.md",
    ),
    "refactorer": (
        # Refactorer needs broad source-code access by definition.
        # Locking it down properly needs a separate review pass; for now
        # keep the existing behaviour (no enforcement) by allowing the
        # universal pattern. Future work: lock to specific dirs per stack.
        "*",
        "**/*",
    ),
    "devops": (
        ".github/**/*",
        "Dockerfile",
        "docker-compose.yml",
        "Makefile",
        "scripts/*",
        "product_memory.md",
        "session_summary.md",
    ),
    "recommender": (
        # Recommender only POSTs to the PM API (creates Pending features);
        # it doesn't write files. Keep the bookkeeping appends allowed in
        # case the prompt is later expanded.
        "product_memory.md",
        "session_summary.md",
    ),
    "product_trainer": (
        "docs/*.md",
        "product_memory.md",
        "session_summary.md",
    ),
}


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    import fnmatch
    normalized = path.replace("\\", "/")
    for pat in patterns:
        if fnmatch.fnmatchcase(normalized, pat):
            return True
    return False


def _post_maintenance_allowlist_check(
    persona: str, working_dir: str, _run, product_name: str = "?",
) -> list[str]:
    """Refuse a maintenance commit that touches paths outside the persona's
    allowed write set.

    Returns a list of violation strings; empty list = clean (commit may proceed).

    The architect is currently the only persona that may modify ARCHITECTURE.md
    -- this is what fixes the post-2026-05-20 gap where designer was locked
    out of ARCHITECTURE.md edits (post_doc allowlist) but no other persona
    was authorized to update it either. If a new maintenance persona later
    needs ARCHITECTURE.md write access, add it to _MAINTENANCE_ALLOWLISTS.

    Personas not listed in _MAINTENANCE_ALLOWLISTS are treated as "unknown" --
    we don't second-guess; the check passes them through (logged as info).
    This matches the existing failure mode where unknown personas just run
    without an allowlist.
    """
    allowlist = _MAINTENANCE_ALLOWLISTS.get(persona)
    if allowlist is None:
        log.info(
            f"[post-{persona}] {product_name}: no allowlist defined "
            f"for persona; commit passes through unfiltered"
        )
        return []

    try:
        diff_r = _run(["git", "diff", "--cached", "--name-status"], timeout=15)
    except Exception:
        return []
    if diff_r.returncode != 0:
        return []

    violations: list[str] = []
    for raw in (diff_r.stdout or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split("\t")
        status_raw = parts[0]
        if not status_raw:
            continue
        status = status_raw[0]
        path = parts[-1].strip()
        if not path:
            continue
        if _path_matches(path, allowlist):
            continue
        verb = {
            "M": "modify", "D": "delete", "R": "rename",
            "C": "copy", "T": "change type of", "A": "add",
        }.get(status, status)
        violations.append(
            f"Persona `{persona}` attempts to {verb} `{path}` -- "
            f"outside its maintenance allowlist. Permitted patterns: "
            f"{', '.join('`' + p + '`' for p in allowlist)}."
        )
    return violations


def _run_post_maintenance_pipeline(product: dict, session_uid: str,
                                    working_dir: str, persona: str) -> None:
    pname = product.get("name", "?")

    _chmod_workspace_via_alpine(working_dir, pname)

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True,
                       timeout=timeout, **kw)

    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and "/Temp/" not in ln and "/Results/" not in ln]
    if not changed:
        log.info(f"[post-{persona}] {pname}: no changes to commit")
        return
    log.info(f"[post-{persona}] {pname}: {len(changed)} changed file(s) — sample {changed[:3]}")

    # Sync local main against origin without losing the agent's uncommitted
    # writes. Same stash-then-checkout-then-pop pattern as post_doc, with
    # conflict resolution favouring the agent's content (their writes are
    # the new docs / analytics report; origin/main is the baseline).
    from orchestrator.integrations.git_ops import git_fetch_authenticated
    git_fetch_authenticated(["origin"], cwd=working_dir, product_name=pname, timeout=120)
    stash_r = _run(["git", "stash", "push", "-u", "-m",
                    f"post-{persona}-{session_uid}"], timeout=300)
    stashed = (stash_r.returncode == 0
               and "No local changes to save" not in (stash_r.stdout or ""))
    co = _run(["git", "checkout", "-B", "main", "origin/main"])
    if co.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git checkout -B main origin/main "
                    f"failed — rc={co.returncode} {co.stderr.strip()[:200]}")
        if stashed:
            _run(["git", "stash", "pop"])  # best-effort restore
        return
    if stashed:
        pop_r = _run(["git", "stash", "pop"])
        if pop_r.returncode != 0:
            conflicts = _run(["git", "diff", "--name-only", "--diff-filter=U"])
            paths = [p for p in conflicts.stdout.splitlines() if p.strip()]
            if paths:
                _run(["git", "checkout", "--theirs", "--"] + paths)
                _run(["git", "add", "--"] + paths)
                log.info(f"[post-{persona}] {pname}: resolved {len(paths)} "
                         f"stash-pop conflict(s) in favor of agent")
            _run(["git", "stash", "drop"])

    add_r = _run(["git", "add", "-A"])
    if add_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git add failed — "
                    f"{add_r.stderr.strip()[:200]}")
        return

    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        log.info(f"[post-{persona}] {pname}: nothing staged after add — done")
        return

    # Persona-aware allowlist check. Refuse the commit if the agent went
    # off-script and touched files outside its maintenance scope. Critically,
    # this is the ONLY enforcement point that lets architect write
    # ARCHITECTURE.md while keeping every other persona out of it.
    violations = _post_maintenance_allowlist_check(persona, working_dir, _run, pname)
    if violations:
        log.warning(
            f"[post-{persona}] {pname}: allowlist refused commit "
            f"({len(violations)} out-of-scope path(s)):"
        )
        for v in violations:
            log.warning(f"  - {v}")
        log.warning(
            f"[post-{persona}] {pname}: maintenance commit discarded; "
            f"the local edits will be cleaned by the next workspace reset"
        )
        return

    # --no-verify: pre-commit hooks (husky, lint-staged) installed by the
    # agent commonly fail outside the agent container because their binaries
    # aren't on the orchestrator's PATH. The maintenance commit is mechanical
    # docs/report content; lint hooks add no value here.
    commit_msg = f"{persona}: maintenance update [{persona}-{session_uid}]"
    commit_r = _run(["git", "commit", "--no-verify", "-m", commit_msg])
    if commit_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git commit failed — "
                    f"{commit_r.stderr.strip()[:200]}")
        return

    from orchestrator.integrations.git_ops import git_push_authenticated
    push_r = git_push_authenticated(
        ["--no-verify", "origin", "main"],
        cwd=working_dir, product_name=pname, timeout=180,
    )
    if push_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git push origin main failed — "
                    f"{push_r.stderr.strip()[:200]}")
        return

    log.info(f"[post-{persona}] {pname}: pushed maintenance commit to origin/main")
