"""
GitHub API client — used by the poller for PR management.

Handles:
  - count_open_prs()            PR count gate (pause if ≥3 open)
  - reconcile_merged_prs()      sync merged/closed PRs → DB feature status
  - reconcile_in_flight_prs()   check every in-flight feature's PR against GitHub
"""

import os
import logging
import time

import httpx

from orchestrator.integrations.github import _parse_repo_slug

log = logging.getLogger("poller.github")

# After this many "PR closed without merge" or "Reviewing → Implementing" reset
# events on the same feature, the reconciler stops retrying and marks the feature
# Blocked so a human can intervene. Prevents infinite review/fix loops.
#
# Default sourced from env at import; `_resolve_max_fix_attempts` below reads
# the live value from system_config on each cycle so PM-driven rotations
# (without an orchestrator restart) take effect immediately.
MAX_FIX_ATTEMPTS = int(os.environ.get("MAX_FIX_ATTEMPTS", "5"))


def _resolve_max_fix_attempts() -> int:
    """Read max_fix_attempts from system_config; fall back to env constant.

    Cheap one-shot HTTP call against the PM API (already a per-cycle
    dependency for everything in this module). Never fails the caller —
    on any error we return the env-default constant so the reconcile
    sweep keeps working.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            if resp.status_code == 200:
                val = (resp.json() or {}).get("max_fix_attempts")
                if isinstance(val, int) and val > 0:
                    return val
    except Exception:
        pass
    return MAX_FIX_ATTEMPTS


def _gh_get(url: str, headers: dict, params: dict | None = None, timeout: int = 15) -> httpx.Response | None:
    """
    GET from the GitHub API with automatic retry on 429/403 rate-limit responses.
    Returns the response on success (2xx), None on permanent failure.
    Retries up to 3 times with exponential back-off honouring Retry-After when present.
    """
    for attempt in range(3):
        try:
            resp = httpx.get(url, params=params, headers=headers, timeout=timeout)
        except Exception as e:
            log.warning(f"GitHub GET {url} attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
            continue

        if resp.status_code in (429, 403):
            retry_after = int(resp.headers.get("Retry-After", 0))
            wait = retry_after if retry_after > 0 else (2 ** attempt * 5)
            log.warning(f"GitHub rate-limited ({resp.status_code}) — waiting {wait}s before retry {attempt + 1}/3")
            time.sleep(wait)
            continue

        return resp

    log.warning(f"GitHub GET {url} failed after 3 attempts (rate-limited)")
    return None

PM_API_URL = os.environ["PM_API_URL"]

def _get_auth_token() -> str:
    """Return a fresh GitHub App installation token, or ``""``.

    Thin str-return alias over the canonical
    ``orchestrator.integrations.github._get_gh_token`` (which returns
    ``str | None``). Kept because callers in this module do
    ``f"Bearer {tok}"`` and need a string fallback rather than ``None``.
    Test patches that target ``orchestrator.github_client._get_auth_token``
    continue to work unchanged.
    """
    from orchestrator.integrations.github import _get_gh_token
    return _get_gh_token() or ""


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = _get_auth_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    return headers


def count_open_prs(product: dict) -> int:
    """Returns number of open PRs for this product's repo."""
    slug = _parse_repo_slug(product.get("github_repo", ""))
    if not slug:
        return 0
    try:
        resp = _gh_get(
            f"https://api.github.com/repos/{slug}/pulls",
            params={"state": "open", "per_page": 10},
            headers=_github_headers(),
        )
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            return len(data) if isinstance(data, list) else 0
    except Exception as e:
        log.warning(f"count_open_prs failed: {e}")
    return 0


