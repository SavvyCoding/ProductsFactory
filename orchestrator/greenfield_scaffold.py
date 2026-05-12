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

from orchestrator.paths import container_path

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
    # working_dir in the DB is the HOST path (so docker run -v can use it).
    # When we run inside the orchestrator container we must translate to the
    # container-side mount point for local FS ops (mkdir / git init / file
    # writes); container_path is a no-op in legacy host-poller mode.
    working_dir     = Path(container_path(product["working_dir"]))

    if not org or not pat or not github_repo_name:
        log.error(f"Scaffold: missing config for product {product['id']} — marking error")
        _patch_product(pm_api_url, product["id"], {"status": "error"})
        return

    try:
        # ① Create GitHub repo
        log.info(f"Scaffold [{product_name}]: creating GitHub repo {org}/{github_repo_name}")
        ssh_url, actual_owner = _create_github_repo(org, github_repo_name, pat)
        # Use plain HTTPS for the remote URL — auth is handled at runtime by
        # git's credential helper, which the agent container's entrypoint
        # configures from $GH_TOKEN. Storing a PAT inside the URL leaks it
        # the moment any tool runs `git remote -v` — and that output flows
        # into the LLM's conversation context, then to Ollama Cloud.
        https_url = f"https://github.com/{actual_owner}/{github_repo_name}.git"
        # Helper URL with embedded PAT — only used by the orchestrator's
        # initial scaffold push so it can authenticate without going through
        # an entrypoint that doesn't run for direct subprocess calls.
        # Never persisted in .git/config.
        push_url = f"https://x-access-token:{pat}@github.com/{actual_owner}/{github_repo_name}.git"

        # ② Generate deploy key on host
        key_slug = github_repo_name.lower().replace("-", "_").replace(".", "_")
        key_path = ssh_dir / f"id_ed25519_{key_slug}"
        log.info(f"Scaffold [{product_name}]: generating deploy key → {key_path}")
        public_key = _generate_deploy_key(key_path)

        # ③ Upload public key to GitHub (use actual_owner in case we fell back to user account)
        log.info(f"Scaffold [{product_name}]: uploading deploy key to GitHub ({actual_owner}/{github_repo_name})")
        _add_deploy_key(actual_owner, github_repo_name, pat, public_key,
                        f"{ssh_key_name}-{key_slug}")

        # ④ Create local folder (idempotent — safe to re-run after a partial prior scaffold)
        working_dir.mkdir(parents=True, exist_ok=True)

        # ⑤ git init + remote (skip init if the repo already exists; update remote url if needed)
        if not (working_dir / ".git").exists():
            subprocess.run(
                ["git", "init", "-b", "main"],
                cwd=working_dir, check=True, capture_output=True,
            )
        # Disable filemode tracking. Docker Desktop on Windows can't reliably
        # persist Unix +x bits through bind-mounts, so the alpine chmod sidecar
        # (`_chmod_workspace_via_alpine` runs `chmod -R a+rwX`) leaves files at
        # 0644 from git's perspective even when they were 0755 on disk before.
        # Without this, every coder session's post-coder pipeline trips on
        # `git checkout -B sprint/X origin/sprint/X failed: Your local changes
        # to the following files would be overwritten by checkout` for any
        # script with +x, blocking commits + pushes for the entire product.
        # Idempotent — safe to re-run.
        subprocess.run(
            ["git", "config", "core.fileMode", "false"],
            cwd=working_dir, capture_output=True,
        )
        # `remote add` fails if origin already exists — use set-url as a fallback so the
        # scaffold re-runs cleanly.
        add = subprocess.run(
            ["git", "remote", "add", "origin", https_url],
            cwd=working_dir, capture_output=True,
        )
        if add.returncode != 0:
            subprocess.run(
                ["git", "remote", "set-url", "origin", https_url],
                cwd=working_dir, check=True, capture_output=True,
            )

        # ⑥ Write scaffold files
        _write_readme(working_dir, product_name)
        _write_product_config(working_dir, vision, preferred_stack, org, github_repo_name)

        # ⑥a Initial commit + push so `main` exists on GitHub. Without this,
        # the first coder session pushes its feature branch BEFORE main has a
        # ref upstream — `gh pr create` then fails with "No commits between
        # main and …, Base ref must be a branch" and features sit in
        # Implementing indefinitely. Pushing main here closes the race.
        try:
            # Need a committer identity for `git commit` to succeed.
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
            # If nothing to commit (re-running scaffold over an existing repo) skip push.
            if commit.returncode == 0:
                # Push with the PAT-embedded URL inline so it isn't persisted
                # to .git/config. Sets upstream tracking using the plain
                # origin URL (`-u origin main` would re-write the URL).
                push = subprocess.run(
                    ["git", "push", push_url, "main:main"],
                    cwd=working_dir, capture_output=True, text=True, timeout=60,
                )
                # Set upstream tracking explicitly so subsequent `git push`
                # without args still works.
                if push.returncode == 0:
                    subprocess.run(
                        ["git", "branch", "--set-upstream-to=origin/main", "main"],
                        cwd=working_dir, capture_output=True,
                    )
                if push.returncode != 0:
                    log.warning(f"Scaffold [{product_name}]: initial push failed: {push.stderr.strip()[:300]}")
                else:
                    log.info(f"Scaffold [{product_name}]: pushed initial main commit")
            else:
                log.info(f"Scaffold [{product_name}]: no initial commit needed ({commit.stdout.strip()[:120]})")
        except Exception as ce:
            log.warning(f"Scaffold [{product_name}]: initial commit/push step failed (non-fatal): {ce}")

        # ⑦ Create AI-suggested features as Pending
        for i, feat in enumerate(suggested_features or []):
            name = (feat.get("name") or "").strip()
            desc = (feat.get("description") or "").strip()
            if name:
                _post_feature(pm_api_url, product["id"], name, desc, priority=50 + i)

        # ⑧ Update product → registered (poller discovery takes over next cycle)
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

def _create_github_repo(org: str, repo_name: str, pat: str) -> tuple[str, str]:
    """
    Create a private GitHub repo under the org (falls back to personal account).
    Returns (ssh_clone_url, actual_owner).
    """
    headers = {
        "Authorization": f"token {pat}",
        "Accept": "application/vnd.github.v3+json",
    }
    payload = {"name": repo_name, "private": True, "auto_init": False}

    with httpx.Client(timeout=30) as client:
        # Try org endpoint first
        resp = client.post(
            f"https://api.github.com/orgs/{org}/repos",
            headers=headers, json=payload,
        )
        if resp.status_code in (404, 403, 422) and "already exists" not in resp.text:
            # Org not found / no access → fall back to personal account
            log.warning(f"Org repo creation failed ({resp.status_code}) — falling back to user repos")
            resp = client.post(
                "https://api.github.com/user/repos",
                headers=headers, json=payload,
            )
        if resp.status_code == 422 and "already exists" in resp.text:
            log.warning(f"Repo {org}/{repo_name} already exists on GitHub — reusing it")
            return f"git@github.com:{org}/{repo_name}.git", org
        resp.raise_for_status()
        data = resp.json()
        actual_owner = data["owner"]["login"]
        log.info(f"Created GitHub repo: {actual_owner}/{repo_name}")
        return data["ssh_url"], actual_owner


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
