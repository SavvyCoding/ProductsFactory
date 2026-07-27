"""
Path B planner/designer post-session pipeline.

The agent only writes docs/ files and appends Designed entries to
session_result.json (the live-poll thread has already PATCHed the DB by the
time this runs). Here we commit those docs and push to main. On any failure
we roll the affected features back to Approved + clear design_doc_path so
the next cycle re-plans them rather than coders running against missing
docs.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import subprocess as _sp
from pathlib import Path

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
    # The pre-commit deletion-safety helper, installed by the renderer
    # alongside the other templates and RO-mounted into the agent container
    # (see orchestrator/docker_runner._PM_CURATED_RO_FILES). Without this
    # the designer's first commit on a greenfield product fails post-doc
    # lint with `check_deletion_safety.py` flagged as out-of-scope, even
    # though the designer never touched it — surfaced by 2026-05-26
    # SmokeTest smoke test.
    "check_deletion_safety.py",
    # Renderer ships pytest.ini on the python stack (templates/renderer.py
    # step 3c2). Designer's first-session `git add -A` picks it up
    # legitimately. Without this entry, every python greenfield first
    # session bounced with "Designer commit attempts to add pytest.ini"
    # even though the renderer wrote it, not the designer (canonical
    # 2026-05-27 Calculator incident — three features blocked, the lint
    # message even cited the designer as the culprit).
    "pytest.ini",
    # Same story for requirements.txt — pre-seeded by the renderer (step
    # 3c3) with the test toolchain so Guard 18 (deps-coherence) doesn't
    # fire on `import pytest` from the first test file. Designer would
    # otherwise inherit it on first `git add -A` and bounce.
    "requirements.txt",
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


def _scan_post_doc_scope(working_dir: str, _run) -> list[dict]:
    """Core scope scanner: returns the out-of-scope staged entries as
    ``{"path", "status", "message"}`` dicts (empty list = clean).

    The designer prompt explicitly enumerates what the agent may write:
    docs/story_<id>.md, session_result.json, session_summary.md,
    product_memory.md. Everything else — ARCHITECTURE.md, CLAUDE.md, src/*,
    tests/*, requirements.txt, etc. — is outside the designer's contract.
    Real incident 2026-05-20: MyDocusign commit 5296af4 (designer-63e95f37
    for #608) modified ARCHITECTURE.md (-70 lines, stripping Phase-1
    sections) AND rewrote CLAUDE.md (+39/-39) alongside the legitimate
    docs/story_*.md output. Neither file is in the designer's write set.

    Inspects `git diff --cached --name-status` and flags any M / D / R / C / T
    status on a path outside `_DESIGNER_MODIFY_ALLOWLIST`. A (newly-added)
    paths use the broader `_DESIGNER_ADD_ALLOWLIST` so that first-time designer
    commits on greenfield products — where the renderer just installed
    CLAUDE.md / ARCHITECTURE.md / etc. as untracked files that post-doc's
    `git add -A` legitimately stages — are not flagged.

    Shared by `_post_doc_allowlist_check` (messages only) and the
    partial-commit path in `_run_post_doc_pipeline` (which needs the PATHS so
    it can DROP them from the commit instead of bouncing the whole thing).
    Idempotent and side-effect-free.
    """
    results: list[dict] = []
    try:
        diff_r = _run(["git", "diff", "--cached", "--name-status"], timeout=15)
    except Exception:
        return results
    if diff_r.returncode != 0:
        return results

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
        message = (
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
        results.append({"path": path, "status": status, "message": message})
    return results


def _post_doc_allowlist_check(working_dir: str, _run, product_name: str = "?") -> list[str]:
    """Backward-compatible wrapper around `_scan_post_doc_scope` that returns
    just the human-readable violation messages; empty list = clean.

    The pipeline itself no longer uses this to bounce — it calls
    `_scan_post_doc_scope` directly to DROP the out-of-scope paths and commit
    the in-scope docs (see `_run_post_doc_pipeline`). Retained for callers/
    tests that only want the messages.
    """
    return [d["message"] for d in _scan_post_doc_scope(working_dir, _run)]


def _run_post_doc_pipeline(product: dict, session_uid: str, working_dir: str,
                            assigned_features: list[dict], persona: str) -> None:
    """
    Path B: orchestrator owns ALL git for planner/designer too. Agents only
    write docs/ files and append `Designed` entries to session_result.json
    (the live-poll has already PATCHed the DB by the time this runs). Here we
    commit those docs and push to main. On push failure we PATCH the affected
    features back to Approved + clear design_doc_path so the next cycle
    re-plans them rather than coders running against missing docs.
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

    # Scope guard -- the designer may only commit docs/story_*.md plus the
    # documented append targets. Historically ANY out-of-scope path bounced
    # the WHOLE commit -- which deadlocked spec_defect escalations: the
    # designer was routed here to FIX a story doc, but its `git add -A` commit
    # also swept an out-of-scope file (e.g. ARCHITECTURE.md), so the corrected
    # doc never landed and the feature looped forever (DogTinder
    # #1872/#1881/#1892, 2026-06-21).
    #
    # Fix: DROP the out-of-scope paths from the commit and still ship the
    # in-scope design docs. Only bounce if NOTHING in-scope remains (the
    # designer produced *only* out-of-scope edits -- the MyDocusign #608
    # class). The out-of-scope working-tree edits are left unstaged and the
    # next _reset_workspace discards them, so the original protection (those
    # changes never reach origin) is preserved.
    scope_oos = _scan_post_doc_scope(working_dir, _run)
    if scope_oos:
        oos_paths = [d["path"] for d in scope_oos]
        _run(["git", "reset", "-q", "HEAD", "--", *oos_paths])
        log.warning(
            f"[post-{persona}] {pname}: scope-guard dropped {len(oos_paths)} "
            f"out-of-scope path(s) from the commit: {oos_paths[:5]}"
        )
        if _run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            # Only out-of-scope changes were staged -- nothing in-scope to
            # commit. Genuine bounce for re-design (MyDocusign #608 class).
            log.warning(
                f"[post-{persona}] {pname}: only out-of-scope changes staged "
                f"-- nothing to commit, bouncing for re-design"
            )
            _bounce_doc_features_for_lint(
                product, assigned_features, persona,
                [d["message"] for d in scope_oos],
            )
            return
        # In-scope design docs remain staged -- surface what we dropped
        # (audit trail) and continue to commit the design.
        _note_doc_scope_dropped(
            product, assigned_features, persona,
            [d["message"] for d in scope_oos],
        )

    # Clarification guard -- refuse the commit if any staged design doc
    # contains a [NEEDS CLARIFICATION: ...] marker. Borrowed from GitHub
    # spec-kit's templates/spec-template.md convention. Reverts the feature
    # to Approved (no fix_attempts bump) and posts a comment surfacing the
    # question(s) to the PM. The right move is to NOT advance an ambiguous
    # design — the coder will optimize for the designer's guess and ship
    # brittle code that the reviewer rejects, multiplied across rework
    # rounds until rapid_flap blocks the feature.
    clarification_violations = _post_doc_clarification_check(
        working_dir, _run, assigned_features, pname,
    )
    if clarification_violations:
        log.warning(
            f"[post-{persona}] {pname}: clarification-guard fired "
            f"({len(clarification_violations)} unresolved question(s)) -- "
            f"refusing commit, rolling features back to Approved for PM "
            f"triage"
        )
        _bounce_doc_features_for_clarification(
            product, assigned_features, persona, clarification_violations,
        )
        return

    # AC-recipe self-check (2026-07-27) — statically validate every `Verify:`
    # recipe in the staged design docs (shell syntax + Expected well-formedness)
    # so a syntactically-broken recipe never reaches a coder session (canonical:
    # MyGroceryApp #3196). Gated by DESIGNER_AC_LINT_ENABLED (default ON). Runs
    # ADVISORY by default during the soak (comment, ship the doc);
    # DESIGNER_AC_LINT_ENFORCE=1 promotes it to a hard bounce like the other
    # post-doc guards. Best-effort; a failure here must not block the commit.
    if os.environ.get("DESIGNER_AC_LINT_ENABLED", "1").strip().lower() \
            not in ("0", "false", "no", "off"):
        try:
            ac_recipe_violations = _post_doc_ac_recipe_check(
                working_dir, _run, assigned_features, pname)
        except Exception:
            log.exception(f"[post-{persona}] {pname}: AC-recipe check failed (non-fatal)")
            ac_recipe_violations = []
        if ac_recipe_violations:
            enforce = os.environ.get("DESIGNER_AC_LINT_ENFORCE", "").strip().lower() \
                in ("1", "true", "yes", "on")
            _handle_doc_ac_recipe_violations(
                product, assigned_features, persona, ac_recipe_violations, enforce)
            if enforce:
                return

    # Over-coupling advisory (wave-10 Layer 1.2) — SOAK mode: posts a
    # sizing-advisor comment on oversized design docs recommending a vertical
    # split, but never bounces. Runs after the hard guards pass so it only
    # annotates a doc that's actually about to ship. Best-effort; a failure
    # here must not block the commit.
    try:
        _post_doc_oversize_advisory(working_dir, _run, assigned_features, product)
    except Exception:
        log.exception(f"[post-{persona}] {pname}: sizing-advisor failed (non-fatal)")

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
    #
    # VERIFY-THE-DOC-EXISTS guard (2026-05-28): only mark a feature Designed
    # if its design doc actually exists at docs/story_{id}.md in the working
    # tree. Without this, the fallback fabricated a design_doc_path from the
    # feature id for EVERY assigned feature — even ones the designer never
    # actually designed. Canonical incident: designer assigned #1005 (it's the
    # only feature in {assigned_features}) but it queried GET /api/features,
    # judged the health-check feature foundational, and designed #1007 instead
    # (writing docs/story_1007.md + reporting #1007 Designed via
    # session_result.json). The fallback then marked the ASSIGNED #1005
    # Designed with phantom docs/story_1005.md — a file that was never
    # written. The #1005 coder then read an empty spec, improvised, and
    # bounced on Guard 6 until the supervisor blocked it. By only marking
    # features whose doc is on disk, a "wandered" assignment stays Approved
    # and gets re-designed next cycle instead of stranding the coder.
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            _mark_assigned_features_designed(
                working_dir, assigned_features, persona, pname, client,
            )
            # SOURCE-side dangling-dependency repair: a designer sizing-split may
            # have just Rejected a parent "as Replaced" and created live children.
            # The split re-homes the children (parent_id) but NOT external
            # dependents' depends_on, stranding them on a dead foundation forever
            # (canonical IndianFoodTruck #1633 → Rejected #1632). Re-home them onto
            # the live replacement now, at the source. ON by default; set
            # DEPENDENCY_REHOME_ENABLED to a falsy value to disable (dry-run logs only).
            _post_doc_rehome_replaced_dependents(product, persona, pname, client)
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: PM client error during designed PATCH: {e}")


def _post_doc_rehome_replaced_dependents(product: dict, persona: str, pname: str, client) -> int:
    """Re-home features whose ``depends_on`` points at a Rejected/Reverted
    target onto its live replacement child — the SOURCE-side cure for the
    re-decomposition stranding class (the per-cycle catch-net is the safety net).

    Reuses ``orchestrator.cycle.dependencies.dangling_dependency_repairs`` (single
    source of truth). **ON by default** for all products; set
    ``DEPENDENCY_REHOME_ENABLED`` to a falsy value (``0``/``false``/``no``/``off``)
    to disable — it then only logs the repairs it *would* make (dry-run). Mirrors
    the ``BLOCKED_REPROCESSOR_ENABLED`` / ``RECONCILER_CHORES_ENABLED`` kill-switch
    convention. Best-effort; never raises into the pipeline. Returns the number of
    re-homes applied.
    """
    from orchestrator.cycle.dependencies import dangling_dependency_repairs
    pid = product.get("id")
    enabled = os.environ.get("DEPENDENCY_REHOME_ENABLED", "on").strip().lower() not in ("0", "false", "no", "off", "")
    try:
        feats = client.get(f"/api/products/{pid}/features").json()
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: rehome fetch failed: {e}")
        return 0
    if not isinstance(feats, list):
        return 0
    repairs = dangling_dependency_repairs(feats)
    if not repairs:
        return 0
    if not enabled:
        for r in repairs:
            log.info(f"[post-{persona}] {pname}: [rehome-soak] would repair #{r['feature_id']}: {r['reason']}")
        return 0
    applied = 0
    for r in repairs:
        fid, new_dep = r["feature_id"], r["new_dep"]
        try:
            if new_dep is not None:
                client.patch(f"/api/features/{fid}", json={
                    "depends_on": new_dep, "changed_by": f"post-{persona}:rehome",
                })
                client.post(f"/api/features/{fid}/comments", json={
                    "author": f"post-{persona}:rehome",
                    "body": (f"🔧 Dependency repair — {r['reason']}. The prior target was "
                             f"Rejected as 'Replaced'; re-homed to its live replacement so "
                             f"the dispatch gate releases this once the replacement ships."),
                })
                applied += 1
            else:
                # No live replacement child — can't safely auto re-home; flag the PM.
                client.post("/api/alerts", json={
                    "level": "warning",
                    "message": f"Dangling dependency — feature #{fid}: {r['reason']}",
                })
        except Exception as e:
            log.warning(f"[post-{persona}] {pname}: rehome for #{fid} failed: {e}")
    if applied:
        log.info(f"[post-{persona}] {pname}: re-homed {applied} dangling dependency(ies)")
    return applied


def _mark_assigned_features_designed(
    working_dir: str, assigned_features: list, persona: str,
    pname: str, client,
) -> None:
    """For each assigned feature, mark it Designed IF its design doc exists
    on disk; otherwise re-queue it to Approved (the designer wandered and
    never wrote this feature's doc — don't fabricate a phantom path).

    Extracted from _run_post_doc_pipeline so the verify-doc-exists guard
    is unit-testable without the surrounding git/chmod side-effects.
    """
    wd = Path(working_dir)
    for f in assigned_features:
        fid = f["id"]
        doc_path = f.get("design_doc_path") or f"docs/story_{fid}.md"
        if not (wd / doc_path).is_file():
            # The designer didn't write this feature's doc (it wandered to a
            # different feature, or named the file wrong). Do NOT fabricate a
            # Designed status with a phantom path — leave it Approved so the
            # next designer cycle re-picks it. The feature the designer DID
            # write (and reported in session_result.json) is handled by the
            # reconcile path independently.
            log.warning(
                f"[post-{persona}] {pname}: feature #{fid} assigned but "
                f"`{doc_path}` not on disk — designer designed a different "
                f"feature. Rolling #{fid} back to Approved for re-design "
                f"(no phantom design_doc_path)."
            )
            try:
                client.patch(f"/api/features/{fid}", json={
                    "status": "Approved",
                    "design_doc_path": None,
                    "changed_by": "post-doc:rollback",
                })
            except Exception as e2:
                log.warning(
                    f"[post-{persona}] {pname}: re-queue PATCH for #{fid} failed: {e2}"
                )
            continue
        try:
            r = client.patch(f"/api/features/{fid}", json={
                "status": "Designed",
                "design_doc_path": doc_path,
                "changed_by": "post-doc:fallback",
            })
            r.raise_for_status()
            log.info(
                f"[post-{persona}] {pname}: feature #{fid} → Designed (doc={doc_path})"
            )
        except Exception as e:
            log.warning(
                f"[post-{persona}] {pname}: direct PATCH for #{fid} failed: {e}"
            )


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


def _note_doc_scope_dropped(
    product: dict,
    assigned_features: list[dict],
    persona: str,
    violations: list[str],
) -> None:
    """Post an informational (NON-bouncing) comment recording that the
    scope-guard dropped out-of-scope paths from the designer commit while
    still committing the in-scope design docs.

    The partial-commit counterpart to `_bounce_doc_features_for_lint`: the
    design ships, so we do NOT roll the feature back — we only leave an audit
    trail so a PM can see the designer reached outside its write set. This is
    what breaks the spec_defect→designer→whole-commit-reject loop (DogTinder
    #1872/#1881/#1892): the corrected story doc now lands even when an
    out-of-scope file was swept into the working tree.
    """
    pname = product.get("name", "?")
    body = (
        f"ℹ️ post-doc scope-guard dropped {len(violations)} out-of-scope "
        f"path(s) from the commit, but committed the in-scope design "
        f"doc(s) so the design still ships:\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\n\nThe out-of-scope edits were NOT committed (discarded by the "
          "next workspace reset). If a structural change is genuinely "
          "needed, route it through the PM (web UI) or the architect "
          "persona — not the designer agent."
    )
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                try:
                    client.post(
                        f"/api/features/{f['id']}/comments",
                        json={"author": "scope-guard", "body": body},
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: scope-guard comment for "
                        f"#{f['id']} failed: {e}"
                    )
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: scope-guard PM client error: {e}")


_AC_DEF_RE = __import__("re").compile(r"^\s*#{0,3}\s*AC\d{1,2}[.:]", __import__("re").M)


def _post_doc_oversize_advisory(
    working_dir: str,
    _run,
    assigned_features: list[dict],
    product: dict,
) -> int:
    """SOAK-mode over-coupling detector (wave-10, Layer 1.2).

    Reads each staged ``docs/story_*.md`` and counts its acceptance criteria
    (``AC1.``/``AC2.`` definition lines) and ``### Files to create`` bullets.
    A doc that exceeds the sizing caps (>4 ACs or >6 created files) is a
    foundation the designer kept whole under the cohesion rule but that a
    single coder session likely can't land (the #1621 class). We post a
    ``sizing-advisor`` comment recommending a VERTICAL split — but do NOT
    bounce the feature.

    Comment-path soak first, per the Guard-17-tuning protocol: a design-doc
    AC count is a softer signal than a committed-code violation, so it earns
    its bounce rights only after a clean audit window. Returns the number of
    advisories posted (for logging / future promotion). Best-effort.
    """
    import re as _re
    from pathlib import Path as _PPath

    try:
        diff_r = _run(["git", "diff", "--cached", "--name-only"], timeout=15)
    except Exception:
        return 0
    if diff_r.returncode != 0:
        return 0
    staged_docs = [
        ln.strip() for ln in (diff_r.stdout or "").splitlines()
        if ln.strip().startswith("docs/story_") and ln.strip().endswith(".md")
    ]
    if not staged_docs:
        return 0

    pname = product.get("name", "?")
    wd = _PPath(working_dir)
    _files_hdr = _re.compile(r"^\s*#{2,4}\s*Files to create", _re.I | _re.M)
    _bullet = _re.compile(r"^\s*[-*]\s+\S")
    _next_hdr = _re.compile(r"^\s*#{2,4}\s")
    posted = 0

    with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
        for doc_path in staged_docs:
            try:
                content = (wd / doc_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            ac_count = len(_AC_DEF_RE.findall(content))
            # Count bullets under the first "Files to create" header until the
            # next markdown header.
            file_count = 0
            m = _files_hdr.search(content)
            if m:
                tail = content[m.end():].splitlines()
                for ln in tail:
                    if _next_hdr.match(ln):
                        break
                    if _bullet.match(ln):
                        file_count += 1
            if ac_count <= 4 and file_count <= 6:
                continue

            # Map doc → feature id (docs/story_<id>.md, padded or not).
            try:
                stem = doc_path.rsplit("/", 1)[-1]
                num = stem.split("_", 1)[1].split(".", 1)[0]
                fid = int(num.lstrip("0") or "0")
            except Exception:
                fid = next((f.get("id") for f in assigned_features
                            if isinstance(f.get("id"), int)), None)
            if not isinstance(fid, int):
                continue

            body = (
                f"📐 **sizing-advisor (advisory — not a bounce):** this design "
                f"declares **{ac_count} ACs** and **{file_count} files to create** "
                f"(soft caps: ≤4 ACs, ≤6 files). A foundation this size tends to "
                f"cap-block in one coder session (the #1621 class).\n\n"
                f"If this is a coupled foundation kept whole under the "
                f"SHARED-ENTRYPOINT COHESION rule, prefer a **VERTICAL split**: "
                f"slice 1 = the thinnest bootable thread that creates the shared "
                f"entrypoint file(s) + one passing test; slice 2+ `depends_on` the "
                f"previous slice and extend it. The orchestrator enforces "
                f"`depends_on` at dispatch, so the slices sequence and cannot "
                f"collide. See designer.md → SHARED-ENTRYPOINT COHESION option (b)."
            )
            try:
                client.post(f"/api/features/{fid}/comments",
                            json={"author": "sizing-advisor", "body": body})
                posted += 1
            except Exception as e:
                log.warning(f"[post-doc] {pname}: sizing-advisor comment for "
                            f"#{fid} failed: {e}")

    if posted:
        log.info(f"[post-doc] {pname}: sizing-advisor posted {posted} "
                 f"over-coupling advisory(ies) (soak — no bounce)")
    return posted


def _post_doc_ac_recipe_check(
    working_dir: str,
    _run,
    assigned_features: list[dict],
    product_name: str = "?",
) -> list[str]:
    """Designer-side AC self-check (2026-07-27): statically validate every
    ``Verify:`` recipe in a staged design doc BEFORE it ships, so a
    syntactically-broken recipe never reaches a coder session.

    At design time the feature isn't built, so we cannot run a recipe
    end-to-end. We CAN catch the failures that actually blocked features:
      - **Shell syntax** — `bash -n -c <cmd>` parses the recipe without
        executing it; an unclosed bracket/quote/paren (canonical: MyGroceryApp
        #3196, AC3 recipe "unclosed brackets", flapped the coder to
        fix_attempts=7) fails to parse and is flagged.
      - **Expected well-formedness** — a JSON-shaped ``Expected:`` block that
        doesn't `json.loads` is a malformed contract the verify-check can
        never match.

    Returns a list of human-readable violation strings (empty = clean).
    Idempotent, side-effect-free, best-effort — any failure returns [] so the
    pipeline proceeds (this must never itself block a good design).
    """
    from pathlib import Path as _PPath
    import json as _json

    violations: list[str] = []
    try:
        diff_r = _run(["git", "diff", "--cached", "--name-only"], timeout=15)
    except Exception:
        return violations
    if diff_r.returncode != 0:
        return violations
    staged_docs = [
        ln.strip() for ln in (diff_r.stdout or "").splitlines()
        if ln.strip().startswith("docs/story_") and ln.strip().endswith(".md")
    ]
    if not staged_docs:
        return violations

    # Reuse the post-coder verify-check's recipe parser (single source of
    # truth for how Verify:/Expected: pairs are extracted).
    try:
        from orchestrator.pipelines.post_coder import _parse_ac_verifies
    except Exception:
        return violations

    wd = _PPath(working_dir)
    for doc_path in staged_docs:
        try:
            content = (wd / doc_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        stem = doc_path.rsplit("/", 1)[-1]
        try:
            triples = _parse_ac_verifies(content)
        except Exception:
            continue
        for ac_num, cmd, exp in triples:
            # 1) Shell syntax — parse-only, never executes the recipe.
            try:
                syn = _run(["bash", "-n", "-c", cmd], timeout=15)
                if syn.returncode != 0:
                    detail = (syn.stderr or "").strip().splitlines()
                    msg = detail[-1] if detail else "shell syntax error"
                    violations.append(
                        f"{stem} AC{ac_num}: Verify recipe has invalid shell "
                        f"syntax ({msg[:120]}) — fix the recipe before it reaches a coder."
                    )
                    continue
            except Exception:
                pass  # bash unavailable / timeout — skip the syntax check, not a violation
            # 2) Expected well-formedness — only when it's clearly JSON-shaped.
            stripped = exp.strip()
            if stripped[:1] in ("{", "["):
                try:
                    _json.loads(stripped)
                except Exception:
                    violations.append(
                        f"{stem} AC{ac_num}: Expected block looks like JSON but "
                        f"does not parse — the verify-check can never match it."
                    )
    if violations:
        log.info(
            f"[post-doc] {product_name}: AC-recipe check found "
            f"{len(violations)} malformed recipe(s) across "
            f"{len(staged_docs)} staged doc(s)"
        )
    return violations


def _handle_doc_ac_recipe_violations(
    product: dict,
    assigned_features: list[dict],
    persona: str,
    violations: list[str],
    enforce: bool,
) -> None:
    """Feedback channel for the AC-recipe self-check. Always posts the malformed
    recipes as a `lint-guard` comment so the next designer cycle sees them; when
    ``enforce`` is set, also rolls the feature back to Approved with the doc
    cleared (a hard bounce, like the other post-doc guards). During the soak
    ``enforce`` is off — advisory only, so we can measure the false-positive
    rate before it starts bouncing real designs. Best-effort."""
    pname = product.get("name", "?")
    verb = "auto-reject" if enforce else "advisory"
    body = (
        f"⚠️ post-doc AC-recipe self-check {verb} "
        f"({len(violations)} malformed Verify recipe(s)):\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\n\nA Verify recipe that is syntactically broken (unclosed brackets) "
          "or has a malformed Expected block can NEVER be satisfied — a coder "
          "session would flap on it until it blocks. "
        + ("This design is back to Approved; the next designer cycle must fix "
           "the recipe(s) in the story doc."
           if enforce else
           "Advisory only (soak): the design still shipped, but fix the "
           "recipe(s) in the next revision.")
    )
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                fid = f["id"]
                try:
                    client.post(f"/api/features/{fid}/comments",
                                json={"author": "lint-guard", "body": body})
                except Exception as e:
                    log.warning(f"[post-{persona}] {pname}: AC-recipe comment #{fid} failed: {e}")
                if enforce:
                    try:
                        client.patch(f"/api/features/{fid}", json={
                            "status": "Approved", "design_doc_path": None,
                            "changed_by": "post-doc:rollback"})
                        log.info(f"[post-{persona}] {pname}: #{fid} bounced by AC-recipe guard")
                    except Exception as e:
                        log.warning(f"[post-{persona}] {pname}: AC-recipe PATCH #{fid} failed: {e}")
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: AC-recipe PM client error: {e}")


def _post_doc_clarification_check(
    working_dir: str,
    _run,
    assigned_features: list[dict],
    product_name: str = "?",
) -> list[tuple[int, str, str]]:
    """Refuse a designer commit whose staged design doc contains a
    ``[NEEDS CLARIFICATION: ...]`` marker.

    Returns a list of ``(feature_id, story_filename, question)`` tuples;
    empty list means clean. The marker convention is borrowed from
    GitHub spec-kit's ``templates/spec-template.md`` — the designer is
    instructed to flag ambiguous intent rather than guess. The post-doc
    pipeline catches the marker before push so the feature is reverted
    to ``Approved`` for PM triage instead of advancing into a coder
    cycle that's destined to bounce.

    Idempotent and side-effect-free. Best-effort: any subprocess /
    decode failure returns an empty list so the pipeline proceeds.
    """
    from pathlib import Path as _PPath
    import re as _re

    findings: list[tuple[int, str, str]] = []
    try:
        diff_r = _run(["git", "diff", "--cached", "--name-only"], timeout=15)
    except Exception:
        return findings
    if diff_r.returncode != 0:
        return findings

    staged_docs = [
        ln.strip() for ln in (diff_r.stdout or "").splitlines()
        if ln.strip().startswith("docs/story_") and ln.strip().endswith(".md")
    ]
    if not staged_docs:
        return findings

    # Map staged filenames back to assigned feature ids when possible.
    # The convention is docs/story_{feature_id:03d}.md but pre-2026-04
    # products use docs/story_<id>.md without zero-padding. Try the
    # exact integer first, then strip a leading zero pad.
    _by_id = {int(f["id"]): f for f in assigned_features if isinstance(f.get("id"), int)}

    marker_re = _re.compile(r"\[NEEDS\s+CLARIFICATION(?:\s*:\s*([^\]]+))?\]", _re.I)
    wd = _PPath(working_dir)
    for doc_path in staged_docs:
        try:
            content = (wd / doc_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in marker_re.finditer(content):
            question = (m.group(1) or "").strip() or "(no question text — designer wrote bare marker)"
            # Best-effort feature_id extraction from filename.
            try:
                stem = doc_path.rsplit("/", 1)[-1]      # "story_007.md"
                num = stem.split("_", 1)[1].split(".", 1)[0]
                fid = int(num)
                if fid not in _by_id:
                    # try un-padded form for sanity
                    fid = int(num.lstrip("0") or "0")
            except Exception:
                fid = -1
            findings.append((fid, doc_path, question))

    if findings:
        log.info(
            f"[post-doc] {product_name}: clarification-guard found "
            f"{len(findings)} [NEEDS CLARIFICATION] marker(s) across "
            f"{len({d for _, d, _ in findings})} doc(s); refusing commit"
        )
    return findings


def _bounce_doc_features_for_clarification(
    product: dict,
    assigned_features: list[dict],
    persona: str,
    findings: list[tuple[int, str, str]],
) -> None:
    """Post the unresolved-question list as a feature comment then revert
    the feature to ``Approved`` (no fix_attempts bump). Mirrors
    ``_bounce_doc_features_for_lint`` but addressed to PM rather than the
    next designer cycle — the design doc is being held until human input
    clarifies intent.

    Uses ``changed_by="post-doc:rollback"`` (already in the rank-guard
    bypass allowlist) so we don't need a new bypass label.
    """
    pname = product.get("name", "?")
    # Group findings by feature_id so we post one comment per feature.
    by_feature: dict[int, list[tuple[str, str]]] = {}
    for fid, doc, question in findings:
        by_feature.setdefault(fid, []).append((doc, question))

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for f in assigned_features:
                fid = f["id"]
                f_findings = by_feature.get(fid) or []
                if not f_findings:
                    # Sibling story flagged something — still bounce this
                    # feature to keep the commit unified, but use a softer
                    # comment so the PM understands this is a co-bounce.
                    f_findings = [(
                        "(sibling design doc)",
                        "Sibling design in same commit flagged clarification "
                        "needs; this feature reverted alongside.",
                    )]
                body = (
                    f"⚠️ post-doc clarification-guard auto-reject "
                    f"({len(f_findings)} unresolved question(s) in staged design):\n\n"
                    + "\n".join(
                        f"- **{q}**\n  *(in `{d}`)*"
                        for d, q in f_findings
                    )
                    + "\n\nThe designer flagged ambiguity in the story intent "
                      "rather than guessing. This feature is back to `Approved` "
                      "(no fix_attempts bump) so a PM can answer the question "
                      "above and the next designer cycle has a concrete contract "
                      "to write. The local design doc has been discarded by the "
                      "next workspace reset.\n\n"
                      "Convention is GitHub spec-kit's `[NEEDS CLARIFICATION: ...]` "
                      "marker (see orchestrator/prompts/designer.md Step 1)."
                )
                try:
                    client.post(
                        f"/api/features/{fid}/comments",
                        json={"author": "clarification-guard", "body": body},
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: clarification-guard comment "
                        f"for #{fid} failed: {e}"
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
                        f"clarification-guard ({len(f_findings)} question(s))"
                    )
                except Exception as e:
                    log.warning(
                        f"[post-{persona}] {pname}: clarification-guard PATCH "
                        f"for #{fid} failed: {e}"
                    )
    except Exception as e:
        log.warning(f"[post-{persona}] {pname}: clarification-guard PM client error: {e}")
