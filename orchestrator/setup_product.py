"""
Auto-discovery: reads a product folder and populates the DB.
Runs once per product (when status == 'registered').

Discovers:
  - name          from README.md first H1 heading
  - github_repo   from git remote origin
  - tech_stack    from presence of requirements.txt, package.json, go.mod, etc.
  - type          greenfield vs brownfield (source file count heuristic)
  - config        product_config.json (if exists)
"""

import os
import re
import json
import logging
import subprocess
from pathlib import Path

import httpx

from templates.renderer import install_templates

log = logging.getLogger("poller.setup")

PM_API_URL = os.environ["PM_API_URL"]
# Agents run inside Docker — they must use the container-accessible URL, not the host URL
PM_API_URL_CONTAINER = os.environ.get("PM_API_URL_CONTAINER", PM_API_URL)

# Brownfield threshold: if more than this many source files exist → brownfield
BROWNFIELD_FILE_THRESHOLD = int(os.environ.get("BROWNFIELD_FILE_THRESHOLD", "10"))


def discover_and_populate(product: dict):
    """
    Entry point — called by poller for each registered product (status=registered).

    1. Reads the working directory to populate DB fields.
    2. Installs AGENT_WORKFLOW.md, CLAUDE.md, ARCHITECTURE.md into the repo
       (only if they don't already exist — safe to re-run).
    3. Sets status → 'ready' for brownfield (existing repo, intentionally registered),
       or 'discovered' for greenfield (PM reviews scaffold before agents run).
    """
    working_dir = Path(product["working_dir"])
    if not working_dir.exists():
        log.error(f"Working dir does not exist: {working_dir}")
        _update_product(product["id"], {"status": "error"})
        return

    _update_product(product["id"], {"status": "discovering"})

    updates: dict = {}
    updates["name"]   = _discover_name(working_dir)

    # Don't overwrite github_repo if already set (greenfield products set this during scaffold)
    if not product.get("github_repo"):
        updates["github_repo"] = _discover_github_repo(working_dir)

    # If product was greenfield-scaffolded (has preferred_stack in config), trust those values
    existing_config = product.get("config") or {}
    if existing_config.get("preferred_stack"):
        # Greenfield: use pre-set stack; don't re-detect type (already 'greenfield')
        if not product.get("tech_stack"):
            updates["tech_stack"] = [existing_config["preferred_stack"]]
    else:
        # Brownfield or untyped: scan the working dir
        updates["tech_stack"] = _discover_tech_stack(working_dir)
        updates["type"]       = _detect_product_type(working_dir)

    updates["config"] = _load_product_config(working_dir)

    # Merge discovered data into product dict so renderer has full context
    merged = {**product, **updates}

    # Install template files (skips any that already exist)
    try:
        written = install_templates(merged, PM_API_URL_CONTAINER, force=False)
        if written:
            log.info(f"Templates installed: {written}")
    except Exception as e:
        log.warning(f"Template install failed (non-fatal): {e}")

    product_type = updates.get("type") or product.get("type", "brownfield")
    updates["status"] = "discovered" if product_type == "greenfield" else "ready"
    _update_product(product["id"], updates)
    log.info(f"Discovery complete for '{updates['name']}': type={product_type} stack={updates.get('tech_stack', product.get('tech_stack'))} → {updates['status']}")


def _discover_name(working_dir: Path) -> str | None:
    readme = working_dir / "README.md"
    if readme.exists():
        for line in readme.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = re.match(r"^#\s+(.+)", line.strip())
            if match:
                return match.group(1).strip()
    return working_dir.name  # fallback to folder name


def _discover_github_repo(working_dir: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=working_dir, capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _discover_tech_stack(working_dir: Path) -> list[str]:
    stack = []
    indicators = {
        "python":     ["requirements.txt", "pyproject.toml", "setup.py"],
        "node":       ["package.json"],
        "go":         ["go.mod"],
        "rust":       ["Cargo.toml"],
        "java":       ["pom.xml", "build.gradle"],
        "dotnet":     ["*.csproj", "*.sln"],
        "ruby":       ["Gemfile"],
    }
    for tech, files in indicators.items():
        for pattern in files:
            if list(working_dir.glob(pattern)):
                stack.append(tech)
                break
    return stack


def _detect_product_type(working_dir: Path) -> str:
    """Count source files to decide greenfield vs brownfield."""
    source_extensions = {".py", ".js", ".ts", ".go", ".rs", ".java", ".cs", ".rb"}
    count = sum(
        1 for f in working_dir.rglob("*")
        if f.is_file()
        and f.suffix in source_extensions
        and ".git" not in f.parts
        and "node_modules" not in f.parts
        and "venv" not in f.parts
    )
    return "brownfield" if count > BROWNFIELD_FILE_THRESHOLD else "greenfield"


def _load_product_config(working_dir: Path) -> dict | None:
    config_path = working_dir / "product_config.json"
    if config_path.exists():
        try:
            return json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning(f"Invalid product_config.json in {working_dir}")
    return None


def _update_product(product_id: int, updates: dict) -> bool:
    """PATCH product fields. Returns True on success, False on failure."""
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.patch(f"/api/products/{product_id}", json=updates)
            resp.raise_for_status()
            return True
    except Exception as e:
        log.error(f"Failed to update product {product_id} with {list(updates.keys())}: {e}")
        return False
