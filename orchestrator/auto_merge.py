"""Per-cycle auto-merge sweep — Phase 1 of PollerRevamp.

Walks every `ready` product once per cycle and squash-merges any feature
that is Reviewed + has a pr_number + review_outcome=approved. See
INVARIANTS.md VII.1.

1-PR model: every approved feature's pr_number is its **session** PR
(`coder/<uid>` → `main`). The sweep squash-merges the session PR
directly to main. Sprints are planning buckets only — there is no sprint
PR to coordinate with.

Idempotent. If a feature was already merged in a prior cycle (status
already Pushed, or PR already merged on GitHub), the sweep is a no-op
for that feature.

Gated on the existing `system_config.auto_merge_enabled` flag — no new
column. If the sweep misbehaves, flip that flag off and both this and
the legacy inline path stop.
"""
from __future__ import annotations

import logging
import os
import uuid

import httpx

from orchestrator.integrations.github import _parse_repo_slug

log = logging.getLogger("auto_merge")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")


def mark_ready_and_poll(repo_slug: str, pr_num: int, headers: dict,
                        attempts: int = 15, interval: float = 2.0) -> bool:
    """Flip a draft PR to ready-for-review, then poll until GitHub's
    eventually-consistent ``draft`` flag actually reads false (observed:
    the PATCH returns 200 immediately but a merge attempt seconds later
    still sees the old draft state). Returns True when the flag cleared,
    False on timeout/error.

    Shared by the per-cycle sweep (this module) and the per-reviewer-
    session path (pipelines/auto_merge_reviewer.py) — the 405-draft
    handling used to be ~40 duplicated lines in each. Callers keep their
    own retry policy: the sweep retries the merge regardless (a still-
    draft 405 propagates to its non-mergeable policy), the reviewer path
    skips the feature on timeout rather than risk a destructive close.
    """
    import time as _t
    try:
        httpx.patch(
            f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}",
            headers=headers, json={"draft": False}, timeout=15,
        )
        for attempt in range(attempts):
            _t.sleep(interval)
            poll = httpx.get(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}",
                headers=headers, timeout=10,
            )
            if poll.status_code == 200 and poll.json().get("draft") is False:
                log.info(f"[draft-poll] PR #{pr_num} draft cleared after {(attempt + 1) * interval:.0f}s")
                return True
    except Exception:
        pass
    return False


def _try_merge_pr(repo_slug: str, pr_num: int, token: str) -> tuple[int, str]:
    """Attempt squash-merge of a single PR. Returns (status_code, body_excerpt).

    Status code 0 indicates a transport-level exception (network, timeout) — the
    sweep treats it as a transient skip, not a failure that should block other PRs.

    Sprint PRs are provisioned as drafts (Phase 6.1). GitHub returns 405 with
    "Pull Request is still a draft" when you try to merge a draft. We detect that
    specific message, mark the PR ready for review, and retry once. Any other 405
    is a real conflict / failing CI / branch protection — return it unchanged so
    the caller can apply its non-mergeable policy.
    """
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        resp = httpx.put(
            f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}/merge",
            headers=headers, json={"merge_method": "squash"}, timeout=30,
        )
        if resp.status_code == 405 and "draft" in resp.text.lower():
            # Mark ready + poll (shared helper). If polling times out, the
            # retry below returns the 405 unchanged so the caller's policy
            # decides — important: caller MUST NOT auto-close on 405-draft
            # (only on 405-not-mergeable, which is a real conflict).
            mark_ready_and_poll(repo_slug, pr_num, headers)
            resp = httpx.put(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}/merge",
                headers=headers, json={"merge_method": "squash"}, timeout=30,
            )
        return resp.status_code, resp.text[:200]
    except Exception as e:
        return 0, f"exception: {e}"


