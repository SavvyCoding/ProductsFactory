"""
Recover products and features from features.md files on disk.

Usage:
    python scripts/recover_db.py

Reads PRODUCTS_BASE_DIR from .env, scans each product directory for features.md,
re-registers the product, and reseeds features with their last-known statuses.

Status mapping on recovery:
  Pushed      → Pushed       (already shipped — keep as-is)
  Reviewing   → Approved     (PR may still exist on GitHub; reset so poller reconciles)
  Reviewed    → Approved     (reviewed but not merged; reset)
  Implemented → Approved     (reset)
  Pending     → Pending
  Blocked     → Pending      (unblock on restore)
  Deferred    → Deferred
  Rejected    → Rejected
"""

import os
import re
import sys
from pathlib import Path

# Load .env
_env = Path(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import httpx

PM_API_URL = os.environ.get("PM_API_URL", "http://localhost:8080")
PM_USERNAME = os.environ.get("PM_USERNAME", "")
PM_PASSWORD = os.environ.get("PM_PASSWORD", "")
AUTH = (PM_USERNAME, PM_PASSWORD) if PM_USERNAME else None
PRODUCTS_BASE_DIR = os.environ.get("PRODUCTS_BASE_DIR", "")

STATUS_MAP = {
    "pushed":      "Pushed",
    "reviewing":   "Approved",
    "reviewed":    "Approved",
    "implemented": "Approved",
    "pending":     "Pending",
    "approved":    "Approved",
    "designing":   "Approved",
    "designed":    "Approved",
    "blocked":     "Pending",
    "deferred":    "Deferred",
    "rejected":    "Rejected",
    "reverted":    "Pending",
}

TYPE_MAP = {
    "feature": "feature",
    "bug":     "bug",
    "chore":   "chore",
}

EMOJI_RE = re.compile(r"[^\x00-\x7F🔍✅❌⚠️]+", re.UNICODE)

def clean_status(raw: str) -> str:
    """Strip emoji and map to valid DB status."""
    clean = EMOJI_RE.sub("", raw).strip().lower()
    return STATUS_MAP.get(clean, "Pending")

def parse_features_md(path: Path) -> list[dict]:
    """Parse the markdown table in features.md — returns list of feature dicts."""
    features = []
    in_table = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("| ID |"):
            in_table = True
            continue
        if in_table and line.startswith("|---"):
            continue
        if in_table and line.startswith("|"):
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) < 5:
                continue
            try:
                fid    = int(cols[0])
                name   = cols[1].strip()
                status = clean_status(cols[2])
                prio   = int(cols[3]) if cols[3].isdigit() else 50
                ftype  = TYPE_MAP.get(cols[4].strip().lower(), "feature")
                features.append({
                    "orig_id": fid,
                    "name": name,
                    "status": status,
                    "priority": prio,
                    "feature_type": ftype,
                })
            except (ValueError, IndexError):
                continue
        elif in_table and not line.startswith("|"):
            in_table = False
    return features


def get_github_remote(working_dir: str) -> str | None:
    """Read git remote URL from .git/config."""
    cfg = Path(working_dir) / ".git" / "config"
    if not cfg.exists():
        return None
    text = cfg.read_text(encoding="utf-8")
    m = re.search(r'url\s*=\s*(.+)', text)
    return m.group(1).strip() if m else None


def recover_product(working_dir: str) -> int | None:
    """Re-register a product. Returns new product_id or None on failure."""
    name = Path(working_dir).name
    github_repo = get_github_remote(working_dir)

    with httpx.Client(base_url=PM_API_URL, timeout=15, auth=AUTH) as client:
        resp = client.post("/api/products", json={
            "working_dir": working_dir,
            "name": name,
            "type": "brownfield",
            "status": "ready",
            "github_repo": github_repo,
        })
        if resp.status_code in (200, 201):
            pid = resp.json()["id"]
            print(f"  ✓ Registered product '{name}' → id={pid} (github={github_repo})")
            return pid
        elif resp.status_code == 409:
            # Already exists
            existing = client.get("/api/products").json()
            for p in existing:
                if p["working_dir"] == working_dir:
                    print(f"  ~ Product '{name}' already exists → id={p['id']}")
                    return p["id"]
        print(f"  ✗ Failed to register '{name}': {resp.status_code} {resp.text[:120]}")
        return None


