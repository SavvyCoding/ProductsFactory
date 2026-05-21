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


# Paths the designer agent is authorized to MODIFY (M / D / R / C / T in
# `git diff --cached --name-status`). Source of truth is the designer prompt
# (orchestrator/prompts/designer.md):
#
#   - "Write the design doc to /workspace/docs/story_{feature_id:03d}.md"
#   - "Append one JSON line to /workspace/session_result.json"
#   - "Append to /workspace/session_summary.md"
#   - "Append cross-session findings to /workspace/product_memory.md"
#   - "Do NOT write application code, tests, or fixtures."
#
# `features.md` is grandfathered for backward-compat with pre-2026 products
# (templates/renderer.py no longer creates it; DB is the source of truth).
# Glob patterns are matched with fnmatch.fnmatchcase against forward-slash paths.
_DESIGNER_MODIFY_ALLOWLIST = (
    "docs/story_*.md",
    "features.md",
    "product_memory.md",
    "session_summary.md",
    "session_result.json",
)

# Paths the designer commit may ADD as new files. Superset of the modify
# allowlist that also accepts the renderer's outputs — on a greenfield first
# designer session, setup_product.py invokes templates.renderer.install_templates
# just before the designer container starts, leaving those files untracked
# in the working tree; the post-doc pipeline's `git add -A` then legitimately
# stages them and we don't want to reject a first-session commit. Keep this
# list in sync with templates/renderer.py:install_templates.
_DESIGNER_ADD_ALLOWLIST = _DESIGNER_MODIFY_ALLOWLIST + (
    "AGENT_WORKFLOW.md",
    "CONTRIBUTING.md",
    "CLAUDE.md",
    "ARCHITECTURE.md",
    ".gitignore",
    "quality_gates.json",
    # `.gitkeep` markers planted by the renderer in the empty src/, tests/,
    # Results/, Temp/ dirs on greenfield products (see templates/renderer.py:
    # the loop around line 230). One pattern matches them anywhere in the
    # repo tree.
    "*/.gitkeep",
)


def _path_matches_allowlist(path: str, allowlist: tuple[str, ...]) -> bool:
    """fnmatch a forward-slashed path against a tuple of glob patterns."""
    import fnmatch
    normalized = path.replace("\\", "/")
    for pat in allowlist:
        if fnmatch.fnmatchcase(normalized, pat):
            return True
    return False


