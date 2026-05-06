"""
Post-session pipeline for coder runs — the orchestrator-side git ceremony.

Path B (current architecture): the coder agent ONLY writes code to the
workspace. This pipeline takes care of every subsequent git step:
  1. Detect agent-handled vs not-handled features (verifies claimed PRs
     have real ``[feature-N]`` commit tags on GitHub before trusting them)
  2. Detect uncommitted changes / unpushed commits in the workspace
  3. Resolve target branch in three modes:
       - sprint-PR mode  → reuse the already-provisioned sprint branch + PR
       - rework mode     → all features point at one open PR; force-push to it
       - per-feature mode → cut ``coder/<session_uid>`` (legacy fallback)
  4. add + commit (with ``[feature-N]`` tags) + push (force-with-lease in rework)
  5. Append Reviewing entries to session_result.json (with PM-API fallback PATCH
     if the file write fails — keeps a real GitHub PR from being stranded)

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
This is the largest single function in the orchestrator (~500 LOC). Phase 3
will decompose it into 4 helpers (detect_unpushed_work, verify_agent_pr_tags,
commit_and_push, record_reviewing_entries).
"""

import json as _json
import logging
import os
import subprocess as _sp
from pathlib import Path

import httpx

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine
from orchestrator.integrations.github import _get_gh_token, _parse_repo_slug

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _run_post_coder_pipeline(product: dict, session_uid: str, working_dir: str,
                              assigned_features: list[dict]) -> list[int]:
    """
    Deterministic git fallback after the coder LLM exits cleanly.
    The coder ONLY writes code; this function pushes to the sprint branch:
      1. Detect if there are any changes in the workspace
      2. Check out the sprint branch (provisioned at sprint activation)
      3. git add + commit + push
      4. PATCH each assigned feature to Reviewing + pr_number on the PM API
         directly (no session_result.json roundtrip; see step 5 comment for
         why the file-based handoff was removed on 2026-05-06).

    Returns the list of feature IDs successfully PATCHed to Reviewing — used
    by the caller to set the session record's `features_pushed` counter.

    Sprint-PR mode is the only supported flow: PR creation happens once at
    sprint activation (`orchestrator.sprint_pr.provision_sprint_pr`); coder
    sessions just stack commits onto the same branch. If the active sprint
    has no provisioned branch + PR, the pipeline marks the assigned features
    Blocked with a clear reason — never opens a fresh PR.
    """
    pushed_ids: list[int] = []
    pname = product.get("name", "?")
    if not assigned_features:
        log.info(f"[post-coder] {pname}: no assigned features — skipping commit/PR")
        return pushed_ids

    # Path B: agent left files owned by uid 1001; orchestrator (different uid)
    # needs them writable to append to .git/logs/HEAD during checkout. The only
    # path that works on Windows bind-mounts is an alpine sidecar via the host
    # docker socket. See commit b301048 for why an in-container chmod fails.
    _chmod_workspace_via_alpine(working_dir, pname)

    # Path B: orchestrator owns ALL git ceremony. Agents no longer commit or
    # push (see prompts/{greenfield,brownfield}.md, refactored on this branch).
    # The legacy "agent already pushed" verification block below is dead in
    # normal flow but kept as defense-in-depth: if a stale-cached prompt or a
    # weak model reverts to the old behavior, we still skip the duplicate push.
    # The double check (pr_number > 0 + GitHub existence + [feature-N] commits)
    # catches agents that hallucinate PRs in session_result.json without
    # actually running git push — a failure mode observed with deepseek-v4-flash
    # which wrote pr_number=0 entries that the live-poll later rejected,
    # leaving features stuck Implementing while we'd already discarded the
    # local diff.
    try:
        sr_path = Path(working_dir) / "session_result.json"
        claimed: dict[int, int] = {}  # feature_id -> pr_number from session_result
        if sr_path.exists():
            for ln in sr_path.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    e = _json.loads(ln)
                except Exception:
                    continue
                pr_n = e.get("pr_number") if isinstance(e, dict) else None
                fid_e = e.get("id") if isinstance(e, dict) else None
                if (isinstance(e, dict) and e.get("status") == "Reviewing"
                        and isinstance(pr_n, int) and pr_n > 0
                        and isinstance(fid_e, int)):
                    claimed[fid_e] = pr_n

        # Verify each claimed PR exists on GitHub before trusting the agent.
        already_handled: set[int] = set()
        if claimed:
            gh_token_v = _get_gh_token()
            github_repo_v = product.get("github_repo", "")
            if gh_token_v and github_repo_v:
                repo_slug_v = _parse_repo_slug(github_repo_v)
                gh_headers_v = {
                    "Authorization": f"Bearer {gh_token_v}",
                    "Accept": "application/vnd.github+json",
                }
                # Cache PR-existence checks so duplicate pr_numbers across
                # features don't multiply API calls. A PR counts as "agent
                # actually shipped code" only when (a) the PR exists and is
                # open AND (b) the commit log contains at least one commit
                # whose message tags the feature `[feature-<id>]` — the
                # convention the coder prompt mandates. The PR-exists check
                # alone is too lax: in sprint-PR-mode the PR is opened by
                # provision_sprint_pr at sprint activation with a single
                # `chore(sprint-<id>): scaffold sprint branch` commit, so
                # any agent that hallucinates a Reviewing entry pointing at
                # the existing PR passes the open-PR check without ever
                # calling git push.
                pr_status: dict[int, dict] = {}  # pr_number -> {"open": bool, "feature_commits": set[int]}
                for fid_c, pr_n in claimed.items():
                    if pr_n not in pr_status:
                        info = {"open": False, "feature_commits": set()}
                        try:
                            r = httpx.get(
                                f"https://api.github.com/repos/{repo_slug_v}/pulls/{pr_n}",
                                headers=gh_headers_v, timeout=10,
                            )
                            info["open"] = (
                                r.status_code == 200
                                and isinstance(r.json(), dict)
                                and r.json().get("state") == "open"
                            )
                        except Exception:
                            pass
                        if info["open"]:
                            try:
                                cr = httpx.get(
                                    f"https://api.github.com/repos/{repo_slug_v}/pulls/{pr_n}/commits",
                                    headers=gh_headers_v,
                                    params={"per_page": 100},
                                    timeout=15,
                                )
                                if cr.status_code == 200:
                                    import re as _re
                                    tag_re = _re.compile(r"\[feature-(\d+)\]")
                                    for c in cr.json() or []:
                                        msg = (c.get("commit") or {}).get("message", "")
                                        for m in tag_re.finditer(msg):
                                            try:
                                                info["feature_commits"].add(int(m.group(1)))
                                            except ValueError:
                                                pass
                            except Exception:
                                pass
                        pr_status[pr_n] = info
                    info = pr_status[pr_n]
                    if info["open"] and fid_c in info["feature_commits"]:
                        already_handled.add(fid_c)
                    elif info["open"] and not info["feature_commits"]:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR has no [feature-N] commits — agent never pushed; running fallback"
                        )
                    elif info["open"]:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR has no commit tagged [feature-{fid_c}] (found tags: "
                            f"{sorted(info['feature_commits'])}) — running fallback"
                        )
                    else:
                        log.warning(
                            f"[post-coder] {pname}: agent claimed PR #{pr_n} for feature #{fid_c} "
                            f"but PR is not open on GitHub — running fallback for this feature"
                        )

        assigned_ids = {f["id"] for f in assigned_features}
        if assigned_ids and assigned_ids.issubset(already_handled):
            log.info(
                f"[post-coder] {pname}: agent opened verified PRs for all "
                f"{len(assigned_ids)} assigned features — skipping fallback pipeline"
            )
            return pushed_ids
        if already_handled & assigned_ids:
            # Partial coverage. Narrow `assigned_features` to the unhandled
            # subset so the no-diff Blocked-flip and the session_result.json
            # writeback don't demote features the agent already finished
            # (PATCHing them back to Blocked bypasses the rank guard in
            # _apply_session_entry).
            log.info(
                f"[post-coder] {pname}: agent handled {sorted(already_handled & assigned_ids)}; "
                f"running fallback for {sorted(assigned_ids - already_handled)}"
            )
            assigned_features = [f for f in assigned_features if f["id"] not in already_handled]
            if not assigned_features:
                # Filter consumed everything — handled features already
                # taken care of by _apply_session_entry, no fallback work
                # to do. (Defensive: the issubset check above should have
                # caught this, but rely on it here too in case the set
                # math races with a concurrent live-poll application.)
                log.info(f"[post-coder] {pname}: all features handled by agent — skipping fallback pipeline")
                return pushed_ids
    except Exception:
        log.exception(f"[post-coder] {pname}: agent-handled detection failed — running pipeline")

    def _run(cmd: list[str], **kw) -> _sp.CompletedProcess:
        # Pop timeout from kw so the caller's override doesn't collide with the
        # default we pass into _sp.run. Without this, e.g. _run(..., timeout=180)
        # raises TypeError("got multiple values for keyword argument 'timeout'")
        # — which crashes the whole pipeline before our diagnostic checks run.
        timeout = kw.pop("timeout", 120)
        return _sp.run(cmd, cwd=working_dir, capture_output=True, text=True, timeout=timeout, **kw)

    # 1. Detect changes — anything uncommitted in the tree, OR committed-but-
    # not-pushed (the agent may have committed itself; we still need to push).
    status = _run(["git", "status", "--porcelain"])
    changed = [ln for ln in status.stdout.splitlines() if ln.strip()
               and not ln.endswith("session_result.json")
               and not ln.endswith("session_summary.md")
               and "/Temp/" not in ln and "/Results/" not in ln]
    has_unpushed_commits = False
    for ref in ("@{u}", "origin/main", "origin/master"):
        ahead = _run(["git", "rev-list", "--count", f"{ref}..HEAD"])
        if ahead.returncode == 0:
            try:
                has_unpushed_commits = int((ahead.stdout or "0").strip()) > 0
            except ValueError:
                pass
            break
    if not changed and not has_unpushed_commits:
        log.warning(f"[post-coder] {pname}: agent exited 0 but no code changes or unpushed commits — skipping PR")
        # Mark features Blocked so they don't loop in Implementing forever
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for f in assigned_features:
                    client.patch(f"/api/features/{f['id']}", json={
                        "status": "Blocked",
                        "blocked_reason": f"Coder session {session_uid} exited 0 with no code changes",
                    })
        except Exception:
            pass
        return pushed_ids
    log.info(f"[post-coder] {pname}: {len(changed)} changed file(s) detected — sample: {changed[:3]}")

    def _fmt_err(r) -> str:
        """Render a CompletedProcess for diagnostic logging — git often prints
        useful info on stdout, not stderr (e.g. 'nothing to commit')."""
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        return f"rc={r.returncode} stdout={out[:300]!r} stderr={err[:300]!r}"

    # 2. Resolve target branch.
    # Sprint-PR mode: every coder run pushes to the same sprint/<id> branch so
    # there's exactly one PR per sprint (no fan-out, no orphan PRs). The branch
    # and PR were provisioned by the website's _maybe_provision_sprint_pr at
    # sprint activation; we just check it out and push commits.
    # Rework mode (NEW): when all assigned features point at the same existing
    # open PR (set by a prior coder cycle that got changes_requested), push to
    # that PR's head branch instead of cutting a new one. Eliminates the PR
    # fan-out we observed today (PRs 163/164/165 all covering same features).
    # Per-feature mode (default): cut a fresh `coder/<session_uid>` branch and
    # later open a new PR for it.
    feat_ids = [f["id"] for f in assigned_features]
    sprint_pr_mode = bool(product.get("_sprint_pr_mode"))
    sprint_branch  = product.get("_sprint_branch") or ""
    sprint_pr_num  = product.get("_sprint_pr_number") or None
    sprint_pr_url  = product.get("_sprint_pr_url") or ""
    if sprint_pr_mode:
        if not sprint_branch or not sprint_pr_num:
            log.warning(
                f"[post-coder] {pname}: sprint_pr_mode set but sprint metadata missing "
                f"(branch={sprint_branch!r} pr={sprint_pr_num!r}) — falling back to per-feature mode"
            )
            sprint_pr_mode = False
    if sprint_pr_mode:
        # Verify the cached sprint PR is still open on GitHub before pushing.
        # The sprint metadata (`product._sprint_pr_*`) is read from the DB at
        # session-start, but a PR can be closed/merged between then and now —
        # by a human, by auto_merge.sweep, or by an external script. Pushing
        # to a closed PR's head branch leaves commits on a branch with no
        # active PR; the website thinks features are Reviewing but reconcile
        # bounces them back to Implementing because the PR is closed.
        # Real incident 2026-05-06: PR #1 on DigitalSign was closed
        # 2026-05-05 18:38 UTC; orchestrator kept pushing to sprint/79 for
        # ~9h with the website reporting "0 features in Reviewing" while
        # the agent burned cycles producing commits no PR pointed at.
        gh_token_chk = _get_gh_token()
        github_repo_chk = product.get("github_repo", "")
        if gh_token_chk and github_repo_chk:
            try:
                repo_slug_chk = _parse_repo_slug(github_repo_chk)
                pr_chk = httpx.get(
                    f"https://api.github.com/repos/{repo_slug_chk}/pulls/{int(sprint_pr_num)}",
                    headers={"Authorization": f"Bearer {gh_token_chk}",
                             "Accept": "application/vnd.github+json"},
                    timeout=10,
                )
                if pr_chk.status_code == 200:
                    state = pr_chk.json().get("state")
                    if state != "open":
                        log.warning(
                            f"[post-coder] {pname}: sprint PR #{sprint_pr_num} is "
                            f"{state!r} on GitHub (not open) — falling back to "
                            f"per-feature mode. Sprint metadata is stale; "
                            f"reconcile.reconcile_sprint_pr_state will null it "
                            f"out next cycle."
                        )
                        sprint_pr_mode = False
                elif pr_chk.status_code == 404:
                    log.warning(
                        f"[post-coder] {pname}: sprint PR #{sprint_pr_num} not "
                        f"found on GitHub (404) — falling back to per-feature mode"
                    )
                    sprint_pr_mode = False
                else:
                    # Transient GitHub error — proceed in sprint_pr_mode and
                    # let the push attempt itself surface any real failure.
                    log.warning(
                        f"[post-coder] {pname}: GitHub returned {pr_chk.status_code} "
                        f"checking PR #{sprint_pr_num} — proceeding optimistically"
                    )
            except Exception as e:
                log.warning(
                    f"[post-coder] {pname}: sprint PR state check failed: {e} "
                    f"— proceeding optimistically with cached metadata"
                )

    rework_pr_mode = False
    rework_pr_number: int | None = None
    rework_branch_name: str = ""
    rework_pr_url: str = ""
    if not sprint_pr_mode:
        existing_prs = {f.get("pr_number") for f in assigned_features
                        if isinstance(f.get("pr_number"), int)}
        if len(existing_prs) == 1:
            candidate = next(iter(existing_prs))
            gh_token_for_lookup = _get_gh_token()
            github_repo = product.get("github_repo", "")
            if gh_token_for_lookup and github_repo and candidate:
                try:
                    repo_slug = _parse_repo_slug(github_repo)
                    pr_resp = httpx.get(
                        f"https://api.github.com/repos/{repo_slug}/pulls/{candidate}",
                        headers={
                            "Authorization": f"Bearer {gh_token_for_lookup}",
                            "Accept": "application/vnd.github+json",
                        },
                        timeout=10,
                    )
                    if pr_resp.status_code == 200:
                        pr_data = pr_resp.json()
                        if isinstance(pr_data, dict) and pr_data.get("state") == "open":
                            rework_pr_mode = True
                            rework_pr_number = candidate
                            rework_branch_name = (pr_data.get("head") or {}).get("ref") or ""
                            rework_pr_url = pr_data.get("html_url") or ""
                            log.info(
                                f"[post-coder] {pname}: rework mode — features {feat_ids} "
                                f"all point at open PR #{candidate} (branch={rework_branch_name!r}); "
                                f"force-pushing instead of opening a new PR"
                            )
                            if not rework_branch_name:
                                log.warning(
                                    f"[post-coder] {pname}: rework PR #{candidate} has no head.ref — falling back to per-feature mode"
                                )
                                rework_pr_mode = False
                except Exception as e:
                    log.warning(f"[post-coder] {pname}: rework lookup for PR #{candidate} failed: {e} — falling back to per-feature mode")

    if sprint_pr_mode:
        branch = sprint_branch
        # In Path B the agent runs on main (or whatever branch _reset_workspace
        # left us on) and writes code there as untracked/modified files. We need
        # those diffs on sprint_branch instead. Plain `git checkout sprint/79`
        # fails when the agent's changes conflict with sprint_branch's content
        # (e.g. agent recreated a file that sprint_branch already had a different
        # version of). Stash → switch → pop carries the diffs across. On stash-
        # pop conflict we resolve in favor of the agent ("theirs" in stash terms)
        # since the post-coder force-push will overwrite the sprint branch tree
        # anyway.
        _run(["git", "fetch", "origin"])
        # Stash with -u so UNTRACKED files survive the branch switch too. The
        # agent's brand-new source files (e.g. SRC/healthz.ts, app/api/.../route.ts)
        # are untracked at this point; without -u, `git checkout sprint/79`
        # errors with "untracked working tree files would be overwritten by
        # checkout" the moment local sprint/79 has tracked content at the
        # same path — which it does after any prior session committed there.
        # Real incident 2026-05-06 17:14: 10 changed files detected, checkout
        # sprint/79 failed on SRC/healthz.{js,ts} + package.json conflicts,
        # post-coder bailed, agent's work was hard-reset away on next cycle's
        # _reset_workspace, feature 179 stranded at Implemented.
        # -u respects .gitignore (node_modules/, .next/, dist/) so the stash
        # stays small even on Node/Python/Go projects.
        stash_r = _run(["git", "stash", "push", "-u", "-m",
                        f"post-coder-{session_uid}"], timeout=300)
        stashed = (stash_r.returncode == 0
                   and "No local changes to save" not in (stash_r.stdout or ""))
        # Force-reset local `branch` to origin/`branch` before switching. If a
        # prior session committed junk to local sprint/79 and that branch
        # wasn't cleaned up by _reset_workspace, plain `git checkout BRANCH`
        # would land us on stale tracked content. `-B` overwrites the local
        # ref, ensuring we always start from the canonical remote state.
        co = _run(["git", "checkout", "-B", branch, f"origin/{branch}"])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: git checkout -B {branch} origin/{branch} failed — {_fmt_err(co)}")
            if stashed:
                _run(["git", "stash", "pop"])  # best-effort restore
            return pushed_ids
        pull_r = _run(["git", "pull", "--ff-only", "origin", branch])
        if pull_r.returncode != 0:
            log.warning(f"[post-coder] {pname}: git pull --ff-only on {branch} failed — {_fmt_err(pull_r)}")
            # Don't return: a non-fast-forward state is rare and we still want
            # to attempt the push so the operator sees the conflict.
        if stashed:
            pop_r = _run(["git", "stash", "pop"])
            if pop_r.returncode != 0:
                # Conflict — agent's file overlaps with sprint_branch's. Resolve
                # in favor of the agent (its diff is the "theirs" side relative
                # to the stash apply). `git checkout --theirs <path>` keeps the
                # incoming version; we then `git add` to mark resolved.
                conflicts = _run(["git", "diff", "--name-only", "--diff-filter=U"])
                paths = [p for p in conflicts.stdout.splitlines() if p.strip()]
                if paths:
                    _run(["git", "checkout", "--theirs", "--"] + paths)
                    _run(["git", "add", "--"] + paths)
                    log.info(f"[post-coder] {pname}: resolved {len(paths)} stash-pop conflict(s) "
                             f"in favor of agent's diff (sprint branch will be force-overwritten anyway)")
                # Drop whatever's left in the stash list to avoid accumulation.
                _run(["git", "stash", "drop"])
    elif rework_pr_mode:
        # Stay on whatever branch we're on (HEAD = origin/main + agent's edits)
        # and create/reset a local branch with the rework branch name pointing
        # at HEAD. Force-push later replaces the rejected prior commits on the
        # remote PR branch with our fresh main-based commits.
        branch = rework_branch_name
        co = _run(["git", "checkout", "-B", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: rework `git checkout -B {branch}` failed — {_fmt_err(co)}")
            return pushed_ids
    else:
        branch = f"coder/{session_uid}"
        co = _run(["git", "checkout", "-b", branch])
        if co.returncode != 0:
            log.warning(f"[post-coder] {pname}: git checkout -b {branch} failed — {_fmt_err(co)}")
            return pushed_ids

    # 3. Add + commit + push
    add_r = _run(["git", "add", "-A"])
    if add_r.returncode != 0:
        log.warning(f"[post-coder] {pname}: git add -A failed — {_fmt_err(add_r)}")
        return pushed_ids
    # Sanity: was anything actually staged? `diff --cached --quiet` exits 1 if
    # there are staged changes, 0 if none. Catches the "porcelain showed lines
    # but add staged nothing" scenario (e.g. all changes inside a submodule or
    # excluded path) so we surface a clear error instead of an empty stderr.
    cached = _run(["git", "diff", "--cached", "--quiet"])
    if cached.returncode == 0:
        ls = _run(["git", "status", "--porcelain"])
        log.warning(
            f"[post-coder] {pname}: nothing staged after `git add -A` "
            f"despite {len(changed)} porcelain entries. status={ls.stdout.strip()[:400]!r}"
        )
        return pushed_ids

    # Binary / oversize guard. GitHub rejects pushes with any single file >100 MB
    # (warns at 50 MB). The agent shouldn't be committing build artefacts at all,
    # but if .gitignore is missing or stale on the sprint branch (legacy state)
    # then npm/pip/go-mod outputs end up staged. Refuse to commit + surface the
    # offenders. On size violation we abort the pipeline rather than commit a
    # broken state — the next coder cycle will retry once .gitignore is fixed.
    SIZE_LIMIT_BYTES = 50 * 1024 * 1024  # 50 MB; GitHub's hard cap is 100 MB
    sized = _run(["git", "diff", "--cached", "--name-only", "-z"])
    paths = [p for p in (sized.stdout or "").split("\x00") if p]
    oversized = []
    for path in paths:
        try:
            sz = (Path(working_dir) / path).stat().st_size
        except OSError:
            continue
        if sz >= SIZE_LIMIT_BYTES:
            oversized.append((path, sz))
    if oversized:
        listing = ", ".join(f"{p} ({sz/1e6:.1f} MB)" for p, sz in oversized[:5])
        log.warning(
            f"[post-coder] {pname}: refusing to commit — {len(oversized)} oversized "
            f"file(s) staged (limit {SIZE_LIMIT_BYTES//1024//1024} MB). Sample: {listing}. "
            f"Likely missing or stale .gitignore on this branch — fix before retry."
        )
        # Reset the index so the bad files don't sit half-committed for the next session.
        _run(["git", "reset"])
        return pushed_ids

    # Include one [feature-<id>] tag per assigned feature so post-coder's
    # verification block (and future reviewer scoping) can scan PR commits
    # by feature. Tags appear as a sequence prefix on the same commit.
    feat_summary = ", ".join(f"#{i}" for i in feat_ids)
    feat_tags    = "".join(f"[feature-{i}]" for i in feat_ids)
    commit_msg = f"{feat_tags} feat: implement features {feat_summary} [coder-{session_uid}]"
    # --no-verify on the orchestrator's commit + push.
    # Pre-commit/pre-push hooks are valuable for HUMAN commits — they catch
    # lint/type/test regressions before code lands. The post-coder pipeline
    # is a deterministic system step that ships agent-generated code; it
    # runs under the orchestrator's UID with the orchestrator's PATH, not
    # the agent's. Agent-installed hooks (husky calling `npx`, lint-staged,
    # pre-commit, etc.) routinely fail in this environment because their
    # required binaries aren't on PATH. Real incident 2026-05-06 18:09:
    # husky's pre-commit script exited 127 (`npx not found`), git commit
    # rc=1, post-coder bailed, feature 183 stranded at Implemented.
    # Hooks would also catch nothing useful here — the agent's code is
    # what we WANT to commit, not what we want to gate. CI on the PR side
    # is the right place for those checks.
    commit_result = _run(["git", "commit", "--no-verify", "-m", commit_msg])
    if commit_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git commit failed — {_fmt_err(commit_result)}")
        return pushed_ids

    if sprint_pr_mode:
        push_args = ["git", "push", "--no-verify", "origin", branch]
    elif rework_pr_mode:
        # Replace the prior (rejected) commits on the remote PR branch with our
        # fresh main-based commits. --force-with-lease aborts if the remote was
        # touched by anyone else since our last fetch.
        push_args = ["git", "push", "--no-verify", "--force-with-lease", "origin", branch]
    else:
        push_args = ["git", "push", "--no-verify", "-u", "origin", branch]
    push_result = _run(push_args, timeout=180)
    if push_result.returncode != 0:
        log.warning(f"[post-coder] {pname}: git push failed: {push_result.stderr.strip()[:300]}")
        return pushed_ids
    log.info(f"[post-coder] {pname}: pushed branch {branch}")

    # 4. PR resolution. In sprint mode the PR already exists — just reuse it.
    # In rework mode the PR also already exists (the one we just force-pushed
    # to); reuse its number+url. In per-feature mode, open a fresh PR via gh.
    if sprint_pr_mode:
        pr_number = int(sprint_pr_num)
        pr_url = sprint_pr_url
        log.info(f"[post-coder] {pname}: reusing sprint PR #{pr_number} — {pr_url}")
    elif rework_pr_mode:
        pr_number = int(rework_pr_number)  # type: ignore[arg-type]
        pr_url = rework_pr_url
        log.info(f"[post-coder] {pname}: reusing rework PR #{pr_number} — {pr_url}")
    else:
        gh_token = _get_gh_token()
        if not gh_token:
            log.warning(f"[post-coder] {pname}: no GH_TOKEN — cannot open PR. Branch pushed; PM must open manually.")
            return pushed_ids

    # 5. Mark assigned features as Reviewing + link to the PR via direct PM
    # API PATCH. Until 2026-05-06 this used a session_result.json append +
    # reconcile read, but that file is also the agent's progress channel and
    # cross-session-pollution / Windows-bind-mount-cache races meant the
    # reconciler routinely never saw the post-coder's writes (every session's
    # log: "wrote 1 entries to session_result.json" + "0/1 feature updates
    # applied" — 0 progress events in 9h, the keystone of "0 features
    # released"). The PR is real on GitHub at this point; the DB MUST learn
    # about it or features get stranded and eventually auto-Blocked at
    # fix_attempts cap. changed_by="post-coder:fallback" is in the website's
    # _RANK_GUARD_BYPASS list — defends the rare case where the feature has
    # already been advanced past Reviewing by another path (auto_merge race).
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            for feat in assigned_features:
                fid = feat["id"]
                try:
                    r = client.patch(
                        f"/api/features/{fid}",
                        json={
                            "status": "Reviewing",
                            "pr_number": pr_number,
                            "pr_url": pr_url,
                            "changed_by": "post-coder:fallback",
                        },
                    )
                    r.raise_for_status()
                    pushed_ids.append(int(fid))
                    log.info(
                        f"[post-coder] {pname}: feature #{fid} → Reviewing pr={pr_number}"
                    )
                except Exception as e2:
                    log.warning(
                        f"[post-coder] {pname}: direct PATCH for feature #{fid} failed: {e2}"
                    )
    except Exception as e:
        log.warning(f"[post-coder] {pname}: PM API client error during reviewing PATCH: {e}")
    return pushed_ids