def recover_features(product_id: int, features: list[dict]):
    """Seed features from the parsed features.md list."""
    pushed = [f for f in features if f["status"] == "Pushed"]
    actionable = [f for f in features if f["status"] not in ("Pushed", "Rejected", "Deferred")]
    other = [f for f in features if f["status"] in ("Rejected", "Deferred")]

    # Deduplicate by name (keep highest priority, prefer Pushed over Pending)
    seen: dict[str, dict] = {}
    for f in features:
        key = f["name"].lower().strip()
        if key not in seen:
            seen[key] = f
        else:
            # Prefer Pushed > Approved > Pending; higher priority wins ties
            existing = seen[key]
            rank = {"Pushed": 0, "Approved": 1, "Pending": 2, "Deferred": 3, "Rejected": 4}
            if rank.get(f["status"], 5) < rank.get(existing["status"], 5):
                seen[key] = f
            elif rank.get(f["status"], 5) == rank.get(existing["status"], 5):
                if f["priority"] > existing["priority"]:
                    seen[key] = f

    deduped = list(seen.values())
    print(f"  Features: {len(features)} in file → {len(deduped)} after dedup")

    created = 0
    with httpx.Client(base_url=PM_API_URL, timeout=15, auth=AUTH) as client:
        for f in deduped:
            # Create as Pending first (only valid create status)
            resp = client.post("/api/features", json={
                "product_id": product_id,
                "name": f["name"],
                "priority": f["priority"],
                "feature_type": f["feature_type"],
                "source": "pm",
            })
            if resp.status_code not in (200, 201):
                print(f"    ✗ Could not create '{f['name']}': {resp.status_code}")
                continue

            fid = resp.json()["id"]

            # If status should be non-Pending, patch it directly via the internal API
            if f["status"] != "Pending":
                patch = client.patch(f"/api/features/{fid}", json={"status": f["status"]})
                if patch.status_code != 200:
                    print(f"    ✗ Could not set status '{f['status']}' for '{f['name']}'")

            created += 1

    print(f"  ✓ Seeded {created}/{len(deduped)} features")


def main():
    if not PRODUCTS_BASE_DIR:
        print("ERROR: PRODUCTS_BASE_DIR not set in .env")
        sys.exit(1)

    base = Path(PRODUCTS_BASE_DIR)
    if not base.exists():
        print(f"ERROR: PRODUCTS_BASE_DIR does not exist: {base}")
        sys.exit(1)

    # Restore system_config settings from .env
    print("\n── Restoring system_config ──")
    cfg = {}
    for key, env_var in [
        ("github_pat", "GITHUB_PAT"),
        ("github_org", "GITHUB_ORG"),
        ("products_root_dir", "PRODUCTS_BASE_DIR"),
    ]:
        val = os.environ.get(env_var)
        if val:
            cfg[key] = val

    if cfg:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/admin/settings", data=cfg)
            # Admin settings endpoint expects form data; try JSON fallback
            if resp.status_code not in (200, 303):
                # Try patching system_config directly via an internal endpoint if available
                pass
        print(f"  Note: re-enter github_pat and other secrets via http://localhost:8080/admin")
    else:
        print("  Note: re-enter all settings via http://localhost:8080/admin")

    # Recover each product directory
    print(f"\n── Scanning {base} ──")
    product_dirs = [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]

    if not product_dirs:
        print("  No product directories found.")
        return

    for product_dir in sorted(product_dirs):
        print(f"\n── Product: {product_dir.name} ──")
        features_md = product_dir / "features.md"

        product_id = recover_product(str(product_dir))
        if product_id is None:
            continue

        if features_md.exists():
            features = parse_features_md(features_md)
            print(f"  Found {len(features)} features in features.md")
            recover_features(product_id, features)
        else:
            print(f"  No features.md found — product registered but no features seeded")

    print("\n── Done ──")
    print("Next steps:")
    print("  1. Open http://localhost:8080/admin and re-enter github_pat, github_org")
    print("  2. Review features at http://localhost:8080 — Pushed features are already marked done")
    print("  3. Approve the features you want the agents to work on next")


if __name__ == "__main__":
    main()
