"""
Host-side Docker invocations the orchestrator makes outside of running agents.

Today this is just the alpine chmod helper, but the module is the home for any
future "docker run / docker ps / docker kill" plumbing the orchestrator owns.
The agent-container build itself (run_claude_in_docker) is intentionally NOT
in here yet — that lives in docker_runner.py until Phase 3 splits it.

Extracted from docker_runner.py during Phase 2 of OrchestratorRefactor.
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("poller.docker")


def _chmod_workspace_via_alpine(working_dir: str, product_name: str = "?") -> None:
    """
    Make the workspace readable/writable to the orchestrator UID after an agent
    session left files owned by uid 1001. Runs an alpine container as root via
    the host docker socket — the only path that works on Windows bind-mounts
    (in-container `chmod` returns EPERM even as root). See commit b301048.

    Translates the orchestrator-container view (/products/X) to the host view
    via PRODUCTS_BASE_DIR so the bind mount on the throwaway alpine resolves
    to the same Windows path the agent container saw. In legacy host-poller
    mode (working_dir already host-side) the translation is a no-op.
    """
    try:
        wd = Path(working_dir)
        try:
            host_base = os.environ.get("PRODUCTS_BASE_DIR", "").rstrip("/\\")
            rel = wd.relative_to("/products")
            host_wd_path = f"{host_base}/{rel}" if host_base else str(wd)
        except ValueError:
            # working_dir isn't under /products — assume it's already a host path
            host_wd_path = str(wd)
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_wd_path}:/ws",
             "alpine", "sh", "-c", "chmod -R a+rwX /ws 2>/dev/null || true"],
            capture_output=True, timeout=30,
        )
    except Exception:
        log.warning(f"[{product_name}] alpine chmod helper failed (non-fatal)")
