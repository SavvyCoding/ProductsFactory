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

log = logging.getLogger("auto_merge")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")

# Mirrors dispatch.TERMINAL but kept local to avoid an import cycle.
TERMINAL = frozenset({"Pushed", "Deferred", "Rejected", "Reverted"})


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
            # GitHub's mergeable_state is eventually consistent. A fixed sleep
            # isn't enough (observed: PATCH succeeds, 3s wait, retry still sees
            # draft). POLL the PR until draft actually flips to false, max
            # ~30s. If polling times out, return the 405 unchanged so the
            # caller's policy decides — important: caller MUST NOT auto-close
            # on 405-draft (only on 405-not-mergeable, which is a real conflict).
            import time as _t
            for _ in range(15):
                _t.sleep(2)
                poll = httpx.get(
                    f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}",
                    headers=headers, timeout=10,
                )
                if poll.status_code == 200 and poll.json().get("draft") is False:
                    break
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
