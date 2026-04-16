"""
features_sync.py — Sync features.md → DB via PM API.

Called at poller startup to self-heal feature state after a DB wipe or manual edit.
Also exposed as POST /api/products/{id}/sync-features for on-demand sync.
"""

import logging
import re
from pathlib import Path

import httpx

log = logging.getLogger("poller.features_sync")

# ── Status mapping: features.md value → canonical DB status ──────────────────
# Transient in-flight states are reset to a "ready" state so agents can re-pick them.
_STATUS_MAP: dict[str, str] = {
    "Pushed":       "Pushed",
    "Approved":     "Approved",
    "Reviewing":    "Approved",     # Reset — PR may be stale
    "Reviewed":     "Reviewed",
    "Designed":     "Designed",
    "Designing":    "Approved",     # Reset
    "Implemented":  "Approved",     # Reset
    "Implementing": "Approved",     # Reset
    "Pending":      "Pending",
    "Blocked":      "Pending",      # Unblock
    "Deferred":     "Deferred",
    "Rejected":     "Rejected",
    "Reverted":     "Pending",
    "Testing":      "Approved",     # Reset
    "Committed":    "Pushed",       # Treat as pushed
}


def _strip_to_ascii_word(cell: str) -> str:
    """
    Strip emojis and non-ASCII characters from a cell value, then strip whitespace.
    e.g. "🔍 Reviewing" → "Reviewing"
    """
    # Remove non-ASCII (covers all emoji ranges)
    cleaned = re.sub(r"[^\x00-\x7F]", "", cell)
    return cleaned.strip()


def _parse_features_md(content: str) -> list[dict]:
    """
    Parse a Markdown table with columns: | ID | Name | Status | Priority | Type |
    (column order may vary — detected from the header row).

    Returns a list of dicts with keys: id (int), name (str), status (str),
    priority (int), feature_type (str).

    Skips header rows, separator rows, blank lines, and malformed rows.
    """
    features = []
    header_cols: list[str] | None = None

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("|"):
            continue

        # Split into cells, strip whitespace, drop empty leading/trailing empty cells
        cells = [c.strip() for c in line.split("|")]
        # Remove empty strings from leading/trailing split artefacts
        cells = [c for c in cells if c != ""]

        if not cells:
            continue

        # Detect separator rows like |---|---|---|
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue

        # Detect header row — contains 'ID' (case-insensitive)
        lowered = [c.lower() for c in cells]
        if "id" in lowered:
            header_cols = lowered
            continue

        # Data row — requires a parsed header
        if header_cols is None:
            continue

        # Map by header position
        def _get(col_name: str) -> str:
            try:
                idx = header_cols.index(col_name)
                return cells[idx] if idx < len(cells) else ""
            except ValueError:
                return ""

        raw_id       = _get("id")
        raw_name     = _get("name")
        raw_status   = _strip_to_ascii_word(_get("status"))
        raw_priority = _get("priority")
        raw_type     = _strip_to_ascii_word(_get("type"))

        # Validate ID
        try:
            feature_id = int(raw_id)
        except (ValueError, TypeError):
            continue

        name = raw_name.strip() if raw_name else f"Feature {feature_id}"

        # Priority default 50
        try:
            priority = int(raw_priority)
            priority = max(1, min(100, priority))
        except (ValueError, TypeError):
            priority = 50

        # Feature type default "feature"
        ftype = raw_type.lower() if raw_type else "feature"
        if ftype not in ("feature", "bug", "chore"):
            ftype = "feature"

        features.append({
            "id":           feature_id,
            "name":         name,
            "status":       raw_status,
            "priority":     priority,
            "feature_type": ftype,
        })

    return features


