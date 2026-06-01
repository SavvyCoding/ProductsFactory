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


def _apply_session_entry(
    client: httpx.Client,
    entry: dict,
    working_dir: str | None = None,
) -> bool:
    """PATCH a single session_result entry to the PM API. Returns True on success.

    `working_dir`: when provided, enables the phantom-design-doc-path guard.
    Designer agents can write `{"status": "Designed", "design_doc_path":
    "docs/story_<id>.md"}` to session_result.json WITHOUT actually writing
    the doc to disk (the designer "wandered" to a different feature, or
    named the file wrong, or hallucinated the path). If we blindly PATCH
    `design_doc_path` to the PM API, the DB ends up with a stale path that
    no file on disk satisfies — the next coder session reads it, finds no
    spec, and fails. The post_doc.py fallback already has a verify-doc-
    exists guard (added 2026-05-28), but the live-poll thread and the
    final session_result reconciler bypass that path entirely. Canonical
    2026-06-01 incident: DocumentSign feature 1178 had design_doc_path
    `docs/story_1178.md` in the DB but the file was missing from the
    working tree; the drift-scanner flagged it at 19:40:02.
    """
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
            # Bump fix_attempts so repeated violations auto-Block at 5.
            # Previously this whole block was wrapped in ``try/except: pass``
            # which made the enforcement theatrical — if the PATCH failed,
            # the agent's contract violation evaporated silently and the
            # auto-Block guarantee no-op'd. Now: log at WARNING on any
            # failure mode (GET, PATCH) so PM-API hiccups are visible in
            # the orchestrator log. The function still returns False on
            # any path so the contract-violation reject behavior is
            # unchanged regardless of whether the bump succeeded.
            try:
                resp = client.get(f"/api/features/{fid}")
                resp.raise_for_status()
                cur = resp.json()
                attempts = int(cur.get("fix_attempts") or 0) + 1
                patch: dict = {"fix_attempts": attempts}
                if attempts >= 5:
                    patch.update({"status": "Blocked",
                                  "blocked_reason": "Agent kept marking Reviewing without a real PR number"})
                patch_resp = client.patch(f"/api/features/{fid}", json=patch)
                patch_resp.raise_for_status()
            except Exception as e:
                log.warning(
                    f"[progress] Feature #{fid}: could not bump fix_attempts "
                    f"after contract-violation reject ({type(e).__name__}: {e}). "
                    f"Auto-Block-at-5 invariant may be off by one this cycle."
                )
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

    # Phantom design_doc_path guard: when working_dir is provided and the
    # entry tries to set a design_doc_path, verify the file actually exists
    # on disk before persisting the path. If it doesn't, strip
    # design_doc_path from the patch so the DB doesn't carry a path that
    # points at nothing. We keep the rest of the patch (e.g. status) so
    # the agent's other state writes still apply — the next designer cycle
    # will re-author this feature because design_doc_path is now NULL.
    # See function docstring for the canonical 2026-06-01 #1178 incident.
    if working_dir and patch_body.get("design_doc_path"):
        from pathlib import Path as _PPath
        doc_rel = patch_body["design_doc_path"]
        if not (_PPath(working_dir) / doc_rel).is_file():
            log.warning(
                f"[progress] Feature #{fid}: stripping phantom "
                f"design_doc_path={doc_rel!r} from session_result patch "
                f"(file not present in working tree). "
                f"Designer wandered or hallucinated the path; next "
                f"designer cycle will re-author."
            )
            patch_body.pop("design_doc_path", None)

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
