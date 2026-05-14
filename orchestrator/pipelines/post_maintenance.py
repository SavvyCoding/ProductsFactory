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
    _run(["git", "fetch", "origin"])
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
