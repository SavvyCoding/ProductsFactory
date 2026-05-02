"""Per-cycle auto-merge sweep — Phase 1 of PollerRevamp.

Walks every `ready` product once per cycle and squash-merges any feature
that is Reviewed + has a pr_number + review_outcome=approved. See
INVARIANTS.md VII.1.

Sprint-PR mode awareness (INVARIANTS.md VII.5): when a feature's
pr_number matches its sprint's pr_number, the PR is shared across every
feature in that sprint (sprint-PR mode). The sweep MUST NOT merge such
a PR until every feature in the sprint is merge-eligible — otherwise it
ships an incomplete sprint as soon as the first feature reaches
Reviewed+approved. Per-feature mode (the default) is unaffected because
each feature's pr_number is its own.

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

log = logging.getLogger("auto_merge")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")

# Mirrors dispatch.TERMINAL but kept local to avoid an import cycle.
TERMINAL = frozenset({"Pushed", "Deferred", "Rejected", "Reverted"})


def _is_merge_eligible(feature: dict, target_pr_num: int) -> bool:
    """A sprint feature is merge-eligible when EITHER:
      - it's already terminal (Pushed/Deferred/Rejected/Reverted) — the
        sprint has explicitly accounted for it, OR
      - it's `Reviewed` + `approved` AND its pr_number matches the target
        sprint PR — meaning it's queued to ship as part of THIS merge.

    Anything else (Pending, Approved, Designed, Implementing, Reviewing,
    Reviewed-changes-requested, Reviewed-pointing-at-different-PR, Blocked)
    means the sprint isn't ready and the merge must wait.
    """
    if feature.get("status") in TERMINAL:
        return True
    if (
        feature.get("status") == "Reviewed"
        and feature.get("review_outcome") == "approved"
        and feature.get("pr_number") == target_pr_num
    ):
        return True
    return False


def _parse_repo_slug(github_repo: str) -> str | None:
    """Extract `owner/repo` from a GitHub URL. Returns None if unparseable."""
    if not github_repo:
        return None
    slug = github_repo.rstrip("/").split("github.com/")[-1].replace(".git", "")
    return slug or None


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
            httpx.patch(
                f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}",
                headers=headers, json={"draft": False}, timeout=15,
            )
            # GitHub's mergeable_state is eventually consistent — an immediate
            # retry can still see the PR as draft. Sleep briefly so the next
            # merge attempt sees the un-drafted state.
            import time as _t
            _t.sleep(3)
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

    pat = sys_cfg.get("github_pat") or ""
    repo_slug = _parse_repo_slug(product.get("github_repo") or "")
    if not pat or not repo_slug:
        counters["skipped"] = 1
        return counters

    product_id = product["id"]
    merged_features: list[dict] = []
    merged_pr_nums: set[int] = set()
    held_sprint_prs: set[int] = set()  # PRs deferred this cycle by the sprint-PR gate

    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            feats_resp = client.get(f"/api/products/{product_id}/features")
            if feats_resp.status_code != 200:
                return counters
            feats = feats_resp.json() or []
            if not isinstance(feats, list):
                return counters

            # Build sprint_id → sprint(with pr_number) map so the sprint-PR
            # mode gate can answer "is this feature's pr_number actually the
            # sprint's PR?". Sprints without a pr_number can't be sprint-PR
            # mode and are excluded.
            sprint_by_id: dict[int, dict] = {}
            try:
                sprints_resp = client.get(f"/api/products/{product_id}/sprints")
                if sprints_resp.status_code == 200:
                    sprints_payload = sprints_resp.json() or []
                    if isinstance(sprints_payload, list):
                        sprint_by_id = {
                            s["id"]: s
                            for s in sprints_payload
                            if s.get("id") and s.get("pr_number")
                        }
            except Exception:
                pass  # Per-feature mode behaves fine with an empty map.

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

                # Same PR shared across multiple features (sprint PRs cover N features):
                # once we successfully merge it, flip every other feature pointing at it.
                if pr_num in merged_pr_nums:
                    client.patch(f"/api/features/{fid}", json=_patch_pushed)
                    counters["merged"] += 1
                    merged_features.append(f)
                    continue

                # If this PR was already determined non-mergeable this cycle by
                # the sprint-PR gate, don't re-check or re-call GitHub.
                if pr_num in held_sprint_prs:
                    counters["skipped"] += 1
                    continue

                # Sprint-PR mode gate (INVARIANTS.md VII.5). When this feature's
                # pr_number matches its sprint's pr_number, the PR is the
                # whole-sprint PR. Merging it now would ship every feature in
                # the sprint, so we hold the merge until every sprint feature
                # is merge-eligible (terminal, or Reviewed+approved on the same
                # PR). Per-feature mode never enters this branch because the
                # feature's pr_number won't match its sprint's pr_number.
                sid = f.get("sprint_id")
                sprint = sprint_by_id.get(sid) if sid else None
                if sprint and sprint.get("pr_number") == pr_num:
                    sprint_features = [x for x in feats if x.get("sprint_id") == sid]
                    blockers = [
                        x for x in sprint_features
                        if not _is_merge_eligible(x, pr_num)
                    ]
                    if blockers:
                        # Log once per held PR per cycle, not once per feature
                        # pointing at it.
                        log.info(
                            f"[auto-merge sweep] product={product_id} sprint={sid} "
                            f"PR=#{pr_num} held: {len(blockers)} sprint feature(s) "
                            f"not yet merge-eligible "
                            f"(e.g. #{blockers[0]['id']} status={blockers[0].get('status')})"
                        )
                        held_sprint_prs.add(pr_num)
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
                # what the sweep merged. status=ended so launch_session's
                # "already has active session" guard doesn't see this as a
                # phantom in-flight session and block real persona launches.
                # ended_at left to website default if it has one; explicit
                # exit_code=0 + features_pushed mark this as a completed run.
                session_uid = str(uuid.uuid4())[:8]
                notes = "Auto-merged PRs (sweep): " + ", ".join(
                    f"#{f['pr_number']} ({(f.get('name') or '')[:30]})"
                    for f in merged_features
                )
                try:
                    client.post("/api/sessions", json={
                        "product_id":         product_id,
                        "session_uid":        session_uid,
                        "persona":            "auto-merge",
                        "backend":            "poller",
                        "container_id":       "poller",
                        "status":             "ended",
                        "exit_code":          0,
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
