"""
Rotate the embedded GitHub PAT across every product's git remote URL.

Each product workspace's .git/config holds a remote URL of the form:

    https://x-access-token:<PAT>@github.com/<org>/<repo>.git

When the PAT in system_config is rotated, every product needs that
embedded token rewritten — otherwise orchestrator and agent containers
keep using the stale PAT until each workspace is touched manually,
which led to the silent "still using ghp_NZSW…" failure mode after
the first credential rotation today.

Idempotent: products already on the new PAT are skipped. Non-fatal:
per-product errors are logged but never abort the batch.

Usage from inside the orchestrator container:

    docker exec pf-orchestrator python -m orchestrator.pat_rotate

…or call rotate_pat() programmatically from tools.run_cycle (which
auto-detects PAT changes between cycles — see _maybe_rotate_pat there).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import httpx

from orchestrator.paths import container_path

log = logging.getLogger("poller.pat_rotate")

PM_API_URL = os.environ.get("PM_API_URL", "http://pm-api:8080")

# Match the embedded token portion of an HTTPS+PAT URL so we can splice in a
# new value without touching the org/repo path.
_PAT_URL_RE = re.compile(
    r"^(https://x-access-token:)([^@]+)(@github\.com/.*)$"
)


def rotate_pat(products: Iterable[dict], new_pat: str) -> tuple[int, int]:
    """
    Rewrite the embedded PAT in each product's git remote URL to ``new_pat``.

    Returns ``(updated, skipped)`` counts. A "skip" is any of:
      - workspace has no .git
      - remote URL doesn't match the HTTPS+PAT pattern (e.g. SSH-style)
      - remote already uses ``new_pat``
      - subprocess error (logged)
    """
    if not new_pat or (
        not new_pat.startswith("ghp_") and not new_pat.startswith("github_pat_")
    ):
        log.warning(
            "[pat-rotate] new_pat doesn't look like a GitHub token "
            "(prefix=%s) — aborting to avoid corrupting remote URLs",
            (new_pat[:5] + "…") if new_pat else "<empty>",
        )
        return (0, 0)

    updated = 0
    skipped = 0

    for p in products:
        pid = p.get("id")
        wd_host = p.get("working_dir") or ""
        wd = Path(container_path(wd_host))
        if not (wd / ".git").exists():
            log.debug("[pat-rotate] product=%s — no .git, skipping", pid)
            skipped += 1
            continue

        try:
            cur = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(wd), capture_output=True, text=True, timeout=10,
            )
            if cur.returncode != 0:
                log.warning(
                    "[pat-rotate] product=%s git remote get-url failed: %s",
                    pid, cur.stderr.strip()[:200],
                )
                skipped += 1
                continue
            old_url = cur.stdout.strip()
        except Exception:
            log.exception("[pat-rotate] product=%s get-url crashed", pid)
            skipped += 1
            continue

        m = _PAT_URL_RE.match(old_url)
        if not m:
            log.debug(
                "[pat-rotate] product=%s remote not HTTPS+PAT (e.g. SSH) — skipping: %s",
                pid, _redact(old_url),
            )
            skipped += 1
            continue

        old_token = m.group(2)
        if old_token == new_pat:
            log.debug("[pat-rotate] product=%s already on new PAT", pid)
            skipped += 1
            continue

        new_url = m.group(1) + new_pat + m.group(3)
        try:
            r = subprocess.run(
                ["git", "remote", "set-url", "origin", new_url],
                cwd=str(wd), capture_output=True, text=True, timeout=10,
            )
        except Exception:
            log.exception("[pat-rotate] product=%s set-url crashed", pid)
            skipped += 1
            continue

        if r.returncode == 0:
            log.info(
                "[pat-rotate] product=%s rotated (was %s…%s → %s…%s)",
                pid,
                old_token[:7], old_token[-4:],
                new_pat[:7],   new_pat[-4:],
            )
            updated += 1
        else:
            log.warning(
                "[pat-rotate] product=%s set-url failed: %s",
                pid, r.stderr.strip()[:200],
            )
            skipped += 1

    return (updated, skipped)


def _redact(url: str) -> str:
    """Mask any embedded token before logging."""
    return _PAT_URL_RE.sub(lambda m: m.group(1) + "***" + m.group(3), url)


# ── CLI entrypoint ──────────────────────────────────────────────────────────
# `python -m orchestrator.pat_rotate` rotates every product to whatever PAT is
# currently in system_config. Useful one-shot after a manual PAT rotation.

def _main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=15) as client:
            cfg = client.get("/api/system-config").json()
            new_pat = cfg.get("github_pat") or ""
            products = client.get("/api/products").json()
    except Exception:
        log.exception("could not fetch system-config or products from PM API")
        return 1

    if not new_pat:
        log.error("system_config.github_pat is empty — nothing to rotate to")
        return 1
    if not isinstance(products, list):
        log.error("expected products list, got %r", type(products).__name__)
        return 1

    updated, skipped = rotate_pat(products, new_pat)
    log.info("[pat-rotate] done: updated=%d skipped=%d total=%d",
             updated, skipped, updated + skipped)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
