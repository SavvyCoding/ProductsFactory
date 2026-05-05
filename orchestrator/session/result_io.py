"""
session_result.json read/write/live-poll.

Agents write feature state transitions to session_result.json (newline-delimited
JSON, one entry per line). The orchestrator drains this file:
  - in real time via _live_poll_session_result (background thread, every 30s)
  - in a final pass after the container exits (handled by the reconciler)

Extracted from docker_runner.py during Phase 1 of OrchestratorRefactor.
"""

import json
import logging
import os
import threading
from pathlib import Path

import httpx

from orchestrator.session.state_machine import _apply_session_entry

log = logging.getLogger("poller.docker")

PM_API_URL = os.environ["PM_API_URL"]


def _read_session_result(working_dir: str) -> list[dict]:
    """
    Read and parse session_result.json (newline-delimited JSON — one entry per line).
    Returns list of feature dicts. Skips blank or malformed lines.

    Also handles the wrapped format where an agent writes a single JSON object with a
    top-level "features" array instead of one object per line, e.g.:
        {"features": [{"id": 42, "status": "Reviewed", ...}, ...]}
    Such entries are unpacked into individual feature dicts.
    """
    result_file = Path(working_dir) / "session_result.json"
    if not result_file.exists():
        return []
    entries = []
    try:
        for line in result_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # Bare JSON array written by some agent versions: [{"id":42,...}, ...]
                if isinstance(obj, list):
                    log.warning(f"[session_result] Unwrapping bare JSON array ({len(obj)} entries)")
                    entries.extend(e for e in obj if isinstance(e, dict))
                # Wrapped {"features": [...]} format
                elif isinstance(obj, dict) and "features" in obj and isinstance(obj["features"], list) and "id" not in obj:
                    log.warning(f"[session_result] Unwrapping nested 'features' array ({len(obj['features'])} entries)")
                    entries.extend(e for e in obj["features"] if isinstance(e, dict))
                elif isinstance(obj, dict):
                    entries.append(obj)
                # else: skip non-dict, non-list top-level values
            except Exception:
                pass  # skip malformed lines (e.g. partial write mid-line)
    except Exception as e:
        log.warning(f"Could not read session_result.json: {e}")
    return entries


def _delete_session_result(working_dir: str) -> None:
    try:
        (Path(working_dir) / "session_result.json").unlink(missing_ok=True)
    except Exception:
        pass


def _live_poll_session_result(working_dir: str, stop_event: threading.Event, persona: str = "") -> None:
    """
    Background thread: polls session_result.json every 30 s while the container runs.
    Applies new NDJSON lines to the DB in real-time as the agent writes phase transitions.
    Tracks applied lines by index so each entry is applied exactly once.

    On shutdown (stop_event set) performs a final drain pass so any entries written
    between the last tick and container exit are not silently lost.
    """
    result_file = Path(working_dir) / "session_result.json"
    applied_up_to = 0  # number of lines already applied this session

    def _drain_new(label: str) -> None:
        nonlocal applied_up_to
        if not result_file.exists():
            return
        try:
            lines = result_file.read_text(encoding="utf-8").splitlines()
        except Exception:
            return
        new_lines = lines[applied_up_to:]
        if not new_lines:
            return
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
                for line in new_lines:
                    line = line.strip()
                    if not line:
                        applied_up_to += 1
                        continue
                    try:
                        entry = json.loads(line)
                        # Reviewer must not set Reviewing (pre-claim artifact).
                        if persona == "reviewer" and entry.get("status") == "Reviewing":
                            log.debug(f"[{label}] Skipping reviewer Reviewing entry for feature #{entry.get('id')}")
                        # Coder/reviewer must not write Pushed — PRs must go through GitHub merge.
                        elif persona in ("coder", "reviewer") and entry.get("status") == "Pushed":
                            log.warning(f"[{label}] Blocked agent-written Pushed for feature #{entry.get('id')} (persona={persona}) — PRs must merge via GitHub")
                        else:
                            _apply_session_entry(client, entry)
                    except Exception:
                        pass  # malformed line — skip, don't block the rest
                    applied_up_to += 1
        except Exception as e:
            log.debug(f"[{label}] PM API error: {e}")

    while not stop_event.wait(30):  # poll every 30 s; exits when stop_event is set
        _drain_new("live-poll")

    # Final drain — catches entries written between the last tick and stop_event.
    # Reconcile will re-apply these idempotently but running it here ensures the
    # DB reaches a consistent state even if reconcile is short-circuited by an
    # exception, and gives the user faster feedback in the UI.
    _drain_new("live-poll-final")
