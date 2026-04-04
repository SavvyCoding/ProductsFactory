"""
Greenfield scaffolding — called by the poller for products in 'greenfield_pending' status.

Steps:
  1. Create a private GitHub repo via the REST API
  2. Generate an Ed25519 SSH deploy key pair (ssh-keygen)
  3. Upload the public key to GitHub
  4. Create the local working_dir folder
  5. git init -b main + git remote add origin
  6. Write README.md and product_config.json
  7. Create AI-suggested features as 'Pending' in DB
  8. Update Product status → 'registered' (triggers normal discovery on next cycle)
"""

import json
import logging
import subprocess
from pathlib import Path

import httpx

log = logging.getLogger("poller.scaffold")


def scaffold_greenfield(product: dict, system_config: dict, pm_api_url: str, ssh_dir: Path):
    """
    Entry point — called by the poller when product['status'] == 'greenfield_pending'.
    Updates product status to 'registered' on success, 'error' on failure.
    """
    cfg = product.get("config") or {}
    github_repo_name  = cfg.get("github_repo_name", "")
    vision            = cfg.get("vision", "")
    preferred_stack   = cfg.get("preferred_stack", "python")
    suggested_features = cfg.get("suggested_features", [])
    product_name      = product.get("name") or github_repo_name

    org             = system_config.get("github_org", "")
    pat             = system_config.get("github_pat", "")
    ssh_key_name    = system_config.get("github_ssh_key_name", "productfactory-deploy")
    working_dir     = Path(product["working_dir"])

    if not org or not pat or not github_repo_name:
        log.error(f"Scaffold: missing config for product {product['id']} — marking error")
        _patch_product(pm_api_url, product["id"], {"status": "error"})
        return

    try:
        # ① Create GitHub repo
        log.info(f"Scaffold [{product_name}]: creating GitHub repo {org}/{github_repo_name}")
        ssh_url = _create_github_repo(org, github_repo_name, pat)

        # ② Generate deploy key on host
        key_slug = github_repo_name.lower().replace("-", "_").replace(".", "_")
        key_path = ssh_dir / f"id_ed25519_{key_slug}"
        log.info(f"Scaffold [{product_name}]: generating deploy key → {key_path}")
        public_key = _generate_deploy_key(key_path)

        # ③ Upload public key to GitHub
        log.info(f"Scaffold [{product_name}]: uploading deploy key to GitHub")
        _add_deploy_key(org, github_repo_name, pat, public_key,
                        f"{ssh_key_name}-{key_slug}")

        # ④ Create local folder
        working_dir.mkdir(parents=True, exist_ok=False)

        # ⑤ git init + remote
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=working_dir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", ssh_url],
            cwd=working_dir, check=True, capture_output=True,
        )

        # ⑥ Write scaffold files
        _write_readme(working_dir, product_name)
        _write_product_config(working_dir, vision, preferred_stack, org, github_repo_name)

        # ⑦ Create AI-suggested features as Pending
        for i, feat in enumerate(suggested_features or []):
            name = (feat.get("name") or "").strip()
            desc = (feat.get("description") or "").strip()
            if name:
                _post_feature(pm_api_url, product["id"], name, desc, priority=50 + i)

        # ⑧ Update product → registered (poller discovery takes over next cycle)
        _patch_product(pm_api_url, product["id"], {
            "status": "registered",
            "github_repo": f"https://github.com/{org}/{github_repo_name}",
            "tech_stack": [preferred_stack],
        })
        log.info(f"Scaffold [{product_name}]: complete — {working_dir}")

    except FileExistsError:
        log.error(f"Scaffold [{product_name}]: working_dir already exists — {working_dir}")
        _patch_product(pm_api_url, product["id"], {"status": "error"})
    except subprocess.CalledProcessError as e:
        log.error(f"Scaffold [{product_name}]: shell command failed: {e.stderr.decode()}")
        _patch_product(pm_api_url, product["id"], {"status": "error"})
    except Exception as e:
        log.exception(f"Scaffold [{product_name}]: unexpected error: {e}")
        _patch_product(pm_api_url, product["id"], {"status": "error"})


# ── GitHub API helpers ────────────────────────────────────────────────────────

def _create_github_repo(org: str, repo_name: str, pat: str) -> str:
    """Create a private GitHub repo. Returns SSH clone URL."""
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {"name": repo_name, "private": True, "auto_init": False}

    with httpx.Client(timeout=30) as client:
        # Try org endpoint first — falls back to personal account if org returns 404/403
        resp = client.post(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers, json=payload,
        )
        if resp.status_code in (404, 403, 422):
            # 422 from org endpoint = not an org; try user repos
            if resp.status_code == 422 and "already exists" not in resp.text:
                resp = client.post(
                    "https://api.github.com/user/repos",
                    headers=headers, json=payload,
                )
        if resp.status_code == 422 and "already exists" in resp.text:
            log.warning(f"Repo {org}/{repo_name} already exists on GitHub — reusing it")
            return f"git@github.com:{org}/{repo_name}.git"
        resp.raise_for_status()
        return resp.json()["ssh_url"]


def _add_deploy_key(org: str, repo_name: str, pat: str, public_key: str, label: str):
    """Upload an SSH public key as a deploy key to a GitHub repo."""
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"https://api.github.com/repos/{org}/{repo_name}/keys",
            headers=headers,
            json={"title": label, "key": public_key, "read_only": False},
        )
        resp.raise_for_status()


# ── SSH key generation ────────────────────────────────────────────────────────

def _generate_deploy_key(key_path: Path) -> str:
    """
    Generate an Ed25519 SSH key pair using ssh-keygen.
    Returns the public key string (one-line OpenSSH format).
    """
    pub_path = Path(str(key_path) + ".pub")
    for p in (key_path, pub_path):
        if p.exists():
            p.unlink()

    subprocess.run(
        [
            "ssh-keygen",
            "-t", "ed25519",
            "-f", str(key_path),
            "-N", "",                     # no passphrase
            "-C", "productfactory-agent",
        ],
        check=True, capture_output=True,
    )
    key_path.chmod(0o600)
    return pub_path.read_text(encoding="utf-8").strip()


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