def sweep_product(product: dict, sys_cfg: dict) -> dict:
    """Auto-merge every Reviewed+approved feature with a pr_number for one product.

    Honors the system-wide `auto_merge_enabled` flag (no-op when False).
    Records one `persona=auto-merge` session row per product when anything moved.
    Returns counters: {checked, merged, conflicts, errors, skipped}.
    """
    counters = {"checked": 0, "merged": 0, "conflicts": 0, "errors": 0, "skipped": 0}

    if not sys_cfg.get("auto_merge_enabled"):
        return counters

    # Prefer GitHub App installation token; fall back to PAT for the
    # transition release. _get_auth_token() reads from system_config each
    # call, so a rotation/revoke is picked up without restart.
    from orchestrator.github_client import _get_auth_token
    pat = _get_auth_token()
    repo_slug = _parse_repo_slug(product.get("github_repo") or "")
    if not pat or not repo_slug:
        counters["skipped"] = 1
        return counters

    product_id = product["id"]
    merged_features: list[dict] = []
    merged_pr_nums: set[int] = set()

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            # (Fix 1a, 2026-06-29) The sweep is a catch-up SAFETY NET, not a
            # competitor to a live reviewer finalize. A reviewer session still
            # running/wrapping is mid-reconcile, and the feature's
            # `Reviewed+approved` state may be a TRANSIENT intermediate — an
            # `approved` entry about to be corrected to `changes_requested` by a
            # later entry in the same session_result. Merging then irreversibly
            # ships an un-finalized review. Canonical: HCS/IFT 2026-06-29 features
            # #2172/#2173 squash-merged with review_outcome=changes_requested via
            # exactly this race (reviewer LGTM'd in prose but the structured field
            # flip-flopped). Defer to a later cycle once the product is quiescent —
            # no merge is lost: a genuinely-approved feature is re-swept then.
            try:
                _sess_resp = client.get(
                    f"/api/products/{product_id}/sessions", params={"limit": 5})
                if _sess_resp.status_code == 200:
                    _sessions = _sess_resp.json() or []
                    if any(isinstance(s, dict)
                           and s.get("persona") == "reviewer"
                           and s.get("status") in ("running", "wrapping")
                           for s in _sessions):
                        log.info(
                            f"[auto-merge sweep] product={product_id} has a live "
                            f"reviewer session — deferring merge to a quiescent "
                            f"cycle (avoids racing the reviewer finalize)"
                        )
                        counters["skipped"] = 1
                        return counters
            except Exception:
                pass  # best-effort guard; fall through on a sessions-API hiccup

            feats_resp = client.get(f"/api/products/{product_id}/features")
            if feats_resp.status_code != 200:
                return counters
            feats = feats_resp.json() or []
            if not isinstance(feats, list):
                return counters

            candidates = [
                f for f in feats
                if f.get("status") == "Reviewed"
                and f.get("pr_number")
                and f.get("review_outcome") == "approved"
            ]
            counters["checked"] = len(candidates)

            for f in candidates:
                fid = f["id"]
                pr_num = f["pr_number"]

                # `changed_by` tags the changelog row so the audit trail
                # attributes Pushed transitions to the sweep, not "agent".
                _patch_pushed = {"status": "Pushed", "changed_by": "auto-merge"}

                # Same PR shared across multiple features: once we
                # successfully merge it, flip every other feature pointing
                # at it. Common case under the 1-PR model: one session PR
                # covers 1-N features that all move to Pushed together.
                if pr_num in merged_pr_nums:
                    client.patch(f"/api/features/{fid}", json=_patch_pushed)
                    counters["merged"] += 1
                    merged_features.append(f)
                    continue

                # (Fix 1c, 2026-06-29) Re-confirm eligibility at the moment of the
                # IRREVERSIBLE GitHub merge. `feats` was fetched once at the top of
                # the sweep; a concurrent reviewer reconcile may have flipped this
                # feature to Implementing+changes_requested since. Re-fetch and
                # re-assert Reviewed+approved; skip on ANY change or fetch error —
                # a missed merge is harmlessly re-swept next cycle, but a wrong
                # merge to main is permanent. Backstops the Fix-1a session guard
                # for the residual window between that check and this merge.
                try:
                    _fresh = client.get(f"/api/features/{fid}")
                    _fj = _fresh.json() if _fresh.status_code == 200 else {}
                    _still_eligible = (
                        isinstance(_fj, dict)
                        and _fj.get("status") == "Reviewed"
                        and _fj.get("review_outcome") == "approved"
                        and _fj.get("pr_number"))
                except Exception:
                    _still_eligible = False
                if not _still_eligible:
                    log.info(
                        f"[auto-merge sweep] product={product_id} feature #{fid} "
                        f"no longer Reviewed+approved at merge time — skipping "
                        f"(concurrent reconcile or fetch error)"
                    )
                    counters["skipped"] += 1
                    continue

                code, body = _try_merge_pr(repo_slug, pr_num, pat)
                if code == 200:
                    log.info(
                        f"[auto-merge sweep] product={product_id} feature=#{fid} "
                        f"PR=#{pr_num} merged"
                    )
                    merged_pr_nums.add(pr_num)
                    client.patch(f"/api/features/{fid}", json=_patch_pushed)
                    counters["merged"] += 1
                    merged_features.append(f)
                elif code == 422:
                    # GitHub says "already merged" — flip the feature to match reality.
                    log.info(
                        f"[auto-merge sweep] product={product_id} PR=#{pr_num} "
                        f"already merged (422), flipping feature #{fid} to Pushed"
                    )
                    merged_pr_nums.add(pr_num)
                    client.patch(f"/api/features/{fid}", json=_patch_pushed)
                    counters["merged"] += 1
                    merged_features.append(f)
                elif code == 405:
                    log.warning(
                        f"[auto-merge sweep] product={product_id} PR=#{pr_num} not "
                        f"mergeable (conflicts or failing checks) — skipping"
                    )
                    counters["conflicts"] += 1
                elif code == 404:
                    log.warning(
                        f"[auto-merge sweep] product={product_id} feature #{fid} "
                        f"references PR #{pr_num} which doesn't exist — needs PM cleanup"
                    )
                    counters["errors"] += 1
                else:
                    log.warning(
                        f"[auto-merge sweep] product={product_id} PR=#{pr_num} "
                        f"returned {code}: {body[:80]}"
                    )
                    counters["errors"] += 1

            if merged_features:
                # Observability row: gives the History tab an audit trail of
                # what the sweep merged. Two API calls are required because
                # SessionCreate (POST /api/sessions) only accepts the start-
                # time fields (product_id, session_uid, persona, backend,
                # container_id, status); end-time counters (features_attempted,
                # features_pushed, exit_code, notes, ended_at) live in
                # SessionEnd and have to go through the PATCH endpoint.
                # Pre-2026-05-07 this was one POST that silently dropped the
                # extra fields → UI rendered "running… 0/0 pushed" forever.
                session_uid = str(uuid.uuid4())[:8]
                notes = "Auto-merged PRs (sweep): " + ", ".join(
                    f"#{f['pr_number']} ({(f.get('name') or '')[:30]})"
                    for f in merged_features
                )
                from datetime import datetime as _dt, timezone as _tz
                try:
                    create_resp = client.post("/api/sessions", json={
                        "product_id":   product_id,
                        "session_uid":  session_uid,
                        "persona":      "auto-merge",
                        "backend":      "poller",
                        "container_id": "poller",
                        # status will be set to 'ended' on the PATCH below;
                        # leave the create-time status at its default so the
                        # row passes the FSM's "pending → ended" transition
                        # cleanly.
                    })
                    new_id = create_resp.json().get("id") if create_resp.status_code == 201 else None
                    if new_id:
                        client.patch(f"/api/sessions/{new_id}", json={
                            "status":             "ended",
                            "exit_code":          0,
                            "ended_at":           _dt.now(_tz.utc).isoformat(),
                            "features_attempted": len(merged_features),
                            "features_pushed":    len(merged_features),
                            "notes":              notes,
                        })
                except Exception:
                    pass  # session row is observability, not correctness
    except Exception:
        log.exception(f"[auto-merge sweep] crashed for product {product_id}")

    return counters


def sweep_all(products: list[dict], sys_cfg: dict) -> dict:
    """Run the sweep across every `ready` product. Returns aggregate counters.

    Called once per poll cycle from poller.py:main(), after PR reconciliation
    and before reviewer-first / round-robin persona selection.
    """
    total = {"checked": 0, "merged": 0, "conflicts": 0, "errors": 0, "skipped": 0}

    if not sys_cfg.get("auto_merge_enabled"):
        return total

    for p in products or []:
        if p.get("status") != "ready":
            continue
        c = sweep_product(p, sys_cfg)
        for k in total:
            total[k] += c[k]

    if total["merged"] or total["conflicts"] or total["errors"]:
        log.info(
            f"[auto-merge sweep] checked={total['checked']} "
            f"merged={total['merged']} conflicts={total['conflicts']} "
            f"errors={total['errors']} skipped={total['skipped']}"
        )
    return total