def reconcile_merged_prs(product: dict):
    """
    Fetch recently closed PRs from GitHub and sync feature status in DB.
    - Merged PRs   → feature status Pushed
    - Closed (not merged) PRs → feature reset to Approved/Implementing so coder retries

    Also handles features where pr_number is null but pr_url contains the PR link.
    """
    slug = _parse_repo_slug(product.get("github_repo", ""))
    if not slug:
        return

    try:
        resp = _gh_get(
            f"https://api.github.com/repos/{slug}/pulls",
            params={"state": "closed", "per_page": 20},
            headers=_github_headers(),
        )
        if resp is None or resp.status_code != 200:
            return

        prs_data = resp.json()
        if not isinstance(prs_data, list):
            log.warning("reconcile_merged_prs: unexpected response shape from GitHub")
            return

        merged_numbers  = {pr["number"] for pr in prs_data if pr.get("merged_at")}
        closed_numbers  = {pr["number"] for pr in prs_data if not pr.get("merged_at")}

        if not merged_numbers and not closed_numbers:
            return

        if merged_numbers:
            log.info(f"Reconciling merged PRs: {sorted(merged_numbers)}")
        if closed_numbers:
            log.info(f"Reconciling closed (unmerged) PRs: {sorted(closed_numbers)}")

        import re as _re

        def _pr_number_for(feature: dict) -> int | None:
            """Return the PR number from pr_number column or parsed from pr_url."""
            n = feature.get("pr_number")
            if n:
                return int(n)
            url = feature.get("pr_url") or ""
            m = _re.search(r"/pull/(\d+)", url)
            return int(m.group(1)) if m else None

        with httpx.Client(base_url=PM_API_URL) as client:
            all_features = client.get(
                f"/api/products/{product['id']}/features",
            )
            if all_features.status_code != 200:
                fallback = client.get(
                    "/api/features/approved",
                    params={"product_id": product["id"]},
                )
                fallback.raise_for_status()
                features_data = fallback.json()
            else:
                features_data = all_features.json()

            if not isinstance(features_data, list):
                log.warning("reconcile_merged_prs: unexpected features response shape")
                return

            terminal = {"Pushed", "Rejected", "Reverted", "Deferred"}

            for feature in features_data:
                if feature.get("status") in terminal:
                    continue
                # Note: prior versions re-fetched the feature here as a
                # "race condition guard" against the status moving to
                # terminal between the bulk fetch above and reaching this
                # row — the same ``terminal`` check on the same record
                # ran 6 lines earlier and the re-fetch repeated it. The
                # PATCH below is idempotent (a Pushed feature stays Pushed
                # under both branches), so the duplicate guard was paying
                # one extra API hit per non-terminal feature for no real
                # safety. Removed in the drift-cleanup pass.
                pr_n = _pr_number_for(feature)
                if pr_n is None:
                    continue

                if pr_n in merged_numbers:
                    client.patch(
                        f"/api/features/{feature['id']}",
                        json={"status": "Pushed", "pr_number": None},
                    )
                    log.info(f"Reconciled feature {feature['id']} → Pushed (PR #{pr_n} merged)")

                elif pr_n in closed_numbers:
                    # PR closed without merging — reset so coder can retry
                    reset_status = "Approved"
                    if feature.get("status") == "Reviewing":
                        reset_status = "Implementing"  # keep coder loop, not restart from scratch
                    client.patch(
                        f"/api/features/{feature['id']}",
                        json={"status": reset_status, "pr_number": None, "pr_url": None, "branch_name": None},
                    )
                    log.info(f"Reconciled feature {feature['id']} → {reset_status} (PR #{pr_n} closed without merge)")

    except Exception as e:
        log.warning(f"reconcile_merged_prs failed: {e}")


