"""
Reviewer-session auto-merge: for every approved feature in the session result,
attempt to squash-merge its PR on GitHub. This is the belt-and-braces second
layer mentioned in INVARIANTS.md VII.1 — the per-cycle ``auto_merge.sweep_all``
covers the steady-state case; this path is what reviewer sessions themselves
fall through into so the merge happens in the same cycle as the approval.

Behavior:
  - approved + already-merged on GitHub → mark Pushed
  - approved + PR closed (not merged)  → re-queue to Implementing
  - approved + PR open                 → update-branch, then squash-merge
                                         (handles draft PRs by polling until
                                         draft=false, retries merge once)
  - 405 with "draft"   → poll, then retry (NEVER close — destructive)
  - 405 / not mergeable → re-queue to Implementing with conflict comment

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os

import httpx

from orchestrator.integrations.github import _get_gh_token, _parse_repo_slug

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _auto_merge_approved(product: dict, features: list[dict]) -> list[dict]:
    """
    For reviewer sessions with auto_merge_enabled: merge every approved feature.
    - approved: attempt GitHub merge → update entry to Pushed
    - PR closed/conflicted: close PR, update entry to Implementing (clears pr_number)

    Takes the session features list, modifies entries in-place, and returns it
    so reconcile applies the final authoritative state.
    """
    gh_token = _get_gh_token()
    if not gh_token:
        log.warning("[auto-merge] No GitHub PAT configured — skipping auto-merge")
        return features

    github_repo = product.get("github_repo", "")
    if not github_repo:
        log.warning("[auto-merge] Product has no github_repo — skipping auto-merge")
        return features

    repo_slug = _parse_repo_slug(github_repo)
    gh_headers = {"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"}

    # Two-tier (session-PR) model note: features carry the **session** PR's
    # number, not the sprint integration PR. Merging an approved session PR
    # into the sprint branch accumulates that session's work onto the
    # sprint integration branch — it ships to main only when the sprint
    # completes (existing _do_complete_sprint path). So the per-reviewer-
    # session merge below is safe to run for every product: there is no
    # "shared PR across the sprint" risk that the old sprint-PR-mode short-
    # circuit defended against.

    for entry in features:
        if not isinstance(entry, dict) or entry.get("review_outcome") != "approved":
            continue

        fid = entry.get("id")
        pr_number = entry.get("pr_number")

        if not pr_number:
            # Fetch pr_number from DB if not in session entry
            try:
                with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                    resp = client.get(f"/api/features/{fid}")
                    resp.raise_for_status()
                    feat = resp.json()
                    pr_number = feat.get("pr_number")
                    # Fallback: parse from pr_url if pr_number column is null
                    if not pr_number and feat.get("pr_url"):
                        import re as _re
                        m = _re.search(r"/pull/(\d+)", feat["pr_url"])
                        if m:
                            pr_number = int(m.group(1))
                            log.info(f"[auto-merge] Feature #{fid}: resolved pr_number={pr_number} from pr_url")
                    entry["pr_number"] = pr_number
            except Exception as e:
                log.warning(f"[auto-merge] Could not fetch feature #{fid}: {e}")
                continue

        if not pr_number:
            log.warning(f"[auto-merge] Feature #{fid} has no PR number — skipping")
            continue

        # Check current PR state on GitHub before doing anything
        try:
            pr_resp = httpx.get(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}",
                headers=gh_headers, timeout=10,
            )
            if pr_resp.status_code == 200:
                pr_data = pr_resp.json()
                pr_state = pr_data.get("state")   # "open" | "closed"
                merged_at = pr_data.get("merged_at")  # None if not merged
            else:
                pr_state, merged_at = None, None
        except Exception as e:
            log.warning(f"[auto-merge] Could not check PR #{pr_number} state: {e}")
            pr_state, merged_at = None, None

        # Already merged on GitHub — just mark Pushed
        if merged_at:
            log.info(f"[auto-merge] PR #{pr_number} already merged — marking feature #{fid} Pushed")
            entry.update({"status": "Pushed", "pr_number": None})
            continue

        # PR closed but not merged — re-queue to Implementing
        if pr_state == "closed":
            log.info(f"[auto-merge] PR #{pr_number} closed (not merged) — re-queuing feature #{fid} to Implementing")
            entry.update({
                "status": "Implementing",
                "pr_number": None,
                "review_outcome": None,
                "review_notes": f"PR #{pr_number} was closed without merging — coder will rebase and reopen.",
            })
            continue

        # PR is open — attempt merge regardless of reviewer-reported confidence.
        # First, try to update the PR branch with main (GitHub's "Update branch"
        # button, REST endpoint /update-branch). If the PR branch is behind main
        # or has conflicts, this rebases/merges main into the branch so the
        # subsequent merge PUT succeeds. 422 = already up-to-date (ok to ignore).
        try:
            upd = httpx.put(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}/update-branch",
                headers=gh_headers, timeout=15,
            )
            if upd.status_code in (202, 200):
                log.info(f"[auto-merge] PR #{pr_number} branch updated — waiting 5s for GitHub to recompute mergeability")
                import time as _t
                _t.sleep(5)
            elif upd.status_code == 422:
                log.info(f"[auto-merge] PR #{pr_number} already up-to-date with base")
            else:
                log.warning(f"[auto-merge] update-branch returned {upd.status_code} for PR #{pr_number}: {upd.text[:120]}")
        except Exception as _ue:
            log.warning(f"[auto-merge] update-branch failed for PR #{pr_number}: {_ue} — proceeding to merge anyway")

        log.info(f"[auto-merge] Merging PR #{pr_number} for feature #{fid}")
        try:
            def _attempt_merge():
                return httpx.put(
                    f"https://api.github.com/repos/{repo_slug}/pulls/{pr_number}/merge",
                    json={"merge_method": "squash", "commit_title": f"feat: auto-merge PR #{pr_number} [ProductFactory]"},
                    headers=gh_headers, timeout=15,
                )

            resp = _attempt_merge()

            # Sprint PRs (Phase 6.1) are provisioned as drafts. GitHub returns 405
            # "Pull Request is still a draft" — that's NOT a conflict, just a state
            # we can fix. Mark ready, then POLL the PR until GitHub actually
            # reports draft=false (eventual consistency: observed today the PATCH
            # returned 200 immediately but a merge attempt 3s later still saw
            # the old draft state). Only retry the merge once draft has flipped.
            if resp.status_code == 405 and "draft" in resp.text.lower():
                log.info(f"[auto-merge] PR #{pr_number} is draft — marking ready and polling")
                from orchestrator.auto_merge import mark_ready_and_poll
                if mark_ready_and_poll(repo_slug, pr_number, gh_headers):
                    resp = _attempt_merge()
                else:
                    # Polling timed out — GitHub still reports draft. Skip this
                    # cycle rather than fall through to close-as-conflict (which
                    # would destroy the PR). Next cycle will retry from scratch.
                    log.warning(f"[auto-merge] PR #{pr_number} draft state did not clear in 30s — skipping (will retry next cycle)")
                    continue

            if resp.status_code in (200, 201):
                log.info(f"[auto-merge] PR #{pr_number} merged successfully")
                entry.update({"status": "Pushed", "pr_number": None})
            else:
                if "application/json" in resp.headers.get("content-type", ""):
                    body = resp.json()
                    gh_msg = body.get("message", resp.text[:120]) if isinstance(body, dict) else resp.text[:120]
                else:
                    gh_msg = resp.text[:120]
                log.warning(f"[auto-merge] GitHub {resp.status_code} for PR #{pr_number}: {gh_msg}")

                # SAFETY (#7-followup): only close on REAL conflicts. If 405 still
                # mentions draft (somehow slipped through the polling above), do
                # NOT close — that's the destructive bug we keep stepping on.
                if resp.status_code == 405 and "draft" in gh_msg.lower():
                    log.warning(f"[auto-merge] PR #{pr_number} still reports draft after polling — skipping (no destructive close)")
                    continue
                if resp.status_code == 405 or "not mergeable" in gh_msg.lower():
                    # Conflicts — re-queue the feature for the coder to rebase.
                    # Keep pr_number set and add a review comment to the PR
                    # explaining the bounce; the coder's rework path (Fix #2 in
                    # _run_post_coder_pipeline) will detect the existing PR and
                    # force-push fresh main-based commits to its branch.
                    # IMPORTANT: review_outcome must be "changes_requested" so
                    # the dispatcher's codeable check matches and the next coder
                    # cycle picks this feature up immediately. Setting it to
                    # None would leave the feature in in_agent_stuck for ~45min
                    # until reset_stuck nudged it.
                    httpx.post(
                        f"https://api.github.com/repos/{repo_slug}/issues/{pr_number}/comments",
                        json={"body": (
                            f"Auto-merge bounced ({resp.status_code}, {gh_msg}). "
                            f"Re-queuing feature(s) {fid} for coder rebase. The PR "
                            f"is left open — the coder will force-push fresh commits."
                        )},
                        headers=gh_headers, timeout=10,
                    )
                    entry.update({
                        "status": "Implementing",
                        "review_outcome": "changes_requested",
                        "review_notes": f"Merge failed: conflicts (GitHub {resp.status_code}). Coder must rebase main.",
                    })
        except Exception as e:
            log.warning(f"[auto-merge] Error merging PR #{pr_number}: {e}")

    return features
