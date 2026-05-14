"""
One-time migration: rewrite each product's local origin URL from
git@github.com[-alias]:owner/repo.git → https://github.com/owner/repo.git

Run after deploying the GitHub App auth code. Safe to re-run (idempotent —
products already on HTTPS are skipped).

Optionally also updates products.github_repo in the DB if the row stored
an SSH URL, to keep the registered URL aligned with what the orchestrator
will use.

Usage:
    python -m scripts.migrate_origins_to_https           # dry run
    python -m scripts.migrate_origins_to_https --apply   # actually rewrite

A separate concern from any GitHub-side org transfer — that's done
manually in the GitHub UI (Repository → Settings → Danger Zone →
Transfer ownership). After transfer, GitHub redirects the old slug for
~1 year, so this script's URL rewrite can happen before or after the
transfer without breaking anything.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx


PM_API_URL = os.environ.get("PM_API_URL", "http://localhost:8080")


def _parse_slug(url: str) -> str | None:
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def _list_products() -> list[dict]:
    with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
        resp = client.get("/api/products")
        resp.raise_for_status()
        return resp.json()


def _migrate_one(product: dict, apply: bool) -> dict:
    working_dir = product.get("working_dir") or ""
    name = product.get("name") or f"#{product.get('id')}"
    github_repo = product.get("github_repo") or ""

    if not working_dir or not Path(working_dir, ".git").exists():
        return {"product": name, "skipped": "no git workspace on disk"}

    cur = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=working_dir, capture_output=True, text=True,
    )
    if cur.returncode != 0:
        return {"product": name, "skipped": f"no origin: {cur.stderr.strip()[:120]}"}

    cur_url = cur.stdout.strip()
    slug = _parse_slug(cur_url) or _parse_slug(github_repo)
    if not slug:
        return {"product": name, "skipped": f"could not parse slug from origin={cur_url!r}"}

    expected = f"https://github.com/{slug}.git"
    if cur_url == expected:
        return {"product": name, "skipped": "already on HTTPS"}

    if not apply:
        return {"product": name, "would_change": f"{cur_url}  →  {expected}"}

    r = subprocess.run(
        ["git", "remote", "set-url", "origin", expected],
        cwd=working_dir, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"product": name, "error": r.stderr.strip()[:200]}

    # Also normalise the DB-stored github_repo if it's an SSH URL — the
    # docker-runner regex parses both forms, but downstream UI links etc.
    # render the literal value, so consistency matters.
    db_should_update = github_repo and github_repo.startswith("git@")
    if db_should_update:
        new_github_repo = f"https://github.com/{slug}"
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            client.patch(f"/api/products/{product['id']}",
                         json={"github_repo": new_github_repo})

    return {
        "product": name,
        "rewrote": f"{cur_url}  →  {expected}",
        "db_updated": db_should_update,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually rewrite the URLs (default is dry-run)")
    args = parser.parse_args()

    products = _list_products()
    results = [_migrate_one(p, apply=args.apply) for p in products]

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n=== Origin rewrite ({mode}) — {len(results)} product(s) ===\n")
    for r in results:
        print(f"  {r['product']:30s}  {r}")

    changed = sum(1 for r in results if "rewrote" in r or "would_change" in r)
    errored = sum(1 for r in results if "error" in r)
    print(f"\n{changed} {'rewritten' if args.apply else 'would change'}, "
          f"{errored} error(s).\n")
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