def reconcile_in_flight_prs(product: dict):
    """
    For every in-flight feature (Implementing/Reviewing/Reviewed) that has a
    pr_url or pr_number, check the actual PR state on GitHub and fix any
    DB/GitHub mismatch immediately — without waiting for reset_stuck timeout.

    Called every poller cycle (coder path) so stuck features self-heal within
    one poll interval (~60s) instead of waiting up to 2 hours.
    """
    slug = _parse_repo_slug(product.get("github_repo", ""))
    if not slug:
        return

    import re as _re

    # Statuses to scan for: anything that may have a PR attached but is not terminal.
    # Designed is included so a coder that opened a PR before advancing status is healed.
    IN_FLIGHT = {"Designed", "Implementing", "Reviewing", "Reviewed"}

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            resp = client.get(f"/api/products/{product['id']}/features")
            if resp.status_code != 200:
                return
            features_data = resp.json()
            if not isinstance(features_data, list):
                return

            # Collect features that have a PR reference
            candidates = []
            for f in features_data:
                if f.get("status") not in IN_FLIGHT:
                    continue
                pr_n = f.get("pr_number")
                if not pr_n:
                    url = f.get("pr_url") or ""
                    m = _re.search(r"/pull/(\d+)", url)
                    if m:
                        pr_n = int(m.group(1))
                if pr_n:
                    candidates.append((f, int(pr_n)))

            # Migration sweep runs BEFORE the in-flight PR reconciliation
            # because products with no in-flight PRs (everything Approved or
            # Blocked, no Reviewing/Implementing) would otherwise short-
            # circuit out via the candidates-empty early return below.
            stranded = [
                f["id"] for f in features_data
                if f.get("status") == "Blocked" and f.get("sprint_id") is not None
            ]
            if stranded:
                try:
                    resp = client.post(
                        f"/api/products/{product['id']}/sprints/blocked/route",
                        json={
                            "feature_ids": stranded[:20],
                            "reason": "Migrated from legacy Blocked state",
                        },
                    )
                    # Only log when a real transition happened. The route
                    # endpoint short-circuits idempotent re-routes (already in
                    # the Blocked sprint), so a stranded list of N can yield
                    # 0 actual moves — no need to flood the orchestrator log
                    # with "routed 3 stranded Blocked features" every cycle.
                    moved = (resp.json() or {}).get("moved", 0) if resp.status_code == 200 else 0
                    if moved:
                        log.info(f"[in-flight] migration sweep: routed {moved} stranded Blocked feature(s)")
                except Exception as re:
                    log.warning(f"[in-flight] migration sweep failed: {re}")

            if not candidates:
                return

            gh_headers = _github_headers()
            for feature, pr_n in candidates:
                fid = feature["id"]
                try:
                    pr_resp = _gh_get(
                        f"https://api.github.com/repos/{slug}/pulls/{pr_n}",
                        headers=gh_headers,
                    )
                    if pr_resp is None or pr_resp.status_code != 200:
                        continue
                    pr = pr_resp.json()
                    if not isinstance(pr, dict):
                        continue
                except Exception as e:
                    log.debug(f"[in-flight] Could not fetch PR #{pr_n}: {e}")
                    continue

                state         = pr.get("state")      # "open" | "closed"
                merged_at     = pr.get("merged_at")  # None if not merged
                cur_status    = feature.get("status")
                has_review    = bool(feature.get("review_outcome"))

                # Merged terminal: Pushed always wins (PR is gone, no agent can disagree).
                if merged_at and cur_status != "Pushed":
                    client.patch(f"/api/features/{fid}",
                                 json={"status": "Pushed", "pr_number": None})
                    log.info(f"[in-flight] Feature #{fid} → Pushed (PR #{pr_n} already merged)")
                    continue

                # Closed-without-merge: PR is gone, agent decisions are moot. Reset.
                if state == "closed" and not merged_at:
                    new_attempts = (feature.get("fix_attempts") or 0) + 1
                    if new_attempts >= _resolve_max_fix_attempts():
                        client.patch(f"/api/features/{fid}", json={
                            "status": "Blocked",
                            "pr_number": None, "pr_url": None, "branch_name": None,
                            "fix_attempts": new_attempts,
                            "blocked_reason": (
                                f"Auto-blocked: PR #{pr_n} closed without merge after "
                                f"{new_attempts} attempts. Needs human review."
                            ),
                        })
                        # Route to the per-product Blocked sprint so the PM
                        # dashboard surfaces it for triage instead of
                        # leaving it stranded on its original delivery
                        # sprint (where it would otherwise contribute to
                        # all_features_done=false and stall DoD).
                        try:
                            client.post(
                                f"/api/products/{product['id']}/sprints/blocked/route",
                                json={
                                    "feature_ids": [fid],
                                    "reason": (
                                        f"Auto-escalated after {new_attempts} closed-PR attempts "
                                        f"(last PR #{pr_n})"
                                    ),
                                },
                            )
                        except Exception as re:
                            log.warning(f"[in-flight] route to Blocked sprint failed for #{fid}: {re}")
                        log.warning(
                            f"[in-flight] Feature #{fid} → Blocked sprint "
                            f"(fix_attempts={new_attempts} ≥ {MAX_FIX_ATTEMPTS})"
                        )
                    else:
                        reset = "Implementing" if cur_status == "Reviewing" else "Approved"
                        client.patch(f"/api/features/{fid}", json={
                            "status": reset,
                            "pr_number": None, "pr_url": None, "branch_name": None,
                            "fix_attempts": new_attempts,
                        })
                        log.info(
                            f"[in-flight] Feature #{fid} → {reset} "
                            f"(PR #{pr_n} closed without merge, attempt {new_attempts})"
                        )
                    continue

                # Open PR + non-terminal disagreement: only advance if no agent
                # decision exists. Once review_outcome is set, the reviewer's
                # status is authoritative until something terminal happens to the PR.
                if state == "open" and not has_review:
                    if cur_status in ("Implementing", "Designed"):
                        client.patch(f"/api/features/{fid}", json={"status": "Reviewing"})
                        log.info(
                            f"[in-flight] Feature #{fid} → Reviewing "
                            f"(PR #{pr_n} open, status was {cur_status})"
                        )

    except Exception as e:
        log.warning(f"reconcile_in_flight_prs failed: {e}")