def _post_doc_allowlist_check(working_dir: str, _run, product_name: str = "?") -> list[str]:
    """Refuse a designer commit that touches paths outside the agent's scope.

    Returns a list of human-readable violation strings; empty list = clean.

    The designer prompt explicitly enumerates what the agent may write:
    docs/story_<id>.md, session_result.json, session_summary.md,
    product_memory.md. Everything else — ARCHITECTURE.md, CLAUDE.md, src/*,
    tests/*, requirements.txt, etc. — is outside the designer's contract.
    Real incident 2026-05-20: MyDocusign commit 5296af4 (designer-63e95f37
    for #608) modified ARCHITECTURE.md (-70 lines, stripping Phase-1
    sections) AND rewrote CLAUDE.md (+39/-39) alongside the legitimate
    docs/story_*.md output. Neither file is in the designer's prompt-level
    write set.

    The check inspects `git diff --cached --name-status` and refuses any
    M / D / R / C / T status on a path outside `_DESIGNER_MODIFY_ALLOWLIST`.
    A (newly-added) paths use the broader `_DESIGNER_ADD_ALLOWLIST` so that
    first-time designer commits on greenfield products — where the renderer
    just installed CLAUDE.md / ARCHITECTURE.md / etc. as untracked files
    that post-doc's `git add -A` legitimately stages — are not blocked.

    Idempotent and side-effect-free. Safe to call before commit.
    """
    violations: list[str] = []
    try:
        diff_r = _run(["git", "diff", "--cached", "--name-status"], timeout=15)
    except Exception:
        return violations
    if diff_r.returncode != 0:
        return violations

    for raw in (diff_r.stdout or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        # Format: "<STATUS>\t<path>" for M/A/D/T; "R<score>\t<old>\t<new>" for
        # renames; "C<score>\t<src>\t<dst>" for copies. The destination is
        # the LAST tab-separated field in every case.
        parts = line.split("\t")
        status_raw = parts[0]
        if not status_raw:
            continue
        status = status_raw[0]  # M, A, D, R, C, T, U (unmerged)
        path = parts[-1].strip()
        if not path:
            continue
        allowlist = (
            _DESIGNER_ADD_ALLOWLIST if status == "A"
            else _DESIGNER_MODIFY_ALLOWLIST
        )
        if _path_matches_allowlist(path, allowlist):
            continue
        verb = {
            "M": "modify",
            "D": "delete",
            "R": "rename",
            "C": "copy",
            "T": "change type of",
            "A": "add",
        }.get(status, status)
        violations.append(
            f"Designer commit attempts to {verb} `{path}` -- outside the "
            f"designer's authorized write set. Per orchestrator/prompts/"
            f"designer.md, designer sessions may only write "
            f"docs/story_<id>.md, session_result.json, session_summary.md, "
            f"product_memory.md (and grandfathered features.md). "
            f"ARCHITECTURE.md, CLAUDE.md, source code, and tests are out "
            f"of scope -- structural changes belong to the PM (via the "
            f"web UI) or the architect persona (filed as chore features), "
            f"not to the designer agent."
        )
    return violations


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

    # 1-PR model: designer commits design docs directly to the default
    # branch (main/master). There's no sprint integration branch any more.
    # The coder's post_coder pipeline picks up the docs because each
    # coder session cuts its session branch off the latest main tip, so
    # docs/story_NNN.md is always in the agent's working tree from turn 1.
    #
    # The agent's tracked changes (`features.md`) and untracked writes
    # (`docs/story_NNN.md`, `session_result_NNN.json`) would block a
    # plain `git checkout main` with "your local changes would be
    # overwritten" / "untracked files would be overwritten" — same hazard
    # the pattern below defends against. Stash -u (tracked + untracked,
    # respects .gitignore), checkout -B, stash pop with conflict-resolve
    # in agent's favor.
    _hb = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    target_branch = (_hb.stdout or "").strip() or "main"

    from orchestrator.integrations.git_ops import git_fetch_authenticated
    git_fetch_authenticated(["origin"], cwd=working_dir, product_name=pname, timeout=120)
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

    # Lint guard -- refuse the commit if it touches paths outside the
    # designer's authorized write set (per orchestrator/prompts/designer.md).
    # See _post_doc_allowlist_check for the rule and the MyDocusign #608
    # incident this catches.
    lint_violations = _post_doc_allowlist_check(working_dir, _run, pname)
    if lint_violations:
        log.warning(
            f"[post-{persona}] {pname}: lint-guard fired "
            f"({len(lint_violations)} out-of-scope path(s)) -- refusing "
            f"commit, rolling features back to Approved for next "
            f"designer cycle"
        )
        _bounce_doc_features_for_lint(
            product, assigned_features, persona, lint_violations,
        )
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

    from orchestrator.integrations.git_ops import git_push_authenticated
    push_r = git_push_authenticated(
        ["--no-verify", "origin", target_branch],
        cwd=working_dir, product_name=pname, timeout=180,
    )
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


def _bounce_doc_features_for_lint(
    product: dict,
    assigned_features: list[dict],
    persona: str,
    violations: list[str],
) -> None:
    """Post the lint-guard violation list as a feature comment, then roll the
    feature back to Approved with `design_doc_path` cleared.

    Mirrors post_coder.py's lint-guard bounce pattern. The comment is the
    feedback channel for the next designer cycle — the agent reads recent
    feature comments at session start, so explicit "you broke X" guidance
    makes the next attempt fix the actual issue rather than re-erring.

    Uses changed_by="post-doc:rollback" (already in the rank-guard bypass
    allowlist — see website/schemas.py PM_ALLOWED_TRANSITIONS / the
    rank_downgrade error message) so we don't need a new bypass label.
    The comment author is "lint-guard" to match the post_coder convention
    that surfaces these in the PM UI under a distinct author.
    """
    pname = product.get("name", "?")
    body = (
        f"❌ post-doc lint-guard auto-reject "
        f"({len(violations)} out-of-scope path(s) in the staged commit):\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\n\nThe orchestrator's deterministic post-doc lint guard caught "
          "these before the commit was pushed. The local edits have been "
          "discarded by the next workspace reset; this feature is back to "
          "Approved so the next designer cycle can re-attempt.\n\n"
          "Next designer cycle: write ONLY the files listed in your prompt "
          "-- `docs/story_<id>.md` for the design itself, plus appends to "
          "`session_result.json`, `session_summary.md`, and "
          "`product_memory.md` as documented. Do NOT modify "
          "`ARCHITECTURE.md`, `CLAUDE.md`, source code, tests, or any "
          "other repo file. If your story genuinely needs an architecture "
          "change, call that out IN the story doc under `## Anchored "
          "Patterns` and leave the architecture edit to the PM."
    )
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                fid = f["id"]
                try:
                    client.post(
                        f"/api/features/{fid}/comments",
                        json={"author": "lint-guard", "body": body},
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: lint-guard comment for "
                        f"#{fid} failed: {e}"
                    )
                try:
                    client.patch(
                        f"/api/features/{fid}",
                        json={
                            "status": "Approved",
                            "design_doc_path": None,
                            "changed_by": "post-doc:rollback",
                        },
                    )
                    log.info(
                        f"[post-{persona}] {pname}: feature #{fid} bounced by "
                        f"lint-guard ({len(violations)} issue(s))"
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: lint-guard PATCH for #{fid} "
                        f"failed: {e}"
                    )
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: lint-guard PM client error: {e}")
