#!/usr/bin/env python3
"""
test_calculator.py — End-to-end test: create "Calculator" greenfield product and
run one full agent cycle (scaffold → discover → coder session → GitHub PR).

Steps:
  1. Ensure ~/.ssh exists
  2. Register Calculator as greenfield_pending via PM API
  3. Scaffold: create GitHub repo, generate SSH deploy key, init local git repo
  4. Discover: install AGENT_WORKFLOW.md / CLAUDE.md / ARCHITECTURE.md
  5. Add "Basic arithmetic operations" feature and approve it
  6. Run coder session (Ollama or Claude) — real git push + gh pr create

Usage:
    python scripts/test_calculator.py
    python scripts/test_calculator.py --dry-run   # skip agent session (just scaffold)
    python scripts/test_calculator.py --persona designer  # run designer instead
"""

import argparse
import os
import sys
import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Load .env
_env_file = REPO_ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            k, _, v = _line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

import httpx

PM_API_URL   = os.environ.get("PM_API_URL", "http://localhost:8080")
PM_USERNAME  = os.environ.get("PM_USERNAME", "digvi")
PM_PASSWORD  = os.environ.get("PM_PASSWORD", "admin123")
SSH_DIR      = Path(os.environ.get("SSH_DIR", "C:/Users/digvi/.ssh"))
PRODUCTS_DIR = Path(os.environ.get("PRODUCTS_BASE_DIR", "C:/Users/digvi/Personal/Products"))
WORKING_DIR  = PRODUCTS_DIR / "Calculator"

PRODUCT_NAME  = "Calculator"
REPO_NAME     = "calculator"
VISION        = "A simple calculator web app with REST API and clean UI."
STACK         = "python"

FEATURES = [
    {
        "name":        "Basic arithmetic operations",
        "description": "REST API endpoints: POST /calculate with body {op: 'add'|'sub'|'mul'|'div', a: float, b: float}. Returns {result: float}. Handle division by zero with 400 error.",
        "priority":    90,
    },
    {
        "name":        "Calculation history",
        "description": "Store each calculation in SQLite. GET /history returns last 20 results with timestamp, operation, inputs, result.",
        "priority":    70,
    },
    {
        "name":        "Health check endpoint",
        "description": "GET /health returns {status: 'ok', version: '1.0.0'}.",
        "priority":    50,
    },
]


def step(msg: str):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print('='*60)


