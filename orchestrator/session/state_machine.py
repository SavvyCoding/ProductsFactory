"""
Feature state machine — the single source of truth for session_result.json
entries being applied to the PM API.

Enforces the contract documented in orchestrator/INVARIANTS.md sections IV.1–IV.6:
  - IV.1 status never silently downgrades (rank guard via _PROGRESS_RANK)
  - IV.2 Reviewing→Implementing and Reviewed→Implementing are allowed (rework path)
  - IV.3 Reviewed entries without review_outcome are rejected
  - IV.4 Reviewer entries with status=Reviewing are filtered (caller's job)
  - IV.5 Coder/reviewer entries with status=Pushed are filtered (caller's job)
  - IV.6 Reviewing entry without pr_number falls back to extracting from pr_url

Extracted from docker_runner.py during Phase 1 of OrchestratorRefactor.
"""

import logging
import re

import httpx

# Use the legacy logger name so log scraping / filters keep working.
log = logging.getLogger("poller.docker")


_VALID_FEATURE_STATUSES = frozenset({
    "Pending", "Approved",
    "Designing", "Designed",
    "Implementing", "Implemented",
    "Reviewing", "Reviewed",
    "Testing", "Committed", "Pushed",
    "Blocked", "Rejected", "Reverted", "Deferred",
})


# Rank-ordered status (IV.1). Reviewing → Implementing and Reviewed → Implementing
# are explicit exceptions in _ALLOWED_BACKWARD (IV.2) — without those the
# reviewer's "changes_requested" PATCH would be silently dropped because
# Implementing(4) < Reviewing(5), trapping the feature in Reviewing forever.
_PROGRESS_RANK = {
    "Pending": 0, "Approved": 1, "Designing": 2, "Designed": 3,
    "Implementing": 4, "Reviewing": 5, "Reviewed": 6, "Pushed": 7,
    "Blocked": 2, "Deferred": 7, "Rejected": 7, "Reverted": 0,
}
_ALLOWED_BACKWARD = {("Reviewing", "Implementing"), ("Reviewed", "Implementing")}


def _apply_session_entry(client: httpx.Client, entry: dict) -> bool:
    """PATCH a single session_result entry to the PM API. Returns True on success."""
    fid = entry.get("id")
    if not fid:
        log.warning(f"[progress] Skipping session_result entry with no feature id: {entry}")
        return False
    # Guard: reject unknown status values before they hit the DB constraint
    status = entry.get("status")
    if status is not None and status not in _VALID_FEATURE_STATUSES:
        log.warning(f"[progress] Skipping feature #{fid} entry with unknown status '{status}' — agent bug?")
        return False
    # Contract: Reviewing entries MUST carry pr_number (from the field or
    # embedded in pr_url). Without it the feature gets stuck (auto-merge has
    # nothing to merge). Previously we warned and let it through; now we
    # REJECT and bump fix_attempts so repeated violations auto-Block.
    if entry.get("status") == "Reviewing" and not entry.get("pr_number"):
        pr_url = entry.get("pr_url", "")
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            entry = dict(entry, pr_number=int(m.group(1)))
            log.info(f"[progress] Feature #{fid}: extracted pr_number={entry['pr_number']} from pr_url")
        else:
            log.warning(f"[progress] REJECTING feature #{fid} — Reviewing without pr_number (agent contract violation)")
            try:
                cur = client.get(f"/api/features/{fid}").json()
                attempts = int(cur.get("fix_attempts") or 0) + 1
                patch = {"fix_attempts": attempts}
                if attempts >= 5:
                    patch.update({"status": "Blocked",
                                  "blocked_reason": "Agent kept marking Reviewing without a real PR number"})
                client.patch(f"/api/features/{fid}", json=patch)
            except Exception:
                pass
            return False

    # Contract: Reviewed entries MUST carry review_outcome. Without it the
    # auto-merge path can't decide whether to merge.
    if entry.get("status") == "Reviewed" and not entry.get("review_outcome"):
        log.warning(f"[progress] REJECTING feature #{fid} — Reviewed without review_outcome")
        return False
    # Guard: never downgrade a feature's status — except for review-driven
    # backward transitions, which are part of the normal pipeline:
    #   - Reviewing → Implementing  (reviewer requested changes; coder reworks)
    #   - Reviewed  → Implementing  (post-approval issue caught; coder reworks)
    if status:
        try:
            current_resp = client.get(f"/api/features/{fid}")
            if current_resp.status_code == 200:
                current_status = current_resp.json().get("status", "")
                if (
                    _PROGRESS_RANK.get(current_status, 0) > _PROGRESS_RANK.get(status, 0)
                    and (current_status, status) not in _ALLOWED_BACKWARD
                ):
                    log.debug(f"[progress] Feature #{fid}: skipping downgrade {current_status} → {status}")
                    return False
        except Exception:
            pass  # proceed with update if check fails

    patch_body = {k: v for k, v in entry.items() if k not in ("id", "confidence")}
    try:
        resp = client.patch(f"/api/features/{fid}", json=patch_body)
        resp.raise_for_status()
        log.info(f"[progress] Feature #{fid} -> {patch_body.get('status', '?')}")
        return True
    except httpx.HTTPStatusError as e:
        log.warning(
            f"[progress] PM API rejected feature #{fid} update "
            f"({e.response.status_code}): {e.response.text[:200]}"
        )
        return False
    except Exception as e:
        log.warning(f"[progress] Could not update feature #{fid}: {e}")
        return False
