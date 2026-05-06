"""
Path B planner/designer post-session pipeline.

The agent only writes docs/ files and appends Designed entries to
session_result.json (the live-poll thread has already PATCHed the DB by the
time this runs). Here we commit those docs and push to origin/main. On any
failure we roll the affected features back to Approved + clear
design_doc_path so the next cycle re-plans them rather than coders running
against missing docs.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import subprocess as _sp

import httpx

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _run_post_doc_pipeline(product: dict, session_uid: str, working_dir: str,
                            assigned_features: list[dict], persona: str) -> None:
    """
    Path B: orchestrator owns ALL git for planner/designer too. Agents only
    write docs/ files and append `Designed` entries to session_result.json
    (the live-poll has already PATCHed the DB by the time this runs). Here we
    commit those docs and push to origin/main. On push failure we PATCH the
    affected features back to Approved + clear design_doc_path so the next
    cycle re-plans them rather than coders running against missing docs.
    """
    pname = product.get("name", "?")
    if not assigned_features:
        log.info(f"[post-{persona}] {pname}: no assigned features — skipping commit")
        return

    _chmod_workspace_via_alpine(working_dir, pname)

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout, **kw)

    # Detect docs changes (and session_result.json — we want the agent's
    # Designed/Blocked record committed alongside its output).
    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and "/Temp/" not in ln and "/Results/" not in ln]
    if not changed:
        log.warning(f"[post-{persona}] {pname}: agent exited 0 with no doc changes — rolling back assigned features")
        _rollback_doc_features(product, assigned_features)
        return
    log.info(f"[post-{persona}] {pname}: {len(changed)} changed file(s) — sample {changed[:3]}")

    # Sync to origin/main first; planner/designer always commit on main.
    # In sprint_pr_mode the orchestrator pre-checks-out sprint/79 for every
    # persona at session start (so agents that forget MANDATORY-FIRST-ACTION
    # still land on the right branch). For planner/designer that's wrong —
    # docs must commit to main. The agent's tracked changes (`features.md`)
    # and untracked writes (`docs/story_NNN.md`, `session_result_NNN.json`)
    # would block `git checkout main` with "your local changes would be
    # overwritten" / "untracked files would be overwritten".
    # Real incident 2026-05-06 18:42: post-product_planner aborted at the
    # checkout for feature 164, _rollback_doc_features kicked the feature
    # back to Approved, next cycle re-ran the planner, exact same failure
    # — infinite loop, no story file ever made it to origin/main.
    # Fix mirrors post_coder: stash -u (covers both tracked + untracked,
    # respects .gitignore so node_modules stays out), then checkout -B
    # to force-reset local main against origin, then stash pop with
    # conflict resolution favoring the agent's content.
    _run(["git", "fetch", "origin"])
    stash_r = _run(["git", "stash", "push", "-u", "-m",
                    f"post-{persona}-{session_uid}"], timeout=300)
    stashed = (stash_r.returncode == 0
               and "No local changes to save" not in (stash_r.stdout or ""))
    co = _run(["git", "checkout", "-B", "main", "origin/main"])
    if co.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git checkout -B main origin/main failed — rc={co.returncode} {co.stderr.strip()[:200]}")
        if stashed:
            _run(["git", "stash", "pop"])  # best-effort restore
        _rollback_doc_features(product, assigned_features)
        return
    if stashed:
        pop_r = _run(["git", "stash", "pop"])
        if pop_r.returncode != 0:
            # Conflict — resolve in favor of agent's content (their writes
            # are the new design; main is the empty baseline).
            conflicts = _run(["git", "diff", "--name-only", "--diff-filter=U"])
            paths = [p for p in conflicts.stdout.splitlines() if p.strip()]
            if paths:
                _run(["git", "checkout", "--theirs", "--"] + paths)
                _run(["git", "add", "--"] + paths)
                log.info(f"[post-{persona}] {pname}: resolved {len(paths)} stash-pop conflict(s) in favor of agent")
            _run(["git", "stash", "drop"])

    add_r = _run(["git", "add", "-A"])
    if add_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git add failed — {add_r.stderr.strip()[:200]}")
        _rollback_doc_features(product, assigned_features)
        return

    # Skip commit if nothing actually staged (untracked Temp/Results filtered above).
    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        log.warning(f"[post-{persona}] {pname}: nothing staged after add — rolling back")
        _rollback_doc_features(product, assigned_features)
        return

    feat_summary = ", ".join(f"#{f['id']}" for f in assigned_features)
    verb = "plan" if persona == "product_planner" else "design"
    commit_msg = f"{verb}: {feat_summary} [{persona}-{session_uid}]"
    # --no-verify: same rationale as post_coder commit. The post-doc
    # pipeline runs outside the agent's environment; agent-installed
    # pre-commit hooks (husky, lint-staged) routinely fail because their
    # binaries aren't on PATH. Hooks add no value for a deterministic
    # docs commit anyway.
    commit_r = _run(["git", "commit", "--no-verify", "-m", commit_msg])
    if commit_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git commit failed — {commit_r.stderr.strip()[:200]}")
        _rollback_doc_features(product, assigned_features)
        return

    push_r = _run(["git", "push", "--no-verify", "origin", "main"], timeout=180)
    if push_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git push failed — {push_r.stderr.strip()[:200]}")
        # Local commit exists; the next _reset_workspace will discard it.
        # Roll back DB so the next planner cycle re-plans these features.
        _rollback_doc_features(product, assigned_features)
        return

    log.info(f"[post-{persona}] {pname}: pushed {len(assigned_features)} doc(s) to origin/main")


def _rollback_doc_features(product: dict, assigned_features: list[dict]) -> None:
    """
    When the post-doc push fails, roll affected features back so the next cycle
    re-runs the planner/designer rather than coders running against docs that
    aren't on origin. Uses changed_by='pm' to bypass the rank-downgrade guard.
    """
    pname = product.get("name", "?")
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                fid = f["id"]
                try:
                    client.patch(f"/api/features/{fid}", json={
                        "status": "Approved",
                        "design_doc_path": None,
                        "changed_by": "post-doc:rollback",
                    })
                    log.info(f"[post-doc-rollback] {pname}: feature #{fid} → Approved (push failed)")
                except Exception as e:
                    log.warning(f"[post-doc-rollback] {pname}: feature #{fid} rollback failed: {e}")
    except Exception as e:
        log.warning(f"[post-doc-rollback] {pname}: PM client error: {e}")