def reconcile_orphaned_session_prs(product: dict):
    """Close GitHub PRs in the coder/<uid> namespace that no feature references.

    The single source of truth for "PR should be open" is the features table:
    if a session PR exists on GitHub but no feature row has pr_number == that
    PR number, the PR is orphaned. Possible causes (all caught by this sweep):

      1. post_coder.py opens the PR at line ~1570, then the lint or test guard
         at line ~1595 / ~1664 rejects the commit and returns early without
         writing pr_number to the DB (the PATCH that links the feature is at
         line ~1793, never reached on gate failure).
      2. supervisor.detect_rapid_flap routes a feature to Blocked while a
         coder session is still running on the same product; post-coder then
         opens a PR at session-end for an already-Blocked feature.
      3. website._close_blocked_feature_pr crashed or returned False on a
         Blocked-route, leaving the GitHub PR open after pr_number was cleared.
      4. Any future leak vector — this sweep is invariant-based, not
         event-based, so new bugs land in the same bucket and self-heal.

    Three safety gates make closing safe:
      - Namespace: head.ref starts with "coder/" (our session-branch prefix).
        Never touches main, feature/, hotfix/, etc.
      - Authorship: user.type == "Bot" (the GitHub App opened it). Excludes
        manual PRs on the same branch namespace.
      - Grace window: PR.created_at is older than ORPHAN_GRACE_SECONDS.
        Absorbs the small gap between gh pr create (post_coder.py:1570) and
        the post-coder PATCH that writes pr_number (post_coder.py:1793),
        so we don't close a PR mid-handoff.
    """
    ORPHAN_GRACE_SECONDS = 90

    slug = _parse_repo_slug(product.get("github_repo", ""))
    if not slug:
        return

    resp = _gh_get(
        f"https://api.github.com/repos/{slug}/pulls",
        params={"state": "open", "per_page": 50},
        headers=_github_headers(),
    )
    if resp is None or resp.status_code != 200:
        return
    open_prs = resp.json()
    if not isinstance(open_prs, list) or not open_prs:
        return

    from datetime import datetime, timezone, timedelta
    grace_cutoff = datetime.now(timezone.utc) - timedelta(seconds=ORPHAN_GRACE_SECONDS)

    candidates: list[dict] = []
    for pr in open_prs:
        head_ref = ((pr.get("head") or {}).get("ref") or "")
        if not head_ref.startswith("coder/"):
            continue
        user = pr.get("user") or {}
        if (user.get("type") or "") != "Bot":
            continue
        created_raw = pr.get("created_at") or ""
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            continue
        if created > grace_cutoff:
            continue
        candidates.append(pr)

    if not candidates:
        return

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            feat_resp = client.get(f"/api/products/{product['id']}/features")
            if feat_resp.status_code != 200:
                log.warning(
                    f"reconcile_orphaned_session_prs: features fetch failed "
                    f"({feat_resp.status_code}) for product {product.get('id')}"
                )
                return
            features = feat_resp.json()
            if not isinstance(features, list):
                return
    except Exception as e:
        log.warning(f"reconcile_orphaned_session_prs: PM client error: {e}")
        return

    import re as _re
    linked: set[int] = set()
    for f in features:
        n = f.get("pr_number")
        if n:
            linked.add(int(n))
            continue
        url = f.get("pr_url") or ""
        m = _re.search(r"/pull/(\d+)", url)
        if m:
            linked.add(int(m.group(1)))

    orphans = [pr for pr in candidates if pr["number"] not in linked]
    if not orphans:
        return

    token = _get_auth_token()
    if not token:
        log.warning(
            f"reconcile_orphaned_session_prs: no auth token; leaving "
            f"{len(orphans)} orphan(s) open on product {product.get('id')}"
        )
        return

    headers = _github_headers()
    closed = 0
    for pr in orphans:
        pr_n = pr["number"]
        head_ref = pr["head"]["ref"]
        body = (
            f"Auto-closed by ProductFactory reconciler: this session PR is "
            f"orphaned — no feature in the database references PR #{pr_n}. "
            f"Common causes are the post-coder lint/test gate rejected the "
            f"commit before linking the feature, or the feature was routed "
            f"to Blocked while the coder session was still running. Branch "
            f"`{head_ref}` is left in place for inspection."
        )
        try:
            httpx.post(
                f"https://api.github.com/repos/{slug}/issues/{pr_n}/comments",
                json={"body": body}, headers=headers, timeout=15,
            )
            cresp = httpx.patch(
                f"https://api.github.com/repos/{slug}/pulls/{pr_n}",
                json={"state": "closed"}, headers=headers, timeout=15,
            )
            if cresp.status_code == 200:
                closed += 1
                log.info(
                    f"reconcile_orphaned_session_prs: closed orphan PR #{pr_n} "
                    f"({head_ref}) on {slug}"
                )
            else:
                log.warning(
                    f"reconcile_orphaned_session_prs: PATCH {cresp.status_code} "
                    f"on PR #{pr_n} ({slug})"
                )
        except Exception as e:
            log.warning(
                f"reconcile_orphaned_session_prs: error closing PR #{pr_n} "
                f"on {slug}: {e}"
            )

    if closed:
        log.info(
            f"reconcile_orphaned_session_prs: closed {closed}/{len(orphans)} "
            f"orphan PR(s) on product {product.get('id')} ({slug})"
        )