def sync_features_from_md(product: dict, pm_api_url: str) -> int:
    """
    Read {working_dir}/features.md and sync statuses to the DB via PM API.

    Logic:
    - Parse the markdown table (| ID | Name | Status | Priority | Type |)
    - For each row:
        - If a feature with that ID exists in DB: update status only if different
        - If no feature with that ID: create it with the mapped status
    - Returns count of changes applied.

    Status mapping (features.md raw → DB status):
        Pushed      → Pushed
        Approved    → Approved
        Reviewing   → Approved   (reset — PR may be stale)
        Reviewed    → Reviewed
        Designed    → Designed
        Designing   → Approved   (reset)
        Implemented → Approved   (reset)
        Implementing → Approved  (reset)
        Pending     → Pending
        Blocked     → Pending    (unblock)
        Deferred    → Deferred
        Rejected    → Rejected
        Reverted    → Pending

    Uses httpx (sync). No auth required (internal API).
    Only updates status — never touches name, description, or priority
    (those are PM-controlled).
    Logs every change at INFO level.
    Skips features already in sync (no-op).
    """
    working_dir = product.get("working_dir", "")
    product_id  = product.get("id")
    product_name = product.get("name") or str(product_id)

    if not working_dir:
        log.debug(f"Product {product_name}: no working_dir — skipping features.md sync")
        return 0

    features_md_path = Path(working_dir) / "features.md"
    if not features_md_path.exists():
        log.debug(f"Product {product_name}: {features_md_path} not found — skipping sync")
        return 0

    try:
        content = features_md_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning(f"Product {product_name}: could not read features.md: {e}")
        return 0

    parsed = _parse_features_md(content)
    if not parsed:
        log.debug(f"Product {product_name}: features.md has no parseable feature rows")
        return 0

    log.info(f"Product {product_name}: found {len(parsed)} feature row(s) in features.md")

    changes = 0

    with httpx.Client(base_url=pm_api_url, timeout=15) as client:
        for row in parsed:
            feature_id   = row["id"]
            md_status    = row["status"]
            target_status = _STATUS_MAP.get(md_status)

            if target_status is None:
                log.warning(
                    f"Product {product_name}: feature #{feature_id} has unknown status "
                    f"{md_status!r} — skipping"
                )
                continue

            # Fetch the feature from DB
            try:
                resp = client.get(f"/api/features/{feature_id}")
            except httpx.HTTPError as e:
                log.warning(f"Product {product_name}: GET /api/features/{feature_id} failed: {e}")
                continue

            if resp.status_code == 200:
                db_feature = resp.json()
                db_status  = db_feature.get("status", "")

                # Verify the feature belongs to this product
                if db_feature.get("product_id") != product_id:
                    log.debug(
                        f"Feature #{feature_id} belongs to product "
                        f"{db_feature.get('product_id')}, not {product_id} — skipping"
                    )
                    continue

                if db_status == target_status:
                    log.debug(
                        f"Feature #{feature_id} already at {target_status!r} — no-op"
                    )
                    continue

                # Status differs — update only status
                try:
                    patch_resp = client.patch(
                        f"/api/features/{feature_id}",
                        json={"status": target_status},
                    )
                    patch_resp.raise_for_status()
                    log.info(
                        f"Product {product_name}: feature #{feature_id} "
                        f"status {db_status!r} → {target_status!r} (from features.md: {md_status!r})"
                    )
                    changes += 1
                except httpx.HTTPError as e:
                    log.warning(
                        f"Product {product_name}: PATCH /api/features/{feature_id} failed: {e}"
                    )

            elif resp.status_code == 404:
                # Feature doesn't exist in DB — create it
                try:
                    create_resp = client.post(
                        "/api/features",
                        json={
                            "product_id":   product_id,
                            "name":         row["name"],
                            "status":       target_status,
                            "priority":     row["priority"],
                            "feature_type": row["feature_type"],
                            "source":       "pm",
                        },
                    )
                    create_resp.raise_for_status()
                    new_feature = create_resp.json()
                    log.info(
                        f"Product {product_name}: created missing feature #{feature_id} "
                        f"(new DB id={new_feature.get('id')}) "
                        f"name={row['name']!r} status={target_status!r}"
                    )
                    changes += 1
                except httpx.HTTPError as e:
                    log.warning(
                        f"Product {product_name}: POST /api/features failed for "
                        f"feature #{feature_id}: {e}"
                    )
            else:
                log.warning(
                    f"Product {product_name}: unexpected status {resp.status_code} "
                    f"fetching feature #{feature_id}"
                )

    log.info(f"Product {product_name}: features.md sync complete — {changes} change(s) applied")
    return changes
