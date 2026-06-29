"""
session_result.json read/write/live-poll.

Agents write feature state transitions to session_result.json (newline-delimited
JSON, one entry per line). The orchestrator drains this file:
  - in real time via _live_poll_session_result (background thread, every 30s)
  - in a final pass after the container exits (handled by the reconciler)

Extracted from docker_runner.py during Phase 1 of OrchestratorRefactor.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from orchestrator.integrations.docker_cli import _rm_path_via_alpine
from orchestrator.session.state_machine import _apply_session_entry

log = logging.getLogger("poller.docker")

# A reviewer NEGATIVE VERDICT in the prose — used to validate the structured
# `review_outcome` against the comment text. Matches verdict markers, NOT the
# verb "reject(s)" used to DESCRIBE correct code behavior. The old check was a
# bare `"REJECT" in body.upper()` substring, which matched "Query rejects empty
# input" / "rejected the malformed request" — normal validation-behavior prose
# in an APPROVING review — and false-bounced genuine LGTMs (HCS #1867; 34
# features hit this in one week). Negative verdict = a ❌ marker, the structured
# term changes_requested, an explicit "request changes", or "reject" with a
# this/the-PR object or an I/we/reviewer subject (never "rejects <input-noun>").
# Keep IN SYNC with the copy in orchestrator/supervisor.py.
_NEG_VERDICT_RE = re.compile(
    r"❌"
    r"|changes[_\s]requested"
    r"|request(?:ing|s)?\s+changes"
    r"|\b(?:i|we|reviewer)\s+(?:would\s+)?reject"
    r"|\breject(?:ing|ed)?\s+(?:this|the\s+(?:pr|merge|commit|change|story|feature))"
    r"|\bmust\s+(?:be\s+)?(?:reject|rework)",
    re.IGNORECASE,
)

PM_API_URL = os.environ["PM_API_URL"]


def _superseded_reviewer_outcome_indices(new_lines: list, persona: str) -> set:
    """Indices in ``new_lines`` whose reviewer-outcome entry is SUPERSEDED by a
    later entry for the SAME feature in the same drain batch.

    A reviewer LLM occasionally emits multiple entries for one feature with
    conflicting verdicts (e.g. ``approved`` then ``changes_requested``). Applied
    in order, the feature transits Reviewed+approved -> Implementing+
    changes_requested, briefly exposing a merge-eligible state the per-cycle
    auto-merge sweep can race and merge irreversibly (HCS/IFT 2026-06-29
    #2172/#2173). Applying only the FINAL outcome per feature keeps the verdict
    a single transition. Non-reviewer batches collapse nothing (return empty)."""
    superseded: set = set()
    if persona != "reviewer":
        return superseded
    last_idx: dict = {}
    for i, ln in enumerate(new_lines):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if (isinstance(e, dict) and e.get("id")
                and e.get("review_outcome") in ("approved", "changes_requested")):
            fid = e["id"]
            if fid in last_idx:
                superseded.add(last_idx[fid])
            last_idx[fid] = i
    return superseded


def _validate_reviewer_outcome_consistency(entry: dict, pm_client, log_prefix: str) -> tuple[str, str]:
    """
    Enforce text-vs-structured consistency on reviewer session_result entries.

    The structured `review_outcome` field and the most recent reviewer comment
    body MUST agree:
        ``approved``           ⇔ comment opens with ✅ / "LGTM" / "APPROVED"
                                 (first 200 chars) AND no ❌ / "changes_requested" /
                                 "REJECT" anywhere in the body.
        ``changes_requested``  ⇔ comment contains ❌ / "changes_requested" /
                                 "REJECT" anywhere in the body.

    Mixed-but-changes-requested ("✅ prior X addressed. ❌ new Y" pattern) is
    allowed: presence of ❌ anywhere downgrades the verdict to changes_requested,
    and the structured outcome must match.

    Returns ``(decision, reason)`` where decision is one of:
      - ``"apply"`` — entry passes (or validation didn't apply because the entry
                     is not a reviewer outcome). Caller applies normally.
      - ``"reject"`` — first mismatch detected this session. Caller MUST NOT
                       apply the entry; a ``system:reviewer-validation`` comment
                       has been posted on the feature explaining the rejection
                       and the feature is left in ``Reviewing`` so the next
                       dispatcher cycle re-launches a reviewer.
      - ``"apply_trust_json"`` — repeat mismatch (validation comment already
                                 exists in the last 2h). Caller applies the
                                 structured outcome as authoritative and logs a
                                 critical alert.

    Canonical incident: 2026-06-03 cycle JR DocumentSign #1131. Reviewer wrote
    "✅ Commits 067a5bd + fbfd876: LGTM — functional/tests/security pass" in
    the comment body but set ``review_outcome=changes_requested`` in
    session_result.json. The structured field was applied as-is; fix_attempts
    bumped 4→5; cap-Block fired; PR #332 closed unmerged; a genuinely-approved
    commit was lost. This guard rejects that pattern at the apply gate.

    Best-effort: any PM-API failure during the comment fetch causes the entry
    to apply normally (we don't want validation to break the live-poll path).
    """
    if not isinstance(entry, dict):
        return ("apply", "")
    review_outcome = entry.get("review_outcome")
    if review_outcome not in ("approved", "changes_requested"):
        return ("apply", "")
    fid = entry.get("id")
    if not isinstance(fid, int):
        return ("apply", "")

    try:
        r = pm_client.get(f"/api/features/{fid}/comments")
        if r.status_code != 200:
            return ("apply", "")
        comments = r.json()
        if not isinstance(comments, list):
            comments = []
    except Exception:
        return ("apply", "")

    comments.sort(key=lambda c: (c.get("created_at") or ""))
    if not any((c.get("author") or "").lower() == "reviewer" for c in comments):
        return ("apply", "")

    # Evaluate the TRAILING RUN of reviewer comments (every reviewer comment
    # after the last non-reviewer comment) — not just the single latest one.
    # The reviewer prompt explicitly instructs one comment per failing
    # section plus a separate "✅ prior X addressed" acknowledgement, so a
    # legitimate changes_requested round routinely ends with a ✅-opening
    # comment. Judging only the latest comment mis-read that round as a
    # mismatch, rejected the entry, and burned a full re-review session —
    # the 2026-06-11 forensic audit traced 5 of the 7 divergent_review_
    # feedback auto-Blocks to exactly this loop. Combined-sentiment rule:
    #   changes_requested is consistent if ANY comment in the run has ❌;
    #   approved is consistent if NO comment in the run has ❌ and at least
    #   one opens positively.
    tail_run: list[dict] = []
    for c in comments:
        if (c.get("author") or "").lower() == "reviewer":
            tail_run.append(c)
        else:
            tail_run = []
    if not tail_run:
        return ("apply", "")

    bodies = [(c.get("body") or "") for c in tail_run]
    head = bodies[0][:200]

    def _pos_head(b: str) -> bool:
        h = b[:200]
        return ("✅" in h) or ("LGTM" in h.upper()) or ("APPROVED" in h.upper())

    has_pos_head = any(_pos_head(b) for b in bodies)
    # Negative VERDICT (not the verb "rejects" describing code behavior) — see
    # _NEG_VERDICT_RE. A bare "REJECT" substring false-bounced approvals whose
    # prose described validation ("Query rejects empty input"). HCS #1867.
    has_neg_body = any(_NEG_VERDICT_RE.search(b) for b in bodies)

    mismatch = False
    kind = ""
    if review_outcome == "changes_requested" and has_pos_head and not has_neg_body:
        mismatch = True
        kind = "structured=changes_requested but comment opens with ✅/LGTM/approved and contains no ❌"
    elif review_outcome == "approved" and has_neg_body:
        # approval with ❌ anywhere — downgrade by definition; structured must match.
        mismatch = True
        kind = "structured=approved but comment body contains ❌/changes_requested/reject"

    if not mismatch:
        return ("apply", "")

    # Has the system already posted a validation comment for this feature in
    # the last 2 hours? If yes, the reviewer has had a chance to fix the
    # mismatch; trust the JSON now and let the operator triage via alert.
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    repeat = any(
        (c.get("author") or "").startswith("system:reviewer-validation")
        and (c.get("created_at") or "") >= cutoff
        for c in comments
    )
    if repeat:
        log.error(
            f"{log_prefix} feature #{fid}: REPEAT reviewer text-vs-structured "
            f"mismatch within 2h window ({kind}); applying structured outcome "
            f"as authoritative — operator should triage."
        )
        return ("apply_trust_json", kind)

    validation_body = (
        f"⚠️ system:reviewer-validation — comment text and structured "
        f"`review_outcome` are inconsistent. The session_result entry was "
        f"REJECTED and the feature is left in `Reviewing` for re-review.\n\n"
        f"- Detected: {kind}\n"
        f"- structured: `{review_outcome}`\n"
        f"- comment body opens: `{head[:120]!r}`\n\n"
        f"To approve: comment opens with ✅ / LGTM AND `review_outcome=approved` "
        f"AND no ❌ in body. To request changes: comment contains ❌ AND "
        f"`review_outcome=changes_requested`. Re-emit a consistent session_result "
        f"line — see reviewer.md Step 5 'Outcome FIRST, prose SECOND'. Canonical "
        f"failure this guards: 2026-06-03 cycle JR DocumentSign #1131."
    )
    try:
        pm_client.post(
            f"/api/features/{fid}/comments",
            json={"author": "system:reviewer-validation", "body": validation_body},
            timeout=5,
        )
    except Exception:
        pass  # best-effort

    log.warning(
        f"{log_prefix} feature #{fid}: REJECTED reviewer outcome — text-vs-"
        f"structured mismatch ({kind}); session_result entry not applied; "
        f"feature left in Reviewing for re-review."
    )
    return ("reject", kind)


def _read_session_result(working_dir: str) -> list[dict]:
    """
    Read and parse session_result.json (newline-delimited JSON — one entry per line).
    Returns list of feature dicts. Skips blank or malformed lines.

    Also handles the wrapped format where an agent writes a single JSON object with a
    top-level "features" array instead of one object per line, e.g.:
        {"features": [{"id": 42, "status": "Reviewed", ...}, ...]}
    Such entries are unpacked into individual feature dicts.
    """
    result_file = Path(working_dir) / "session_result.json"
    if not result_file.exists():
        return []
    entries = []
    try:
        for line in result_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # Bare JSON array written by some agent versions: [{"id":42,...}, ...]
                if isinstance(obj, list):
                    log.warning(f"[session_result] Unwrapping bare JSON array ({len(obj)} entries)")
                    entries.extend(e for e in obj if isinstance(e, dict))
                # Wrapped {"features": [...]} format
                elif isinstance(obj, dict) and "features" in obj and isinstance(obj["features"], list) and "id" not in obj:
                    log.warning(f"[session_result] Unwrapping nested 'features' array ({len(obj['features'])} entries)")
                    entries.extend(e for e in obj["features"] if isinstance(e, dict))
                elif isinstance(obj, dict):
                    entries.append(obj)
                # else: skip non-dict, non-list top-level values
            except Exception:
                pass  # skip malformed lines (e.g. partial write mid-line)
    except Exception as e:
        log.warning(f"Could not read session_result.json: {e}")
    return entries


def _filter_session_result_by_id(working_dir: str, exclude_ids: set[int],
                                  product_name: str = "?") -> int:
    """
    Drop NDJSON entries from session_result.json whose ``id`` is in ``exclude_ids``.

    Used by post_coder's lint-guard to remove the agent's stale "Implemented"
    claims for features it has just bounced back to Implementing for rework.
    Without this, the subsequent ``_reconcile_session_result`` re-applies the
    agent's claim and undoes the rework downgrade — a 16ms race observed
    2026-05-07 on MySalesforce feature #255 (changelog: post-coder:lint-guard
    Implemented→Implementing at 09:38:04.505, agent Implementing→Implemented
    at 09:38:04.521 — same wall-clock millisecond, opposite direction).

    Idempotent. Malformed/blank lines are preserved (filter only acts on JSON
    objects with an "id" field). Returns the count of entries dropped.
    """
    if not exclude_ids:
        return 0
    sr_path = Path(working_dir) / "session_result.json"
    if not sr_path.exists():
        return 0
    try:
        original = sr_path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        log.warning(f"[{product_name}] _filter_session_result_by_id read failed: {e}")
        return 0
    kept: list[str] = []
    dropped = 0
    for line in original:
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        try:
            entry = json.loads(stripped)
            if isinstance(entry, dict) and entry.get("id") in exclude_ids:
                dropped += 1
                continue
        except Exception:
            pass  # malformed — leave it; reconcile will skip it too
        kept.append(line)
    if dropped == 0:
        return 0
    try:
        sr_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        log.info(
            f"[{product_name}] _filter_session_result_by_id: dropped {dropped} "
            f"entry/entries for feature ids {sorted(exclude_ids)} "
            f"(post-coder authoritative; reconcile must not re-apply)"
        )
    except Exception as e:
        log.warning(f"[{product_name}] _filter_session_result_by_id write failed: {e}")
        return 0
    return dropped


def _delete_session_result(working_dir: str, product_name: str = "?") -> None:
    """
    Robust delete of session_result.json.

    Two failure modes the simple `unlink(missing_ok=True)` couldn't handle:
      1. The agent ran as UID 1001 inside the container and left the file
         mode 644 owned by 1001. The orchestrator (different UID) can't
         unlink it → silent EACCES. Until 2026-05-06 this caused stale
         agent writes to leak into the next session's reconcile.
      2. On Windows Docker bind-mounts the host filesystem occasionally
         holds the inode briefly after the agent container exits.

    Order of attempts:
      a. chmod 0o666 + unlink — works in container-mode where the orchestrator
         and the agent share UID/GID space.
      b. alpine sidecar rm — runs as root via the host docker socket, the same
         escape hatch `_chmod_workspace_via_alpine` uses for the same bind-
         mount permission class.

    Logs a warning if the file still exists after both attempts. Idempotent:
    a missing file is success.
    """
    sr_path = Path(working_dir) / "session_result.json"
    if not sr_path.exists():
        return
    # Best-effort chmod so unlink doesn't fail on root-owned files written by
    # the agent container. unlink(missing_ok=True) doesn't raise on missing
    # file; the only failure mode is a permission error, which is handled by
    # the alpine sidecar fallback below. Previously this was wrapped in two
    # nested ``try/except: pass`` layers that swallowed every possible error
    # without benefit.
    try:
        os.chmod(sr_path, 0o666)
    except OSError:
        pass
    try:
        sr_path.unlink(missing_ok=True)
    except OSError:
        pass
    if not sr_path.exists():
        return
    # Direct unlink failed — escalate to alpine sidecar.
    if _rm_path_via_alpine(working_dir, "session_result.json", product_name):
        if not sr_path.exists():
            log.info(f"[{product_name}] cleared stale session_result.json via alpine sidecar")
            return
    # Both attempts failed; surface so the next reconcile's "0/N applied"
    # has a breadcrumb back to the cause. The reconciler already filters
    # out unknown statuses, so a leftover file isn't catastrophic — just
    # confusing.
    log.warning(
        f"[{product_name}] could NOT delete stale session_result.json — "
        f"next session may pick up cross-session entries"
    )


def _live_poll_session_result(working_dir: str, stop_event: threading.Event, persona: str = "") -> None:
    """
    Background thread: polls session_result.json every 30 s while the container runs.
    Applies new NDJSON lines to the DB in real-time as the agent writes phase transitions.
    Tracks applied lines by index so each entry is applied exactly once.

    On shutdown (stop_event set) performs a final drain pass so any entries written
    between the last tick and container exit are not silently lost.
    """
    result_file = Path(working_dir) / "session_result.json"
    applied_up_to = 0  # number of lines already applied this session

    def _drain_new(label: str) -> None:
        nonlocal applied_up_to
        if not result_file.exists():
            return
        try:
            lines = result_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        new_lines = lines[applied_up_to:]
        if not new_lines:
            return
        # (Fix 2, 2026-06-29) Collapse SUPERSEDED reviewer-outcome entries within
        # this batch. A reviewer LLM occasionally emits multiple entries for the
        # same feature with conflicting verdicts (e.g. `approved` then
        # `changes_requested`). Applied in order, the feature transits
        # Reviewed+approved -> Implementing+changes_requested, briefly exposing a
        # merge-eligible state the per-cycle auto-merge sweep can race and merge
        # irreversibly (HCS/IFT 2026-06-29 #2172/#2173). Apply only the FINAL
        # reviewer-outcome entry per feature so the verdict lands in a single
        # transition with no transient Reviewed+approved. (Pairs with the Fix-1a/1c
        # guards in auto_merge.py, which cover the cross-tick / cross-process case.)
        _superseded = _superseded_reviewer_outcome_indices(new_lines, persona)
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for _idx, line in enumerate(new_lines):
                    line = line.strip()
                    if not line:
                        applied_up_to += 1
                        continue
                    if _idx in _superseded:
                        log.info(
                            f"[{label}] skipping superseded reviewer outcome "
                            f"(a later verdict for the same feature exists in this "
                            f"batch) — avoids a transient Reviewed+approved the "
                            f"auto-merge sweep could race"
                        )
                        applied_up_to += 1
                        continue
                    try:
                        entry = json.loads(line)
                        # Reviewer must not set Reviewing (pre-claim artifact).
                        if persona == "reviewer" and entry.get("status") == "Reviewing":
                            log.debug(f"[{label}] Skipping reviewer Reviewing entry for feature #{entry.get('id')}")
                        # Coder/reviewer must not write Pushed — PRs must go through GitHub merge.
                        elif persona in ("coder", "reviewer") and entry.get("status") == "Pushed":
                            log.warning(f"[{label}] Blocked agent-written Pushed for feature #{entry.get('id')} (persona={persona}) — PRs must merge via GitHub")
                        elif persona == "reviewer" and entry.get("review_outcome") in ("approved", "changes_requested"):
                            # Borrowed from Aider's structured-outcome-first pattern.
                            # See _validate_reviewer_outcome_consistency() for the
                            # canonical incident (cycle JR DocumentSign #1131).
                            decision, _reason = _validate_reviewer_outcome_consistency(
                                entry, client, f"[{label}]",
                            )
                            if decision != "reject":
                                _apply_session_entry(client, entry, working_dir=working_dir)
                            # else: rejected, do not apply; counter still advances
                            # below so we don't reprocess this line every tick.
                        else:
                            _apply_session_entry(client, entry, working_dir=working_dir)
                    except Exception:
                        pass  # malformed line — skip, don't block the rest
                    applied_up_to += 1
        except Exception as e:
            log.debug(f"[{label}] PM API error: {e}")

    while not stop_event.wait(30):  # poll every 30 s; exits when stop_event is set
        _drain_new("live-poll")

    # Final drain — catches entries written between the last tick and stop_event.
    # Reconcile will re-apply these idempotently but running it here ensures the
    # DB reaches a consistent state even if reconcile is short-circuited by an
    # exception, and gives the user faster feedback in the UI.
    _drain_new("live-poll-final")
