"""Per-cycle auto-merge sweep — Phase 1 of PollerRevamp.

Walks every `ready` product once per cycle and squash-merges any feature
that is Reviewed + has a pr_number + review_outcome=approved, regardless
of sprint membership. Replaces the historical pattern where auto-merge
was only triggered inside reviewer sessions or inside `determine_persona`
for active-sprint Reviewed features (see invariant VII.1 in INVARIANTS.md).

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
    """
    try:
        resp = httpx.put(
            f"https://api.github.com/repos/{repo_slug}/pulls/{pr_num}/merge",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"merge_method": "squash"},
            timeout=30,
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

                # Same PR shared across multiple features (sprint PRs cover N features):
                # once we successfully merge it, flip every other feature pointing at it.
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
