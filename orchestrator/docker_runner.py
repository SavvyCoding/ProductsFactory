"""
Launches a Claude Code session inside an isolated Docker container.

Security model:
  - Isolated bridge network (productfactory-net) — NOT --network host
  - ~/.claude mounted read-only (OAuth session, never writable by container)
  - ~/.ssh mounted read-only (per-repo deploy key injected separately)
  - /workspace bound to product working_dir only
  - No --privileged
  - pm-api resolves to Windows host via host-gateway (PM website in Docker)
"""

import os
import uuid
import subprocess
import logging
from pathlib import Path

from orchestrator.prompts import build_prompt
from orchestrator.alerts import send_alert

log = logging.getLogger("poller.docker")

CLAUDE_DIR  = Path(os.environ.get("CLAUDE_DIR",  "C:/Users/digvi/.claude"))
SSH_DIR     = Path(os.environ.get("SSH_DIR",     "C:/Users/digvi/.ssh"))
AGENT_IMAGE = os.environ.get("AGENT_IMAGE", "productfactory-agent")
PM_API_URL  = os.environ["PM_API_URL"]

# Timeout: kill container if it runs longer than this (minutes → seconds)
SESSION_TIMEOUT_SECONDS = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "90")) * 60

# Deploy key filename inside SSH_DIR.
# Each product repo has its own key: id_ed25519_{product_name}
# The PM generates this key and adds it as a GitHub deploy key.
DEPLOY_KEY_FILENAME = os.environ.get("DEPLOY_KEY_FILENAME", "id_ed25519_productfactory")


def _get_deploy_key_path(product: dict) -> Path | None:
    """
    Returns the deploy key path for this product, falling back to the default key.
    Mounting only the key file (not the whole .ssh dir) preserves the
    known_hosts baked into the image.
    """
    # Per-product key: id_ed25519_{product_name_snake}
    name_slug = (product.get("name") or "").lower().replace(" ", "_").replace("-", "_")
    per_product = SSH_DIR / f"id_ed25519_{name_slug}"
    if per_product.exists():
        return per_product
    # Fall back to the default ProductFactory deploy key
    default_key = SSH_DIR / DEPLOY_KEY_FILENAME
    if default_key.exists():
        return default_key
    log.warning(f"No deploy key found for product '{product.get('name')}' — git push may fail")
    return None


def run_claude_in_docker(product: dict) -> int:
    """
    Launches the agent container. Blocks until container exits.
    Returns Docker exit code (0 = clean, non-zero = crash/auth failure).
    """
    session_uid = str(uuid.uuid4())[:8]
    working_dir = product["working_dir"]
    prompt = build_prompt(product, session_uid)

    lock_path = Path(working_dir) / "session.lock"
    if lock_path.exists():
        log.warning(f"session.lock exists for {product['name']} — skipping (duplicate launch guard)")
        return 1

    # Mount only the deploy key, not the whole .ssh directory.
    # This preserves the known_hosts baked into the image.
    deploy_key = _get_deploy_key_path(product)
    ssh_mount = []
    if deploy_key:
        ssh_mount = ["-v", f"{deploy_key}:/root/.ssh/id_ed25519:ro"]

    cmd = [
        "docker", "run", "--rm",
        "--name", f"pf-{product['id']}-{session_uid}",
        "--network", "productfactory-net",
        "--add-host", "pm-api:host-gateway",  # resolves to Windows host where pm-api container exposes :8080
        "--memory", "4g",
        "--cpus", "2",
        "-v", f"{working_dir}:/workspace",
        "-v", f"{CLAUDE_DIR}:/root/.claude:ro",   # read-only — OAuth session
        *ssh_mount,                                # deploy key :ro (not whole .ssh dir)
        "-e", f"PM_API_URL={PM_API_URL}",
        "-e", f"SESSION_UID={session_uid}",
        AGENT_IMAGE,
        "claude", "--dangerously-skip-permissions", "-p", prompt,
    ]

    log.info(f"docker run: session={session_uid} product={product['name']}")

    try:
        result = subprocess.run(cmd, timeout=SESSION_TIMEOUT_SECONDS)
        return result.returncode
    except subprocess.TimeoutExpired:
        log.error(f"Session timed out after {SESSION_TIMEOUT_SECONDS}s — killing container")
        subprocess.run(["docker", "kill", f"pf-{product['id']}-{session_uid}"], capture_output=True)
        send_alert("error", f"{product['name']}: session timed out after {SESSION_TIMEOUT_SECONDS//60}m")
        return 1
    except Exception as e:
        log.exception(f"docker run failed: {e}")
        return 1
