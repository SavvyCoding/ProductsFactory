"""
Path B planner/designer post-session pipeline.

The agent only writes docs/ files and appends Designed entries to
session_result.json (the live-poll thread has already PATCHed the DB by the
time this runs). Here we commit those docs and push to the right branch:
sprint_branch when sprint_pr_mode is on, otherwise main. On any failure we
roll the affected features back to Approved + clear design_doc_path so the
next cycle re-plans them rather than coders running against missing docs.

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
    commit those docs and push to the sprint branch (sprint_pr_mode) or main
    (legacy). On push failure we PATCH the affected features back to Approved
    + clear design_doc_path so the next cycle re-plans them rather than
    coders running against missing docs.
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

    # Sync and commit to the right branch:
    #   - sprint_pr_mode on  → commit to sprint_branch (the unit of merge for
    #     the sprint PR). Coder runs on sprint_branch and must be able to read
    #     the design docs from its working tree.
    #   - sprint_pr_mode off → commit to main (legacy per-feature-PR flow).
    #
    # The agent's tracked changes (`features.md`) and untracked writes
    # (`docs/story_NNN.md`, `session_result_NNN.json`) would block a plain
    # `git checkout TARGET` with "your local changes would be overwritten"
    # / "untracked files would be overwritten".
    # Real incident 2026-05-06 18:42: post-product_planner aborted at the
    # checkout for feature 164, _rollback_doc_features kicked the feature
    # back to Approved, next cycle re-ran the planner, exact same failure
    # — infinite loop, no story file ever made it to origin/main.
    # Real incident 2026-05-07 03:30: docs landed on origin/main (target
    # was hardcoded "main") but coder runs on sprint_branch, branched from
    # origin/main at provision time and never picked up subsequent main-side
    # docs. Result: every coder session saw an empty docs/, found no story
    # for its assigned feature, stranded with no progress. Fix: target
    # sprint_branch when sprint_pr_mode is on.
    # Pattern mirrors post_coder: stash -u (covers both tracked + untracked,
    # respects .gitignore so node_modules stays out), then checkout -B
    # to force-reset local TARGET against origin, then stash pop with
    # conflict resolution favoring the agent's content.
    sprint_pr_mode = bool(product.get("_sprint_pr_mode"))
    sprint_branch = product.get("_sprint_branch") or ""
    target_branch = sprint_branch if (sprint_pr_mode and sprint_branch) else "main"

    _run(["git", "fetch", "origin"])
    stash_r = _run(["git", "stash", "push", "-u", "-m",
                    f"post-{persona}-{session_uid}"], timeout=300)
    stashed = (stash_r.returncode == 0
               and "No local changes to save" not in (stash_r.stdout or ""))
    co = _run(["git", "checkout", "-B", target_branch, f"origin/{target_branch}"])
    if co.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git checkout -B {target_branch} origin/{target_branch} failed — rc={co.returncode} {co.stderr.strip()[:200]}")
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
    # product_planner merged into designer 2026-05-06 — both produce per-
    # feature design docs, single verb is fine.
    commit_msg = f"design: {feat_summary} [{persona}-{session_uid}]"
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

    push_r = _run(["git", "push", "--no-verify", "origin", target_branch], timeout=180)
    if push_r.returncode != 0:
        log.warning(f"[post-{persona}] {pname}: git push origin {target_branch} failed — {push_r.stderr.strip()[:200]}")
        # Local commit exists; the next _reset_workspace will discard it.
        # Roll back DB so the next planner cycle re-plans these features.
        _rollback_doc_features(product, assigned_features)
        return

    log.info(f"[post-{persona}] {pname}: pushed {len(assigned_features)} doc(s) to origin/{target_branch}")

    # Mark each assigned feature Designed + link to its story file directly.
    # Mirrors post_coder's direct-PATCH-to-Reviewing fix (commit df898e8) for
    # the planner/designer pipeline. Until 2026-05-06 this step relied on
    # the agent appending `{"status": "Designed", "design_doc_path": ...}`
    # to session_result.json; agents that exited via task_done without
    # writing that line left the feature stuck Approved while the file was
    # already on origin/main. Result: the orchestrator launched another
    # planner next cycle, which created a second story file with the same
    # name (idempotent on disk but wasted tokens), pushed again, looped.
    # Real loop observed: DigitalSign feature 164, sessions 1963→1964→1966,
    # ~3 planner sessions back-to-back to push the same docs/story_164.md.
    # changed_by="post-doc:fallback" mirrors the post-coder bypass label.
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                fid = f["id"]
                # Convention: agents write docs/story_{id}.md. If the agent
                # used a different filename we don't try to discover it —
                # the next coder run will read whatever's at the conventional
                # path; mismatch is logged via post-coder's "no design doc"
                # warning rather than corrupting state here.
                doc_path = f.get("design_doc_path") or f"docs/story_{fid}.md"
                try:
                    r = client.patch(f"/api/features/{fid}", json={
                        "status": "Designed",
                        "design_doc_path": doc_path,
                        "changed_by": "post-doc:fallback",
                    })
                    r.raise_for_status()
                    log.info(
                        f"[post-{persona}] {pname}: feature #{fid} → Designed "
                        f"(doc={doc_path})"
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: direct PATCH for #{fid} failed: {e}"
                    )
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: PM client error during designed PATCH: {e}")


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
