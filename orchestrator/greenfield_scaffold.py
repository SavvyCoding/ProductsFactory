"""
Greenfield scaffolding — called by the poller for products in 'greenfield_pending' status.

Steps:
  1. Create a private GitHub repo via POST /orgs/{org}/repos using the
     GitHub App's installation token (no PAT).
  2. Create the local working_dir folder.
  3. git init -b main + add origin (HTTPS URL, no token).
  4. Write README.md and product_config.json.
  5. Commit and push the initial scaffold via git_push_authenticated —
     the App installation token is supplied via a one-shot credential
     helper, never persisted to .git/config.
  6. Create AI-suggested features as 'Pending' in DB.
  7. Update Product status → 'registered' (triggers normal discovery
     on next cycle).

Migration note (2026-05-14):
  Pre-migration this module also generated per-product Ed25519 SSH
  deploy keys, uploaded them to GitHub, and appended Host blocks to
  ~/.ssh/config. All of that is gone — the App's installation token
  handles every authenticated operation. The ``ssh_dir`` argument is
  retained on the function signature only to keep the legacy poller
  callsite unchanged for one release; it is unused.
"""

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

import httpx

from orchestrator.paths import container_path

log = logging.getLogger("poller.scaffold")


def scaffold_greenfield(
    product: dict,
    system_config: dict,
    pm_api_url: str,
    ssh_dir: Optional[Path] = None,  # deprecated, ignored
):
    """
    Entry point — called by the poller when product['status'] == 'greenfield_pending'.
    Updates product status to 'registered' on success, 'error' on failure.
    """
    cfg = product.get("config") or {}
    github_repo_name   = cfg.get("github_repo_name", "")
    vision             = cfg.get("vision", "")
    preferred_stack    = cfg.get("preferred_stack", "python")
    suggested_features = cfg.get("suggested_features", [])
    product_name       = product.get("name") or github_repo_name

    org = system_config.get("github_org", "")
    # working_dir in the DB is the HOST path (so docker run -v can use it).
    # When we run inside the orchestrator container we must translate to the
    # container-side mount point for local FS ops (mkdir / git init / file
    # writes); container_path is a no-op in legacy host-poller mode.
    working_dir = Path(container_path(product["working_dir"]))

    # The App token is fetched per call from system_config via the central
    # helper; keep this here to bail loudly when the App isn't configured
    # rather than silently calling GitHub with no credentials.
    from orchestrator.integrations.github import _get_gh_token
    token = _get_gh_token()
    if not org or not token or not github_repo_name:
        log.error(
            f"Scaffold: missing config for product {product['id']} "
            f"(org={'ok' if org else 'MISSING'} "
            f"token={'ok' if token else 'MISSING'} "
            f"repo_name={'ok' if github_repo_name else 'MISSING'}) — marking error"
        )
        _patch_product(pm_api_url, product["id"], {"status": "error"})
        return

    try:
        # ① Create GitHub repo in the dedicated org.
        log.info(f"Scaffold [{product_name}]: creating GitHub repo {org}/{github_repo_name}")
        actual_owner = _create_github_repo(org, github_repo_name, token)
        https_url = f"https://github.com/{actual_owner}/{github_repo_name}.git"

        # ② Create local folder (idempotent — safe to re-run).
        working_dir.mkdir(parents=True, exist_ok=True)

        # ③ git init + remote.
        if not (working_dir / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=working_dir, check=True, capture_output=True,
            )
        # Disable filemode tracking. Docker Desktop on Windows can't reliably
        # persist Unix +x bits through bind-mounts; without this, agent
        # sessions trip on "Your local changes would be overwritten" for
        # scripts that toggle 0644↔0755 between commits. (Will revisit once
        # the workspace migrates to named volumes on Linux backend.)
        subprocess.run(
            ["git", "config", "core.fileMode", "false"],
            cwd=working_dir, capture_output=True,
        )
        # `remote add` fails if origin already exists — use set-url as a fallback
        # so the scaffold re-runs cleanly.
        add = subprocess.run(
            ["git", "remote", "add", "origin", https_url],
            cwd=working_dir, capture_output=True,
        )
        if add.returncode != 0:
            subprocess.run(
                ["git", "remote", "set-url", "origin", https_url],
                cwd=working_dir, check=True, capture_output=True,
            )

        # ④ Write scaffold files.
        _write_readme(working_dir, product_name)
        _write_product_config(working_dir, vision, preferred_stack, org, github_repo_name)

        # ⑤ Initial commit + push so `main` exists on GitHub. Without this,
        # the first coder session pushes its feature branch BEFORE main has
        # a ref upstream — `gh pr create` then fails with "No commits between
        # main and …".
        try:
            subprocess.run(["git", "config", "user.email", "orchestrator@productfactory.local"],
                           cwd=working_dir, capture_output=True)
            subprocess.run(["git", "config", "user.name", "ProductFactory Orchestrator"],
                           cwd=working_dir, capture_output=True)
            subprocess.run(["git", "add", "README.md", "product_config.json"],
                           cwd=working_dir, check=True, capture_output=True)
            commit = subprocess.run(
                ["git", "commit", "-m", "chore: initial scaffold"],
                cwd=working_dir, capture_output=True, text=True,
            )
            if commit.returncode == 0:
                # Push via the App-token credential helper — token never lands
                # in .git/config or in the URL.
                from orchestrator.integrations.git_ops import git_push_authenticated
                push = git_push_authenticated(
                    ["-u", "origin", "main"],
                    cwd=working_dir, product_name=product_name, timeout=60,
                )
                if push.returncode == 0:
                    log.info(f"Scaffold [{product_name}]: pushed initial main commit")
                else:
                    log.warning(
                        f"Scaffold [{product_name}]: initial push failed: "
                        f"{(push.stderr or '').strip()[:300]}"
                    )
            else:
                log.info(
                    f"Scaffold [{product_name}]: no initial commit needed "
                    f"({commit.stdout.strip()[:120]})"
                )
        except Exception as ce:
            log.warning(f"Scaffold [{product_name}]: initial commit/push step failed (non-fatal): {ce}")

        # ⑥ Create AI-suggested features as Pending.
        for i, feat in enumerate(suggested_features or []):
            name = (feat.get("name") or "").strip()
            desc = (feat.get("description") or "").strip()
            if name:
                _post_feature(pm_api_url, product["id"], name, desc, priority=50 + i)

        # ⑦ Update product → registered (poller discovery takes over next cycle).
        _patch_product(pm_api_url, product["id"], {
            "status": "registered",
            "github_repo": f"https://github.com/{actual_owner}/{github_repo_name}",
            "tech_stack": [preferred_stack],
        })
        log.info(f"Scaffold [{product_name}]: complete — {working_dir}")

    except subprocess.CalledProcessError as e:
        log.error(f"Scaffold [{product_name}]: shell command failed: {e.stderr.decode()}")
        _patch_product(pm_api_url, product["id"], {"status": "error"})
    except Exception as e:
        log.exception(f"Scaffold [{product_name}]: unexpected error: {e}")
        _patch_product(pm_api_url, product["id"], {"status": "error"})


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _create_github_repo(org: str, repo_name: str, token: str) -> str:
    """Create a private repo in the dedicated org. Returns the actual owner login.

    Authenticates with the GitHub App installation token. Requires the App
    to have ``Administration: write`` on the org and to be installed there.
    No user-account fallback — under the dedicated-org model, repos always
    live in the org so a failure here is a real configuration problem
    that should bubble up as an 'error' product status.

    Idempotent: a pre-existing repo with the same name returns its owner
    without raising (so re-running a partially-completed scaffold is safe).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {"name": repo_name, "private": True, "auto_init": False}

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers, json=payload,
        )
        if resp.status_code == 422 and "already exists" in resp.text:
            log.warning(f"Repo {org}/{repo_name} already exists on GitHub — reusing it")
            return org
        resp.raise_for_status()
        data = resp.json()
        actual_owner = data["owner"]["login"]
        log.info(f"Created GitHub repo: {actual_owner}/{repo_name}")
        return actual_owner


# ── Scaffold file writers ─────────────────────────────────────────────────────

def _write_readme(working_dir: Path, product_name: str):
    (working_dir / "README.md").write_text(
        f"# {product_name}\n\n"
        "See [ARCHITECTURE.md](ARCHITECTURE.md) for the product vision and technical design.\n",
        encoding="utf-8",
    )


def _write_product_config(
    working_dir: Path,
    vision: str,
    stack: str,
    org: str,
    repo_name: str,
):
    config = {
        "vision": vision,
        "preferred_stack": stack,
        "github_org": org,
        "github_repo_name": repo_name,
    }
    (working_dir / "product_config.json").write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ── PM API helpers ────────────────────────────────────────────────────────────

def _patch_product(pm_api_url: str, product_id: int, updates: dict):
    with httpx.Client(base_url=pm_api_url, timeout=10) as client:
        client.patch(f"/api/products/{product_id}", json=updates)


def _post_feature(pm_api_url: str, product_id: int, name: str, description: str, priority: int):
    with httpx.Client(base_url=pm_api_url, timeout=10) as client:
        client.post("/api/features", json={
            "product_id": product_id,
            "name": name,
            "description": description,
            "priority": priority,
            "source": "ai",
        })
