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


def _resolve_host_path(path: str) -> str:
    """Translate orchestrator-container path (/products/X) to host bind-mount source.

    No-op when the path is already host-side (legacy host-poller mode) or when
    PRODUCTS_BASE_DIR is unset.
    """
    p = Path(path)
    try:
        host_base = os.environ.get("PRODUCTS_BASE_DIR", "").rstrip("/\\")
        rel = p.relative_to("/products")
        return f"{host_base}/{rel}" if host_base else str(p)
    except ValueError:
        return str(p)


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
        host_wd_path = _resolve_host_path(working_dir)
        subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_wd_path}:/ws",
             "alpine", "sh", "-c", "chmod -R a+rwX /ws 2>/dev/null || true"],
            capture_output=True, timeout=30,
        )
    except Exception:
        log.warning(f"[{product_name}] alpine chmod helper failed (non-fatal)")


def _rm_path_via_alpine(working_dir: str, relative_path: str, product_name: str = "?") -> bool:
    """
    Force-delete a single file inside the workspace via an alpine sidecar.
    Used as a fallback for `_delete_session_result` when the orchestrator
    process can't unlink an agent-owned file (UID drift on Windows bind-
    mounts: file is mode 644 owned by 1001, orchestrator runs as a different
    uid → EACCES). Same trick as `_chmod_workspace_via_alpine`.

    `relative_path` is joined onto the workspace root inside the alpine
    container; pass a leaf filename like ``session_result.json``, not an
    absolute path. Returns True on success, False otherwise — caller logs.
    """
    try:
        host_wd_path = _resolve_host_path(working_dir)
        # Single-quote the relative path inside the shell command so spaces
        # and shell-meta characters in agent-written filenames can't escape.
        # Refuse a path with a single quote in it (vanishingly rare, would
        # need explicit handling — fail loud rather than mis-interpret).
        if "'" in relative_path:
            log.warning(f"[{product_name}] _rm_path_via_alpine refused suspicious path: {relative_path!r}")
            return False
        r = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_wd_path}:/ws",
             "alpine", "sh", "-c", f"rm -f '/ws/{relative_path}'"],
            capture_output=True, timeout=30,
        )
        return r.returncode == 0
    except Exception as e:
        log.warning(f"[{product_name}] alpine rm helper failed: {e}")
        return False