def api(method: str, path: str, **kwargs):
    with httpx.Client(base_url=PM_API_URL, timeout=30,
                      auth=(PM_USERNAME, PM_PASSWORD)) as c:
        resp = getattr(c, method)(path, **kwargs)
        resp.raise_for_status()
        return resp.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run",  action="store_true", help="Scaffold only — skip agent session")
    parser.add_argument("--persona",  default="coder", choices=["coder", "designer", "planner"])
    parser.add_argument("--no-push",  action="store_true", help="Block real git push / gh pr create")
    args = parser.parse_args()

    # ── 0. Pre-flight ─────────────────────────────────────────────────────────
    step("0 / Pre-flight checks")
    SSH_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  SSH dir   : {SSH_DIR}")
    print(f"  Products  : {PRODUCTS_DIR}")
    print(f"  Working   : {WORKING_DIR}")
    print(f"  PM API    : {PM_API_URL}")

    try:
        api("get", "/api/products")
        print("  PM API    : reachable ✓")
    except Exception as e:
        sys.exit(f"  PM API not reachable: {e}")

    # ── 1. Fetch system config (need PAT + org) ───────────────────────────────
    step("1 / Fetch system config")
    sys_cfg = api("get", "/api/system-config")
    github_org = sys_cfg.get("github_org") or ""
    github_pat = sys_cfg.get("github_pat") or ""
    if not github_org or not github_pat:
        sys.exit("ERROR: github_org and github_pat must be set in Admin → System settings.")
    print(f"  GitHub org: {github_org}")
    print(f"  PAT       : {github_pat[:12]}…")

    # ── 2. Register Calculator product ───────────────────────────────────────
    step("2 / Register Calculator product")

    # Check if already exists
    products = api("get", "/api/products")
    existing = next((p for p in products if p["name"] == PRODUCT_NAME), None)

    if existing:
        product_id = existing["id"]
        print(f"  Already exists — id={product_id} status={existing['status']}")
        product = existing
    else:
        PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
        product = api("post", "/api/products", json={
            "working_dir": str(WORKING_DIR),
            "name":        PRODUCT_NAME,
            "type":        "greenfield",
            "status":      "greenfield_pending",
            "config": {
                "github_repo_name":  REPO_NAME,
                "vision":            VISION,
                "preferred_stack":   STACK,
                "suggested_features": [],
            },
        })
        product_id = product["id"]
        print(f"  Created — id={product_id}")

    # ── 3. Scaffold (GitHub repo + SSH key + local git init) ──────────────────
    if product.get("status") == "greenfield_pending" or not WORKING_DIR.exists():
        step("3 / Scaffold greenfield (GitHub repo + SSH key)")
        from orchestrator.greenfield_scaffold import scaffold_greenfield
        scaffold_greenfield(
            product=api("get", f"/api/products/{product_id}"),
            system_config=sys_cfg,
            pm_api_url=PM_API_URL,
            ssh_dir=SSH_DIR,
        )
        product = api("get", f"/api/products/{product_id}")
        print(f"  Status after scaffold: {product['status']}")
        if product["status"] == "error":
            sys.exit("Scaffold failed — check logs above.")
    else:
        print(f"  Skipped — working dir already exists ({product['status']})")

    # ── 4. Discover (install templates, set ready) ────────────────────────────
    if product.get("status") == "registered":
        step("4 / Discover product (install agent templates)")
        from orchestrator.setup_product import discover_and_populate
        discover_and_populate(api("get", f"/api/products/{product_id}"))
        product = api("get", f"/api/products/{product_id}")
        # Force ready
        if product["status"] in ("discovered", "discovering"):
            api("patch", f"/api/products/{product_id}", json={"status": "ready"})
            product = api("get", f"/api/products/{product_id}")
        print(f"  Status after discovery: {product['status']}")
    else:
        step("4 / Discovery")
        print(f"  Skipped — status is '{product['status']}'")

    # ── 5. Add features ───────────────────────────────────────────────────────
    step("5 / Add and approve features")
    existing_features = api("get", f"/api/products/{product_id}/features")
    existing_names    = {f["name"] for f in existing_features}

    added = 0
    for feat in FEATURES:
        if feat["name"] in existing_names:
            print(f"  Already exists: {feat['name']}")
            continue
        f = api("post", "/api/features", json={
            "product_id":  product_id,
            "name":        feat["name"],
            "description": feat["description"],
            "priority":    feat["priority"],
            "source":      "pm",
        })
        # Approve immediately
        api("patch", f"/api/features/{f['id']}/pm-status", json={"status": "Approved"})
        print(f"  Added + Approved: {feat['name']}")
        added += 1

    # Also approve any Pending features already there
    for f in api("get", f"/api/products/{product_id}/features"):
        if f["status"] == "Pending":
            api("patch", f"/api/features/{f['id']}/pm-status", json={"status": "Approved"})
            print(f"  Approved existing: {f['name']}")

    if args.dry_run:
        step("Dry run complete — skipping agent session")
        print(f"  Product id : {product_id}")
        print(f"  Working dir: {WORKING_DIR}")
        print(f"\n  To run the agent manually:")
        print(f"    python scripts/test_run.py --product-id {product_id} --working-dir \"{WORKING_DIR}\" --persona coder --allow-push")
        return

    # ── 6. Run agent session ──────────────────────────────────────────────────
    step(f"6 / Run {args.persona} agent session")

    allow_push_flag = [] if args.no_push else ["--allow-push"]
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "test_run.py"),
        "--product-id",  str(product_id),
        "--working-dir", str(WORKING_DIR),
        "--persona",     args.persona,
        *allow_push_flag,
    ]
    print(f"  Command: {' '.join(cmd)}")
    print(f"  Push to GitHub: {'YES' if not args.no_push else 'NO (--no-push)'}")
    print()

    result = subprocess.run(cmd)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
