"""
Distributed poller-lock — partial Phase 4 extraction (option A).

Owns the two lock functions whose only state is the immutable per-process
identity (PID + hostname): _acquire_db_lock and _heartbeat_loop. Plus the
shared lock-stolen signal Event.

Stays in poller.py (not extracted): _release_db_lock and the mutable
_hb_stop global. Reason: _hb_stop is rebound by main() at runtime
(`global _hb_stop; _hb_stop = threading.Event()`) and the only reader is
_release_db_lock. Hoisting _release_db_lock here would put it in a different
module namespace than the global it reads, silently neutralising the
heartbeat-stop signal at process shutdown. Keeping both together preserves
behavior exactly. INVARIANTS I.4 (release never raises) is enforced by the
caller in poller.py.

INVARIANTS I.1–I.3 are enforced by the functions in this module.

Extracted from poller.py during Phase 4 of OrchestratorRefactor.
"""

import logging
import os
import socket
import threading

import httpx

log = logging.getLogger("poller")

PM_API_URL = os.environ["PM_API_URL"]

# These must stay in sync with the server's TTL logic in /api/poller/heartbeat.
# Do NOT make them env-configurable without also updating the server endpoint.
_HEARTBEAT_INTERVAL = 15   # seconds between heartbeat updates
_LOCK_TTL           = 30   # seconds - stale lock threshold (must match server)

_LOCK_PID  = os.getpid()
_LOCK_HOST = socket.gethostname()

# Set by _heartbeat_loop on 404; main() in poller.py reads this each cycle.
_hb_lock_stolen = threading.Event()


def _acquire_db_lock() -> bool:
    """
    Atomically acquire the poller distributed lock via PM API.
    Uses a single PostgreSQL UPDATE WHERE so two callers can never both succeed.
    Returns True on success, False if another live poller holds the lock.
    Falls back to True (allow start) if the API is unreachable - better to risk
    a duplicate than to prevent all pollers from ever starting.

    On 409, if the holder is on the same host but the PID is dead, force-unlock
    and retry once — recovers from hard-crashed pollers without waiting for the
    30s TTL.
    """
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=10) as client:
            resp = client.post("/api/poller/lock", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
            if resp.status_code == 200:
                log.info(f"Poller lock acquired (pid={_LOCK_PID}, host={_LOCK_HOST})")
                return True
            if resp.status_code == 409:
                d = resp.json().get("detail", {})
                holder_pid  = d.get("holder_pid")
                holder_host = d.get("holder_host")
                # Same-host stale-PID recovery: probe the holder PID locally.
                if holder_host == _LOCK_HOST and isinstance(holder_pid, int) and holder_pid != _LOCK_PID:
                    try:
                        os.kill(holder_pid, 0)
                        # Holder is alive — genuine conflict.
                    except OSError:
                        log.warning(
                            f"Lock holder pid={holder_pid} on this host is dead — "
                            f"force-unlocking and retrying."
                        )
                        client.post("/api/poller/force-unlock")
                        retry = client.post(
                            "/api/poller/lock",
                            json={"pid": _LOCK_PID, "host": _LOCK_HOST},
                        )
                        if retry.status_code == 200:
                            log.info(f"Poller lock acquired after force-unlock (pid={_LOCK_PID})")
                            return True
                log.error(
                    f"Another poller holds the lock - "
                    f"pid={holder_pid}, host={holder_host}, "
                    f"last_heartbeat={d.get('heartbeat_at')}. Exiting."
                )
                return False
            log.error(f"Unexpected response from lock endpoint: {resp.status_code} - allowing start")
            return True
    except Exception as e:
        log.warning(f"Could not acquire DB lock ({e}) - allowing start (API may be starting up)")
        return True


def _heartbeat_loop(stop_event: threading.Event):
    """
    Background thread: refreshes the DB lock heartbeat every 15s.
    If the API returns 404, the lock was stolen (another poller took over) - signal the
    main thread to abort the current cycle and re-acquire the lock or exit.
    """
    while not stop_event.wait(_HEARTBEAT_INTERVAL):
        try:
            with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
                resp = client.post("/api/poller/heartbeat", json={"pid": _LOCK_PID, "host": _LOCK_HOST})
            if resp.status_code == 404:
                log.critical("Heartbeat 404 - lock was stolen or expired. Signalling main thread to abort cycle.")
                _hb_lock_stolen.set()
            elif resp.status_code != 200:
                log.warning(f"Heartbeat unexpected status: {resp.status_code}")
        except Exception as e:
            log.warning(f"Heartbeat failed: {e}")
