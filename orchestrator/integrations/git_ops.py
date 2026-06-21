"""
Git workspace operations the orchestrator runs against a product's working
directory: pre-session reset, sprint-branch pre-checkout, post-session cleanup.

Provides a single ``safe_run`` subprocess wrapper that replaces four nearly-
identical ``_run`` helpers that had drifted across docker_runner.py over time.
The unified version:
  - always returns a ``subprocess.CompletedProcess`` (timeouts are translated
    into a synthetic ``returncode=124`` so callers can branch on returncode
    uniformly without try/except)
  - preserves whatever stdout the timed-out process emitted before being killed
  - optionally logs a warning when a timeout fires (controlled by ``log_label``
    so silent legacy call sites stay silent)

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import re
import subprocess
import time
from pathlib import Path

from orchestrator.integrations.docker_cli import _chmod_workspace_via_alpine

log = logging.getLogger("poller.docker")


def _remove_stale_git_lock(working_dir: str, product_name: str, max_age_seconds: int = 60) -> None:
    """
    Remove ``.git/index.lock`` if it's older than ``max_age_seconds``.

    Git creates ``.git/index.lock`` during writes (commit, checkout, reset, clean)
    and removes it on completion. A crashed or killed git process leaves a stale
    lock that blocks every subsequent git operation with
    ``Unable to create '.git/index.lock': File exists`` — and the entire
    ``_reset_workspace`` cascade silently fails.

    Real incident 2026-05-05: an index.lock from 11:28 (a previously-crashed
    session) blocked every subsequent coder session for the rest of the day.
    The agent's ``git status`` calls timed out, ``_agent_made_edits`` repeatedly
    fail-closed, and the agent burned 200 turns without making progress.

    Conservative threshold: 60s. A live git process holding the lock should
    finish in seconds; anything older is necessarily abandoned.
    """
    lock_path = Path(working_dir) / ".git" / "index.lock"
    if not lock_path.exists():
        return
    try:
        age = time.time() - lock_path.stat().st_mtime
        if age < max_age_seconds:
            log.info(f"[{product_name}] .git/index.lock present but age={int(age)}s — leaving (may be live)")
            return
        lock_path.unlink()
        log.warning(f"[{product_name}] removed stale .git/index.lock (age={int(age)}s)")
    except Exception as e:
        log.warning(f"[{product_name}] could not check/remove .git/index.lock: {e}")


def _git_with_token(
    verb: str,
    args: list[str],
    *,
    cwd: str | Path,
    product_name: str,
    timeout: int,
) -> subprocess.CompletedProcess:
    """Run ``git <verb> <args...>`` with a GitHub App installation token.

    Shared body for git CLI operations that go over the network (push,
    fetch, pull). Under HTTPS auth, every one of those needs credentials —
    the SSH path used to auto-supply them via the deploy key; now we wire
    them through the App.

    Auth flow:
      - Fetch a fresh installation token via the App module (falls back to
        the legacy PAT if the App isn't fully configured).
      - Set ``GITHUB_TOKEN`` in the subprocess env only — never on the parent.
      - Pass a one-shot ``credential.helper`` via ``git -c`` that reads the
        env var. The helper string never contains the token, so the token
        does NOT appear in ``ps``, in ``docker inspect``, or in ``.git/config``.

    Returns ``CompletedProcess`` with the same shape as ``safe_run`` so
    callers can branch on returncode uniformly.
    """
    from orchestrator.integrations.github import _get_gh_token  # late: avoids circular import
    token = _get_gh_token() or ""
    if not token:
        log.warning(f"[{product_name}] git {verb}: no token available — operation will fail under HTTPS auth")

    # `credential.helper=` (empty) clears every helper inherited from
    # /etc/gitconfig and ~/.gitconfig — defends against a stale `store`
    # helper (or any other persistent helper) intercepting the request
    # before our one-shot runs. Without the clear, git would call helpers
    # in order and the first to return credentials wins; a `store` helper
    # backed by a revoked PAT would silently lose us the auth flow.
    # Then our one-shot helper is the ONLY helper git knows about for
    # this command.
    helper = '!f() { echo "username=x-access-token"; echo "password=$GITHUB_TOKEN"; }; f'
    cmd = [
        "git",
        "-c", "credential.helper=",
        "-c", f"credential.helper={helper}",
        verb, *args,
    ]
    env = {**os.environ, "GITHUB_TOKEN": token}

    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as te:
        log.warning(f"[{product_name}] git {verb} timed out after {timeout}s")
        return subprocess.CompletedProcess(
            cmd, returncode=124,
            stdout=(te.stdout.decode() if isinstance(te.stdout, bytes) else (te.stdout or "")),
            stderr=f"timed out after {timeout}s",
        )


def git_push_authenticated(
    push_args: list[str],
    *,
    cwd: str | Path,
    product_name: str,
    timeout: int = 180,
) -> subprocess.CompletedProcess:
    """``git push <push_args...>`` with App-token auth. See ``_git_with_token``."""
    return _git_with_token("push", push_args, cwd=cwd, product_name=product_name, timeout=timeout)


def git_fetch_authenticated(
    fetch_args: list[str],
    *,
    cwd: str | Path,
    product_name: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """``git fetch <fetch_args...>`` with App-token auth. See ``_git_with_token``.

    Before the App migration, the orchestrator's host-side fetches relied on
    the SSH deploy key being auto-used; under HTTPS that path silently failed
    with ``could not read Username for 'https://github.com'``. Wire fetch
    callsites through this helper to restore the cred path.
    """
    return _git_with_token("fetch", fetch_args, cwd=cwd, product_name=product_name, timeout=timeout)


def safe_run(
    cmd: list[str],
    *,
    cwd: str | Path,
    timeout: int = 60,
    log_label: str | None = None,
) -> subprocess.CompletedProcess:
    """
    Run a subprocess with a hard timeout. Returns CompletedProcess always —
    timeouts are translated to ``returncode=124`` and ``stderr="timed out
    after Ns"`` so callers branch on returncode uniformly.

    If ``log_label`` is provided and the call times out, emit a warning
    line tagged with the label (matches legacy behaviour where some call
    sites logged and some didn't).
    """
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as te:
        if log_label:
            log.warning(f"[{log_label}] {' '.join(cmd)} timed out after {timeout}s")
        return subprocess.CompletedProcess(
            cmd, returncode=124,
            stdout=(te.stdout.decode() if isinstance(te.stdout, bytes) else (te.stdout or "")),
            stderr=f"timed out after {timeout}s",
        )


def _enforce_https_origin(working_dir: str, product_name: str) -> None:
    """Re-point origin at the canonical ``https://github.com/<slug>.git`` URL.

    Successor to ``_enforce_ssh_origin``. Under the GitHub App auth model,
    pushes authenticate via a short-lived installation token delivered at
    runtime — there's no per-product SSH key to mount, no host SSH config
    aliases, no chmod sidecar.

    Two contracts this function preserves:

      1. **Token is never written to ``.git/config``.** The token is
         supplied per-invocation via the ``GITHUB_TOKEN`` env var, read by
         a one-shot ``credential.helper`` invoked by ``git push``. If the
         workspace folder is ever exfiltrated, no credential leaks with it.

      2. **Idempotent + tolerant of pre-existing legacy URLs.** Any prior
         SSH origin (``git@github.com-foo:owner/repo.git``) is rewritten
         to the HTTPS form. Any HTTPS origin that already has a token
         embedded inline (legacy ``https://x-access-token:...@github.com/...``
         created before this function existed) is normalized back to the
         token-less form.
    """
    wd = Path(working_dir)
    try:
        cur = safe_run(["git", "remote", "get-url", "origin"], cwd=wd, log_label=product_name)
        cur_url = (cur.stdout or "").strip()
        if not cur_url:
            return
        m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", cur_url)
        if not m:
            return
        slug = m.group(1)
        expected_url = f"https://github.com/{slug}.git"
        if cur_url == expected_url:
            return
        r = safe_run(
            ["git", "remote", "set-url", "origin", expected_url],
            cwd=wd, log_label=product_name,
        )
        if r.returncode == 0:
            # Redact any inline token in the prior URL before logging.
            redacted = re.sub(r":[^:@/]+@", ":***@", cur_url)
            log.info(
                f"[{product_name}] origin re-pointed at HTTPS "
                f"({expected_url}) (was: {redacted})"
            )
        else:
            log.warning(
                f"[{product_name}] _enforce_https_origin set-url failed: "
                f"{r.stderr.strip()[:200]}"
            )
    except Exception:
        log.exception(f"[{product_name}] _enforce_https_origin crashed")


def _reset_workspace(working_dir: str, product_name: str) -> None:
    """
    Sync the product workspace to the latest state on origin/main before each session.
    Uses git fetch + reset --hard (not pull --ff-only) so it succeeds even if local
    has diverged from remote (e.g. partial commits from a crashed previous session).
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return  # Not a git repo yet — skip

    # Fix workspace perms so the orchestrator (different uid than the agent's
    # 1001) can reset/checkout/clean. See _chmod_workspace_via_alpine for why
    # we have to detour through a host-docker alpine sidecar on Windows.
    _chmod_workspace_via_alpine(working_dir, product_name)

    # Clear any stale .git/index.lock from a previous crashed/killed session.
    # Without this, every subsequent git operation in this workspace fails
    # with "Unable to create index.lock: File exists" — silently breaking
    # the rest of this _reset_workspace cascade. See helper docstring.
    _remove_stale_git_lock(working_dir, product_name)

    # Prune orphaned git worktrees BEFORE the `checkout main` below. The
    # post-coder baseline-test step creates a throwaway worktree off origin/main
    # (orchestrator/pipelines/post_coder.py::_baseline_pytest_failures). If its
    # `git worktree remove` cleanup never runs — the orchestrator container is
    # recreated mid-pipeline, the session is killed, or legacy code left one
    # behind — the worktree DIRECTORY (in the container's ephemeral /tmp) is
    # gone but its metadata persists in the mounted repo's .git/worktrees/,
    # still HOLDING the branch it checked out. A worktree holding `main` then
    # makes every `git checkout -f main` fail with rc=128 ("'main' is already
    # used by worktree at ...") — silently breaking the entire reset cascade, so
    # the workspace is never cleaned and stale/cross-feature state accumulates.
    # Canonical: DogTinder /tmp/maincheck (legacy) wedged resets for ~8h on
    # 2026-06-21, fuelling the #1872/#1881/#1892 contamination. `prune` only
    # removes entries whose working dir is missing, so an in-use worktree is
    # never touched. Best-effort; never raise.
    safe_run(["git", "worktree", "prune", "-v"], cwd=wd, log_label=product_name)

    # Enforce HTTPS origin under the GitHub App auth model. Catches any
    # workspace that ended up with a stale SSH origin (from legacy products
    # migrated mid-flight, or `gh repo clone` defaults) and rewrites it back
    # to the canonical token-less HTTPS URL. The actual token is supplied
    # at push time via the GITHUB_TOKEN env var + a one-shot credential
    # helper — not persisted to .git/config — so this rewrite leaks no secret
    # even if the workspace is later exfiltrated.
    _enforce_https_origin(working_dir, product_name)

    # 1. Fetch latest from origin (updates remote-tracking refs, prunes deleted branches)
    # Longer timeout — fetch can legitimately take a while on slow networks.
    # git_fetch_authenticated supplies the App token via credential.helper —
    # bare safe_run(["git", "fetch", ...]) used to work because the SSH key was
    # auto-used; under HTTPS auth fetch needs explicit creds.
    r = git_fetch_authenticated(["origin", "--prune"], cwd=wd, product_name=product_name, timeout=120)
    if r.returncode != 0:
        log.warning(f"[{product_name}] git fetch failed: {r.stderr.strip()[:200]}")

    # 2. Switch to main (or master) — abandon any half-baked feature branch.
    #    Use -f to discard tracked-file changes from a prior session that didn't
    #    clean up; the subsequent `git reset --hard` and `git clean` would have
    #    discarded them anyway. Without -f, dirty trees from Path B sessions
    #    block the switch and the entire reset is silently skipped.
    main_branch: str | None = None
    for branch in ("main", "master"):
        r = safe_run(["git", "checkout", "-f", branch], cwd=wd, log_label=product_name)
        if r.returncode == 0:
            main_branch = branch
            break
    if not main_branch:
        log.warning(f"[{product_name}] Could not checkout main/master — workspace reset skipped")
        return

    # 3. Hard-reset to origin — discards any local commits or staged changes
    r = safe_run(["git", "reset", "--hard", f"origin/{main_branch}"], cwd=wd, log_label=product_name)
    if r.returncode != 0:
        log.warning(f"[{product_name}] git reset --hard failed: {r.stderr.strip()[:200]}")

    # 4. Remove untracked and ignored files (session artifacts, .pyc, etc.)
    #    Preserve:
    #      - output/ and Results/ — artefacts the PM cares about
    #      - Temp/ — scratch the agent may want within a session
    #      - session_summary.md — the cross-session continuity doc. It's
    #        gitignored (never on the remote), so without this exclude
    #        `git clean -fdx` wiped it on every reset, BEFORE
    #        docker_runner._read_session_summary reads it to build the
    #        next session's {prev_session_summary}. Net effect: every
    #        session got an EMPTY prior-summary and improvised without
    #        continuity. Excluding it here is what makes the continuity
    #        mechanism actually work. (session_result.json is deliberately
    #        NOT excluded — it's a per-session output channel the
    #        orchestrator drains each cycle, not cross-session state.)
    safe_run(
        ["git", "clean", "-fdx",
         "--exclude=output/", "--exclude=Results/", "--exclude=Temp/",
         "--exclude=session_summary.md"],
        cwd=wd, log_label=product_name,
    )

    # 5. Delete stale local feature branches (not main/master)
    r = safe_run(["git", "branch"], cwd=wd, log_label=product_name)
    for line in r.stdout.splitlines():
        branch = line.strip().lstrip("* ")
        if branch and branch not in ("main", "master"):
            safe_run(["git", "branch", "-D", branch], cwd=wd, log_label=product_name)
            log.info(f"[{product_name}] Deleted stale local branch: {branch}")

    log.info(f"[{product_name}] Workspace synced to origin/{main_branch} (hard reset)")


def _checkout_branch(working_dir: str, branch: str, product_name: str) -> bool:
    """
    Pre-checkout the given branch before launching the agent so the agent's
    very first tool call lands on the right branch regardless of whether it
    follows the prompt's MANDATORY-FIRST-ACTION instruction.

    Under the 1-PR model this is the reviewer's session branch (the head of
    the open session PR they're reviewing). Runs after `_reset_workspace`
    (which leaves us on main) and assumes the branch already exists on
    origin.

    Returns True on success. On failure logs a warning and returns False —
    caller should leave the agent on `main` and rely on the post-coder
    pipeline's own checkout to recover, but flag this loudly so the operator
    knows the branch wasn't pre-set.
    """
    if not branch:
        return False
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        log.warning(f"[{product_name}] pre-checkout: not a git repo, skipping")
        return False

    # Fetch first so the remote ref is current. _reset_workspace already
    # fetched, but it was for origin/main with --prune; the target ref may
    # not have existed at fetch time if just opened.
    r = git_fetch_authenticated(
        ["origin", branch], cwd=wd, product_name=product_name, timeout=60,
    )
    if r.returncode != 0:
        log.warning(
            f"[{product_name}] pre-checkout: fetch origin {branch} failed: "
            f"{r.stderr.strip()[:200]}"
        )
        return False

    # Check it out as a tracking branch. -B forces creation/reset so we always
    # end up on a clean local branch tracking origin/<branch>.
    r = safe_run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=wd)
    if r.returncode != 0:
        log.warning(
            f"[{product_name}] pre-checkout: checkout {branch} failed: "
            f"{r.stderr.strip()[:200]}"
        )
        return False

    log.info(f"[{product_name}] pre-checkout: now on {branch}")
    return True


def _cleanup_workspace_post_session(working_dir: str, product_name: str) -> None:
    """
    Post-exit cleanup: return to main branch and remove uncommitted session artifacts.
    Runs after container exits (success or failure) so the next session starts clean.
    Does NOT delete the working directory — git history and pushed branches are preserved.
    """
    wd = Path(working_dir)
    if not (wd / ".git").exists():
        return

    # Return to main branch (agent may have left us on a feature branch)
    for branch in ("main", "master"):
        if safe_run(["git", "checkout", branch], cwd=wd, log_label=product_name).returncode == 0:
            break

    # Remove any files the agent created but didn't commit/push
    # Keep docs/, Results/, Temp/ — those may contain artefacts the PM cares about
    safe_run(
        ["git", "clean", "-fd", "--exclude=docs/", "--exclude=Results/", "--exclude=Temp/"],
        cwd=wd, log_label=product_name,
    )
    log.info(f"[{product_name}] Post-session workspace cleanup complete")
