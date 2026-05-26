"""
Host ↔ container path translation for orchestrator-container mode.

When the poller runs on the Windows host (legacy mode), every path used by
docker_runner is already a valid host path — `docker run -v {path}:/...` just
works. These translators are no-ops.

When the poller runs inside the orchestrator container, there are two path spaces:

- **Host paths** — what the Docker daemon sees (e.g. `C:/Users/digvi/.../Calculator`
  on Windows, `/home/user/products/Calculator` on Linux). These are what
  `docker run -v` mount sources must be.
- **Container paths** — what the orchestrator Python process sees through its
  bind mounts (`/products/Calculator`, `/home/orchestrator/.claude`, etc).
  These are what `open()`, `Path.mkdir()`, subprocess `cwd=` need.

This module exposes:

- `host_path(container_path)` — translate a container-side path back to the
  host-side path, for use in `docker run -v` mount args.
- `container_path(host_path)` — translate a host-side path (e.g. a working_dir
  loaded from the DB) to the container-side path for local file ops.

Both are no-ops if the relevant env vars aren't set. This lets the same
docker_runner.py work in both host-mode and container-mode.

Required env vars when running inside the orchestrator container:
    PRODUCTS_BASE_DIR           — host path root (same as before)
    PRODUCTS_MOUNT_PATH         — container mount point for products root
    CLAUDE_DIR_HOST             — host path of Claude creds dir
    CLAUDE_DIR                  — container mount point (same env var reused)
    SSH_DIR_HOST                — host path of SSH dir
    SSH_DIR                     — container mount point
    ORCHESTRATOR_STAGING_HOST      — host path of the /tmp staging dir
    ORCHESTRATOR_STAGING_CONTAINER — container mount point (TMPDIR lives here)
"""

from __future__ import annotations

import os


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/")


# Pre-compute mappings once on import. None if not in container mode.
def _build_mappings() -> list[tuple[str, str]]:
    """Return list of (container_prefix, host_prefix) pairs, longest prefix first."""
    pairs: list[tuple[str, str]] = []
    mapping_env = [
        ("PRODUCTS_MOUNT_PATH",            "PRODUCTS_BASE_DIR"),
        ("CLAUDE_DIR",                     "CLAUDE_DIR_HOST"),
        ("SSH_DIR",                        "SSH_DIR_HOST"),
        ("ORCHESTRATOR_STAGING_CONTAINER", "ORCHESTRATOR_STAGING_HOST"),
    ]
    for cvar, hvar in mapping_env:
        c = _norm(os.environ.get(cvar, ""))
        h = _norm(os.environ.get(hvar, ""))
        if c and h and c != h:
            pairs.append((c, h))
    # Longest prefix first so /home/orchestrator/.claude matches before /home
    pairs.sort(key=lambda x: len(x[0]), reverse=True)
    return pairs


_C2H = _build_mappings()
_H2C = [(h, c) for (c, h) in _C2H]
_H2C.sort(key=lambda x: len(x[0]), reverse=True)


def host_path(path) -> str:
    """Translate a container-visible path to its host-side equivalent."""
    if not path:
        return path
    p = _norm(path)
    for cprefix, hprefix in _C2H:
        if p == cprefix:
            return hprefix
        if p.startswith(cprefix + "/"):
            return hprefix + p[len(cprefix):]
    return str(path)


def container_path(path) -> str:
    """Translate a host-side path (e.g. a DB working_dir) to the container path."""
    if not path:
        return path
    p = _norm(path)
    for hprefix, cprefix in _H2C:
        if p == hprefix:
            return cprefix
        if p.startswith(hprefix + "/"):
            return cprefix + p[len(hprefix):]
    return str(path)


