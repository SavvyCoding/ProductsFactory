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
#
# `Implemented` (4), `Testing` (5), `Committed` (5) appear in the
# `_VALID_FEATURE_STATUSES` set and the agent prompt instructs coders to
# write `{"status": "Implemented"}` to session_result.json. Without them in
# this rank table they fall to default 0 and EVERY agent progress write
# triggered the rank-downgrade path silently — the keystone bug behind the
# "9-hour, millions-of-tokens, 0 features released" incident on 2026-05-06.
# Ranks chosen so the natural pipeline order Designed→Implementing→
# Implemented→Reviewing→Reviewed→Pushed never trips the guard.
_PROGRESS_RANK = {
    "Pending": 0, "Approved": 1, "Designing": 2, "Designed": 3,
    "Implementing": 4, "Implemented": 4,
    "Reviewing": 5, "Testing": 5, "Committed": 5,
    "Reviewed": 6, "Pushed": 7,
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

    # ── Normalizer: hybrid Reviewed/Reviewing + changes_requested combos ────
    # The reviewer prompt mandates `status=Implementing` when posting
    # `review_outcome=changes_requested`. Models occasionally violate this
    # and write `status=Reviewed` (or `status=Reviewing`) alongside
    # `changes_requested` — an invalid combination that lands in the DB,
    # passes _is_merge_eligible (so it blocks the sprint PR), but doesn't
    # match the codeable filter (so no coder ever picks it up). The
    # feature gets stranded forever — incident pattern observed on #374
    # (2026-05-09 04:03) and #377 (2026-05-09 17:18).
    #
    # Auto-correct silently. The legitimate semantics of changes_requested
    # is "send back to coder for rework", which means status MUST be
    # Implementing. Log a warning so prompt drift is visible in operator
    # logs, but don't fail the apply — bouncing the entry would re-trigger
    # the silent-task_done loop the new reviewer prompt rule guards against.
    if (status in ("Reviewed", "Reviewing")
            and entry.get("review_outcome") == "changes_requested"):
        log.warning(
            f"[progress] Feature #{fid}: normalizing invalid combo "
            f"status={status!r} + review_outcome='changes_requested' to "
            f"status='Implementing' (reviewer prompt violation)."
        )
        entry = dict(entry, status="Implementing")
        status = "Implementing"
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
                    # Logged at INFO so cap-the-loop diagnostics work — until
                    # 2026-05-06 this was log.debug and the orchestrator silently
                    # dropped every progress write, masking the "0 features
                    # released" incident for 9h. If you're spelunking pipeline
                    # stalls and the [progress] log is empty, look here first.
                    log.info(
                        f"[progress] Feature #{fid}: rejecting downgrade "
                        f"{current_status} → {status} (not in _ALLOWED_BACKWARD)"
                    )
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
